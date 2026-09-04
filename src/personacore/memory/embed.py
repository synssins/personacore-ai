"""Bundled ONNX sentence embedding (contract memory.md §4, plan joint J3).

CPU only, always: ``onnxruntime``'s ``CPUExecutionProvider``, never a GPU
provider, for the same reason and in the same shape as
``voice/engines/vits_onnx.py::open_session`` — CLAUDE.md's hard constraint
applies here exactly as it does to voice. ``onnxruntime`` is imported lazily,
inside ``_ensure_session``, so importing this module (or calling
``Embedder.available()``) never requires it to be installed and never touches
the filesystem beyond a couple of ``is_file()`` checks.

``embed`` is synchronous and CPU-bound. Callers run it with
``asyncio.to_thread`` so the event loop is never blocked by inference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from personacore.memory.tokenizer import WordPieceTokenizer

#: The two files that make up the bundled model, both under
#: ``personacore/memory/models/`` — installed with the package, not appdata.
#: See ``deploy/fetch_embedding_model.py`` for where they come from and their
#: pinned checksums.
MODEL_FILENAME = "model_quint8_avx2.onnx"
VOCAB_FILENAME = "vocab.txt"

_MAX_TOKENS = 256


class EmbedderError(RuntimeError):
    """The embedding model or its tokenizer could not be loaded or run."""


def _models_dir() -> Path:
    return Path(__file__).resolve().parent / "models"


class Embedder:
    """A single bundled sentence embedder, 384 dimensions, CPU only.

    One ``onnxruntime.InferenceSession`` per instance, created lazily on the
    first call to :meth:`embed` — constructing an ``Embedder`` (including via
    :meth:`bundled`) never loads the model.
    """

    dimensions = 384

    def __init__(self, model_path: Path, vocab_path: Path) -> None:
        self._model_path = Path(model_path)
        self._vocab_path = Path(vocab_path)
        self._tokenizer: WordPieceTokenizer | None = None
        self._session: Any = None

    @classmethod
    def bundled(cls) -> Embedder:
        """The embedder over the model shipped inside the package."""
        models = _models_dir()
        return cls(models / MODEL_FILENAME, models / VOCAB_FILENAME)

    @classmethod
    def available(cls) -> bool:
        """Whether both bundled model files are present.

        Does not check that ``onnxruntime`` is importable or that the model
        actually loads — that is what constructing a session (inside
        :meth:`embed`) would tell you, at the cost of loading it. Callers
        such as ``server.py`` use this to decide, without paying that cost,
        whether to build a :class:`Embedder` at all.
        """
        models = _models_dir()
        return (models / MODEL_FILENAME).is_file() and (models / VOCAB_FILENAME).is_file()

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        if not self._model_path.is_file():
            raise EmbedderError(f"embedding model not found at {self._model_path}")
        if not self._vocab_path.is_file():
            raise EmbedderError(f"tokenizer vocab not found at {self._vocab_path}")

        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - the image always has it
            raise EmbedderError(
                "onnxruntime is not installed in this container, so memory "
                "cannot embed text. The image is meant to carry it; this "
                "build did not."
            ) from exc

        options = ort.SessionOptions()
        options.log_severity_level = 3
        try:
            session = ort.InferenceSession(
                str(self._model_path),
                sess_options=options,
                # CPU ONLY, and stated rather than discovered (CLAUDE.md's
                # hard constraint) — passing the available-provider list
                # instead would silently pick up a GPU on somebody's machine.
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own types
            raise EmbedderError(f"the embedding model could not be loaded: {exc}") from exc

        # Only assigned once the session has loaded cleanly, so a failed load
        # never leaves a half-initialised embedder that looks ready.
        self._tokenizer = WordPieceTokenizer(self._vocab_path)
        self._session = session

    def embed(self, text: str) -> list[float]:
        """Embed one string: mean-pool over the attention mask, L2-normalise.

        Synchronous and CPU-bound — run it with ``asyncio.to_thread``. Empty
        text embeds as the ``[CLS] [SEP]`` sequence rather than raising.
        """
        self._ensure_session()
        session = self._session
        tokenizer = self._tokenizer
        if session is None or tokenizer is None:  # pragma: no cover - defensive
            raise EmbedderError("embedder session failed to initialise")

        input_ids, attention_mask, token_type_ids = tokenizer.encode(text, max_len=_MAX_TOKENS)
        ids = np.asarray([input_ids], dtype=np.int64)
        mask = np.asarray([attention_mask], dtype=np.int64)
        types = np.asarray([token_type_ids], dtype=np.int64)

        outputs = session.run(
            ["last_hidden_state"],
            {"input_ids": ids, "attention_mask": mask, "token_type_ids": types},
        )
        hidden = np.asarray(outputs[0][0], dtype=np.float64)  # [seq, 384]
        weights = mask[0].astype(np.float64)[:, None]  # [seq, 1]

        summed = (hidden * weights).sum(axis=0)
        count = max(float(weights.sum()), 1e-9)
        pooled = summed / count

        norm = float(np.linalg.norm(pooled))
        if norm > 0:
            pooled = pooled / norm
        return [float(v) for v in pooled]
