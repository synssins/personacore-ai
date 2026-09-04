"""The shape of `memory.db`, and the ladder that gets it there (contract
§3.3): the same shape `audit/store_schema.py` uses -- a `schema_version`
table plus an ordered, append-only list of migration functions, applied and
version-bumped as one transaction each by `store.py`'s `_migrate`.

**The list is append-only and the numbers are permanent**, for the same
reason `audit/store_schema.py` gives: a household's `memory.db` may already
have run an old migration, and rewriting it makes this build's idea of
"version 1" different from that database's.

Nothing here opens a connection or runs anything outside a migration
function's own `conn` argument -- `store.py`'s `MemoryStore._migrate` owns
the transaction, the `schema_version` bump, and the decision to run at all.

The vector table's width is not a literal: `memory_vectors` is created with
`FLOAT[{dimensions}]`, where `dimensions` is the embedder's own
`dimensions` attribute (384 today, contract §4) rather than a number typed
twice in two files that could quietly drift apart.
"""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]


class MemorySchemaError(RuntimeError):
    """Base for `memory.db` schema problems this store refuses to paper
    over -- the same refusal `audit/store_schema.py`'s errors document, for
    the same reason (spec §7: appdata format changes are explicit).
    """


class SchemaDowngradeError(MemorySchemaError):
    """The on-disk `schema_version` is ahead of what this build's migration
    list knows how to produce -- the database was last opened by a newer
    PersonaCore build. Refusing beats silently running unreviewed code
    against a schema this build has never seen.
    """


class MigrationFailedError(MemorySchemaError):
    """A migration step's DDL failed to apply cleanly -- most often a
    partially-applied migration from a run that crashed between the DDL and
    the `schema_version` bump. See `MemoryStore._run_migration_step`.
    """


def _migration_0001_initial_schema(conn: sqlite3.Connection, *, dimensions: int) -> None:
    """Version 1: `memories`, `memory_vectors`, `review_marks` (contract
    §3.1, §3.2, §5.2). Indexes per contract §3.1: `(owner, holder,
    last_used_at)` for the recall and dedupe filters, `(holder, created_at)`
    for the screen's per-holder newest-first listing.
    """
    conn.execute(
        """
        CREATE TABLE memories (
            memory_id        TEXT PRIMARY KEY,
            text             TEXT NOT NULL,
            owner            TEXT NOT NULL,
            holder           TEXT NOT NULL,
            importance       REAL NOT NULL,
            created_at       TEXT NOT NULL,
            last_used_at     TEXT NOT NULL,
            use_count        INTEGER NOT NULL,
            written_by       TEXT NOT NULL,
            written_persona  TEXT NOT NULL,
            written_model    TEXT NOT NULL,
            conversation_id  TEXT,
            correlation_id   TEXT NOT NULL,
            edited_by        TEXT,
            edited_at        TEXT,
            promoted_by      TEXT,
            promoted_at      TEXT,
            truncated        INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_memories_owner_holder ON memories (owner, holder, last_used_at)"
    )
    conn.execute("CREATE INDEX idx_memories_holder_created ON memories (holder, created_at)")

    # sqlite-vec virtual table: keyed by the same `memory_id`, one row per
    # memory, written and deleted in the same transaction as the row above
    # (contract §3.2). `dimensions` is trusted -- it is the embedder's own
    # attribute, never request input -- so the f-string is not a query
    # built from anything a caller supplies.
    conn.execute(
        "CREATE VIRTUAL TABLE memory_vectors USING vec0("
        f"memory_id TEXT PRIMARY KEY, embedding FLOAT[{dimensions}])"  # noqa: S608
    )

    conn.execute(
        """
        CREATE TABLE review_marks (
            conversation_id  TEXT NOT NULL,
            persona          TEXT NOT NULL,
            mark             TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            PRIMARY KEY (conversation_id, persona)
        )
        """
    )


def _migration_0002_written_owner(conn: sqlite3.Connection) -> None:
    """Version 2: `memories.written_owner` -- the owner at write time,
    never touched by promote (contract §8's promoted-row conversation
    link; the owner column itself becomes `household` on promote, which
    left review with no person to open). Existing rows get `''`, which the
    screen reads as "no link" -- the same "fall back to no link" the
    contract asks for on a database that ran only version 1.
    """
    conn.execute("ALTER TABLE memories ADD COLUMN written_owner TEXT NOT NULL DEFAULT ''")


def _migration_0003_review_runs(conn: sqlite3.Connection) -> None:
    """Version 3: `review_runs` -- the review log the Memory screen reads.

    One row per `DueReview` a tick attempted, whatever the outcome
    (`written` / `nothing` / `parse_failed` / `model_failed` / `skipped`).
    `kept_json` and `dropped_json` are JSON text, not further-normalised
    tables: nothing here is ever queried by their contents, only listed
    newest-first and purged by age, so a table per item would buy nothing
    a text column does not already give for free.

    `finished_at` is indexed for the screen's newest-first listing and for
    `purge_short_term`'s age cutoff -- the same two reasons `memories` gets
    `(holder, created_at)`.
    """
    conn.execute(
        """
        CREATE TABLE review_runs (
            run_id           TEXT PRIMARY KEY,
            conversation_id  TEXT NOT NULL,
            persona          TEXT NOT NULL,
            owner            TEXT NOT NULL,
            started_at       TEXT NOT NULL,
            finished_at      TEXT NOT NULL,
            model            TEXT,
            outcome          TEXT NOT NULL,
            written          INTEGER NOT NULL,
            touched          INTEGER NOT NULL,
            dropped          INTEGER NOT NULL,
            kept_json        TEXT NOT NULL,
            dropped_json     TEXT NOT NULL,
            error            TEXT
        )
        """
    )
    conn.execute("CREATE INDEX idx_review_runs_finished ON review_runs (finished_at)")


def build_migrations(dimensions: int) -> list[Migration]:
    """The migration ladder, bound to this embedder's vector width.

    A plain module-level list (like `audit/store_schema.py`'s
    `_MIGRATIONS`) cannot carry `dimensions` -- it is only known once the
    embedder handed to `MemoryStore.__init__` exists. `functools.partial`
    keeps every entry a plain `Migration` (one `conn` argument) so the
    ladder-climbing in `store.py` reads exactly like the audit store's.
    """
    return [
        functools.partial(_migration_0001_initial_schema, dimensions=dimensions),
        _migration_0002_written_owner,
        _migration_0003_review_runs,
    ]
