"""The health dashboard's data, and the one route that answers with it.

Spec section 9's dashboard and spec section 10's "health checks on every
dependency", split out of :mod:`personacore.admin.routes` (ADR-0040).

Every probe in here is wrapped, and that is the module's whole character: a
health endpoint that fails because a dependency is unhealthy is the one
endpoint you cannot afford to lose. Nothing here raises, and the route always
answers ``200``.

It does not decide anything about plugins — :mod:`personacore.admin.api_plugin_listing`
does, and this module lays its rows out beside the LLM, the bus, the audit
store and the disk.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime

from fastapi import APIRouter

from personacore.admin.api_shared import AdminApiContext
from personacore.admin.models import (
    ComponentHealth,
    HealthState,
    PluginListing,
    SystemHealth,
)
from personacore.admin.protocols import (
    AuditGateway,
    EventBusSource,
    LLMHealthSource,
)
from personacore.audit import get_logger
from personacore.bus.client import bus_address
from personacore.config.appdata import AppdataLayout

logger = get_logger(__name__)

DEFAULT_DISK_WARNING_BYTES = 1 * 1024**3
"""1 GiB of appdata headroom. Below this the dashboard says so, because the
first symptom of a full volume is the audit log silently failing to write —
which is the one component whose failure hides every other failure."""


async def build_system_health(
    *,
    layout: AppdataLayout,
    llm: LLMHealthSource,
    bus: EventBusSource,
    audit: AuditGateway,
    listing: PluginListing,
    disk_warning_bytes: int = DEFAULT_DISK_WARNING_BYTES,
) -> SystemHealth:
    """Spec section 9's dashboard, spec section 10's "health checks on every
    dependency".

    Nothing here raises. A health endpoint that fails because a dependency is
    unhealthy is the one endpoint you cannot afford to lose, so every probe is
    wrapped and a probe that itself misbehaves is reported as ``unknown`` with
    the reason attached.
    """
    components = [
        *await _llm_components(llm),
        _bus_health(bus),
        _audit_health(layout, audit),
        _disk_health(layout, disk_warning_bytes),
        *_plugin_components(listing),
    ]
    if any(c.state is HealthState.FAILING for c in components):
        overall = HealthState.FAILING
    elif any(c.state is HealthState.UNKNOWN for c in components):
        overall = HealthState.UNKNOWN
    else:
        overall = HealthState.OK
    return SystemHealth(state=overall, checked_at=datetime.now(UTC), components=components)


async def _llm_components(llm: LLMHealthSource) -> list[ComponentHealth]:
    """One row per LLM role — ADR-0011.

    "The LLM is up" is no longer a single fact: five roles can point at five
    hosts, and an operator whose scene description stopped working needs to see
    *which* endpoint is down. A source that predates roles (a bare
    ``LLMClient``, as the CLI builds) still reports the single ``llm`` row it
    always did.

    Roles sharing an endpoint are probed once and reported twice. Five requests
    to the same host every time someone loads the dashboard would be a bug the
    dashboard caused.
    """
    views = getattr(llm, "role_views", None)
    if views is None:
        return [await _llm_role_health(llm, name="llm")]

    rows: list[ComponentHealth] = []
    probed: dict[str, ComponentHealth] = {}
    for view in views():
        seen = probed.get(view.endpoint)
        row = seen if seen is not None else await _llm_role_health(view, name="llm")
        probed.setdefault(view.endpoint, row)
        rows.append(
            row.model_copy(update={"name": f"llm.{view.role}", "facts": dict(view.facts)})
        )
    return rows


async def _llm_role_health(source: LLMHealthSource, *, name: str) -> ComponentHealth:
    try:
        result = await source.health_check()
    except Exception as exc:  # noqa: BLE001 - a probe must never take the dashboard down
        return ComponentHealth(
            name=name,
            state=HealthState.UNKNOWN,
            detail=f"The language model host could not be checked: {exc!r}",
        )
    if result.healthy:
        return ComponentHealth(name=name, state=HealthState.OK)
    return ComponentHealth(
        name=name,
        state=HealthState.FAILING,
        detail=result.detail or "The language model host is not responding.",
    )


def _bus_health(bus: EventBusSource) -> ComponentHealth:
    try:
        facts = bus.health.as_dict()
        connected = bool(facts.get("connected"))
        last_error = facts.get("last_error")
    except Exception as exc:  # noqa: BLE001 - see _llm_role_health
        return ComponentHealth(
            name="event_bus",
            state=HealthState.UNKNOWN,
            detail=f"The event bus could not be checked: {exc!r}",
        )
    # The address the running bus holds, named in the sentence rather than left
    # for the reader to find among the facts. An operator diagnosing a broker
    # that will not connect needs the error and the thing it was tried against
    # in the same breath: "Name or service not known" is a DNS failure, which an
    # IP literal cannot produce — unless the value being dialled is not the
    # value in the file, or has whitespace in it. Both are visible here now.
    where = f" at {bus_address(facts)}" if facts.get("host") is not None else ""
    if connected:
        return ComponentHealth(name="event_bus", state=HealthState.OK, facts=dict(facts))
    return ComponentHealth(
        name="event_bus",
        state=HealthState.FAILING,
        detail=(
            f"Not connected to the message broker{where}: {last_error}"
            if last_error
            else f"Not connected to the message broker{where}."
        ),
        facts=dict(facts),
    )


def _audit_health(layout: AppdataLayout, audit: AuditGateway) -> ComponentHealth:
    """Is the audit store writable?

    Checked rather than assumed because spec section 7 makes the audit log
    mandatory: an assistant that keeps answering while silently recording
    nothing is worse than one that stops, and this is the only place that
    difference becomes visible.

    Writability is tested with ``os.access`` on the audit directory instead of
    by writing a probe record — the store is the evidence trail, and salting it
    with health-check rows to prove it works corrupts the thing being proven.
    """
    directory = layout.audit
    try:
        version = audit.schema_version
    except Exception as exc:  # noqa: BLE001 - see _llm_role_health
        return ComponentHealth(
            name="audit_store",
            state=HealthState.FAILING,
            detail=(
                f"The audit database could not be read: {exc!r}. Nothing is being "
                "recorded until this is fixed."
            ),
        )
    if not directory.is_dir():
        return ComponentHealth(
            name="audit_store",
            state=HealthState.FAILING,
            detail=f"The audit directory {directory} is missing.",
            facts={"schema_version": version},
        )
    if not os.access(directory, os.W_OK):
        return ComponentHealth(
            name="audit_store",
            state=HealthState.FAILING,
            detail=(
                f"The audit directory {directory} is not writable by the core. "
                "Check the appdata volume's ownership and permissions."
            ),
            facts={"schema_version": version},
        )
    return ComponentHealth(
        name="audit_store",
        state=HealthState.OK,
        facts={"schema_version": version, "directory": str(directory)},
    )


def _disk_health(layout: AppdataLayout, warning_bytes: int) -> ComponentHealth:
    try:
        usage = shutil.disk_usage(layout.root)
    except OSError as exc:
        return ComponentHealth(
            name="appdata_disk",
            state=HealthState.UNKNOWN,
            detail=(
                f"Free space on {layout.root} could not be measured: "
                f"{exc.strerror or exc}."
            ),
        )
    facts = {
        "path": str(layout.root),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "warning_bytes": warning_bytes,
    }
    if usage.free < warning_bytes:
        return ComponentHealth(
            name="appdata_disk",
            state=HealthState.FAILING,
            detail=(
                f"Only {_human_bytes(usage.free)} free on the appdata volume. "
                "Memory, transcripts and the audit log all write here — free some "
                "space before it runs out."
            ),
            facts=facts,
        )
    return ComponentHealth(name="appdata_disk", state=HealthState.OK, facts=facts)


def _human_bytes(count: int) -> str:
    """Bytes as something readable in a dashboard, not as 10737418240."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - unreachable, loop always returns


def _plugin_components(listing: PluginListing) -> list[ComponentHealth]:
    """One dashboard row per plugin — spec section 9 lists "each plugin"
    alongside the LLM host and the bus, and a plugin that failed to load is a
    plugin whose row says why."""
    rows = [
        ComponentHealth(
            name=f"plugin:{plugin.name}",
            state=plugin.state,
            detail=plugin.detail
            or (
                "No plugin supervisor is running yet, so this plugin's live status "
                "is not known."
                if plugin.state is HealthState.UNKNOWN
                else None
            ),
            facts={"transport": plugin.transport, "restarts": plugin.restarts},
        )
        for plugin in listing.plugins
    ]
    rows.extend(
        ComponentHealth(
            name=f"plugin:{failure.name or failure.source}",
            state=HealthState.FAILING,
            detail=failure.reason,
            facts={"source": failure.source},
        )
        for failure in listing.failures
    )
    return rows


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register ``GET /admin/api/health`` on the guarded router."""
    api = router
    layout = ctx.layout
    llm = ctx.llm
    bus = ctx.bus
    audit = ctx.audit
    scans = ctx.scans
    disk_warning_bytes = ctx.disk_warning_bytes

    # -- health ------------------------------------------------------------

    @api.get("/health", response_model=SystemHealth, summary="System health dashboard")
    async def system_health() -> SystemHealth:
        """Spec section 9: LLM host reachability, event bus, each plugin, disk.

        Always ``200``: the caller reads ``state``. An HTTP error here would
        make "the dashboard is down" and "the system is down" indistinguishable.
        """
        return await build_system_health(
            layout=layout,
            llm=llm,
            bus=bus,
            audit=audit,
            listing=await scans.current(),
            disk_warning_bytes=disk_warning_bytes,
        )


__all__ = [
    "DEFAULT_DISK_WARNING_BYTES",
    "build_system_health",
    "register",
]
