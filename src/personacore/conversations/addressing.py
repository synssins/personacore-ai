"""Who a message in a room is for — the many-voices contract, §3 and §4.2.

Pure text, no I/O and no model. That is the whole point of the first mechanism
(§3.1): a message that names somebody is matched here, for free, and the
expensive open-floor ask (§3.2) never runs. A room where every message costs a
model call per persona would be a room nobody could afford to leave open.

Three things live here because all three are decisions about *text* rather than
about conversations, storage or screens:

* :func:`addressed` — who a message names, by §3.1's two forms and nothing
  else.
* :func:`repeats` — whether a persona has just said what it already said in
  this exchange (§4.2), which is one of the four things that ends it.
* :data:`FLOOR_QUESTION`, :data:`FLOOR_NO_THINKING`, :class:`FloorAnswer` and
  :func:`claims_floor` — the words put to a persona on the open floor, the
  extra request fields that go with them, the shape of what comes back, and
  the reading of it. All four are here rather than beside the loop that asks so
  that how the question is put and how the reply is read cannot drift apart.
  They drifted once already, in the only way that matters: the ceiling was set
  for a model that answers immediately and the model actually in use thinks
  first.

Never logs and never sees anything but the text it was handed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from types import MappingProxyType
from typing import Any

MAX_ADDRESS_SCAN_CHARS = 4000
"""How much of a message is scanned for a name.

§3.1's leading form is anchored to the start, so it is decided in the first few
characters; the ``@`` form can be anywhere, and a pasted log file is a
legitimate message. Past this point the scan is pure cost — a name buried
thirty kilobytes into a paste is not somebody being addressed.
"""

#: Punctuation and quotation a leading name may be wrapped in. §3.1 says
#: matching "ignores surrounding punctuation", and the case that actually
#: happens is a message that opens with a quote mark or an em dash.
_LEADING_NOISE = r"[\s\"'“”‘’(\[\-–—*_>]*"

#: What may follow a leading name for it to be an address: the separators the
#: contract names, or the end of the message. A name followed by a letter is
#: part of a longer word and is not this persona.
_AFTER_LEADING = r"(?=$|[\s,:;!?.…\-–—])"

#: What may follow an ``@name`` — anything that is not a name character, so
#: ``@Aria`` does not match a persona called ``Yod``.
_AFTER_AT = r"(?![\w'-])"


def _patterns(display: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The two ways one display name can address its persona.

    Built per call rather than cached: a roster is a handful of names, a turn
    is a model call, and a cache keyed on operator-supplied text is a place for
    a leak to live.
    """
    name = re.escape(display.strip())
    leading = re.compile(rf"^{_LEADING_NOISE}{name}{_AFTER_LEADING}", re.IGNORECASE)
    at = re.compile(rf"@{name}{_AFTER_AT}", re.IGNORECASE)
    return leading, at


def addressed(text: str, roster: Sequence[tuple[str, str]]) -> list[str]:
    """Which personas ``text`` addresses, in roster order (§3.1).

    ``roster`` is ``(persona name, display name)`` pairs — the identity the
    turn is run with, and the name a person actually types. They differ
    routinely (``glados`` is called ``GLaDOS``), and matching on the folder
    name would mean nobody could address anybody by the name on the screen.

    A message addresses a persona when it **starts with** that display name,
    followed by a comma, colon, whitespace or the end of the message, or when
    it contains ``@`` immediately followed by that name. Matching is
    case-insensitive and ignores punctuation around the name.

    A name appearing anywhere else is **not** an address: *I was reading about
    Aria today* addresses nobody. That restriction is the whole reason this
    mechanism can be trusted to be cheap — without it, a room would summon a
    character every time somebody mentioned one, and §3.3 makes that twice as
    important, because a persona that mentions another's name in passing must
    not drag it into the conversation.

    More than one persona may be addressed by one message, and all of them are
    returned. Order is roster order rather than the order the names appear,
    because the roster is the order the room was assembled in and a person
    writing two names in a sentence is not ranking them.
    """
    scanned = text[:MAX_ADDRESS_SCAN_CHARS]
    if not scanned.strip():
        return []
    found: list[str] = []
    for name, display in roster:
        label = (display or name).strip()
        if not label:
            continue
        leading, at = _patterns(label)
        if leading.search(scanned) or at.search(scanned):
            if name not in found:
                found.append(name)
    return found


# ---------------------------------------------------------------------------
# Saying the same thing twice — §4.2
# ---------------------------------------------------------------------------

REPETITION_RATIO = 0.95
"""How alike two replies have to be to count as the same reply.

High on purpose. §4.2 is explicit that this must **under-detect**: it exists to
catch two bots restating themselves at each other, which is the failure that
actually happens, and stopping a real debate one round early is worse than
letting one extra round run. A threshold low enough to catch a slow semantic
loop would also catch a character with a consistent opinion.
"""

REPETITION_MIN_CHARS = 40
"""Shorter than this and repetition is not evidence of anything.

Two personas both answering *"Yes."* have not looped; they have agreed. Every
short reply is near-identical to every other short reply, so the check simply
does not apply to them — again, under-detecting by design.
"""

#: Everything that is not a letter, a digit or a space, dropped before
#: comparing. A model that says the same sentence twice with different
#: punctuation has said it twice.
_UNCOMPARED = re.compile(r"[^\w\s]+")

#: Whitespace of every kind, collapsed, so a reflowed paragraph is the same
#: paragraph.
_SPACES = re.compile(r"\s+")


def _normalised(text: str) -> str:
    return _SPACES.sub(" ", _UNCOMPARED.sub(" ", text.casefold())).strip()


def repeats(reply: str, earlier: Sequence[str]) -> bool:
    """Whether this persona has already given this reply in this exchange.

    Compared against **its own** earlier replies only. Two personas agreeing
    word for word is a conversation reaching a conclusion, not a loop, and
    stopping it would be this rule deciding a debate.

    "This exchange" is the caller's list and resets when a person speaks
    (§4), because the same character answering the same question the same way
    tomorrow is not a fault.
    """
    now = _normalised(reply)
    if len(now) < REPETITION_MIN_CHARS:
        return False
    for said in earlier:
        before = _normalised(said)
        if len(before) < REPETITION_MIN_CHARS:
            continue
        if now == before:
            return True
        if SequenceMatcher(None, now, before).ratio() >= REPETITION_RATIO:
            return True
    return False


# ---------------------------------------------------------------------------
# The open floor — §3.2
# ---------------------------------------------------------------------------

FLOOR_QUESTION = (
    "That was said to the room, not to you by name. Do you want to answer it? "
    "Reply with one word: YES if you want to speak now, NO if you would rather "
    "let it go. Do not answer the message itself."
)
"""What a persona is asked when nobody was named.

Kept to a yes-or-no because it is one extra model call per persona per turn and
the answer is thrown away — everything about it is shaped to be cheap. It is
put to the persona *in its own voice*, with its own system prompt, so a
character that would not have anything to say about the weather says no for the
same reason it would have said nothing.
"""

FLOOR_NO_THINKING: Mapping[str, Any] = MappingProxyType(
    {"chat_template_kwargs": MappingProxyType({"enable_thinking": False})}
)
"""Extra request fields sent with the floor question, and only with it.

**This exists because the floor question had never once returned a yes against
a reasoning backend.** A reasoning model writes its thinking into a separate
``reasoning_content`` field and only starts filling ``content`` once the
thinking is finished. Put to it at
:data:`FLOOR_MAX_TOKENS`, the whole budget went on thinking and ``content``
came back empty every single time — so :func:`claims_floor` read no, no persona
could ever claim the floor, and every untagged message fell to the primary.
Reversing §3.2 so the primary always answers was right on its own merits and it
also hid this for a day: the room looked like it was working.

Asking the model not to think is the only affordable answer. Raising the budget
instead is measured: 1,671 tokens of thinking for a three-word answer, per
persona, per untagged message, before the first reply streams.

``chat_template_kwargs`` is **llama.cpp's own documented request field** — its
server README names it and gives ``{"enable_thinking": false}`` as the example
— and ``enable_thinking`` is the value the Qwen template reads. vLLM spells it
the same way. So on the host this was written for it is not an unknown field at
all; it is the documented one.

What is *not* verified is how every other OpenAI-compatible backend treats a
field it has never heard of, and this core is deliberately generic about what
is behind it. So that is not assumed: if a host rejects the request,
:meth:`~personacore.agent.loop.AgentLoop.ask_persona` asks again once without
these fields. The worst case on such a host is one refused round trip, which
costs no generation, and never a silent loss of the feature.

Sending it is also not a guarantee it is obeyed — there are reports of Qwen
templates thinking anyway. That is why nothing downstream depends on it: an
empty answer still means no, the primary persona still answers, and a question
cut off mid-thought is counted and reported rather than passing for a shrug.

It is named here, beside the question and the ceiling, so how the question is
asked and how the reply is read cannot drift apart.
"""

FLOOR_MAX_TOKENS = 24
"""A ceiling on the answer, because the answer is one word.

Small enough that a model which ignores the instruction and starts a speech is
cut off after the part that decides the question, rather than being paid for in
full.

**Raised from 8 on 2026-08-31**, with :data:`FLOOR_NO_THINKING`. Eight was too
tight even for a model that agreed not to think: the Qwen template still opens
and closes an empty ``<think>`` block first, and on a host that is not parsing
reasoning out into its own field those tags *are* the first tokens of the
answer. Eight tokens bought the tags and nothing else. Twenty-four is still one
short answer and still cuts a speech off early.
"""

#: A model's thinking, when the host hands it back inline instead of splitting
#: it into ``reasoning_content``. Dropped before the answer is read, because
#: otherwise the first word of "<think>…</think> YES" is *think* — which is not
#: yes, so a persona that plainly said yes would be recorded as silent. An
#: unterminated block (the budget ran out mid-thought) matches to the end and
#: leaves nothing, which is correct: an unfinished thought is not a claim.
_THINKING = re.compile(
    r"<(?:think|thinking)\b[^>]*>.*?(?:</(?:think|thinking)\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)

#: The first word of an answer, which is the only part read. A model that
#: writes "Yes — that one's mine" has said yes; one that writes "I think the
#: question was really for GLaDOS" has not.
_FIRST_WORD = re.compile(r"[a-z]+")

_YES = frozenset({"yes", "yeah", "yep", "y", "aye", "sure", "affirmative"})


def claims_floor(answer: str) -> bool:
    """Whether a persona said it wanted the floor (§3.2).

    **Anything that cannot be read as yes is no.** Not a guess, not a fallback
    to the first persona: a character that could not say it wanted to speak
    does not get to. An empty answer, a refusal, a model that started
    monologuing instead of answering, a failed call — all of them are silence,
    and silence in a room is somebody not speaking.

    A reasoning model's thinking is dropped first (:data:`_THINKING`) when the
    host left it inline. What is deliberately **not** done is reading the
    thinking for a verdict: the only time there is thinking and no answer is
    when the budget ran out during it, so the thinking on offer is by
    definition unfinished, and a room does not hand the floor to somebody who
    was still making their mind up.
    """
    found = _FIRST_WORD.search(_THINKING.sub(" ", answer).strip().casefold())
    return found is not None and found.group(0) in _YES


@dataclass(frozen=True, slots=True)
class FloorAnswer:
    """What came back when one persona was asked whether it wanted the floor.

    More than the words, because the words on their own could not tell two very
    different things apart: a character that considered the question and passed,
    and a call that was cut off before the character got to the question at all.
    **That is the property that hid the reasoning-model defect for a day** — the
    floor question was being answered "no" by a budget running out, and from
    here it was indistinguishable from a shrug.

    Never carries anything but the persona's own answer, a flag and a flag. The
    thinking itself is not kept and is never logged.
    """

    text: str = ""
    """The persona's own words, unread. Empty is every kind of failure, and
    :func:`claims_floor` reads it as no — the direction §3.2 says to fail in."""

    truncated: bool = False
    """The host stopped for ``finish_reason: "length"`` — the answer ran out of
    budget rather than finishing."""

    thought: bool = False
    """The host emitted reasoning: this model thinks before it answers, and
    :data:`FLOOR_NO_THINKING` did not stop it. A flag, never the text."""

    @property
    def cut_off_mid_thought(self) -> bool:
        """The exact signature of the defect: thinking, no answer, out of room.

        Worth naming rather than re-deriving at each caller, because this is
        the one combination that must never again read as a considered no
        without somebody being told.
        """
        return self.truncated and self.thought and not self.text.strip()


__all__ = [
    "FLOOR_MAX_TOKENS",
    "FLOOR_NO_THINKING",
    "FLOOR_QUESTION",
    "MAX_ADDRESS_SCAN_CHARS",
    "REPETITION_MIN_CHARS",
    "REPETITION_RATIO",
    "FloorAnswer",
    "addressed",
    "claims_floor",
    "repeats",
]
