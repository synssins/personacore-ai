"""Data models for the audit and transcript store.

Spec section 7 (audit log: every tool call, event, confirmation, admin change),
section 8 (multi-user attribution), section 9 (trace view), section 13.3
(schemas before features), and ADR-0004 (conversation audit and retention,
which adds message-level transcript logging on top of section 7's audit log).

Two record families share one schema shape because they answer the same
question from two angles — "what did the assistant do" (:class:`AuditRecord`)
and "what was said" (:class:`TranscriptRecord`) — and the section 9 trace view
needs to group and filter both the same way: by owner, by surface, by time,
and by correlation id. A transcript record is linked to whatever audit records
the message it carries produced by sharing that message's ``correlation_id``
(ADR-0004: "linked to the audit records the message produced") rather than by
a separate join table — one fewer thing to keep in sync, and it is the same
id the section 10 structured-logging trace view already groups by.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personacore.contracts import RiskLevel

DEFAULT_RETENTION_DAYS = 30
"""ADR-0004: age-out default. Overridable per surface."""


class Surface(StrEnum):
    """Where a message was said or an action originated.

    ADR-0004 requires retention to be configurable per surface, and spec
    section 9's trace view filters by it, so this is a first-class field on
    both record families rather than something inferred from context.
    """

    VOICE = "voice"
    ADMIN_UI = "admin_ui"
    API = "api"
    """The exposed OpenAI-compatible API, spec section 5.4."""

    ANONYMOUS = "anonymous"
    """The unauthenticated tier, ADR-0003."""

    SYSTEM = "system"
    """Not tied to a live conversation exchange: scheduler-fired events, admin
    changes made outside a chat turn, etc."""


class OwnerKind(StrEnum):
    """Spec section 8 / 13.3: every record is attributed. Nullable-and-hope is
    exactly what this enum exists to rule out — a record with no clear owner
    should fail to construct, not fall back to NULL."""

    PROFILE = "profile"
    """A concrete profile id — household member or API-key client."""

    HOUSEHOLD = "household"
    """Explicitly shared, no single owner."""

    ANONYMOUS = "anonymous"
    """The unauthenticated tier, ADR-0003."""


_OWNER_SENTINELS: dict[OwnerKind, str] = {
    OwnerKind.HOUSEHOLD: "household",
    OwnerKind.ANONYMOUS: "anonymous",
}


class Owner(BaseModel):
    """Who a record belongs to.

    The classmethod constructors are the intended entry points: they pin the
    sentinel id used for the non-profile kinds so every caller in the codebase
    uses the same literal string, rather than each call site inventing its
    own spelling of "household" or "anonymous".
    """

    model_config = ConfigDict(extra="forbid")

    kind: OwnerKind
    id: str = Field(min_length=1)

    @classmethod
    def profile(cls, profile_id: str) -> Owner:
        return cls(kind=OwnerKind.PROFILE, id=profile_id)

    @classmethod
    def household(cls) -> Owner:
        return cls(kind=OwnerKind.HOUSEHOLD, id=_OWNER_SENTINELS[OwnerKind.HOUSEHOLD])

    @classmethod
    def anonymous(cls) -> Owner:
        return cls(kind=OwnerKind.ANONYMOUS, id=_OWNER_SENTINELS[OwnerKind.ANONYMOUS])


class AuthorKind(StrEnum):
    """Whether a message was said by a person or by a persona.

    Separate from :class:`OwnerKind`, which answers a different question.
    ``Owner`` is *whose record this is* — the account the retention purge, the
    ownership check and the trace view all key on. ``AuthorKind`` is *who
    spoke*: an assistant reply is owned by the operator it was said to and
    authored by the persona that said it, and collapsing the two would make a
    persona's words look like the operator's own.
    """

    HUMAN = "human"
    PERSONA = "persona"


class Author(BaseModel):
    """Who said one message.

    Lives here rather than in ``personacore.conversations.models`` — where the
    chat-room contract nominally puts it — because :class:`TranscriptRecord`
    below has to carry it, and ``conversations.models`` already imports this
    module for :class:`Owner` and :class:`Surface`. Defining it there and
    importing it here would be a genuine cycle, not a style preference (see
    ``personacore.audit.store._conversations`` for the same problem solved the
    expensive way). It is re-exported from ``personacore.conversations.models``,
    so the import path the contract names works exactly as written.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Display name: the account's, or the persona's."""

    kind: AuthorKind

    model: str | None = None
    """Which model spoke, for a persona; ``None`` for a human.

    That asymmetry is the whole feature: a name with a model beside it is a
    persona and a name on its own is a person, so the reader can tell them
    apart without a legend. Not validated as "required when kind is PERSONA",
    because the model id is learned from the stream that answered and a turn
    that failed before the first chunk has no honest value to put here — a
    persona whose model is unknown is a name with no parentheses, which is a
    worse render but not a lost message.
    """


def _require_tz(v: datetime) -> datetime:
    # A naive timestamp is ambiguous, and the trace view (spec section 9) and
    # retention purge (ADR-0004) are only correct if ordering and age are real.
    # Mirrors personacore.contracts.events.EventEnvelope's same requirement.
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return v


class AuditCategory(StrEnum):
    """The four things spec section 7 says the audit log must cover."""

    TOOL_CALL = "tool_call"
    EVENT = "event"
    """An event that woke the agent, spec section 5.2 / 7."""

    CONFIRMATION = "confirmation"
    """A confirm/restricted decision, given or refused, spec section 7 / 8."""

    ADMIN_CHANGE = "admin_change"

    ACCESS = "access"
    """A turn refused before it began — no profile, a disabled profile, or one
    barred from the surface it arrived on.

    Spec section 7 names four things the audit log must cover, and this is not
    literally one of them. It is here because the list is a floor, not a
    ceiling: a refused access attempt is what an intrusion looks like from the
    inside, and a log that records every door opened but no door rattled is
    missing the half that matters. Added additively — the stored column is
    plain text with no constraint, so no migration is required."""


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    REFUSED = "refused"
    """Confirmation withheld, or a restricted action denied by policy."""

    PENDING = "pending"
    """Awaiting a confirmation that has not yet arrived."""


class AuditRecord(BaseModel):
    """One row of the audit log: a tool call, event, confirmation, or admin
    change. ``detail`` carries arguments/outcome payload — this is action
    metadata, not conversation content, so storing it here (never in the log
    stream) does not trip the transcript-privacy rule below.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    """Database row id. None until the store assigns one."""

    record_id: UUID = Field(default_factory=uuid4)
    correlation_id: str
    """Groups everything one request did — the same id structlog binds per
    request (personacore.audit.logging), so a trace-view query can pull the
    full story of one exchange across both record families."""

    timestamp: datetime
    surface: Surface
    owner: Owner
    category: AuditCategory
    action: str
    """What happened, e.g. "weather.get_forecast", "front-door.person-detected",
    or an admin setting name. Free text in the publisher's own vocabulary,
    same convention as EventEnvelope.type."""

    risk_level: RiskLevel | None = None
    """Only meaningful for TOOL_CALL; None for the other categories."""

    outcome: AuditOutcome
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _require_tz(v)


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class TranscriptRecord(BaseModel):
    """One message, in or out, on any surface (ADR-0004).

    ``content`` is the most privacy-sensitive field in the whole system: it is
    written to this store and must never be passed to the structured-logging
    stream (personacore.audit.logging enforces the redaction side; this
    module's store enforces the "don't log it at all" side).
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    record_id: UUID = Field(default_factory=uuid4)
    correlation_id: str
    timestamp: datetime
    surface: Surface
    owner: Owner
    role: MessageRole
    content: str

    conversation_id: str | None = None
    """Which conversation this message belongs to
    (:class:`personacore.conversations.models.Conversation`), or ``None`` for a
    message that is not attached to one.

    Nullable on purpose, in both directions. Backwards: every transcript row
    written before conversations existed has no conversation, and inventing
    one at read time would be a different answer every read. Forwards: a
    writer that has no conversation to name — a plugin, the exposed API, an
    event that woke the agent with nobody watching — must still be able to
    record what was said. A message is a message whether or not anybody is
    holding a thread of it; only the thread is optional.

    Set either by the writer, or afterwards by
    :meth:`personacore.audit.store.AuditStore.attach_to_conversation`, which is
    how a surface whose write path it does not own (the admin chat screen,
    whose rows are written by the agent loop) claims the rows its turn just
    produced.
    """

    author: Author | None = None
    """Who said this, or ``None`` when nobody recorded it.

    ``None`` is the honest answer for every row written before authorship
    existed, and it stays ``None``: a migration that backfilled a guess would
    be inventing history, and "the persona at the time" is not knowable from a
    row that never wrote it down. A reader shows the name it can work out and
    no model — never the word "unknown", and never a guessed model.

    Supplied by whoever writes the row. The store records what it is given and
    infers nothing: a store that filled this in from the role would call every
    assistant reply a persona, including the ones this core spoke on its own
    behalf when a turn failed.
    """

    @field_validator("timestamp")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _require_tz(v)


class ReasoningRecord(BaseModel):
    """One reply's own reasoning — the model's ``reasoning_content``, kept
    verbatim, for the reply whose :class:`TranscriptRecord` shares its
    ``correlation_id`` (:mod:`personacore.web.screens.chat_streaming`
    mints one per persona per turn, so this is always a 1:1 match, never a
    join that could land one reply's thinking under another's).

    **Its own table, not a column and not a ride on
    :class:`AuditRecord`.** :class:`AuditRecord`'s own docstring calls
    ``detail`` "action metadata, not conversation content" — and that
    distinction is what lets this package's logging calls treat ``detail``
    as safe to summarise without checking what is in it. Reasoning is the
    model's own words about what a person said: it is conversation content
    exactly as :attr:`TranscriptRecord.content` is, and the size is not
    incidental either — ten to fifteen thousand tokens of it is ordinary for
    a reasoning model, tens of kilobytes on a turn that
    :func:`~personacore.web.screens.chat_reply.TurnMetrics` describes in
    four numbers. Filing that under the same category and window every tool
    call and turn-metrics record share would mean every read of that window
    — the Chat screen's own replay among them — drags kilobytes of thinking
    behind a name and a duration it did not ask for.

    Ageing and deletion still follow the transcript exactly (the owner's rule:
    retention matches the transcript's, gone when a conversation is deleted).
    That is why this table copies
    :class:`AuditRecord`'s own two mechanisms rather than inventing a third —
    ``surface``/``timestamp`` for the per-surface purge
    (:meth:`~personacore.audit.store_retention.RetentionMixin.purge_older_than`)
    and ``correlation_id`` for an administrator's delete
    (:meth:`~personacore.audit.store_conversations.ConversationsMixin.delete_conversation`)
    — rather than the attachments table's approach of reading age off
    ``transcript_records`` by ``NOT EXISTS``: a turn's reasoning and its
    reply are written within the same request and always share one
    timestamp, so copying it costs nothing and keeps the same cutoff finding
    both rows on the same pass, the same guarantee
    ``chat_reply._metrics_detail`` relies on for turn metrics today.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    """Database row id. None until the store assigns one."""

    correlation_id: str
    """The reply this reasoning belongs to. Unique in this table — one turn
    reasons once, and a second write for the same id would mean two turns
    are trying to speak through one reply."""

    timestamp: datetime
    """Copied from the reply's own transcript row, not ``datetime.now(UTC)``
    — the same reason :class:`~personacore.web.screens.chat_reply.
    TurnMetrics`'s own record is timestamped this way: a purge's cutoff
    compares against this column, and a later "now" could land the wrong
    side of it relative to the reply it describes."""

    surface: Surface
    owner: Owner
    """Who may read this back — the same owner as the reply's own row, never
    supplied by a caller. Every read is filtered by owner in the query's
    ``WHERE``, the same rule every other table in this store enforces."""

    text: str
    """The model's reasoning, verbatim. Plain text, not markup — the
    requirement is to be able to hand this back to a model, which means
    reading it out as the words it is rather than only rendering it into
    HTML."""

    @field_validator("timestamp")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _require_tz(v)


class AttachmentRecord(BaseModel):
    """One file stored under ``<appdata>/attachments/<attachment_id>/`` —
    docs/contracts/attachments.md §2.

    A transcript row may carry several, so this is its own table joined to a
    message by ``correlation_id`` rather than a column on
    :class:`TranscriptRecord` — the same reason a room's roster (§5) is not a
    column either. **These are the contract's fields; nothing here is
    invented and nothing is renamed**, because the composer and display half
    of this feature is written against this exact list.
    """

    model_config = ConfigDict(extra="forbid")

    attachment_id: str
    """The random id: primary key, and the directory name on disk. Never a
    content hash — see :mod:`personacore.attachments` for why a hash would be
    an equality oracle between two people who attached the same file."""

    correlation_id: str
    """The turn this was attached to — the same column
    :class:`TranscriptRecord` and :class:`AuditRecord` already share."""

    conversation_id: str | None = None
    """Nullable, exactly as :attr:`TranscriptRecord.conversation_id` is, and
    for the same documented reasons: a message with no thread must still be
    able to carry a file."""

    owner: Owner
    """Who this belongs to. The row is the authority on who may fetch the
    file (contract §3): every read is filtered by owner in the query's
    ``WHERE``, never by the id alone."""

    media_type: str
    """The type this core chose from its own allowlist (contract §6) —
    never one an uploader supplied, and never ``text/html``."""

    byte_size: int = Field(ge=0)
    """As stored."""

    original_name: str
    """What the file was called when it arrived, for display only. Never
    used to build a path, and never logged (contract §6/§7/§11)."""

    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _require_tz(v)


class RetentionConfig(BaseModel):
    """ADR-0004: "retention is an age-out with a configurable window; default
    30 days... set in the admin UI, per surface." ``per_surface_days``
    overrides ``default_days`` for the surfaces named in it.
    """

    model_config = ConfigDict(extra="forbid")

    default_days: int = Field(default=DEFAULT_RETENTION_DAYS, ge=1)

    per_surface_days: dict[Surface, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    """Bounded at 1 for the same reason ``default_days`` is: ``days_for`` feeds
    ``_purge_older_than``'s ``reference - timedelta(days=...)``, so a zero or
    negative window puts the cutoff at or after the reference time and the
    DELETE takes every row on that surface, whatever its age."""

    def days_for(self, surface: Surface) -> int:
        return self.per_surface_days.get(surface, self.default_days)


class AuditStoreConfig(BaseModel):
    """Constructor-argument config for :class:`personacore.audit.store.AuditStore`.

    Deliberately a small local model rather than reaching for a shared/global
    config object — CLAUDE.md's task brief for this component is explicit that
    another agent owns cross-cutting config, and this module must not collide
    with it.
    """

    model_config = ConfigDict(extra="forbid")

    database_path: Path
    """Where the sqlite file lives, e.g. <appdata>/audit/audit.db. The parent
    directory is created if missing; appdata layout itself (Appendix B) is the
    caller's concern, not this module's."""

    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class PurgeResult(BaseModel):
    """What :meth:`AuditStore.purge_older_than` deleted, per record family."""

    model_config = ConfigDict(extra="forbid")

    audit_deleted: int = 0
    transcript_deleted: int = 0

    reasoning_deleted: int = 0
    """Reply reasoning rows removed on this pass — migration 0007's own
    table, aged out on the same per-surface cutoff as ``audit_records`` and
    ``transcript_records`` because it copies its ``surface``/timestamp from
    the same reply row (see that migration's docstring), never by reading
    age off the transcript the way ``attachments`` does."""

    conversations_deleted: int = 0
    """Conversation headings removed because the purge left them empty.

    A conversation is a heading over transcript rows; it holds no message text
    of its own, so nothing about it ages out on its own schedule. What ages
    out is its contents — and a heading whose every message has been purged is
    a row in the conversation list that opens onto nothing. It is swept in the
    same pass that emptied it (see
    :meth:`personacore.audit.store.AuditStore.purge_older_than`) rather than
    left for a later tidy-up, because between the two passes an operator would
    be looking at a list of ghosts.
    """
