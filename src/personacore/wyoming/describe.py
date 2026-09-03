"""The ``info`` event — what Home Assistant reads to decide what we are.

This is the trap-dense half of the protocol, so it has its own module and its
own tests. Every named trap below was verified against ``wyoming==1.10.2``'s
own dataclasses and Home Assistant's integration source, not remembered:

* **A text-to-speech program lists ``voices``, not ``models``.** The protocol's
  README says ``models``; the code says ``voices``, and Home Assistant raises
  ``TypeError`` on ``models``. The README is wrong.
* **``attribution`` and ``installed`` are required** on every program and every
  model or voice. They read like decoration and are positional dataclass
  arguments. Home Assistant does not catch the ``TypeError``, so an operator
  sees an unexplained crash rather than "cannot connect".
* **``installed=False``, or an empty ``languages`` list, produces an entity
  that appears in Home Assistant and can never be selected** — supported
  languages are built only from installed models. So a half we cannot actually
  provide is *omitted*, never advertised as not-installed.
* **No ``satellite`` block, ever.** If one is present and installed, Home
  Assistant returns before registering any platform at all: no speech-to-text,
  no text-to-speech, nothing. It is absent by construction here — nothing in
  this module can set it.
* **``asr[0]`` is used unconditionally**, not "the first installed one". Only
  ever one program is listed.
* **``supports_synthesize_streaming`` is advertised, and the replay it turns on
  is the client obeying the protocol rather than a quirk.** This bullet used to
  say the opposite and it cost the feature a week. The observation was right —
  a streaming client does send the whole message again as a plain
  ``synthesize`` — but the conclusion was not: the protocol *requires* that of
  every client ("Original ``synthesize`` message must be sent for backwards
  compatibility"), so that a server which understood only ``synthesize`` still
  receives a complete request. The server is what makes it harmless, in one
  line: while a stream is open, drop it
  (:class:`personacore.wyoming.handler.PersonaCoreEventHandler`). Withholding
  the flag did not avoid the problem, it avoided the feature.
"""

from __future__ import annotations

from typing import Any

from wyoming.info import AsrModel, AsrProgram, Attribution, Info, TtsProgram, TtsVoice

from personacore.wyoming.seams import HearingSource, VoiceSource, current_recogniser

ATTRIBUTION = Attribution(
    name="PersonaCore",
    url="https://github.com/synssins/personacore-ai",
)
"""Who to blame, on every program and every voice. Required, both fields."""

ASR_PROGRAM_NAME = "personacore"
ASR_MODEL_NAME = "personacore"

TTS_PROGRAM_NAME = "personacore"

DEFAULT_LANGUAGES = ("en",)
"""What a voice or a recogniser is assumed to speak when it does not say.

Never empty: a model with no languages is an entity Home Assistant shows and
nobody can select. Guessing "English" and being wrong is recoverable — the
operator picks a different voice. Guessing nothing is not.
"""


def _field(holder: Any, name: str) -> Any:
    """One named value off an object or out of a mapping."""
    if isinstance(holder, dict):
        return holder.get(name)
    return getattr(holder, name, None)


def _languages(source: Any) -> list[str]:
    """The languages an object claims, or :data:`DEFAULT_LANGUAGES`.

    Read generously, the way :meth:`personacore.voice.engine.Voice.adopt` reads
    an engine's voice: ``languages`` (a list) or ``language`` (one), on the
    object or in its ``metadata``. A voice pack that says nothing costs its
    owner nothing.
    """
    for holder in (source, getattr(source, "metadata", None) or {}):
        many = _field(holder, "languages")
        if isinstance(many, list | tuple):
            named = [one for one in many if isinstance(one, str) and one]
            if named:
                return named
        one = _field(holder, "language")
        if isinstance(one, str) and one:
            return [one]
    return list(DEFAULT_LANGUAGES)


def asr_program(
    hearing: HearingSource | None, *, version: str | None = None
) -> AsrProgram | None:
    """The speech-to-text half, or ``None`` when no recogniser is switched on.

    ``None`` rather than ``installed=False`` on purpose — see the module
    docstring. A core that is not listening simply does not offer
    speech-to-text, and Home Assistant registers only the other entity.

    Not permanent, either: every recogniser is a switch, ``describe`` comes
    back in thirty seconds, and one switched on in the admin UI is advertised
    on the next one without a restart.
    """
    recogniser = current_recogniser(hearing)
    if recogniser is None:
        return None
    return AsrProgram(
        name=ASR_PROGRAM_NAME,
        description="Speech to text, by PersonaCore.",
        attribution=ATTRIBUTION,
        installed=True,
        version=version,
        models=[
            AsrModel(
                # The recogniser's own id — `moonshine`, `none` — so an
                # operator reading Home Assistant's entity can tell which of
                # this core's switches is answering.
                name=getattr(recogniser, "id", None) or ASR_MODEL_NAME,
                description=getattr(recogniser, "display", None) or ASR_MODEL_NAME,
                attribution=ATTRIBUTION,
                installed=True,
                version=version,
                languages=_languages(recogniser),
            )
        ],
    )


def tts_program(voices: VoiceSource | None, *, version: str | None = None) -> TtsProgram | None:
    """The text-to-speech half, or ``None`` when nothing can speak yet.

    Built from :meth:`~personacore.voice.library.VoiceLibrary.snapshot`, which
    never reads the disk — ``describe`` arrives every thirty seconds, forever,
    with a five-second timeout, and a filesystem walk on that path is how a
    working service starts reporting itself unavailable.

    An empty snapshot is therefore not an error and not permanent: the core
    walks the voice directory at startup and after every settings save, and
    Home Assistant asks again in thirty seconds.
    """
    if voices is None:
        return None
    entries = tuple(voices.snapshot().speakable())
    if not entries:
        return None
    return TtsProgram(
        name=TTS_PROGRAM_NAME,
        description="Text to speech, in this core's own voices.",
        attribution=ATTRIBUTION,
        installed=True,
        version=version,
        # Speech starts on the first finished sentence instead of after the
        # whole reply. True only because the handler implements the whole of
        # it — the audio groups, the dropped compatibility
        # `synthesize`, and `synthesize-stopped` at the end, which is the only
        # thing that ends a streamed response for the client. A server that
        # advertises this and misses any one of them is worse than one that
        # never advertised it: the client takes the streaming path for *every*
        # utterance once this is true, including a one-line announcement.
        supports_synthesize_streaming=True,
        # `voices`, never `models`. The one field name that silently breaks
        # Home Assistant's setup rather than this server's.
        voices=[
            TtsVoice(
                # `engine/voice` — the core's own unique name for a voice
                # (`LibraryEntry.key`). A bare voice id is not unique: two
                # engines may each ship one called `glados`, and resolving that
                # by guessing would speak in a different voice depending on
                # which engines happened to be switched on.
                name=entry.key,
                description=entry.label,
                attribution=ATTRIBUTION,
                installed=True,
                version=version,
                languages=_languages(entry.voice),
            )
            for entry in entries
        ],
    )


def build_info(
    *,
    hearing: HearingSource | None = None,
    voices: VoiceSource | None = None,
    version: str | None = None,
) -> Info:
    """The whole ``info`` event. Cheap enough to build on every ``describe``.

    Built per request rather than captured once so that a voice installed while
    the core runs is advertised without a restart — the reference Piper server
    does the same thing for the same reason.
    """
    asr = asr_program(hearing, version=version)
    tts = tts_program(voices, version=version)
    return Info(
        asr=[asr] if asr is not None else [],
        tts=[tts] if tts is not None else [],
        # Everything else stays empty. `satellite` in particular: see above.
    )
