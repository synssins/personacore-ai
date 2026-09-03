"""Stored rows back into models, and the timestamps on both sides of that.

Everything in this module answers one question: what does a row mean? It reads,
it never writes, and it holds no connection — the store hands it a
:class:`sqlite3.Row` and gets a model back, which is why every other module of
the store can import it and it imports none of them.

**Tolerant on purpose, in one direction only.** A single row this build cannot
make sense of is skipped and counted rather than raised, because the strict
alternative turns a stray value in one column into a conversation list that
will not draw at all. That trade is argued in each function's own docstring,
and it applies to *reading*: nothing here decides whether something may be
read, and ownership is checked in the queries, never here.

Privacy: as everywhere in this package, these functions log counts and record
families, never content.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from personacore.audit.logging import get_logger
from personacore.audit.models import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    Author,
    AuthorKind,
    MessageRole,
    Owner,
    OwnerKind,
    RiskLevel,
    Surface,
    TranscriptRecord,
)

if TYPE_CHECKING:  # pragma: no cover - see _conversations() below
    from personacore.conversations.models import Conversation

_logger = get_logger(__name__)


def _conversations() -> Any:
    """:mod:`personacore.conversations.models`, imported at call time.

    A module-level import here is a genuine cycle, not a style preference:
    ``personacore.audit.__init__`` eagerly imports this module, and
    ``conversations.models`` types a conversation's owner and surface with
    ``personacore.audit.models``' own ``Owner`` and ``Surface`` — so importing
    either package first sends Python back through the other one before it has
    finished defining what the first one needs.

    Three ways out were available. Retyping ``Conversation.owner`` as loose
    strings would break the one property this feature has to get right —
    ownership is checked in every query, and a second, weaker notion of who
    owns something is how that check eventually stops matching. Stripping the
    eager import out of ``personacore.audit.__init__`` would fix it at the
    root, but that file is imported by most of the codebase and is not this
    change's to rewrite. So the import moves to call time, where the cycle is
    already resolved: by the time any of these methods run, both modules are
    fully defined. ``sys.modules`` makes the repeat cost a dictionary lookup.
    """
    from personacore.conversations import models

    return models


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _tolerant[T](
    rows: Sequence[sqlite3.Row],
    build: Callable[[sqlite3.Row], T],
    *,
    what: str,
) -> list[T]:
    """Build models from rows, dropping any single row that will not build.

    The strict alternative -- let one bad row raise out of a list query -- is
    what turns a stray value in one column into "the conversation list is
    gone" or, on a page that reads at import-adjacent time, into a surface
    that will not come up at all. A row this build cannot make sense of is
    skipped and *counted in the log*, so the damage is one missing entry and a
    line saying so rather than a screen an operator cannot open to investigate
    with.

    Content is never logged here, only the count and the record family.
    """
    built: list[T] = []
    skipped = 0
    for row in rows:
        try:
            built.append(build(row))
        except Exception:  # noqa: BLE001, PERF203 - see docstring
            skipped += 1
    if skipped:
        _logger.warning("store_rows_unreadable", record=what, skipped=skipped)
    return built


def _group_by_gap(rows: Sequence[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Split transcript rows into conversations by owner, surface and silence.

    Rows arrive ordered by owner, surface and time (see
    :meth:`AuditStore.backfill_conversations`). A new group starts when the
    owner or the surface changes -- those are hard boundaries, never a
    judgement call -- or when more than
    :data:`~personacore.conversations.models.SESSION_GAP` passed between one
    message and the next.

    A row whose timestamp will not parse does not abort the pass and does not
    get merged into whatever it happens to sit beside: it starts its own
    group, so a single corrupt timestamp costs one oddly-split conversation
    rather than the whole backfill.
    """
    session_gap = _conversations().SESSION_GAP
    groups: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []
    previous_key: tuple[str, str, str] | None = None
    previous_ts: datetime | None = None

    for row in rows:
        key = (row["owner_kind"], row["owner_id"], row["surface"])
        try:
            ts: datetime | None = _parse_iso(row["ts_utc"])
        except (TypeError, ValueError):
            ts = None

        starts_new = (
            not current
            or key != previous_key
            or ts is None
            or previous_ts is None
            or ts - previous_ts > session_gap
        )
        if starts_new and current:
            groups.append(current)
            current = []
        current.append(row)
        previous_key = key
        previous_ts = ts

    if current:
        groups.append(current)
    return groups


def _row_to_audit(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(
        id=row["id"],
        record_id=UUID(row["record_id"]),
        correlation_id=row["correlation_id"],
        timestamp=_parse_iso(row["ts_utc"]),
        surface=Surface(row["surface"]),
        owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
        category=AuditCategory(row["category"]),
        action=row["action"],
        risk_level=RiskLevel(row["risk_level"]) if row["risk_level"] else None,
        outcome=AuditOutcome(row["outcome"]),
        detail=json.loads(row["detail_json"]),
    )


def _column(row: sqlite3.Row, name: str) -> object | None:
    """One column, or ``None`` if this row does not have it.

    ``sqlite3.Row`` raises ``IndexError`` for an absent column, and the
    absent-column case is real: a ``SELECT`` written against a newer schema
    than the connection is actually holding is precisely what a half-applied
    migration looks like. Reading a transcript is not worth taking a surface
    down over (see the module docstring on failure direction), so a missing
    column reads as "not set" and the record comes back without it.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _row_to_author(row: sqlite3.Row) -> Author | None:
    """The three author columns as an :class:`Author`, or ``None``.

    ``None`` for every row written before migration 0004, and for any row whose
    writer did not say who was speaking. Both are the same state — nobody
    recorded it — and the reader's answer to both is a name with no model
    beside it, never the word "unknown".

    A name with an unrecognised ``author_kind`` also reads as ``None`` rather
    than raising. Refusing to return a message because one column holds a word
    this build has not heard of would lose the sentence to protect the label on
    it; ``_tolerant`` would drop the whole row, and the row is the part that
    matters.
    """
    name = _column(row, "author_name")
    raw_kind = _column(row, "author_kind")
    if not name or not raw_kind:
        return None
    try:
        kind = AuthorKind(str(raw_kind))
    except ValueError:
        return None
    model = _column(row, "author_model")
    return Author(name=str(name), kind=kind, model=str(model) if model else None)


def _row_to_transcript(row: sqlite3.Row) -> TranscriptRecord:
    conversation_id = _column(row, "conversation_id")
    return TranscriptRecord(
        id=row["id"],
        record_id=UUID(row["record_id"]),
        correlation_id=row["correlation_id"],
        timestamp=_parse_iso(row["ts_utc"]),
        surface=Surface(row["surface"]),
        owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
        role=MessageRole(row["role"]),
        content=row["content"],
        conversation_id=str(conversation_id) if conversation_id is not None else None,
        author=_row_to_author(row),
    )


def _row_to_roster(stored: object) -> tuple[str, ...]:
    """The other personas in a room, out of one stored JSON array.

    Tolerant in exactly one direction: anything that is not a list of
    non-empty strings reads as an empty roster, which is the single-persona
    conversation this column was added to leave alone. A database from before
    migration 0005, a NULL, and a row somebody hand-edited into nonsense are
    all the same state — nobody else is in the room — and refusing to list a
    household's conversations because one row has a stray character in it would
    be a spectacularly bad trade for a feature that is an addition to a
    conversation rather than a condition of it.
    """
    if not stored:
        return ()
    try:
        parsed = json.loads(str(stored))
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(
        item.strip() for item in parsed if isinstance(item, str) and item.strip()
    )


def _row_to_conversation(row: sqlite3.Row) -> Conversation:
    """One `conversations` row as a model.

    ``origin`` is read tolerantly: an unrecognised value becomes ``BACKFILL``
    rather than raising, because ``origin`` is a label on a heading and no
    decision anywhere depends on it. Refusing to list somebody's whole
    conversation history because one row carries a word this build has not
    heard of would be a spectacularly bad trade.
    """
    models = _conversations()
    raw_origin = str(_column(row, "origin") or "")
    try:
        origin = models.ConversationOrigin(raw_origin)
    except ValueError:
        origin = models.ConversationOrigin.BACKFILL
    count = _column(row, "message_count")
    # Read through _column and normalised to None: a row written by a build
    # before migration 0003, and a row whose thread never chose anybody, are
    # the same state — no persona recorded, so the default answers.
    persona = _column(row, "persona")
    # Same treatment for the room columns: a database from before migration
    # 0004 has none of them, and "not there" and "never set" are one state --
    # a visible, ungrouped conversation, which is what every conversation was
    # before this existed.
    hidden_at = _column(row, "hidden_at")
    hidden_by = _column(row, "hidden_by")
    group_name = _column(row, "group_name")
    # Unlike every other tolerant read in this function, absence and an
    # unrecognised value both fall back to TEXT rather than to a state that
    # means "nothing recorded". A row from before migration 0008 has no
    # column to read at all, and a conversation held before this field
    # existed was, as a fact, a text conversation -- there is no "nobody
    # chose" state here the way there is for `persona`.
    raw_kind = str(_column(row, "kind") or "")
    try:
        kind = models.ConversationKind(raw_kind)
    except ValueError:
        kind = models.ConversationKind.TEXT
    return models.Conversation(
        also_present=_row_to_roster(_column(row, "also_present")),
        conversation_id=row["conversation_id"],
        owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
        surface=Surface(row["surface"]),
        title=row["title"],
        started_at=_parse_iso(row["started_at"]),
        last_activity_at=_parse_iso(row["last_activity_at"]),
        origin=origin,
        kind=kind,
        message_count=int(count) if count is not None else 0,
        persona=str(persona) if persona else None,
        hidden_at=_parse_iso(str(hidden_at)) if hidden_at else None,
        hidden_by=str(hidden_by) if hidden_by else None,
        group_name=str(group_name) if group_name else None,
    )
