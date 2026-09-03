"""Speaking a finished reply — PC-256, and the first time anything but a
button calls :meth:`~personacore.voice.library.VoiceResolution.speak`.

Voice was built and never connected: engines switch on, voices install, the
test-speak button talks, and the assistant answered in text because no code
path between the agent loop and an engine existed. This module is that path,
for the simple case only — **the text finishes, then it is spoken.** Streaming
(PC-252 onward) is a separate, later piece of work and nothing here presumes
what it will look like.

Three properties, and the third is the one that decides the shape.

**The plain text is what gets spoken.** PC-264 keeps a reply in plain text
beside its rendered form for exactly this moment; the agent loop's own string
arrives here before any template has seen it. Plain is not the same as
speakable, though — the model writes ordinary markdown, and ``**Today**`` read
out as "asterisk asterisk Today" is the failure PC-264 was written to prevent.
:func:`speakable_text` is where markup stops.

**The persona's own timing travels with the reply.** A character that says
"Hmm." constantly can list that word with pauses of its own
(:mod:`personacore.agent.personas`); this module carries that list from the
persona to the engine call, where it is used as *timing only*. No word of the
reply is changed by it — not for speech, not for the screen, not for the
transcript.

**A persona whose engine is off replies in text and keeps working** (PC-336).
:meth:`VoiceLibrary.resolve` already answers that for every case and raises for
none; this module keeps the property by never treating "cannot speak" as an
error — it is a reply with no audio and a sentence saying why.

**Speech may never delay or break the text**, which is why nothing is
synthesised on the reply path at all. :meth:`ReplySpeaker.offer` does string
work and one filesystem lookup, mints a handle, and returns; the audio is
synthesised only when something asks for it, on a request of its own, after
the answer is already on the operator's screen. An engine that is slow, dead
or dies mid-sentence therefore costs the audio and cannot touch the reply —
not because the failure is caught carefully, but because the reply was already
delivered before the engine was ever asked. The alternative (synthesise, then
return both) makes the answer wait on a model that may be loading, and buries
a 1 MB WAV per turn in the process for a reply nobody may play.

The handle is what a surface is given, and it is deliberately not the text: a
URL carrying the reply would put an arbitrary-text synthesis endpoint on the
admin surface, and a long answer does not fit in one anyway.
"""

from __future__ import annotations

import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import structlog

from personacore.voice.engine import Audio, EngineError, VoiceState

log = structlog.get_logger(__name__)

AUDIO_PATH_PREFIX = "/admin/chat/reply"
"""Where the audio for a reply is fetched from. One constant, read by the
speaker that writes the URL and by the route that answers it, so the two
cannot be changed apart."""

SPEAKER_ATTRIBUTE = "reply_speech"
"""What the running application keeps the speaker under
(``app.state.reply_speech``), alongside ``voices`` and ``voice_registry``.

``None`` — or absent — means this core was assembled without one, and every
reply is text. The route that serves the audio says so rather than failing."""

MAX_SPOKEN_CHARS = 4000
"""How much of one reply is synthesised.

Roughly five minutes of speech. A reply longer than this is a document, and
synthesising all of it holds a worker thread for minutes to produce audio
nobody listens to the end of. The text is never truncated — only the audio
stops, at a sentence boundary, which is the failure mode a listener can
actually understand."""

MAX_PENDING_REPLIES = 32
"""Replies whose audio can still be fetched, per core.

Oldest first out. A conversation is read back from the transcript store, so
this is not memory of the conversation — it is only how far back the play
button keeps working, and holding every reply of every session would be an
unbounded dictionary fed by whatever the model writes."""

REPLY_GONE = (
    "That reply is no longer available to speak. Only the last few replies "
    "keep their audio; the text is still on the screen and in the log."
)
"""Also what a handle belonging to somebody else gets, word for word — an
expired reply and another operator's reply must not be tellable apart."""

SPEECH_UNAVAILABLE = (
    "This reply is text only: the voice subsystem could not be asked whether "
    "it could speak, so the answer was sent without waiting for it."
)

SYNTHESIS_FAILED = (
    "{engine} could not speak that reply. The reply itself is unaffected — it "
    "is on the screen and in the log. Try the test line on the Voice screen to "
    "see whether the engine is answering at all."
)


# ---------------------------------------------------------------------------
# Markup never reaches an engine
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_QUOTE = re.compile(r"^ {0,3}(?:>\s?)+")
_LIST_MARKER = re.compile(r"^([ \t]*)(?:[-*+]|\d{1,9}[.)])\s+")
_RULE = re.compile(r"^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$")
_TABLE_DELIMITER = re.compile(r"^[\s|:-]*[-][\s|:-]*$")
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_BOLD_ITALIC = re.compile(r"\*{1,3}(\S(?:.*?\S)?)\*{1,3}", re.DOTALL)
_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9_])_{1,3}(\S(?:.*?\S)?)_{1,3}(?![A-Za-z0-9_])")
_LEFTOVER_STARS = re.compile(r"\*+")
_BLANK_RUN = re.compile(r"\n{3,}")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s")


def speakable_text(reply: str, *, limit: int = MAX_SPOKEN_CHARS) -> str:
    """The reply as words to be said, with every mark of markup taken out.

    The subset handled is the one the chat screen renders
    (:mod:`personacore.web.markdown`) — headings, emphasis, lists,
    quotes, code, links, tables — because that is what the model writes and
    what the screen already treats as markup. Whatever is left after this is
    what an engine is asked to pronounce, so anything ambiguous is dropped
    rather than kept: a stray asterisk said out loud is a defect, and a stray
    asterisk missing from speech is nothing at all.

    Two decisions worth naming, since both change what a listener hears
    relative to what a reader sees:

    * **A link is spoken as its label, never its target.** A URL read aloud is
      unusable to a listener and interminable; the reader still gets it from
      the screen, where it is printed as text.
    * **A code block is spoken, without its fence.** Skipping it would leave a
      listener with an answer whose middle silently went missing, which is
      worse than hearing punctuation named. It is the one place this function
      keeps something a listener may not want.
    """
    text = _CONTROL.sub("", reply or "")
    lines: list[str] = []
    fenced = False
    for raw in text.splitlines():
        if _FENCE.match(raw):
            fenced = not fenced
            continue
        if fenced:
            # Inside a fence nothing is markup: the asterisks are the code.
            lines.append(raw)
            continue
        line = raw
        if _RULE.match(line) or (_TABLE_DELIMITER.match(line) and "|" in line):
            continue
        line = _QUOTE.sub("", line)
        heading = _HEADING.match(line)
        if heading is not None:
            line = heading.group(1)
        line = _LIST_MARKER.sub(r"\1", line)
        if "|" in line:
            # A table row is read as its cells, comma separated. The pipes
            # themselves are furniture and would be spoken as "vertical bar".
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            line = ", ".join(cell for cell in cells if cell)
        lines.append(_inline(line))
    out = _BLANK_RUN.sub("\n\n", "\n".join(line.rstrip() for line in lines)).strip()
    return _capped(out, limit)


def _inline(line: str) -> str:
    """One line's inline markup removed, in the order the marks nest."""
    line = _HTML_TAG.sub("", line)
    line = _IMAGE.sub(r"\1", line)
    line = _LINK.sub(r"\1", line)
    # Code spans first: the backticks protect what is inside them from the
    # emphasis rules, and `**` inside a span is code, not bold.
    line = _CODE_SPAN.sub(r"\2", line)
    line = _STRIKE.sub(r"\1", line)
    line = _BOLD_ITALIC.sub(r"\1", line)
    line = _UNDERSCORE.sub(r"\1", line)
    # What is left is unpaired — an opened bold that never closed, a bullet
    # that was not at the start of a line. Backticks and asterisks are never
    # words, so they go; underscores are left alone because they are how
    # identifiers are spelled and cutting them would rename things.
    line = line.replace("`", "")
    return _LEFTOVER_STARS.sub("", line)


def _capped(text: str, limit: int) -> str:
    """``text``, ending at the last sentence that fits inside ``limit``."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    ends = list(_SENTENCE_END.finditer(head))
    if ends and ends[-1].end() > limit // 2:
        return head[: ends[-1].end()].rstrip()
    cut = head.rfind(" ")
    return (head[:cut] if cut > limit // 2 else head).rstrip()


# ---------------------------------------------------------------------------
# A persona's own pauses, on the way past
# ---------------------------------------------------------------------------


def _pauses_of(persona: Any) -> tuple[tuple[str, int, int], ...]:
    """One persona's speech pauses as plain triples, or none at all.

    **Never raises and never rewrites anything.** A persona's pauses are words
    it says with silences of their own (spec §5.5, and
    :mod:`personacore.agent.personas`); all this does is carry them to the
    engine call, where :func:`~personacore.voice.pacing.paced_words` uses them
    as timing. Anything unexpected in that attribute is no pauses — the reply
    is spoken exactly as it is today, which is the failure mode this whole
    module is built around.
    """
    try:
        listed = getattr(persona, "speech_pauses", ()) or ()
        return tuple(
            (str(word), int(before), int(after))
            for word, before, after in listed
            if str(word)
        )
    except Exception as exc:  # noqa: BLE001 - pacing may never cost the reply
        log.warning("reply_speech_pauses_unusable", error=repr(exc))
        return ()


# ---------------------------------------------------------------------------
# What a reply offers a surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplySpeech:
    """Audio for one finished reply, or the sentence saying why there is none.

    The whole surface a chat screen consumes. ``audio_url`` is set exactly when
    :attr:`can_speak`, so a screen renders a player or a line of text and can
    never be handed both or neither.

    ``reason`` is ``None`` when there is nothing to say — a persona with no
    voice is a choice, not a fault, and a sentence about it on every single
    reply would be noise. It carries the library's own wording (PC-336) when a
    voice was chosen and cannot be heard, so what the operator reads under the
    reply is what actually decided the reply was silent.
    """

    can_speak: bool = False
    audio_url: str | None = None
    reason: str | None = None
    voice_label: str = ""
    """``GLaDOS (vits-onnx)``, or ``""`` when no voice was resolved."""


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """One kept reply, as words and a voice, for a synthesiser that is not here.

    The Wyoming path needs exactly this and nothing else: the text to say and
    the name of the voice to say it in. ``engine/voice`` is the core's own
    unique name for a voice (:attr:`~personacore.voice.library.LibraryEntry.key`)
    and is what this core's Wyoming server advertises, so it is what comes
    back — a bare voice id is not unique across engines.
    """

    text: str
    engine_id: str
    voice_id: str
    voice_label: str = ""

    @property
    def voice_name(self) -> str:
        """``vits-onnx/glados`` — what goes on ``synthesize-start``."""
        return f"{self.engine_id}/{self.voice_id}"


@dataclass(frozen=True, slots=True)
class _Pending:
    """One reply that can still be spoken, and who it belongs to.

    The engine and voice are kept **by name**, not as the resolution that
    produced them: the switch may be flipped between the reply arriving and the
    play button being pressed, and re-asking the library gets that operator the
    PC-336 sentence instead of audio from an engine that was told to stop.
    """

    text: str
    engine_id: str
    voice_id: str
    owner: str
    pauses: tuple[tuple[str, int, int], ...] = ()
    """The speaking persona's words that take pauses of their own, kept with
    the reply for the same reason the text is: the audio is synthesised later,
    and it must be paced as the persona that answered — not as whichever
    persona is selected by the time the play button is pressed."""


class ReplySpeaker:
    """Turns a finished reply into audio a surface can fetch.

    One instance per application, shared across turns; the only mutable state
    is the small ring of pending replies, which is guarded by a lock because
    turns are concurrent and the audio for one is fetched on a request of its
    own.

    Both methods are ordinary blocking calls — the library reads the filesystem
    and an engine synthesises on the CPU — so an async caller runs them in a
    worker thread, the same way the test-speak route does.
    """

    def __init__(
        self,
        library: Any,
        personas: Any,
        *,
        url_prefix: str = AUDIO_PATH_PREFIX,
        max_pending: int = MAX_PENDING_REPLIES,
        max_chars: int = MAX_SPOKEN_CHARS,
    ) -> None:
        self._library = library
        self._personas = personas
        self._prefix = url_prefix.rstrip("/")
        self._max_pending = max(1, max_pending)
        self._max_chars = max_chars
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._lock = threading.Lock()

    # -- the reply path ----------------------------------------------------

    def offer(self, reply: str, *, persona: str | None = None, owner: str = "") -> ReplySpeech:
        """What this reply can be heard as. **Never raises, never synthesises.**

        Called on the reply path, where the answer is already composed and is
        waiting on nothing but this. It resolves the persona's voice, keeps the
        speakable text against a handle, and returns; whether an engine can
        actually produce the audio is not asked until something fetches it.

        Every failure — no library, an unloadable persona, a library that
        raises where its contract says it does not — comes back as a reply with
        no audio. Speech is an addition to the answer and may not become a
        condition of it.
        """
        try:
            return self._offer(reply, persona, owner)
        except Exception as exc:  # noqa: BLE001 - speech may never cost the reply
            log.warning("reply_speech_offer_failed", error=repr(exc))
            return ReplySpeech(can_speak=False, reason=SPEECH_UNAVAILABLE)

    def _offer(self, reply: str, persona: str | None, owner: str) -> ReplySpeech:
        text = speakable_text(reply, limit=self._max_chars)
        if not text:
            # An empty or markup-only reply. There is nothing to say and
            # nothing to explain.
            return ReplySpeech()
        if self._library is None:
            return ReplySpeech(can_speak=False, reason=SPEECH_UNAVAILABLE)

        engine_id, voice_id, pauses = self._voice_of(persona)
        resolution = self._library.resolve(engine_id, voice_id)
        label = resolution.voice.label if resolution.voice is not None else ""
        if not resolution.can_speak:
            return ReplySpeech(
                can_speak=False,
                # A persona that names no voice is silent on purpose (ADR-0029
                # §6 read forwards: voice is detachable), so it gets no
                # sentence. Everything else gets the library's.
                reason=(
                    None
                    if resolution.state is VoiceState.NO_VOICE_CHOSEN
                    else resolution.reason
                ),
                voice_label=label,
            )

        handle = self._keep(
            _Pending(
                text=text,
                engine_id=resolution.engine_id,
                voice_id=resolution.voice.id if resolution.voice else "",
                owner=owner,
                pauses=pauses,
            )
        )
        return ReplySpeech(
            can_speak=True,
            audio_url=f"{self._prefix}/{handle}.wav",
            voice_label=label,
        )

    def resolve(self, persona: str | None) -> tuple[Any, tuple[tuple[str, int, int], ...]]:
        """This persona's voice as the library sees it, and its speech pauses.

        The one thing a *streaming* reply needs that :meth:`offer` cannot give
        it: whether there will be audio has to be decided before the reply
        exists, so the resolution is asked for on its own rather than as a
        side-effect of keeping a finished text.

        It is here rather than in :mod:`personacore.voice.live` so that both
        paths ask the same question of the same library in the same order — a
        second copy of "which voice does this persona have" is how the streamed
        reply ends up spoken by somebody else. Raises nothing that
        :meth:`offer` would not: a persona that will not load is no voice, and
        the caller reads :attr:`~personacore.voice.library.VoiceResolution.can_speak`.
        """
        if self._library is None:
            return None, ()
        engine_id, voice_id, pauses = self._voice_of(persona)
        return self._library.resolve(engine_id, voice_id), pauses

    def _voice_of(
        self, persona: str | None
    ) -> tuple[str | None, str | None, tuple[tuple[str, int, int], ...]]:
        """The persona's chosen voice and its speech pauses, or nothing at all.

        A persona that will not load is not this module's problem to report —
        the agent loop has already said so, in the reply itself — so it is
        logged and treated as "no voice".

        The pauses are read defensively for the same reason every other field
        here is: this object is a persona store by duck typing, and a store
        that answers with something older or simpler than the current
        :class:`~personacore.agent.personas.Persona` costs the pacing, never
        the reply.
        """
        try:
            loaded = self._personas.load(persona)
        except Exception as exc:  # noqa: BLE001 - the loop already said this out loud
            log.info("reply_speech_persona_unavailable", error=repr(exc))
            return None, None, ()
        return (
            getattr(loaded, "voice_engine", None),
            getattr(loaded, "voice_name", None),
            _pauses_of(loaded),
        )

    def _keep(self, pending: _Pending) -> str:
        """Store one pending reply under a fresh handle, oldest out first.

        The handle is a random token rather than a counter or the reply's hash:
        it is what stands between one operator's reply and another's, so it
        must not be guessable or derivable from anything on the screen.
        """
        handle = secrets.token_urlsafe(16)
        with self._lock:
            self._pending[handle] = pending
            while len(self._pending) > self._max_pending:
                self._pending.popitem(last=False)
        return handle

    # -- the audio path ----------------------------------------------------

    def request(self, handle: str, *, owner: str = "") -> SpeechRequest:
        """What a *different* synthesiser would need to speak this reply.

        Added for the Wyoming client, which sends the words over a socket to
        this core's own speech server instead of calling the engine here. It
        answers the same two questions :meth:`audio` answers before it
        synthesises — is this handle this operator's, and can that voice still
        speak — and raises the same :class:`EngineError` with the same wording
        when either is no. Two paths to the same audio must not have two
        different ideas of who may fetch it.

        What it does **not** carry is the persona's speech pauses. They are
        timing applied by :func:`~personacore.voice.pacing.paced_words` inside
        an engine call, and the Wyoming protocol has no event that could
        express them — a reply spoken over that path is paced by the server's
        own sentence gaps and nothing else. Worth knowing when the two paths
        are compared by ear.
        """
        pending, resolution = self._speakable(handle, owner)
        return SpeechRequest(
            text=pending.text,
            engine_id=resolution.engine_id or pending.engine_id,
            voice_id=(resolution.voice.id if resolution.voice else pending.voice_id),
            voice_label=resolution.voice.label if resolution.voice else "",
        )

    def _speakable(self, handle: str, owner: str) -> tuple[_Pending, Any]:
        """One pending reply and its voice, or the operator's sentence.

        The whole of the "may this be spoken, and by what" decision, in one
        place because two callers now make it — :meth:`audio` and
        :meth:`request` — and a second copy is how one of them ends up laxer
        than the other.
        """
        with self._lock:
            pending = self._pending.get(handle)
        if pending is None or pending.owner != owner:
            # One sentence for both, so a guessed handle cannot be told from an
            # expired one. Which it was is in the log, not in the answer.
            log.info(
                "reply_audio_refused",
                known=pending is not None,
                mine=bool(pending is not None and pending.owner == owner),
            )
            raise EngineError(REPLY_GONE)

        resolution = self._library.resolve(pending.engine_id, pending.voice_id)
        if not resolution.can_speak:
            raise EngineError(resolution.reason or REPLY_GONE)
        return pending, resolution

    def audio(self, handle: str, *, owner: str = "") -> Audio:
        """The audio for one earlier reply, synthesised now.

        Raises :class:`~personacore.voice.engine.EngineError` — carrying a
        sentence written for an operator — for a handle that is not this
        operator's, for a voice that has stopped being speakable since the
        reply, and for an engine that fails. Every one of those costs the
        audio only: the reply it belongs to was delivered before this was ever
        called.
        """
        pending, resolution = self._speakable(handle, owner)
        try:
            # The text is exactly what was kept at reply time. The pauses only
            # decide where it is cut and how long the silences are.
            return resolution.speak(pending.text, pauses=pending.pauses)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 - an engine's failure is a sentence
            # Deliberately not the exception's own text: this is whatever an
            # engine raised outside its contract, and it goes to the log rather
            # than to a screen it was never written for.
            log.warning(
                "reply_audio_failed",
                engine=pending.engine_id,
                error=repr(exc),
            )
            raise EngineError(
                SYNTHESIS_FAILED.format(engine=resolution.engine_display or pending.engine_id)
            ) from exc


__all__ = [
    "AUDIO_PATH_PREFIX",
    "MAX_PENDING_REPLIES",
    "MAX_SPOKEN_CHARS",
    "REPLY_GONE",
    "SPEAKER_ATTRIBUTE",
    "SPEECH_UNAVAILABLE",
    "SYNTHESIS_FAILED",
    "ReplySpeaker",
    "ReplySpeech",
    "SpeechRequest",
    "speakable_text",
]
