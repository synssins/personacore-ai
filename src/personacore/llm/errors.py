"""Typed exceptions for the outbound LLM client — spec section 10.

Section 10 requires "graceful degradation" with the assistant saying, in plain
speech, that something is wrong ("I can't reach the music system right now").
Every exception here carries a `spoken_message` fit to be read aloud verbatim,
instead of a stack trace or an httpx repr — that raw detail (never the API
key; see client.py) goes on `.detail`, for structured logs, not the user.
"""

from __future__ import annotations


class LLMClientError(Exception):
    """Base for every error this client raises."""

    def __init__(self, spoken_message: str, *, detail: str | None = None) -> None:
        super().__init__(spoken_message)
        self.spoken_message = spoken_message
        self.detail = detail


class LLMConnectionError(LLMClientError):
    """The LLM host could not be reached at all — DNS failure, connection
    refused, network unreachable."""


class LLMTimeoutError(LLMClientError):
    """The LLM host was reached but did not respond within the configured
    timeout (connect or read — see LLMClientConfig)."""


class LLMAuthenticationError(LLMClientError):
    """The LLM host rejected the request as unauthorised (401/403). Never
    carries the API key itself — only the fact that it was rejected."""


class LLMResponseError(LLMClientError):
    """The LLM host responded, but with a non-2xx status or a body the client
    could not parse as the expected shape.

    ``status_code`` is the HTTP status when the failure *was* a status — and
    ``None`` when the host answered 2xx with a body that could not be read.
    It exists because "this host does not implement that endpoint" (404/405)
    and "this host broke" are different things to say to an operator, and the
    only honest place to tell them apart is where the status was seen. Callers
    that only need a sentence keep using ``spoken_message``; ``detail`` still
    carries the same ``status=<code>`` text it always did, for logs.
    """

    def __init__(
        self,
        spoken_message: str,
        *,
        detail: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(spoken_message, detail=detail)
        self.status_code = status_code


class LLMStreamIncompleteError(LLMResponseError):
    """A streamed reply ended without ever reaching a recognised terminator —
    neither a `[DONE]` sentinel nor a chunk with a non-null `finish_reason`
    (spec section 5.3/10). The body simply stopped: a crashed backend, a
    killed connection, or a reverse proxy that truncates responses. Without
    this, a cut-off reply is indistinguishable from a complete one and voice
    would speak (or act on) a partial answer as if it had finished. A subclass
    of LLMResponseError — it IS a response error, just one specific enough to
    name, in case callers ever want to tell "truncated" apart from "malformed"."""


class LLMCircuitOpenError(LLMClientError):
    """The circuit breaker is open: recent consecutive failures mean this call
    is refused immediately, without touching the network, until the cooldown
    elapses and a single probe is allowed through."""

    def __init__(self, spoken_message: str, *, cooldown_remaining_seconds: float) -> None:
        super().__init__(spoken_message)
        self.cooldown_remaining_seconds = cooldown_remaining_seconds
