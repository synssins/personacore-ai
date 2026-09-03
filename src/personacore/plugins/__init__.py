"""Plugin discovery, supervision and the MCP host — spec section 5.1.

Four layers, deliberately separable:

* ``discovery`` — a directory on disk becomes a validated :class:`PluginRecord`.
  Knows nothing about MCP and launches nothing.
* ``mcp_client`` — one MCP connection, over stdio or HTTP, plus the
  least-privilege child environment (spec section 7).
* ``supervisor`` — one plugin kept alive: start, watch, restart with backoff,
  give up, and contain anything it does on the way (spec section 5.1's "a bad
  plugin never takes the core down").
* ``host`` — all of them as one :class:`personacore.agent.protocols.ToolProvider`,
  with the risk gate and the audit record at the boundary.
"""

from personacore.plugins.discovery import (
    DiscoveryResult,
    PluginDiscovery,
    PluginRecord,
)
from personacore.plugins.errors import (
    PluginLoadFailure,
    PluginRejected,
    PluginSecurityError,
)
from personacore.plugins.health import PluginHealth, PluginState
from personacore.plugins.host import PluginHost, PluginHostConfig
from personacore.plugins.mcp_client import (
    ChildEnvironmentError,
    McpSessionFactory,
    MissingPluginSecrets,
    PluginContractMismatch,
    PluginSession,
    PluginToolError,
    PluginTransportError,
    RemoteTool,
    RemoteToolResult,
    SessionFactory,
    build_child_environment,
)
from personacore.plugins.supervisor import PluginSupervisor, SupervisorConfig

__all__ = [
    "ChildEnvironmentError",
    "DiscoveryResult",
    "McpSessionFactory",
    "MissingPluginSecrets",
    "PluginContractMismatch",
    "PluginDiscovery",
    "PluginHealth",
    "PluginHost",
    "PluginHostConfig",
    "PluginLoadFailure",
    "PluginRecord",
    "PluginRejected",
    "PluginSecurityError",
    "PluginSession",
    "PluginState",
    "PluginSupervisor",
    "PluginToolError",
    "PluginTransportError",
    "RemoteTool",
    "RemoteToolResult",
    "SessionFactory",
    "SupervisorConfig",
    "build_child_environment",
]
