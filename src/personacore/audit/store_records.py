"""Writing and reading the two record families — spec section 7, ADR-0004.

An audit record is what the core did; a transcript record is what was said.
They are separate tables with the same attribution columns (correlation id,
surface, owner) and they are handled together here because a change to one of
those columns is almost always a change to both, and reading the two INSERTs
side by side is the only way to notice when it is not.

Both queries build their WHERE clause out of fixed literals and bind every
value through a placeholder. That is the whole of the injection story on this
surface and it is worth keeping in one file.

Deliberately not here: anything about conversations. A transcript row's
``conversation_id`` is written by whoever claims it afterwards
(:mod:`personacore.audit.store_conversations`), never derived here, because a
writer that guessed at a thread would file words into one they were not said
in.

Privacy: this module's logging calls carry ids, surfaces and roles — never
content, never detail payloads (see this package's ``logging`` module).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from personacore.audit.logging import get_logger
from personacore.audit.models import (
    AuditRecord,
    Owner,
    Surface,
    TranscriptRecord,
)
from personacore.audit.store_base import StoreBase
from personacore.audit.store_rows import _iso, _row_to_audit, _row_to_transcript

_logger = get_logger(__name__)


class RecordsMixin(StoreBase):
    """The audit and transcript tables: one writer and one query each."""

    # -- writes -------------------------------------------------------------

    async def record_audit(self, record: AuditRecord) -> AuditRecord:
        return await asyncio.to_thread(self._insert_audit, record)

    def _insert_audit(self, record: AuditRecord) -> AuditRecord:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO audit_records (
                    record_id, correlation_id, ts_utc, surface,
                    owner_kind, owner_id, category, action,
                    risk_level, outcome, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.record_id),
                    record.correlation_id,
                    _iso(record.timestamp),
                    record.surface.value,
                    record.owner.kind.value,
                    record.owner.id,
                    record.category.value,
                    record.action,
                    record.risk_level.value if record.risk_level else None,
                    record.outcome.value,
                    json.dumps(record.detail),
                ),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        # No detail/argument payload logged here — action metadata only.
        _logger.info(
            "audit_record_stored",
            id=row_id,
            record_id=str(record.record_id),
            correlation_id=record.correlation_id,
            surface=record.surface.value,
            category=record.category.value,
            outcome=record.outcome.value,
        )
        return record.model_copy(update={"id": row_id})

    async def record_transcript(self, record: TranscriptRecord) -> TranscriptRecord:
        return await asyncio.to_thread(self._insert_transcript, record)

    def _insert_transcript(self, record: TranscriptRecord) -> TranscriptRecord:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO transcript_records (
                    record_id, correlation_id, ts_utc, surface,
                    owner_kind, owner_id, role, content, conversation_id,
                    author_name, author_kind, author_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.record_id),
                    record.correlation_id,
                    _iso(record.timestamp),
                    record.surface.value,
                    record.owner.kind.value,
                    record.owner.id,
                    record.role.value,
                    record.content,
                    record.conversation_id,
                    # Written exactly as given, or left NULL. Nothing here
                    # derives an author from the role: a store that called
                    # every assistant row a persona would attribute this
                    # core's own "I could not reach the model" to whichever
                    # character was picked, which is the one sentence a
                    # persona certainly did not say.
                    record.author.name if record.author else None,
                    record.author.kind.value if record.author else None,
                    record.author.model if record.author else None,
                ),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        # Content is deliberately absent from this log line — see module
        # docstring and personacore/audit/logging.py.
        _logger.info(
            "transcript_record_stored",
            id=row_id,
            record_id=str(record.record_id),
            correlation_id=record.correlation_id,
            surface=record.surface.value,
            role=record.role.value,
        )
        return record.model_copy(update={"id": row_id})

    # -- reads ----------------------------------------------------------

    async def query_audit(
        self,
        *,
        owner: Owner | None = None,
        surface: Surface | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        return await asyncio.to_thread(
            self._query_audit, owner, surface, correlation_id, since, until, limit
        )

    def _query_audit(
        self,
        owner: Owner | None,
        surface: Surface | None,
        correlation_id: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if owner is not None:
            clauses.append("owner_kind = ? AND owner_id = ?")
            params.extend([owner.kind.value, owner.id])
        if surface is not None:
            clauses.append("surface = ?")
            params.append(surface.value)
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if since is not None:
            clauses.append("ts_utc >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("ts_utc <= ?")
            params.append(_iso(until))

        # `where` is assembled only from the fixed clause literals above, never
        # from caller-supplied text; every actual value is bound through `?`.
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit_records {where} ORDER BY ts_utc DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [_row_to_audit(row) for row in rows]

    async def query_transcript(
        self,
        *,
        owner: Owner | None = None,
        surface: Surface | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[TranscriptRecord]:
        return await asyncio.to_thread(
            self._query_transcript, owner, surface, correlation_id, since, until, limit
        )

    def _query_transcript(
        self,
        owner: Owner | None,
        surface: Surface | None,
        correlation_id: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[TranscriptRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if owner is not None:
            clauses.append("owner_kind = ? AND owner_id = ?")
            params.extend([owner.kind.value, owner.id])
        if surface is not None:
            clauses.append("surface = ?")
            params.append(surface.value)
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if since is not None:
            clauses.append("ts_utc >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("ts_utc <= ?")
            params.append(_iso(until))

        # Same reasoning as query_audit above: `where` is fixed literals only.
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM transcript_records {where} ORDER BY ts_utc DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [_row_to_transcript(row) for row in rows]
