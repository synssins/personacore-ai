"""Core memory package (ADR-0045).

This module only exports; the store, provider and tools that turn these
pieces into a working feature are built by later tasks in
``working/PLAN-memory.md`` and wired in ``server.py``. Importing this package
never touches onnxruntime or the filesystem — see ``embed.Embedder``.
"""

from __future__ import annotations

from personacore.memory.embed import Embedder
from personacore.memory.tokenizer import WordPieceTokenizer

__all__ = ["Embedder", "WordPieceTokenizer"]
