"""Saving one conversation: the transcript, and the replies spoken again.

The chat-room contract's §5.2, and the whole of it is one decision the owner
made: rather than saving the audio file, it is regenerated behind the scenes
when asked for. Nothing is kept on disk between the reply being heard and the
file being asked for. The live stream a listener hears is ephemeral and stays
that way; asking for the download re-synthesises every spoken reply from the
words the transcript already holds.

What that buys, and why it is not a shortcut: no second copy of a household's
audio on disk, no growth to watch, and no purge window of its own to keep in
step with the retention rule (ADR-0004). The transcript is already the record;
the audio is a rendering of it, and a rendering can be made again.

**A reply whose voice is gone is a missing file, not a failed download.** An
engine that was uninstalled, a persona that was deleted, a voice that was
switched off — each of those costs one audio file and puts one line in the
transcript saying so. The alternative is a save button that fails entirely
because one of forty replies cannot be spoken, which would make the whole
feature unreliable for the sake of a file nobody could have had anyway.

**The zip is written as it is produced.** One reply's audio is in memory at a
time and the bytes go out to the browser as each entry finishes, rather than a
whole conversation's audio being assembled first — a long thread is tens of
megabytes and this core is a small CPU-only container (CLAUDE.md's hard
constraint), so holding all of it to answer one click is not a trade worth
making.

Counts, formats and timings may be logged here. **Never the text and never the
audio** — the same rule the rest of the voice path keeps, for the same reason.
"""

from __future__ import annotations

import asyncio
import re
import zipfile
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from personacore.audit.models import AuthorKind, MessageRole, TranscriptRecord
from personacore.voice.engine import EngineError, wav_bytes
from personacore.voice.reply import MAX_SPOKEN_CHARS, speakable_text

log = structlog.get_logger(__name__)

TRANSCRIPT_NAME = "transcript.txt"
"""What the words are called inside the zip. Plain text rather than markdown or
JSON: the thing somebody saving a conversation wants is to be able to read it,
and a reply's own markdown is already readable as it was written."""

AUDIO_DIRECTORY = "audio"

AUDIO_MISSING = "[no audio: {reason}]"
"""The line that stands where an audio file could not be produced.

Named in the transcript rather than left as a silent gap, because a
conversation with eleven replies and nine audio files is otherwise a puzzle:
which two, and why. The reason is the voice library's own sentence, which
already says whether an engine is off, missing, or was never chosen.
"""

NO_VOICE = "this reply had no voice"
PERSONA_GONE = "the persona that said it is not installed any more"
NO_SPEECH_SUBSYSTEM = "this core has no voice subsystem"
SYNTHESIS_REFUSED = "the engine could not speak it"

MAX_SAVED_MESSAGES = 500
"""How many messages one save may contain.

The screen reads far fewer than this; the ceiling is here so that a store which
one day returns more cannot turn one click into an unbounded amount of
synthesis. It is a limit on the *work*, not a policy about conversations.
"""


@dataclass(frozen=True, slots=True)
class SavedMessage:
    """One line of the transcript, already attributed.

    ``author`` is spelled by :func:`~personacore.web.screens.chat.author_label`
    before it gets here — the parentheses rule (§5.3) is applied in exactly one
    place and this file is not it.
    """

    author: str
    when: datetime
    text: str
    spoken: bool
    """Whether this is a reply that should have audio beside it."""

    persona: str | None = None
    """Which persona to speak it in, or ``None`` for the core's default. A
    *name*, not a voice: the voice is resolved from the persona at download
    time, so a persona whose voice changed since is heard as it is now, which
    is the same answer the play button under the reply gives."""

    persona_gone: bool = False
    """This reply names a persona this core no longer has.

    Kept apart from ``persona`` being ``None`` — which means "nobody was
    recorded, use the thread's" — because the two want opposite answers. A
    persona that has been deleted must not be substituted for: the reply would
    then be read in a voice its author never had, and nothing in the zip would
    say so. It gets :data:`PERSONA_GONE` and no file.
    """


class _Sink:
    """Somewhere for :mod:`zipfile` to write that is not a file.

    ``zipfile`` writes to anything with ``write`` and ``flush``; with no
    ``tell`` it treats the output as a stream and emits the data descriptors
    that make that legal, which is exactly what is wanted — the bytes are
    handed to the browser as they are produced and nothing is buffered whole.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:  # pragma: no cover - zipfile calls it, it does nothing
        return None

    def drain(self) -> bytes:
        """Everything written since the last drain, and forget it."""
        joined = b"".join(self._chunks)
        self._chunks.clear()
        return joined


_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


def save_filename(title: str, started: datetime) -> str:
    """What the browser calls the file it just downloaded.

    Built from characters this function chose rather than from the title as
    typed: a conversation can be named anything, including quotes and newlines,
    and a filename goes into a ``Content-Disposition`` header where those are
    how a header gets split. The date is included because somebody saving two
    conversations about the kettle wants to be able to tell them apart.
    """
    slug = _UNSAFE.sub("-", title).strip("-").lower()[:40].strip("-")
    return f"conversation-{slug or 'untitled'}-{started.strftime('%Y%m%d-%H%M')}.zip"


def audio_name(index: int, author: str) -> str:
    """Where one reply's audio sits in the zip.

    Numbered by its position in the conversation so the files sort into reading
    order, and named after who said it so a thread with two personas in it can
    be told apart without opening anything.
    """
    who = _UNSAFE.sub("-", author).strip("-").lower()[:24].strip("-")
    return f"{AUDIO_DIRECTORY}/{index:04d}-{who or 'reply'}.wav"


def saved_messages(
    rows: Sequence[TranscriptRecord],
    *,
    names: Mapping[str, str],
    persona: str | None,
    author_of: Any,
    human: str,
    assistant: str,
) -> list[SavedMessage]:
    """The conversation's rows as the lines this file writes.

    ``names`` maps a persona's display name to the name its files are under, so
    a reply is spoken by the persona that actually said it rather than by
    whoever the thread is set to now — a conversation may contain several
    personas and that is deliberate (§5.1).

    **A reply whose persona this core no longer has gets no audio at all.** Not
    the thread's current voice: the row says who said it, that persona has been
    deleted, and speaking their words in somebody else's voice would put a
    sentence in a character's mouth it never said — which is a worse answer
    than a missing file and one the listener could not tell from the truth.

    A reply with *no author recorded* — every reply from before authorship
    existed — is different, and does fall back to the thread's persona. There
    is no fact being contradicted there, only one that was never written down,
    and the thread's persona is the voice that reply would have been read in.
    """
    out: list[SavedMessage] = []
    for record in rows[:MAX_SAVED_MESSAGES]:
        if record.role not in (MessageRole.USER, MessageRole.ASSISTANT):
            continue
        reply = record.role is MessageRole.ASSISTANT
        author = author_of(record.author, fallback=assistant if reply else human)
        said_by = record.author if reply else None
        gone = False
        speaks_as = persona
        if said_by is not None and said_by.kind is AuthorKind.PERSONA:
            known = names.get(said_by.name)
            # A name the persona store does not answer to any more. Recorded as
            # a fact rather than resolved to a fallback, because "who said this"
            # and "who is in this thread now" are different questions and only
            # one of them has an answer here.
            gone = known is None
            speaks_as = known or persona
        out.append(
            SavedMessage(
                author=author,
                when=record.timestamp,
                text=record.content,
                spoken=reply,
                persona=speaks_as,
                persona_gone=gone,
            )
        )
    return out


def _heading(title: str, people: Sequence[str], when: datetime) -> str:
    lines = [title, when.strftime("%Y-%m-%d %H:%M UTC")]
    if people:
        lines.append("Participants: " + ", ".join(people))
    lines.append("")
    return "\n".join(lines)


def _entry(message: SavedMessage, note: str) -> str:
    """One message in the transcript, with whatever is known about its audio."""
    head = f"{message.author} — {message.when.strftime('%Y-%m-%d %H:%M:%S')}"
    body = message.text.rstrip()
    return f"{head}\n{body}\n{note}\n" if note else f"{head}\n{body}\n"


async def _spoken_wav(made: Any, text: str, persona: str | None, cache: dict[str, Any]) -> bytes:
    """This reply, synthesised now. Raises :class:`EngineError` with the voice
    library's own sentence when it cannot be.

    The resolution is cached per persona for the length of one save: a
    forty-reply conversation with one persona in it asked the library forty
    times otherwise, and the library reads the disk.
    """
    key = persona or ""
    resolved = cache.get(key)
    if resolved is None:
        # In a worker thread because it reads the disk, and again below because
        # synthesis is CPU work in a C library — neither may block the loop
        # this core serves everything else on.
        resolved = await asyncio.to_thread(made.resolve, persona)
        cache[key] = resolved
    resolution, pauses = resolved
    if resolution is None or not getattr(resolution, "can_speak", False):
        raise EngineError(getattr(resolution, "reason", None) or NO_VOICE)
    audio = await asyncio.to_thread(resolution.speak, text, pauses=pauses)
    return wav_bytes(audio)


async def conversation_zip(
    messages: Sequence[SavedMessage],
    *,
    title: str,
    people: Sequence[str],
    started: datetime,
    made: Any | None,
) -> AsyncIterator[bytes]:
    """The whole download, one chunk at a time.

    Audio first, transcript last. The transcript cannot be written until every
    reply has been tried, because it carries a line for each one that could not
    be spoken — and in a zip written to a stream, entries go out in the order
    they are finished. Nothing reading a zip cares what that order is.

    Nothing in here may raise past this generator: a failure part way through
    would truncate a download the browser has already begun, and there is no
    status left to change by then. One reply's failure costs that reply's file
    and is recorded as a line in the transcript.
    """
    sink = _Sink()
    archive = zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED)
    notes: list[str] = []
    spoken = 0
    try:
        cache: dict[str, Any] = {}
        for index, message in enumerate(messages, start=1):
            note = ""
            if message.spoken:
                note = await _write_audio(archive, index, message, made, cache)
                if not note:
                    spoken += 1
                for chunk in _drained(sink):
                    yield chunk
            notes.append(note)

        archive.writestr(
            TRANSCRIPT_NAME,
            _heading(title, people, started)
            + "\n".join(_entry(m, n) for m, n in zip(messages, notes, strict=True)),
        )
    finally:
        archive.close()
    log.info("conversation_saved", messages=len(messages), audio=spoken)
    for chunk in _drained(sink):
        yield chunk


def _drained(sink: _Sink) -> Iterator[bytes]:
    data = sink.drain()
    if data:
        yield data


async def _write_audio(
    archive: zipfile.ZipFile,
    index: int,
    message: SavedMessage,
    made: Any | None,
    cache: dict[str, Any],
) -> str:
    """Put one reply's audio in the zip, or say why there is none.

    Returns the line for the transcript: empty when the audio is there.
    """
    text = speakable_text(message.text, limit=MAX_SPOKEN_CHARS)
    if not text:
        # A reply that is only markup or only whitespace was never spoken
        # either, so there is nothing missing and nothing to explain.
        return ""
    if message.persona_gone:
        # Deliberately before the engine is asked anything. There is a voice
        # this core could speak with; it is simply not the one that said this.
        return AUDIO_MISSING.format(reason=PERSONA_GONE)
    if made is None:
        return AUDIO_MISSING.format(reason=NO_SPEECH_SUBSYSTEM)
    try:
        data = await _spoken_wav(made, text, message.persona, cache)
    except EngineError as exc:
        # The library's own sentence, written for an operator: "switched off",
        # "not installed", "no voice chosen".
        return AUDIO_MISSING.format(reason=str(exc).rstrip("."))
    except Exception as exc:  # noqa: BLE001 - one silent reply, not a dead download
        log.warning("conversation_save_audio_failed", error=repr(exc))
        return AUDIO_MISSING.format(reason=SYNTHESIS_REFUSED)
    # Stored rather than deflated: a WAV of speech does not compress usefully
    # and deflating it costs the CPU this container has least of.
    archive.writestr(audio_name(index, message.author), data, zipfile.ZIP_STORED)
    return ""


__all__ = [
    "AUDIO_MISSING",
    "PERSONA_GONE",
    "MAX_SAVED_MESSAGES",
    "TRANSCRIPT_NAME",
    "SavedMessage",
    "audio_name",
    "conversation_zip",
    "save_filename",
    "saved_messages",
]
