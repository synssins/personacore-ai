"""The agent loop — the conversation engine, spec section 3.2 (pull path).

Persona plus context in, streamed reply and tool calls out, with the risk gate
of sections 3.2/7 in front of every tool. See ``loop.py`` for the design
rationale, ``personas.py`` for hot-swap and hot-reload (section 5.5),
``untrusted.py`` for how outside content is fenced (section 7), and
``protocols.py`` for the seams — tools, confirmation, memory — this component
talks through.
"""

from __future__ import annotations

from personacore.agent.errors import (
    AgentError,
    PersonaError,
    PersonaInvalidError,
    PersonaNotFoundError,
)
from personacore.agent.loop import (
    DEFAULT_SAFETY_BLOCK,
    MAX_CLIENT_TOOLS,
    RISK_RANK,
    AgentEvent,
    AgentEventType,
    AgentLoop,
    AgentLoopConfig,
    ClientTool,
    ClientToolCall,
    ClientToolFunction,
    ClientToolResult,
    ClientToolRound,
    ConversationMessage,
    ToolGateDecision,
    TurnContext,
    TurnRequest,
)
from personacore.agent.personas import Persona, PersonaStore
from personacore.agent.protocols import (
    AuditSink,
    ChatStreamer,
    ConfirmationOutcome,
    ConfirmationProvider,
    ConfirmationRequest,
    MemoryItem,
    MemoryProvider,
    MemoryRecallRequest,
    PersonaLLMSource,
    ToolFile,
    ToolProvider,
    ToolResult,
    ToolSpec,
    WorkspaceAccess,
)
from personacore.agent.untrusted import UntrustedKind, wrap_untrusted

__all__ = [
    "DEFAULT_SAFETY_BLOCK",
    "MAX_CLIENT_TOOLS",
    "RISK_RANK",
    "AgentError",
    "AgentEvent",
    "AgentEventType",
    "AgentLoop",
    "AgentLoopConfig",
    "AuditSink",
    "ChatStreamer",
    "ConfirmationOutcome",
    "ConfirmationProvider",
    "ConfirmationRequest",
    "ClientTool",
    "ClientToolCall",
    "ClientToolFunction",
    "ClientToolResult",
    "ClientToolRound",
    "ConversationMessage",
    "MemoryItem",
    "MemoryProvider",
    "MemoryRecallRequest",
    "PersonaLLMSource",
    "Persona",
    "PersonaError",
    "PersonaInvalidError",
    "PersonaNotFoundError",
    "PersonaStore",
    "ToolFile",
    "ToolGateDecision",
    "ToolProvider",
    "ToolResult",
    "ToolSpec",
    "TurnContext",
    "TurnRequest",
    "UntrustedKind",
    "WorkspaceAccess",
    "wrap_untrusted",
]
