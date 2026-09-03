"""The admin API, and the admin UI built on it — spec section 9.

ADR-0007 deferred the admin *UI* and built the API first, because the API is the
part that survives the design work: "it is a contract, it is versioned and
documented like one". The design landed (ADR-0020); the API did not change.

What this package is:

* ``routes`` — :func:`create_admin_router`, a factory returning an
  ``APIRouter``. It builds no ``FastAPI`` app and starts no server; whoever
  assembles the app mounts this alongside the OpenAI-compatible router
  (spec section 5.4).
* ``models`` — the request/response contract.
* ``protocols`` — the seams every collaborator arrives through, so this package
  imports no broker, database or HTTP client.
* ``config_io`` — ``core.toml`` reading and writing, with the plain-English
  validation spec section 9 requires and the "secrets by name, never by value"
  rule spec section 7 requires.
* ``ui`` — the designed admin UI (ADR-0020), served under ``/admin`` from the
  same factory and behind the same authentication as the API. It is a consumer
  of the API above like any other. ADR-0007's design-free test surface, which
  stood in for it, was deleted rather than evolved when it landed.
"""

from __future__ import annotations

from personacore.admin.models import (
    SHOWN_ONCE_WARNING,
    AdminUser,
    ApiError,
    ApiKeyIssued,
    ApiKeyIssueRequest,
    ApiKeyListing,
    ApiKeyView,
    ComponentHealth,
    ConfigProblem,
    ConfigResponse,
    ConfigUpdateRequest,
    ConfigUpdateResponse,
    HealthState,
    InstallResult,
    PersonaDetail,
    PersonaListing,
    PersonaSelected,
    PersonaSummary,
    PluginFailureView,
    PluginInstalled,
    PluginListing,
    PluginLookupRequest,
    PluginLookupResponse,
    PluginLookupResult,
    PluginToggled,
    PluginUninstalled,
    PluginView,
    ReloadResult,
    SystemHealth,
    TraceEntry,
    TraceFilters,
    TraceKind,
    TracePage,
)
from personacore.admin.protocols import (
    ApiKeyGateway,
    AuditGateway,
    ChatRunner,
    ChatStreamEvent,
    ChatStreamRunner,
    ChatTurnResult,
    EventBusSource,
    LLMHealthSource,
    LLMRoleView,
    PluginHealthSource,
    PluginRuntimeStatus,
    PluginToggle,
    PluginToolCaller,
    SecretNameSource,
    SettingsApplier,
    ToolCallOutcome,
)
from personacore.admin.routes import create_admin_router, make_admin_user_dependency

__all__ = [
    "SHOWN_ONCE_WARNING",
    "AdminUser",
    "ApiError",
    "ApiKeyGateway",
    "ApiKeyIssued",
    "ApiKeyIssueRequest",
    "ApiKeyListing",
    "ApiKeyView",
    "AuditGateway",
    "ChatRunner",
    "ChatStreamEvent",
    "ChatStreamRunner",
    "ChatTurnResult",
    "ComponentHealth",
    "ConfigProblem",
    "ConfigResponse",
    "ConfigUpdateRequest",
    "ConfigUpdateResponse",
    "EventBusSource",
    "HealthState",
    "InstallResult",
    "LLMHealthSource",
    "LLMRoleView",
    "PersonaDetail",
    "PersonaListing",
    "PersonaSelected",
    "PersonaSummary",
    "PluginFailureView",
    "PluginHealthSource",
    "PluginInstalled",
    "PluginListing",
    "PluginLookupRequest",
    "PluginLookupResponse",
    "PluginLookupResult",
    "PluginRuntimeStatus",
    "PluginToggle",
    "PluginToggled",
    "PluginToolCaller",
    "PluginUninstalled",
    "PluginView",
    "ReloadResult",
    "SecretNameSource",
    "SettingsApplier",
    "SystemHealth",
    "TraceEntry",
    "TraceFilters",
    "TraceKind",
    "ToolCallOutcome",
    "TracePage",
    "create_admin_router",
    "make_admin_user_dependency",
]
