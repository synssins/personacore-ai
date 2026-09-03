"""Punctuation as timing — PC-342, and the reason it is in core.

A multi-sentence weather reply was heard synthesised as one unbroken breath —
the periods were present in the text and did nothing to the audio. What was
actually happening is written up in the requirement and in ADR-0029, and the
two facts that decide this module's shape are worth repeating here because
both of them make the obvious code wrong:

1. **Punctuation was never dropped.** espeak returns a sentence at a time with
   its stops intact, and the engine glued those chunks into one string and
   synthesised them as a single breath
   (:func:`personacore.voice.engines.espeak.clean_espeak_output`). That is the
   run-on, and it is not a bug in the phonemisation — the phonemes are right.

2. **Synthesising each sentence and butting the audio together is worse.**
   Both failure modes were heard in practice. Splitting also removes the
   sentence-final slowing the model produces on its own, so the pieces land
   flatter than the run-on version did. The pause has to be *put back
   deliberately*, as silence, between separately synthesised pieces.

So: core splits, core calls the engine once per piece, core joins the audio
with the gaps. **An engine is handed one piece of text and returns audio for
it. It never splits, never inserts silence, and never reads punctuation as a
timing instruction** — ADR-0029, "Pacing belongs to core, and this was learned
the hard way". Two reasons, both in the ADR: every engine has this problem, so
solving it inside ``vits-onnx`` means solving it again in Kokoro and
differently; and PC-253's streaming needs the same splitter, dividing text at
clause and sentence boundaries *before* synthesis.

**Zero is legal in all three gaps and means no gap.** That is exactly today's
behaviour — the whole text handed over in one call, with the model's own
sentence-final slowing and nothing added — and it is reachable on purpose,
because a pacing control that cannot be turned off is a preference dressed as
a feature. When every gap is zero the text is not split at all, so a voice set
to zero is byte-for-byte what it was before this module existed.

The numbers were chosen by ear on a real voice: **sentence 450 ms**,
clause **half** of it, paragraph **double**. Tuning the one number keeps the
shape, which is why the other two follow it by ratio unless they are set.

**The marks are per voice too, and for the same reason the numbers are.** Which
characters end a sentence and which end a clause started life as three constants
in this module, and the comma moved between them once already, by ear, on one
voice. A voice that has been made to sound right has to *stay* right when it is
handed to somebody else, so the marks live in the voice's ``voice.toml`` beside
the gaps, travel in an exported pack, and are edited on the voice page — exactly
the shape the three gaps already had. Empty is the default, which is today's
behaviour; the paragraph rule is not on that list, because a blank line is a
blank line and not a character.

Those strings are operator input going into a regular expression, so they only
ever reach one through :func:`_char_class`, one ``re.escape`` per character. A
voice whose marks are ``.*[`` paces oddly; nothing raises.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from personacore.voice.engine import Audio

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------

DEFAULT_SENTENCE_GAP_MS = 450
"""The one setting. Chosen by ear on a real voice, not derived from anything.

A voice that says nothing about pacing gets this, which is why the fault this
module fixes does not come back the moment somebody installs a stock Piper
voice carrying no ``voice.toml`` at all."""

CLAUSE_RATIO = 0.5
PARAGRAPH_RATIO = 2.0
"""Clause is half the sentence gap and a paragraph is double it.

Ratios rather than three independent numbers so that **tuning one number keeps
the shape**: a voice slowed down for a large room stays proportioned. Either
may be overridden outright for a voice whose pacing does not fit the ratio —
that is what the advanced boxes are for."""

GAP_LIMITS: tuple[int, int] = (0, 5000)
"""(low, high) milliseconds, for every one of the three.

Zero is in range and is the whole point of the low end. Five seconds is the
high end because a gap longer than that is not pacing, it is a fault an
operator would report as "the voice stopped" — and the value is typed into a
box beside two others that follow it by ratio, so a mistyped ``4500`` for
``450`` already doubles to nine seconds between paragraphs."""

PACING_FIELDS: tuple[str, ...] = ("sentence_gap_ms", "clause_gap_ms", "paragraph_gap_ms")
"""The three settings, by the name each carries in ``voice.toml`` and on the
form. Named once so the writer, the reader and the form cannot disagree."""

DEFAULT_SENTENCE_MARKS = ".!?…"
"""The marks a voice breaks a sentence at when it says nothing about it."""

DEFAULT_CLAUSE_MARKS = "—–;,"
"""Em dash, en dash, semicolon and comma — the clause marks a voice gets when
it says nothing about it.

The comma is in here by ear on one voice, which is the whole argument
for the setting existing: the right answer was not the same for every voice,
and it was found by listening. A voice that wants the older, run-together
reading at its commas writes ``clause_marks = "—–;"`` and takes that with it."""

MARKS_LIMIT = 16
"""How many distinct characters either set may name.

Not a safety limit — :func:`_char_class` is what makes these strings safe — but
a typing one: past a dozen or so marks, a box that was meant to hold ``.!?`` is
holding something that was pasted in by accident, and a splitter that breaks at
sixteen different characters is not pacing a voice."""

PACING_MARK_FIELDS: tuple[str, ...] = ("sentence_marks", "clause_marks")
"""The two character sets, by the name each carries in ``voice.toml`` and on
the form.

**One string per set, and it replaces the default rather than adding to it.**
The owner asked to add, remove and replace; a replacing list is all three at once —
"add the colon" is ``.!?…:`` and "remove the comma" is ``—–;`` — where an
add-list and a remove-list beside each other would be two boxes, two orders of
application, and a question about what a character in both of them means. There
is no fourth behaviour to gain by building the other two."""

PACING_MEANING: dict[str, str] = {
    "sentence_gap_ms": "the silence after a full stop",
    # Not "after a dash or a semicolon" any more: which marks those are is this
    # voice's to say, so a sentence naming them would be wrong for any voice
    # that had changed them -- which is the setting below this one.
    "clause_gap_ms": "the silence at a clause mark, such as a dash or a comma",
    "paragraph_gap_ms": "the silence after a blank line",
    "sentence_marks": "which characters end a sentence",
    "clause_marks": "which characters end a clause",
}

PACING_OUT_OF_RANGE = (
    "{field} is {value} ms, which is outside {low} to {high}. That setting is "
    "{meaning}; zero switches it off and anything past the top of that range is "
    "a voice that sounds like it stopped. Nothing was saved."
)

PACING_NOT_A_NUMBER = (
    "{field} is {value!r}, which is not a whole number of milliseconds. That "
    "setting is {meaning}; leave the box empty to follow the sentence gap. "
    "Nothing was saved."
)


PACING_MARKS_NOT_TEXT = (
    "{field} is {value!r}, which is not a list of characters. That setting is "
    "{meaning}; leave the box empty for the usual marks. Nothing was saved."
)

PACING_MARKS_NOT_PUNCTUATION = (
    "{field} contains “{char}”, which is a letter or a digit rather than a "
    "punctuation mark. A voice breaking there would stop in the middle of "
    "words. That setting is {meaning}; leave the box empty for the usual "
    "marks. Nothing was saved."
)

PACING_TOO_MANY_MARKS = (
    "{field} names {count} different characters, which is past the limit of "
    "{limit}. That setting is {meaning}; leave the box empty for the usual "
    "marks. Nothing was saved."
)


def marks_refusal(field: str, value: object) -> str | None:
    """``None`` if ``value`` is a usable character set for ``field``, else why.

    :func:`pacing_refusal`'s job for the two boxes beside the gaps, and the same
    contract: this is the *form's* half, said while the operator is still
    looking at what they typed. The reader's half is :func:`clean_marks`, which
    never refuses anything, because by the time a file is being read there is
    nobody to tell (the rule ``[synthesis]`` and the gaps already follow).

    Empty is always fine and means the default. A letter or a digit is refused
    rather than cleaned away here so that ``sentence_marks = "period"`` is
    answered as the mistake it is instead of quietly pacing at nothing.
    """
    if value is None:
        return None
    meaning = PACING_MEANING.get(field, "which characters break the speech")
    label = _label(field)
    if not isinstance(value, str):
        return PACING_MARKS_NOT_TEXT.format(field=label, value=value, meaning=meaning)
    if not value.strip():
        return None
    for char in value:
        if char.isalnum():
            return PACING_MARKS_NOT_PUNCTUATION.format(field=label, char=char, meaning=meaning)
    distinct = {char for char in value if not char.isspace()}
    if len(distinct) > MARKS_LIMIT:
        return PACING_TOO_MANY_MARKS.format(
            field=label, count=len(distinct), limit=MARKS_LIMIT, meaning=meaning
        )
    return None


def clean_marks(value: object) -> str:
    """The characters of ``value`` that can be broken at, or ``""``.

    The reader's counterpart to :func:`marks_refusal`, and deliberately silent:
    whitespace, letters, digits, repeats and anything past
    :data:`MARKS_LIMIT` are dropped rather than refused, so a hand-edited or
    hostile ``voice.toml`` costs the pacing and never the voice (PC-331).
    ``""`` out means "nothing usable was named", which the pattern builders
    read as the default — never as "no breaks at all", which is what a zero gap
    is for and has to stay distinct from.

    Whitespace goes because a space is not a mark: a voice naming one would be
    asking for a synthesis call per word.
    """
    if not isinstance(value, str):
        return ""
    kept: list[str] = []
    for char in value:
        if char.isspace() or char.isalnum() or char in kept:
            continue
        kept.append(char)
        if len(kept) == MARKS_LIMIT:
            break
    return "".join(kept)


def pacing_refusal(field: str, value: object) -> str | None:
    """``None`` if ``value`` is a usable gap for ``field``, else the sentence.

    The same shape and the same job as
    :func:`personacore.voice.engine.synthesis_refusal`, deliberately: the other
    numbers on that form are refused at save with a sentence and clamped with a
    note when they are hand-edited into the file, and these are numbers on that
    same form. A second convention for a box three inches lower down would be a
    second thing to learn for no reason.

    ``None`` — the box left empty — is always fine. For the sentence gap that
    means the default; for the other two it means "follow the ratio". Zero is
    **not** empty: it is a value, it is legal, and it means no gap.
    """
    if value is None:
        return None
    low, high = GAP_LIMITS
    meaning = PACING_MEANING.get(field, "a pacing gap")
    label = _label(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return PACING_NOT_A_NUMBER.format(field=label, value=value, meaning=meaning)
    number = float(value)
    # NaN fails every comparison, so it is caught by asking rather than by
    # falling through a range check that quietly says yes.
    if number != number or number in (float("inf"), float("-inf")):
        return PACING_NOT_A_NUMBER.format(field=label, value=value, meaning=meaning)
    if not low <= number <= high:
        return PACING_OUT_OF_RANGE.format(
            field=label,
            value=int(number) if number.is_integer() else number,
            low=low,
            high=high,
            meaning=meaning,
        )
    return None


def _label(field: str) -> str:
    """``sentence_gap_ms`` -> ``Sentence gap``. What the operator's box says."""
    return field.removesuffix("_ms").replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# What the splitter finds
# ---------------------------------------------------------------------------


class Break(StrEnum):
    """What follows a piece of text, and therefore how long the silence is."""

    NONE = "none"
    """Nothing follows. The last piece of the text."""

    CLAUSE = "clause"
    """An em dash, an en dash or a semicolon. Half a sentence gap."""

    SENTENCE = "sentence"
    """A full stop, a question mark, an exclamation mark — or a line break,
    which stops a line of a list every bit as hard as a full stop does and
    would otherwise run the whole list together, which is the fault this
    module exists to fix wearing different clothes."""

    PARAGRAPH = "paragraph"
    """A blank line. Double a sentence gap."""


@dataclass(frozen=True, slots=True)
class Piece:
    """One stretch of text to synthesise, and what comes after it.

    ``text`` is what an engine is handed — **with its own closing punctuation
    still on it**, because that punctuation is what produces the sentence-final
    slowing inside the model, and taking it off is precisely the mistake that
    made the first repair sound worse than the fault.
    """

    text: str
    break_after: Break = Break.NONE
    gap_ms: int | None = None
    """The silence after this piece, in milliseconds, when something asked for
    a specific one. ``None`` — the ordinary case — means the gap
    :attr:`break_after` implies for this voice.

    Set only by a persona's speech pauses (:func:`paced_words`), which is why
    it overrides rather than adjusts: a word listed there is being taken *out*
    of the punctuation's timing, not tuned within it. **Zero is a set value**
    and means no silence, so a word can be made to run straight on."""


@dataclass(frozen=True, slots=True)
class Pacing:
    """The three gaps for one voice, in milliseconds.

    ``clause_ms`` and ``paragraph_ms`` are ``None`` when nobody set them, which
    means "follow the ratio" — the same rule the rest of the voice metadata
    already uses, where an empty box and an absent field are one state. Zero is
    a set value and means no gap.

    ``sentence_marks`` and ``clause_marks`` are the characters this voice breaks
    at, and ``None`` — or a string with nothing usable in it — is the same "not
    set", meaning :data:`DEFAULT_SENTENCE_MARKS` and
    :data:`DEFAULT_CLAUSE_MARKS`. Empty is *not* "never break": that is a zero
    gap, and the two stay distinct.
    """

    sentence_ms: int = DEFAULT_SENTENCE_GAP_MS
    clause_ms: int | None = None
    paragraph_ms: int | None = None
    sentence_marks: str | None = None
    clause_marks: str | None = None

    @property
    def sentence_re(self) -> re.Pattern[str]:
        """What this voice ends a sentence at. Never raises, always a pattern."""
        return sentence_pattern(self.sentence_marks if isinstance(self.sentence_marks, str) else "")

    @property
    def clause_re(self) -> re.Pattern[str]:
        """What this voice ends a clause at. Never raises, always a pattern."""
        return clause_pattern(self.clause_marks if isinstance(self.clause_marks, str) else "")

    @property
    def sentence(self) -> int:
        return max(0, self.sentence_ms)

    @property
    def clause(self) -> int:
        if self.clause_ms is not None:
            return max(0, self.clause_ms)
        return max(0, round(self.sentence * CLAUSE_RATIO))

    @property
    def paragraph(self) -> int:
        if self.paragraph_ms is not None:
            return max(0, self.paragraph_ms)
        return max(0, round(self.sentence * PARAGRAPH_RATIO))

    @property
    def silent(self) -> bool:
        """Every gap is zero, so there is nothing to add and nothing to split.

        The text goes to the engine in one piece, which is what it did before
        PC-342 — not an approximation of it.
        """
        return not (self.sentence or self.clause or self.paragraph)

    def gap_ms(self, kind: Break) -> int:
        if kind is Break.SENTENCE:
            return self.sentence
        if kind is Break.CLAUSE:
            return self.clause
        if kind is Break.PARAGRAPH:
            return self.paragraph
        return 0


NO_PACING = Pacing(sentence_ms=0, clause_ms=0, paragraph_ms=0)
"""Every gap off — the running-together behaviour, on purpose."""


# ---------------------------------------------------------------------------
# The splitter — also PC-253's, which is half of why it is here
# ---------------------------------------------------------------------------

_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n\s*")
"""A blank line. Any run of them is one paragraph break, not several."""

_CLOSERS = r"""['"’”)\]]*"""
"""What may ride along after a sentence's own mark: a closing quote or bracket.

Not a per-voice setting. A voice does not pause differently because the full
stop was inside the quotation marks, and a mark that only ever *follows* the
break is not a mark anybody tunes a voice by."""

_SENTENCE_RE = re.compile(rf"""[{re.escape(DEFAULT_SENTENCE_MARKS)}]+{_CLOSERS}(?=\s|$)|\n""")
"""Sentence-final punctuation, keeping whatever closes a quote or a bracket
with it, and only when whitespace or the end follows.

The lookahead is what stops ``87.5`` and ``e.g.`` mid-word from becoming two
sentences: a stop with a word pressed against it is not the end of anything.
A bare newline counts — see :attr:`Break.SENTENCE`.

Built from :data:`DEFAULT_SENTENCE_MARKS` through the same escaping every voice's
own marks go through, so the default is not a second code path that could drift
from the one an operator's string takes."""

_CLAUSE_RE = re.compile(rf"[{re.escape(DEFAULT_CLAUSE_MARKS)}]")
"""Em dash, en dash, semicolon **and comma** — a decision made after
listening, and now only this voice's *default* rather than the rule.

The comma was deliberately left out at first, on the reasoning that a gap at
every comma is how speech starts to stuttering. Measured on real audio, the
model gives a comma **80 to 140 ms**, against 540 ms at a sentence: fast
enough to produce no audible pause at all. The clause gap was requested for
it, which is the same one a dash gets, and that is what this is — for a
voice that does not say otherwise.

Still not here by default: a hyphen joins words, and a colon introduces rather
than stops. Either is one character in a voice's ``clause_marks``."""

CLAUSE_MARKS_KEPT = frozenset(";")
"""Clause marks that travel with the clause they close, rather than being
dropped as separators.

**Core's, not the voice's.** Which characters break is a judgement about this
voice's pacing and is tuned by ear; whether a mark is worth handing to the
model is a fact about what a synthesiser does with the character, and it does
not change per voice. A semicolon reaches the model as a shortening of the
breath; a dash and a comma are marks on a page that read as nothing, and a
piece left ending in a dangling dash is handed to espeak with nothing after it
— the shape rule 2 in :mod:`personacore.voice.engines.espeak` is about.

Sentence marks are never dropped, which is not the same decision: taking the
full stop off is precisely what made the first repair sound worse than the
fault (:class:`Piece`)."""


def _char_class(marks: str) -> str:
    """``marks`` as the inside of a ``[...]``, every character escaped.

    **The one place an operator's string reaches a regular expression.** One
    :func:`re.escape` per character rather than any hand-rolled quoting: it is
    the standard library's answer to this exact question and it covers the four
    characters that mean something inside a class — ``]``, ``^``, ``-`` and the
    backslash — along with everything else. Nothing is ever interpolated raw,
    so ``.*[`` is three literal characters to break at and not a pattern.
    """
    return "".join(re.escape(char) for char in marks)


@lru_cache(maxsize=64)
def sentence_pattern(marks: str = "") -> re.Pattern[str]:
    """The sentence splitter for a voice naming ``marks``. Never raises.

    Cached because it is rebuilt on every reply and the answer only depends on
    the string; bounded because the string comes from a file.

    Two ways to end up with the default, and both are deliberate: nothing
    usable was named (:func:`clean_marks`), or the pattern would not compile —
    which :func:`_char_class` should already make impossible and which is
    caught anyway, because "the pacing is wrong" and "the reply does not
    happen" are not the same size of problem.
    """
    body = _char_class(clean_marks(marks))
    if not body:
        return _SENTENCE_RE
    try:
        return re.compile(rf"[{body}]+{_CLOSERS}(?=\s|$)|\n")
    except re.error as exc:  # pragma: no cover - _char_class is what prevents this
        _log.warning(
            "pacing: sentence marks %r will not compile (%s); using the default", marks, exc
        )
        return _SENTENCE_RE


@lru_cache(maxsize=64)
def clause_pattern(marks: str = "") -> re.Pattern[str]:
    """The clause splitter for a voice naming ``marks``. Never raises.

    :func:`sentence_pattern`'s twin, on the same terms. A voice that names no
    clause marks at all still gets the default set — switching clause breaks
    off is ``clause_gap_ms = 0``, and keeping the two apart is what lets an
    operator silence the clause gap without also losing where the clauses are.
    """
    body = _char_class(clean_marks(marks))
    if not body:
        return _CLAUSE_RE
    try:
        return re.compile(f"[{body}]")
    except re.error as exc:  # pragma: no cover - _char_class is what prevents this
        _log.warning("pacing: clause marks %r will not compile (%s); using the default", marks, exc)
        return _CLAUSE_RE


_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "mt",
        "no",
        "vs",
        "etc",
        "fig",
        "jr",
        "sr",
        "inc",
        "ltd",
        "co",
        "approx",
        "est",
        "dept",
        "univ",
    }
)
"""Words that end in a full stop without ending a sentence.

Short and unambitious on purpose: this list only has to stop the pauses a
listener would notice as wrong. Anything it misses costs one gap in the middle
of a sentence, which is a blemish; anything wrongly *in* it costs a missing
pause between two sentences, which is the fault being fixed. When in doubt a
word stays out.
"""

_WORD_BEFORE = re.compile(r"(\S+)\s*$")

_HAS_SOUND = re.compile(r"[^\W_]", re.UNICODE)
"""Something in this piece that can be pronounced. A piece of pure punctuation
is not handed to an engine on its own; it is kept with the piece before it."""

MAX_PIECES = 500
"""A ceiling on how finely one call is cut up.

Text reaching here has already been capped
(:data:`personacore.voice.reply.MAX_SPOKEN_CHARS`), but a synthesis call per
piece is real work and something arriving from a model is third-party input
(spec §7). Past this the remainder is spoken as one piece rather than refused:
losing the pacing on the tail of an enormous reply is a disappointment, and
losing the reply is a fault.
"""


def split_paced(
    text: str,
    pacing: Pacing | None = None,
    *,
    pauses: Sequence[PausedWord] = (),
) -> list[Piece]:
    """``text`` cut at its pace points, each piece carrying what follows it.

    Paragraphs first, then sentences inside a paragraph, then clauses inside a
    sentence — so the strongest break at any position wins, and a full stop at
    the end of a paragraph produces one paragraph gap rather than a sentence
    gap followed by a paragraph one.

    ``pacing`` says which characters this voice breaks at. Omitted, the defaults
    apply, which is what every caller wanted before the marks became a setting.
    The **paragraph** break is not among them: a blank line is a blank line, not
    a character somebody could name.

    The pieces are what an engine is asked to say, so each keeps its own
    closing punctuation. The separators that are *only* separators go — see
    :data:`CLAUSE_MARKS_KEPT`.

    ``pauses`` is the persona's list of words that get pauses of their own
    (:func:`paced_words`). It runs last and changes no text at all — only where
    the cuts are and how long the silence at them is.
    """
    body = (text or "").strip()
    if not body:
        return []
    paced = pacing if pacing is not None else Pacing()
    if paced.silent:
        # Every gap off is "do not split", not "split and add nothing": the
        # text reaches the engine in one call exactly as it did before this
        # module existed. A persona's own pauses are the one thing that still
        # cuts it, because those are an instruction about a particular word
        # rather than a default this voice declined.
        whole = [Piece(text=body)]
        return _capped(paced_words(whole, pauses) if pauses else whole)
    sentence_re = paced.sentence_re
    clause_re = paced.clause_re
    pieces: list[Piece] = []
    paragraphs = [part for part in _PARAGRAPH_RE.split(body) if part.strip()]
    for index, paragraph in enumerate(paragraphs):
        last_paragraph = index == len(paragraphs) - 1
        sentences = _sentences(paragraph, sentence_re)
        for position, sentence in enumerate(sentences):
            last_sentence = position == len(sentences) - 1
            after = (
                Break.NONE
                if last_sentence and last_paragraph
                else Break.PARAGRAPH
                if last_sentence
                else Break.SENTENCE
            )
            pieces.extend(_clauses(sentence, after, clause_re))
    merged = _merge_unspeakable(pieces)
    # The named words last, so they are cut out of the pieces punctuation
    # produced rather than competing with it.
    return _capped(paced_words(merged, pauses) if pauses else merged)


def complete_sentences(text: str, pacing: Pacing | None = None) -> int:
    """How much of ``text`` is sentences that have **certainly** ended.

    :func:`split_paced`'s answer to a buffer that is still being written to.
    Text arriving a token at a time cannot be split by asking "where are the
    boundaries" — the answer changes as the next characters land — so this
    returns an offset: ``text[:offset]`` is safe to speak now and
    ``text[offset:]`` has to wait for more. ``0`` means nothing is finished
    yet, which is the ordinary answer partway through a sentence.

    The one rule that makes it safe is that **a boundary at the very end of
    the buffer does not count**. ``The reading is 87.`` is a finished sentence
    or the first half of ``87.5`` depending on a character that has not
    arrived, and the same goes for a stop that a closing quote is about to
    follow. Waiting for one more character costs a chunk of latency; guessing
    costs a sentence spoken as two, mid-number, in the listener's ear.

    Everything else — which characters this voice breaks at, and the
    abbreviation guard that keeps ``e.g.`` and ``Dr.`` whole — is
    :func:`split_paced`'s own, deliberately: streaming and non-streaming
    speech that split at different places would be two different readings of
    the same reply (PC-257), and ADR-0029 already said this is one splitter.
    """
    if not text:
        return 0
    paced = pacing if pacing is not None else Pacing()
    end = 0
    for match in paced.sentence_re.finditer(text):
        if match.end() >= len(text):
            # Nothing further along can be safe either, so stop looking.
            break
        if _abbreviating(text, match):
            continue
        end = match.end()
    return end


def _sentences(paragraph: str, pattern: re.Pattern[str]) -> list[str]:
    """One paragraph cut at its sentence ends, punctuation kept on the left."""
    out: list[str] = []
    start = 0
    for match in pattern.finditer(paragraph):
        if _abbreviating(paragraph, match):
            continue
        chunk = paragraph[start : match.end()].strip()
        if chunk:
            out.append(chunk)
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        out.append(tail)
    return out or ([paragraph.strip()] if paragraph.strip() else [])


def _abbreviating(paragraph: str, match: re.Match[str]) -> bool:
    """Is this full stop shortening a word rather than ending a sentence?

    Only a lone ``.`` is ever in question — nobody abbreviates with ``?`` — and
    only by what stands in front of it: a word with a stop already inside it
    (``e.g.``, ``a.m.``, ``U.S.``), a single letter (``J. R. R.``), or one of
    the handful in :data:`_ABBREVIATIONS`. A pause inside "e.g. a coat" is
    exactly as wrong as no pause between two sentences, and this is the cheap
    half of not doing either.
    """
    if match.group() != ".":
        return False
    before = _WORD_BEFORE.search(paragraph[: match.start() + 1])
    if before is None:
        return False
    token = before.group(1)
    if "." in token[:-1]:
        return True
    stem = token.rstrip(".").casefold()
    return len(stem) == 1 or stem in _ABBREVIATIONS


def _clauses(sentence: str, after: Break, pattern: re.Pattern[str]) -> list[Piece]:
    """One sentence cut at its clause marks; the last piece carries ``after``.

    The semicolon is kept on the clause it ends and everything else is dropped
    — :data:`CLAUSE_MARKS_KEPT`, which is core's judgement about what a model
    does with a character and not part of what a voice tunes.
    """
    parts: list[str] = []
    start = 0
    for match in pattern.finditer(sentence):
        # A semicolon closes the clause and travels with it; a dash does not.
        end = match.end() if match.group() in CLAUSE_MARKS_KEPT else match.start()
        chunk = sentence[start:end].strip()
        if chunk:
            parts.append(chunk)
        start = match.end()
    tail = sentence[start:].strip()
    if tail:
        parts.append(tail)
    if not parts:
        return []
    return [
        Piece(text=part, break_after=after if index == len(parts) - 1 else Break.CLAUSE)
        for index, part in enumerate(parts)
    ]


def _merge_unspeakable(pieces: list[Piece]) -> list[Piece]:
    """Fold a piece with nothing pronounceable in it into the one before.

    ``"..."`` or ``")"`` alone is not something to hand a model and time a gap
    around; it belongs to the words it came off. The break it carried is kept,
    because the pause after it is still real.
    """
    out: list[Piece] = []
    for piece in pieces:
        if _HAS_SOUND.search(piece.text) or not out:
            out.append(piece)
            continue
        previous = out[-1]
        # Rejoined with the space that was between them: "Done. ..." is what
        # was written and "Done...." is not, and the difference reaches a model.
        out[-1] = Piece(
            text=f"{previous.text} {piece.text}",
            break_after=piece.break_after,
        )
    # A leading piece of pure punctuation survives the loop above, since there
    # was nothing before it to join. Drop it rather than synthesise it.
    return [piece for piece in out if _HAS_SOUND.search(piece.text)]


def _capped(pieces: list[Piece]) -> list[Piece]:
    """At most :data:`MAX_PIECES`, with the remainder spoken as one."""
    if len(pieces) <= MAX_PIECES:
        return pieces
    head = pieces[: MAX_PIECES - 1]
    tail = " ".join(piece.text for piece in pieces[MAX_PIECES - 1 :])
    _log.warning(
        "pacing: %d pieces is past the ceiling of %d; the tail is spoken as one",
        len(pieces),
        MAX_PIECES,
    )
    return [*head, Piece(text=tail, break_after=Break.NONE)]


# ---------------------------------------------------------------------------
# Words a persona pauses around — the same timing, aimed by hand
# ---------------------------------------------------------------------------
#
# "Hmm." is a complete sentence, so everything above gives it a full sentence
# gap on both sides and a throwaway murmur is delivered like a pronouncement.
# A persona lists such words with the pauses they should get instead
# (:mod:`personacore.agent.personas`), and this is where that list acts.
#
# **The text is never touched.** The word is not replaced, shortened, or
# repunctuated — it becomes a piece of its own with stated gaps around it, so
# what the engine is asked to say is character-for-character what the model
# wrote and only the silence between the pieces differs. Rewriting the words
# instead was considered and rejected: turning "Hmm." into "Hmm?" changes an
# aside into a question, which is a change of meaning made to fix a change of
# delivery.

PausedWord = tuple[str, int, int]
"""``(word, before_ms, after_ms)`` — a plain tuple, so a persona's list crosses
into this module without either side importing the other's classes."""

_WORD_EDGE = r"[\w'’]"
"""What may not sit against a listed word for it to count as that word.

Letters, digits and underscore, plus both apostrophes: ``hmm`` must not fire
inside ``hmmm``, and ``don`` must not fire inside ``don't``. Punctuation is not
on the list, so a listed ``Hmm`` still matches the ``Hmm`` in ``Hmm.``, which
is the whole case this exists for."""


@lru_cache(maxsize=64)
def pause_pattern(words: tuple[str, ...]) -> re.Pattern[str] | None:
    """One case-insensitive matcher for a persona's listed words. Never raises.

    **Every word is a literal**, one :func:`re.escape` each, for the reason
    :func:`_char_class` gives about the marks: what an operator types is
    find-a-word, and nothing they can put in that box may reach the engine as a
    pattern. Longest first, so ``uh huh`` is offered before ``uh`` and wins the
    text they both cover.

    Cached on the tuple of words because it is rebuilt for every reply and the
    answer depends on nothing else; bounded because the list comes from a file.
    ``None`` when there is nothing to match, which is the ordinary case.
    """
    escaped = [re.escape(word) for word in sorted(set(words), key=lambda w: (-len(w), w)) if word]
    if not escaped:
        return None
    return re.compile(
        rf"(?<!{_WORD_EDGE})(?:{'|'.join(escaped)})(?!{_WORD_EDGE})",
        re.IGNORECASE,
    )


def paced_words(pieces: list[Piece], pauses: Sequence[PausedWord]) -> list[Piece]:
    """``pieces`` again, with each listed word standing alone in its own gaps.

    A word already alone — ``Hmm.`` as its own sentence — keeps its text and
    only changes the silence either side. A word **mid-sentence** is cut out of
    the piece it was in, so "yes, mmm, very good" becomes three pieces where
    the punctuation had already made three, and "I think hmm it works" becomes
    three where the punctuation had made one. Either way the words are the same
    words in the same order.
    """
    gaps = {word.casefold(): (before, after) for word, before, after in pauses}
    pattern = pause_pattern(tuple(word for word, _, _ in pauses))
    if pattern is None:
        return pieces
    out: list[Piece] = []
    for piece in pieces:
        _extend_paced(out, piece, pattern, gaps)
    return out


def _extend_paced(
    out: list[Piece],
    piece: Piece,
    pattern: re.Pattern[str],
    gaps: Mapping[str, tuple[int, int]],
) -> None:
    """Append ``piece``, split around any listed word inside it, onto ``out``."""
    text = piece.text
    parts: list[Piece] = []
    cursor = 0
    for match in pattern.finditer(text):
        found = match.group()
        pause = gaps.get(found.casefold(), gaps.get(found.lower()))
        if pause is None:  # pragma: no cover - a fold that is not a lowercase
            continue
        before_ms, after_ms = pause
        start, end = match.start(), match.end()
        head = text[cursor:start]
        if _HAS_SOUND.search(head):
            parts.append(Piece(text=head.strip(), gap_ms=before_ms))
        else:
            # Nothing pronounceable in front of the word inside this piece —
            # whitespace, or an opening bracket. It travels with the word
            # rather than becoming a piece an engine is asked to say, and the
            # pause lands on whatever comes before instead.
            start = cursor
            _pause_before(out, parts, before_ms)
        if not _HAS_SOUND.search(text[end:]):
            # The tail is the word's own punctuation ("Hmm." for a listed
            # "Hmm"). Sliced, not stripped and rejoined, so the piece is the
            # original characters exactly — "Hmm ." reaches a model as
            # something else.
            end = len(text)
        parts.append(Piece(text=text[start:end].strip(), gap_ms=after_ms))
        cursor = end
        if cursor >= len(text):
            break
    if not parts:
        out.append(piece)
        return
    remainder = text[cursor:]
    if _HAS_SOUND.search(remainder):
        parts.append(Piece(text=remainder.strip(), break_after=piece.break_after))
    else:
        # The listed word ended the piece, so it inherits what followed the
        # piece — but **its own gap wins over the punctuation's**, which is the
        # whole point: "Hmm." stops being a sentence-sized silence. At the very
        # end of a reply nothing follows at all, so there is no gap to keep.
        last = parts[-1]
        parts[-1] = replace(
            last,
            break_after=piece.break_after,
            gap_ms=None if piece.break_after is Break.NONE else last.gap_ms,
        )
    out.extend(parts)


def _pause_before(out: list[Piece], parts: list[Piece], before_ms: int) -> None:
    """Put a listed word's leading pause on whatever precedes it.

    Nothing precedes the first piece of a reply, and silence before the first
    word is not a pause — it is a delay — so there it is dropped.

    Where two named pauses meet (one word's "after" against the next word's
    "before") the **longer wins** rather than the two adding up: adding them
    produces a silence neither rule asked for, and the operator who wrote the
    longer one has already said how long that gap should be.
    """
    target = parts or out
    if not target:
        return
    previous = target[-1]
    merged = before_ms if previous.gap_ms is None else max(previous.gap_ms, before_ms)
    target[-1] = replace(previous, gap_ms=merged)


# ---------------------------------------------------------------------------
# Silence, and putting the pieces back together
# ---------------------------------------------------------------------------

_SAMPLE_WIDTHS: dict[str, int] = {
    "pcm_s16le": 2,
    "pcm_s24le": 3,
    "pcm_s32le": 4,
    "pcm_f32le": 4,
}
"""Bytes per sample per channel, by :attr:`personacore.voice.engine.Audio.
encoding`. Anything else is assumed 16-bit, which is what every engine in this
build produces and what :func:`personacore.voice.engine.wav_bytes` writes."""


def sample_width(encoding: str) -> int:
    return _SAMPLE_WIDTHS.get((encoding or "").lower(), 2)


def silence(milliseconds: int, *, sample_rate: int, channels: int, encoding: str) -> bytes:
    """A gap, as raw samples in the same format as the audio around it.

    Zero bytes, and aligned to a whole frame: half a frame of silence spliced
    between two pieces would shift every sample after it by one byte and turn
    the rest of the reply into noise.
    """
    if milliseconds <= 0 or sample_rate <= 0:
        return b""
    frames = int(sample_rate * milliseconds / 1000)
    return bytes(frames * max(1, channels) * sample_width(encoding))


SpeakOne = Callable[[str], Audio]
"""``text -> audio``, one engine call. The seam this module is written against
so that it never knows what an engine is."""


def speak_paced(
    speak_one: SpeakOne,
    text: str,
    pacing: Pacing,
    *,
    pauses: Sequence[PausedWord] = (),
) -> Audio:
    """``text`` spoken, split at its pace points and joined with silence.

    The one place the split-and-pace step happens, called from
    :meth:`personacore.voice.library.VoiceResolution.speak` — which is the only
    thing in this build that calls an engine's ``speak`` at all.

    ``pauses`` is the speaking persona's list of words that take pauses of
    their own. It changes where the cuts fall and how long the silences are; it
    never changes a character of ``text``.

    **One piece is one call with the original text**, unsplit and unmodified.
    That is not an optimisation: a single sentence, and every text at all when
    the gaps are zero, has to reach the engine exactly as it did before this
    module existed, or "zero gives the old behaviour" would be an approximation
    of the old behaviour instead of the thing itself.
    """
    pieces = split_paced(text, pacing, pauses=pauses)
    if len(pieces) <= 1:
        return speak_one(text)

    spoken = [speak_one(piece.text) for piece in pieces]
    first = spoken[0]
    # The format of the first piece is the format of the whole: one voice on
    # one engine produces one rate, and an engine that changed it mid-reply
    # would be a defect in the engine rather than something to resample here.
    rate = int(getattr(first, "sample_rate", 22050) or 22050)
    channels = int(getattr(first, "channels", 1) or 1)
    encoding = str(getattr(first, "encoding", "pcm_s16le") or "pcm_s16le")
    gaps: dict[int, bytes] = {}

    def gap(milliseconds: int) -> bytes:
        """The silence for one gap, made once per distinct length.

        A dictionary keyed by milliseconds rather than by break kind, because a
        persona's paused word names a length directly and two pieces asking for
        the same silence should not build it twice.
        """
        if milliseconds not in gaps:
            gaps[milliseconds] = silence(
                milliseconds, sample_rate=rate, channels=channels, encoding=encoding
            )
        return gaps[milliseconds]

    data = bytearray()
    for piece, audio in zip(pieces, spoken, strict=True):
        data += bytes(getattr(audio, "data", b"") or b"")
        # An explicit gap wins over the one the punctuation implies — that is
        # what a persona's speech pause *is* (:func:`paced_words`).
        after = piece.gap_ms if piece.gap_ms is not None else pacing.gap_ms(piece.break_after)
        if after > 0:
            data += gap(after)
    return Audio(data=bytes(data), sample_rate=rate, channels=channels, encoding=encoding)


# ---------------------------------------------------------------------------
# What a voice folder says about its own pacing
# ---------------------------------------------------------------------------

PACING_TABLE = "pacing"
"""``[pacing]`` in ``voice.toml`` — ``docs/voice-pack-format.md``."""


def read_pacing(directory: Path | None) -> tuple[Pacing, list[str]]:
    """One voice's ``[pacing]``, and any note about what was not honoured.

    **Never raises**, for the same reason the engine's own reader does not: a
    ``voice.toml`` that will not parse costs the labels in it, never the voice
    (PC-331). A voice with no file, no table or no keys gets the defaults,
    which is the 450 ms chosen by ear.

    A value hand-edited outside the range is **clamped with a note** rather
    than refused — the form refuses it while the operator is still looking at
    it (:func:`pacing_refusal`), and by the time a file is being read there is
    nobody to tell, so the choice is between speaking at a sensible figure and
    saying so, or not speaking. This is the rule ``[synthesis]`` already
    follows in :func:`personacore.voice.engines.vits_onnx._synthesis_defaults`,
    matched rather than reinvented.
    """
    document = _document(directory)
    table = document.get(PACING_TABLE)
    if not isinstance(table, Mapping):
        return Pacing(), []

    notes: list[str] = []
    #: Keyed by the field name in the file; :class:`Pacing` names them without
    #: the units, because a millisecond is not part of what the number means.
    values: dict[str, int | None] = {
        "sentence_gap_ms": DEFAULT_SENTENCE_GAP_MS,
        "clause_gap_ms": None,
        "paragraph_gap_ms": None,
    }
    for field in PACING_FIELDS:
        if field not in table:
            continue
        candidate = table[field]
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            # An export writes every field and the ones nobody filled in carry
            # `""`. Blank is "not set", exactly as a missing key is — which for
            # the two ratio fields is what makes them follow the sentence gap.
            continue
        refusal = pacing_refusal(field, candidate)
        if refusal is None:
            values[field] = int(candidate)
            continue
        low, high = GAP_LIMITS
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            number = float(candidate)
            if number == number and number not in (float("inf"), float("-inf")):
                used = int(min(high, max(low, number)))
                values[field] = used
                notes.append(
                    f"voice.toml asks for {field} = {candidate}, which is outside "
                    f"{low} to {high} ms; this voice paces at {used} ms."
                )
                continue
        notes.append(
            f"voice.toml's {field} is not a whole number of milliseconds, so it "
            "was ignored and this voice paces at its default."
        )
    marks = _read_marks(table, notes)
    for note in notes:
        _log.warning("voice %s: %s", getattr(directory, "name", "?"), note)
    return (
        Pacing(
            sentence_ms=values["sentence_gap_ms"] or 0,
            clause_ms=values["clause_gap_ms"],
            paragraph_ms=values["paragraph_gap_ms"],
            sentence_marks=marks["sentence_marks"],
            clause_marks=marks["clause_marks"],
        ),
        notes,
    )


def _read_marks(table: Mapping[str, Any], notes: list[str]) -> dict[str, str | None]:
    """The two character sets out of ``[pacing]``, cleaned, never refused.

    The gaps above are clamped with a note when a file asks for something out
    of range; these are *narrowed* with a note, by exactly the same reasoning.
    A file naming five marks of which one is a letter paces at the other four
    and says so, because the alternative on a machine with nobody watching is a
    voice that stops speaking over a typo.
    """
    marks: dict[str, str | None] = {field: None for field in PACING_MARK_FIELDS}
    for field in PACING_MARK_FIELDS:
        if field not in table:
            continue
        candidate = table[field]
        # An export writes every field and blank means "not set", the same
        # state as a missing key — which here is the default set, never "no
        # breaks at all". Nothing to say about it.
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            continue
        if not isinstance(candidate, str):
            notes.append(
                f"voice.toml's {field} is not a list of characters, so it was "
                "ignored and this voice breaks at the usual marks."
            )
            continue
        cleaned = clean_marks(candidate)
        if not cleaned:
            notes.append(
                f"voice.toml's {field} names nothing that can be broken at, so it "
                "was ignored and this voice breaks at the usual marks."
            )
            continue
        if cleaned != candidate:
            notes.append(
                f"voice.toml asks for {field} = {candidate!r}; letters, spaces and "
                f"repeats are not marks, so this voice breaks at {cleaned!r}."
            )
        marks[field] = cleaned
    return marks


def _document(directory: Path | None) -> Mapping[str, Any]:
    """The parsed ``voice.toml``, or an empty mapping. Never raises."""
    if directory is None:
        return {}
    path = Path(directory) / "voice.toml"
    try:
        if not path.is_file():
            return {}
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _log.warning("voice %s: ignoring voice.toml, it does not parse (%s)", path.parent.name, exc)
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


__all__ = [
    "CLAUSE_MARKS_KEPT",
    "CLAUSE_RATIO",
    "DEFAULT_CLAUSE_MARKS",
    "DEFAULT_SENTENCE_GAP_MS",
    "DEFAULT_SENTENCE_MARKS",
    "GAP_LIMITS",
    "MARKS_LIMIT",
    "MAX_PIECES",
    "NO_PACING",
    "PACING_FIELDS",
    "PACING_MARKS_NOT_PUNCTUATION",
    "PACING_MARKS_NOT_TEXT",
    "PACING_MARK_FIELDS",
    "PACING_MEANING",
    "PACING_NOT_A_NUMBER",
    "PACING_OUT_OF_RANGE",
    "PACING_TABLE",
    "PACING_TOO_MANY_MARKS",
    "PARAGRAPH_RATIO",
    "Break",
    "Pacing",
    "Piece",
    "PausedWord",
    "clause_pattern",
    "clean_marks",
    "marks_refusal",
    "paced_words",
    "pacing_refusal",
    "pause_pattern",
    "read_pacing",
    "sample_width",
    "sentence_pattern",
    "silence",
    "speak_paced",
    "split_paced",
]
