"""Mounting the two surfaces onto one listener — ADR-0040.

Moved out of ``personacore.server``. Each surface is imported late, so which
surfaces a build actually has is discovered rather than assumed and ``/health``
can report the answer.

**The two surfaces are not equally optional, and ADR-0040 §3 says which is
which.** The ``/v1`` API is skipped and reported if it will not mount: a
container that starts without it is still an assistant. The admin surface is
not — it is the only place a wrong setting, a missing secret or a broken plugin
can be fixed, so a core that could not mount it refuses to boot rather than
serving a health check that says everything is fine. Both used to be tolerated;
the tolerant one was how a core could come up with no way in and look healthy.

``[keyless]`` lives here for the same reason (ADR-0018): it is read per request
off ``app.state.settings`` so that saving the switch takes effect on the next
request rather than the next restart — and so that turning it OFF closes the
door just as promptly, which is the direction that matters. It is handed to the
``/v1`` router and to nothing else: two doors, two decisions (ADR-0032).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI

from personacore.admin.authn import AuthContext, LiveAuth
from personacore.agent.loop import AgentLoop
from personacore.agent.personas import PersonaStore
from personacore.audit.store import AuditStore
from personacore.auth.method import AuthDecision
from personacore.boot.degrade import DegradedPieces, degraded
from personacore.boot.llm import LLMRoster
from personacore.bus.client import EventBus
from personacore.config import AppdataLayout, SecretStore
from personacore.config.settings import KeylessSettings
from personacore.contracts.policy import PolicyProfile
from personacore.plugins.discovery import PluginDiscovery
from personacore.preferences import PreferenceStore

log = structlog.get_logger(__name__)


def _api_key_store(layout: AppdataLayout) -> Any:
    """The admin side's handle on API keys, for issuing and revoking.

    A separate instance from the one the /v1 surface verifies against is fine:
    both read the same ``api_keys`` table on every lookup, so a key issued here
    is usable immediately -- and a key revoked here stops working immediately,
    which is the half that matters and the reason neither instance caches.
    """
    from personacore.api import ApiKeyStore

    return ApiKeyStore(layout)


def _mount_admin(
    app: FastAPI,
    layout: AppdataLayout,
    discovery: PluginDiscovery,
    personas: PersonaStore,
    preferences: PreferenceStore,
    audit: AuditStore,
    llm: LLMRoster,
    bus: EventBus,
    trusted_header: str,
    auth: AuthDecision,
    auth_context: AuthContext | None,
    auth_live: LiveAuth,
    apply_settings: Any,
    chat: Any,
    plugin_health: Any,
    plugin_toggle: Any,
) -> None:
    # No guard, on purpose (ADR-0040 §3). An ImportError here means a build
    # without its own front door, and a mount that raises means one whose front
    # door is broken; both are worse to serve than to refuse, because the
    # container's health check would answer either of them with "ok".
    from personacore.admin import create_admin_router

    app.include_router(
        create_admin_router(
            layout=layout,
            discovery=discovery,
            personas=personas,
            preferences=preferences,
            audit=audit,
            llm=llm,
            bus=bus,
            trusted_user_header=trusted_header,
            # PC-294's one rule, handed down rather than re-derived. The
            # admin surface never asks the environment which door is open.
            auth=auth,
            auth_context=auth_context,
            # The holder, so the routers ask which door is open per request
            # instead of closing over the one this process booted with.
            auth_live=auth_live,
            api_keys=_api_key_store(layout),
            secrets=SecretStore(layout),
            # ADR-0016: a settings field can be filled by asking the plugin.
            # Passing the host's own call_tool is the whole wiring — it
            # already enforces the manifest's declared risk, applies the
            # plugin's permissions, and records the call in the audit log
            # and the trace, which is what the ADR requires of a lookup.
            call_plugin_tool=plugin_toggle.call_tool,
            apply_settings=apply_settings,
            chat=chat,
            plugin_health=plugin_health,
            plugin_toggle=plugin_toggle,
        )
    )
    app.state.surfaces.add("admin")
    log.info("surface_mounted", surface="admin")


def _keyless_settings(app: FastAPI) -> KeylessSettings:
    """``[keyless]`` as the running process has it (ADR-0018).

    From ``app.state.settings``, which ``_apply_settings`` replaces on every
    save, so this is what is in force rather than what the file said at boot.
    A ``getattr`` because ``/health`` is served by assemblies that never set it.
    """
    settings = getattr(app.state, "settings", None)
    return getattr(settings, "keyless", None) or KeylessSettings()


def _keyless_profile(app: FastAPI) -> PolicyProfile | None:
    """The profile a caller with no key gets, or ``None`` if there is no such
    caller.

    ``None`` when the switch is off, so "keyless is off" is an absence at the
    door rather than a disabled profile the router has to remember to check.
    """
    keyless = _keyless_settings(app)
    return keyless.profile() if keyless.enabled else None


def _mount_openai(
    app: FastAPI, layout: AppdataLayout, agent: AgentLoop, audit: AuditStore
) -> None:
    """Mount the exposed OpenAI-compatible API (spec §5.4).

    Kept tolerant on purpose: a container that refuses to start because one of
    two surfaces is absent is less useful than one that starts and reports which
    surfaces it has. The admin surface above is the exception, and ADR-0040 §3
    is why.

    Tolerant no longer means silent. A surface that is absent or would not mount
    is recorded as a skipped piece, so ``/health`` and the Health screen name it
    with the reason rather than an operator inferring it from a list that is one
    entry short.
    """
    pieces: DegradedPieces = degraded(app)
    costs = "the OpenAI-compatible API on /v1 is not served; the admin UI is unaffected"
    try:
        from personacore.api import ApiKeyStore, create_openai_router
    except ImportError as exc:
        log.info("surface_absent", surface="openai", detail=str(exc))
        pieces.skip("the OpenAI-compatible API", costs=costs, error=exc)
        return
    try:
        app.include_router(
            create_openai_router(
                agent=agent,
                keys=ApiKeyStore(layout),
                audit=audit,
                # ADR-0018. Read off `app.state.settings` per request rather
                # than captured here, so the switch takes effect on the next
                # request after it is saved (ADR-0010) instead of at the next
                # restart -- and so turning it OFF closes the door just as
                # promptly, which is the direction that matters.
                #
                # Handed to the `/v1` router and to nothing else. The admin
                # surface is mounted above with no knowledge of this setting at
                # all: two doors, two decisions (ADR-0032).
                keyless=lambda: _keyless_profile(app),
            )
        )
    except Exception as exc:  # noqa: BLE001 - a broken surface must not stop the rest
        log.error("surface_mount_failed", surface="openai", error=repr(exc))
        pieces.skip("the OpenAI-compatible API", costs=costs, error=exc)
        return
    app.state.surfaces.add("openai")
    log.info("surface_mounted", surface="openai")
