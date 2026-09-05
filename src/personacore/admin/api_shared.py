"""What every module of the admin JSON API needs (ADR-0040).

The surface is split one concern to a module — health, plugins, personas,
trace, settings, keys, accounts — and each of those is meant to be opened and
read without the others. This module is the deliberate exception: the one error
shape, the one audit write, and the bundle of collaborators the router factory
builds once and hands to each ``register``.

:class:`AdminApiContext` is the joint, and it is written down here rather than
discovered per module on purpose: every register function builds against this
document, not against another register function.

**Nothing here decides who a caller is.** ``require_user`` is carried as data
and attached to the router by the factory in
:mod:`personacore.admin.routes`, which is what makes ADR-0032's default-deny a
property of one router rather than of twenty-five handlers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request

from personacore.admin.api_plugin_listing import _PluginScanCache
from personacore.admin.authn import AuthContext, LiveAuth
from personacore.admin.models import (
    AdminUser,
    ApiError,
    ConfigProblem,
    ConfigResponse,
)
from personacore.admin.protocols import (
    ApiKeyGateway,
    AuditGateway,
    EventBusSource,
    LLMHealthSource,
    PluginHealthSource,
    PluginToggle,
    PluginToolCaller,
    SecretNameSource,
)
from personacore.agent.personas import PersonaStore
from personacore.audit import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    Owner,
    Surface,
    get_correlation_id,
    get_logger,
)
from personacore.config.appdata import AppdataLayout
from personacore.plugins.packages import PackageLimits
from personacore.runbooks import RunbookStore

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _fail(
    status_code: int, message: str, problems: list[ConfigProblem] | None = None
) -> HTTPException:
    """Build the one error shape this API returns (see module docstring)."""
    return HTTPException(
        status_code=status_code,
        detail=ApiError(error=message, problems=problems or []).model_dump(),
    )



# ---------------------------------------------------------------------------
# The bundle every register is given
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminApiContext:
    """Everything a module of this surface is handed, built once by the factory.

    Every field is passed in rather than rebuilt, for the reason
    :func:`personacore.admin.routes.create_admin_router` gives for each of them
    one by one: a second scan cache, a second ``require_user`` or a second way
    of saving settings is a second answer to a question the core has already
    answered.
    """

    layout: AppdataLayout
    personas: PersonaStore
    audit: AuditGateway
    llm: LLMHealthSource
    bus: EventBusSource
    #: The one authorisation seam, as data. Attached to the router by the
    #: factory (ADR-0032); nothing here re-derives it.
    require_user: Callable[[Request], AdminUser]
    #: The holder the door lives in, so a door swapped at runtime is in force
    #: on the next request without anything being re-mounted (ADR-0010).
    live_auth: LiveAuth
    auth_context: AuthContext | None
    api_keys: ApiKeyGateway | None
    plugin_health: PluginHealthSource | None
    plugin_toggle: PluginToggle | None
    #: Start or stop one plugin in the running core, whichever collaborator
    #: turned out to be able to.
    live_toggle: Callable[..., Awaitable[None]] | None
    call_plugin_tool: PluginToolCaller | None
    secrets: SecretNameSource | None
    scans: _PluginScanCache
    disk_warning_bytes: int
    package_limits: PackageLimits
    #: Read the settings document. See :mod:`personacore.admin.api_config`.
    read_config: Callable[[], Awaitable[ConfigResponse]]
    #: Validate, write, apply live and audit — the one path, shared with the
    #: designed UI so that "saved" cannot come to mean two things.
    save_config: Callable[..., Awaitable[ConfigResponse]]
    #: The runbook file store (``working/contracts/runbook.md`` §6). ``None``
    #: on an assembly that never built one — see
    #: :func:`personacore.admin.routes.create_admin_router`'s own docstring.
    runbooks: RunbookStore | None


# ---------------------------------------------------------------------------
# The audit write every change on this surface makes
# ---------------------------------------------------------------------------


async def _record_change(
    audit: AuditGateway,
    user: AdminUser,
    *,
    action: str,
    outcome: AuditOutcome,
    detail: dict[str, Any],
) -> None:
    """Record one admin change — spec section 7 names "every admin change" as
    one of the four things the audit log must cover.

    Never raises. An audit store that cannot be written is already reported by
    the health endpoint; making the admin action itself fail here would take
    away the interface an operator needs to fix that.
    """
    try:
        await audit.record_audit(
            AuditRecord(
                correlation_id=get_correlation_id() or uuid4().hex,
                timestamp=datetime.now(UTC),
                surface=Surface.ADMIN_UI,
                owner=Owner.profile(user.id),
                category=AuditCategory.ADMIN_CHANGE,
                action=action,
                outcome=outcome,
                detail=detail,
            )
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.error("admin_audit_write_failed", action=action, error=repr(exc))


__all__ = [
    "AdminApiContext",
]
