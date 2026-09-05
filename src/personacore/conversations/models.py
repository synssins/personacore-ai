"""What a conversation *is*, with no idea of where it is stored.

The owner asked for prior conversations, not just history: yesterday's exchange
still sitting there, separate from today's, and resumable. Until now the chat
screen read the last twenty transcript rows belonging to the signed-in
operator and called that the conversation — one rolling, endless transcript per
person, with no way to name a thread, leave it, and come back.

This module holds the vocabulary. It deliberately imports nothing that touches
disk: :mod:`personacore.audit.store` needs :func:`derive_title` *during a
schema migration*, and a migration that pulled in a service layer to name a
row would be a cycle waiting to happen.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from personacore.audit.models import Author, AuthorKind, Owner, Surface

# ``Author`` and ``AuthorKind`` are re-exported below rather than defined
# here. The chat-room contract names this module as where they are imported
# from, and that import path works — but the classes themselves have to live
# beside ``Owner`` in ``personacore.audit.models``, because
# ``TranscriptRecord`` carries one and this module already imports that one.
# Defining them here would make the two modules import each other.

UNTITLED = "Untitled conversation"
"""What a conversation is called before anybody has said anything in it.

Not blank: a list of blank rows is a list an operator cannot click on with any
confidence, and "the title has not been derived yet" is a real state that
lasts from opening the screen until the first message lands.
"""

MAX_TITLE_LENGTH = 60
"""How long a derived title may be, in characters.

Sixty is about what fits in a sidebar row at the design's body size without
the list turning into a wall of text. It is a *display* cap, not a data one —
the message itself is untouched in the transcript.
"""

_TITLE_SCAN_LIMIT = 4096
"""How much of an opening message :func:`derive_title` will even look at.

A pasted log file is a legitimate first message. Normalising a megabyte of it
to produce sixty characters is pure waste, and every character past this point
is guaranteed to be discarded anyway.
"""

SESSION_GAP = timedelta(hours=2)
"""How long a silence has to be before it separates two conversations.

Used only when reconstructing conversations from transcript rows written
before conversations existed (see
:meth:`personacore.audit.store.AuditStore.backfill_conversations`). Two hours
is the "you went and did something else" threshold: it keeps a morning's
back-and-forth together, including the pauses to go and look something up,
while putting yesterday evening and this morning in separate rows — which is
the whole thing the owner asked to be able to see.
"""

#: Whitespace of every kind, including the newlines a pasted message is full
#: of, collapsed to one space so a title stays one line.
_WHITESPACE = re.compile(r"\s+")

#: C0 and C1 control characters, which a title has no use for and a terminal
#: rendering of the conversation list would obey.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class ConversationOrigin(StrEnum):
    """How a conversation came to exist.

    Kept because the two are genuinely different claims. A ``LIVE``
    conversation's boundaries are facts: somebody opened the screen, and these
    messages were posted into it. A ``BACKFILL`` conversation's boundaries are
    an *inference* drawn after the fact from a gap in time — a good guess, but
    a guess, and one an operator staring at a surprising split deserves to be
    able to tell apart from the real thing.
    """

    LIVE = "live"
    BACKFILL = "backfill"


class ConversationKind(StrEnum):
    """What kind of thing answers in this conversation.

    Not the same shape as :attr:`Conversation.persona`, and deliberately so.
    ``persona`` defaults to ``None`` because "nobody chose" is a real,
    meaningful state that resolves against whatever the default happens to be
    *at the moment it answers* — see that field's own docstring. ``kind`` has
    no such state to represent: every conversation that exists, including
    every one written before this field did, is a text conversation until
    something says otherwise. There was never a moment where nothing had been
    decided, so there is no ``None`` to default to, and the default here is
    :attr:`TEXT` rather than an absence.

    The set is expected to grow — ``music`` is a named future member, not a
    hypothetical one — so this is a ``StrEnum`` in the style of
    :class:`ConversationOrigin` rather than a boolean or a two-member literal.
    Code that selects a responder from this value belongs in one place that
    maps a kind to a responder; a scattered ``if kind == "image"`` is the
    shape this exists to avoid.
    """

    TEXT = "text"
    IMAGE = "image"


class Conversation(BaseModel):
    """One named, resumable thread belonging to one person."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1)
    """Stable identity, a UUID4 string, minted once and never reused — so a
    URL can name this conversation and still mean it after a restart, a purge,
    or a retitle. Deliberately *not* the sqlite rowid: a rowid is an
    implementation detail of one file that a restored backup can reassign."""

    owner: Owner
    """Whose it is. Taken from the transcript rows' own attribution (spec
    section 8) rather than being a second notion of ownership: one person's
    conversations are never visible to another, and the check is the same
    ``owner_kind``/``owner_id`` pair every other query in this store uses."""

    surface: Surface
    title: str
    started_at: datetime
    last_activity_at: datetime
    """When this thread was last spoken in. The list is ordered by this and
    not by ``started_at`` — a conversation you resumed this morning belongs at
    the top even if you began it last week."""

    origin: ConversationOrigin = ConversationOrigin.LIVE

    kind: ConversationKind = ConversationKind.TEXT
    """What kind of thing answers in this thread — see :class:`ConversationKind`.

    Set once, at creation, and never changed: a thread does not become an
    image thread halfway down, because the transcript would then hold two
    different kinds of exchange with nothing recording where the switch
    happened. There is no route or setter for it here for that reason.

    ``TEXT`` for every conversation that predates this field, and that is a
    fact rather than an inference — unlike :attr:`persona`, whose ``None``
    genuinely means nobody chose.
    """

    persona: str | None = None
    """Who this thread is being held with, or ``None`` for "whoever the core's
    default is".

    The choice belongs here rather than to the core's settings because it is a
    property of a *conversation*: a thread started with GLaDOS is still GLaDOS
    tomorrow, while ``[core] default_persona`` keeps meaning what it has always
    meant for the OpenAI-compatible API, for an event waking the agent, and for
    every other surface. Picking one here writes nothing to ``core.toml``.

    ``None`` is deliberately not filled in. A conversation with no persona
    recorded resolves the default *at the moment it answers*, so a thread held
    before this field existed — or before anybody chose — follows the default
    the way it always did. Stamping today's default onto it would be inventing
    a history it does not have, and would silently freeze it there the next
    time the default changed.
    """

    hidden_at: datetime | None = None
    """When the owner took this conversation off their own list, or ``None``
    for a conversation they can still see.

    A timestamp rather than a boolean, and there is no ``deleted`` field beside
    it, because hiding destroys nothing: the conversation is kept for
    administrator review and ages out on exactly the retention schedule a
    visible one does. The first question anybody reviewing it asks is *when*,
    and a boolean cannot answer that.
    """

    hidden_by: str | None = None
    """The ``owner_id`` that hid it, or ``None`` while it is visible.

    Recorded separately from :attr:`owner` because they will not always be the
    same: an administrator path that hides somebody else's conversation is the
    obvious next thing to want, and a field that only ever held a copy of the
    owner would be no use the day one exists.
    """

    group_name: str | None = None
    """The group this conversation is filed under, or ``None`` for ungrouped.

    Free text the owner typed, not a foreign key into a groups table. A group
    is a label, and a label with a lifecycle of its own — created, renamed,
    emptied, deleted — is a second thing to keep in step with the conversations
    that point at it for no benefit anybody asked for. The picker's suggestions
    are simply the distinct labels already in use.
    """

    also_present: tuple[str, ...] = ()
    """The other personas in this room, besides the one :attr:`persona` names.

    The many-voices contract's roster (§2) is :attr:`persona` **and** these,
    in that order — see :func:`roster_of`. It is stored as "the others" rather
    than as the whole list for one reason, and it is the reason §7 exists: a
    conversation with nobody added has an empty tuple here, reads its persona
    exactly as it always did, and cannot be told apart from a conversation
    built before any of this existed. There is no list to keep in step with the
    picker, so there is no way for the two to disagree.

    A persona is removed from a room by leaving this tuple. It stays in the
    transcript — it said what it said — and stops being asked to speak.
    """

    thinking: bool | None = None
    """This conversation's own override of its persona's thinking switch
    (workspace contract §13, D), or ``None`` for "follow the persona".

    The same shape :attr:`persona` already has, and for the same reason:
    the choice belongs to the *thread*, not to a setting that would move it
    for every other conversation held with the same persona. ``None`` is
    every conversation before this field existed and every one that has
    never touched the chat header's Thinking checkbox — it is not stamped
    with today's persona setting, because the persona's own switch may
    change later and a stamped value would silently stop following it.

    ``True``/``False`` pins thinking on or off for this thread regardless of
    the persona's own file, until the override is cleared back to ``None``.
    Read by :class:`~personacore.agent.loop.TurnRequest` as :attr:`thinking`,
    written through
    :meth:`~personacore.conversations.service.ConversationService.set_thinking`.
    """

    message_count: int = 0
    """How many transcript rows still carry this conversation's id.

    Recomputed on read rather than kept as a running total, because the
    retention purge (ADR-0004) deletes rows out from under it and a stored
    counter would quietly start lying the first time somebody's history aged
    out.
    """


def derive_title(opening_message: str | None) -> str:
    """A title for a conversation, from the message it opened with.

    The obvious answer, and the one every chat product uses, because the first
    thing you said is the thing you remember about a conversation. The awkward
    cases, all of which happen:

    * **No opening message yet** — the screen has been opened but nothing has
      been said. Returns :data:`UNTITLED`; the title is derived for real the
      moment a user message is attached.
    * **A message that is only whitespace, or only control characters.** Same
      as above: there is nothing to name it after, so it stays
      :data:`UNTITLED` rather than becoming a blank row.
    * **One word.** The title is that word. It is not padded out, not
      suffixed with a date, and not rejected for being short — "Lights" is a
      perfectly good name for the conversation about the lights, and inventing
      extra words would make the list harder to scan, not easier.
    * **Four hundred characters.** Trimmed to :data:`MAX_TITLE_LENGTH` at the
      last word boundary that fits, with a single ellipsis, so the title
      breaks between words rather than mid-word.
    * **Four hundred characters with no spaces at all** — a URL, a base64
      blob, a language with no word breaks. There is no word boundary to trim
      at, so it is cut hard at the limit and still gets the ellipsis. A hard
      cut is deliberate: the alternative, falling back to
      :data:`UNTITLED`, would give a list of identically-named rows precisely
      when the messages are most distinguishable.
    """
    if not opening_message:
        return UNTITLED
    text = _CONTROL.sub(" ", opening_message[:_TITLE_SCAN_LIMIT])
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return UNTITLED
    if len(text) <= MAX_TITLE_LENGTH:
        return text

    head = text[:MAX_TITLE_LENGTH]
    cut = head.rfind(" ")
    # `cut > 0` and not `>= 0`: a leading space cannot happen after the strip
    # above, but a single very long first word gives rfind() == -1, which
    # slicing would silently turn into "drop the last character".
    if cut > 0:
        head = head[:cut]
    return head.rstrip(" ,.;:-—") + "…"


MAX_ROSTER = 6
"""How many personas may be in one room.

Not a storage limit — it is what the open-floor ask (§3.2) costs. Nobody names
anybody, and every persona present gets a model call to be asked whether the
message was for it; six is already six extra calls on a turn where nothing was
said to anyone in particular. A room bigger than this is not a conversation,
it is a broadcast, and the person paying for it in latency is the one who typed
the message.
"""


def roster_of(conversation: object | None, *, default: str) -> list[str]:
    """Who may speak next in this conversation, in order (§2).

    The persona the picker names first, then everybody else who was added.
    ``default`` is the core's configured persona, used when the thread never
    chose one — which is the same resolution a single-persona conversation has
    always had, so a conversation nobody has touched produces a list of exactly
    one and behaves as it did before rooms existed.

    Duplicates are dropped rather than refused: adding the persona that is
    already answering is a click, not an error, and a roster naming the same
    character twice would ask it the same question twice.

    Deliberately tolerant of ``None`` and of an object that carries neither
    field. Every read of a conversation on the chat screen already copes with a
    store that knows nothing about them, and losing the room controls is not a
    reason to lose the conversation.
    """
    first = str(getattr(conversation, "persona", None) or "") or default
    roster = [first] if first else []
    for name in getattr(conversation, "also_present", ()) or ():
        cleaned = str(name).strip()
        if cleaned and cleaned not in roster:
            roster.append(cleaned)
        if len(roster) >= MAX_ROSTER:
            break
    return roster


def new_conversation_id() -> str:
    """A fresh conversation identity. One place, so nothing invents its own."""
    return str(uuid4())


__all__ = [
    "MAX_TITLE_LENGTH",
    "SESSION_GAP",
    "UNTITLED",
    "Author",
    "AuthorKind",
    "Conversation",
    "ConversationKind",
    "ConversationOrigin",
    "MAX_ROSTER",
    "derive_title",
    "new_conversation_id",
    "roster_of",
]
