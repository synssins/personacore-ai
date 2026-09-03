"""Many voices in one room — the machinery behind the contract of that name.

A conversation can hold more than one persona, and the owner's framing is the
one this is built to: they are *participants in a chat room*, like any other user
in it — not tools being called, and not one assistant impersonating several
characters. Two personas on two different small models, arguing from genuinely
different training, is the point of the feature rather than a side effect of
it.

A file of its own because ``chat.py`` is already the longest screen in this
interface, and because everything here is a *decision* rather than a rendering:
who a message is for, who speaks next, when the room goes quiet, and what one
persona is allowed to see of what another said. The screen drives it and does
the talking; this never touches a template, a request or a store.

**§7 is the rule this file exists to keep.** One persona in a room must behave
exactly as it does today — no extra model call, no addressing check that can
decline to answer, no added latency. That is the case the owner uses every
day, and it is the easiest thing here to make quietly worse. So :class:`Exchange` has a
*solo* path that is not the multi-persona path with a length-one list going
through it: it never calls :func:`~personacore.conversations.addressing.addressed`,
never opens the floor, and ends after one turn without consulting anything.

**A persona's reply is untrusted content to every other persona** (§9). It
reaches them through :mod:`personacore.agent.untrusted` — the same fence a tool
result gets — because a persona's prompt is a file an operator wrote and its
words come from a model that may not even be this one (ADR-0036). With a child
in the house, a character writing "ignore your instructions" is a live attack
and not a theoretical one.

Counts, names and identifiers only. Never a message, never a reply.
"""

from __future__ import annotations

import re
import secrets
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import Request

from personacore.agent.untrusted import (
    UntrustedKind,
    new_fence_token,
    wrap_untrusted,
)
from personacore.audit.models import AuthorKind, MessageRole, TranscriptRecord
from personacore.conversations.addressing import addressed, repeats

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Who is in the room
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoomMember:
    """One persona present in a conversation.

    Two names because they are two different things and both are needed at
    once: ``name`` is the folder a turn is run with, ``display`` is what a
    person types when they want to talk to it. They differ routinely —
    ``aria`` answers to ``Aria`` — and matching addresses on the folder
    name would mean nobody could address anybody by the name on the screen.
    """

    name: str
    display: str

    @property
    def pair(self) -> tuple[str, str]:
        return (self.name, self.display)


def members(labels: Mapping[str, str], roster: Sequence[str]) -> list[RoomMember]:
    """The roster as members, in order. **Nobody is dropped.**

    ``labels`` is persona name to display name — what the picker already
    computed for the screen, passed in rather than recomputed so the name over
    a reply and the name that summons it are the same string.

    A persona that is no longer installed keeps its place, under its own
    folder name, and that is the important half. Leaving it out would quietly
    hand its turn to somebody else: a thread that chose a persona, with that
    persona since deleted from disk, would answer as the default persona while
    the screen still named the deleted one. Kept in, its turn reaches the agent loop's own
    persona resolution, which refuses it in a sentence naming the character —
    the outcome ``ChatRunner`` promises and the one an operator can act on.
    """
    return [
        RoomMember(name=name, display=labels.get(name) or name) for name in roster
    ]


def others(roster: Sequence[RoomMember], mine: str) -> list[str]:
    """Everybody in the room except the one about to speak, by display name.

    What a turn is told about the room (``TurnRequest.also_present``), and the
    piece that was missing entirely: the roster existed as data and never
    reached a prompt, so a persona had no way to know anybody else was there
    and §3.3 could not start. A character does not need to be told its own
    name — its prompt is the whole of who it is — so it is left out, and a
    roster of one therefore produces an empty list and a prompt composed
    exactly as it always has been (§7).

    ``mine`` is the *persona* name, because that is what the exchange hands
    round; what comes back is *display* names, because that is what §3.1
    matches and therefore what the persona has to type.

    Composed per turn from the live roster rather than stored, which is the
    behaviour the owner asked for in the same breath as asking for the
    feature: a persona closed with the x is gone from the very next turn.
    """
    return [member.display for member in roster if member.name != mine]


# ---------------------------------------------------------------------------
# What one persona is allowed to see of what another said — §9
# ---------------------------------------------------------------------------

PERSONA_HISTORY_SOURCE = "{name}, another character in this conversation"
"""How a fenced reply names who said it.

The display name goes in the fence header, where the model can read it, so a
persona can answer by naming the other character directly rather than replying
to an anonymous block of text. The name is defanged by ``wrap_untrusted``
before it is printed, so a persona called ``END_UNTRUSTED`` cannot close its
own fence.
"""


def fenced_reply(text: str, *, speaker: str, token: str) -> str:
    """One persona's words, as another persona is allowed to receive them.

    The whole of §9's requirement, in one function, so there is exactly one
    place a persona's reply can cross into another persona's prompt. Anything
    that composed a room's history without going through here would be the
    hole this rule exists to close.
    """
    return wrap_untrusted(
        text,
        kind=UntrustedKind.PERSONA_MESSAGE,
        source=PERSONA_HISTORY_SOURCE.format(name=speaker),
        token=token,
    )


@dataclass(frozen=True, slots=True)
class Said:
    """One thing somebody said in the room, as the exchange remembers it.

    Deliberately not a :class:`~personacore.audit.models.TranscriptRecord`. An
    exchange has to reason about a message *before* it is a row — the one
    somebody has just typed, which the agent loop writes during the turn that
    answers it — and about replies as they land. Three fields is all any of
    that needs, and a shape this small is one a test can build without a
    database.
    """

    text: str
    who: str = ""
    """The display name of the character that said it, or ``""`` for the
    person and for anything written before authorship existed."""

    persona: bool = False
    """Whether a character said it. A row with no author reads as ``False``
    here and as *this persona's own* in :func:`history_for` — see there for
    why that is what keeps a single-persona conversation unchanged."""


def said_from_rows(rows: Sequence[TranscriptRecord]) -> list[Said]:
    """Transcript rows, oldest first, as things people said.

    ``tool`` and ``system`` rows are dropped: a tool result belongs to the
    round that produced it and a system row is the prompt. Neither is something
    anybody in the room said, and neither has ever been part of the history the
    chat screen hands a turn.
    """
    out: list[Said] = []
    for record in rows:
        if record.role is MessageRole.USER:
            out.append(Said(text=record.content))
        elif record.role is MessageRole.ASSISTANT:
            author = record.author
            named = (
                author.name
                if author is not None and author.kind is AuthorKind.PERSONA
                else ""
            )
            out.append(Said(text=record.content, who=named, persona=bool(named)))
    return out


def _by_someone_else(item: Said, mine: str) -> bool:
    """Whether this was said by a character that is not the one being composed
    for.

    A row with **no author** — every message written before authorship existed
    — is read as this persona's own, deliberately. Those rows were this
    assistant's replies, and fencing them would change what an ordinary
    single-persona conversation looks like to the model in every thread that
    predates rooms, for no gain (§7).
    """
    return item.persona and bool(item.who) and item.who != mine


def prompt_for(item: Said, *, mine: str, token: str) -> str:
    """What one persona is asked to answer, as it is allowed to receive it.

    A person's own words go through as they were typed. **Another character's
    words are fenced** (§9): the whole reason this function exists rather than
    the caller passing ``item.text`` is that there is then exactly one place a
    persona's reply can become another persona's prompt.
    """
    if _by_someone_else(item, mine):
        return fenced_reply(item.text, speaker=item.who, token=token)
    return item.text


def history_for(
    said: Sequence[Said],
    *,
    mine: str,
    message_class: Any,
    token: str | None = None,
) -> list[Any]:
    """The conversation so far, as one persona in the room may read it.

    ``mine`` is that persona's display name — the string the agent loop writes
    onto its own assistant rows — and it decides the one thing that matters:

    * this persona's own replies come back as ``assistant``, which is what they
      are: things it said;
    * **anybody else's** come back as ``user``, fenced (§9), because a reply by
      another character is somebody else's words and not this one's;
    * the person's own messages come back as ``user``, unfenced, because that
      is what a user message has always been.

    ``said`` is oldest first and already capped by the caller;
    ``message_class`` is the two-field shape the runner takes, passed in rather
    than imported so this module stays independent of the screen it serves.
    """
    fence = token or new_fence_token()
    out: list[Any] = []
    for item in said:
        if not item.persona:
            out.append(message_class(role="user", content=item.text))
        elif _by_someone_else(item, mine):
            out.append(
                message_class(
                    role="user",
                    content=fenced_reply(item.text, speaker=item.who, token=fence),
                )
            )
        else:
            out.append(message_class(role="assistant", content=item.text))
    return out


# ---------------------------------------------------------------------------
# The exchange — §3 and §4
# ---------------------------------------------------------------------------

DEFAULT_MAX_PERSONA_TURNS = 6
"""§4.3's hard cap: how many persona turns one exchange may run.

Six, because two personas answering each other is the case this is for and six
turns is three rounds each — long enough for a disagreement to develop and be
resolved, short enough that a conversation which is going nowhere costs
seconds rather than minutes. It is a *backstop* rather than the mechanism: the
addressing rules end almost every exchange long before it, and an exchange that
reaches this has usually found the loop §4.2 is meant to catch and did not.

An administrator can change it — ``[chat] max_persona_turns`` in ``core.toml``.

It applies only to a room with more than one persona in it. A single persona
answers once and stops, which is what it has always done and what §7 requires;
there is no count for the cap to reach.
"""

MAX_PERSONA_TURNS_CEILING = 40
"""What an administrator may set the cap to at most.

Not distrust of the operator: it is that every persona turn is a model call
somebody is waiting on with a browser open, and a mistyped ``600`` would be a
conversation that could not be got out of except by the stop button. The stop
button exists; a ceiling means it is not the only thing standing between a typo
and a very long afternoon.
"""

CHAT_SECTION = "chat"
MAX_PERSONA_TURNS_KEY = "max_persona_turns"


def max_persona_turns(settings: Any) -> int:
    """§4.3's cap, as this core is configured, read from the settings document.

    Read from the raw document rather than a typed field for the reason
    ``chat.py``'s dictation switch is: this is a household setting nobody has
    drawn a screen for yet, and a ``core.toml`` that cannot be read must fall
    back to the default rather than becoming the reason a conversation will not
    run. Anything that is not a positive whole number is the default, and
    anything above the ceiling is the ceiling.
    """
    section = None
    try:
        section = settings.get(CHAT_SECTION) if settings is not None else None
    except Exception:  # noqa: BLE001 - a broken settings file is not a dead room
        return DEFAULT_MAX_PERSONA_TURNS
    if not isinstance(section, dict):
        return DEFAULT_MAX_PERSONA_TURNS
    raw = section.get(MAX_PERSONA_TURNS_KEY)
    try:
        wanted = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_PERSONA_TURNS
    if wanted < 1:
        return DEFAULT_MAX_PERSONA_TURNS
    return min(wanted, MAX_PERSONA_TURNS_CEILING)


@dataclass(slots=True)
class Exchange:
    """Who speaks next, and when the room goes quiet.

    Everything that follows one human message until nothing is left to say
    (§4's "exchange"). It holds the decisions and none of the doing: the screen
    runs the turns, asks the floor question and renders the replies, and comes
    back here to find out what happens next. That split is what makes the four
    stop conditions testable without a model.

    The four ways it ends, and the **first** of them wins:

    1. nobody is addressed — the last reply named nobody and, on the open-floor
       ask, nobody claimed it;
    2. a persona repeats itself, and *that persona* stops;
    3. the hard cap (:data:`DEFAULT_MAX_PERSONA_TURNS`);
    4. somebody pressed stop (§5).
    """

    roster: list[RoomMember]
    cap: int = DEFAULT_MAX_PERSONA_TURNS

    _queue: list[str] = field(default_factory=list, init=False)
    _said: dict[str, list[str]] = field(default_factory=dict, init=False)
    _silenced: set[str] = field(default_factory=set, init=False)
    _last: str | None = field(default=None, init=False)
    _turns: int = field(default=0, init=False)
    _stopped: bool = field(default=False, init=False)
    _opened: bool = field(default=False, init=False)

    @property
    def solo(self) -> bool:
        """A room with one persona in it — the case §7 protects.

        Everything downstream branches on this rather than on ``len(roster)``,
        so the reason is written once where it can be read: a solo room takes
        no addressing decision at all, because an addressing check that can
        decline to answer would mean the assistant the owner talks to every
        day could now say nothing.
        """
        return len(self.roster) == 1

    @property
    def turns(self) -> int:
        return self._turns

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        """§5. Ends the exchange at the current turn's boundary — the turn that
        is running finishes and is recorded, and nobody else is asked."""
        self._stopped = True

    def open(self, message: str) -> list[str]:
        """Start the exchange with what a person just said. Returns who speaks.

        For a **solo** room this is the one persona, unconditionally and with
        no check of any kind: the message was for it because there is nobody
        else it could have been for.

        Otherwise §3.1 decides: whoever the message names, by leading name or
        by ``@``, in roster order. An empty list means the floor is open and
        the caller should ask (§3.2) — which is the expensive path, and the
        whole reason the cheap one is tried first.
        """
        self._opened = True
        if self.solo:
            self._queue = [self.roster[0].name]
            return list(self._queue)
        self._queue = list(addressed(message, [m.pair for m in self.roster]))
        return list(self._queue)

    def next(self) -> str | None:
        """Who speaks now, or ``None`` because the room has gone quiet.

        The three exhaustion conditions are checked here, in one place, so a
        caller cannot run a turn past one of them by forgetting to ask.
        """
        if self._stopped or not self._opened:
            return None
        if not self.solo and self._turns >= self.cap:
            return None
        while self._queue:
            who = self._queue.pop(0)
            if who in self._silenced:
                continue
            return who
        return None

    def spoke(self, name: str, reply: str) -> list[str]:
        """Record what one persona just said, and who it addressed.

        Returns the personas that reply names, which are queued to answer.
        §3.3: a persona's own words are read for addresses by exactly the same
        rules as a person's, which is what makes them talk to each other — and
        it is why §3.1's anchoring matters twice over, because a character that
        mentions another's name in passing must not summon it.

        §4.2's repetition rule is applied here and applies to **this persona
        only**: a reply near-identical to one it has already given in this
        exchange stops that character, and the others carry on. It
        under-detects on purpose — stopping a real debate is worse than one
        extra round.
        """
        self._turns += 1
        self._last = name
        earlier = self._said.setdefault(name, [])
        if repeats(reply, earlier):
            self._silenced.add(name)
            log.info("persona_stopped_repeating", persona=name, turns=self._turns)
        earlier.append(reply)
        if self.solo or self._stopped:
            return []
        named = [
            who
            for who in addressed(reply, [m.pair for m in self.roster])
            if who != name and who not in self._silenced and who not in self._queue
        ]
        if named:
            # **The one observable proof that the roster is being used.** A
            # test against a fake host proves the names reached the prompt and
            # can prove nothing else — a fake host does not reason, it returns
            # what it was told to return. Whether a real model *acts* on being
            # told who is present is only answerable from a real model, so it
            # is answered here, in the container log, against a live deployment:
            # a room of two that never produces this line is a roster being
            # ignored, and we know rather than suspect. Names and counts, never
            # a word of what was said.
            log.info(
                "persona_addressed_persona",
                persona=name,
                addressed=named,
                turns=self._turns,
            )
        self._queue.extend(named)
        return named

    def open_floor(self) -> list[str]:
        """Who to put §3.2's question to, or nothing because it does not apply.

        Empty for a solo room — always, and that is the guarantee §7 asks for:
        the ask is one model call per persona per turn and it must never run
        for the conversation the owner has open every day.

        Empty, too, whenever somebody is already queued to speak: §3.2 runs
        **only** on the open floor and never when §3.1 matched. And empty once
        the cap or the stop button has ended things, because asking a room
        whether it wants to speak after deciding nobody may is a model call
        spent on a question with no consequence.
        """
        if self.solo or self._stopped or self._queue:
            return []
        if self._turns >= self.cap:
            return []
        return [
            m.name
            for m in self.roster
            if m.name != self._last and m.name not in self._silenced
        ]

    def claim(self, names: Sequence[str]) -> None:
        """Queue whoever said yes to the floor question — and, if nobody did
        and nobody has spoken yet, the primary persona anyway (§3.2).

        **Reversed by the owner on 2026-08-31, after using it.** This used to
        say that nobody claiming the floor meant nobody answered, and that a
        fallback would be the single-persona behaviour wearing a costume. The
        owner added a second persona to a room with an existing one, asked a
        question naming neither, and got silence from both. A room where a
        question can land in silence is not a room, and there is no way for
        the person typing to tell "nobody wanted it" from "it broke".

        So the floor question decides *how many* answer and *who joins in*,
        never whether anything happens at all: **a message always gets a
        reply**, from the persona the picker names.

        Only when nothing has been said yet, which is the whole of the rule —
        the message that started this exchange has not been answered. Once a
        character has spoken, §4.1 still ends the room on an open floor nobody
        claims, and it has to: a fallback that ran after every reply would
        queue the primary again and again until the cap, which is a room that
        cannot stop talking.
        """
        for name in names:
            if name not in self._silenced and name not in self._queue:
                self._queue.append(name)
        if self._queue or self._turns or self._stopped:
            return
        primary = self.roster[0].name
        if primary not in self._silenced:
            self._queue.append(primary)


# ---------------------------------------------------------------------------
# The stop button — §5
# ---------------------------------------------------------------------------

STOP_ATTRIBUTE = "chat_exchange_stops"
"""Where the running exchanges live on ``app.state``.

On the application rather than in a module global for the reason every other
piece of per-core state here is: the test suite builds several applications in
one process, and a global would let one core's exchanges be stopped through
another's.
"""

STOPS_KEPT = 16
"""How many exchanges are remembered, oldest out first.

An exchange is interesting for the seconds it is running. This is far more than
one person can have in flight and small enough that a dictionary of them is
never a thing to think about.
"""

STOP_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
"""What :func:`secrets.token_urlsafe` produces, and nothing else. The token is
a dictionary key and never touches a path, so this is not a traversal guard —
it is a cheap door that keeps anything shaped unlike a token out of the lookup
and out of the log line it would otherwise fill."""


def begin(request: Request, owner: str, exchange: Exchange) -> str:
    """Register a running exchange so it can be stopped, and hand back its token.

    The token goes to the browser in the stream's first frame. It is random
    rather than the conversation's identity because a conversation is a
    long-lived thing somebody can bookmark and an exchange lasts seconds:
    naming one by the other would let a stale tab stop a conversation that had
    moved on.
    """
    state = request.app.state
    ring: OrderedDict[str, tuple[str, Exchange]] | None = getattr(state, STOP_ATTRIBUTE, None)
    if ring is None:
        ring = OrderedDict()
        setattr(state, STOP_ATTRIBUTE, ring)
    token = secrets.token_urlsafe(16)
    ring[token] = (owner, exchange)
    ring.move_to_end(token)
    while len(ring) > STOPS_KEPT:
        # Evicted, not stopped: an exchange pushed out of the ring is one
        # somebody started long ago, and ending it from here would cut a
        # conversation short because somebody else opened a tab.
        ring.popitem(last=False)
    return token


def release(request: Request, token: str) -> None:
    """Forget a finished exchange. Never raises — this runs on the way out of a
    stream that may already have failed, and tidying up must not become the
    failure."""
    ring = getattr(request.app.state, STOP_ATTRIBUTE, None)
    if isinstance(ring, OrderedDict):
        ring.pop(token, None)


def stop(request: Request, token: str, owner: str) -> bool:
    """Stop this operator's running exchange. ``True`` when one was stopped.

    The owner check is the same one a reply's audio gets, and it matters as
    much: a token is a way to reach into somebody's conversation and silence
    it. An unknown token, somebody else's token and an exchange that has
    already finished are one answer, because which it was belongs in the log
    and not in the response.
    """
    if not STOP_PATTERN.match(token):
        return False
    ring = getattr(request.app.state, STOP_ATTRIBUTE, None) or {}
    kept = ring.get(token)
    if kept is None or kept[0] != owner:
        return False
    kept[1].stop()
    log.info("chat_exchange_stopped", turns=kept[1].turns)
    return True


# ---------------------------------------------------------------------------
# The per-conversation voice switch — §6.2
# ---------------------------------------------------------------------------

_MUTED_PREFIX = "speech.conversation_muted."

#: A conversation id, which is a UUID4 string and nothing else. Checked before
#: it becomes part of a preference key, so a hand-edited field cannot write
#: unbounded rows into the preference table.
_CONVERSATION_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def muted_preference(conversation_id: str) -> str | None:
    """The preference name for one conversation's voice switch, or ``None``.

    The owner asked for this in the same breath as the audio queue, as a way
    to silence a room of personas that has become distracting to have read
    aloud, and turning it off has to be possible without turning off the
    assistant's voice everywhere.

    It is a preference rather than a column on the conversation because that is
    what it is: **this person's** choice about **this** thread. ADR-0030 keys
    those on the door and the identity together, which is the same key the
    autoplay setting it defers to already uses — so the two are resolved from
    one table with one rule and cannot end up in different stores disagreeing.

    ``None`` for anything that is not a conversation id, which leaves the
    caller with the person's own setting and no way to write a key from a form.
    """
    return (
        f"{_MUTED_PREFIX}{conversation_id}"
        if _CONVERSATION_ID.match(conversation_id or "")
        else None
    )


__all__ = [
    "CHAT_SECTION",
    "DEFAULT_MAX_PERSONA_TURNS",
    "MAX_PERSONA_TURNS_CEILING",
    "MAX_PERSONA_TURNS_KEY",
    "PERSONA_HISTORY_SOURCE",
    "STOPS_KEPT",
    "STOP_ATTRIBUTE",
    "STOP_PATTERN",
    "Exchange",
    "RoomMember",
    "Said",
    "begin",
    "fenced_reply",
    "history_for",
    "prompt_for",
    "said_from_rows",
    "max_persona_turns",
    "members",
    "muted_preference",
    "release",
    "stop",
]
