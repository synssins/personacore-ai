"""The hearing subsystem — the mirror of :mod:`personacore.voice`.

Core owns hearing. A recogniser is a switch, not a container: every supported
recogniser is built into the image, each has its own independent switch, and
one nobody switched on has loaded nothing.

Two modules, and the split between them is voice's own:

``engine``
    The contract every recogniser implements, and the data types crossing it.
``registry``
    Which recognisers exist, which are on, and starting and stopping them.

Nothing in here may stop the core starting. Hearing is as detachable as speech
and every failure path degrades hearing alone — a core that cannot listen is a
core you type at, which is what every core is today.
"""

from personacore.hearing.engine import (
    ENGINE_ID_PATTERN,
    Audio,
    HearingError,
    Recogniser,
    RecogniserState,
    Transcript,
    looks_like_recogniser,
)
from personacore.hearing.registry import (
    ApplyReport,
    EngineState,
    EngineStatus,
    HearingRegistry,
    builtin_engines,
)

__all__ = [
    "ENGINE_ID_PATTERN",
    "ApplyReport",
    "Audio",
    "EngineState",
    "EngineStatus",
    "HearingError",
    "HearingRegistry",
    "Recogniser",
    "RecogniserState",
    "Transcript",
    "builtin_engines",
    "looks_like_recogniser",
]
