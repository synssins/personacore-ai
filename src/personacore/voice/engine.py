"""The engine contract — ADR-0029 and its interface addendum.

An engine answers two questions and owns nothing else: *what can I speak*, and
*speak this*. Everything around it — which voice a persona uses, the single
list, the switches, installing a voice — is core's, and lives in
:mod:`personacore.voice.registry` and :mod:`personacore.voice.library`.

Two words that are easy to conflate and must not be:

``available``
    About this build and this hardware. Fixed for the life of the process:
    the PyTorch engines are not usable on ARM, so on that hardware they say so
    rather than offering a switch that starts something which cannot work
    (PC-338).

``enabled``
    The operator's switch, in ``[voice.engines.<id>] enabled``. Changed at any
    time, and saving it starts or stops the engine there and then (PC-335).

The data types here are deliberately forgiving. Engines are written by other
hands against this module, so every field except an identity has a default and
the registry backfills what an engine leaves blank rather than refusing the
voice — a voice dropped for a missing display name would be a silent loss of
something an operator installed.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

ENGINE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
"""``vits-onnx``. Lowercase, digits and dashes, and never a voice name.

The id is also a directory name under ``appdata/voices/``, which is why it is
bounded and refuses a dot or a slash before any path is built from it.
"""

VOICE_ID_RULE = r"[a-z0-9][a-z0-9._-]{0,63}"
"""What a voice id may be. **One rule, and this is where it lives.**

There were two: this module allowed ``A-Z`` and
:mod:`personacore.plugins.voice_packages` did not, so a folder called
``GLaDOS`` was discovered, listed and speakable by the engine while being
invisible on the Voices screen and unexportable and unremovable there. A voice
that is discoverable has to be manageable, and the only way to keep that true
is for both sides to read the same pattern rather than two that agree until one
is edited. The installer imports it from here.

**Lowercase**, of the two rules, because the id is a directory name: on Windows
and macOS ``GLaDOS`` and ``glados`` are the same folder and on Linux they are
two, so permitting both invites a pair of voices that collide on one host and
not on another — and a persona's stored ``voice_name`` that resolves to
different audio depending on where the container runs. It is also what
``docs/voice-pack-format.md`` already specifies. Dots, dashes and underscores
stay in: real Piper voices are called ``en_US-libritts_r-medium``.

An id that is not permitted is refused at install with a sentence
(``VOICE_ID_UNUSABLE``); one already on disk is skipped and named rather than
half-shown.
"""

VOICE_ID_PATTERN = re.compile(f"^{VOICE_ID_RULE}$")
"""The rule above, anchored, for the readers that match against it.

It becomes a directory name, so traversal is refused at the cheapest door."""


SYNTHESIS_FIELDS: tuple[str, ...] = ("length_scale", "noise_scale", "noise_w")
"""The synthesis defaults a voice pack may carry — ``docs/voice-pack-format.md``
``[synthesis]``, in the order the manage screen shows them."""

SYNTHESIS_LIMITS: dict[str, tuple[float, float]] = {
    "length_scale": (0.1, 5.0),
    "noise_scale": (0.0, 2.0),
    "noise_w": (0.0, 2.0),
}
"""(low, high) per field. Wide enough to be expressive, narrow enough that a
mistyped default cannot make a model allocate a minute of audio for one word.

The format says "ranges are the engine's to validate", and these are the
contract's — an engine adopts them (:data:`personacore.voice.engines.vits_onnx.
SCALE_LIMITS` is this table) and may narrow them. They are here rather than
inside one engine so the installer can refuse a nonsense value **at the moment
it is typed**, which is the only place the operator is still looking.
"""

SYNTHESIS_MEANING: dict[str, str] = {
    "length_scale": "speed — higher is slower",
    "noise_scale": "expressiveness",
    "noise_w": "pitch variance",
}

SYNTHESIS_OUT_OF_RANGE = (
    "{field} is {value}, which is outside {low} to {high}. That setting is "
    "{meaning}, and a value past the ends of that range does not make the voice "
    "more of anything — it makes it unusable. Nothing was saved."
)

SYNTHESIS_NOT_A_NUMBER = (
    "{field} is {value!r}, which is not a number. That setting is {meaning}; "
    "leave the box empty to use the model's own default. Nothing was saved."
)


def synthesis_refusal(field: str, value: object) -> str | None:
    """``None`` if ``value`` is a usable default for ``field``, else the sentence.

    The one validator both sides call: the installer before it writes
    ``voice.toml`` (so the operator reads it while the form is still on screen)
    and the engine over what it finds on disk (so a file edited by hand cannot
    reach the model). ``None`` — the field not set — is always fine; that is
    what "the model's own default" means.
    """
    if value is None:
        return None
    low, high = SYNTHESIS_LIMITS[field]
    meaning = SYNTHESIS_MEANING.get(field, "a synthesis default")
    label = field.replace("_", " ").capitalize()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return SYNTHESIS_NOT_A_NUMBER.format(field=label, value=value, meaning=meaning)
    number = float(value)
    # NaN fails every comparison, so it is caught by asking rather than by
    # falling through a range check that quietly says yes.
    if number != number or number in (float("inf"), float("-inf")):
        return SYNTHESIS_NOT_A_NUMBER.format(field=label, value=value, meaning=meaning)
    if not low <= number <= high:
        return SYNTHESIS_OUT_OF_RANGE.format(
            field=label, value=number, low=low, high=high, meaning=meaning
        )
    return None


UNSUPPORTED_VOICE = (
    "Unsupported voice. {name} is not a model file. Check the files in the "
    "pack and install it again."
)
"""What an operator reads when a pack's model is not a model.

Deliberately says nothing about *why* the file is wrong. The check below can
tell a web page from a model; it cannot tell which of a dozen ways somebody
got one, and a sentence that guesses would send the reader after the wrong
thing. The pack format and how to build one belong in the help pages, not in a
refusal.
"""

# What a file that is plainly a text document starts with, when it is sitting
# where a model should be. Every one of these is something a download produces
# instead of the file that was wanted:
#
#   ``<``  an HTML page -- a repository's *page* for a file rather than the
#          file, which is what a browser saves from a "blob" or "view" URL.
#   ``{``  ``[``  JSON -- an API's answer, or an error document.
#   ``version https://git-lfs``  a Git LFS pointer, which is what a clone
#          without LFS installed leaves in place of every large file.
#   ``#!``  a script.
#   ``PK``  a zip. A pack's own archive, saved under the model's name.
#
# Byte zero, after leading whitespace, and nothing deeper. A ``.safetensors``
# file is JSON too -- but its JSON starts at byte eight, behind a length, so
# testing the first byte does not catch it.
_NOT_A_MODEL: tuple[bytes, ...] = (
    b"<",
    b"{",
    b"[",
    b"version https://git-lfs",
    b"#!",
    b"PK",
)


def model_refusal(path: Path) -> str | None:
    """``None`` if ``path`` could be a model, else the sentence to show.

    The one validator both sides call, in the shape :func:`synthesis_refusal`
    already set: the installer, over a pack's files before it writes any of
    them, and the voice list, over what is already on disk. One predicate
    because a voice refused on the way in and a voice marked unusable on the
    list must agree -- two checks that drifted would mean a voice the list
    calls broken installing without complaint.

    **It answers "is this obviously not a model", never "is this a valid
    model".** Core does not load models and is not the authority on their
    formats -- ADR-0029 gives that to the engine, which reads its own
    directory. So this is permissive on purpose: an unfamiliar format passes,
    and only a file that is plainly a text document is refused. Anything
    subtler fails at the engine, where the error can name the format.

    Cheap enough for a list screen: 64 bytes, no model load.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        # Unreadable is the caller's problem, not this one's. Both callers
        # already survive a folder they cannot read, and a refusal written
        # from an I/O error would blame the file for the volume.
        return None
    if not head:
        return UNSUPPORTED_VOICE.format(name=path.name)
    if head.lstrip().startswith(_NOT_A_MODEL):
        return UNSUPPORTED_VOICE.format(name=path.name)
    return None


class EngineError(RuntimeError):
    """An engine could not do what was asked.

    Engines raise this and never a bare exception, so the speak path can tell
    "this voice did not come out" from a defect in the core. The text reaches
    an operator, so it says what happened in a sentence.
    """


class VoiceState(StrEnum):
    """Why a chosen voice can or cannot be spoken right now.

    A closed set because the persona path branches on it, and the difference
    between "switched off" and "not installed" is the difference between an
    operator flipping a switch and an operator installing something.
    """

    READY = "ready"
    """The engine is on and holds this voice. Audio comes out."""

    NO_VOICE_CHOSEN = "no-voice-chosen"
    """The persona names no voice. Not a fault — most personas do not."""

    ENGINE_UNKNOWN = "engine-unknown"
    """Nothing in this build provides an engine by that id."""

    ENGINE_UNAVAILABLE = "engine-unavailable"
    """The engine exists but cannot run here (PC-338). No switch is offered."""

    ENGINE_DISABLED = "engine-disabled"
    """The operator's switch is off (PC-336). The persona replies in text."""

    ENGINE_FAILED = "engine-failed"
    """Switched on, but starting it failed. Degraded, and said out loud."""

    VOICE_MISSING = "voice-missing"
    """The engine is on and has no voice by that name installed."""


def _first_str(source: object, names: tuple[str, ...]) -> str | None:
    """The first of ``names`` on ``source`` that is a non-empty string."""
    for name in names:
        value = getattr(source, name, None)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class Voice:
    """One installed voice.

    ``engine_id`` is carried on the voice itself rather than being implied by
    where it was found, because the single list (ADR-0029 §5) renders
    ``GLaDOS (vits-onnx)`` and a voice separated from its engine cannot be
    spoken. An engine may leave it blank; the library fills it in from the
    engine that produced the voice, which is the only authority on it.
    """

    id: str
    display: str = ""
    path: Path | None = None
    engine_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    native: Any = None
    """The engine's own object for this voice, handed straight back to
    :meth:`Engine.speak`.

    A real engine's voice carries things core has no business knowing — a model
    path, a phoneme map, an inference profile — and it wants those back when it
    speaks rather than looking the folder up a second time. So core adopts what
    an engine returns into this shape for its own list, keeps the original
    here, and returns the original verbatim when asking for audio. Neither side
    has to know the other's fields.
    """

    def __post_init__(self) -> None:
        # A voice with no display name is shown by its id rather than by an
        # empty string, so `voice.toml` staying optional (PC-331) costs
        # nothing in the picker.
        if not self.display:
            object.__setattr__(self, "display", self.id)

    @classmethod
    def adopt(cls, source: object, engine_id: str) -> Voice | None:
        """Core's reading of whatever an engine returned, or ``None``.

        The interface addendum names ``Voice`` in the protocol and never
        defines its fields, so the engines were written against their own —
        ``voice_id``/``directory``/``name`` where this module says
        ``id``/``path``/``display``. That is a real gap in the contract and it
        is written up rather than papered over, but it must not cost an
        operator their voices in the meantime, and core should not be handing
        an engine's private object to a template regardless.

        So identity is read by any of its plausible names, everything else
        falls back, and the engine's own object is kept on :attr:`native` for
        the trip back to :meth:`Engine.speak`. An object with no usable id at
        all is refused — that is the one thing that cannot be guessed.
        """
        if isinstance(source, cls):
            return dataclasses.replace(
                source,
                engine_id=engine_id,
                native=source.native if source.native is not None else source,
            )

        voice_id = _first_str(source, ("id", "voice_id", "name"))
        if voice_id is None:
            return None
        display = _first_str(source, ("display", "display_name", "name", "title")) or voice_id
        path = None
        for attribute in ("path", "directory", "dir", "root"):
            candidate = getattr(source, attribute, None)
            if isinstance(candidate, Path):
                path = candidate
                break
        metadata = getattr(source, "metadata", None)
        return cls(
            id=voice_id,
            display=display,
            path=path,
            engine_id=engine_id,
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            native=source,
        )

    @property
    def speakable_object(self) -> Any:
        """What to pass to :meth:`Engine.speak` — the engine's own object."""
        return self.native if self.native is not None else self

    @property
    def label(self) -> str:
        """``GLaDOS (vits-onnx)`` — ADR-0029 §5, in one place.

        Every surface that lists voices reads this rather than formatting its
        own, so the picker, the persona screen and a log line cannot drift into
        three spellings of the same voice.
        """
        return f"{self.display} ({self.engine_id})" if self.engine_id else self.display


@dataclass(frozen=True)
class SkippedVoice:
    """A voice folder that could not be read, and why.

    Named rather than counted: "3 voices skipped" tells an operator nothing
    they can act on, and one unreadable folder must never cost them the other
    nine or stop the core starting.
    """

    id: str
    reason: str
    path: Path | None = None
    engine_id: str = ""

    @classmethod
    def adopt(cls, source: object, engine_id: str) -> SkippedVoice:
        """Core's reading of a skip an engine reported. See :meth:`Voice.adopt`."""
        if isinstance(source, cls):
            return dataclasses.replace(source, engine_id=source.engine_id or engine_id)
        path = getattr(source, "path", None)
        return cls(
            id=_first_str(source, ("id", "voice_id", "name")) or "?",
            reason=_first_str(source, ("reason", "message", "detail"))
            or "it could not be read.",
            path=path if isinstance(path, Path) else None,
            engine_id=engine_id,
        )


@dataclass(frozen=True)
class Audio:
    """Speech, as bytes plus what is needed to play them.

    Raw samples rather than an encoded container: the streaming decision is
    core's (ADR-0029), and an engine that returned a WAV would have made a
    framing decision on core's behalf.
    """

    data: bytes
    sample_rate: int
    channels: int = 1
    encoding: str = "pcm_s16le"

    def __len__(self) -> int:
        return len(self.data)


def wav_bytes(spoken: Any) -> bytes:
    """An engine's raw samples in a WAV container, for something to play.

    The framing decision ADR-0029 keeps in core, written once. :class:`Audio`
    is raw samples precisely so no engine makes this choice, which means every
    surface that hands audio to a browser has to make it instead — and two
    surfaces writing their own headers is the "``Audio`` existed twice" mistake
    the interface addendum was corrected for.

    Read with ``getattr`` rather than typed to :class:`Audio` for the same
    reason :meth:`Voice.adopt` is generous: an engine returns its own object
    and only the four fields below are ever asked of it.
    """
    data = bytes(getattr(spoken, "data", b"") or b"")
    rate = int(getattr(spoken, "sample_rate", 22050) or 22050)
    channels = int(getattr(spoken, "channels", 1) or 1)
    bits = 16
    block = channels * bits // 8
    header = b"".join(
        (
            b"RIFF",
            (36 + len(data)).to_bytes(4, "little"),
            b"WAVEfmt ",
            (16).to_bytes(4, "little"),
            (1).to_bytes(2, "little"),
            channels.to_bytes(2, "little"),
            rate.to_bytes(4, "little"),
            (rate * block).to_bytes(4, "little"),
            block.to_bytes(2, "little"),
            bits.to_bytes(2, "little"),
            b"data",
            len(data).to_bytes(4, "little"),
        )
    )
    return header + data


@runtime_checkable
class Engine(Protocol):
    """What every engine implements — the ADR-0029 addendum, verbatim in shape.

    Lifecycle, and the whole reason four engines fit in one image:

    * A registered engine has done nothing. Constructing it must not load a
      model, open a runtime or read a voice.
    * :meth:`start` is called when the operator switches it on.
    * :meth:`stop` releases everything. After it returns the engine holds no
      model in memory (PC-335).
    """

    id: str
    display: str
    available: bool
    unavailable_reason: str | None

    def start(self) -> None:
        """Load whatever the engine needs to answer. Called when switched on."""
        ...

    def stop(self) -> None:
        """Release it. After this the engine holds no memory (PC-335)."""
        ...

    def voices(self, root: Path) -> tuple[list[Voice], list[SkippedVoice]]:
        """What this engine can speak, from its own directory, and what it
        could not read and why. Never raises for a bad voice folder.

        ``root`` is the engine's **own** directory —
        ``appdata/voices/<engine-id>`` — not the voices root. The registry
        resolves it (:func:`personacore.voice.library.engine_voice_root`) so
        that an engine never composes a path from its own id and can never
        read another engine's voices.

        Listing is filesystem work and must not start the engine: a disabled
        engine's voices are still *known*, and asking what is installed is how
        the warning in PC-336 gets a voice name to put in its sentence.
        """
        ...

    def speak(self, voice: Voice, text: str, **knobs: Any) -> Audio:
        """Text in, audio out. Raises :class:`EngineError`, never a bare one."""
        ...


def looks_like_engine(candidate: object) -> str | None:
    """``None`` if ``candidate`` satisfies :class:`Engine`, else what is wrong.

    ``isinstance`` against a runtime-checkable protocol answers yes/no and
    nothing else, and "this object is not an engine" is useless to whoever has
    to fix it. This names the missing member instead, and is what the registry
    refuses a bad registration with.
    """
    missing = [
        name
        for name in ("id", "display", "available", "start", "stop", "voices", "speak")
        if not hasattr(candidate, name)
    ]
    if missing:
        return f"it has no {', '.join(missing)}"
    for name in ("start", "stop", "voices", "speak"):
        if not callable(getattr(candidate, name)):
            return f"its {name} is not callable"
    engine_id = getattr(candidate, "id", None)
    if not isinstance(engine_id, str) or not ENGINE_ID_PATTERN.match(engine_id):
        return (
            f"its id {engine_id!r} is not a usable engine id — lowercase letters, "
            "digits and dashes, up to 32 characters, e.g. 'vits-onnx'"
        )
    return None
