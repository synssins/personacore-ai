"""``CompositeToolProvider`` — one `ToolProvider` wrapping the plugin host and
the core's own memory tools, joint J6/organ C of ``working/PLAN-memory.md``,
built to ``working/contracts/memory.md`` §2.

Contract §2: "The loop does not learn that some tools are the core's own."
This is the wrapper that makes that true — `AgentLoop` is handed exactly one
`ToolProvider`, and whether a given tool call runs in a plugin process or
directly against `MemoryStore` is decided here, one name check, and nowhere
else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import structlog

from personacore.agent.protocols import ToolProvider, ToolResult, ToolSpec
from personacore.contracts import RiskLevel
from personacore.memory.tools import MemoryTools
from personacore.workspace_tools import WorkspaceTools

logger = structlog.get_logger(__name__)

_MEMORY_PREFIX = "memory."
_WORKSPACE_PREFIX = "workspace."


class CompositeToolProvider:
    """`ToolProvider` over ``host`` (a `PluginHost`, or `None`) plus the
    core's own tool families: `memory`'s two, and — workspace contract §5 —
    `workspace`'s three.

    `host` is `None` for the same reason `AgentLoop._tools` already tolerates
    it — a core with no plugin host still has `memory.remember`/`recall` if
    a persona has memory on, and this class is what makes that true without
    `AgentLoop` growing a second tools seam. `memory` and `workspace` are each
    independently optional for the same reason: a core with no bundled
    embedder has no `MemoryTools` to hand in (`server.py`), and a caller that
    never wires workspace support up (a test, or a build without it) should
    not have to construct one it will never use.
    """

    def __init__(
        self,
        host: ToolProvider | None,
        memory: MemoryTools | None = None,
        workspace: WorkspaceTools | None = None,
    ) -> None:
        self._host = host
        self._memory = memory
        self._workspace = workspace

    async def list_tools(self) -> Sequence[ToolSpec]:
        """The host's tools (if any, and if it answers) plus the core's own.

        A host that raises listing its own tools costs the host's tools, not
        the core's — the same "a dead plugin is not a dead turn" rule
        `AgentLoop._tool_schemas` already applies one layer up, applied here
        too so a broken host cannot also take memory or workspace down with
        it.
        """
        host_specs: list[ToolSpec] = []
        if self._host is not None:
            try:
                host_specs = list(await self._host.list_tools())
            except Exception as exc:  # noqa: BLE001 - a dead host is not a dead memory
                logger.error("composite_host_listing_failed", error=repr(exc))
                host_specs = []
        memory_specs = self._memory.specs() if self._memory is not None else []
        workspace_specs = self._workspace.specs() if self._workspace is not None else []
        return [*host_specs, *memory_specs, *workspace_specs]

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
        """Route by name: `memory.*` and `workspace.*` run here, everything
        else goes to the host unchanged — including a host of `None`, which
        the loop's own `_invoke` never reaches for a name it did not offer,
        and which `ToolProvider` implementations elsewhere already answer
        with a refusal rather than an exception.

        `caller_detail["conversation_id"]` is how a workspace call learns
        which conversation it belongs to — the same slot
        `MemoryTools.call`'s own `conversation_id` argument already reads
        (contract §5.1, plan joint J5). No protocol change was needed for
        this: `ToolProvider.call_tool`'s `caller_detail` was already an open
        `Mapping[str, Any]`, and `agent/loop.py:_handle_tool_call` already
        put `conversation_id` on it for memory's sake before this tool
        family existed.
        """
        if name.startswith(_WORKSPACE_PREFIX):
            if self._workspace is None:
                return ToolResult(ok=False, error=f"I can't reach {name} right now.")
            detail = caller_detail or {}
            return await self._workspace.call(
                name,
                dict(arguments),
                conversation_id=detail.get("conversation_id"),
            )

        if not name.startswith(_MEMORY_PREFIX):
            if self._host is None:
                return ToolResult(ok=False, error=f"I can't reach {name} right now.")
            return await self._host.call_tool(
                name,
                arguments,
                risk_ceiling=risk_ceiling,
                correlation_id=correlation_id,
                owner=owner,
                surface=surface,
                caller_detail=caller_detail,
            )

        if self._memory is None:
            return ToolResult(ok=False, error=f"I can't reach {name} right now.")
        detail = caller_detail or {}
        persona = detail.get("persona")
        if not persona:
            return ToolResult(ok=False, error="There's no persona on this turn.")
        if owner is None:
            return ToolResult(ok=False, error="There's nobody this turn belongs to.")
        return await self._memory.call(
            name,
            dict(arguments),
            owner=owner,
            persona=str(persona),
            model=detail.get("model"),
            conversation_id=detail.get("conversation_id"),
            correlation_id=correlation_id or "",
        )


__all__ = ["CompositeToolProvider"]
