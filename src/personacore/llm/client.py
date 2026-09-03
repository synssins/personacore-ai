"""Outbound LLM client — spec section 5.3 (OpenAI-compatible only, no vendor
SDK, backend swap is config-only) and section 10 (timeouts, circuit breaker,
health checks, plain-English failures). This is the core's only path to the
LLM host.

Deliberately thin on the request side: `messages`, `tools`, `tool_choice` and
any extra sampling parameters pass through as plain dicts rather than being
modelled field-by-field. The acceptance test for every design choice in this
module is "swap llama.cpp for Ollama for vLLM with zero code change" — modelling
the full OpenAI request schema would tempt this client into having an opinion
on backend-specific request shapes it has no business validating. Responses
ARE modelled (leniently, `extra="allow"`) because the core has to reason about
them, above all reassembling tool-call deltas that arrive fragmented across
streamed chunks — the hardest part of this module, spec section 5.3.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from personacore.llm.errors import (
    LLMAuthenticationError,
    LLMCircuitOpenError,
    LLMClientError,
    LLMConnectionError,
    LLMResponseError,
    LLMStreamIncompleteError,
    LLMTimeoutError,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "BreakerSnapshot",
    "ChatCompletionChunk",
    "ChatCompletionResponse",
    "ChatMessage",
    "CircuitState",
    "FunctionCall",
    "HealthStatus",
    "LLMClient",
    "LLMClientConfig",
    "ModelInfo",
    "ModelsResponse",
    "ToolCall",
    "ToolCallAccumulator",
]


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class LLMClientConfig(BaseModel):
    """Backend connection settings.

    Spec section 5.3: base URL, model name and API key are ALL that changes to
    swap backends. Frozen — a running client's config never mutates underneath
    it; swapping backends means constructing a new client, not poking fields.

    This model is deliberately NOT a shared/global config module: it lives here
    so the module that owns real application settings has no collision to
    avoid. Whoever wires up the real settings reads values into this.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str
    """Root of the OpenAI-compatible host, including any version prefix the
    backend uses, e.g. "http://llm-host:8080/v1" — the OpenAI SDK convention.
    Endpoints are `f"{base_url}/chat/completions"` and `f"{base_url}/models"`.

    A full endpoint URL is accepted too and trimmed back to the root, because
    that is what every other tool's settings screen shows and what a person will
    paste. Silently producing ".../chat/completions/chat/completions" from an
    otherwise reasonable input is the sort of unexplained failure spec section 9
    exists to prevent."""

    model: str
    """Model name sent as `model` in every request body."""

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, value: str) -> str:
        """Trim a pasted endpoint back to the root the client builds paths from.

        Accepts every shape a person is likely to have on their clipboard:
        ".../v1", ".../v1/", ".../v1/chat/completions", ".../v1/completions",
        ".../v1/models".
        """
        trimmed = value.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions", "/models"):
            if trimmed.endswith(suffix):
                trimmed = trimmed[: -len(suffix)]
                break
        return trimmed.rstrip("/")

    api_key: SecretStr | None = None
    """Bearer token, if the backend requires one. None sends no Authorization
    header — many local backends (llama.cpp, Ollama) don't need one. Never
    logged, never placed in an exception message; redacted automatically by
    SecretStr in any repr of this config."""

    connect_timeout: float = Field(default=5.0, gt=0)
    """How long to wait for the TCP/TLS handshake."""

    read_timeout: float = Field(default=60.0, gt=0)
    """How long to wait for the body once the request is sent. Separate from
    connect_timeout on purpose (spec section 10): a long generation must not
    trip the connect timeout — only this one applies once the request is in
    flight."""

    write_timeout: float = Field(default=10.0, gt=0)
    pool_timeout: float = Field(default=5.0, gt=0)

    failure_threshold: int = Field(default=5, ge=1)
    """Consecutive failures before the circuit breaker opens."""

    cooldown_seconds: float = Field(default=30.0, gt=0)
    """How long the breaker stays open before letting one probe through."""

    def httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
            pool=self.pool_timeout,
        )


# --------------------------------------------------------------------------
# Response models — lenient on purpose; see module docstring.
# --------------------------------------------------------------------------


class FunctionCall(BaseModel):
    """A fully assembled function call — either from a non-streaming response,
    or reassembled from streamed deltas by ToolCallAccumulator."""

    model_config = ConfigDict(extra="allow")

    name: str
    arguments: str
    """Raw JSON text, exactly as the model produced it. Not parsed here — a
    tool-calling model can emit invalid JSON, and deciding what to do about
    that is the caller's business, not this transport's."""


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    reasoning_content: str | None = None
    """A reasoning model's own thinking, when the backend splits it out of
    ``content`` rather than inlining it (a `<think>` tag) or discarding it.
    Not an OpenAI field — llama.cpp, vLLM and Deepseek's own API all use this
    name — and confirmed present here too, not only on the streamed delta
    below: a non-streaming `/v1/chat/completions` reply from such a host has
    ``message.keys() == ['role', 'content', 'reasoning_content']``. Additive
    and optional, so a backend that never sends it (every backend this was
    tested against until now) parses exactly as before."""


class ChatCompletionChoice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    """A non-streaming `/chat/completions` response."""

    model_config = ConfigDict(extra="allow")

    id: str
    model: str
    choices: list[ChatCompletionChoice]
    usage: dict[str, Any] | None = None


class FunctionCallDelta(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    arguments: str | None = None


class ToolCallDelta(BaseModel):
    """One fragment of one streamed tool call.

    `index` is the tool call's position in the `tool_calls` array and is the
    only identifier guaranteed present on every fragment — `id` and
    `function.name` typically arrive once, on the first fragment for that
    index, while `function.arguments` arrives split arbitrarily across many
    fragments to be concatenated in order. `index` is therefore the reassembly
    key, not `id` — but it is not a trustworthy one on its own: see
    ToolCallAccumulator for what happens when a backend stamps every parallel
    call with the same index.
    """

    model_config = ConfigDict(extra="allow")

    index: int
    id: str | None = None
    type: str | None = None
    function: FunctionCallDelta | None = None


class ChatDelta(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str | None = None
    content: str | None = None
    tool_calls: list[ToolCallDelta] | None = None
    reasoning_content: str | None = None
    """A fragment of a reasoning model's thinking, streamed on its own channel
    while ``content`` stays empty — the measured shape observed in practice:
    ``n_gen = 12429`` tokens generated and the screen showed none of it,
    because almost all of it was thinking and this field did not exist yet to
    carry it. Not an OpenAI field (see :attr:`ChatMessage.reasoning_content`
    for the backends that send it); ``extra="allow"`` meant it was already
    arriving on every such chunk and being silently discarded before this was
    declared. Additive: every existing caller that reads ``.content`` and
    ignores everything else keeps working exactly as it did."""


class ChatCompletionChunkChoice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int
    delta: ChatDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """One SSE `data:` event from a streaming `/chat/completions` call."""

    model_config = ConfigDict(extra="allow")

    id: str
    model: str
    choices: list[ChatCompletionChunkChoice] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    """The exact token count the backend's own tokenizer produced for this
    turn — never estimated here (a guess that disagrees with the model is
    worse than no number, because it is the number a person plans around).

    Absent on every ordinary chunk. OpenAI's convention, which llama.cpp
    matches (checked against a live host, 2026-09-02): a streamed reply
    reports usage only on one extra, otherwise-empty chunk at the very end
    — `choices: []`, this field carrying `prompt_tokens`/`completion_tokens`/
    `total_tokens` — and only when the request asked for it
    (`stream_chat_completion`'s `stream_options`, below). Without that ask,
    this stays `None` for the whole stream, on every backend tried so far."""


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "model"


class ModelsResponse(BaseModel):
    """A `/v1/models` response."""

    model_config = ConfigDict(extra="allow")

    object: str = "list"
    data: list[ModelInfo] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Tool-call reassembly — the hard part.
# --------------------------------------------------------------------------


class _PendingToolCall:
    __slots__ = ("id", "type", "name", "arguments")

    def __init__(self) -> None:
        self.id: str | None = None
        self.type: str | None = None
        self.name: str = ""
        self.arguments: str = ""


class ToolCallAccumulator:
    """Reassembles tool-call deltas streamed across many chunks into complete
    ToolCall objects, spec section 5.3.

    Feed it every chunk as it arrives via `add_chunk`; call `result()` once the
    stream ends (or at any point, to inspect progress). `index` (the tool
    call's position in the array) is the reassembly key, because it is the only
    field a backend is guaranteed to repeat on every fragment: the fragments
    carrying `function.arguments` usually carry nothing else.

    `index` alone is not enough, though, because backends get it wrong.
    **Ollama has twice shipped a build that stamps every parallel tool call
    with `index: 0`** (ollama/ollama#7881, #15457). Keyed purely on index, two
    distinct calls land in one slot and their names concatenate — the
    `get_weatherget_weather` shape already named in the DeltaToolCall docstring
    on our own inbound surface. The mangled name then matches no tool we know
    about, so *both* calls are dropped and the caller gets a cheerful 200 with
    plain text. Asked to do two things, the assistant does neither and says
    nothing about it. That is worse than a crash.

    So `id`, when the backend states one, outranks `index`: a fragment whose
    `id` differs from the open slot's starts a **new** call at that index
    instead of joining it. `id` is what makes a call a call; `index` is only
    the backend's bookkeeping. Fragments that state no `id` — the ordinary
    arguments continuations — always join the open slot, which is the whole
    reason index-keying exists and is what keeps a correctly-behaved backend
    reassembling exactly as before.

    Second rule, independent of the first: **a name is never appended to
    itself.** A backend that restates `get_weather` on every fragment of one
    call used to end up with `get_weatherget_weather` here too. A repeat of the
    name we already hold is identity being restated, so the first write wins. A
    name piece that *differs* is still appended, because a name genuinely split
    across fragments is a real (if rare) stream, pinned by the QA suite.

    What is left ambiguous on the wire: several calls at one index where the
    backend states no `id` at all. Nothing tells that apart from one call's
    identity being restated, so it stays merged — but `result()` then drops it
    as malformed, and says so in the log, rather than dispatching a name
    nobody asked for.
    """

    def __init__(self) -> None:
        self._open_by_index: dict[int, _PendingToolCall] = {}
        self._pending: list[tuple[int, _PendingToolCall]] = []

    def add_chunk(self, chunk: ChatCompletionChunk) -> None:
        for choice in chunk.choices:
            tool_call_deltas = choice.delta.tool_calls
            if not tool_call_deltas:
                continue
            for delta in tool_call_deltas:
                pending = self._open_by_index.get(delta.index)
                if pending is None:
                    pending = self._open_slot(delta.index)
                elif _contradicts(pending, delta):
                    logger.warning(
                        "tool_call_index_reused",
                        index=delta.index,
                        open_call_id=pending.id,
                        open_call_name=pending.name or None,
                        new_call_id=delta.id,
                        new_call_name=delta.function.name if delta.function else None,
                        detail=(
                            "The model host reused one tool-call index for two "
                            "different calls; treating them as separate calls."
                        ),
                    )
                    pending = self._open_slot(delta.index)
                if delta.id:
                    pending.id = delta.id
                if delta.type:
                    pending.type = delta.type
                if delta.function is not None:
                    _merge_name(pending, delta.function.name)
                    if delta.function.arguments:
                        pending.arguments += delta.function.arguments

    def _open_slot(self, index: int) -> _PendingToolCall:
        pending = _PendingToolCall()
        self._open_by_index[index] = pending
        self._pending.append((index, pending))
        return pending

    def result(self) -> list[ToolCall]:
        """Assembled tool calls, in array order — ties on a reused index broken
        by arrival order. A call with no `id` ever observed is skipped: that is
        a malformed stream, not a tool call. It is logged, because a dropped
        call is exactly the kind of nothing-happened nobody notices."""
        calls: list[ToolCall] = []
        # sorted() is stable, so two calls sharing an index keep arrival order.
        for index, pending in sorted(self._pending, key=lambda item: item[0]):
            if pending.id is None:
                logger.warning(
                    "tool_call_dropped_without_id",
                    index=index,
                    name=pending.name or None,
                    detail="A streamed tool call never carried an id, so it can't be run.",
                )
                continue
            calls.append(
                ToolCall(
                    id=pending.id,
                    type=pending.type or "function",
                    function=FunctionCall(name=pending.name, arguments=pending.arguments),
                )
            )
        return calls


def _contradicts(pending: _PendingToolCall, delta: ToolCallDelta) -> bool:
    """True when this fragment's `id` cannot belong to the open call.

    Only a *stated* id counts. A fragment carrying no id — the ordinary
    arguments continuation — never contradicts anything, so a correctly
    streamed call still reassembles from as many fragments as the backend
    cares to send.
    """
    return bool(delta.id and pending.id and delta.id != pending.id)


def _merge_name(pending: _PendingToolCall, incoming: str | None) -> None:
    """Fold a name fragment in without ever gluing a name to a copy of itself.

    Two real backend habits collide here: splitting a name across fragments
    (rare, but it happens — tests/llm/test_client_qa.py pins it), and restating
    the whole name on every fragment of one call. Appending blindly serves the
    first and turns the second into `get_weatherget_weather`, which matches no
    tool we have. Appending only what we do not already hold serves both.
    """
    if not incoming:
        return
    if not pending.name:
        pending.name = incoming
    elif incoming != pending.name:
        pending.name += incoming


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class CircuitState(StrEnum):
    CLOSED = "closed"
    """Normal operation."""

    OPEN = "open"
    """Failing fast: calls are refused without touching the network."""

    HALF_OPEN = "half_open"
    """Cooldown has elapsed; exactly one probe call is in flight to test
    whether the host has recovered."""


class BreakerSnapshot(BaseModel):
    """Read-only view for the health dashboard, spec section 9/10."""

    model_config = ConfigDict(frozen=True)

    state: CircuitState
    consecutive_failures: int
    failure_threshold: int
    opened_at: datetime | None
    cooldown_seconds: float


class CircuitBreaker:
    """Per-backend circuit breaker, spec section 10: after N consecutive
    failures, fail fast for a cooldown instead of hammering a dead host, then
    probe.

    CLOSED --(N consecutive failures)--> OPEN
    OPEN --(cooldown elapses, next call)--> HALF_OPEN (one probe let through)
    HALF_OPEN --(probe succeeds)--> CLOSED
    HALF_OPEN --(probe fails)--> OPEN (cooldown restarts)

    A monotonic clock drives the cooldown maths so an NTP adjustment to the
    wall clock can't reopen or hold the breaker incorrectly; a wall-clock
    timestamp is kept separately purely for the dashboard to display.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at_monotonic: float | None = None
        self._opened_at_wall: datetime | None = None
        self._half_open_probe_in_flight = False
        self._lock = asyncio.Lock()

    async def before_call(self) -> None:
        """Raise LLMCircuitOpenError if this call must not be attempted now."""
        async with self._lock:
            if self._state is CircuitState.CLOSED:
                return

            assert self._opened_at_monotonic is not None  # noqa: S101 — invariant, not a test
            remaining = self._cooldown_seconds - (self._clock() - self._opened_at_monotonic)

            if self._state is CircuitState.OPEN and remaining <= 0:
                self._state = CircuitState.HALF_OPEN

            if self._state is CircuitState.HALF_OPEN and not self._half_open_probe_in_flight:
                self._half_open_probe_in_flight = True
                return

            raise LLMCircuitOpenError(
                "The language model hasn't been answering, so I'm giving it a "
                "short break before trying again.",
                cooldown_remaining_seconds=max(remaining, 0.0),
            )

    async def on_success(self) -> None:
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at_monotonic = None
            self._opened_at_wall = None
            self._half_open_probe_in_flight = False

    async def on_failure(self) -> None:
        async with self._lock:
            was_probing = self._state is CircuitState.HALF_OPEN
            self._half_open_probe_in_flight = False
            self._consecutive_failures += 1
            if was_probing or self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at_monotonic = self._clock()
                self._opened_at_wall = datetime.now(UTC)

    async def release_abandoned_probe(self) -> None:
        """Free the half-open probe slot for a call that ended without ever
        reporting success or failure to `on_success`/`on_failure` — spec
        section 10. This happens when a caller abandons a streaming
        generator mid-body (breaks out of an `async for` before `[DONE]` or
        a typed error resolves it): the outcome is genuinely unknown, so this
        does NOT touch `state` or `consecutive_failures` the way a real
        success or failure would. It only clears the "in flight" marker,
        because leaving it set would wedge the breaker in HALF_OPEN forever —
        every future call finding the one probe slot already (falsely) taken
        and refusing itself, even after the cooldown has long since elapsed
        again. Safe to call unconditionally: a no-op outside HALF_OPEN, since
        the flag is already False there."""
        async with self._lock:
            self._half_open_probe_in_flight = False

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            state=self._state,
            consecutive_failures=self._consecutive_failures,
            failure_threshold=self._failure_threshold,
            opened_at=self._opened_at_wall,
            cooldown_seconds=self._cooldown_seconds,
        )


class HealthStatus(BaseModel):
    """Result of `LLMClient.health_check()` — spec section 10, for the admin
    dashboard (section 9)."""

    model_config = ConfigDict(frozen=True)

    healthy: bool
    detail: str | None
    """Plain-English detail when unhealthy; None when healthy."""
    breaker: BreakerSnapshot


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


def _parse_sse_line(line: str) -> str | None:
    """Pull the payload out of one SSE line — spec section 5.3: `data: ...`
    lines terminated by a literal `data: [DONE]`. Blank lines (event
    separators) and `:`-prefixed comment/heartbeat lines are ignored rather
    than treated as malformed input, because backends are not perfectly
    consistent about emitting them."""
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    return line[len("data:") :].strip()


class LLMClient:
    """The core's only path to the LLM host — spec section 5.3.

    One instance per backend. Owns a persistent `httpx.AsyncClient` (connection
    pooling) and a `CircuitBreaker` shared by every call made through it, so a
    background health-check probe and a live chat request observe the same
    breaker state.

    Usable as an async context manager, or with explicit `aclose()`.
    """

    def __init__(
        self,
        config: LLMClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        headers = {"Content-Type": "application/json"}
        if config.api_key is not None:
            headers["Authorization"] = f"Bearer {config.api_key.get_secret_value()}"
        self._http = httpx.AsyncClient(
            timeout=config.httpx_timeout(),
            headers=headers,
            transport=transport,
        )
        self._breaker = CircuitBreaker(
            failure_threshold=config.failure_threshold,
            cooldown_seconds=config.cooldown_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def is_closed(self) -> bool:
        """Whether this client's connection pool has been shut down.

        Exists so whoever owns a client's lifetime can prove it did the right
        thing: a client nobody points at any more that was never closed is a
        leaked pool, and a client still in use that WAS closed loses an
        in-flight turn its connection mid-answer. Both are silent without this.
        """
        return self._http.is_closed

    @property
    def config(self) -> LLMClientConfig:
        """The settings this client was built from.

        Safe to expose: the model is frozen, so nothing can reconfigure a
        running client through it, and ``api_key`` is a ``SecretStr`` that
        redacts itself in any repr. Read by the assembly's per-role health view
        (ADR-0011) so the dashboard names the model actually in use rather than
        the one the settings file claims — which are different things for as
        long as it takes a save to be applied.
        """
        return self._config

    @property
    def breaker_snapshot(self) -> BreakerSnapshot:
        """For the health dashboard, spec section 9/10."""
        return self._breaker.snapshot()

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _log_failure(
        self,
        event: str,
        exc: Exception | None = None,
        *,
        status_code: int | None = None,
    ) -> None:
        # Structured, and deliberately narrow: base_url/model/exception type/
        # status code only. Never headers, never the request body, never the
        # API key — config.api_key is a SecretStr so accidentally logging
        # `self._config` itself would still redact it, but we don't log the
        # config at all.
        #
        # `exc` is optional because HTTP-status failures (401/403/429/500/
        # 503) have no exception to report — httpx raises nothing for those,
        # the caller reads it off `response.status_code` — but spec section
        # 10 wants them logged through this same path, at the same level, so
        # a failing backend is never invisible to the trace view/dashboard
        # just because it fails via status codes instead of raising.
        logger.warning(
            event,
            base_url=self._config.base_url,
            model=self._config.model,
            error_type=type(exc).__name__ if exc is not None else None,
            status_code=status_code,
        )

    async def _send(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        await self._breaker.before_call()
        try:
            response = await self._http.request(method, self._url(path), json=json_body)
        except httpx.TimeoutException as exc:
            await self._breaker.on_failure()
            self._log_failure("llm_request_timeout", exc)
            raise LLMTimeoutError(
                "The language model is taking too long to respond.", detail=repr(exc)
            ) from exc
        except httpx.HTTPError as exc:
            await self._breaker.on_failure()
            self._log_failure("llm_request_failed", exc)
            raise LLMConnectionError(
                "I can't reach the language model right now.", detail=repr(exc)
            ) from exc
        except Exception:
            # Safety net so an unanticipated exception can't leave a half-open
            # probe stuck in flight forever. Re-raised unchanged — this is not
            # trying to explain the error, only to keep the breaker honest.
            await self._breaker.on_failure()
            raise

        if response.status_code in (401, 403):
            await self._breaker.on_failure()
            self._log_failure("llm_request_status_error", status_code=response.status_code)
            raise LLMAuthenticationError(
                "The language model host rejected the request — check the configured API key."
            )
        if response.status_code >= 400:
            await self._breaker.on_failure()
            self._log_failure("llm_request_status_error", status_code=response.status_code)
            raise LLMResponseError(
                "The language model host returned an error.",
                detail=f"status={response.status_code}",
                status_code=response.status_code,
            )

        # NOTE: no `on_success()` here (defect #4 fix, spec section 10). A
        # 2xx status only means the headers arrived — the body might still
        # fail to parse. Success is recorded by the caller (chat_completion/
        # list_models) only once the body has actually been read and
        # validated, so a backend that reliably 200s with a garbage body
        # still trips the breaker.
        return response

    async def chat_completion(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> ChatCompletionResponse:
        """Non-streaming `/chat/completions`. `messages`/`tools`/`tool_choice`
        and any `extra` sampling parameters pass through untouched — see the
        module docstring for why."""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": list(messages),
            "stream": False,
            **extra,
        }
        if tools is not None:
            payload["tools"] = list(tools)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        response = await self._send("POST", "/chat/completions", json_body=payload)
        try:
            result = ChatCompletionResponse.model_validate(response.json())
        except ValueError as exc:
            # Defect #4 fix: a 2xx status with an unparseable body is a real
            # backend failure (misconfigured proxy, wrong content-type) and
            # must count against the breaker, not just raise to the caller.
            await self._breaker.on_failure()
            self._log_failure("llm_request_parse_failed", exc)
            raise LLMResponseError(
                "The language model host sent a response I couldn't understand.",
                detail=repr(exc),
            ) from exc
        await self._breaker.on_success()
        return result

    async def stream_chat_completion(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Streaming `/chat/completions` — SSE `data:` lines terminated by
        `[DONE]`, spec section 5.3/10 (this is how voice meets its ~2 second
        first-audio budget). Yields chunks faithfully, in order, for the
        caller to forward downstream and/or feed to a ToolCallAccumulator.

        Termination is checked deliberately tolerantly (defect fix, spec
        section 5.3/10): a stream counts as properly terminated if EITHER a
        `[DONE]` sentinel arrives OR any chunk carries a non-null
        `finish_reason`. Requiring `[DONE]` specifically would couple this
        client to one backend's convention and break the promise that
        swapping backends needs no code change. If the body ends with
        neither, that's a truncated reply — indistinguishable, if left
        unchecked, from a complete one — so `LLMStreamIncompleteError` is
        raised instead of returning silently.

        The circuit breaker only records success once the stream has fully
        and properly terminated, and records failure for any body-stage
        problem (transport error, truncation, unparseable chunk) — not at
        the status line, which arrives before any of that is known (defect
        fix, spec section 10: otherwise the breaker can never open for a
        backend that 200s and then dies mid-body)."""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": list(messages),
            "stream": True,
            # OpenAI's own switch for a real token count on a streamed reply
            # (`ChatCompletionChunk.usage`, above). Without it a streamed
            # `[DONE]` carries no count at all, on every backend tried so
            # far — checked directly against a live llama.cpp host,
            # 2026-09-02. `extra` is spread after this, so a caller with its
            # own reason to drop or replace it still can.
            "stream_options": {"include_usage": True},
            **extra,
        }
        if tools is not None:
            payload["tools"] = list(tools)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        await self._breaker.before_call()
        # `resolved` tracks whether this call's outcome has already been
        # reported to the breaker via on_success()/on_failure(), on ANY exit
        # path. It's needed because a caller can abandon this generator mid-
        # body (break out of an `async for`, or call `.aclose()` directly)
        # instead of letting it run to a typed error or natural completion —
        # that arrives here as GeneratorExit, which the `except` clauses
        # below don't (and must not) catch. Without this, an abandoned
        # HALF_OPEN probe would leave `_half_open_probe_in_flight` stuck True
        # forever; see `finally` below and CircuitBreaker.release_abandoned_probe.
        resolved = False
        terminated = False  # `[DONE]` seen, or a chunk had a finish_reason
        try:
            async with self._http.stream(
                "POST", self._url("/chat/completions"), json=payload
            ) as response:
                if response.status_code in (401, 403):
                    await response.aread()
                    await self._breaker.on_failure()
                    resolved = True
                    self._log_failure(
                        "llm_stream_status_error", status_code=response.status_code
                    )
                    raise LLMAuthenticationError(
                        "The language model host rejected the request — check "
                        "the configured API key."
                    )
                if response.status_code >= 400:
                    await response.aread()
                    await self._breaker.on_failure()
                    resolved = True
                    self._log_failure(
                        "llm_stream_status_error", status_code=response.status_code
                    )
                    raise LLMResponseError(
                        "The language model host returned an error.",
                        detail=f"status={response.status_code}",
                        status_code=response.status_code,
                    )

                async for line in response.aiter_lines():
                    payload_text = _parse_sse_line(line)
                    if payload_text is None:
                        continue
                    if payload_text == "[DONE]":
                        terminated = True
                        break
                    try:
                        chunk = ChatCompletionChunk.model_validate(json.loads(payload_text))
                    except ValueError as exc:
                        await self._breaker.on_failure()
                        resolved = True
                        self._log_failure("llm_stream_parse_failed", exc)
                        raise LLMResponseError(
                            "The language model host sent a response I couldn't understand.",
                            detail=repr(exc),
                        ) from exc
                    if any(choice.finish_reason is not None for choice in chunk.choices):
                        terminated = True
                    yield chunk

                if not terminated:
                    await self._breaker.on_failure()
                    resolved = True
                    raise LLMStreamIncompleteError(
                        "The reply was cut off before it finished — the "
                        "language model host closed the connection early."
                    )

                await self._breaker.on_success()
                resolved = True
        except httpx.TimeoutException as exc:
            await self._breaker.on_failure()
            resolved = True
            self._log_failure("llm_stream_timeout", exc)
            raise LLMTimeoutError(
                "The language model is taking too long to respond.", detail=repr(exc)
            ) from exc
        except httpx.HTTPError as exc:
            await self._breaker.on_failure()
            resolved = True
            self._log_failure("llm_stream_failed", exc)
            raise LLMConnectionError(
                "I can't reach the language model right now.", detail=repr(exc)
            ) from exc
        finally:
            if not resolved:
                # Reached only via GeneratorExit (caller abandoned the
                # stream) or some other exception not handled above. Either
                # way the outcome is genuinely unknown — don't guess success
                # or failure — but the half-open probe slot must be freed or
                # an abandoned (not resolved) probe wedges the breaker.
                await self._breaker.release_abandoned_probe()

    async def list_models(self) -> ModelsResponse:
        """`/v1/models` — also the cheap call `health_check` uses."""
        response = await self._send("GET", "/models")
        try:
            result = ModelsResponse.model_validate(response.json())
        except ValueError as exc:
            # Same defect #4 fix as chat_completion: don't record success
            # until the body is actually parsed.
            await self._breaker.on_failure()
            self._log_failure("llm_request_parse_failed", exc)
            raise LLMResponseError(
                "The language model host sent a response I couldn't understand.",
                detail=repr(exc),
            ) from exc
        await self._breaker.on_success()
        return result

    async def health_check(self) -> HealthStatus:
        """Liveness probe for the admin dashboard, spec section 9/10. Never
        raises — a dead or misbehaving host is reported as an unhealthy
        status with a plain-English detail, not an exception."""
        try:
            await self.list_models()
        except LLMClientError as exc:
            return HealthStatus(
                healthy=False, detail=exc.spoken_message, breaker=self.breaker_snapshot
            )
        return HealthStatus(healthy=True, detail=None, breaker=self.breaker_snapshot)

    async def context_length(self) -> int | None:
        """The backend's own context window, in tokens — or ``None`` when it
        genuinely cannot be known.

        Not part of the OpenAI-compatible surface this client otherwise
        speaks exclusively (spec section 5.3): it is llama.cpp's own `/props`
        endpoint, at the server root rather than under `/v1` — confirmed
        directly against a live host, 2026-09-02
        (`default_generation_settings.n_ctx`). Read anyway, on a best-effort
        basis, because a token count with nothing to measure it against is
        half an answer and llama.cpp is this project's own default backend
        (`LLMSettings.base_url`). Ollama and vLLM have no equivalent; asking
        them this question 404s, which this treats exactly like every other
        way of not knowing.

        **A guessed ceiling is worse than none.** A percentage against an
        invented denominator is a number a person would plan around, so any
        failure here — no such endpoint, an unreachable host, a body that
        does not have the shape expected — answers ``None`` rather than a
        default. Never raises, and never touches the circuit breaker: a host
        with no `/props` is not "down" for the chat completions this class
        exists to make, and one slow or missing introspection call must not
        open the breaker every reply depends on.

        A short, fixed timeout rather than this client's own (possibly very
        long — `read_timeout_seconds` is minutes, for a slow prompt) read
        timeout: this is meant to be a near-instant local lookup, and a page
        waiting minutes on it to fail would be a worse outcome than the
        count simply standing alone.
        """
        root = self._config.base_url
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        try:
            response = await self._http.get(
                f"{root.rstrip('/')}/props", timeout=self._config.connect_timeout
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        settings = body.get("default_generation_settings") if isinstance(body, dict) else None
        n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
        return n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else None
