"""The voice subsystem — ADR-0029.

Core owns voice. An engine is a switch, not a container: every supported engine
is built into the image, each has its own independent switch, and an engine
nobody switched on has loaded nothing.

Three modules, and the split between them is the ADR's own:

``engine``
    The contract every engine implements, and the data types crossing it.
``registry``
    Which engines exist, which are on, and starting and stopping them.
``library``
    Every installed voice across every engine as one list, and the answer to
    "can this persona's voice speak right now, and if not, what do I say".

Nothing in here may stop the core starting. Speech is the most detachable thing
in the system (ADR-0029 §6) and every failure path degrades speech alone.
"""

from personacore.voice.engine import (
    ENGINE_ID_PATTERN,
    VOICE_ID_PATTERN,
    Audio,
    Engine,
    EngineError,
    SkippedVoice,
    Voice,
    VoiceState,
    looks_like_engine,
)
from personacore.voice.library import (
    LibraryEntry,
    VoiceHealth,
    VoiceLibrary,
    VoiceListing,
    VoiceResolution,
    engine_voice_root,
    voice_health,
)
from personacore.voice.registry import (
    ApplyReport,
    EngineState,
    EngineStatus,
    VoiceRegistry,
    builtin_engines,
)

__all__ = [
    "ENGINE_ID_PATTERN",
    "VOICE_ID_PATTERN",
    "ApplyReport",
    "Audio",
    "Engine",
    "EngineError",
    "EngineState",
    "EngineStatus",
    "LibraryEntry",
    "SkippedVoice",
    "Voice",
    "VoiceHealth",
    "VoiceLibrary",
    "VoiceListing",
    "VoiceRegistry",
    "VoiceResolution",
    "VoiceState",
    "builtin_engines",
    "engine_voice_root",
    "looks_like_engine",
    "voice_health",
]
