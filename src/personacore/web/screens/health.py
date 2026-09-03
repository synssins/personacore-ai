"""The health dashboard (spec section 9) and its refresh fragment.

Everything here is rendered from ``build_system_health`` - the same builder
``GET /admin/api/health`` answers with - for the reason ADR-0007 gave for
building the API first: two code paths that answer "what is the state of this
system" differently is one code path too many.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from personacore.admin.models import (
    ComponentHealth,
    PluginListing,
    SystemHealth,
)
from personacore.boot.degrade import degraded
from personacore.bus.client import bus_address, has_whitespace, visible
from personacore.web.shared import (
    _STATE_WORD,
    UIContext,
    _dev_admin_user,
)

_STATE_LABEL: dict[str, str] = {
    "ok": "ok",
    "down": "failing",
    "degraded": "unknown",
    "off": "switched off",
}

_COMPONENT_NAMES: dict[str, str] = {
    "llm": "Language model",
    "event_bus": "Event bus",
    "audit_store": "Audit store",
    "appdata_disk": "Appdata disk",
}

_HEALTHY_DETAIL: dict[str, str] = {
    "event_bus": "Connected to the message broker.",
    "audit_store": "Recording every turn and every admin change.",
}


def _fact_lines(facts: dict[str, Any], human_bytes: Callable[[int], str]) -> list[str]:
    """A component's free-form facts as one readable line each.

    Byte counts are rendered through the same helper the JSON API's dashboard
    uses, because ``11811160064`` on a health screen is a number nobody reads.

    A fact with no value is dropped rather than printed: "last error: None" is
    Python leaking onto a screen that spec section 9 requires to be plain
    English, and it reads to an operator as an error whose text went missing.
    Booleans get "yes"/"no" for the same reason.
    """
    lines: list[str] = []
    for key, value in facts.items():
        if value is None:
            continue
        label = key.replace("_", " ")
        if key.endswith("_bytes") and isinstance(value, int) and not isinstance(value, bool):
            lines.append(f"{label.removesuffix(' bytes')}: {human_bytes(value)}")
        elif isinstance(value, bool):
            lines.append(f"{label}: {'yes' if value else 'no'}")
        else:
            lines.append(f"{label}: {value}")
    return lines


BUS_TARGET_KEYS = ("host", "port", "client_id", "username", "password_set")
"""The health facts that describe *what the bus is dialling* rather than how it
is getting on. Rendered by :func:`bus_fact_lines` instead of by the generic
``key: value`` walk, because each one needs saying differently: an address wants
its whitespace drawn, a missing user name wants the word "none" rather than
being dropped, and a password wants to be reported as present without being
reported at all."""


def bus_fact_lines(facts: dict[str, Any]) -> list[str]:
    """What the running bus is pointed at, in words an operator can check.

    This is the fix for the hour nobody gets back. Health used to report the
    failure and never the attempt, so ``MqttError: [Errno -2] Name or service
    not known`` — a DNS failure, which an IP address literally cannot cause —
    left three indistinguishable explanations on the table: the value is wrong,
    the value is right but the running process never got it, or the value has a
    space in it and is therefore not the value it appears to be. Printing what
    the live object holds settles all three at a glance.

    Every value is quoted and has its whitespace drawn (:func:`visible`), and a
    host with whitespace in it gets told so outright — the point is that a
    wrong-looking value should never be able to look right.

    The password is reported as set or not set, and never in any other way:
    not the value, not its length, not the name of the secret behind it.
    """
    lines: list[str] = []
    host = facts.get("host")
    if host is not None:
        line = f"broker: {bus_address(facts)}"
        if isinstance(host, str) and has_whitespace(host):
            line += " — this address contains whitespace, which is never valid"
        lines.append(line)
    client_id = facts.get("client_id")
    if client_id is not None:
        lines.append(f"client id: {visible(str(client_id))}")
    username = facts.get("username")
    lines.append(
        f"user name: {visible(str(username))}"
        if username is not None
        else "user name: none — connecting anonymously"
    )
    lines.append(
        "password: a password is set" if facts.get("password_set") else "password: no password"
    )
    return lines


def _component_fact_lines(
    component: ComponentHealth, human_bytes: Callable[[int], str]
) -> list[str]:
    """One component's facts as lines, with the bus's address facts said properly.

    The bus is the only component whose facts include configuration rather than
    only measurements, so it is the only one that needs wording of its own; the
    counters beside them still go through the generic walk.
    """
    facts = dict(component.facts)
    if component.name != "event_bus":
        return _fact_lines(facts, human_bytes)
    rest = {key: value for key, value in facts.items() if key not in BUS_TARGET_KEYS}
    return [*bus_fact_lines(facts), *_fact_lines(rest, human_bytes)]


def _component_name(name: str) -> str:
    """The API's identifier as a heading someone can read."""
    if name.startswith("plugin:"):
        return f"Plugin · {name.removeprefix('plugin:')}"
    if name.startswith("llm."):
        return f"Language model · {name.removeprefix('llm.')}"
    return _COMPONENT_NAMES.get(name, name.replace("_", " ").capitalize())


def _healthy_detail(component: ComponentHealth, human_bytes: Callable[[int], str]) -> str:
    """What a working component's detail line says.

    The API leaves ``detail`` empty when a component is fine — the field exists
    to explain a failure. The design gives every row a detail line, so a healthy
    row gets a short true sentence rather than an empty paragraph, which would
    leave a ragged gap between rows and read as missing text.
    """
    if component.name == "appdata_disk":
        free = component.facts.get("free_bytes")
        path = component.facts.get("path")
        if isinstance(free, int) and path:
            return f"{human_bytes(free)} free on {path}."
    if component.name == "event_bus":
        # Named even when it is working. "Connected." leaves an operator no way
        # to notice they are connected to the wrong broker, which is the other
        # half of the same confusion a bare error message causes.
        if component.facts.get("host") is not None:
            return f"Connected to the message broker at {bus_address(component.facts)}."
        return _HEALTHY_DETAIL["event_bus"]
    if component.name.startswith("llm"):
        return "The language model host answered."
    if component.name.startswith("plugin:"):
        return "Running."
    return _HEALTHY_DETAIL.get(component.name, "Working.")


def health_rows(
    health: SystemHealth,
    listing: PluginListing,
    human_bytes: Callable[[int], str],
) -> list[dict[str, Any]]:
    """One dashboard row per component, in the design's vocabulary.

    ``listing`` is consulted only to tell "switched off on purpose" from "not
    running", which the API keeps as two separate fields (``enabled`` beside
    ``state``) precisely so a dashboard does not have to conflate them.
    """
    disabled = {plugin.name for plugin in listing.plugins if not plugin.enabled}
    rows: list[dict[str, Any]] = []
    for component in health.components:
        plugin_name = (
            component.name.removeprefix("plugin:") if component.name.startswith("plugin:") else None
        )
        if plugin_name is not None and plugin_name in disabled:
            state = "off"
        else:
            state = _STATE_WORD[component.state]
        rows.append(
            {
                "state": state,
                "name": _component_name(component.name),
                "state_label": _STATE_LABEL[state],
                "detail": component.detail or _healthy_detail(component, human_bytes),
                "facts": _component_fact_lines(component, human_bytes),
                # Only the plugin list exists in this slice; a row with no page
                # to open gets no "open" button rather than a link to a 404.
                "href": "/admin/plugins" if plugin_name is not None else None,
            }
        )
    return rows


def health_notices(
    dev_admin_user: str | None,
    skipped: Any = (),
) -> list[dict[str, str]]:
    """The banners across the top of the dashboard — dangerous states, said out
    loud (ADR-0018: a dangerous posture that is invisible is one nobody
    reviews).

    Two backends today. The development authentication bypass, and every
    optional piece this core skipped at boot (ADR-0040 §3) — the piece, what it
    costs, and the exception that stopped it. That last one is the difference
    between a degradation and an outage with better manners: without it, a core
    running with no voice and no plugins looks exactly like a healthy one.

    ``skipped`` defaults to nothing so that a caller which predates degradable
    loading still gets the bypass banner it asked for.

    Keyless access (PC-113) and the retention purge's health are the design's
    other two examples and are not built, so nothing is invented for them.
    """
    notices: list[dict[str, str]] = []
    for piece in skipped:
        notices.append(
            {
                # `warn` and not `danger`: the core is doing what it can, and
                # colouring a missing voice the same as an open admin door
                # teaches an operator to ignore both. The word is the CSS
                # class the banner takes (`.banner.warn`), not a synonym.
                "tone": "warn",
                "title": f"Skipped at startup: {piece.piece}.",
                "body": (
                    f"{piece.costs}. It was skipped rather than stopping the "
                    f"core, and the reason it gave was: {piece.error}"
                ),
                "href": "/admin/logs",
                "link_label": "Logs",
            }
        )
    if not dev_admin_user:
        return notices
    notices.append(
        {
            "tone": "danger",
            "title": "Admin authentication is bypassed.",
            "body": (
                f"Every request to this interface is treated as “{dev_admin_user}”, so "
                "anyone who can reach this port is an admin. Unset the environment "
                "variable PERSONACORE_ADMIN_DEV_USER and restart once a login proxy is "
                "in front."
            ),
            "href": "/admin/core",
            "link_label": "Core settings",
        }
    )
    return notices


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the health dashboard and the fragment its refresh swaps in."""
    # Imported inside `register` for the reason the factory imports it inside
    # itself: `admin/routes.py` builds this router, so a top-level import back
    # into it would be a cycle.
    from personacore.admin.routes import _human_bytes

    templates = ctx.templates
    _shell = ctx.shell
    _health = ctx.health


    # -- health ------------------------------------------------------------

    @router.get("/health", response_class=HTMLResponse, summary="Health dashboard")
    async def health_page(request: Request) -> HTMLResponse:
        """Spec section 9's dashboard, and the interface's landing screen."""
        health, listing = await _health()
        return templates.TemplateResponse(
            request=request,
            name="health.html",
            context={
                **await _shell(request, "health"),
                "overall": _STATE_WORD[health.state],
                "checked_ago": None,
                "notices": health_notices(_dev_admin_user(request), degraded(request.app)),
                "components": health_rows(health, listing, _human_bytes),
            },
        )

    @router.get(
        "/health/fragment", response_class=HTMLResponse, summary="Health rows (15s poll)"
    )
    async def health_fragment(request: Request) -> HTMLResponse:
        """The polled body of the dashboard — notices and rows, nothing else.

        The notices are inside the poll boundary on purpose (the design says
        so): a bypass that has been turned off has to stop being announced
        without anyone reloading the page.
        """
        health, listing = await _health()
        return templates.TemplateResponse(
            request=request,
            name="fragments/health_body.html",
            context={
                "notices": health_notices(_dev_admin_user(request), degraded(request.app)),
                "components": health_rows(health, listing, _human_bytes),
            },
        )
