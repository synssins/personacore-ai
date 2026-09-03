"""The image generation service — docs/contracts/image-conversations.md.

An image conversation's responder: a prompt in, a picture back, over an
OpenAI-shaped HTTP endpoint (``/v1/images/generations``), reached the same
way any ``[llm.*]`` role is. See ``client.py`` for the client itself and what
was checked against the upstream project before it was written.
"""

from __future__ import annotations

from personacore.images.client import (
    DEFAULT_OUTPUT_FORMAT,
    GeneratedImage,
    ImageClient,
    ImageRefused,
    ImageUnavailable,
)

__all__ = [
    "DEFAULT_OUTPUT_FORMAT",
    "GeneratedImage",
    "ImageClient",
    "ImageRefused",
    "ImageUnavailable",
]
