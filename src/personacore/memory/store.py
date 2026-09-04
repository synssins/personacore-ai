"""`MemoryStore` -- joint J4 of `working/PLAN-memory.md`, built to
`working/contracts/memory.md` §3, §6, §7, §11.

One SQLite file (`appdata/memory/memory.db`), `sqlite-vec` loaded as an
extension for the vector column, and one connection guarded by one lock --
the same shape `audit/store_base.py` and `audit/store_schema.py` document,
`asyncio.to_thread` carrying every blocking call off the event loop.

Two things this module deliberately does not import: `personacore.config.
memory.MemorySettings` and `personacore.memory.embed.Embedder`. Both are
being written by other agents in the same wave (Task 1 and Task 2 of the
plan); importing either here would make this task wait on work it does not
need. Instead `SettingsLike` and `EmbedderLike` below name exactly the
attributes this store reads, as `typing.Protocol`s -- the real classes will
satisfy them structurally once they exist, and the fake embedder the tests
build satisfies `EmbedderLike` today without depending on either.

**Never logs memory text.** Nothing in this module calls `get_logger`;
there is no log line here for text to leak into, on purpose.

**Filtering by owner and holder is a SQL clause, never a Python filter
after the fact** (contract §6: "The filter is in the SQL. There is no code
path that reads another person's or another persona's memories."). Both
`_find_similar_locked` (dedupe, `add`) and `_recall` restrict the vector
search itself with `memory_id IN (SELECT memory_id FROM memories WHERE
...)` -- verified against the installed sqlite-vec 0.1.9 to combine cleanly
with the `MATCH ... ORDER BY distance LIMIT ?` a `vec0` KNN query requires.
A `WHERE embedding MATCH ? AND owner = ? ...` joined directly against
`memories` was tried first and rejected: `vec0` refuses any query that
mixes `MATCH` with a plain equality filter from a joined table ("A LIMIT or
'k = ?' constraint is required on vec0 knn queries"), so the eligible ids
have to be narrowed in a subquery instead.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import sqlite_vec

from personacore.memory.models import GLOBAL_HOLDER, HOUSEHOLD_OWNER, MAX_TEXT_CHARS, MemoryRecord
from personacore.memory.schema import (
    Migration,  # noqa: F401 - kept importable; mirrors audit/store.py's re-export shape
    SchemaDowngradeError,
    build_migrations,
)

_SENTENCE_ENDERS = (".", "!", "?")


class SettingsLike(Protocol):
    """The five attributes `MemoryStore` reads off `MemorySettings` (joint
    J1). A `Protocol` rather than an import so this task does not wait on
    the settings module -- see the module docstring.
    """

    quiet_minutes: int
    recall_limit: int
    half_life_days: float
    duplicate_threshold: float
    short_term_days: int


class EmbedderLike(Protocol):
    """The two attributes `MemoryStore` reads off `Embedder` (joint J3).
    Tests build a fake satisfying exactly this -- deterministic 384-d
    vectors from a seeded hash of the text -- so this task does not wait on
    the real ONNX embedder either.
    """

    dimensions: int

    def embed(self, text: str) -> list[float]: ...


def _iso(dt: datetime) -> str:
    """UTC ISO string for storage (contract §3.1: every timestamp column is
    "text, ISO UTC"). Naive datetimes are never expected here -- every
    timestamp this module mints is `datetime.now(UTC)`.
    """
    return dt.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_iso_opt(value: str | None) -> datetime | None:
    return _parse_iso(value) if value is not None else None


def truncate_text(text: str) -> tuple[str, bool]:
    """Cut `text` to `MAX_TEXT_CHARS` at the last sentence end at or before
    the cap; fall back to a hard cut when none is found (contract §3.1).

    Returns `(text, truncated)`. `truncated` is `False` when `text` was
    already within the cap, even if it happens to end mid-sentence -- only
    a cut this function performed counts.
    """
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    window = text[:MAX_TEXT_CHARS]
    cut = max(window.rfind(ender) for ender in _SENTENCE_ENDERS)
    if cut == -1:
        return window, True
    return window[: cut + 1], True


def _cosine_from_l2(distance: float) -> float:
    """`sqlite-vec`'s KNN distance is L2. For the unit-norm vectors this
    embedder always produces (J3: "L2-normalised"), cosine similarity is
    `1 - distance**2 / 2` -- verified today against the installed
    `sqlite-vec` 0.1.9 alongside the query shape above.
    """
    return 1.0 - (distance**2) / 2.0


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row["memory_id"],
        text=row["text"],
        owner=row["owner"],
        holder=row["holder"],
        importance=row["importance"],
        created_at=_parse_iso(row["created_at"]),
        last_used_at=_parse_iso(row["last_used_at"]),
        use_count=row["use_count"],
        written_by=row["written_by"],
        written_persona=row["written_persona"],
        written_model=row["written_model"],
        conversation_id=row["conversation_id"],
        correlation_id=row["correlation_id"],
        edited_by=row["edited_by"],
        edited_at=_parse_iso_opt(row["edited_at"]),
        promoted_by=row["promoted_by"],
        promoted_at=_parse_iso_opt(row["promoted_at"]),
        truncated=bool(row["truncated"]),
    )


class MemoryStore:
    """SQLite + sqlite-vec store for `memory.db`, one connection, one lock.

    Nothing touches disk until `open()` -- not even `__init__` -- so a
    persona with memory off (server.py never calls `open()` for it) leaves
    no file behind (contract §3: "Memory off for every persona means the
    file is never opened").
    """

    def __init__(self, path: Path, embedder: EmbedderLike, settings: SettingsLike) -> None:
        self._path = path
        self._embedder = embedder
        self._settings = settings
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed = True

    # -- lifecycle ----------------------------------------------------------

    async def open(self) -> None:
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        with self._lock:
            self._conn = conn
            self._closed = False
            self._migrate()

    def _migrate(self) -> None:
        """Caller holds `self._lock` and `self._conn` is set. Same ladder
        shape as `audit/store.py._migrate`; see `schema.py` for why the
        list is built rather than a module-level constant.
        """
        conn = self._require_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        conn.commit()
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")
            conn.commit()
            current = 0
        else:
            current = int(row["version"])

        migrations = build_migrations(self._embedder.dimensions)
        target = len(migrations)

        if current > target:
            raise SchemaDowngradeError(
                f"memory.db schema_version is {current}, but this build only "
                f"knows migrations up to version {target}. This database was "
                "last opened by a newer PersonaCore build."
            )

        for step_index in range(current, target):
            self._run_migration_step(step_index, migrations[step_index])

    def _run_migration_step(self, step_index: int, migration: Migration) -> None:
        """One migration and its `schema_version` bump as a single atomic
        unit -- identical technique to `audit/store.py._run_migration_step`
        and for the same reason: the connection's default isolation mode
        implicitly commits before DDL, which would split the transaction
        and reopen the partial-migration hole this closes.
        """
        conn = self._require_conn()
        previous_isolation_level = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                migration(conn)
                conn.execute("UPDATE schema_version SET version = ?", (step_index + 1,))
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.isolation_level = previous_isolation_level

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._conn is not None:
                self._conn.close()

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("MemoryStore.open() has not been called")
        return self._conn

    # -- writing --------------------------------------------------------

    async def add(
        self,
        *,
        text: str,
        owner: str,
        holder: str,
        written_by: str,
        written_persona: str,
        written_model: str,
        conversation_id: str | None,
        correlation_id: str,
        importance: float = 0.5,
    ) -> tuple[MemoryRecord, bool]:
        return await asyncio.to_thread(
            self._add,
            text,
            owner,
            holder,
            written_by,
            written_persona,
            written_model,
            conversation_id,
            correlation_id,
            importance,
        )

    def _add(
        self,
        text: str,
        owner: str,
        holder: str,
        written_by: str,
        written_persona: str,
        written_model: str,
        conversation_id: str | None,
        correlation_id: str,
        importance: float,
    ) -> tuple[MemoryRecord, bool]:
        truncated_text, was_truncated = truncate_text(text)
        vector = self._embedder.embed(truncated_text)
        now = datetime.now(UTC)
        with self._lock:
            candidate = self._find_similar_locked(vector, owner, holder)
            if candidate is not None:
                memory_id, distance = candidate
                if _cosine_from_l2(distance) >= self._settings.duplicate_threshold:
                    record = self._touch_locked(memory_id, now, importance=importance)
                    return record, False
            record = self._insert_locked(
                text=truncated_text,
                owner=owner,
                holder=holder,
                importance=importance,
                now=now,
                written_by=written_by,
                written_persona=written_persona,
                written_model=written_model,
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                truncated=was_truncated,
                vector=vector,
            )
            return record, True

    def _find_similar_locked(
        self, vector: list[float], owner: str, holder: str
    ) -> tuple[str, float] | None:
        """Nearest existing row for this exact owner+holder, or `None`.
        Caller holds `self._lock`. See the module docstring for why the
        restriction is a subquery rather than a join.
        """
        conn = self._require_conn()
        cur = conn.execute(
            """
            SELECT memory_id, distance FROM memory_vectors
            WHERE embedding MATCH ?
              AND memory_id IN (
                  SELECT memory_id FROM memories WHERE owner = ? AND holder = ?
              )
            ORDER BY distance LIMIT 1
            """,
            (json.dumps(vector), owner, holder),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["memory_id"], row["distance"]

    def _insert_locked(
        self,
        *,
        text: str,
        owner: str,
        holder: str,
        importance: float,
        now: datetime,
        written_by: str,
        written_persona: str,
        written_model: str,
        conversation_id: str | None,
        correlation_id: str,
        truncated: bool,
        vector: list[float],
    ) -> MemoryRecord:
        conn = self._require_conn()
        memory_id = secrets.token_hex(16)
        now_iso = _iso(now)
        conn.execute(
            """
            INSERT INTO memories (
                memory_id, text, owner, holder, importance, created_at,
                last_used_at, use_count, written_by, written_persona,
                written_model, conversation_id, correlation_id, edited_by,
                edited_at, promoted_by, promoted_at, truncated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
            """,
            (
                memory_id,
                text,
                owner,
                holder,
                importance,
                now_iso,
                now_iso,
                written_by,
                written_persona,
                written_model,
                conversation_id,
                correlation_id,
                int(truncated),
            ),
        )
        conn.execute(
            "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
            (memory_id, json.dumps(vector)),
        )
        conn.commit()
        record = self._get_locked(memory_id)
        if record is None:  # pragma: no cover - just inserted, under the same lock
            raise RuntimeError("memory row vanished immediately after insert")
        return record

    def _touch_locked(
        self, memory_id: str, now: datetime, *, importance: float | None = None
    ) -> MemoryRecord:
        """`last_used_at = now`, `use_count += 1`, and -- when `importance`
        is given -- the row's importance becomes the max of old and new
        (contract §5.1). Caller holds `self._lock`; the row must exist.
        """
        conn = self._require_conn()
        row = conn.execute(
            "SELECT importance FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        new_importance = row["importance"]
        if importance is not None:
            new_importance = max(new_importance, importance)
        conn.execute(
            """
            UPDATE memories
            SET last_used_at = ?, use_count = use_count + 1, importance = ?
            WHERE memory_id = ?
            """,
            (_iso(now), new_importance, memory_id),
        )
        conn.commit()
        record = self._get_locked(memory_id)
        if record is None:  # pragma: no cover - row existed one line above, under the same lock
            raise RuntimeError("memory row vanished during touch")
        return record

    # -- reading ----------------------------------------------------------

    async def recall(
        self,
        *,
        owner: str,
        holder: str,
        query: str,
        limit: int,
        include_global: bool = True,
    ) -> list[tuple[MemoryRecord, float]]:
        return await asyncio.to_thread(self._recall, owner, holder, query, limit, include_global)

    def _recall(
        self, owner: str, holder: str, query: str, limit: int, include_global: bool
    ) -> list[tuple[MemoryRecord, float]]:
        vector = self._embedder.embed(query)
        now = datetime.now(UTC)
        conn = self._require_conn()
        if include_global:
            predicate = "(owner = ? AND holder = ?) OR (holder = ?)"
            params: tuple[str, ...] = (owner, holder, GLOBAL_HOLDER)
        else:
            predicate = "owner = ? AND holder = ?"
            params = (owner, holder)
        candidate_limit = max(limit * 4, limit, 1)

        with self._lock:
            cur = conn.execute(
                f"""
                SELECT memory_id, distance FROM memory_vectors
                WHERE embedding MATCH ?
                  AND memory_id IN (SELECT memory_id FROM memories WHERE {predicate})
                ORDER BY distance LIMIT ?
                """,  # noqa: S608 - `predicate` is one of two fixed literals above, never input
                (json.dumps(vector), *params, candidate_limit),
            )
            candidates = cur.fetchall()

            scored: list[tuple[MemoryRecord, float]] = []
            for row in candidates:
                record = self._get_locked(row["memory_id"])
                if record is None:
                    continue
                cosine = _cosine_from_l2(row["distance"])
                days_since_used = max((now - record.last_used_at).total_seconds() / 86400.0, 0.0)
                recency = 0.2 + 0.8 * (0.5 ** (days_since_used / self._settings.half_life_days))
                scored.append((record, cosine * record.importance * recency))

            scored.sort(key=lambda pair: pair[1], reverse=True)
            top = scored[:limit]
            return [(self._touch_locked(record.memory_id, now), score) for record, score in top]

    async def get(self, memory_id: str) -> MemoryRecord | None:
        return await asyncio.to_thread(self._get, memory_id)

    def _get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            return self._get_locked(memory_id)

    def _get_locked(self, memory_id: str) -> MemoryRecord | None:
        conn = self._require_conn()
        row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        return _row_to_record(row) if row is not None else None

    async def list(
        self,
        *,
        owners: Sequence[str] | None = None,
        holders: Sequence[str] | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[MemoryRecord]:
        return await asyncio.to_thread(self._list, owners, holders, search, limit)

    def _list(
        self,
        owners: Sequence[str] | None,
        holders: Sequence[str] | None,
        search: str | None,
        limit: int,
    ) -> list[MemoryRecord]:
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[object] = []
        if owners:
            clauses.append(f"owner IN ({','.join('?' for _ in owners)})")
            params.extend(owners)
        if holders:
            clauses.append(f"holder IN ({','.join('?' for _ in holders)})")
            params.extend(holders)
        if search:
            clauses.append("text LIKE ?")
            params.append(f"%{search}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            cur = conn.execute(
                f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            )
            rows = cur.fetchall()
        return [_row_to_record(row) for row in rows]

    async def counts(self) -> dict[tuple[str, str], int]:
        return await asyncio.to_thread(self._counts)

    def _counts(self) -> dict[tuple[str, str], int]:
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT owner, holder, COUNT(*) AS n FROM memories GROUP BY owner, holder"
            ).fetchall()
        return {(row["owner"], row["holder"]): row["n"] for row in rows}

    # -- administrator actions ---------------------------------------------

    async def promote(self, memory_id: str, *, by: str) -> bool:
        return await asyncio.to_thread(self._promote, memory_id, by)

    def _promote(self, memory_id: str, by: str) -> bool:
        conn = self._require_conn()
        now_iso = _iso(datetime.now(UTC))
        with self._lock:
            cur = conn.execute(
                """
                UPDATE memories
                SET holder = ?, owner = ?, promoted_by = ?, promoted_at = ?
                WHERE memory_id = ?
                """,
                (GLOBAL_HOLDER, HOUSEHOLD_OWNER, by, now_iso, memory_id),
            )
            conn.commit()
            return cur.rowcount > 0

    async def edit(self, memory_id: str, *, text: str, by: str) -> bool:
        return await asyncio.to_thread(self._edit, memory_id, text, by)

    def _edit(self, memory_id: str, text: str, by: str) -> bool:
        truncated_text, was_truncated = truncate_text(text)
        vector = self._embedder.embed(truncated_text)
        conn = self._require_conn()
        now_iso = _iso(datetime.now(UTC))
        with self._lock:
            exists = conn.execute(
                "SELECT 1 FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if exists is None:
                return False
            conn.execute(
                """
                UPDATE memories
                SET text = ?, truncated = ?, edited_by = ?, edited_at = ?
                WHERE memory_id = ?
                """,
                (truncated_text, int(was_truncated), by, now_iso, memory_id),
            )
            conn.execute(
                "UPDATE memory_vectors SET embedding = ? WHERE memory_id = ?",
                (json.dumps(vector), memory_id),
            )
            conn.commit()
            return True

    async def delete(self, memory_id: str) -> bool:
        return await asyncio.to_thread(self._delete, memory_id)

    def _delete(self, memory_id: str) -> bool:
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            conn.commit()
            return cur.rowcount > 0

    async def purge_short_term(self, *, older_than_days: int) -> int:
        return await asyncio.to_thread(self._purge_short_term, older_than_days)

    def _purge_short_term(self, older_than_days: int) -> int:
        """Delete short-term rows (`holder != 'global'`) whose
        `last_used_at` is older than `older_than_days`, row and vector
        together (contract §7). A promoted (long-term) row is never
        touched here -- that is the entire point of `holder != 'global'`.
        """
        conn = self._require_conn()
        cutoff_iso = _iso(datetime.now(UTC) - timedelta(days=older_than_days))
        with self._lock:
            rows = conn.execute(
                "SELECT memory_id FROM memories WHERE holder != ? AND last_used_at < ?",
                (GLOBAL_HOLDER, cutoff_iso),
            ).fetchall()
            ids = [row["memory_id"] for row in rows]
            for memory_id in ids:
                conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM memories WHERE memory_id IN ({placeholders})",  # noqa: S608
                    ids,
                )
            conn.commit()
            return len(ids)

    # -- review marks -------------------------------------------------------

    async def review_mark(self, conversation_id: str, persona: str) -> str | None:
        return await asyncio.to_thread(self._review_mark, conversation_id, persona)

    def _review_mark(self, conversation_id: str, persona: str) -> str | None:
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT mark FROM review_marks WHERE conversation_id = ? AND persona = ?",
                (conversation_id, persona),
            ).fetchone()
        return row["mark"] if row is not None else None

    async def set_review_mark(self, conversation_id: str, persona: str, mark: str) -> None:
        await asyncio.to_thread(self._set_review_mark, conversation_id, persona, mark)

    def _set_review_mark(self, conversation_id: str, persona: str, mark: str) -> None:
        conn = self._require_conn()
        now_iso = _iso(datetime.now(UTC))
        with self._lock:
            conn.execute(
                """
                INSERT INTO review_marks (conversation_id, persona, mark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id, persona)
                DO UPDATE SET mark = excluded.mark, updated_at = excluded.updated_at
                """,
                (conversation_id, persona, mark, now_iso),
            )
            conn.commit()


__all__ = ["EmbedderLike", "MemoryStore", "SettingsLike", "truncate_text"]
