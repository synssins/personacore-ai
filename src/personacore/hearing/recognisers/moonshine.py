"""Moonshine base, ONNX, quantised — the first recogniser that actually hears.

Speech in, words out, on the ``onnxruntime`` this image already carries for
speech. No second inference runtime, no PyTorch, no ``transformers``: adding
either would be a defect rather than an optimisation, and the whole reason this
model was chosen is that it needs neither.

**Why Moonshine and not Whisper.** Whisper zero-pads every clip to thirty
seconds before encoding it, so a four-second dictation costs the same encode as
a twenty-five-second one — a latency floor you cannot get under no matter how
short the utterance. Moonshine's encoder is variable-length: it costs what the
audio costs. For push-to-talk dictation, where almost every clip is short, that
is the entire argument.

**CPU only, permanently.** The provider list is literally
``["CPUExecutionProvider"]`` — not "whatever is available", which is how a GPU
path gets in by accident on somebody else's machine (``CLAUDE.md``).

**The model is in the image, not in appdata.** ~63 MB of weights fetched at
build time and pinned by commit revision, so an air-gapped container starts and
transcribes with no network at all. Appdata is the assistant and an upgrade must
never touch it; a model is not appdata, it is part of the build, and baking it
in is what keeps those two facts from arguing.

**Known weakness, not worked around.** Moonshine degrades on utterances under
about one second and can emit a run of repeated tokens there. Dictation clips
are longer, so this is documented rather than papered over — a "fix" that
post-filtered repeats would also eat the real ones. The 0.1 s floor below is the
vendor's own bound, not an attempt to dodge this.

Two facts taken from the vendor's source rather than inferred, and enforced in
:meth:`MoonshineRecogniser.transcribe`: the model accepts **0.1 s to 64 s** of
audio per call. Longer is refused with a sentence; shorter is answered as
silence, because sending it is what produces the repeated-token failure above.
"""

from __future__ import annotations

import gc
import json
import logging
import math
import os
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..engine import Audio, HearingError, Transcript

_log = logging.getLogger(__name__)

ENGINE_ID = "moonshine"
ENGINE_DISPLAY = "Moonshine (base, English)"


# --------------------------------------------------------------------------
# Where the model lives, and what it is
# --------------------------------------------------------------------------

DEFAULT_MODEL_DIR = Path("/opt/personacore/models/moonshine-base")
"""Baked into the image by ``deploy/Dockerfile``, on the read-only side.

Overridden by ``PERSONACORE_MOONSHINE_DIR``, which is how a developer points at
a tree outside a container without editing anything. The same shape as
``PERSONACORE_BUNDLED_PLUGINS``.
"""

MODEL_DIR_ENV = "PERSONACORE_MOONSHINE_DIR"

ENCODER_FILE = "encoder_model.onnx"
DECODER_FILE = "decoder_model_merged.onnx"
TOKENIZER_FILE = "tokenizer.json"

MODEL_REVISION = "48b4e427b587bcf67797a5be706d6ddc4a298149"
"""``moonshine-ai/moonshine`` on Hugging Face, pinned by commit.

Recorded here as well as in the Dockerfile so that the number an operator can
read in the source is the number the image was built from. The vendor's own
repository rather than a community mirror, because it is the one whose LICENSE
can be pointed at: MIT for code and weights. (The two ``.onnx`` files are
byte-identical to ``onnx-community/moonshine-base-ONNX``; the licence is the
reason to prefer the vendor.)
"""

SAMPLE_RATE = 16000
"""What the model was trained at, from the vendor's ``preprocessor_config.json``.
Audio at any other rate is resampled here — that is the engine's job, not the
caller's, because the two callers (Wyoming and a browser) will not agree on a
rate and neither should have to know what the model wants."""

MIN_SECONDS = 0.1
MAX_SECONDS = 64.0
"""The vendor's own bounds, asserted in ``moonshine-onnx/src/transcribe.py``."""

DECODER_LAYERS = 8
ATTENTION_HEADS = 8
HEAD_DIM = 52
"""The merged decoder's cache geometry, read off its own signature rather than
derived from ``config.json``: ``hidden_size`` 416 over 8 heads is 52, but the
number that matters is the one in the tensor shape."""

DECODER_START_TOKEN = 1
EOS_TOKEN = 2
MAX_NEW_TOKENS = 194
"""``decoder_start_token_id``, ``eos_token_id`` and ``max_length`` from the
vendor's ``generation_config.json``."""

MAX_TOKENIZER_BYTES = 16 * 1024 * 1024
"""``tokenizer.json`` is 3.8 MB. Anything vastly larger is a malfunction or an
attempt to make us chew through memory, and it is refused unread — the same
guard ``vits_onnx`` puts on a voice config."""


# --------------------------------------------------------------------------
# The tokeniser, in the standard library
# --------------------------------------------------------------------------


class Detokeniser:
    """Token ids to text, with no dependency at all.

    The ``tokenizers`` package would do this, and was the obvious move — but it
    pulls ``huggingface-hub`` and with it ``requests``, which means putting a
    network client into an image whose whole promise is that nothing leaves the
    house. Decoding is also the easy half: this model never needs to *encode*,
    and the decode pipeline its ``tokenizer.json`` declares is four steps —
    replace ``U+2581`` with a space, turn ``<0xNN>`` tokens back into bytes,
    join, and strip one leading space.

    Checked against ``tokenizers`` 0.23.1 over 35,002 cases: every single
    non-special id on its own, 3,000 random id sequences and a handful of real
    sentences. One mismatch, and only in how a *malformed* byte run renders
    (U+FFFD placement) — reachable from random ids and not from anything the
    model emits, which always produces valid UTF-8.
    """

    def __init__(self, vocab: Mapping[str, int], added: Sequence[Mapping[str, Any]]) -> None:
        self._token_for: dict[int, str] = {}
        for token, index in vocab.items():
            if isinstance(token, str) and isinstance(index, int):
                self._token_for[index] = token
        self._special: set[int] = set()
        for entry in added:
            index = entry.get("id")
            content = entry.get("content")
            if not isinstance(index, int) or not isinstance(content, str):
                continue
            self._token_for[index] = content
            if entry.get("special"):
                self._special.add(index)

    @classmethod
    def from_file(cls, path: Path) -> Detokeniser:
        size = path.stat().st_size
        if size > MAX_TOKENIZER_BYTES:
            raise HearingError(
                f"{path.name} is {size} bytes, which is far too large for a "
                "tokeniser; it was not read."
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HearingError(f"{path.name} could not be read: {exc}") from exc
        model = document.get("model") if isinstance(document, Mapping) else None
        vocab = model.get("vocab") if isinstance(model, Mapping) else None
        if not isinstance(vocab, Mapping) or not vocab:
            raise HearingError(
                f"{path.name} has no vocabulary in it, so token numbers could not "
                "be turned back into words."
            )
        added = document.get("added_tokens")
        return cls(vocab, added if isinstance(added, list) else [])

    def decode(self, ids: Sequence[int]) -> str:
        """Ids to text. Special tokens are dropped; unknown ids are skipped."""
        out = bytearray()
        pending = bytearray()
        for index in ids:
            if index in self._special:
                continue
            token = self._token_for.get(index)
            if token is None:
                continue
            # `<0xNN>` — a byte-fallback token. They arrive in runs that together
            # spell one character, so they are accumulated and only decoded once
            # the run ends; decoding each on its own would turn every non-ASCII
            # character into replacement junk.
            if len(token) == 6 and token.startswith("<0x") and token.endswith(">"):
                try:
                    pending.append(int(token[3:5], 16))
                    continue
                except ValueError:
                    pass
            if pending:
                out.extend(pending)
                pending.clear()
            out.extend(token.replace("▁", " ").encode("utf-8"))
        out.extend(pending)
        text = out.decode("utf-8", errors="replace")
        # The tokeniser's own `Strip` step: one leading space, because the
        # normaliser prepended one before the first word.
        return text[1:] if text.startswith(" ") else text


# --------------------------------------------------------------------------
# Audio, into the shape the encoder wants
# --------------------------------------------------------------------------


def _lowpass(samples: Any, ratio: float) -> Any:
    """A windowed-sinc lowpass, applied only when downsampling.

    Dropping samples without one aliases, and the common case here is a browser
    capturing at 48 kHz: everything above 8 kHz would fold back down into the
    speech band as a metallic buzz the model then has to hear through. 63 taps
    is cheap — a few milliseconds on a five-second clip — and the difference is
    audible.
    """
    import numpy as np

    taps = 63
    n = np.arange(taps) - (taps - 1) / 2.0
    cutoff = 0.5 * ratio
    # `np.where` evaluates both branches, so the centre tap has to be kept out
    # of the divisor rather than selected away from it — otherwise the division
    # by zero still happens and still warns, on every clip that is resampled.
    safe = np.where(n == 0, 1.0, n)
    sinc = np.where(n == 0, 2.0 * cutoff, np.sin(2.0 * np.pi * cutoff * safe) / (np.pi * safe))
    kernel = (sinc * np.hamming(taps)).astype(np.float32)
    kernel /= max(float(kernel.sum()), 1e-9)
    return np.convolve(samples, kernel, mode="same").astype(np.float32)


def to_model_input(audio: Audio) -> Any:
    """PCM bytes to the ``(1, num_samples)`` float32 the encoder takes.

    Three things happen, in this order, and each is the engine's job rather than
    the caller's: multi-channel audio is mixed down to mono, the samples are
    scaled to roughly [-1, 1], and anything not already at 16 kHz is resampled.

    Raw samples, deliberately. There is no log-mel front end anywhere in this
    model — unlike Whisper, whose filterbank is a whole second implementation to
    get wrong. The encoder's only input is ``input_values``, and its own
    ``preprocessor_config.json`` says ``do_normalize: false``, so the samples go
    in as they are with no loudness normalisation.
    """
    import numpy as np

    if audio.encoding != "pcm_s16le":
        raise HearingError(
            f"this recogniser reads 16-bit PCM and was handed {audio.encoding!r}. "
            "Decode it to raw samples before asking for words."
        )
    flat = np.frombuffer(audio.data, dtype="<i2").astype(np.float32) / 32768.0
    channels = max(1, audio.channels)
    if channels > 1:
        usable = (flat.size // channels) * channels
        flat = flat[:usable].reshape(-1, channels).mean(axis=1)

    rate = audio.sample_rate
    if rate != SAMPLE_RATE and flat.size:
        if rate <= 0:
            raise HearingError(
                "this audio does not say what rate it was recorded at, and "
                "guessing is the difference between speech and a chipmunk."
            )
        if rate > SAMPLE_RATE:
            flat = _lowpass(flat, SAMPLE_RATE / rate)
        target = int(round(flat.size * SAMPLE_RATE / rate))
        if target < 1:
            return np.zeros((1, 0), dtype=np.float32)
        source_positions = np.arange(flat.size, dtype=np.float64)
        target_positions = np.arange(target, dtype=np.float64) * (rate / SAMPLE_RATE)
        flat = np.interp(target_positions, source_positions, flat).astype(np.float32)

    return np.ascontiguousarray(flat, dtype=np.float32).reshape(1, -1)


# --------------------------------------------------------------------------
# The runtime
# --------------------------------------------------------------------------


class InferenceSession(Protocol):
    """The slice of onnxruntime this module uses, so tests need no model file."""

    def get_inputs(self) -> Sequence[Any]: ...

    def get_outputs(self) -> Sequence[Any]: ...

    def run(
        self, output_names: Sequence[str] | None, input_feed: Mapping[str, Any]
    ) -> Sequence[Any]: ...


def onnxruntime_is_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("onnxruntime") is not None


def open_session(model_path: str) -> InferenceSession:
    """One onnxruntime session, pinned to the CPU provider.

    Imported here rather than at module scope so the parts of this recogniser
    that do not run a model — construction, availability, audio conversion —
    work with onnxruntime absent, and so an image missing it says so in a
    sentence instead of failing to import.
    """
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - the image always has it
        raise HearingError(
            "onnxruntime is not installed in this container, so nothing can be "
            "transcribed. The image is meant to carry it; this build did not."
        ) from exc

    options = ort.SessionOptions()
    options.log_severity_level = 3
    try:
        return ort.InferenceSession(
            model_path,
            sess_options=options,
            # CPU ONLY, and stated rather than discovered — the same line, for
            # the same reason, as `voice/engines/vits_onnx.py`.
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own types
        raise HearingError(f"the model file could not be loaded: {exc}") from exc


def _model_dir(explicit: Path | str | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(MODEL_DIR_ENV)
    if from_env:
        return Path(from_env)
    return DEFAULT_MODEL_DIR


class MoonshineRecogniser:
    """Moonshine base on onnxruntime, as one switch.

    Constructing it costs nothing: no model is opened, no runtime imported, no
    file read beyond asking whether three of them exist. That is what lets this
    recogniser sit in the image alongside others an operator never turns on.

    ``session_factory`` is the seam. A test passes a stub and exercises the
    lifecycle, the bounds and the audio conversion without 63 MB of weights.
    """

    id = ENGINE_ID
    display = ENGINE_DISPLAY

    def __init__(
        self,
        *,
        model_dir: Path | str | None = None,
        session_factory: Any = open_session,
    ) -> None:
        self.model_dir = _model_dir(model_dir)
        self.encoder_path = self.model_dir / ENCODER_FILE
        self.decoder_path = self.model_dir / DECODER_FILE
        self.tokeniser_path = self.model_dir / TOKENIZER_FILE
        self._session_factory = session_factory
        self._needs_onnxruntime = session_factory is open_session

        self._lock = threading.Lock()
        self._encoder: InferenceSession | None = None
        self._decoder: InferenceSession | None = None
        self._decoder_outputs: list[str] = []
        self._detokeniser: Detokeniser | None = None
        self._started = False

        self.available, self.unavailable_reason = self._availability()

    # -- availability ------------------------------------------------------

    def _availability(self) -> tuple[bool, str | None]:
        """Can this build, on this machine, run this recogniser at all?

        Not the operator's switch. Answered once, by *looking* — never by
        importing or loading — so a container built without the model files says
        this sentence rather than offering a switch that starts something which
        cannot work.
        """
        missing: list[str] = []
        if self._needs_onnxruntime and not onnxruntime_is_installed():
            missing.append("onnxruntime, the model runtime, is not installed in it")
        absent = [
            path.name
            for path in (self.encoder_path, self.decoder_path, self.tokeniser_path)
            if not self._is_file(path)
        ]
        if absent:
            missing.append(
                f"the model files are not in {self.model_dir} ({', '.join(absent)})"
            )
        if not missing:
            return True, None
        return False, (
            "This build cannot run the Moonshine recogniser because "
            + " and ".join(missing)
            + ". The image is meant to carry them; this one does not, so no "
            "switch is offered rather than one that starts something which "
            "cannot listen."
        )

    @staticmethod
    def _is_file(path: Path) -> bool:
        # A model directory that cannot be read is the same fact as one that is
        # not there, and neither may raise out of a constructor.
        try:
            return path.is_file()
        except OSError:  # pragma: no cover - permissions, a broken mount
            return False

    # -- the switch --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._started

    def start(self) -> None:
        """Load the two sessions and the tokeniser. Called when switched on.

        Both models load here rather than on first use. Unlike a voice, where an
        operator may have ten and loading all of them would make the switch mean
        something nobody asked for, there is exactly one model here — and the
        first thing anyone does after switching hearing on is speak to it, so
        deferring the cost only moves it somewhere it looks like a bug.
        """
        if not self.available:
            raise HearingError(self.unavailable_reason or "this recogniser is not available")
        with self._lock:
            if self._started:
                return
        encoder = self._session_factory(str(self.encoder_path))
        decoder = self._session_factory(str(self.decoder_path))
        detokeniser = Detokeniser.from_file(self.tokeniser_path)
        outputs = [getattr(o, "name", "") for o in decoder.get_outputs()]
        with self._lock:
            self._encoder = encoder
            self._decoder = decoder
            self._decoder_outputs = outputs
            self._detokeniser = detokeniser
            self._started = True
        _log.info("recogniser %s started from %s", self.id, self.model_dir)

    def stop(self) -> None:
        """Release it. After this the recogniser holds no model.

        Off means off. Both sessions are dropped, and with them the weights —
        a session is the only thing here holding one. The collector runs
        explicitly because a session's arena is native memory freed when the
        Python object is finalised, and waiting for the next natural collection
        would make "off" mean "off shortly".

        Calling it twice, or on one that was never started, is fine.
        """
        with self._lock:
            sessions = [s for s in (self._encoder, self._decoder) if s is not None]
            self._encoder = None
            self._decoder = None
            self._decoder_outputs = []
            self._detokeniser = None
            self._started = False
        # Popped rather than iterated: `for s in sessions` leaves the last one
        # bound to a local until this frame ends, which is the sort of
        # "released" that measures a model short.
        while sessions:
            session = sessions.pop()
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # noqa: BLE001 - a failed close is not fatal
                    _log.warning("a Moonshine session would not close cleanly: %s", exc)
            del closer, session
        del sessions
        gc.collect()
        _log.info("recogniser %s stopped; no model is held", self.id)

    # -- listening ---------------------------------------------------------

    def transcribe(self, audio: Audio, **knobs: Any) -> Transcript:
        """Audio in, words out. Raises :class:`HearingError`, never a bare one.

        ``language`` is the only knob, and it is a check rather than a setting:
        this is the English model, so anything else is refused with a sentence
        instead of being transcribed into confident nonsense.

        Returns ``Transcript(text="")`` for silence and for anything under the
        model's 0.1 s floor. **Empty is a success** — a caller that cannot tell
        "nothing was said" from "the recogniser is broken" will eventually
        report one as the other to an operator.
        """
        language = knobs.pop("language", None)
        if knobs:
            raise HearingError(
                "this recogniser has no setting called "
                + ", ".join(repr(k) for k in sorted(knobs))
                + "."
            )
        if language is not None:
            if not isinstance(language, str) or language.split("-")[0].lower() != "en":
                raise HearingError(
                    f"this is the English Moonshine model and it was asked for "
                    f"{language!r}. It would return confident nonsense rather "
                    "than nothing, so it declines."
                )
        if not self._started:
            raise HearingError(
                f"the {self.display} recogniser is switched off, so it cannot "
                "listen. Turn it on in Settings and it works again."
            )
        if not isinstance(audio, Audio):
            raise HearingError("transcribe was handed something that is not Audio")

        seconds = audio.seconds
        if seconds > MAX_SECONDS:
            raise HearingError(
                f"that is {seconds:.1f} seconds of audio and this recogniser "
                f"takes at most {MAX_SECONDS:.0f} in one go. Split it before "
                "sending it — which is what a caller listening to a conversation "
                "has to do anyway."
            )
        if seconds < MIN_SECONDS:
            # Below the model's floor. Not an error and not sent: this is where
            # Moonshine emits runs of repeated tokens, so a fifty-millisecond
            # click would come back as words nobody said.
            return self._empty(seconds, resampled=False)

        samples = to_model_input(audio)
        if samples.shape[1] < int(MIN_SECONDS * SAMPLE_RATE):
            return self._empty(seconds, resampled=audio.sample_rate != SAMPLE_RATE)

        started = time.perf_counter()
        ids = self._generate(samples)
        elapsed = time.perf_counter() - started

        detokeniser = self._detokeniser
        text = detokeniser.decode(ids).strip() if detokeniser is not None else ""
        _log.info(
            "heard %.2fs of audio as %d token(s) in %.3fs", seconds, len(ids), elapsed
        )
        return Transcript(
            text=text,
            language="en",
            metadata={
                "engine": self.id,
                "model": "moonshine-base-quantized",
                "revision": MODEL_REVISION,
                "audio_seconds": round(seconds, 3),
                "elapsed_seconds": round(elapsed, 3),
                "tokens": len(ids),
                "resampled_from": (
                    audio.sample_rate if audio.sample_rate != SAMPLE_RATE else None
                ),
            },
        )

    def _empty(self, seconds: float, *, resampled: bool) -> Transcript:
        return Transcript(
            text="",
            language="en",
            metadata={
                "engine": self.id,
                "audio_seconds": round(seconds, 3),
                "below_minimum": True,
                "minimum_seconds": MIN_SECONDS,
                "resampled": resampled,
            },
        )

    # -- greedy decoding ---------------------------------------------------

    def _generate(self, samples: Any) -> list[int]:
        """Encode once, then step the merged decoder until it says stop.

        Greedy, deliberately: beam search buys a fraction of a WER point for a
        multiple of the latency, and latency is the reason this model is here at
        all.

        The one thing that is not obvious, and that cost an afternoon to find:
        the merged decoder's **encoder** cache is captured from the first
        no-cache step and then held constant. On a cache-branch step the model
        returns a degenerate ``(0, 8, 1, 52)`` for ``present.*.encoder.*`` —
        a pass-through placeholder, not a cache — and feeding that back in makes
        the next step fail inside a MatMul with a broadcast error. Only
        ``present.*.decoder.*`` is threaded forward.
        """
        import numpy as np

        with self._lock:
            encoder, decoder = self._encoder, self._decoder
            output_names = list(self._decoder_outputs)
        if encoder is None or decoder is None:  # pragma: no cover - guarded by _started
            raise HearingError("the recogniser was switched off mid-utterance")

        try:
            hidden = encoder.run(["last_hidden_state"], {"input_values": samples})[0]
        except HearingError:
            raise
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own types
            raise HearingError(f"the encoder would not run on that audio: {exc}") from exc

        empty = np.zeros((1, ATTENTION_HEADS, 0, HEAD_DIM), dtype=np.float32)
        feed: dict[str, Any] = {
            "encoder_hidden_states": hidden,
            "input_ids": np.array([[DECODER_START_TOKEN]], dtype=np.int64),
            "use_cache_branch": np.array([False]),
        }
        for layer in range(DECODER_LAYERS):
            for part in ("decoder", "encoder"):
                feed[f"past_key_values.{layer}.{part}.key"] = empty
                feed[f"past_key_values.{layer}.{part}.value"] = empty

        ids: list[int] = []
        encoder_cache: dict[str, Any] = {}
        try:
            for step in range(MAX_NEW_TOKENS):
                out = dict(zip(output_names, decoder.run(None, feed), strict=False))
                if step == 0:
                    encoder_cache = {
                        f"past_key_values.{layer}.encoder.{part}": out[
                            f"present.{layer}.encoder.{part}"
                        ]
                        for layer in range(DECODER_LAYERS)
                        for part in ("key", "value")
                    }
                token = int(np.asarray(out["logits"])[0, -1].argmax())
                if token == EOS_TOKEN:
                    break
                ids.append(token)
                feed = {
                    "encoder_hidden_states": hidden,
                    "input_ids": np.array([[token]], dtype=np.int64),
                    "use_cache_branch": np.array([True]),
                }
                feed.update(encoder_cache)
                for layer in range(DECODER_LAYERS):
                    for part in ("key", "value"):
                        feed[f"past_key_values.{layer}.decoder.{part}"] = out[
                            f"present.{layer}.decoder.{part}"
                        ]
        except HearingError:
            raise
        except (KeyError, IndexError) as exc:
            raise HearingError(
                f"the decoder did not return what this recogniser expects ({exc}). "
                "The model files may be a different export than the one this "
                "build was written against."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own types
            raise HearingError(f"the decoder would not run on that audio: {exc}") from exc
        return ids


def encoder_frames(num_samples: int) -> int:
    """How many frames the encoder produces for ``num_samples`` at 16 kHz.

    The model's own output shape, written out: ``floor(floor(floor(n/64 -
    127/64)/3)/2) - 1``. Not used to run anything — it is here so the 0.1 s
    floor can be *shown* to be the point below which the encoder stops
    producing a usable sequence, rather than asserted.
    """
    return math.floor(math.floor(math.floor(num_samples / 64 - 127 / 64) / 3) / 2) - 1
