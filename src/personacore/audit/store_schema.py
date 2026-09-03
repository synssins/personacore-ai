"""The shape of the database, and the ladder that gets it there.

Every migration this build knows how to apply, in the order they are applied,
plus the three ways a schema can be wrong in a way this store refuses to paper
over. Nothing here opens a connection or runs anything: a migration is a
function handed a connection by :meth:`personacore.audit.store.AuditStore._migrate`,
which owns the transaction, the ``schema_version`` bump and the decision to run
at all.

**The list is append-only and the numbers are permanent.** A schema change is a
new ``_migration_00NN`` appended to :data:`_MIGRATIONS`, never an edit to one
that has shipped — a database in a household somewhere has already run the old
one, and rewriting it makes this build's idea of "version 3" different from
that database's. Renumbering or reordering does the same thing more quietly.

Deliberately not here: the ladder-climbing itself, which stays beside the
connection it drives, and any notion of what the rows mean —
:mod:`personacore.audit.store_rows` reads them back out.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]


class AuditStoreSchemaError(RuntimeError):
    """Base for appdata schema problems this store refuses to paper over.

    Spec section 7 requires appdata format changes to be explicit; a schema
    mismatch this code cannot account for must fail loudly rather than run
    against a database shape it has never seen.
    """


class SchemaDowngradeError(AuditStoreSchemaError):
    """Raised when the on-disk `schema_version` is ahead of what this build's
    `_MIGRATIONS` list knows how to produce -- i.e. the database was last
    opened by a newer PersonaCore build (a downgrade, or a restored backup
    from the future relative to this binary). Silently proceeding would run
    unreviewed code against a schema it has never seen; refusing is the
    explicit-appdata-format-change spec section 7 requires.
    """


class MigrationFailedError(AuditStoreSchemaError):
    """Raised when a migration step's DDL fails to apply cleanly -- most
    often a partially-applied migration from a previous run that crashed
    between the DDL and the `schema_version` bump (see
    `AuditStore._run_migration_step`). Wraps the underlying sqlite3 error
    with actionable guidance instead of leaking a raw
    `sqlite3.OperationalError` to the caller.
    """


def _migration_0001_initial_schema(conn: sqlite3.Connection) -> None:
    """The initial shape of both record families, spec section 13.3: designed
    now, before the features that surface them, because owner, correlation id
    and surface are exactly the fields that are miserable to retrofit.

    Deliberately individual `execute()` calls rather than one
    `executescript()`: `executescript()` issues an implicit COMMIT of its own
    before running, which would defeat `_run_migration_step`'s explicit
    transaction and reopen the partial-migration hole this module fixes (see
    that function's docstring). Splitting the script changes nothing about
    the resulting schema.
    """
    conn.execute(
        """
        CREATE TABLE audit_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id       TEXT NOT NULL UNIQUE,
            correlation_id  TEXT NOT NULL,
            ts_utc          TEXT NOT NULL,
            surface         TEXT NOT NULL,
            owner_kind      TEXT NOT NULL,
            owner_id        TEXT NOT NULL,
            category        TEXT NOT NULL,
            action          TEXT NOT NULL,
            risk_level      TEXT,
            outcome         TEXT NOT NULL,
            detail_json     TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_audit_ts ON audit_records (ts_utc)")
    conn.execute("CREATE INDEX idx_audit_owner ON audit_records (owner_kind, owner_id)")
    conn.execute("CREATE INDEX idx_audit_correlation ON audit_records (correlation_id)")
    conn.execute("CREATE INDEX idx_audit_surface ON audit_records (surface)")

    conn.execute(
        """
        CREATE TABLE transcript_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id       TEXT NOT NULL UNIQUE,
            correlation_id  TEXT NOT NULL,
            ts_utc          TEXT NOT NULL,
            surface         TEXT NOT NULL,
            owner_kind      TEXT NOT NULL,
            owner_id        TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_transcript_ts ON transcript_records (ts_utc)")
    conn.execute(
        "CREATE INDEX idx_transcript_owner ON transcript_records (owner_kind, owner_id)"
    )
    conn.execute(
        "CREATE INDEX idx_transcript_correlation ON transcript_records (correlation_id)"
    )
    conn.execute("CREATE INDEX idx_transcript_surface ON transcript_records (surface)")


def _migration_0002_conversations(conn: sqlite3.Connection) -> None:
    """Conversations: a person's transcript becomes threads rather than one
    endless roll (see personacore/conversations/models.py).

    Two changes, both additive, because the transcript is the one table in
    appdata that must survive every future decision: a new `conversations`
    table holding only the *heading* (identity, owner, title, activity), and a
    nullable `conversation_id` column on `transcript_records` pointing at it.

    The link deliberately lives on the message row rather than in a join
    table. A join table would be a third place a purged message has to be
    forgotten from, and forgetting it in two places out of three is how a
    retention system quietly stops being one (ADR-0004). Deleting the message
    row takes the link with it, with no cascade to get wrong.

    There is no FOREIGN KEY on the column either, and that is not laziness.
    `conversation_id` is nullable and a message may legitimately outlive the
    reaping of its (now empty) heading; a constraint here would turn a
    retention purge into a foreign-key failure at exactly the moment the store
    must not fail. Ownership, which is the constraint that actually matters,
    is enforced in the queries: every read is filtered by owner, never by the
    conversation id alone.

    No data is moved here. Reconstructing conversations for rows that predate
    this migration is `AuditStore.backfill_conversations`, which runs outside
    the migration transaction and is allowed to fail — see its docstring.
    """
    conn.execute(
        """
        CREATE TABLE conversations (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id   TEXT NOT NULL UNIQUE,
            owner_kind        TEXT NOT NULL,
            owner_id          TEXT NOT NULL,
            surface           TEXT NOT NULL,
            title             TEXT NOT NULL,
            started_at        TEXT NOT NULL,
            last_activity_at  TEXT NOT NULL,
            origin            TEXT NOT NULL
        )
        """
    )
    # The conversation list's only query: this owner's threads, most recently
    # spoken in first. Covered by the index so the list does not sort at read
    # time as somebody's history grows.
    conn.execute(
        "CREATE INDEX idx_conversations_owner "
        "ON conversations (owner_kind, owner_id, last_activity_at DESC)"
    )
    # Resolving "the conversation this page is holding" from its start instant,
    # which is what the chat form posts back (see the admin chat screen).
    conn.execute(
        "CREATE INDEX idx_conversations_started ON conversations (owner_kind, owner_id, started_at)"
    )
    conn.execute("ALTER TABLE transcript_records ADD COLUMN conversation_id TEXT")
    # Reading one conversation, and counting what is left of it after a purge.
    conn.execute(
        "CREATE INDEX idx_transcript_conversation "
        "ON transcript_records (conversation_id, ts_utc)"
    )


def _migration_0003_conversation_persona(conn: sqlite3.Connection) -> None:
    """Which persona a conversation is being held with.

    One nullable column on the heading, because the choice belongs to the
    thread: start a conversation with GLaDOS and it is still GLaDOS when you
    come back to it tomorrow, without anything having been written to
    ``core.toml`` and without the rest of the house changing character.

    **Nullable, and never backfilled.** Every conversation that exists when
    this runs was held with whatever the default was at the time, and the
    default may since have changed — so writing today's default into
    yesterday's rows would be inventing history. ``NULL`` means "no persona was
    chosen for this thread", which resolves to the configured default *at the
    moment it answers*, which is exactly what those conversations did before
    this column existed.

    No index. The column is only ever read through a conversation that has
    already been found by id or by start instant, never searched on.
    """
    conn.execute("ALTER TABLE conversations ADD COLUMN persona TEXT")


def _migration_0004_rooms_and_authorship(conn: sqlite3.Connection) -> None:
    """A conversation becomes a room, and a message says who spoke.

    Six columns and one index, all additive, all nullable, and **nothing is
    backfilled**.

    On ``conversations``: ``hidden_at``/``hidden_by`` record the owner taking a
    thread off their own list, and ``group_name`` files it. Hiding is not
    deleting — the conversation is kept for administrator review — so the fact
    is a timestamp rather than a flag: "when" is the first thing anybody
    reviewing it asks, and a boolean cannot answer it. There is deliberately no
    new column for a chosen title: a rename writes to ``title``, the same
    column a derived title uses, because a title is a title and the store has
    no reason to care which one somebody typed.

    On ``transcript_records``: ``author_name``, ``author_kind`` and
    ``author_model``. Every row that already exists keeps NULL in all three,
    and that is the point. There is no way to know from a stored row which
    persona said it or which model answered, so a backfill would be inventing
    history — and the reader's rule (a name with a model beside it is a
    persona, a name alone is a person) would then be quietly wrong for every
    old row instead of visibly silent about them.

    The index exists because the conversation list's query now filters on
    ``hidden_at`` as well as owner, so without it every list read would scan
    and sort somebody's whole history to drop the hidden ones. It replaces no
    existing index: ``idx_conversations_owner`` still serves the reads that do
    not care about visibility (the administrator's, most of all).
    """
    conn.execute("ALTER TABLE conversations ADD COLUMN hidden_at TEXT")
    conn.execute("ALTER TABLE conversations ADD COLUMN hidden_by TEXT")
    conn.execute("ALTER TABLE conversations ADD COLUMN group_name TEXT")
    conn.execute(
        "CREATE INDEX idx_conversations_visible "
        "ON conversations (owner_kind, owner_id, hidden_at, last_activity_at DESC)"
    )
    conn.execute("ALTER TABLE transcript_records ADD COLUMN author_name TEXT")
    conn.execute("ALTER TABLE transcript_records ADD COLUMN author_kind TEXT")
    conn.execute("ALTER TABLE transcript_records ADD COLUMN author_model TEXT")


def _migration_0005_conversation_roster(conn: sqlite3.Connection) -> None:
    """More than one persona in a room — the many-voices contract, §2.

    One nullable column holding **the personas besides the one ``persona``
    names**, as a JSON array of persona names. Not the whole roster: the
    contract's §7 is that a conversation with one persona must behave exactly
    as it does today, and the cheapest way to guarantee that is for the
    one-persona case to be *literally unchanged* — ``persona`` alone, this
    column NULL, nothing to keep in step and nothing new to read.

    Nullable and never backfilled, for the reason every other column on this
    table is: every conversation that exists when this runs had one persona in
    it, and NULL already says that.

    No index. It is only ever read through a conversation already found by id
    or by start instant, exactly like ``persona``.
    """
    conn.execute("ALTER TABLE conversations ADD COLUMN also_present TEXT")


def _migration_0006_attachments(conn: sqlite3.Connection) -> None:
    """Attachments: the link between a message and the files it carries —
    docs/contracts/attachments.md §2.

    A transcript row may carry several attachments, so this is a table rather
    than a column, joined to a message by `correlation_id` — the same id the
    audit records already share with a transcript row, not a new concept.

    `attachment_id` is the primary key **and** the directory name under
    `<appdata>/attachments/` (contract §3): a random id minted by
    :mod:`personacore.attachments`, never a content hash, so two household
    members attaching the same file never land on the same id and cannot use
    it to learn what the other sent.

    No FOREIGN KEY on `correlation_id` or `conversation_id`, for the same
    reason `transcript_records.conversation_id` has none (migration 0002): a
    constraint here would turn the retention purge into a foreign-key failure
    at exactly the moment the store must not fail, and ownership — the
    constraint that actually matters — is enforced in every query's WHERE,
    never by the schema.

    `conversation_id` is nullable, exactly as `transcript_records.
    conversation_id` is and for the same documented reasons: a message
    written by a surface that does not thread its messages must still be able
    to carry a file.

    There is deliberately no `surface` or age column here. The contract fixes
    this table's fields and neither is among them — an attachment ages out
    with the message it belongs to (contract §7), which is read off
    `transcript_records` by `correlation_id` rather than tracked a second
    time on this table (see `personacore.audit.store_attachments.
    AttachmentsMixin.orphaned_attachments`).
    """
    conn.execute(
        """
        CREATE TABLE attachments (
            attachment_id    TEXT PRIMARY KEY,
            correlation_id   TEXT NOT NULL,
            conversation_id  TEXT,
            owner_kind       TEXT NOT NULL,
            owner_id         TEXT NOT NULL,
            media_type       TEXT NOT NULL,
            byte_size        INTEGER NOT NULL,
            original_name    TEXT NOT NULL,
            created_at       TEXT NOT NULL
        )
        """
    )
    # The orphan sweep's own lookup: every attachment sharing a correlation id
    # with a transcript row, checked in the direction that query runs.
    conn.execute("CREATE INDEX idx_attachments_correlation ON attachments (correlation_id)")
    # `get_attachment`'s WHERE clause: id and owner together, so the two are
    # covered by one index rather than a table scan filtered by owner alone.
    conn.execute("CREATE INDEX idx_attachments_owner ON attachments (owner_kind, owner_id)")
    conn.execute("CREATE INDEX idx_attachments_conversation ON attachments (conversation_id)")


def _migration_0007_reply_reasoning(conn: sqlite3.Connection) -> None:
    """A reply's own reasoning — its own table, not a column on
    ``audit_records`` or ``transcript_records``.

    The owner overruled the first cut of this feature (2026-09-02): reasoning
    must be retained, as additional context that can be handed back to the
    model when necessary. That is the rule this schema is built
    against — retrievable as plain text, not only ever rendered.

    Turn metrics (first token, tokens, first audio) answered the same "does a
    turn need its own row" question by riding on `AuditRecord.detail`
    (:data:`~personacore.web.screens.chat_reply.TURN_METRICS_CATEGORY`)
    and this does not, for two reasons neither of which applied to four small
    numbers:

    * ``AuditRecord``'s own docstring calls ``detail`` "action metadata, not
      conversation content" — the very fact that lets this package's logging
      calls treat it as safe to summarise. Reasoning is the model's own words
      about what a person said; storing it there would make that sentence
      false the day somebody logs a `detail` for debugging and does not know
      this table broke the promise.
    * Size. Ten to fifteen thousand tokens of reasoning is ordinary for a
      reasoning model — tens of kilobytes on a turn a `TurnMetrics` record
      describes in four numbers. Every read of the audit window this Chat
      screen already makes to find a thread's tool calls and its metrics
      (`chat_thread.CHAT_AUDIT_WINDOW`) would drag that behind it whether or
      not anybody had expanded the thinking line.

    `correlation_id` is `UNIQUE`, not merely indexed: one persona's one turn
    reasons once (`chat_streaming` mints a fresh correlation id per persona
    per turn), so a second write for the same id would mean two turns are
    trying to speak through the same reply, and the constraint says so
    outright rather than silently keeping whichever arrived first.

    `surface` and `ts_utc` are copied from the reply's own transcript row,
    exactly as `TurnMetrics`'s own record is (`chat_streaming.
    _record_turn_metrics`'s own comment on why: "now" can land outside the
    span a query bounds itself to). Copying them here, rather than reading
    age off `transcript_records` the way `attachments` does (migration 0006),
    means `purge_older_than` ages this table out with one more pair of
    `DELETE ... WHERE surface = ? AND ts_utc < ?` statements sharing the exact
    cutoff `audit_records`/`transcript_records` already use — the same
    request wrote both rows with the same timestamp, so the two purges can
    never disagree about which turn's reasoning survives.

    No `conversation_id` column: unlike `attachments` (a person's own file,
    which must still exist for a message with no thread), this is written by
    the same route that already knows the reply's correlation id and nothing
    else about it needs to be found by conversation directly — an
    administrator's `delete_conversation` reaches it exactly as it reaches
    `audit_records`, by joining `correlation_id` against the transcript rows
    being deleted.

    No `FOREIGN KEY`, for the same reason migration 0002 gives none on
    `transcript_records.conversation_id`: a constraint here would turn the
    retention purge into a foreign-key failure at exactly the moment the
    store must not fail.
    """
    conn.execute(
        """
        CREATE TABLE reply_reasoning (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id  TEXT NOT NULL UNIQUE,
            ts_utc          TEXT NOT NULL,
            surface         TEXT NOT NULL,
            owner_kind      TEXT NOT NULL,
            owner_id        TEXT NOT NULL,
            reasoning       TEXT NOT NULL
        )
        """
    )
    # The one lookup this table exists to answer: this reply's own reasoning,
    # by the id its transcript row and its turn-metrics record already share.
    # UNIQUE above already covers equality lookups with an index; this is
    # named separately so it reads like every other index in this ladder
    # rather than relying on the constraint's implicit one.
    conn.execute(
        "CREATE INDEX idx_reasoning_correlation ON reply_reasoning (correlation_id)"
    )
    # purge_older_than's own per-surface cutoff sweep.
    conn.execute("CREATE INDEX idx_reasoning_ts ON reply_reasoning (ts_utc)")
    # No query in this build filters this table by owner alone — every read is
    # a single correlation id already known to belong to this owner's thread —
    # so this exists for the one that reasonably could later, the same
    # forward-looking reason `idx_attachments_owner` was added in migration
    # 0006 rather than waited for.
    conn.execute("CREATE INDEX idx_reasoning_owner ON reply_reasoning (owner_kind, owner_id)")


def _migration_0008_conversation_kind(conn: sqlite3.Connection) -> None:
    """What kind of thing answers in a conversation — ``docs/contracts/
    image-conversations.md`` §4.

    One column on ``conversations``, and unlike every other column this
    ladder has added to that table (``persona``, ``hidden_at``, ``hidden_by``,
    ``group_name``, ``also_present``), this one is **not nullable and is not
    left for the reader to default**. Those columns are all genuinely absent
    for an old row — nobody had chosen a persona, nobody had hidden it — and
    ``NULL`` says exactly that. There is no equivalent absence here: a
    conversation held before this column existed was still, as a fact, a text
    conversation. ``NOT NULL DEFAULT 'text'`` says so once, in the schema,
    rather than asking every reader downstream (``store_rows._row_to_
    conversation``, same as every tolerant column there) to invent the same
    fallback independently. SQLite applies a non-NULL constant default to
    every existing row without rewriting the table, which is exactly the
    "additive, safe on a database with rows already in it" shape every
    migration in this ladder has kept to.

    No backfill statement is needed or written for the same reason: the
    default *is* the correct value for every row that predates this column,
    not a placeholder standing in for one.

    No index. Exactly like ``persona`` and ``origin``, this is only ever read
    through a conversation already found by id or by start instant — nothing
    in this build lists or filters conversations by kind.
    """
    conn.execute("ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")


# Ordered, append-only. Adding schema changes later means appending a new
# _migration_00NN function here and to this list — never editing an already
# shipped migration, and never relying on an ORM to infer the diff.
_MIGRATIONS: list[Migration] = [
    _migration_0001_initial_schema,
    _migration_0002_conversations,
    _migration_0003_conversation_persona,
    _migration_0004_rooms_and_authorship,
    _migration_0005_conversation_roster,
    _migration_0006_attachments,
    _migration_0007_reply_reasoning,
    _migration_0008_conversation_kind,
]
