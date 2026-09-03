"""One connection, and the two conversations it might be.

Home Assistant opens a **new TCP connection for every operation**, so a handler
is short-lived and holds no state anybody else needs. Three shapes arrive here:

*Discovery*, on a connection of its own, repeated every thirty seconds forever,
with a five-second timeout and three retries::

    HA -> describe
    HA <- info

*Transcription*, on a fresh connection where ``describe`` is never sent — so a
server that waits for a handshake before accepting ``transcribe`` never
transcribes anything::

    HA -> transcribe   {"language": "en"}
    HA -> audio-start  {"rate": 16000, "width": 2, "channels": 1}
    HA -> audio-chunk  {...} + raw PCM                            (xN)
    HA -> audio-stop
    HA <- transcript   {"text": "..."}        or: error

*Synthesis*, the whole reply at once::

    HA -> synthesize   {"text": "...", "voice": {"name": ...}}
    HA <- audio-start  {rate, width, channels}
    HA <- audio-chunk  {rate, width, channels} + PCM              (xN)
    HA <- audio-stop                            <- the terminator

Two things about that last one are easy to get wrong and impossible to see:
Home Assistant builds its WAV header from the **first ``audio-chunk``**, not
from ``audio-start``, so the real values go in both; and ``audio-stop`` is what
ends the response, so a synthesis that returns without it hangs the pipeline.

*Synthesis, streamed* — the same speech, started before the text has finished
arriving, so a listener hears the first sentence while a model is still writing
the last one::

    HA -> synthesize-start   {"voice": {...}}      <- no text on this one
    HA -> synthesize-chunk   {"text": "..."}                      (xN)
    HA <- audio-start / audio-chunk (xN) / audio-stop   per flush, as ready
    HA -> synthesize         {"text": "<the whole message again>"}
    HA -> synthesize-stop
    HA <- audio-start / audio-chunk (xN) / audio-stop   the last sentence
    HA <- synthesize-stopped                    <- the terminator, here

Three of those are traps, and each one is silent rather than loud:

* **That replayed ``synthesize`` is required of the client** by the protocol
  ("Original ``synthesize`` message must be sent for backwards compatibility"),
  not a Home Assistant quirk. Every compliant client sends it, and the server
  is what resolves it — by ignoring it while a stream is open. A server that
  answered it too would say every reply twice.
* **``audio-stop`` is not the terminator here.** In its streaming read loop
  Home Assistant never tests for ``audio-stop`` at all; the only ``break`` is
  on ``synthesize-stopped``. Send the last audio and stop there and the client
  waits on the socket for ever, with no error, no timeout and nothing logged.
* **Only the first ``audio-start`` writes a header**, and every later group's
  samples are concatenated underneath it. So every group of one utterance has
  to carry the same rate, width and channels — one that differs plays back
  wrong instead of failing.

All three, and where each was read, are in ``docs/research/wyoming-streaming-tts.md``.

Nothing here blocks the event loop. Every piece of inference runs in a worker
thread, because a loop stalled inside a transcription cannot answer the
``describe`` that arrives during it, and a service that misses ``describe`` is
marked unavailable while it is working perfectly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import structlog
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.server import AsyncEventHandler
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

from personacore.hearing import HearingError

# Core's own splitter, and core's own silence. Not a second one for streaming:
# ADR-0029 put splitting and pacing in core precisely so that every caller
# breaks a reply in the same places, and named this — streaming — as the second
# caller. Two splitters would mean the streamed reading and the whole-reply
# reading differ in where they breathe, which is PC-257's named failure.
from personacore.voice.pacing import complete_sentences, silence
from personacore.wyoming.seams import (
    CAPTURE_CHANNELS,
    CAPTURE_RATE,
    CAPTURE_WIDTH,
    MINIMUM_SECONDS,
    HearingSource,
    VoiceSource,
    captured,
    current_recogniser,
)

log = structlog.get_logger(__name__)

CHUNK_BYTES = 2048
"""How much synthesised audio goes in one ``audio-chunk``. Bytes, not samples."""

_WIDTHS = {"pcm_s8": 1, "pcm_s16le": 2, "pcm_s24le": 3, "pcm_s32le": 4}

NO_HEARING = (
    "No recogniser is switched on in this PersonaCore, so it cannot turn audio "
    "into words. Turn one on from the admin UI."
)
NO_SPEECH = "This PersonaCore has no voice installed, so it cannot speak."
HEARING_FAILED = "PersonaCore could not transcribe that audio. Check the core's logs."
SPEECH_FAILED = "PersonaCore could not speak that text. Check the core's logs."
"""Plain English, and deliberately without detail.

This port carries no credential (see :mod:`personacore.config.wyoming`), so
whoever is on it is not owed a stack trace, a file path or a model name. The
detail goes to the log, where the operator is. The one exception is a voice
resolution's own ``reason`` and a :class:`~personacore.hearing.HearingError`,
both of which are written for an operator by contract, say something they can
act on, and name nothing internal.
"""


@dataclass
class _Stream:
    """One ``synthesize-start`` … ``synthesize-stop``, and what it has heard.

    Its existence is the protocol's one required piece of connection state:
    **a ``synthesize`` arriving while this is not ``None`` is the compatibility
    replay and must be dropped.** That check, and nothing else, is what stops a
    compliant client hearing every reply twice.
    """

    resolution: Any = None
    """The voice, resolved once when the stream opened rather than per sentence.

    ``synthesize-start`` is the only event of a stream that carries a voice, so
    once is also all the protocol offers — and resolving reads the voice's
    folder, which is not something to do again between two sentences of one
    reply.
    """
    text: str = ""
    """Everything ``synthesize-chunk`` has delivered and no sentence has taken
    yet. Never logged: it is what the household is about to be told."""
    format: tuple[int, int, int] | None = None
    """``(rate, width, channels)`` of the first audio group, which is the
    format of every later one — see the module docstring."""
    groups: int = 0
    spoken: int = 0
    """Counters for one log line at the end. Counts, never text."""
    failed: bool = False
    """Something already answered ``error``. The stream stops synthesising and
    still ends properly, because a client that ignored the error is otherwise
    left reading a socket that will never say anything again."""


def _width_of(encoding: str) -> int:
    """Bytes per sample for an engine's encoding. Defaults to 16-bit."""
    return _WIDTHS.get(encoding, CAPTURE_WIDTH)


def _slices(data: bytes, size: int) -> Iterator[bytes]:
    """``data`` in chunks of ``size``, and **at least one chunk even if empty**.

    Home Assistant reads the audio format off the first chunk, so a response
    with no chunk at all leaves it with nothing to build a WAV header from.
    """
    if not data:
        yield b""
        return
    for start in range(0, len(data), size):
        yield data[start : start + size]


def split_voice_name(name: str | None) -> tuple[str | None, str | None]:
    """``vits-onnx/glados`` -> ``("vits-onnx", "glados")``.

    That is :attr:`personacore.voice.library.LibraryEntry.key`, which is what
    this server advertises, so it is what comes back. A bare name with no slash
    is taken as a voice id with the engine unknown, and looked up.
    """
    if not name:
        return None, None
    engine_id, slash, voice_id = name.partition("/")
    if not slash:
        return None, engine_id
    return engine_id or None, voice_id or None


class PersonaCoreEventHandler(AsyncEventHandler):
    """A Wyoming connection, answered from this core's hearing and voices.

    Both halves are optional, and each may go away while the core runs —
    a recogniser and a voice engine are both switches. A core with nothing
    listening never advertised speech-to-text and answers ``error`` if asked
    anyway; the same for voices. Neither absence may take the other half down.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        info_factory: Callable[[], Info],
        hearing: HearingSource | None = None,
        voices: VoiceSource | None = None,
        hearing_lock: asyncio.Lock | None = None,
        speech_lock: asyncio.Lock | None = None,
    ) -> None:
        super().__init__(reader, writer)
        # Called per `describe` rather than captured once, so a voice installed
        # while the core runs is advertised without a restart. It is cheap by
        # construction: see `describe.tts_program`.
        self._info_factory = info_factory
        self._hearing = hearing
        self._voices = voices
        # Shared across every connection, because concurrency here is
        # per-connection with no limit and no shared state of the protocol's
        # own: two Home Assistant pipelines can transcribe at the same moment.
        # The models underneath are not promised to be re-entrant, so they are
        # serialised — separately, so that speaking does not queue behind
        # hearing or the other way about.
        self._hearing_lock = hearing_lock or asyncio.Lock()
        self._speech_lock = speech_lock or asyncio.Lock()
        # Per connection: the converter carries resampling state between
        # chunks, and two utterances must not share it.
        self._converter = AudioChunkConverter(
            rate=CAPTURE_RATE, width=CAPTURE_WIDTH, channels=CAPTURE_CHANNELS
        )
        self._captured: list[bytes] = []
        self._language: str | None = None
        # Per connection, like everything else here. `None` means no stream is
        # open, which is also how a real `synthesize` is told apart from the
        # compatibility replay inside one.
        self._stream: _Stream | None = None
        # Logged here rather than left to the surrounding server: this is a
        # new TCP connection every time (see the module docstring), so this is
        # the only place that ever sees one arrive. It answers the first
        # question in any silent-failure report — did the client reach this
        # port at all — before anything about hearing or speech applies.
        log.info("wyoming_client_connected", peer=self._peer())

    def _peer(self) -> str | None:
        """The client's address, for a log line. Never anything it said."""
        peer = self.writer.get_extra_info("peername")
        return str(peer) if peer is not None else None

    async def disconnect(self) -> None:
        """Called by the base class when the client goes away, for any reason."""
        log.info("wyoming_client_disconnected", peer=self._peer())

    # -- the protocol -----------------------------------------------------

    async def handle_event(self, event: Event) -> bool:
        """Returning ``True`` keeps the connection; ``False`` disconnects.

        **Everything returns ``True``, including events this server has never
        heard of.** The protocol's forward compatibility is exactly that: a
        client may send something newer than the server, and a server that
        hangs up on it breaks a pipeline over an event it could have ignored.
        """
        if Describe.is_type(event.type):
            # This path may not read a disk, take a lock, or wait on a model:
            # `describe` arrives every thirty seconds with a five-second
            # timeout, and missing it is what marks the service unavailable.
            await self.write_event(self._info_factory().event())
            return True

        if Transcribe.is_type(event.type):
            # Always the first event of a transcription, and the only place the
            # requested language appears. Nothing else about it is needed:
            # which recogniser answers is the hearing registry's business.
            self._language = Transcribe.from_event(event).language
            self._captured = []
            log.info("wyoming_transcribe_requested", language=self._language)
            return True

        if AudioStart.is_type(event.type):
            self._captured = []
            start = AudioStart.from_event(event)
            log.info(
                "wyoming_audio_start",
                rate=start.rate,
                width=start.width,
                channels=start.channels,
            )
            return True

        if AudioChunk.is_type(event.type):
            # Home Assistant hardcodes 16 kHz 16-bit mono and resamples
            # upstream, so this converter is a no-op for it. Other Wyoming
            # clients are not bound by that, and refusing them for a format
            # this can trivially convert would be a server bug, not a client
            # one. `wyoming` ships a `pyaudioop` fallback, so Python 3.13
            # dropping `audioop` from the stdlib is already handled.
            chunk = self._converter.convert(AudioChunk.from_event(event))
            self._captured.append(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            await self._finish_transcription()
            return True

        if Synthesize.is_type(event.type):
            if self._stream is not None:
                # The replay. Every compliant client sends the whole message
                # again inside a stream — the protocol requires it of them, so
                # that a server which only ever understood `synthesize` still
                # gets a complete request — and the server is what makes it
                # harmless. Answering it here is how a reply gets said twice.
                log.info("wyoming_synthesize_replay_ignored")
                return True
            await self._synthesise(Synthesize.from_event(event))
            return True

        if SynthesizeStart.is_type(event.type):
            await self._open_stream(SynthesizeStart.from_event(event))
            return True

        if SynthesizeChunk.is_type(event.type):
            await self._stream_text(SynthesizeChunk.from_event(event))
            return True

        if SynthesizeStop.is_type(event.type):
            await self._close_stream()
            return True

        return True

    # -- speech to text ---------------------------------------------------

    async def _finish_transcription(self) -> None:
        audio = captured(b"".join(self._captured))
        self._captured = []
        log.info(
            "wyoming_audio_captured", bytes=len(audio.data), seconds=audio.seconds
        )

        if audio.seconds < MINIMUM_SECONDS:
            # Home Assistant sends `audio-stop` with no preceding chunk when
            # its voice-activity detection heard nothing, and a fragment when
            # it fired on a door closing. Both are ordinary outcomes of a
            # pipeline rather than failures, and an `error` here would surface
            # to somebody who simply did not speak. The threshold is also the
            # floor the first engine refuses below, so this is the caller half
            # of a bound the engine enforces.
            log.warning(
                "wyoming_transcribe_too_short",
                seconds=audio.seconds,
                minimum_seconds=MINIMUM_SECONDS,
            )
            await self.write_event(Transcript(text="").event())
            return

        recogniser = current_recogniser(self._hearing)
        if recogniser is None:
            await self._error(NO_HEARING, "no-recogniser")
            return

        try:
            async with self._hearing_lock:
                started = time.monotonic()
                result = await asyncio.to_thread(self._transcribe, recogniser, audio)
                elapsed = time.monotonic() - started
        except HearingError as exc:
            # By contract this text is written for an operator and names
            # nothing internal, so it is one of the two failures whose own
            # words go over the wire.
            log.info("wyoming_transcribe_refused", error=str(exc))
            await self._error(str(exc), "transcribe-failed")
            return
        except Exception as exc:
            # Never silence. Home Assistant checks for `error` before anything
            # else and will otherwise wait for this socket to close.
            log.warning("wyoming_transcribe_failed", error=repr(exc))
            await self._error(HEARING_FAILED, "transcribe-failed")
            return

        if result.text:
            log.info(
                "wyoming_transcribe_completed",
                elapsed_seconds=elapsed,
                characters=len(result.text),
            )
        else:
            # A real attempt that came back with nothing is a different fault
            # than never attempting one — see the module's own reason for
            # logging this path at all — so it gets its own event rather than
            # being indistinguishable from `wyoming_transcribe_completed`.
            log.warning("wyoming_transcribe_empty", elapsed_seconds=elapsed)

        await self.write_event(Transcript(text=result.text).event())

    def _transcribe(self, recogniser: Any, audio: Any) -> Any:
        """The blocking call, in a worker thread.

        ``language`` is passed only when the client asked for one. Every
        recogniser takes ``**knobs``, so naming it is safe; passing ``None``
        would be telling an engine "the language is unknown" in a keyword that
        exists to say the opposite.
        """
        if self._language:
            return recogniser.transcribe(audio, language=self._language)
        return recogniser.transcribe(audio)

    # -- text to speech ---------------------------------------------------

    async def _synthesise(self, request: Synthesize) -> None:
        """The whole reply, in one go. The path a client that knows nothing
        about streaming takes, and it is untouched by streaming existing."""
        started = time.perf_counter()
        resolution = await self._resolve_voice(request.voice)
        if resolution is None:
            return
        audio = await self._speak(resolution, request.text)
        if audio is None:
            return
        # A success here used to log nothing at all, so grepping the log for
        # synthesis found only failures — and an empty result read as "nobody
        # asked us to speak" when it meant "nothing went wrong". That false
        # reading once led to changing a setting that was already correct.
        log.info(
            "wyoming_synthesize_completed",
            elapsed_seconds=round(time.perf_counter() - started, 3),
            characters=len(request.text or ""),
        )
        # Read by attribute for the same reason `personacore.voice.wav_bytes`
        # does: an engine returns its own object and only these fields are ever
        # asked of it.
        await self._write_audio(
            getattr(audio, "data", b"") or b"",
            rate=int(getattr(audio, "sample_rate", CAPTURE_RATE) or CAPTURE_RATE),
            width=_width_of(getattr(audio, "encoding", "pcm_s16le")),
            channels=int(getattr(audio, "channels", 1) or 1),
        )

    async def _resolve_voice(self, requested: Any) -> Any | None:
        """The voice to speak in, or ``None`` — with the ``error`` already sent."""
        if self._voices is None:
            await self._error(NO_SPEECH, "no-voices")
            return None

        engine_id, voice_id = split_voice_name(
            requested.name if requested is not None else None
        )
        if voice_id is None:
            engine_id, voice_id = self._default_voice()
        elif engine_id is None:
            engine_id = self._engine_of(voice_id)

        resolution = self._voices.resolve(engine_id, voice_id)
        if not getattr(resolution, "can_speak", False):
            # `reason` is written for an operator and names nothing internal,
            # so it is the one failure whose own words go over the wire.
            await self._error(getattr(resolution, "reason", None) or NO_SPEECH, "voice-unavailable")
            return None
        return resolution

    async def _speak(self, resolution: Any, text: str) -> Any | None:
        """One synthesis, in a worker thread, or ``None`` with ``error`` sent.

        Every call takes the speech lock separately rather than one lock for a
        whole streamed reply: the models underneath are not promised to be
        re-entrant, but holding it across a stream would make one slow client
        the only client this core can speak for.
        """
        try:
            async with self._speech_lock:
                return await asyncio.to_thread(resolution.speak, text)
        except Exception as exc:
            log.warning("wyoming_synthesize_failed", error=repr(exc))
            await self._error(SPEECH_FAILED, "synthesize-failed")
            return None

    async def _write_audio(self, data: bytes, *, rate: int, width: int, channels: int) -> None:
        """One complete audio group: ``audio-start``, chunks, ``audio-stop``."""
        await self.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
        for piece in _slices(data, CHUNK_BYTES):
            # The same rate/width/channels on every chunk, because the header
            # Home Assistant writes comes from the first one rather than from
            # `audio-start`.
            await self.write_event(
                AudioChunk(rate=rate, width=width, channels=channels, audio=piece).event()
            )
        await self.write_event(AudioStop().event())

    # -- text to speech, a sentence at a time ------------------------------

    async def _open_stream(self, request: SynthesizeStart) -> None:
        """``synthesize-start``: the voice for the whole stream, and no text.

        The voice is resolved now rather than at the first sentence so that a
        core with nothing installed says so immediately instead of after the
        client has finished writing a paragraph it will never hear.
        """
        self._stream = stream = _Stream()
        log.info(
            "wyoming_synthesize_stream_started",
            voice=request.voice.name if request.voice is not None else None,
        )
        stream.resolution = await self._resolve_voice(request.voice)
        # The stream still opens on a failure, and deliberately: the client is
        # going to send the compatibility `synthesize` regardless, and without
        # a stream to be inside it would be answered as a fresh request and
        # produce a second `error` — or worse, speech, after the refusal.
        stream.failed = stream.resolution is None

    async def _stream_text(self, request: SynthesizeChunk) -> None:
        """``synthesize-chunk``: text as it is written, spoken as it finishes.

        Everything complete goes to the engine **now**. Waiting until
        ``synthesize-stop`` would be a legal implementation of the events and
        no implementation at all of the feature: the point is that speech
        starts before the writing does.
        """
        stream = self._stream
        if stream is None:
            # A chunk with no stream open. Ignored rather than answered — see
            # `handle_event`: this server never hangs up over an event, and a
            # client that is out of order gets no audio, which it will notice.
            log.warning("wyoming_synthesize_chunk_outside_stream")
            return
        stream.text += request.text
        if stream.failed:
            return
        pacing = getattr(stream.resolution, "pacing", None)
        cut = complete_sentences(stream.text, pacing)
        if not cut:
            return
        finished, stream.text = stream.text[:cut], stream.text[cut:]
        await self._speak_group(stream, finished, last=False)

    async def _close_stream(self) -> None:
        """``synthesize-stop``: the last sentence, then the only terminator.

        **The remainder is almost always a whole sentence**, not a fragment: a
        full stop is only a boundary once something follows it, so the last
        sentence of a reply has nothing to release it but this. Forget the
        flush and every reply loses its ending.
        """
        stream = self._stream
        # Cleared before the flush, so a stream always closes — including one
        # whose final synthesis raises. This is where this server parts company
        # with the reference implementation, which sets its streaming flag and
        # never clears it: on a connection that streamed once, every later
        # plain `synthesize` is silently ignored for the life of the socket.
        # Home Assistant never notices because it reconnects per utterance. A
        # client that keeps a connection would be met with silence, and the
        # protocol says a `synthesize` outside a stream is to be answered.
        self._stream = None
        if stream is not None:
            await self._speak_group(stream, stream.text, last=True)
            log.info(
                "wyoming_synthesize_stream_finished",
                groups=stream.groups,
                characters=stream.spoken,
                failed=stream.failed,
            )
        # Always, even for a stream that failed or was never opened. In
        # streaming mode this event, and not `audio-stop`, is what ends the
        # client's read loop; without it the client waits on the socket with
        # no error and no timeout.
        await self.write_event(SynthesizeStopped().event())

    async def _speak_group(self, stream: _Stream, text: str, *, last: bool) -> None:
        """One piece of a streamed reply, as its own audio group."""
        body = text.strip()
        if stream.failed or not body:
            return
        audio = await self._speak(stream.resolution, body)
        if audio is None:
            stream.failed = True
            return

        data = getattr(audio, "data", b"") or b""
        rate = int(getattr(audio, "sample_rate", CAPTURE_RATE) or CAPTURE_RATE)
        channels = int(getattr(audio, "channels", 1) or 1)
        encoding = str(getattr(audio, "encoding", "pcm_s16le") or "pcm_s16le")
        width = _width_of(encoding)

        gap = 0 if last else int(getattr(getattr(stream.resolution, "pacing", None), "sentence", 0))
        if gap > 0:
            # The pause after a full stop, which `speak_paced` would have put
            # between these two sentences had it seen them in one call. Without
            # it a streamed reply runs its sentences together — which is
            # exactly the fault PC-342 exists to fix, wearing a new hat. None
            # after the last one: nothing follows it to be paced away from.
            data += silence(gap, sample_rate=rate, channels=channels, encoding=encoding)

        if stream.format is None:
            stream.format = (rate, width, channels)
        elif stream.format != (rate, width, channels):
            # Only the first `audio-start` of an utterance writes the client's
            # WAV header; every later group's samples are appended underneath
            # it. So a group in another format does not fail, it plays back at
            # the wrong speed. Converted rather than refused, with the same
            # converter the hearing path uses — this needs one voice to change
            # its own output rate mid-reply, which no engine here does, and a
            # silently wrong reply is not the way to find out that one has.
            rate, width, channels = stream.format
            log.warning(
                "wyoming_synthesize_format_changed", rate=rate, width=width, channels=channels
            )
            data = (
                AudioChunkConverter(rate=rate, width=width, channels=channels)
                .convert(
                    AudioChunk(
                        rate=int(getattr(audio, "sample_rate", rate) or rate),
                        width=_width_of(encoding),
                        channels=int(getattr(audio, "channels", channels) or channels),
                        audio=data,
                    )
                )
                .audio
            )

        stream.groups += 1
        stream.spoken += len(body)
        log.info(
            "wyoming_synthesize_stream_audio",
            group=stream.groups,
            characters=len(body),
            bytes=len(data),
            rate=rate,
            width=width,
            channels=channels,
            last=last,
        )
        await self._write_audio(data, rate=rate, width=width, channels=channels)

    # -- choosing a voice -------------------------------------------------

    def _speakable(self) -> tuple[Any, ...]:
        if self._voices is None:
            return ()
        try:
            return tuple(self._voices.snapshot().speakable())
        except Exception as exc:
            log.warning("wyoming_voice_snapshot_failed", error=repr(exc))
            return ()

    def _default_voice(self) -> tuple[str | None, str | None]:
        """What to speak in when the client named no voice.

        The first speakable voice in the core's own order, which is the same
        order the Voices screen shows — so "the default" is a thing an operator
        can look at rather than an accident of a dictionary.
        """
        for entry in self._speakable():
            return entry.engine_id, entry.voice.id
        return None, None

    def _engine_of(self, voice_id: str) -> str | None:
        """The engine for a bare voice name, for clients that send one."""
        for entry in self._speakable():
            if entry.voice.id == voice_id:
                return entry.engine_id
        return None

    # -- failure ----------------------------------------------------------

    async def _error(self, text: str, code: str) -> None:
        await self.write_event(Error(text=text, code=code).event())
