"""The audit and transcript store — spec section 7, section 8, section 9,
section 13.3, and ADR-0004.

Backed by stdlib `sqlite3` (CLAUDE.md dependency policy: no new dependency
without approval, and sqlite3 is stdlib). One file under appdata holds both
record families described in personacore/audit/models.py.

Migrations are explicit and versioned (a `schema_version` table plus an
ordered list of migration functions) rather than an ORM's auto-migration,
per spec section 7's requirement that appdata format changes be explicit and
documented.

Async-friendliness: the core is asyncio, and sqlite3 is a blocking library.
Every public method that touches disk hands the actual sqlite3 call to a
worker thread via `asyncio.to_thread` so it never blocks the event loop.
`AuditStore.__init__` itself (opening the file and running migrations) is the
one synchronous exception: it runs once at startup before the event loop is
serving requests, exactly like importing a module, so blocking there costs
nothing a caller would notice — this is the "document clearly why a given
call is safe to make synchronously" case the task brief allows for.

Privacy: this module's own logging calls never pass record content or detail
payloads as fields — only ids, categories, surfaces and counts. Transcript
content and audit detail belong in the store, never in the log stream (see
personacore/audit/logging.py's module docstring for the companion rule on
the logging side).

**What is left in this file, and why it is this** (ADR-0040). The store was one
1,973-line module; the operations now live beside the concern they serve, and
what stayed here is the part they all borrow: the connection, the lock that
makes one connection safe from many threads, and the ladder that gets the
database into a shape the operations can assume. Those three cannot be moved
apart from each other — the lock exists for the connection, and the migrations
are the connection's first act — and they cannot be moved away from the class,
because ``AuditStore`` is what constructs them.

The ladder-climbing (:meth:`AuditStore._migrate`) stays here for a second,
sharper reason: it reads ``_MIGRATIONS`` out of *this* module's namespace, and
a test upgrades a database from the previous build by replacing that name here.
Moving the reader without the name it reads would leave the test patching
something nothing looks at, and it would pass while proving nothing.

The pieces, and what each owns:

* :mod:`personacore.audit.store_schema` — the migration ladder itself, and what
  a schema this build cannot account for means.
* :mod:`personacore.audit.store_rows` — a stored row read back as a model, and
  the timestamps on both sides of that.
* :mod:`personacore.audit.store_base` — the four attributes every group of
  operations borrows from this class.
* :mod:`personacore.audit.store_records` — audit and transcript writes and
  queries.
* :mod:`personacore.audit.store_conversations` — starting, finding, reading and
  destroying a conversation.
* :mod:`personacore.audit.store_rooms` — what an owner can set on one: persona,
  roster, name, group, hidden.
* :mod:`personacore.audit.store_attachments` — the attachments table: insert,
  fetch-for-an-owner, delete, and finding what has been orphaned.
* :mod:`personacore.audit.store_backfill` — conversations for rows written
  before conversations existed. The one piece allowed to fail.
* :mod:`personacore.audit.store_retention` — the age-out pass.

They are mixed into one class rather than composed, because they are one
object: every method holds the same lock over the same connection, and handing
each group its own object would give the lock more than one holder, which is
the thing the lock exists to prevent.

Every name importable from this module before the split is still importable
from it. Callers should not have to know which file a name landed in, and the
underscore-prefixed ones are re-exported too because the tests reach for them.
"""

from __future__ import annotations

import sqlite3
import threading

from personacore.audit.logging import get_logger
from personacore.audit.models import (
    AuditStoreConfig,
    RetentionConfig,
)
from personacore.audit.store_attachments import AttachmentsMixin
from personacore.audit.store_backfill import (
    BACKFILL_BATCH,
    BackfillMixin,
)
from personacore.audit.store_base import StoreBase
from personacore.audit.store_conversations import ConversationsMixin
from personacore.audit.store_reasoning import ReasoningMixin
from personacore.audit.store_records import RecordsMixin
from personacore.audit.store_retention import RetentionMixin
from personacore.audit.store_rooms import RoomsMixin
from personacore.audit.store_rows import (
    _column,  # noqa: F401 - kept importable from this module; see the note below
    _conversations,  # noqa: F401 - kept importable from this module; see the note below
    _group_by_gap,  # noqa: F401 - kept importable from this module; see the note below
    _iso,  # noqa: F401 - kept importable from this module; see the note below
    _parse_iso,  # noqa: F401 - kept importable from this module; see the note below
    _row_to_audit,  # noqa: F401 - kept importable from this module; see the note below
    _row_to_author,  # noqa: F401 - kept importable from this module; see the note below
    _row_to_conversation,  # noqa: F401 - kept importable from this module; see the note below
    _row_to_roster,  # noqa: F401 - kept importable from this module; see the note below
    _row_to_transcript,  # noqa: F401 - kept importable from this module; see the note below
    _tolerant,  # noqa: F401 - kept importable from this module; see the note below
)
from personacore.audit.store_schema import (
    _MIGRATIONS,
    AuditStoreSchemaError,  # noqa: F401 - kept importable from this module; see below
    Migration,
    MigrationFailedError,
    SchemaDowngradeError,
    _migration_0001_initial_schema,  # noqa: F401 - kept importable; see below
    _migration_0002_conversations,  # noqa: F401 - kept importable; see below
    _migration_0003_conversation_persona,  # noqa: F401 - kept importable; see below
    _migration_0004_rooms_and_authorship,  # noqa: F401 - kept importable; see below
    _migration_0005_conversation_roster,  # noqa: F401 - kept importable; see below
    _migration_0006_attachments,  # noqa: F401 - kept importable; see below
    _migration_0007_reply_reasoning,  # noqa: F401 - kept importable; see below
    _migration_0008_conversation_kind,  # noqa: F401 - kept importable; see below
)

# Every ``noqa: F401`` above is load-bearing. ``ruff check --fix`` deletes an
# unused import, and deleting one of these breaks a caller that has imported
# that name from ``personacore.audit.store`` since before the split -- the
# tests reach for ``_MIGRATIONS`` and ``SchemaDowngradeError`` by that path
# today. Re-exporting is what makes the split invisible from outside.

_logger = get_logger(__name__)


class AuditStore(
    RecordsMixin,
    ConversationsMixin,
    RoomsMixin,
    AttachmentsMixin,
    ReasoningMixin,
    BackfillMixin,
    RetentionMixin,
    StoreBase,
):
    """SQLite-backed audit and transcript store living under appdata."""

    def __init__(self, config: AuditStoreConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        #: Set by `close`, read under the lock. Only the retention purge asks:
        #: it is the one caller that runs unattended on a timer and can still
        #: be queued behind the lock when the store is shut.
        self._closed = False

        self._config.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._config.database_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        with self._lock:
            self._migrate()

        # Conversations for rows written before conversations existed. After
        # the lock, because it takes the lock itself; best-effort, because
        # nothing about regrouping old history is worth failing to open the
        # store over (see _backfill_on_open).
        self._backfill_on_open()

    # -- schema -----------------------------------------------------------

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        self._conn.commit()
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO schema_version (version) VALUES (0)")
            self._conn.commit()
            current = 0
        else:
            current = int(row["version"])

        target = len(_MIGRATIONS)

        # Downgrade: the database has seen a newer build than this one. Never
        # guess at an unreviewed schema -- refuse and name both versions so
        # the operator knows exactly what to do (spec section 7).
        if current > target:
            raise SchemaDowngradeError(
                f"Database schema_version is {current}, but this PersonaCore "
                f"build only knows migrations up to version {target}. This "
                "database was last opened by a newer build. Run that newer "
                "build again, or restore an appdata backup that matches this "
                "build's schema version (spec section 7 / section 10 backup)."
            )

        for step_index in range(current, target):
            self._run_migration_step(step_index, _MIGRATIONS[step_index])

        if target > current:
            _logger.info(
                "audit_store_migrated", schema_version_from=current, schema_version_to=target
            )

    def _run_migration_step(self, step_index: int, migration: Migration) -> None:
        """Apply one migration and its `schema_version` bump as a single
        atomic unit.

        Defect fixed: previously the DDL ran and `schema_version` was bumped
        as separate statements outside any explicit transaction, so a crash
        between the two left the database holding half of step
        ``step_index + 1`` forever -- the next open would re-run the same
        DDL (e.g. `CREATE TABLE`) against a database that already has it,
        raising a raw `sqlite3.OperationalError`.

        `self._conn`'s default (legacy) isolation mode implicitly commits any
        pending transaction before a DDL statement, which would silently
        split our transaction in two and reintroduce exactly that hole. So
        for the duration of one migration step we switch the connection to
        autocommit mode (`isolation_level = None`) and drive the transaction
        ourselves with explicit BEGIN/COMMIT/ROLLBACK -- the standard sqlite3
        idiom for making DDL genuinely transactional -- then restore the
        connection's isolation mode for the rest of the store's lifetime.
        """
        previous_isolation_level = self._conn.isolation_level
        self._conn.isolation_level = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                migration(self._conn)
                self._conn.execute(
                    "UPDATE schema_version SET version = ?", (step_index + 1,)
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            raise MigrationFailedError(
                f"Migration step {step_index + 1} failed to apply: {exc}. The "
                "database may be left over from a previous run that crashed "
                "mid-migration. Restore an appdata backup from before that "
                "run (spec section 7 / section 10) rather than continuing on "
                "a database in an unknown state."
            ) from exc
        finally:
            self._conn.isolation_level = previous_isolation_level

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row else 0

    def close(self) -> None:
        """Hang up the connection, once every other user of it has let go.

        **Under the lock, and that is the whole point.** ``check_same_thread``
        is off, so this one sqlite connection is used from every worker thread
        `asyncio.to_thread` hands work to, and `_lock` is the only thing
        serialising them. This method used to be the single exception -- it
        took the connection and freed it without asking -- and freeing a
        sqlite handle while another thread is inside ``sqlite3_step`` is not an
        exception, it is a use-after-free. It segfaulted CI: the retention
        purge was mid-``DELETE`` on a worker thread while a test called this,
        and the interpreter died rather than raising anything a test could see.

        So this waits. A close that blocks for the length of a purge is the
        correct price; there is no safe way to take a connection away from a
        statement that is still running on it. `personacore.server`'s shutdown
        already waits for a running purge for exactly this reason -- that wait
        was advisory, and this is what makes it true.

        Idempotent, because the callers are test fixtures and several of them
        close a store their own ``finally`` will close again. Nothing in the
        running core closes the store at all today, which is why a defect this
        old went unseen outside CI.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def set_retention(self, retention: RetentionConfig) -> None:
        """Swap the retention window without restarting (ADR-0010).

        The window is read fresh on every pass, so replacing the config is all
        it takes -- which is what lets a retention change saved in the admin UI
        apply now rather than at the next restart. The store's lock is
        deliberately NOT taken: it guards the sqlite connection, and a purge
        already sweeping a large database would hold it long enough to stall
        the caller. A purge that has already read the old config finishes on
        it; the next one uses the new one.
        """
        self._config = self._config.model_copy(update={"retention": retention})


__all__ = ["BACKFILL_BATCH", "AuditStore"]
