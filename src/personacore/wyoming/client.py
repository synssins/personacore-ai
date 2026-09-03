"""The other end of the socket — this core speaking to its own Wyoming server.

Everything else in this package answers Home Assistant. This module *asks*, and
it exists for one reason: until something drove the streaming synthesis over a
real socket, the only thing that had ever exercised it was the test suite
written by the author of the server. A server tested only by its own author has
never met a client.

It lives here, beside the handler, rather than under ``admin/``, because what it
knows is the protocol: which four events go up, in which order, which one comes
back, and — the trap that costs an afternoon — that ``audio-stop`` does **not**
end a streamed response. That knowledge belongs next to the code that has to
agree with it, so a change to one is read beside the other. What it knows about
*speech* is nothing: it is handed text and a voice name and hands back bytes.

The exchange it drives, which is the handler's docstring read from the far end::

    -> synthesize-start   {"voice": {"name": "engine/voice"}}   no text
    -> synthesize-chunk   {"text": "..."}                       (xN)
    <- audio-start / audio-chunk (xN) / audio-stop     per sentence, as ready
    -> synthesize         {"text": "<the whole message again>"}
    -> synthesize-stop
    <- audio-start / audio-chunk (xN) / audio-stop     the last sentence
    <- synthesize-stopped                              <- the terminator

**The host is a constant and there is no way to make it anything else.** Not a
parameter, not a setting, not a field on the request. This client is reached
from an admin page, and an admin-facing endpoint that will open a TCP
connection to a host somebody else names is a hole through which anybody who
reaches the admin UI can probe the household network. The *port* comes from the
running service, because that is a number an operator already chose and the
listener already bound; the address does not.

Nothing here logs or keeps the text or the audio. What comes back is
:class:`SpeechRun` — counts, timings and a format, which is the convention
commit ``57b5365`` set for this whole surface.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import structlog
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
    SynthesizeVoice,
)

log = structlog.get_logger(__name__)

LOOPBACK_HOST = "127.0.0.1"
"""Where this client connects, always.

A constant with no override anywhere above it. See the module docstring: the
only reason this endpoint is safe to expose on the admin surface is that the
address it dials cannot be influenced by the request that triggered it.

The consequence is worth stating rather than discovering: if an operator has
moved the listener onto one named interface — the box exists for a port clash
on a host-networked container — then loopback will not reach it and this client
reports that it could not connect. That is the correct outcome. Widening this
to "whatever ``[wyoming] host`` says" would accept ``host`` as an address to
dial, and ``host`` is an operator-editable setting.
"""

CHUNK_CHARS = 48
"""How much text goes in one ``synthesize-chunk``.

The reply is already finished by the time this runs, so the chunking is
simulated — but it must not be *one* chunk. A single chunk carrying a whole
reply is a legal streaming request that tests almost nothing: the server would
see every sentence boundary at once and the run would prove only that the
events parse. Cutting at roughly this many characters, on a space, lands
boundaries in the middle of sentences, which is where a model's tokens land and
where the server's buffer has to do its work.

Never mid-word: a chunk that split ``Dr.`` from ``Chapman`` would be testing
this function's cruelty rather than the server's boundary rule.
"""

CONNECT_TIMEOUT = 2.0
"""Long enough for a loopback connection and no longer.

Nothing is between here and there — same container, same loopback interface —
so a connection that has not completed in two seconds is not slow, it is a
listener that is not there. Waiting longer only delays saying so.
"""

READ_TIMEOUT = 60.0
"""How long one event may take to arrive.

Per event, not for the whole run: a long reply is many sentences and each takes
the engine as long as it takes. This is the bound that keeps a synthesis which
has silently died from holding a browser request open for ever — the failure
the handler's own docstring names, where a client waits on a socket with no
error and no timeout.
"""


class WyomingSpeechError(RuntimeError):
    """The synthesis did not happen over Wyoming, and this is why.

    The message is written for an operator looking at the admin UI. It names
    the port, because that is the thing they can check, and nothing else about
    the inside of this core.
    """


@dataclass
class SpeechRun:
    """What one attempt at speaking did. Never what it said.

    This is the evidence a validation surface prints. Every field answers a
    question that cannot be answered by hearing the audio: *did the first
    sentence really arrive before the last one was written* (``first_audio_ms``
    against the whole run), *was it streamed at all* (``groups`` — one group is
    a whole reply synthesised in one go and handed over at the end), and *did
    the exchange finish the way the protocol says* (``stopped``, which is the
    only terminator a streamed response has).
    """

    path: str = "wyoming"
    """``wyoming`` or ``direct``. The one thing that must never be guessed at
    from the outside: audio that sounds fine proves nothing about which code
    produced it."""
    port: int | None = None
    groups: int = 0
    """``audio-start`` events seen. One per sentence the server finished."""
    audio_bytes: int = 0
    first_audio_ms: float | None = None
    """From writing ``synthesize-start`` to the first byte of audio."""
    total_ms: float | None = None
    stopped: bool = False
    """``synthesize-stopped`` arrived. False after a complete-looking run means
    the server stopped talking without ending the exchange, which is precisely
    the failure that hangs Home Assistant."""
    rate: int | None = None
    width: int | None = None
    channels: int | None = None
    format_changed: bool = False
    """A later ``audio-start`` disagreed with the first. Recorded because only
    the first one frames the audio: every later group's samples are played
    underneath that header, so a disagreement is heard as wrong-speed speech
    rather than raised as an error."""
    failure: str | None = None
    """The sentence to show when there is one. ``None`` on a clean run."""
    note: str | None = None
    """Why this run took the path it did, when that needs saying — "Wyoming is
    switched off", "Wyoming did not answer". Present on a fallback, absent on
    an ordinary Wyoming run."""

    @property
    def ok(self) -> bool:
        return self.failure is None

    def as_log(self) -> dict[str, object]:
        """The run as log keywords. Counts, timings and formats only."""
        return {
            "path": self.path,
            "groups": self.groups,
            "audio_bytes": self.audio_bytes,
            "first_audio_ms": self.first_audio_ms,
            "total_ms": self.total_ms,
            "stopped": self.stopped,
            "rate": self.rate,
            "format_changed": self.format_changed,
            "failed": not self.ok,
        }


def text_chunks(text: str, size: int = CHUNK_CHARS) -> Iterator[str]:
    """``text`` as the pieces a model would have produced it in.

    Cut on whitespace at or after ``size`` characters, so no chunk ends inside
    a word and every chunk carries the spacing that followed it — the server
    concatenates these back into one buffer, so a lost space would join two
    words and change what is spoken.
    """
    if size < 1:
        size = 1
    start = 0
    length = len(text)
    while start < length:
        end = start + size
        if end >= length:
            yield text[start:]
            return
        # Forward to the next space rather than back to the previous one: back
        # can find no space at all in a long token and would then cut mid-word.
        while end < length and not text[end].isspace():
            end += 1
        while end < length and text[end].isspace():
            end += 1
        yield text[start:end]
        start = end


class WyomingSpeech:
    """One streamed synthesis, over one connection, on loopback.

    Used in two steps on purpose::

        speech = WyomingSpeech(port=10300, voice_name="vits-onnx/glados")
        first = await speech.start(text)      # format, and the first audio
        async for pcm in speech.audio():      # everything, first group included
            ...
        await speech.close()

    ``start`` returns only once the **first** audio has arrived, and the
    caller cannot usefully act sooner: a WAV header needs a rate, a width and a
    channel count, and those are not known until the server has spoken a
    sentence. It is also what makes a clean fallback possible — everything that
    can go wrong before the first byte goes wrong before a single byte of the
    response has been written, so the caller is still free to choose a
    different path and say that it did.

    One connection per synthesis, opened and closed here. That is what Home
    Assistant does, and it is not only convention: the reference server latches
    a connection into streaming mode and never clears it, so reusing a
    connection is a compatibility risk for nothing gained.
    """

    def __init__(
        self,
        *,
        port: int,
        voice_name: str | None = None,
        chunk_chars: int = CHUNK_CHARS,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        self._port = int(port)
        self._voice_name = voice_name or None
        self._chunk_chars = chunk_chars
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._client: AsyncTcpClient | None = None
        self._started = 0.0
        self._pending: list[bytes] = []
        self._finished = False
        self.run = SpeechRun(path="wyoming", port=self._port)

    # -- driving the exchange ---------------------------------------------

    async def start(self, text: str) -> SpeechRun:
        """Connect, send the whole request, and read up to the first audio.

        Every event of the request is written before anything is read. That
        cannot deadlock here and it is worth saying why rather than leaving it
        to be rediscovered: what this side writes is text, bounded by
        :data:`~personacore.voice.reply.MAX_SPOKEN_CHARS`, which is far inside
        one socket buffer — so the server never stalls waiting for us to read
        while we are still writing. A client that streamed a model's output
        indefinitely would have to read and write at once.
        """
        client = AsyncTcpClient(
            LOOPBACK_HOST,
            self._port,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
        )
        try:
            await client.connect()
        except (TimeoutError, OSError) as exc:
            log.info("wyoming_client_connect_failed", port=self._port, error=repr(exc))
            raise WyomingSpeechError(
                f"Nothing answered the Wyoming server on port {self._port}."
            ) from exc
        self._client = client
        self._started = time.monotonic()

        voice = SynthesizeVoice(name=self._voice_name) if self._voice_name else None
        try:
            await client.write_event(SynthesizeStart(voice=voice).event())
            for piece in text_chunks(text, self._chunk_chars):
                await client.write_event(SynthesizeChunk(text=piece).event())
            # The whole message again, before `synthesize-stop` and not after
            # the audio. The protocol requires this of every client so that a
            # server which only ever understood `synthesize` still receives a
            # complete request; a server that answers it as well as the stream
            # says the reply twice. Sending it is how this client finds out
            # whether ours drops it.
            await client.write_event(Synthesize(text=text, voice=voice).event())
            await client.write_event(SynthesizeStop().event())
        except (TimeoutError, OSError) as exc:
            await self.close()
            raise WyomingSpeechError(
                f"The Wyoming server on port {self._port} closed the connection."
            ) from exc

        await self._read_to_first_audio()
        return self.run

    async def _read_to_first_audio(self) -> None:
        """Read events until there is audio to hand back, or there will not be."""
        while True:
            event = await self._next()
            if event is None:
                return
            audio = self._absorb(event)
            if audio:
                self._pending.append(audio)
                return

    async def audio(self) -> AsyncIterator[bytes]:
        """Every byte of the synthesis, first group included, as it arrives."""
        for piece in self._pending:
            yield piece
        self._pending = []
        while not self._finished:
            event = await self._next()
            if event is None:
                return
            audio = self._absorb(event)
            if audio:
                yield audio

    async def _next(self) -> Event | None:
        """One event, or ``None`` when the exchange is over for any reason.

        A failure is recorded on the run rather than raised: by the time this
        is reading, the response may already have bytes in it, and there is no
        way to un-send them. The stream ends where it ends and the run says
        why — which is the difference between audio that stopped early and a
        page that fell over.
        """
        client = self._client
        if client is None or self._finished:
            return None
        try:
            event = await client.read_event()
        except (TimeoutError, OSError) as exc:
            self._fail(f"The Wyoming server on port {self._port} stopped answering.", exc)
            return None
        if event is None:
            # The socket closed without `synthesize-stopped`. Not fatal to what
            # has already played, but it is exactly the fault this whole client
            # exists to be able to see, so it is named rather than shrugged at.
            self._fail(
                f"The Wyoming server on port {self._port} closed the connection "
                "before it said the speech had finished."
            )
            return None
        return event

    def _absorb(self, event: Event) -> bytes:
        """One event's effect on the run, and the audio it carried, if any."""
        if Error.is_type(event.type):
            failure = Error.from_event(event)
            # The server's own sentence. It is written for an operator by
            # contract and names nothing internal, which is why it is the one
            # thing from the far side that goes on the screen.
            self._fail(str(failure.text or "The Wyoming server refused to speak that."))
            return b""

        if SynthesizeStopped.is_type(event.type):
            # The terminator, and the only one. `audio-stop` ends a group; this
            # ends the response.
            self.run.stopped = True
            self._finish()
            return b""

        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.run.groups += 1
            self._note_format(start.rate, start.width, start.channels)
            return b""

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            # Read off the chunk, not off `audio-start`: the chunk is what
            # carries the format on every event, and it is what a client
            # building a header is meant to trust.
            self._note_format(chunk.rate, chunk.width, chunk.channels)
            data = chunk.audio or b""
            if data:
                if self.run.first_audio_ms is None:
                    self.run.first_audio_ms = (time.monotonic() - self._started) * 1000.0
                self.run.audio_bytes += len(data)
            return data

        if AudioStop.is_type(event.type):
            return b""

        # Anything else — an `info`, something newer than this client. Ignored
        # for the same reason the server ignores what it does not know.
        return b""

    def _note_format(self, rate: int, width: int, channels: int) -> None:
        """The first format wins; a later one that differs is recorded."""
        if self.run.rate is None:
            self.run.rate, self.run.width, self.run.channels = rate, width, channels
            return
        if (self.run.rate, self.run.width, self.run.channels) != (rate, width, channels):
            self.run.format_changed = True

    def _fail(self, message: str, exc: BaseException | None = None) -> None:
        if self.run.failure is None:
            self.run.failure = message
        log.info(
            "wyoming_client_stream_failed",
            port=self._port,
            groups=self.run.groups,
            error=repr(exc) if exc is not None else None,
        )
        self._finish()

    def _finish(self) -> None:
        self._finished = True
        if self.run.total_ms is None and self._started:
            self.run.total_ms = (time.monotonic() - self._started) * 1000.0

    async def close(self) -> None:
        """Hang up. Never raises — a socket that will not close politely has
        already given us everything it was going to."""
        client, self._client = self._client, None
        self._finish()
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 - closing may not become the failure
            log.info("wyoming_client_disconnect_failed", port=self._port, error=repr(exc))


def streaming_wav_header(*, rate: int, width: int, channels: int) -> bytes:
    """A WAV header for audio whose length is not known yet.

    :func:`~personacore.voice.engine.wav_bytes` writes the real sizes because
    it has all the samples. This one is written before the second sentence
    exists, so the two length fields carry ``0xFFFFFFFF`` — the convention for
    a WAV that is being produced as it is played. A player reads until the
    stream ends rather than counting.

    Zero was the other option and is what Home Assistant writes, but Home
    Assistant is feeding its own pipeline; a browser handed a ``data`` chunk of
    length zero is entitled to conclude there is no audio, and some do.
    """
    unknown = 0xFFFFFFFF
    block = channels * width
    return b"".join(
        (
            b"RIFF",
            unknown.to_bytes(4, "little"),
            b"WAVEfmt ",
            (16).to_bytes(4, "little"),
            (1).to_bytes(2, "little"),
            channels.to_bytes(2, "little"),
            rate.to_bytes(4, "little"),
            (rate * block).to_bytes(4, "little"),
            block.to_bytes(2, "little"),
            (width * 8).to_bytes(2, "little"),
            b"data",
            unknown.to_bytes(4, "little"),
        )
    )


__all__ = [
    "CHUNK_CHARS",
    "CONNECT_TIMEOUT",
    "LOOPBACK_HOST",
    "READ_TIMEOUT",
    "SpeechRun",
    "WyomingSpeech",
    "WyomingSpeechError",
    "streaming_wav_header",
    "text_chunks",
]
