"""The seams the agent loop talks through — spec sections 5.1, 5.3, 6, 7, 8.

Every collaborator of :class:`personacore.agent.loop.AgentLoop` is a Protocol
defined here rather than a concrete import, for three different reasons:

* **Tools** (:class:`ToolProvider`) — the MCP plugin host (spec section 5.1) is
  a separate component built in parallel. The loop needs exactly two verbs from
  it: list what is callable, and call one by name. Depending on the shape
  instead of the module keeps the two components independently testable and
  keeps "everything is a plugin" (section 4.2) from turning into a cycle.
* **Confirmation** (:class:`ConfirmationProvider`) — spec section 5.1 says a
  ``confirm``-risk tool needs "spoken or UI confirmation". Voice asks and
  listens, the admin UI shows a dialog, the exposed API (section 5.4) can only
  answer from something the client already sent. Confirmation is therefore an
  interface the caller supplies, never a mechanism the loop assumes.
* **Memory** (:class:`MemoryProvider`) — spec section 6 is explicit that memory
  is a plugin, not core code, and it is P1. This is the seam it will arrive
  through. There is deliberately no implementation and no stub in core: an
  assistant that pretends to remember is worse than one that plainly does not.

* **Which model answers** (:class:`PersonaLLMSource`) — a persona may carry a
  connection of its own (ADR-0036), and turning one into a client means pooling
  per endpoint and sharing a circuit breaker with whatever role points at the
  same host. That is the assembly's knowledge, not the loop's, so the loop asks
  a question and is handed a streamer. :class:`~personacore.agent.personas.Persona`
  IS imported concretely here — it is the loop's own data type, not a
  collaborator it would otherwise depend on a module for.

:class:`AuditSink` is the narrow view of ``personacore.audit.AuditStore`` the
loop uses. The real store satisfies it structurally; it is written as a
Protocol only so tests can record calls without a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from personacore.agent.personas import Persona
from personacore.audit import AuditRecord, TranscriptRecord
from personacore.contracts import MemoryScope, RiskLevel
from personacore.llm import ChatCompletionChunk

# ---------------------------------------------------------------------------
# Tools — spec sections 5.1 and 7
# ---------------------------------------------------------------------------


class ToolSpec(BaseModel):
    """One callable tool, as the plugin host presents it to the agent.

    ``risk`` comes from the plugin manifest (spec section 5.1: "manifest
    declares, core enforces"). It is required and typed: an unknown or absent
    risk must be impossible to construct here, because the gate in
    ``loop.py`` refuses anything it cannot rank and a defaulted ``safe``
    would fail open.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    """Fully qualified ``"<plugin>.<tool>"`` — the same spelling
    ``PolicyProfile.allowed_tools`` uses, so the allowlist check is a plain
    set membership test with no name mangling in between."""

    risk: RiskLevel
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the arguments, passed to the model untouched."""


class ToolResult(BaseModel):
    """What came back from a tool call.

    A failed call is a value, not an exception: the model is told the tool
    failed and gets to say so in the persona's own words (spec section 10,
    "I can't reach the music system right now"), which is a better outcome
    than an aborted turn.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    content: str = ""
    """Whatever the tool returned, rendered as text. UNTRUSTED — the loop
    fences it via ``personacore.agent.untrusted`` before it reaches the
    model."""

    error: str | None = None
    """Plain-English failure, safe to speak aloud. Set when ``ok`` is False."""

    audited: bool = False
    """The callee already wrote this call's audit record.

    Set by :class:`personacore.plugins.host.PluginHost`, which is the boundary
    that knows how long the call took. When it is true the agent loop does not
    write a second ``tool_call`` record: one call must produce one record, or
    the trace shows the same call twice with different fields (PC-014) and the
    per-surface retention window is applied to two different rows.

    Defaults to false so a provider that does not audit — a test double, or a
    future in-process tool source — still gets its call recorded by the loop.
    """


@runtime_checkable
class ToolProvider(Protocol):
    """The MCP plugin host, as the agent loop sees it (spec section 5.1)."""

    async def list_tools(self) -> Sequence[ToolSpec]:
        """Every tool currently callable, across all healthy plugins. Risk
        levels come from each plugin's manifest."""
        ...

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        risk_ceiling: RiskLevel | None = None,
        correlation_id: str | None = None,
        owner: Any = None,
        surface: Any = None,
        caller_detail: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        """Invoke one tool.

        The loop's risk gate has already run and never calls this for a refused
        tool. ``risk_ceiling`` is passed anyway, so the host can enforce the same
        limit independently at the plugin boundary: two checks that agree cost
        nothing, and a host whose own check is disabled by default is a control
        that only looks present.

        The rest is the caller's attribution, and it is not optional politeness:
        the provider writes the audit record for this call, and a record without
        the caller's ``owner`` and ``surface`` is filed against the household on
        ``system``. That hides it from PC-019's user and surface filters and
        ages it out on the wrong per-surface retention window (ADR-0004) — an
        operator who shortens retention for one surface does not get what they
        asked for. ``owner`` is an ``Owner`` and ``surface`` a ``Surface``,
        typed as ``Any`` here only to keep this protocol free of an audit
        import. ``caller_detail`` carries what the caller knows and the provider
        cannot see — the confirmation outcome, above all — so it lands on the
        one record rather than needing a second.
        """
        ...


# ---------------------------------------------------------------------------
# Confirmation — spec sections 5.1, 7, 8
# ---------------------------------------------------------------------------


class ConfirmationOutcome(StrEnum):
    """The three answers, kept distinct on purpose.

    ``UNAVAILABLE`` is not the same as ``DENIED`` — nobody said no, there was
    simply no way to ask (a headless API call, a voice channel that timed
    out). Both block the tool; only the audit log needs to tell them apart, so
    that "the front door was never unlocked because nobody could be asked" is
    a legible line in the trace view rather than a phantom refusal.
    """

    GRANTED = "granted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class ConfirmationRequest(BaseModel):
    """What the channel is being asked to put to the human."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    profile_id: str
    tool_name: str
    risk: RiskLevel
    arguments: dict[str, Any] = Field(default_factory=dict)
    prompt: str
    """A complete question, phrased to be spoken aloud or shown verbatim —
    the channel should not have to compose English of its own."""


@runtime_checkable
class ConfirmationProvider(Protocol):
    """How this caller asks a human. Supplied per surface by whoever builds the
    turn; ``None`` means there is no channel, which the gate treats as
    ``UNAVAILABLE`` and therefore a refusal (fail closed)."""

    async def request_confirmation(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        ...


# ---------------------------------------------------------------------------
# Memory — spec section 6 (a plugin, P1). Seam only.
# ---------------------------------------------------------------------------


class MemoryItem(BaseModel):
    """One recalled memory. ``text`` is untrusted — see ADR-0003."""

    model_config = ConfigDict(extra="forbid")

    text: str
    source: str = "memory"
    score: float | None = None


class MemoryRecallRequest(BaseModel):
    """The scope is passed, not assumed: spec section 8 and ADR-0003 make
    memory visibility a property of the caller's policy profile, and the loop
    refuses to call a memory plugin at all when the scope is ``none``."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    profile_id: str
    scope: MemoryScope
    query: str
    limit: int = Field(default=8, ge=1)


@runtime_checkable
class MemoryProvider(Protocol):
    """The P1 memory plugin's read side (spec section 6). Unimplemented in
    core by design — the loop composes retrieved items as untrusted context if
    a provider is supplied, and simply has no memory if one is not."""

    async def recall(self, request: MemoryRecallRequest) -> Sequence[MemoryItem]:
        ...


# ---------------------------------------------------------------------------
# The LLM and the audit store, as narrow shapes
# ---------------------------------------------------------------------------


class ChatStreamer(Protocol):
    """The one method the loop needs from ``personacore.llm.LLMClient``.

    Spec section 10 gives voice roughly two seconds to first audio, which is
    only reachable by streaming (section 5.3), so the loop has no
    non-streaming path at all — there is nothing here to accidentally call.
    """

    def stream_chat_completion(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> AsyncIterator[ChatCompletionChunk]:
        ...


class PersonaLLMSource(Protocol):
    """Which client answers for one persona — the seam ADR-0036 opens.

    A persona may carry its own connection, and the thing that turns that
    connection into a client is not the loop's business: clients are pooled per
    endpoint and share a circuit breaker with whatever role points at the same
    host, and only the assembly knows about either. So the loop asks a
    question — "who answers for this character?" — and is handed a streamer.

    It is one call per round rather than one per turn on purpose: this is what
    several personas in one conversation, each on a different model, will need,
    and a per-turn answer cached on the turn would have to be undone to get
    there.

    Implementations may raise :class:`~personacore.llm.errors.LLMClientError`
    for a connection that cannot be used at all — a persona naming an API key
    secret this core was never given. The loop already says that kind of
    failure out loud (spec §10) rather than crashing on it.
    """

    def stream_for(self, persona: Persona) -> ChatStreamer:
        ...


class AuditSink(Protocol):
    """The write side of ``personacore.audit.AuditStore``, spec section 7."""

    async def record_audit(self, record: AuditRecord) -> AuditRecord:
        ...

    async def record_transcript(self, record: TranscriptRecord) -> TranscriptRecord:
        ...


__all__ = [
    "AuditSink",
    "ChatStreamer",
    "ConfirmationOutcome",
    "ConfirmationProvider",
    "ConfirmationRequest",
    "MemoryItem",
    "MemoryProvider",
    "MemoryRecallRequest",
    "PersonaLLMSource",
    "ToolProvider",
    "ToolResult",
    "ToolSpec",
]
