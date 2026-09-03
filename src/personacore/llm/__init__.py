"""Outbound LLM client — spec section 5.3.

The core's only path to the LLM host: an OpenAI-compatible HTTP client, so
swapping llama.cpp for Ollama for vLLM is a `LLMClientConfig` change with zero
code change. See `client.py` for the design rationale and `errors.py` for the
plain-English exceptions spec section 10 requires.
"""

from __future__ import annotations

from personacore.llm.client import (
    BreakerSnapshot,
    ChatCompletionChunk,
    ChatCompletionResponse,
    ChatMessage,
    CircuitState,
    FunctionCall,
    HealthStatus,
    LLMClient,
    LLMClientConfig,
    ModelInfo,
    ModelsResponse,
    ToolCall,
    ToolCallAccumulator,
)
from personacore.llm.errors import (
    LLMAuthenticationError,
    LLMCircuitOpenError,
    LLMClientError,
    LLMConnectionError,
    LLMResponseError,
    LLMStreamIncompleteError,
    LLMTimeoutError,
)

__all__ = [
    "BreakerSnapshot",
    "ChatCompletionChunk",
    "ChatCompletionResponse",
    "ChatMessage",
    "CircuitState",
    "FunctionCall",
    "HealthStatus",
    "LLMAuthenticationError",
    "LLMCircuitOpenError",
    "LLMClient",
    "LLMClientConfig",
    "LLMClientError",
    "LLMConnectionError",
    "LLMResponseError",
    "LLMStreamIncompleteError",
    "LLMTimeoutError",
    "ModelInfo",
    "ModelsResponse",
    "ToolCall",
    "ToolCallAccumulator",
]
