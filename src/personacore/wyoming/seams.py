"""Everything this server needs from the rest of the core — and nothing else.

Both halves arrive as constructor arguments rather than as imports of a
concrete thing, for two reasons. The tests drive the real socket with fakes on
the far side, which is the only way to test the framing; and this package must
not take a decision that already belongs to speech or to hearing.

The **data types are the real ones**: :class:`personacore.hearing.Audio` and
:class:`personacore.hearing.Transcript`, imported rather than mirrored. The
hearing contract is explicit that the Wyoming surface "gets ``Audio`` in and
``Transcript`` out, and nothing else", and a local copy of a dataclass is how
two definitions of the same type start to drift.

The **registries are structural** — what is asked of an object, not which class
it has to be — so that :class:`~personacore.hearing.HearingRegistry` and
:class:`~personacore.voice.library.VoiceLibrary` satisfy them without either
module importing this one.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from personacore.hearing import Audio, Recogniser

#: What Home Assistant sends, always: it hardcodes 16 kHz 16-bit mono and
#: resamples upstream. Every other Wyoming client is normalised to it on the way
#: in — see :mod:`personacore.wyoming.handler`. It is also
#: :class:`~personacore.hearing.Audio`'s own default, which is not a
#: coincidence: it is the internal currency the hearing contract sets.
CAPTURE_RATE = 16000
CAPTURE_WIDTH = 2
CAPTURE_CHANNELS = 1

MINIMUM_SECONDS = 0.1
"""Below this, answer silence rather than calling a recogniser.

The first engine — Moonshine base — accepts 0.1 s to 64 s per call and raises
:class:`~personacore.hearing.HearingError` outside it. The bound belongs to the
engine and the engine enforces it; this is the other half of the contract's own
instruction that a caller who knows better should not feed it something it will
only refuse. A voice-activity detector that fired on a door closing produces
exactly this, and "nothing was said" is the true answer to it.
"""


@runtime_checkable
class HearingSource(Protocol):
    """The two methods of :class:`~personacore.hearing.HearingRegistry` used here.

    Every recogniser is a switch and every switch can be off, so this may
    legitimately have nothing running. What to report in that state is a
    protocol question, not a hearing question, and it is answered in
    :mod:`personacore.wyoming.describe` and :mod:`personacore.wyoming.handler`.
    """

    def running_ids(self) -> tuple[str, ...]:
        """Every recogniser that could transcribe right now, in id order."""

    def get(self, engine_id: str) -> Any:
        """That recogniser, or ``None``."""


@runtime_checkable
class VoiceSource(Protocol):
    """The two methods of :class:`~personacore.voice.library.VoiceLibrary` used here.

    ``snapshot`` rather than ``listing`` on purpose: ``describe`` must never
    touch the disk, and ``listing`` walks ``appdata/voices``.
    """

    def snapshot(self) -> Any:
        """The last voice listing, without reading the disk."""

    def resolve(self, engine_id: str | None, voice_id: str | None) -> Any:
        """Whether that voice can speak right now, and if not, the sentence."""


def current_recogniser(hearing: HearingSource | None) -> Recogniser | None:
    """Which recogniser answers, or ``None`` when nothing is switched on.

    ``running_ids`` is the hearing registry's own answer to "what could
    transcribe right now", so this asks it rather than re-deriving the rule
    from statuses and drifting from it. More than one may be running — every
    engine has an independent switch — and the first in id order wins, which is
    stable across restarts and does not depend on start order.
    """
    if hearing is None:
        return None
    for engine_id in hearing.running_ids():
        recogniser = hearing.get(engine_id)
        if recogniser is not None:
            return recogniser
    return None


def captured(data: bytes) -> Audio:
    """Bytes off the wire as the hearing contract's :class:`Audio`.

    Rate, channels and encoding are stated rather than defaulted even though
    the defaults match: this is the point where a protocol's promise becomes a
    contract's promise, and it should read as a claim somebody could check.
    """
    return Audio(
        data=data,
        sample_rate=CAPTURE_RATE,
        channels=CAPTURE_CHANNELS,
        encoding="pcm_s16le",
    )
