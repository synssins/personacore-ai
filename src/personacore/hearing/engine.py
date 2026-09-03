"""The recogniser contract — the mirror of :mod:`personacore.voice.engine`.

A recogniser answers one question and owns nothing else: *turn this audio into
words*. Everything around it — which recogniser is on, the switches, what to do
with the words afterwards — is core's, and lives in
:mod:`personacore.hearing.registry`.

Two words that are easy to conflate and must not be, and they mean here exactly
what they mean for voice:

``available``
    About this build and this hardware. Fixed for the life of the process: a
    recogniser whose runtime is not in this image says so rather than offering
    a switch that starts something which cannot work.

``enabled``
    The operator's switch, in ``[hearing.engines.<id>] enabled``. Changed at
    any time, and saving it starts or stops the recogniser there and then.

The data types here are deliberately forgiving, for the same reason voice's
are: recognisers are written by other hands against this module, so every field
except an identity has a default and the registry backfills what a recogniser
leaves blank rather than refusing it.

**Nothing in this subsystem may stop the core starting.** Hearing is as
detachable as speech: a core that cannot listen is a core you type at, which is
what every core is today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

ENGINE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
"""``whisper-onnx``. Lowercase, digits and dashes, and never a model name.

The same rule as :data:`personacore.voice.engine.ENGINE_ID_PATTERN`, **restated
rather than imported**, and there is a test in ``tests/hearing`` that fails if
the two ever stop agreeing.

Imported would have been the obvious move, and it is the move voice itself
makes for ``VOICE_ID_RULE`` — but that case is two readers of *one* namespace,
where a folder discoverable by one side and invisible to the other is a real
defect. This is not that. A hearing engine id and a speech engine id are
separate namespaces that happen to share a lexical rule: ``vits-onnx`` being a
speech engine says nothing at all about what can listen. Importing would mean a
rule widened for a voice-specific reason silently widens here too, and would
put :mod:`personacore.voice` on the import path of every core that only wants
to listen — so a defect in voice could stop hearing loading, which is precisely
what both subsystems exist to make impossible. The test keeps them honest
without the coupling.

The id is bounded and refuses a dot or a slash because it is also a directory
name under ``appdata/`` and a key in ``core.toml``.
"""


class HearingError(RuntimeError):
    """A recogniser could not do what was asked.

    Recognisers raise this and never a bare exception, so the listen path can
    tell "these words did not come out" from a defect in the core. The text
    reaches an operator, so it says what happened in a sentence.
    """


@dataclass(frozen=True)
class Audio:
    """Sound, as bytes plus what is needed to read them.

    The mirror of :class:`personacore.voice.engine.Audio`, and deliberately the
    same four fields in the same order: somebody who has read one must be able
    to read the other without learning new conventions, and audio going *in*
    and audio coming *out* differing in shape would be a trap.

    Raw samples rather than an encoded container. Where the audio came from —
    a microphone, a Wyoming client, an upload — is the surface's business, and
    a recogniser handed a WAV would have to unwrap a framing decision the
    surface had already made.
    """

    data: bytes
    sample_rate: int = 16000
    """16 kHz because that is what speech recognition wants and what every
    engine in this family is trained at. Defaulted rather than required so a
    surface that captures at the expected rate does not have to say so twice —
    the same forgiveness the rest of this module is built on. A surface that
    captures at anything else must pass it."""

    channels: int = 1
    encoding: str = "pcm_s16le"

    def __len__(self) -> int:
        return len(self.data)

    @property
    def seconds(self) -> float:
        """How long this is, for a log line and for a length guard.

        Assumes 16-bit samples, which is what :attr:`encoding` says. Zero
        rather than a division error if a caller built one with no rate at all.
        """
        frame = max(1, self.channels) * 2
        rate = self.sample_rate
        if rate <= 0:
            return 0.0
        return len(self.data) / (frame * rate)


@dataclass(frozen=True)
class Transcript:
    """What was heard.

    ``text`` is the whole of the promise; everything else has a default, so a
    recogniser that knows only the words returns ``Transcript(text=...)`` and a
    surface that wants only the words reads ``.text``.

    **An empty transcript is a success.** Silence, a cough, a door closing —
    the recogniser worked and there were no words in it. That is
    ``Transcript(text="")`` and never :class:`HearingError`, because a caller
    that cannot tell "nothing was said" from "the recogniser is broken" will
    eventually report one as the other to an operator.
    """

    text: str = ""
    language: str | None = None
    """BCP-47 if the recogniser knows it, ``None`` if it does not. A recogniser
    that was told which language to expect is not thereby an authority on what
    was actually spoken, so this stays optional."""

    confidence: float | None = None
    """The recogniser's own number, unnormalised and uninterpreted. ``None``
    from anything that does not produce one — which is most of them, and a
    fabricated 1.0 would be worse than an honest absence."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Whatever else the recogniser wants to carry. Core reads nothing from it;
    it exists so a recogniser with more to say does not need this dataclass
    edited to say it."""

    @property
    def is_empty(self) -> bool:
        """Whether anything was actually heard.

        Whitespace is nothing: a recogniser that returns ``" "`` for silence
        has said the same thing as one that returns ``""``, and one place to
        decide that beats every caller writing its own ``strip()``.
        """
        return not self.text.strip()


class RecogniserState(StrEnum):
    """Why audio can or cannot be turned into words right now.

    A closed set because the listen path branches on it, and the difference
    between "switched off" and "not in this build" is the difference between an
    operator flipping a switch and an operator not having the thing at all.
    """

    READY = "ready"
    """A recogniser is on. Audio becomes words."""

    NONE_CHOSEN = "none-chosen"
    """Nothing is switched on. Not a fault — that is every core until somebody
    flips a switch, and such a core is one you type at."""

    ENGINE_UNKNOWN = "engine-unknown"
    """Nothing in this build provides a recogniser by that id."""

    ENGINE_UNAVAILABLE = "engine-unavailable"
    """It exists but cannot run here. No switch is offered."""

    ENGINE_DISABLED = "engine-disabled"
    """The operator's switch is off."""

    ENGINE_FAILED = "engine-failed"
    """Switched on, but starting it failed. Degraded, and said out loud."""


@runtime_checkable
class Recogniser(Protocol):
    """What every recogniser implements.

    Lifecycle, and the whole reason several recognisers fit in one image — the
    same bargain :class:`personacore.voice.engine.Engine` makes:

    * A registered recogniser has done nothing. Constructing it must not load a
      model, open a runtime or read a file.
    * :meth:`start` is called when the operator switches it on.
    * :meth:`stop` releases everything. After it returns the recogniser holds no
      model in memory.
    """

    id: str
    display: str
    available: bool
    unavailable_reason: str | None

    def start(self) -> None:
        """Load whatever it needs to answer. Called when switched on."""
        ...

    def stop(self) -> None:
        """Release it. After this the recogniser holds no memory."""
        ...

    def transcribe(self, audio: Audio, **knobs: Any) -> Transcript:
        """Audio in, words out.

        Returns ``Transcript(text="")`` when there were no words in it — that
        is a success. Raises :class:`HearingError`, never a bare one, when the
        recogniser could not do the work at all.
        """
        ...


def looks_like_recogniser(candidate: object) -> str | None:
    """``None`` if ``candidate`` satisfies :class:`Recogniser`, else what is wrong.

    ``isinstance`` against a runtime-checkable protocol answers yes/no and
    nothing else, and "this object is not a recogniser" is useless to whoever
    has to fix it. This names the missing member instead, and is what the
    registry refuses a bad registration with.
    """
    missing = [
        name
        for name in ("id", "display", "available", "start", "stop", "transcribe")
        if not hasattr(candidate, name)
    ]
    if missing:
        return f"it has no {', '.join(missing)}"
    for name in ("start", "stop", "transcribe"):
        if not callable(getattr(candidate, name)):
            return f"its {name} is not callable"
    engine_id = getattr(candidate, "id", None)
    if not isinstance(engine_id, str) or not ENGINE_ID_PATTERN.match(engine_id):
        return (
            f"its id {engine_id!r} is not a usable engine id — lowercase letters, "
            "digits and dashes, up to 32 characters, e.g. 'whisper-onnx'"
        )
    return None
