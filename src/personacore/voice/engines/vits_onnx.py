"""`vits-onnx` — Piper-format VITS models run on onnxruntime, CPU only.

ADR-0029: core owns voice and an engine is a switch. This module is that switch's
other end. It carries no HTTP surface, no port and no container of its own — it
is a class the registry constructs, starts, stops, and asks two questions:
what can you speak, and speak this.

Three things happen here, and one of them lives next door:

* **Discovery.** A voice folder is read *exactly as it was downloaded* — a
  ``.onnx`` and its JSON config, and nothing else is ever required (PC-331). A
  folder that cannot be read is skipped and named, never fatal.
* **Phonemisation**, in :mod:`.espeak`, where the two measured rules are.
* **Synthesis.** One onnxruntime session per voice, loaded on first use and
  released in full by :meth:`VitsOnnxEngine.stop` (PC-335).

**CPU only, permanently.** The provider list handed to onnxruntime is literally
``["CPUExecutionProvider"]`` — not "whatever is available", which is how a GPU
path gets in by accident on somebody else's machine. The package is
``onnxruntime``; the ``-gpu`` build is a defect, not an optimisation.

**No voice assets, no model and no lexicon are in the image.** A voice is ~60 MB
of third-party data with its own licence and it arrives in appdata.
"""

from __future__ import annotations

import gc
import json
import logging
import threading
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# The shared vocabulary of ADR-0029's interface addendum. `Audio` there is raw
# PCM rather than a container: the streaming and framing decisions are core's,
# and an engine that returned a WAV would have made them on core's behalf.
from ..engine import (
    SYNTHESIS_FIELDS,
    SYNTHESIS_LIMITS,
    Audio,
    EngineError,
    synthesis_refusal,
)
from . import espeak

_log = logging.getLogger(__name__)

ENGINE_ID = "vits-onnx"
ENGINE_DISPLAY = "VITS ONNX"


MAX_CONFIG_BYTES = 8 * 1024 * 1024
"""A Piper config is a few kilobytes; the biggest part of it is the phoneme map.
Anything vastly larger is a malfunction or an attempt to make us chew through
memory, and it is refused unread."""

DEFAULT_INFERENCE = {"length_scale": 1.0, "noise_scale": 0.667, "noise_w": 0.8}
"""Used only when a config omits them. Piper's own defaults."""

MAX_ABS = 32767.0
"""Full scale for 16-bit PCM. The model emits float in roughly [-1, 1]."""

SCALE_LIMITS = dict(SYNTHESIS_LIMITS)
"""(low, high) per knob — the contract's table (``voice/engine.py``), adopted
rather than restated.

The format says ranges are the engine's to validate, and they are: this engine
is what decides a length scale of 40 is not speech. They live one module up so
that the installer refuses such a value while the operator is still looking at
the form, instead of it being written to disk and quietly clamped hours later.
"""

DEFAULT_MAX_CHARACTERS = 2000

DEFAULT_STREAMING_MARGIN = 1.2

DEFAULT_CALIBRATION_TEXT = "The cake is a lie, and so is the timing."
"""The clause the streaming measurement is taken on.

**A sentence, and never a single word.** A Piper-format VITS model mispronounces
short isolated utterances even when the phonemes handed to it are correct —
"about" comes back as "bout", "put" as "putt" — which is the model's behaviour on
very short inputs and not something this engine tries to correct
(``docs/pronunciation-guide.md``). Timing a bare word would measure that worst
case instead of the work streaming speech actually does.
"""


# --------------------------------------------------------------------------
# What a voice is, to this engine
# --------------------------------------------------------------------------


class VoiceUnreadable(Exception):
    """This folder is not a usable voice, and the message says why in English."""


@dataclass(frozen=True)
class Voice:
    """One voice folder, read but not loaded. The model file is not opened here.

    The engine's **own** record, not :class:`personacore.voice.engine.Voice`.
    Core adopts this into its shape for the single list and hands the original
    straight back at :meth:`VitsOnnxEngine.speak` (``Voice.adopt`` and
    ``speakable_object`` there), so a model path and a phoneme map never have to
    become core's business and speaking never re-reads the folder.
    """

    voice_id: str
    engine_id: str = ENGINE_ID
    directory: Path = field(default_factory=Path)
    model_path: Path = field(default_factory=Path)
    config_path: Path = field(default_factory=Path)
    sample_rate: int = 22050
    espeak_voice: str = espeak.DEFAULT_ESPEAK_VOICE
    phoneme_id_map: Mapping[str, list[int]] = field(default_factory=dict)
    inference: Mapping[str, float] = field(default_factory=dict)
    overrides: Mapping[str, str] = field(default_factory=dict)
    num_speakers: int = 1
    name: str = ""
    language: str = ""
    licence: str = ""
    description: str = ""
    notes: tuple[str, ...] = ()
    """What ``voice.toml`` asked for that this engine did not do, in sentences.

    Empty for almost every voice. It carries the one thing a silently-ignored
    setting cannot: a value the operator typed, a value the engine used, and
    which of the two the voice is speaking with. Each note is also logged when
    the folder is read, so it exists whether or not a screen renders it.
    """

    @property
    def display_name(self) -> str:
        return self.name or self.voice_id

    def summary(self) -> dict[str, Any]:
        """What the core says about this voice. No paths: an operator's directory
        layout is not the admin UI's business, and it is not the model's."""
        return {
            "id": self.voice_id,
            "name": self.display_name,
            "engine": self.engine_id,
            "sample_rate": self.sample_rate,
            "language": self.language,
            "licence": self.licence,
            "description": self.description,
            "speakers": self.num_speakers,
            "pronunciation_overrides": len(self.overrides),
            "defaults": dict(self.inference),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SkippedVoice:
    """A folder that looked like a voice and was not. Named, with the reason."""

    voice_id: str
    reason: str

    def summary(self) -> dict[str, str]:
        return {"id": self.voice_id, "reason": self.reason}


@dataclass(frozen=True)
class Spoken:
    """One synthesis: the audio core receives, and what it cost to make.

    :meth:`VitsOnnxEngine.speak` returns only the :class:`Audio` — that is the
    contract. The timings stay on this side because the streaming declaration
    (PC-262) has to be *measured*, and measuring means keeping the wall clock
    the sentence took rather than asking for it back later.

    ``realtime_factor`` is seconds of speech produced per second of wall clock.
    Above 1.0 the engine generates faster than the audio plays.
    """

    audio: Audio
    audio_seconds: float
    elapsed_seconds: float
    phonemes: str
    voice_id: str = ""

    @property
    def sample_rate(self) -> int:
        return self.audio.sample_rate

    @property
    def realtime_factor(self) -> float:
        if self.elapsed_seconds <= 0:
            return float("inf")
        return self.audio_seconds / self.elapsed_seconds


@dataclass(frozen=True)
class Streaming:
    """The PC-262 declaration, measured on this machine and never asserted."""

    streaming_synthesis: bool
    realtime_factor: float | None
    margin_required: float
    measured: bool
    reason: str
    measured_with: Mapping[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "engine": ENGINE_ID,
            "streaming_synthesis": self.streaming_synthesis,
            "realtime_factor": self.realtime_factor,
            "margin_required": self.margin_required,
            "measured": self.measured,
            "reason": self.reason,
            "measured_with": dict(self.measured_with) if self.measured_with else None,
        }


# --------------------------------------------------------------------------
# Reading a voice folder, exactly as it was downloaded
# --------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise VoiceUnreadable(
            f"{path.name} is {path.stat().st_size} bytes, which is far too large "
            "for a voice config; it was not read."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise VoiceUnreadable(f"{path.name} is not UTF-8 text: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise VoiceUnreadable(f"{path.name} is not valid JSON: {exc}") from exc


def find_model(directory: Path) -> Path:
    """The one ``.onnx`` in the folder.

    More than one is refused rather than guessed at, unless exactly one of them
    is named after the folder — which is the case where the guess is not a
    guess. Getting this wrong means speaking in the wrong voice, silently.
    """
    models = sorted(p for p in directory.glob("*.onnx") if p.is_file())
    if not models:
        raise VoiceUnreadable("there is no .onnx model file in this folder")
    if len(models) == 1:
        return models[0]
    named = [p for p in models if p.stem == directory.name]
    if len(named) == 1:
        return named[0]
    raise VoiceUnreadable(
        "there are "
        + str(len(models))
        + " .onnx files here ("
        + ", ".join(p.name for p in models)
        + ") and none is named after the folder, so which one is the voice is a "
        "guess. Put one voice in one folder."
    )


def find_config(model_path: Path) -> Path:
    """The model's config: ``<model>.onnx.json`` first, then ``<model>.json``.

    Stock Piper ships the first. The GLaDOS assets use the second. Both are
    accepted here so that neither has to be renamed, which is the whole point of
    PC-331 — a voice works as downloaded or the guarantee is not a guarantee.
    """
    candidates = [
        model_path.with_name(model_path.name + ".json"),
        model_path.with_suffix(".json"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise VoiceUnreadable(
        f"there is no config beside {model_path.name}. A Piper voice is two "
        f"files: the model and its JSON config, named either "
        f"{candidates[0].name} or {candidates[1].name}."
    )


def _phoneme_id_map(config: Mapping[str, Any], config_name: str) -> dict[str, list[int]]:
    raw = config.get("phoneme_id_map")
    if not isinstance(raw, Mapping) or not raw:
        raise VoiceUnreadable(
            f"{config_name} has no phoneme_id_map. That table is the model's own "
            "vocabulary and there is no substitute for it — a separate map file "
            "from somewhere else would produce noise, not this voice."
        )
    id_map: dict[str, list[int]] = {}
    for symbol, ids in raw.items():
        if not isinstance(symbol, str):
            continue
        if isinstance(ids, int) and not isinstance(ids, bool):
            id_map[symbol] = [ids]
        elif isinstance(ids, list) and all(isinstance(i, int) for i in ids):
            id_map[symbol] = list(ids)
    missing = [token for token in (espeak.BOS, espeak.EOS, espeak.PAD) if token not in id_map]
    if missing:
        raise VoiceUnreadable(
            f"{config_name}'s phoneme_id_map is missing {', '.join(repr(m) for m in missing)}. "
            "Those are the start, end and padding symbols every Piper voice uses; "
            "without them the model cannot be given a sentence."
        )
    return id_map


def _inference(config: Mapping[str, Any]) -> dict[str, float]:
    raw = config.get("inference")
    values = dict(DEFAULT_INFERENCE)
    if isinstance(raw, Mapping):
        for key in values:
            candidate = raw.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                values[key] = float(candidate)
    return values


def _voice_toml(directory: Path) -> Mapping[str, Any]:
    """The parsed ``voice.toml``, or an empty mapping. Never raises.

    Optional means optional (ADR-0029 §4): a missing file or a malformed one
    leaves the voice working with its folder name and no licence recorded. This
    file adds labels and defaults; it never gates speech and it is never
    required.
    """
    path = directory / "voice.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _log.warning("voice %s: ignoring voice.toml, it does not parse (%s)", directory.name, exc)
        return {}


def _table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    table = document.get(name)
    return table if isinstance(table, Mapping) else {}


def _synthesis_defaults(
    document: Mapping[str, Any], base: Mapping[str, float]
) -> tuple[dict[str, float], list[str]]:
    """``[synthesis]`` laid over the model config's own ``inference``.

    **This is the half of PC-339 that was missing.** The manage screen writes
    length scale, noise scale and noise w into ``voice.toml``; until now nothing
    read them, so an operator moved a speed control, saved, heard no difference
    and had no way to learn the field was never connected. These are the
    defaults every sentence is spoken with unless a caller overrides them per
    request (:func:`clamp_scales`).

    ``docs/voice-pack-format.md`` puts them under ``[synthesis]`` and nowhere
    else, so that is the only place looked at — a second location would be a
    second thing to keep in step.

    The order is deliberate: the model's config is the base, because it is the
    voice's author speaking, and ``voice.toml`` is on top, because it is the
    operator speaking and they are the later authority. Ranges are this
    engine's (:data:`SCALE_LIMITS`) and are enforced here rather than trusted:
    the installer already refuses a nonsense value at the form, and this is what
    catches one written into the file by hand — clamped to the nearest usable
    figure with a sentence saying so, never applied silently and never carried
    into the model to fail mid-speech.
    """
    values = dict(base)
    notes: list[str] = []
    table = _table(document, "synthesis")
    for field_name in SYNTHESIS_FIELDS:
        if field_name not in table:
            continue
        candidate = table[field_name]
        if isinstance(candidate, str) and not candidate.strip():
            # An export writes every field, and the ones nobody filled in carry
            # `""`. Blank is "not set", exactly as a missing key is.
            continue
        refusal = synthesis_refusal(field_name, candidate)
        if refusal is None:
            values[field_name] = float(candidate)
            continue
        low, high = SCALE_LIMITS[field_name]
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            number = float(candidate)
            if number == number and number not in (float("inf"), float("-inf")):
                used = min(high, max(low, number))
                values[field_name] = used
                notes.append(
                    f"voice.toml asks for {field_name} = {number}, which is outside "
                    f"{low} to {high}; this voice speaks at {used}."
                )
                continue
        notes.append(
            f"voice.toml's {field_name} is not a number, so this voice speaks at "
            f"{values.get(field_name, DEFAULT_INFERENCE[field_name])}."
        )
    return values, notes


def _sample_rate_note(document: Mapping[str, Any], produced: int) -> str | None:
    """A ``voice.toml`` that disagrees with the model about its own sample rate.

    The figure is **not** honoured, and this is the one field on the manage
    screen that cannot be: the rate is what the model emits, stated in its own
    config, and this engine has no resampler. Writing a different number would
    not slow the voice down — it would mislabel the audio, which is the
    difference between a voice and a chipmunk.

    So the model's config wins and the disagreement is said out loud rather
    than swallowed. The control belongs off the screen; until it goes, this is
    what stops it lying.
    """
    declared = _table(document, "audio").get("sample_rate")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared <= 0:
        return None
    if declared == produced:
        return None
    return (
        f"voice.toml says the sample rate is {declared} Hz; the model's own config "
        f"says {produced} Hz, which is what it actually produces, so that is what "
        "is used. The rate is the model's to state, not a setting."
    )


def _metadata(document: Mapping[str, Any]) -> dict[str, str]:
    """The text labels out of a parsed ``voice.toml``.

    A field that is absent, blank or of the wrong type is simply not there,
    which is the same state as never having been filled in.
    """
    voice = _table(document, "voice")
    licence = _table(document, "licence") or _table(document, "license")
    out: dict[str, str] = {}
    for key, source, source_key in (
        ("name", voice, "name"),
        ("language", voice, "language"),
        ("description", voice, "description"),
        ("licence", licence, "spdx"),
    ):
        value = source.get(source_key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:200]
    return out


def read_voice(directory: Path) -> Voice:
    """Read one voice folder. Raises :class:`VoiceUnreadable` with a reason."""
    model_path = find_model(directory)
    config_path = find_config(model_path)
    config = _read_json(config_path)
    if not isinstance(config, Mapping):
        raise VoiceUnreadable(f"{config_path.name} is JSON, but not an object")

    audio = config.get("audio") if isinstance(config.get("audio"), Mapping) else {}
    sample_rate = audio.get("sample_rate")
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
        raise VoiceUnreadable(
            f"{config_path.name} does not say what sample rate this model "
            "produces (audio.sample_rate). Guessing it is the difference between "
            "a voice and a chipmunk, so it is not guessed."
        )

    espeak_table = config.get("espeak") if isinstance(config.get("espeak"), Mapping) else {}
    language = config.get("language") if isinstance(config.get("language"), Mapping) else {}
    espeak_voice = (
        espeak_table.get("voice") or language.get("code") or espeak.DEFAULT_ESPEAK_VOICE
    )
    if not isinstance(espeak_voice, str):
        espeak_voice = espeak.DEFAULT_ESPEAK_VOICE

    num_speakers = config.get("num_speakers")
    if not isinstance(num_speakers, int) or isinstance(num_speakers, bool) or num_speakers < 1:
        num_speakers = 1

    overrides: dict[str, str] = {}
    pronunciation = directory / "pronunciation.json"
    if pronunciation.is_file():
        try:
            overrides = espeak.load_overrides(_read_json(pronunciation))
        except VoiceUnreadable as exc:
            # A broken pronunciation file costs its overrides, not the voice.
            _log.warning("voice %s: ignoring pronunciation.json (%s)", directory.name, exc)

    document = _voice_toml(directory)
    meta = _metadata(document)
    inference, notes = _synthesis_defaults(document, _inference(config))
    rate_note = _sample_rate_note(document, sample_rate)
    if rate_note:
        notes.append(rate_note)
    for note in notes:
        _log.warning("voice %s: %s", directory.name, note)
    return Voice(
        voice_id=directory.name,
        engine_id=ENGINE_ID,
        directory=directory,
        model_path=model_path,
        config_path=config_path,
        sample_rate=sample_rate,
        espeak_voice=espeak_voice,
        phoneme_id_map=_phoneme_id_map(config, config_path.name),
        inference=inference,
        overrides=overrides,
        num_speakers=num_speakers,
        name=meta.get("name", ""),
        language=meta.get("language", "")
        or (language.get("code") if isinstance(language.get("code"), str) else ""),
        licence=meta.get("licence", ""),
        description=meta.get("description", ""),
        notes=tuple(notes),
    )


def discover(root: Path) -> tuple[list[Voice], list[SkippedVoice]]:
    """Every voice under ``root``, and every folder that failed, with its reason.

    **Never raises.** A missing root is an empty list and a log line — an engine
    with no voices yet is a normal state on a fresh install, not a crash — and
    one unreadable folder must not cost an operator the other nine. This is the
    lockout class this project has produced three times already.
    """
    voices: list[Voice] = []
    skipped: list[SkippedVoice] = []
    try:
        if not root.is_dir():
            _log.warning(
                "voices directory %s does not exist, so no voice can be found. "
                "Install a voice through the admin UI, or check the mount.",
                root,
            )
            return voices, skipped
        resolved_root = root.resolve()
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        _log.warning("voices directory %s could not be listed: %s", root, exc)
        return voices, skipped

    for directory in entries:
        if directory.name.startswith("."):
            continue
        try:
            # An engine never reads outside its own voices directory. A folder
            # that is a link to somewhere else is refused rather than followed:
            # a voice archive is third-party data and this is the last place
            # that stays true.
            if directory.resolve().parent != resolved_root:
                raise VoiceUnreadable(
                    "this folder is a link pointing outside the voices "
                    "directory, and an engine only reads its own."
                )
            voice = read_voice(directory)
        except VoiceUnreadable as exc:
            _log.warning("voice %s skipped: %s", directory.name, exc)
            skipped.append(SkippedVoice(directory.name, str(exc)))
            continue
        except OSError as exc:
            reason = f"the folder could not be read: {exc}"
            _log.warning("voice %s skipped: %s", directory.name, reason)
            skipped.append(SkippedVoice(directory.name, reason))
            continue
        voices.append(voice)
        _log.info(
            "voice %s loaded from %s at %d Hz, %d pronunciation override(s)",
            voice.voice_id,
            voice.model_path.name,
            voice.sample_rate,
            len(voice.overrides),
        )
    return voices, skipped


# --------------------------------------------------------------------------
# Running the model
# --------------------------------------------------------------------------


class InferenceSession(Protocol):
    """The slice of onnxruntime this module uses, so tests need no model file."""

    def get_inputs(self) -> Sequence[Any]: ...

    def run(
        self, output_names: Sequence[str] | None, input_feed: Mapping[str, Any]
    ) -> Sequence[Any]: ...


def onnxruntime_is_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("onnxruntime") is not None


def open_session(model_path: str) -> InferenceSession:
    """One onnxruntime session, pinned to the CPU provider.

    Imported here rather than at module scope so that the parts of this engine
    that do not run a model — discovery, phonemisation, availability — can be
    imported and tested without onnxruntime installed, and so that an image
    missing it says so in a sentence instead of failing to import.
    """
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - the image always has it
        raise EngineError(
            "onnxruntime is not installed in this container, so no voice can be "
            "run. The image is meant to carry it; this build did not."
        ) from exc

    options = ort.SessionOptions()
    options.log_severity_level = 3
    try:
        return ort.InferenceSession(
            model_path,
            sess_options=options,
            # CPU ONLY, and stated rather than discovered. Every container in
            # this project is CPU-only, always (CLAUDE.md); passing the
            # available-provider list instead would silently pick up a GPU on
            # somebody's machine and make this engine behave differently there.
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own types
        raise EngineError(f"the model file could not be loaded: {exc}") from exc


def clamp_scales(
    defaults: Mapping[str, float], requested: Mapping[str, float | None]
) -> dict[str, float]:
    """The voice's own ``inference`` values, with any per-request override.

    A request may override; it may not go outside :data:`SCALE_LIMITS`. Out of
    range is clamped rather than refused: these are expressive knobs, and a
    caller asking for a slightly-too-slow voice wants speech, not an error.
    """
    out = {key: float(defaults.get(key, 1.0)) for key in SCALE_LIMITS}
    for key, (low, high) in SCALE_LIMITS.items():
        value = requested.get(key)
        if value is None:
            continue
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            continue
        out[key] = min(high, max(low, candidate))
    return out


def to_pcm_s16le(samples: Any) -> bytes:
    """float32 mono -> little-endian 16-bit PCM samples, and nothing around them.

    No container. :class:`personacore.voice.engine.Audio` is raw samples plus
    the rate needed to play them, because framing them into a WAV would be this
    engine making core's streaming decision for it.

    Clipped, not normalised. Normalising would make one loud sentence quieten
    the next, which across a stream of clauses is audible as pumping.
    """
    import numpy as np

    flat = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = np.clip(flat * MAX_ABS, -MAX_ABS, MAX_ABS).astype("<i2")
    return pcm.tobytes()


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class VitsOnnxEngine:
    """Piper-format VITS on onnxruntime, as one switch (ADR-0029).

    Constructing it costs nothing: no model is opened, no runtime is imported,
    no thread is started. That is what makes four engines in one image
    defensible — an engine nobody turns on costs disk and nothing else.

    ``session_factory`` and ``espeak_runner`` are the two seams. Tests pass a
    stub session and a stub phonemiser and exercise everything around the model
    without a 60 MB file or a phonemiser installed.
    """

    id = ENGINE_ID
    display = ENGINE_DISPLAY

    def __init__(
        self,
        *,
        session_factory: Any = open_session,
        espeak_runner: espeak.EspeakRunner | None = None,
        espeak_binary: str = espeak.ESPEAK_BINARY,
        max_characters: int = DEFAULT_MAX_CHARACTERS,
        streaming_margin: float = DEFAULT_STREAMING_MARGIN,
        calibration_text: str = DEFAULT_CALIBRATION_TEXT,
    ) -> None:
        self._session_factory = session_factory
        # Both seams are also the reason a test does not need the real thing
        # installed: a stub session factory means onnxruntime is not what will
        # be run, and a stub runner means espeak-ng is not what will be called,
        # so neither is required for this engine to be available.
        self._needs_onnxruntime = session_factory is open_session
        self._espeak_runner = espeak_runner
        self._espeak_binary = espeak_binary
        self.max_characters = max_characters
        self.streaming_margin = streaming_margin
        self.calibration_text = calibration_text

        self._lock = threading.Lock()
        self._sessions: dict[str, InferenceSession] = {}
        self._voices: dict[str, Voice] = {}
        self._streaming: Streaming | None = None
        self._started = False

        self.available, self.unavailable_reason = self._availability()

    # -- availability ------------------------------------------------------

    def _availability(self) -> tuple[bool, str | None]:
        """Can this *build*, on this *hardware*, run this engine at all?

        Not the operator's switch (ADR-0029: "available is not enabled"). It is
        answered once, here, and never changes at runtime. Both halves are
        checked by looking, not by importing or executing: a missing espeak-ng
        must produce this sentence rather than an exception on the way to it.
        """
        missing: list[str] = []
        if self._needs_onnxruntime and not onnxruntime_is_installed():
            missing.append("onnxruntime, the model runtime")
        if self._espeak_runner is None and not espeak.espeak_is_installed(self._espeak_binary):
            missing.append(f"{self._espeak_binary}, the phonemiser")
        if not missing:
            return True, None
        return False, (
            "This build cannot run the VITS ONNX engine because "
            + " and ".join(missing)
            + " is not installed in it. The image is meant to carry both; this "
            "one does not, so the engine offers no switch rather than a switch "
            "that starts something which cannot speak."
        )

    # -- the switch --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._started

    def start(self) -> None:
        """Load whatever the engine needs to answer.

        The model runtime is imported here, so a broken install fails the switch
        rather than the first sentence somebody tries to say. Voice *models* are
        not loaded: a voice is tens of megabytes, an operator may have ten, and
        loading all of them to turn one engine on would make the switch mean
        something nobody asked for. They load on first use and :meth:`stop`
        releases them.
        """
        if not self.available:
            raise EngineError(self.unavailable_reason or "this engine is not available")
        with self._lock:
            if self._started:
                return
        if self._needs_onnxruntime:
            try:
                import onnxruntime  # noqa: F401
            except ImportError as exc:  # pragma: no cover - guarded by `available`
                raise EngineError(
                    "onnxruntime is not installed in this container, so no voice "
                    "can be run. The image is meant to carry it; this build did not."
                ) from exc
        with self._lock:
            self._started = True
        _log.info("engine %s started", self.id)

    def stop(self) -> None:
        """Release it. After this the engine holds no memory (PC-335).

        Off means off. Every onnxruntime session is dropped, and with it the
        weights — a session is the only thing here that holds a model, and
        nothing else keeps a reference to one. The collector is run explicitly
        because a session's arena is native memory freed when the Python object
        is finalised, and waiting for the next natural collection would make
        "off" mean "off shortly".

        Calling it twice, or on an engine that was never started, is fine and
        does nothing.
        """
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._voices.clear()
            self._streaming = None
            self._started = False
        # Popped rather than iterated, and the loop variable deleted with each
        # one: `for session in sessions` leaves the last session bound to a
        # local until this frame ends, which would make the collection below
        # free every model except one — the sort of "released" that measures
        # 60 MB short.
        while sessions:
            session = sessions.pop()
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # noqa: BLE001 - a failed close is not fatal
                    _log.warning("a voice's model would not close cleanly: %s", exc)
            del closer, session
        del sessions
        gc.collect()
        _log.info("engine %s stopped; no model is held", self.id)

    def loaded_voice_ids(self) -> list[str]:
        """Which models are in memory right now. Empty after :meth:`stop`."""
        with self._lock:
            return sorted(self._sessions)

    # -- what it can speak -------------------------------------------------

    def voices(self, root: Path) -> tuple[list[Voice], list[SkippedVoice]]:
        """What this engine can speak, and what it could not read and why.

        Never raises for a bad voice folder, and never for a missing directory.
        Reads only ``root``, which is the engine's own
        ``appdata/voices/vits-onnx/``.
        """
        found, skipped = discover(Path(root))
        with self._lock:
            self._voices = {voice.voice_id: voice for voice in found}
        return found, skipped

    def _known(self, voice_id: str) -> Voice | None:
        with self._lock:
            return self._voices.get(voice_id)

    # -- speaking ----------------------------------------------------------

    def speak(self, voice: Voice, text: str, **knobs: Any) -> Audio:
        """Text in, audio out. Raises :class:`EngineError`, never a bare exception.

        ``knobs`` are ``length_scale`` (higher is slower), ``noise_scale``
        (expressiveness), ``noise_w`` (pitch variance) and ``speaker`` (a
        multi-speaker model only). Anything else is refused rather than ignored:
        a misspelled ``lenght_scale`` that silently did nothing is worse to
        diagnose than one that said so.
        """
        return self.synthesise(voice, text, **knobs).audio

    def synthesise(self, voice: Voice, text: str, **knobs: Any) -> Spoken:
        """:meth:`speak`, plus the timings and the phonemes it went through.

        The protocol asks for audio and nothing else, so this is what the
        streaming measurement and anyone diagnosing a pronunciation use, and
        :meth:`speak` is a one-line wrapper over it.
        """
        # The switch is checked before anything else, so an operator who turned
        # the engine off reads that rather than a second-order complaint about
        # a voice the stopped engine no longer has in hand.
        if not self._started:
            raise EngineError(
                f"the {self.display} engine is switched off, so it cannot speak. "
                "Turn it on in Settings and the voice works again."
            )
        voice = self._resolve(voice)
        if not isinstance(text, str) or not text.strip():
            raise EngineError("there was no text to say")
        if len(text) > self.max_characters:
            raise EngineError(
                f"that is {len(text)} characters and this engine speaks at most "
                f"{self.max_characters} in one go. Send it a clause at a time — "
                "which is how streaming speech works anyway."
            )
        unknown = sorted(set(knobs) - {"length_scale", "noise_scale", "noise_w", "speaker"})
        if unknown:
            raise EngineError(
                "this engine has no setting called " + ", ".join(repr(k) for k in unknown) + "."
            )

        try:
            phonemes = self._phonemiser(voice).phonemise(text)
        except espeak.PhonemiserUnavailable as exc:
            raise EngineError(str(exc)) from exc
        ids = espeak.phonemes_to_ids(phonemes, voice.phoneme_id_map)
        if not ids:
            raise EngineError("there was nothing to say once the text was phonemised")
        scales = clamp_scales(voice.inference, knobs)
        speaker = knobs.get("speaker") or 0
        return self._run(voice, phonemes, ids, scales, int(speaker))

    def _resolve(self, voice: Any) -> Voice:
        """Accept this engine's own :class:`Voice`, or anything naming one.

        The registry hands back what :meth:`voices` gave it, which is the first
        branch. The second exists so a voice identified only by id — from a
        persona's saved setting, say — still resolves rather than becoming a
        type error the operator has to read.
        """
        if isinstance(voice, Voice):
            return voice
        voice_id = getattr(voice, "voice_id", None) or getattr(voice, "id", None)
        known = self._known(voice_id) if isinstance(voice_id, str) else None
        if known is not None:
            return known
        raise EngineError(
            f"the {self.display} engine has no voice called {voice_id!r} loaded. "
            "Ask it for its voices before asking it to speak."
        )

    def _phonemiser(self, voice: Voice) -> espeak.Phonemiser:
        binary = self._espeak_binary
        runner = self._espeak_runner or (
            lambda text, espeak_voice: espeak.run_espeak(text, espeak_voice, binary=binary)
        )
        return espeak.Phonemiser(
            espeak_voice=voice.espeak_voice,
            overrides=voice.overrides,
            runner=runner,
        )

    def _session_for(self, voice: Voice) -> InferenceSession:
        """One session per voice, loaded once and kept until :meth:`stop`.

        A Piper voice is tens of megabytes and re-reading it per sentence is the
        difference between an engine that can keep ahead of a stream and one
        that cannot.
        """
        with self._lock:
            session = self._sessions.get(voice.voice_id)
            if session is not None:
                return session
        _log.info("loading model for voice %s", voice.voice_id)
        session = self._session_factory(str(voice.model_path))
        with self._lock:
            # Another thread may have won the race; keep whichever landed first
            # so there is never more than one model of one voice in memory.
            existing = self._sessions.get(voice.voice_id)
            if existing is not None:
                return existing
            self._sessions[voice.voice_id] = session
            return session

    def _run(
        self,
        voice: Voice,
        phonemes: str,
        ids: Sequence[int],
        scales: Mapping[str, float],
        speaker_id: int,
    ) -> Spoken:
        import numpy as np

        session = self._session_for(voice)
        text = np.asarray([list(ids)], dtype=np.int64)
        feed: dict[str, Any] = {
            "input": text,
            "input_lengths": np.asarray([text.shape[1]], dtype=np.int64),
            # Order fixed by the model's own signature, not by ours.
            "scales": np.asarray(
                [scales["noise_scale"], scales["length_scale"], scales["noise_w"]],
                dtype=np.float32,
            ),
        }
        input_names = {getattr(i, "name", "") for i in session.get_inputs()}
        if "sid" in input_names:
            # Multi-speaker models only. A single-speaker voice has no such
            # input and must not be handed one.
            feed["sid"] = np.asarray([speaker_id], dtype=np.int64)

        started = time.perf_counter()
        try:
            outputs = session.run(None, feed)
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own types
            raise EngineError(f"the model would not run on this input: {exc}") from exc
        elapsed = time.perf_counter() - started

        samples = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if samples.size == 0:
            raise EngineError("the model produced no audio for that text")
        return Spoken(
            audio=Audio(
                data=to_pcm_s16le(samples),
                sample_rate=voice.sample_rate,
                channels=1,
                encoding="pcm_s16le",
            ),
            audio_seconds=samples.size / voice.sample_rate,
            elapsed_seconds=elapsed,
            phonemes=phonemes,
            voice_id=voice.voice_id,
        )

    # -- PC-262: can it keep ahead of speech, on this CPU? -----------------

    def streaming(self, root: Path) -> Streaming:
        """Measured on this machine, once, and never declared from a constant.

        A clause is synthesised and timed, and the answer is the ratio of the
        audio's own duration to the wall clock it took to make it. Above the
        configured margin the engine produces faster than the words are spoken,
        with headroom to sustain it while a model is still generating.

        Taken on demand rather than at start, so an engine with no voice
        installed, or one whose phonemiser is missing, still starts and still
        says what is wrong.
        """
        with self._lock:
            if self._streaming is not None:
                return self._streaming
        answer = self._measure(Path(root))
        with self._lock:
            self._streaming = answer
        return answer

    def _measure(self, root: Path) -> Streaming:
        found, _ = self.voices(root)
        if not found:
            return Streaming(
                streaming_synthesis=False,
                realtime_factor=None,
                margin_required=self.streaming_margin,
                measured=False,
                reason=(
                    "there is no voice installed, so there was nothing to time. "
                    "Install a voice and ask again."
                ),
            )
        voice = sorted(found, key=lambda v: v.voice_id)[0]
        try:
            spoken = self.synthesise(voice, self.calibration_text)
        except EngineError as exc:
            return Streaming(
                streaming_synthesis=False,
                realtime_factor=None,
                margin_required=self.streaming_margin,
                measured=False,
                reason=f"the measurement could not be taken: {exc}",
            )
        factor = spoken.realtime_factor
        fast_enough = factor >= self.streaming_margin
        return Streaming(
            streaming_synthesis=fast_enough,
            realtime_factor=round(factor, 3),
            margin_required=self.streaming_margin,
            measured=True,
            reason=(
                "measured on this machine by synthesising one clause and timing it"
                if fast_enough
                else (
                    "this CPU produces speech more slowly than the margin asks "
                    "for, so speech is spoken at the end rather than streamed"
                )
            ),
            measured_with={
                "voice": voice.voice_id,
                "text": self.calibration_text,
                "audio_seconds": round(spoken.audio_seconds, 3),
                "elapsed_seconds": round(spoken.elapsed_seconds, 3),
            },
        )
