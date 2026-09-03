"""The exposed OpenAI-compatible API — spec section 5.4 (inbound).

The core hosts its own OpenAI-compatible endpoint so that anything in the house
which would otherwise point at the LLM host points here instead and gets the
persona, its tools and its memory for free.

This package exports a **router factory**, not an application. The app object,
its lifespan and the mounting of the admin API alongside this one belong to
whoever assembles the process::

    from personacore.api import ApiKeyStore, create_openai_router

    app.include_router(
        create_openai_router(agent=loop, keys=ApiKeyStore(layout), audit=store)
    )

See ``openai.py`` for the surface's rules and the list of ``openai_*`` modules
it is assembled from — everything it exported before that split it still
exports — and ``keys.py`` for per-client keys and the policy each one carries.
"""

from __future__ import annotations

from personacore.api.keys import (
    KEY_FILENAME,
    KEY_PREFIX,
    ApiKeyError,
    ApiKeyRecord,
    ApiKeyStore,
    IssuedKey,
)
from personacore.api.openai import (
    DEFAULT_MODEL_ID,
    SSE_DONE,
    ApiError,
    ApiErrorDetail,
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ModelCard,
    ModelList,
    OpenAIApiConfig,
    TurnRunner,
    Usage,
    create_openai_router,
)

__all__ = [
    "DEFAULT_MODEL_ID",
    "KEY_FILENAME",
    "KEY_PREFIX",
    "SSE_DONE",
    "ApiError",
    "ApiErrorDetail",
    "ApiKeyError",
    "ApiKeyRecord",
    "ApiKeyStore",
    "ChatCompletion",
    "ChatCompletionChunk",
    "ChatCompletionRequest",
    "IssuedKey",
    "ModelCard",
    "ModelList",
    "OpenAIApiConfig",
    "TurnRunner",
    "Usage",
    "create_openai_router",
]
