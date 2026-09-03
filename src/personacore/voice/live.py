"""A reply spoken while it is still being written — the other half of PC-252.

:mod:`personacore.voice.reply` speaks a reply that has *finished*. Everything
about it is right for that case and wrong for this one: it is handed the whole
text, it resolves a voice once, and the audio is fetched later. The owner
asked for the case underneath it — audio beginning to play while the text was
still generating — and the only thing that makes that possible is
that :func:`~personacore.voice.pacing.complete_sentences` can say how much of a
growing buffer has *certainly* ended. ADR-0029 named this module's caller
before this module existed.

**The turn never waits for the engine.** :meth:`LiveSpeech.add` puts text on a
queue and returns; nothing in it touches an engine, a thread or a socket. The
synthesis happens on the browser's own request for the audio, which is a
different request on a different task, so an engine that is slow, stopped or
broken costs the audio and cannot reach into the turn producing the text. That
is the same property :mod:`~personacore.voice.reply` has, kept by the same
means — the reply was already on its way before anything was asked to speak.

**One splitter, one reading.** The pieces handed to the engine come from
:func:`~personacore.voice.pacing.split_paced`, the same function the finished
path uses, so a sentence is cut in the same place whether it was spoken as it
arrived or replayed afterwards (PC-257). The gaps between pieces are core's
too, for the reason ADR-0029 gives: an engine is handed one piece of text and
never reads punctuation as timing.

**The gap goes in front of a piece, not behind it.** Which is not fussiness: a
gap written after a piece has to be written before anything knows whether
another piece follows, and a reply that ended with half a second of silence
would sound like a voice that had stalled rather than finished.

Counts, timings and a format. **Never the text and never the audio** — the
convention commit ``57b5365`` set for every speech surface here, and the reason
:class:`LiveSpeech` has a ``pieces`` count and no transcript.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog

from personacore.voice.pacing import (
    Break,
    Pacing,
    PausedWord,
    complete_sentences,
    sample_width,
    silence,
    split_paced,
)

log = structlog.get_logger(__name__)

IDLE_TIMEOUT = 120.0
"""How long the audio may wait for the next piece of a reply that is still
being written.

A bound rather than a guess at how slow a model may be: this is per piece, and
a turn that has produced nothing for two minutes is not slow, it is a turn
whose producer has gone away without saying so. Without it, a browser holding
this stream open would wait for ever on a queue nobody is going to fill —
which is the leak this whole module has to not have.
"""

MAX_QUEUED_PIECES = 512
"""Pieces that may be waiting to be spoken at once.

The text is already capped by the caller's own character limit, so this is the
second fence rather than the first: it stops a reply made entirely of one-word
sentences from turning into an unbounded list while nobody is listening.
"""


class LiveSpeech:
    """One reply being synthesised as it is written.

    Two sides, deliberately, and they run on different requests:

    *The turn* calls :meth:`add` with each stretch of text that has certainly
    ended, and :meth:`close` with whatever is left when the reply finishes.
    Both are ordinary function calls that put something on a queue.

    *The browser's audio request* calls :meth:`start` once — which waits for
    the first piece, speaks it, and is where the sample format becomes known —
    and then iterates :meth:`audio` for the rest.

    Shaped after :class:`personacore.wyoming.client.WyomingSpeech` on purpose.
    The two-step form is not style: a WAV header needs a rate, a width and a
    channel count, and nothing knows those until a voice has actually said
    something.
    """

    def __init__(
        self,
        resolution: Any,
        *,
        pacing: Pacing | None = None,
        pauses: Sequence[PausedWord] = (),
        voice_label: str = "",
        audio_url: str = "",
        max_chars: int = 4000,
        started: float | None = None,
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        self._resolution = resolution
        # The voice's own gaps, off the resolution when it carries them, so a
        # caller does not have to read the voice folder a second time.
        self._pacing = pacing or getattr(resolution, "pacing", None) or Pacing()
        self._pauses = tuple(pauses)
        self._max_chars = max(0, int(max_chars))
        self._idle_timeout = idle_timeout
        #: Measured from the moment the *turn* began, not from the moment this
        #: object was made: "time to first audio" is a question about the turn.
        self._started = started if started is not None else time.monotonic()

        self.voice_label = voice_label
        self.audio_url = audio_url

        self._queue: asyncio.Queue[tuple[str, int] | None] = asyncio.Queue()
        self._gap_after = 0
        self._chars = 0
        self._closed = False
        self._taken = False
        self._first: bytes = b""
        self._encoding = "pcm_s16le"

        #: Counts and timings, and nothing that could reconstruct what was said.
        self.pieces = 0
        self.audio_bytes = 0
        self.first_audio_ms: float | None = None
        self.rate: int | None = None
        self.width: int | None = None
        self.channels: int | None = None
        self.failure: str | None = None
        self.capped = False
        """The reply outran :data:`max_chars` and the audio stops early. The
        text is untouched — only the speaking stops, at a piece boundary."""

    # -- the turn's side ---------------------------------------------------

    @property
    def pacing(self) -> Pacing:
        """This voice's gaps and, more to the point, its sentence marks.

        Exposed because the caller has to ask
        :func:`~personacore.voice.pacing.complete_sentences` the same question
        with the same marks: a voice that breaks at ``।`` and a caller that
        breaks at ``.`` would hand over half-sentences all day.
        """
        return self._pacing

    @property
    def closed(self) -> bool:
        return self._closed

    def add(self, text: str) -> None:
        """Speak this much of the reply. **Never blocks and never raises.**

        ``text`` is a stretch that has certainly ended — what
        :func:`~personacore.voice.pacing.complete_sentences` marked off — so
        the last piece in it is followed by a sentence gap rather than by
        nothing. A paragraph break that fell exactly on the boundary between
        two calls is heard as a sentence gap instead of a paragraph one: half a
        second either way, on a break the splitter can no longer see, and not
        worth holding a sentence back to find out.
        """
        self._push(text, final=False)

    def close(self, text: str = "") -> None:
        """The rest of the reply, and the end of it.

        The tail keeps whatever break the splitter gives it — which is
        :attr:`~personacore.voice.pacing.Break.NONE` for the last piece of a
        reply, so the audio stops when the words do.
        """
        if self._closed:
            return
        if text:
            self._push(text, final=True)
        self._closed = True
        self._queue.put_nowait(None)

    def abandon(self, reason: str | None = None) -> None:
        """The turn ended without finishing — the client went, the model died.

        Ends the audio where it is rather than leaving the stream waiting on a
        queue nobody will fill again.
        """
        if reason and self.failure is None:
            self.failure = reason
        self.close()

    def _push(self, text: str, *, final: bool) -> None:
        if self._closed or not text.strip():
            return
        pieces = split_paced(text, self._pacing, pauses=self._pauses)
        last = len(pieces) - 1
        for index, piece in enumerate(pieces):
            if self._chars >= self._max_chars:
                # Stopping between pieces rather than mid-word: a listener can
                # understand a reply that stopped being read out at the end of
                # a sentence, and cannot understand one cut off mid-syllable.
                self.capped = True
                break
            if self._queue.qsize() >= MAX_QUEUED_PIECES:
                self.capped = True
                break
            self._chars += len(piece.text)
            self._queue.put_nowait((piece.text, self._gap_after))
            after = (
                piece.gap_ms
                if piece.gap_ms is not None
                else self._pacing.gap_ms(piece.break_after)
            )
            if not final and index == last and piece.break_after is Break.NONE:
                # This chunk ends where a sentence ended — that is the only
                # reason it was handed over — so what follows it is a sentence
                # gap, not the nothing that ends a whole reply.
                after = self._pacing.gap_ms(Break.SENTENCE)
            self._gap_after = after

    # -- the browser's side ------------------------------------------------

    async def start(self) -> bool:
        """Wait for the first piece and speak it. ``True`` when there is audio.

        ``False`` means this reply produced nothing speakable at all — an empty
        reply, a reply that was only markup, a turn that failed before it said
        anything. The caller still has a status to answer with, because not one
        byte has been written yet; after this returns true there is no status
        left to change and a failure ends the audio instead.
        """
        if self._taken:
            # A live stream is consumed once. A second request for the same
            # handle is a reload or a second tab, and handing it the remainder
            # of somebody else's playback would be worse than refusing.
            return False
        self._taken = True
        while True:
            item = await self._next()
            if item is None:
                return False
            audio = await self._speak(item)
            if audio:
                self._first = audio
                return True
            if self.failure is not None:
                return False

    async def audio(self) -> AsyncIterator[bytes]:
        """Every sample of the reply, the first piece included, as it is made."""
        if self._first:
            yield self._first
            self._first = b""
        while True:
            item = await self._next()
            if item is None:
                return
            piece = await self._speak(item)
            if piece:
                yield piece
            elif self.failure is not None:
                return

    async def _next(self) -> tuple[str, int] | None:
        """The next piece, or ``None`` when the reply is over for any reason."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=self._idle_timeout)
        except TimeoutError:
            if self.failure is None:
                self.failure = "The reply stopped arriving before it finished being spoken."
            log.info("live_speech_idle", pieces=self.pieces, audio_bytes=self.audio_bytes)
            return None

    async def _speak(self, item: tuple[str, int]) -> bytes:
        """One piece, with the silence that goes in front of it.

        The engine call runs in a worker thread because it is CPU work in a C
        library, and this process is serving everything else while it happens.
        """
        text, gap_ms = item
        try:
            spoken = await asyncio.to_thread(self._resolution.speak, text)
        except Exception as exc:  # noqa: BLE001 - an engine's failure ends the audio, not the turn
            # Not the exception's own words: whatever an engine raised outside
            # its contract is written for a log, not for a screen.
            log.warning("live_speech_failed", error=repr(exc), pieces=self.pieces)
            if self.failure is None:
                self.failure = "The voice stopped part-way through this reply."
            return b""

        data = bytes(getattr(spoken, "data", b"") or b"")
        if not data:
            return b""
        self._note_format(spoken)
        lead = self._silence(gap_ms)
        self.pieces += 1
        self.audio_bytes += len(data)
        if self.first_audio_ms is None:
            self.first_audio_ms = (time.monotonic() - self._started) * 1000.0
        return lead + data

    def _note_format(self, spoken: Any) -> None:
        """The first piece's format frames the whole stream.

        One voice on one engine produces one rate — the assumption
        :func:`~personacore.voice.pacing.speak_paced` already makes for a
        finished reply, and the header has been written by the time a second
        piece exists, so there is nowhere to put a different answer.
        """
        if self.rate is not None:
            return
        self.rate = int(getattr(spoken, "sample_rate", 22050) or 22050)
        self.channels = int(getattr(spoken, "channels", 1) or 1)
        self.width = sample_width(str(getattr(spoken, "encoding", "pcm_s16le") or "pcm_s16le"))
        self._encoding = str(getattr(spoken, "encoding", "pcm_s16le") or "pcm_s16le")

    def _silence(self, milliseconds: int) -> bytes:
        if milliseconds <= 0 or self.rate is None:
            return b""
        return silence(
            milliseconds,
            sample_rate=self.rate,
            channels=self.channels or 1,
            encoding=self._encoding,
        )

    def as_log(self) -> dict[str, object]:
        """The run as log keywords. Counts, timings and a format only."""
        return {
            "path": "live",
            "pieces": self.pieces,
            "audio_bytes": self.audio_bytes,
            "first_audio_ms": self.first_audio_ms,
            "rate": self.rate,
            "capped": self.capped,
            "failed": self.failure is not None,
        }


def finished_prefix(buffer: str, spoken: int, pacing: Pacing | None = None) -> tuple[str, int]:
    """The next stretch of ``buffer`` that is safe to speak, and the new mark.

    The whole of what a streaming caller has to do with
    :func:`~personacore.voice.pacing.complete_sentences`, in one place so that
    every caller does it the same way: ``spoken`` is how much of the buffer has
    already been handed over, and what comes back is the slice after it that
    has certainly ended — empty when the sentence in progress has not finished.
    """
    end = complete_sentences(buffer, pacing)
    if end <= spoken:
        return "", spoken
    return buffer[spoken:end], end


__all__ = [
    "IDLE_TIMEOUT",
    "MAX_QUEUED_PIECES",
    "LiveSpeech",
    "finished_prefix",
]
