"""Transcript rows, read back as a conversation on the screen.

Everything on the Chat screen that turns what the store kept into what a person
reads: who said one message (§5.3), the rail of earlier threads and the
headings they are filed under (§5.5), the messages of the thread that is open,
and the history the next turn is given.

A file of its own because none of it needs a request, a store or a runner. It
is given rows and it answers with plain data, which is why the whole of it is
testable without a server and why ``chat.py`` can be read without it.

**It deliberately does not decide anything about a turn.** What one reply cost,
whether it can be spoken, and what a refusal looks like are
:mod:`personacore.web.screens.chat_reply`'s; who speaks next is
:mod:`personacore.web.screens.chat_voices`'. Nothing here touches a
template, and nothing here reads a setting.

Split out of ``chat.py`` unchanged (ADR-0040). Every name below is still
importable from that module, because the screen is what the rest of this
package knows about.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from personacore.admin.models import AdminUser
from personacore.audit.models import (
    AuditCategory,
    AuditRecord,
    Author,
    AuthorKind,
    MessageRole,
    TranscriptRecord,
)
from personacore.conversations.models import (
    MAX_TITLE_LENGTH,
    SESSION_GAP,
    UNTITLED,
    Conversation,
    ConversationKind,
    derive_title,
)
from personacore.web.markdown import render_markdown
from personacore.web.screens.chat_attachments import (
    ATTACHMENTS_ACTION,
    ATTACHMENTS_CATEGORY,
    attachments_from_detail,
)
from personacore.web.screens.chat_reply import (
    TURN_METRICS_ACTION,
    TURN_METRICS_CATEGORY,
    _latency,
    _metrics_fields,
)
from personacore.web.screens.chat_workspace import (
    WORKSPACE_FILES_ACTION,
    WORKSPACE_FILES_CATEGORY,
    workspace_files_from_detail,
)
from personacore.web.shared import _human_gap

CHAT_HISTORY_MESSAGES = 20
"""How many earlier messages — ten exchanges — go into the next turn's prompt.

A conversation cannot be allowed to grow into the context window without limit:
past some length the model silently loses the *start* of the conversation, or
the host refuses the request outright, and both look to an operator like the
assistant becoming stupid for no reason. Twenty messages is comfortably inside
any context this project targets while being long enough that "it remembers
what we were talking about" is true in practice.

The cap trims the **oldest** messages, so the most recent exchanges — the ones
a follow-up question is actually about — always survive.
"""

CHAT_TRANSCRIPT_WINDOW = 120
"""Transcript rows read to build those messages.

The store returns rows, and rows are not all conversation: one turn writes a
``tool`` row per tool call as well (see ``AgentLoop._transcript``), and those
are not history — a tool result belongs to the round that produced it. So a
fixed window is read and then filtered, for the same reason
:data:`LOG_RECORD_WINDOW` exists: asking the store for "twenty messages" would
mean guessing how many rows that is.
"""

CHAT_AUDIT_WINDOW = 400
"""Audit rows read to find one thread's tool calls, when it is read back.

Every tool call writes an :class:`~personacore.audit.models.AuditRecord`
(category ``TOOL_CALL``) sharing its turn's ``correlation_id`` with the
transcript rows either side of it — the same fact the Logs screen already
reads back (:func:`~personacore.web.screens.logs.log_exchanges`). A
caller bounds the query to the thread's own span (its first row's timestamp to
its last), so this is a ceiling on one thread's own tool traffic rather than a
guess at how many exchanges that is — the same reasoning
:data:`CHAT_TRANSCRIPT_WINDOW` gives for messages.
"""

CONVERSATION_SCAN_WINDOW = 400
"""Transcript rows read to build the rail of earlier conversations.

Deliberately a *row* budget and not a day count. What an operator wants from
this list is "the threads I can still get back to", and how far back that
reaches is decided by the retention window (spec §7) and by how much they talk
— neither of which this screen is entitled to guess at. Four hundred rows is
roughly a fortnight of ordinary use and one query.
"""

CONVERSATION_LIST_LIMIT = CONVERSATION_SCAN_WINDOW
"""How many conversation rows the screen asks the store for.

Not :data:`CONVERSATIONS_SHOWN`, and deliberately much larger. The rail shows
thirty; this list is also what says which conversations are **hidden**, and a
hidden thread that fell off the end of it would come back onto the screen the
moment somebody had thirty newer ones.

Tied to :data:`CONVERSATION_SCAN_WINDOW` rather than to a number of its own,
because that window is what decides how many threads can appear at all: a
conversation needs at least one row in it to be on the screen, so a list as
long as the row budget cannot be outrun by one.
"""

CONVERSATIONS_SHOWN = 30
"""How many threads the rail lists. Past this the list stops being something
you scan and becomes something you search, and there is no search yet — so it
says plainly that older ones are in the log view rather than growing without
end."""

UNGROUPED_HEADING = "Ungrouped"
"""What the rail calls the conversations nobody filed. They come last, because
"everything else" under a heading somebody chose would put a label on a
decision nobody made."""

ASSISTANT_UNATTRIBUTED = "Assistant"
"""The name over a reply whose author was never recorded — every message
written before authorship existed (§5.3).

Not "unknown", which the contract forbids, and not a guess at a persona: the
role is a fact the row carries and the name it can be worked out from. It has
no parentheses, which is the whole signal — see :func:`author_label`.
"""

MAX_AUTHOR_CHARS = 80
"""How much of a name goes over a message.

A persona's display name comes out of a file an operator wrote, so it is not
core-controlled (spec §7). The template escapes it, so this is not about
injection: it is that a name a kilobyte long must not push the conversation off
the screen.
"""

#: Parentheses, which are removed from a name and from a model id before either
#: is printed. §5.3's rule is that a name in parentheses is a model and nothing
#: else ever is, so a persona called ``Bob (test)`` would make every human on
#: the screen look like a persona. Dropping the brackets keeps the one signal
#: the reader is asked to rely on.
_BRACKETS = re.compile(r"[()]")

#: Whitespace of every kind, collapsed so a name stays on one line.
_SPACES = re.compile(r"\s+")


def _plain(text: str) -> str:
    """One name, on one line, with no parentheses in it."""
    return _SPACES.sub(" ", _BRACKETS.sub("", text)).strip()[:MAX_AUTHOR_CHARS].strip()


def author_parts(author: Author | None, *, fallback: str = "") -> tuple[str, str | None]:
    """Who said one message, and what model spoke through them — split, never
    joined back into one string.

    The name is always there (``fallback`` when there is nothing else to call
    them); the model is ``None`` for a person, and for a persona whose model
    was never learned — a turn that failed before the first chunk knows who
    was asked and not what answered. :func:`author_label` calls this and
    glues the two back together for the callers that still want the one
    string; a caller that wants to print them apart — the reply's own header,
    now that the model lives in the collapsed bar under it rather than beside
    the name — calls this instead.
    """
    if author is None:
        return _plain(fallback), None
    name = _plain(author.name) or _plain(fallback)
    if author.kind is AuthorKind.PERSONA and author.model:
        model = _plain(author.model)
        if model:
            return name, model
    return name, None


def author_label(author: Author | None, *, fallback: str = "") -> str:
    """Who said one message, as the one combined string (§5.3's original
    shape). ``Alex`` for a person. ``Aria (small-model)`` for a persona: the
    name, then the model that actually spoke through it, in parentheses.
    Built from :func:`author_parts`, which is what a caller wanting the two
    apart reaches for instead.

    **This is no longer the signal that tells a person from a persona on the
    Chat screen.** It used to be: a name with no parentheses over a message
    was a person and one with them was a persona, and that held only for as
    long as the model rode along inside the same string. It stopped being true
    the day the model moved into the reply's own collapsed bar
    (``fragments/chat_exchange_body.html``) — a persona's header now reads
    ``Aria``, the same shape as a person's ``Alex``, because the bracket that
    used to ride with it is not printed there any more. **The signal on
    screen now is alignment and colour**: a person's message sits
    right-aligned in the accent bubble, a persona's reply sits left-aligned in
    the surface one, and that pairing — not the presence of a bracket — is
    what tells the two apart.

    The bracket rule this function still applies is real work wherever the
    combined string is still what gets printed: the room's own participant
    line ("In here: Alex · Aria (small-model)", ``fragments/chat_room.html``)
    and a saved conversation's transcript (``chat_save.SavedMessage``) are
    both a list or a document rather than a bubble with its own alignment, so
    the parenthesis is the only thing telling the two kinds of name apart in
    either of them.

    ``None`` is a row written before authorship existed. It renders as
    ``fallback`` — the name the screen can work out, which is the operator's own
    for a message they typed and :data:`ASSISTANT_UNATTRIBUTED` for a reply —
    with no parentheses and never the word "unknown". A persona whose model was
    never learned (a turn that failed before the first chunk) is likewise a name
    on its own: less than the whole truth, never a wrong one.
    """
    name, model = author_parts(author, fallback=fallback)
    return f"{name} ({model})" if model else name


def participants(
    rows: Sequence[TranscriptRecord], *, human: str = "", assistant: str = ASSISTANT_UNATTRIBUTED
) -> list[str]:
    """Everybody who has said anything in this conversation (§5.4).

    Rendered by :func:`author_label`, so a persona is named with its model here
    exactly as it is over its messages — one spelling of a participant, not two.
    The same persona answering on two models appears twice, deliberately: they
    are two different things to have been talking to.

    Order is first-appearance rather than alphabetical: a room is introduced in
    the order people spoke in it.
    """
    seen: list[str] = []
    for record in rows:
        if record.role not in (MessageRole.USER, MessageRole.ASSISTANT):
            continue
        fallback = human if record.role is MessageRole.USER else assistant
        label = author_label(record.author, fallback=fallback)
        if label and label not in seen:
            seen.append(label)
    return seen


@dataclass(frozen=True, slots=True)
class ChatHistoryMessage:
    """One earlier message, in the shape a
    :class:`~personacore.admin.protocols.ChatRunner` takes.

    A plain object of exactly two fields, so the only thing this surface can
    ever hand the agent loop is a ``user`` or ``assistant`` line of the
    conversation it just read back — never a ``system`` message, which would be
    a route past the persona and the safety block (ADR-0005) that starts at a
    web form.
    """

    role: str
    content: str


def conversation_start(value: str | None) -> datetime | None:
    """The instant the page says this conversation began, or ``None``.

    ``None`` for absent, empty or unparseable — and ``None`` means *no history*,
    which is the safe direction to fail in: the worst outcome is an assistant
    that has forgotten the last thing it said, rather than one answering with
    somebody else's sentences.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


_PLUS_EATEN = re.compile(r"(\d{2}:\d{2}:\d{2}(?:\.\d+)?) (\d{2}:\d{2})$")
"""An ISO instant whose ``+`` was eaten on the way through a query string.

``2026-08-27T09:00:00+00:00`` is a perfectly ordinary conversation identity and
``+`` is a perfectly ordinary space in ``application/x-www-form-urlencoded``, so
one arrives as the other the moment somebody types the address by hand, copies
it out of a log line, or writes a link without encoding it. The rendered links
encode it properly; this repairs the ones that did not, because the alternative
is a bookmark that silently opens a *different, empty* conversation — which
looks exactly like the thread having been lost.
"""


def wanted_conversation(value: str | None) -> str | None:
    """One ``?c=`` or hidden-field value, repaired if a query string ate its
    offset. Anything else is passed through untouched for
    :func:`conversation_start` to accept or reject."""
    if not value:
        return None
    return _PLUS_EATEN.sub("\\1+\\2", value, count=1)


def conversation_history(
    records: Sequence[TranscriptRecord], *, limit: int = CHAT_HISTORY_MESSAGES
) -> list[ChatHistoryMessage]:
    """Transcript rows — newest first, as the store returns them — as history.

    ``tool`` and ``system`` rows are dropped (see
    :data:`CHAT_TRANSCRIPT_WINDOW`), the newest ``limit`` messages are kept, and
    the result comes back oldest-first because that is the order a conversation
    is read in.

    A runbook run's rows are dropped too — see the loop below.
    """
    kept: list[ChatHistoryMessage] = []
    for record in records:
        if record.role not in (MessageRole.USER, MessageRole.ASSISTANT):
            continue
        # A runbook run writes its scripted prompts and their replies to this
        # conversation as ordinary user/assistant rows, because they are the
        # record of what the run did (ADR-0004) and the page draws them. They
        # are not *history* for the next person who types here, though: replaying
        # a script's turns would answer them out of instructions they never saw.
        if record.author is not None and record.author.kind == AuthorKind.RUNBOOK:
            continue
        kept.append(ChatHistoryMessage(role=record.role.value, content=record.content))
        if len(kept) >= limit:
            break
    kept.reverse()
    return kept


# ---------------------------------------------------------------------------
# Conversations — separate, owned, resumable threads
# ---------------------------------------------------------------------------
#
# The vocabulary comes from ``personacore.conversations.models``: what a
# conversation is called (``derive_title``), and how long a silence has to be
# before it is two conversations rather than one (``SESSION_GAP``). Both are
# *consumed* here and neither is redefined, so the rail splits threads at the
# same place the store's own backfill does and names them the same way.
#
# **Grouping goes through one seam**, :func:`_grouped`, which reads a row's
# ``conversation_id`` when it has one and falls back to the gap rule when it
# does not. That fallback is now the exception rather than the rule: the store
# does list, read and attach, and every message this screen produces is
# attached to a real conversation row before the reply is rendered.
#
# **How a message ends up in the right thread.** The screen names a thread by
# an instant — the moment it was opened, or the moment its first message
# landed. ``ConversationService.at`` turns either into the conversation row
# (creating one the first time something is said), and
# ``ConversationService.append`` claims the rows the turn just wrote, which the
# agent loop writes knowing nothing about conversations (ADR-0004). So
# resuming yesterday's thread and asking a follow-up puts the follow-up in
# *yesterday's* thread, which is the thing that makes a conversation resumable
# rather than merely readable.
#
# The gap-rule fallback stays for two real cases: a core assembled with an
# audit store that has no conversation methods at all, and rows written before
# any of this existed on an install where the startup backfill has not yet
# reached them.


@dataclass(frozen=True, slots=True)
class ConversationRow:
    """One earlier conversation, in the shape the rail renders.

    ``id`` is the ISO-8601 instant it began, which is the same value the
    composer carries in its hidden field and the same value ``?c=`` in the URL
    holds — one identity for the thread across the page, the address bar and
    the form, rather than three that have to agree.
    """

    id: str
    title: str
    when: str
    messages: int
    active: bool = False

    group: str | None = None
    """The label this thread is filed under, or ``None`` for ungrouped. Read
    from the conversation row rather than inferred from anything: a group is
    free text somebody typed (see ``Conversation.group_name``)."""

    conversation_id: str | None = None
    """The store's own identity for this thread, when it has one.

    Carried beside :attr:`id` rather than instead of it. ``id`` stays the
    instant, because that is what the composer, the rail and ``?c=`` all agree
    on across this screen; this is here so a row can be *told apart* from the
    conversation the store knows about — which is how the rail gets a title
    somebody typed instead of one derived from the opening message.
    """

    kind: ConversationKind = ConversationKind.TEXT
    """Which glyph this row draws (docs/contracts/conversation-icons.md §§2, 5).

    Read straight off :attr:`Conversation.kind` for a thread the store knows
    about, and computed as :attr:`~ConversationKind.TEXT` — never stored — for
    one it does not: a thread that predates conversations, or one still
    reached through the gap-rule fallback, is a text conversation exactly the
    way :class:`Conversation` itself already treats a row with nothing
    recorded. This is presentation only; nothing reads it to decide what a
    conversation can do.
    """


@dataclass(frozen=True, slots=True)
class RailGroup:
    """One heading in the rail, and the conversations under it.

    ``name`` is ``None`` for the ungrouped ones, which come last (§5.5). The
    grouping is done here rather than in the template because Jinja's
    ``groupby`` sorts by the attribute, and sorting ``None`` beside a string
    raises — a rail that dies because somebody has not filed everything would
    be a poor trade for four lines of Python.
    """

    name: str | None
    rows: list[ConversationRow]


def _local_day(moment: datetime, *, now: datetime) -> str:
    """When a conversation happened, as somebody would say it.

    Today and yesterday get named rather than dated, because those are the two
    an operator is actually looking for and a timestamp makes them hunt.
    """
    day = moment.date()
    today = now.date()
    if day == today:
        return moment.strftime("%H:%M")
    if (today - day).days == 1:
        return f"yesterday {moment.strftime('%H:%M')}"
    if (today - day).days < 7:
        return moment.strftime("%a %H:%M")
    return moment.strftime("%d %b").lstrip("0")


def _grouped(records: Sequence[TranscriptRecord]) -> list[list[TranscriptRecord]]:
    """Transcript rows, oldest first, split into conversations.

    Two rules, in this order. A row that names a conversation belongs to that
    conversation, full stop — the store said so and this screen does not
    second-guess it. A row that names none is grouped by silence: a gap longer
    than ``SESSION_GAP`` starts a new thread, which is the same inference
    ``AuditStore.backfill_conversations`` is specified to make, drawn from the
    same constant so the two can never disagree about where yesterday ended.
    """
    groups: list[list[TranscriptRecord]] = []
    named: dict[str, list[TranscriptRecord]] = {}
    previous: TranscriptRecord | None = None
    for record in records:
        key = record.conversation_id
        if key:
            bucket = named.get(key)
            if bucket is None:
                bucket = []
                named[key] = bucket
                groups.append(bucket)
            bucket.append(record)
            previous = None
            continue
        if (
            previous is None
            or previous.conversation_id
            or record.timestamp - previous.timestamp > SESSION_GAP
        ):
            groups.append([])
        groups[-1].append(record)
        previous = record
    return [group for group in groups if group]


def _spoken(group: Sequence[TranscriptRecord]) -> list[TranscriptRecord]:
    """Just the conversation out of a group — no tool rows, no system rows."""
    return [record for record in group if record.role in (MessageRole.USER, MessageRole.ASSISTANT)]


def _run_notices(group: Sequence[TranscriptRecord]) -> list[TranscriptRecord]:
    """A run's own progress rows out of a group (contract ``runbook.md`` §3).

    ``system``-role rows a runbook wrote — the per-step and per-item notices
    :meth:`personacore.runbooks.runner.Runner._say` posts. They are not
    messages and are never counted as any, but they *are* the whole content
    of a conversation a run made and nobody typed in: a runbook-level
    ``foreach`` parent (§1.12) holds nothing else for its whole life.
    """
    return [
        record
        for record in group
        if record.role is MessageRole.SYSTEM
        and record.author is not None
        # `==`, not `is` — WAVE2.md's rework item 3, and the same comparison
        # `conversation_history` above makes for the same reason: a row read
        # back from a store that does not validate carries the raw string,
        # which equals `AuthorKind.RUNBOOK` (a `StrEnum`) without being it.
        and record.author.kind == AuthorKind.RUNBOOK
    ]


def anchor_row(group: Sequence[TranscriptRecord]) -> TranscriptRecord | None:
    """The row a conversation is identified and dated by, or ``None`` when the
    group is not one the rail shows.

    The first **spoken** row, unchanged for every conversation that has one —
    everything before runbooks. Falling back to the first row a *run* wrote,
    so a conversation whose only content is a run's own progress lines is a
    conversation rather than nothing: without it a parent run's conversation
    (contract §1.12) has no rail row, and the heading over its messages —
    read off that row by :func:`_room` — falls back to "Untitled
    conversation" while the store holds the runbook's own title.

    One function because the rail (:func:`conversation_rows`), the open
    thread (:func:`thread_identity`) and the thread lookup
    (:func:`thread_records`) all have to pick the *same* row: they agree on
    which row that is by asking here, rather than by three rules that can
    drift apart and leave a row in the list that nothing ever lights.
    """
    spoken = _spoken(group)
    if spoken:
        return spoken[0]
    notices = _run_notices(group)
    return notices[0] if notices else None


def conversation_rows(
    records: Sequence[TranscriptRecord],
    *,
    active: str | None,
    now: datetime,
    limit: int = CONVERSATIONS_SHOWN,
    known: Mapping[str, Conversation] | None = None,
) -> list[ConversationRow]:
    """The rail, newest activity first.

    Rows the operator cannot see the point of are left out: a group with
    neither a spoken message nor a run's own progress line in it is tool
    traffic, not a conversation, and a list padded with untitled empty rows is
    a list nobody trusts. **A run's rows count as activity** — see
    :func:`anchor_row`. A conversation a runbook made and nobody typed in is
    still one the operator has to be able to reach, and a parent run's
    (contract §1.12) holds nothing but those lines from start to finish.

    ``known`` is what the store says about these threads, keyed by
    conversation id — the title somebody typed (§5.5) and the group they filed
    it under. It is consulted rather than relied on: a thread whose rows name
    no conversation, or one the store has not caught up with, keeps the title
    derived from its opening message, which is what the whole rail did before
    renaming existed.
    """
    rows: list[ConversationRow] = []
    for group in _grouped(records):
        anchor = anchor_row(group)
        if anchor is None:
            continue
        spoken = _spoken(group)
        # A run's own conversation is dated by its last progress line, an
        # ordinary one by its last message — `group[-1]` for the first and
        # `spoken[-1]` for the second, which is the same row whenever there
        # is a spoken one at all.
        latest = spoken[-1] if spoken else group[-1]
        started = anchor.timestamp
        identity = started.isoformat()
        opening = next(
            (record.content for record in spoken if record.role is MessageRole.USER),
            None,
        )
        conversation_id = anchor.conversation_id
        room = (known or {}).get(conversation_id) if conversation_id else None
        rows.append(
            ConversationRow(
                id=identity,
                title=(room.title if room and room.title else derive_title(opening)),
                when=_local_day(latest.timestamp, now=now),
                # A progress line is not a message and is never counted as
                # one: a parent run's conversation honestly reads "0
                # messages" until somebody types in it.
                messages=len(spoken),
                active=identity == active,
                group=getattr(room, "group_name", None),
                conversation_id=conversation_id,
                kind=getattr(room, "kind", None) or ConversationKind.TEXT,
            )
        )
    rows.sort(key=lambda row: row.id, reverse=True)
    return rows[:limit]


def railed(rows: Sequence[ConversationRow]) -> list[RailGroup]:
    """The rail's rows under their group headings, ungrouped last (§5.5).

    Groups keep the order their newest conversation gives them, so the rail
    still reads newest-first from the top — a rail sorted alphabetically by
    group would move yesterday's thread above this morning's for no reason the
    reader asked for.
    """
    order: list[str] = []
    filed: dict[str, list[ConversationRow]] = {}
    loose: list[ConversationRow] = []
    for row in rows:
        if not row.group:
            loose.append(row)
            continue
        if row.group not in filed:
            filed[row.group] = []
            order.append(row.group)
        filed[row.group].append(row)
    groups = [RailGroup(name=name, rows=filed[name]) for name in order]
    if loose:
        groups.append(RailGroup(name=None, rows=loose))
    return groups


def thread_records(
    records: Sequence[TranscriptRecord],
    started: datetime | None,
    *,
    conversation_id: str | None = None,
) -> list[TranscriptRecord]:
    """The rows belonging to the conversation the screen is showing.

    ``conversation_id`` is **the answer whenever the screen has one, full
    stop.** The caller has already resolved the address it was given to a
    conversation (``ConversationService.at``); asking here for the group that
    names it is then a lookup rather than an inference. **When an id is given
    and no group carries it, the answer is an empty list — the instant
    fallback below is never reached.** A conversation the id names but that
    has written no row yet is indistinguishable, from here, from one that
    never will; both are the real, ordinary "nothing said yet" state
    :func:`thread_identity` and the screen already render as empty, so
    guessing at a row is not needed and is exactly the mistake this rule
    exists to rule out (next paragraph).

    **The instant is only for a caller with no id at all.** It is the first
    group whose own :func:`anchor_row` is at or after ``started``. ``None``,
    or an instant nothing was said after, is an empty conversation, which is
    a real state (the screen has just been opened) and not an error.

    **The instant must never be asked to stand in for an id that named
    nothing.** A parent run (contract §1.12) creates its item's conversation
    *before* it writes its own "chapter 1: running" line, so the item
    conversation's own start instant is earlier than the parent's first row.
    The item's conversation id is known — it is what the item line's "Open"
    link carries — so falling through to the instant for it, the moment it
    has zero rows of its own, hands back the parent's transcript instead:
    that is what put a parent's progress row on a page whose run status,
    composer and persona were all the item's. Returning ``[]`` for an id that
    names nothing, rather than searching by instant, is what keeps a
    zero-row child conversation from ever being answered with a sibling's.

    The rows returned are the whole group **except** ``tool``-role ones —
    audit-only rows never meant to be their own line — so a runbook's own
    ``system`` notices (contract §3 "Progress in the chat") ride along with
    the ordinary conversation rather than being stripped before
    :func:`transcript_exchanges` ever sees them.
    """
    groups = _grouped(records)
    if conversation_id:
        for group in groups:
            if any(row.conversation_id == conversation_id for row in group):
                return [row for row in group if row.role is not MessageRole.TOOL]
        return []
    if started is None:
        return []
    for group in groups:
        # `anchor_row`, widened to *any* ``system`` row: this fallback has
        # always accepted one, and narrowing it to a run's own rows here
        # would drop a thread off the screen rather than off the rail.
        anchor = anchor_row(group) or next(
            (row for row in group if row.role is MessageRole.SYSTEM), None
        )
        if anchor is not None and anchor.timestamp >= started:
            return [row for row in group if row.role is not MessageRole.TOOL]
    return []


def bulk_selected_rows(
    rows: Sequence[ConversationRow], markers: Sequence[str], *, select_all: bool
) -> list[ConversationRow]:
    """Which of this owner's own rail rows a bulk-delete form actually named
    (docs/contracts/conversation-list-bulk-actions.md, rule 3).

    ``rows`` is this owner's own rail, read fresh by the caller and never
    anything else — so a marker naming nothing on it (tampered, stale, or
    somebody else's conversation entirely) simply selects nothing, the same
    non-committal outcome a stray id already gets from
    :func:`~personacore.web.screens.chat_room.chat_hide`. Order is the
    rail's own, not the form's, and a marker repeated in the post selects its
    row once.

    ``select_all`` is resolved against this same list rather than a wider
    one: a checkbox can only exist on screen for a row the rail actually
    rendered, so "all of them" means all of what is on screen and not every
    conversation this owner has ever had.

    **``select_all`` applies only when no row was checked, and the row
    checkboxes win whenever it is a contest.** This used to be a plain
    ``if select_all: return list(rows)``, which discarded the markers
    entirely, and the owner lost conversations to it within minutes of the
    release: selecting all, then unselecting a single conversation, then
    deleting the marked set deleted everything, including the one row that had
    just been unchecked.

    With a script running, ticking Select all ticks every row -- so unticking
    one leaves the rest ticked, and honouring the *markers* is what was
    intended. The flag stayed ticked because nothing untied it, and the flag
    was being believed over the boxes that were visibly unchecked on screen.

    Without a script the flag cannot tick anything, so a post carrying it and
    no markers is the only way to say "all of them" -- which is exactly the
    case this still serves. A post carrying both is ambiguous, and for a
    destructive action the safe reading of an ambiguous instruction is the
    narrower one.
    """
    if select_all and not markers:
        return list(rows)
    wanted = {
        repaired
        for repaired in (wanted_conversation(marker) for marker in markers)
        if repaired
    }
    return [row for row in rows if row.id in wanted]


def thread_identity(rows: Sequence[TranscriptRecord], fallback: str) -> str:
    """Which rail row a page is showing.

    A conversation opened at 09:00 whose first message landed at 09:04 is the
    same conversation under two instants; the rail knows it by the message, the
    composer by the opening. This resolves the first to the second so exactly
    one row is highlighted, and falls back to the composer's own value while
    nothing has been said yet.

    Read through :func:`anchor_row`, the same row :func:`conversation_rows`
    builds its ``id`` from, so "which row is lit" and "which row exists" can
    never name two different instants. That matters for a conversation a run
    made: its rows open with a progress line and not with a message, so
    ``rows[0]`` and the rail's own anchor are the same row there only because
    both ask the same question.
    """
    anchor = anchor_row(rows)
    return anchor.timestamp.isoformat() if anchor is not None else fallback


def transcript_exchanges(
    rows: Sequence[TranscriptRecord],
    *,
    human: str = "",
    assistant: str = ASSISTANT_UNATTRIBUTED,
    audit_rows: Sequence[AuditRecord] = (),
    reasoning_by_correlation: Mapping[str, str] | None = None,
    context_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Earlier messages in the same shape a fresh turn renders in.

    One shape for a message whether it arrived a second ago or is being read
    back tomorrow, so ``fragments/chat_exchange.html`` is the only thing that
    knows what a message looks like.

    **Less of this is unrecoverable than it looks.** How long the turn took and
    which tools it called both turned out to already be on disk, in the rows
    the store already keeps — the Logs screen has read the first back this way
    since it existed (:func:`~personacore.web.screens.logs.log_exchanges`)
    and the second from every ``TOOL_CALL`` audit record's own ``duration_ms``
    (``agent.loop`` times the call and writes it down whether or not anybody
    ever asks). Both are filled in here, from ``audit_rows`` and the rows'
    own timestamps, rather than left blank a second time.

    What genuinely is not recoverable: how many tools were *offered* (only a
    count of what was actually called is ever written down), and the
    turn-internal timing breakdown — first token, token rate, first audio.
    Nothing durable ever held those; they live only in the in-memory counters
    a live SSE turn keeps while it streams
    (:class:`~personacore.web.screens.chat_reply.TurnMetrics`), and are
    gone with the process that produced them. Those stay absent rather than
    guessed at, same as the audio: a handle is minted per reply and does not
    outlive the process, which is why a replayed reply is resynthesised on
    request instead (:mod:`personacore.web.screens.chat_audio`).

    What it *can* have, and what a fresh turn cannot always, is who said each
    message: the author is on the row (§3.1), so a conversation read back
    tomorrow names the persona and the model that answered at the time — not
    the one that would answer now. ``human`` and ``assistant`` are the names
    used for rows written before authorship existed; see :func:`author_label`.

    ``reasoning_by_correlation`` is a reply's own thinking, keyed by the same
    correlation id ``audit_rows`` is matched by — read back from
    :class:`~personacore.audit.models.ReasoningRecord`'s own table, not from
    ``audit_rows``, because that is not where it is kept (see that model's
    docstring for why reasoning is not filed beside a turn's metrics). Built
    by the caller, the same way ``audit_rows`` is: this module does not hold a
    store. Absent — the default, an empty mapping — for every message this
    screen renders, and for a reply the model produced no reasoning for.

    ``context_limit`` is the backend's own context window, read fresh by the
    caller (:meth:`~personacore.llm.client.LLMClient.context_length`) — a fact
    about the connection as it stands today, not about the turn being read
    back, so it is asked once per render rather than persisted per message.
    ``None`` when it cannot be known, which prints every turn's own token count
    with no denominator rather than a guessed one.
    """
    # Local, not a module-level import: another builder is in this same file's
    # `loop.py`/store neighbourhood at the same time this alpha's gate-context
    # task is landing, so this function's own edit stays inside this function
    # rather than touching the shared import block above. `QUESTIONS_PROMPT`
    # is the fixed core prompt every `questions: model` gate turn is built
    # from (`personacore.runbooks.gates.questions_prompt`) — a runbook's own
    # prompt file can never coincidentally start with it, so it identifies
    # that one turn's own user row precisely.
    from personacore.runbooks.gates import QUESTIONS_PROMPT, GateError, parse_questions

    reasoning = reasoning_by_correlation or {}
    exchanges: list[dict[str, Any]] = []
    # `pending` — the exchange still waiting for its reply — is tracked
    # separately from `exchanges[-1]` so a `system` row appended in between
    # (a runbook's own progress line, PLAN.md web row alpha.19) never becomes
    # the thing the next real reply is folded into. Before this existed the
    # two were the same variable, which is exactly what made a notice safe to
    # insert here: nothing below reads `exchanges[-1]` for pairing any more.
    pending: dict[str, Any] | None = None
    asked_at: datetime | None = None
    for record in rows:
        if record.role is MessageRole.SYSTEM:
            exchanges.append(_notice_entry(record, audit_rows=audit_rows))
            continue
        if record.role is MessageRole.USER:
            entry = _replayed(
                message=record.content,
                author=author_label(record.author, fallback=human),
            )
            # A run's own machine-written prompt (WAVE2.md's "Run prompt
            # rows"): recorded as an ordinary `user` row, author kind
            # `runbook`, so it still pairs with whatever answered it exactly
            # like a person's own message does — only how it is *drawn*
            # changes (fragments/chat_exchange_body.html reads
            # `runbook_prompt` and collapses it). The label reuses the
            # author's own name (the step's — "Pass p2", say) exactly as
            # every other row on this screen is headed by it; nothing new is
            # asked of the row to build it.
            if record.author is not None and record.author.kind == AuthorKind.RUNBOOK:
                step_label = _plain(record.author.name) or "Runbook"
                entry["runbook_prompt"] = True
                entry["runbook_prompt_label"] = (
                    f"{step_label} prompt · {len(record.content):,} characters"
                )
                # This alpha's own finding: a gate's own questions turn and an
                # ordinary model step's turn both write author kind `runbook`
                # on every row they leave, so the reply is indistinguishable
                # from a model step's own (the flags, the edited text) by
                # author alone — and a model step's own reply must stay
                # visible. The fixed prompt text is what tells them apart.
                if record.content.startswith(QUESTIONS_PROMPT):
                    entry["gate_questions_turn"] = True
            exchanges.append(entry)
            pending = entry
            asked_at = record.timestamp
        elif pending is not None and not pending["reply"]:
            _fill_reply(
                pending,
                record,
                fallback=assistant,
                audit_rows=audit_rows,
                asked_at=asked_at,
                reasoning_by_correlation=reasoning,
                context_limit=context_limit,
            )
            if pending.pop("gate_questions_turn", False):
                # Fold the reply away like the run's own prompt row above it
                # — the raw questions JSON is not something a person reads,
                # it is what the gate card was built from.
                try:
                    count = len(parse_questions(record.content).questions)
                    summary = f"questions · {count}"
                except GateError:
                    summary = "questions"
                pending["gate_questions_reply"] = True
                pending["gate_questions_summary"] = summary
            pending = None
            asked_at = None
        else:
            # No `reply_author=` here. `_fill_reply` sets it on the next line
            # from `author_parts`, so anything passed in is overwritten before
            # it can be read — and passing the combined `author_label` string
            # read as though a replayed reply still headed itself `Aria
            # (small-model)` while a fresh one headed itself `Aria`. It does not;
            # the two agree, which is the property this whole path exists for.
            entry = _replayed()
            _fill_reply(
                entry,
                record,
                fallback=assistant,
                audit_rows=audit_rows,
                asked_at=None,
                reasoning_by_correlation=reasoning,
                context_limit=context_limit,
            )
            exchanges.append(entry)
            pending = None
    return exchanges


def _notice_entry(record: TranscriptRecord, *, audit_rows: Sequence[AuditRecord]) -> dict[str, Any]:
    """A ``system``-role row drawn as a quiet notice rather than a bubble.

    A runbook's own progress line (contract ``working/contracts/runbook.md``
    §3, "Progress in the chat"; PLAN.md's ``web`` row, alpha.19) — never part
    of either side of the conversation, so it gets no author, no reply-fill
    pairing and none of a turn's own metrics. ``fragments/chat_exchange_body.
    html`` reads ``kind`` to draw this in place of the whole of the rest of
    that template.

    ``workspace_files`` reuses the exact read :func:`_fill_reply` gives a
    model reply's own files (:func:`_workspace_detail_for`, keyed by this
    row's own correlation id) — a tool step's output is shown as the same
    card a model's own tool call draws, per the contract's "the file as a
    card."
    """
    return {
        "kind": "notice",
        "text": record.content,
        "workspace_files": workspace_files_from_detail(
            _workspace_detail_for(audit_rows, record.correlation_id)
        ),
    }


def _fill_reply(
    entry: dict[str, Any],
    record: TranscriptRecord,
    *,
    fallback: str,
    audit_rows: Sequence[AuditRecord],
    asked_at: datetime | None,
    reasoning_by_correlation: Mapping[str, str] = MappingProxyType({}),
    context_limit: int | None = None,
) -> None:
    """Complete a replayed exchange with the reply that answered it, and
    everything about that turn the store still has.

    ``asked_at`` is the timestamp of the question this reply belongs to, when
    there is one on this same exchange — a room's second and third replies
    share their *own* correlation id but not the person's original message
    (:mod:`personacore.web.screens.chat_streaming` mints one per turn), so
    there is nothing on disk to measure their duration against and it is left
    absent rather than computed from the wrong pair of rows.

    ``context_limit`` is passed straight through to :func:`_metrics_fields`;
    see :func:`transcript_exchanges` for why it is asked fresh rather than
    read back off this turn's own persisted detail.
    """
    entry["reply"] = record.content
    entry["reply_html"] = render_markdown(record.content)
    entry["ok"] = True
    # Split rather than the combined `author_label` string: the header prints
    # the name alone, and the model — when there is one to know — goes in the
    # bar's collapsed region instead (see `author_parts`).
    name, model = author_parts(record.author, fallback=fallback)
    entry["reply_author"] = name
    entry["reply_model"] = model or ""
    entry["correlation_id"] = record.correlation_id
    entry["reply_persona_name"] = (
        record.author.name
        if record.author is not None and record.author.kind is AuthorKind.PERSONA
        else None
    )
    if asked_at is not None:
        # The same measurement the Logs screen makes
        # (`logs.log_exchanges`): the span between the first row of the turn
        # and the last, both already timestamped by the store. It is a
        # read-back of a wall clock, not the `total_ms` a live turn times with
        # `time.monotonic()` — the two can differ by a beat, which is the
        # price of not keeping a second copy of a number the rows already
        # answer between them. (`_metrics_fields` below keeps its own copy of
        # `total_ms` for exactly this reason — a beat is nothing to a duration
        # read to the nearest tenth of a second, and everything to a rate.)
        entry["duration"] = _human_gap(record.timestamp - asked_at)
    names, calls = _tool_calls_for(audit_rows, record.correlation_id)
    entry["tools_called"] = names
    entry["tool_calls"] = calls
    # A message that carried an attachment has more in its stored `content`
    # than what was typed — chat_attachments.compose_model_message appended
    # the attachment's own text for the model to read (contract §4.1), and
    # that appended text is not part of what a person is shown (contract §6:
    # a chip, "not... forty screens inlined into the transcript"). `typed_len`
    # is where the line is, recorded once at send time
    # (chat_attachments.store_pending) because there is no second field on
    # `TranscriptRecord` to keep it in. `ids` is how the chips themselves are
    # found again — see chat.py's `_attach_replay_attachments`, which is the
    # only place with a store handy to turn an id back into a record.
    attachment_ids, typed_len = attachments_from_detail(
        _attachment_detail_for(audit_rows, record.correlation_id)
    )
    if typed_len is not None:
        entry["message"] = entry["message"][:typed_len]
    entry["attachment_ids"] = attachment_ids
    # This turn's own workspace files (workspace contract §7) — the read
    # side of `chat_streaming._record_workspace_files`'s write, the same
    # correlation id every other per-turn fact on this row is read back by.
    # Names only; `chat.py`'s `_attach_replay_workspace_files` is what turns
    # them into cards, the same split `attachment_ids` above keeps for the
    # same reason (this module holds no store to hydrate them with).
    entry["workspace_files"] = workspace_files_from_detail(
        _workspace_detail_for(audit_rows, record.correlation_id)
    )
    # First token, tokens, first audio and the rate they give: absent for a
    # turn that predates this feature, or whose write failed (`chat_streaming`
    # treats a failed write as costing the replay's numbers, never the turn) —
    # `_replayed`'s own empty strings are left standing rather than
    # overwritten with a guess.
    detail = _metrics_for(audit_rows, record.correlation_id)
    if detail is not None:
        entry.update(_metrics_fields(detail, context_limit=context_limit))
    # The thinking line, read back by the same correlation id every other
    # per-turn fact on this row is. Absent — `_replayed`'s own `""` left
    # standing — for a turn that predates this feature, one whose write
    # failed, or one the model produced no reasoning for; all three read the
    # same way, which is "draw no line", exactly as a fresh reply does.
    entry["reasoning"] = reasoning_by_correlation.get(record.correlation_id, "")


def _tool_calls_for(
    audit_rows: Sequence[AuditRecord], correlation_id: str
) -> tuple[list[str], list[dict[str, str]]]:
    """The tools one turn called, read back from its own audit trail.

    Every tool call this core makes writes an
    :class:`~personacore.audit.models.AuditRecord` (category ``TOOL_CALL``)
    sharing the turn's correlation id, and ``agent.loop`` already times the
    call and writes ``duration_ms`` onto it — the exact fact this function
    reads back rather than re-measuring. Ordered by timestamp because a model
    that called the same tool twice in one turn must not be told apart from
    one that called two different tools by a dictionary collapsing the first
    call into the second.
    """
    calls = sorted(
        (
            row
            for row in audit_rows
            if row.correlation_id == correlation_id and row.category is AuditCategory.TOOL_CALL
        ),
        key=lambda row: row.timestamp,
    )
    names = [row.action for row in calls]
    detailed = [
        {"name": row.action, "took": _latency(row.detail.get("duration_ms"))} for row in calls
    ]
    return names, detailed


def _metrics_for(
    audit_rows: Sequence[AuditRecord], correlation_id: str
) -> Mapping[str, Any] | None:
    """One turn's own persisted timing, read back by the id
    :data:`~personacore.web.screens.chat_reply.TURN_METRICS_ACTION` was
    filed under — or ``None`` for a turn that predates this feature, or whose
    write failed. Both read the same way: nothing to show, not a guess.

    A room shares one correlation id between a persona's reply and nothing
    else on this list (:mod:`personacore.web.screens.chat_streaming`
    mints a fresh one per persona per turn), so — exactly like
    :func:`_tool_calls_for` — there is never more than one match and never a
    chance of one persona's numbers landing on another's reply.
    """
    return next(
        (
            row.detail
            for row in audit_rows
            if row.correlation_id == correlation_id
            and row.category is TURN_METRICS_CATEGORY
            and row.action == TURN_METRICS_ACTION
        ),
        None,
    )


def _attachment_detail_for(
    audit_rows: Sequence[AuditRecord], correlation_id: str
) -> Mapping[str, Any] | None:
    """One turn's own :data:`~personacore.web.screens.chat_attachments.
    ATTACHMENTS_ACTION` record, read back by correlation id — the read side
    of :func:`~personacore.web.screens.chat_attachments.store_pending`'s
    write, the same shape :func:`_metrics_for` reads a turn's timing back
    with and for the same reason: nothing to show is not a guess, it is a
    turn that carried no attachment or predates this feature."""
    return next(
        (
            row.detail
            for row in audit_rows
            if row.correlation_id == correlation_id
            and row.category is ATTACHMENTS_CATEGORY
            and row.action == ATTACHMENTS_ACTION
        ),
        None,
    )


def _workspace_detail_for(
    audit_rows: Sequence[AuditRecord], correlation_id: str
) -> Mapping[str, Any] | None:
    """One turn's own :data:`~personacore.web.screens.chat_workspace.
    WORKSPACE_FILES_ACTION` record, read back by correlation id — the read
    side of :func:`personacore.web.screens.chat_streaming._record_workspace_files`'s
    write, the same shape :func:`_attachment_detail_for` reads its own
    record with and for the same reason: nothing to show is not a guess, it
    is a turn whose tools kept nothing or one that predates this feature."""
    return next(
        (
            row.detail
            for row in audit_rows
            if row.correlation_id == correlation_id
            and row.category is WORKSPACE_FILES_CATEGORY
            and row.action == WORKSPACE_FILES_ACTION
        ),
        None,
    )


def _replayed(
    *, message: str = "", reply: str = "", author: str = "", reply_author: str = ""
) -> dict[str, Any]:
    """One message read back out of the transcript."""
    return {
        "message": message,
        "attachments": [],
        "attachment_notice": "",
        "attachment_ids": [],
        "workspace_files": [],
        "author": author,
        "reply_author": reply_author,
        # Filled in by `_fill_reply` for a real reply; an empty entry (the
        # message half, or the placeholder `_replayed()` builds before it
        # knows the reply) has none to report.
        "reply_model": "",
        "ok": bool(reply),
        "reply": reply,
        "reply_html": render_markdown(reply),
        # Filled in by `_fill_reply` once the reply this belongs to is known —
        # see that function's own last line. Empty here for the same reason
        # `reply_model` is: nothing has been read back yet.
        "reasoning": "",
        "error": None,
        "persona": "",
        "correlation_id": "",
        "reply_persona_name": None,
        "tools_offered": None,
        "tools_called": [],
        "tool_calls": [],
        "duration": "",
        "first_token": "",
        "token_rate": "",
        "first_audio": "",
        "token_usage": "",
        "audio_url": None,
        "audio_report_url": None,
        "voice_note": None,
        "replayed": True,
        # WAVE2.md "Run prompt rows" — set true only for a `user` row whose
        # author kind is `runbook`; every other exchange leaves these at
        # their default so the template's plain bubble is what draws.
        "runbook_prompt": False,
        "runbook_prompt_label": "",
    }


# ---------------------------------------------------------------------------
# What the header over the messages says
# ---------------------------------------------------------------------------


def _my_name(records: Sequence[TranscriptRecord], user: AdminUser) -> str:
    """What to print over a message this operator sent that carries no
    author — every message they sent before authorship existed.

    Taken from their own newest attributed message rather than invented, so
    the name over yesterday's question is the same one over today's. With
    nothing attributed at all it falls back to the account id, which is the
    name this surface already calls them everywhere else. Never "unknown",
    and never parentheses (§5.3).
    """
    for record in reversed(records):
        if record.role is MessageRole.USER and record.author is not None:
            return author_label(record.author)
    return _plain(user.id)


def _room(
    rail: Sequence[ConversationRow], rows: Sequence[TranscriptRecord], mine: str
) -> dict[str, Any]:
    """What the header says about the conversation that is open: its name,
    the group it is filed under, and who is in it.

    The name and the group come off the rail row rather than from a second
    query, so the heading over the messages and the row in the list can
    never disagree about what this thread is called.
    """
    here = next((row for row in rail if row.active), None)
    return {
        "title": here.title if here else UNTITLED,
        "group": (here.group if here else None) or "",
        "participants": participants(rows, human=mine),
        "max_title_chars": MAX_TITLE_LENGTH,
    }
