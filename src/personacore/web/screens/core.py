"""The Core settings screen (spec section 4): listen address, the door,
the broker, and how long records are kept.

The form and the raw JSON editor are two presentations of one document, and
both save through the JSON API's own helper - this screen never validates a
setting itself. What a posted form means is in ``core_form``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from functools import partial
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from personacore.admin.authn import BYPASS_NOT_A_TEST, require_admin
from personacore.admin.models import (
    ConfigResponse,
)
from personacore.audit.models import (
    Surface,
)
from personacore.auth.method import (
    CHOOSABLE,
    DEFAULT_METHOD,
    coerce_method,
    help_text,
    label,
)
from personacore.web.screens.core_form import (
    AUTH_METHOD_FIELD,
    WYOMING_ENABLED_FIELD,
    WYOMING_HOST_FIELD,
    WYOMING_PORT_FIELD,
    core_payload,
)
from personacore.web.shared import (
    RETENTION_DEFAULT_KEY,
    UIContext,
    _dev_admin_user,
    _section,
    _text,
    current_config,
    settings_problems,
)

RETENTION_LABELS: dict[str, str] = {
    RETENTION_DEFAULT_KEY: "Everything else",
    Surface.VOICE.value: "Voice",
    Surface.API.value: "API",
    Surface.ADMIN_UI.value: "Admin",
    Surface.ANONYMOUS.value: "Anonymous",
    Surface.SYSTEM.value: "System",
}
"""Every retention window that has a home, in the design's vocabulary.

ADR-0004 exists because a child uses this system: these are privacy controls,
not a cleanup schedule, so all of them are on the screen rather than only the
ones somebody happened to set.
"""

def purge_schedule() -> str:
    """When the retention purge actually runs, in words.

    The design's copy said "nightly at 03:00". That was never true of this
    implementation — the purge runs once at startup and then on a fixed
    interval — and a privacy control that describes a schedule it does not keep
    is worse than one that says nothing. Read from the server's own constant so
    the sentence cannot drift from the loop; imported inside the function
    because ``personacore.server`` is what mounts this router.
    """
    from personacore.server import RETENTION_PURGE_INTERVAL_SECONDS

    seconds = RETENTION_PURGE_INTERVAL_SECONDS
    if seconds % 3600 == 0:
        hours = seconds // 3600
        every = "every hour" if hours == 1 else f"every {hours} hours"
    else:
        minutes = max(round(seconds / 60), 1)
        every = "every minute" if minutes == 1 else f"every {minutes} minutes"
    return f"at startup and then {every}"


def retention_rows(
    retention: Any,
    *,
    typed: Mapping[str, str] | None = None,
    errors: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One input per retention window, with the operator's input preserved.

    ``typed`` outranks what is on disk so a refused save re-renders exactly what
    was submitted — a form that silently reverts to the stored value while
    printing an error is how somebody fixes the same typo three times. A typed
    key nobody recognises still gets a row for the same reason: the value has to
    be visible beside the message that refused it.
    """
    section = retention if isinstance(retention, dict) else {}
    per_surface = section.get("per_surface_days")
    per_surface = per_surface if isinstance(per_surface, dict) else {}

    stored: dict[str, Any] = {RETENTION_DEFAULT_KEY: section.get("default_days", "")}
    for key, value in per_surface.items():
        stored[str(key)] = value

    keys = list(RETENTION_LABELS)
    for key in (*stored, *(typed or {})):
        if key not in keys:
            keys.append(key)

    rows: list[dict[str, Any]] = []
    for key in keys:
        if typed is not None and key in typed:
            value: Any = typed[key]
        else:
            value = stored.get(key, "")
        rows.append(
            {
                "surface": key,
                "label": RETENTION_LABELS.get(key, key),
                "value": "" if value is None else value,
                "error": (errors or {}).get(key),
            }
        )
    return rows


MEMORY_LABELS: dict[str, str] = {
    "quiet_minutes": "Quiet minutes before a conversation is reviewed",
    "recall_limit": "Memories recalled per turn (applies after a restart)",
    "half_life_days": "Recency half-life (days)",
    "duplicate_threshold": "Duplicate threshold (0–1)",
    "short_term_days": "Short-term memories expire after (days) unless promoted",
    "recall_floor": "Minimum match to recall (0–1)",
}
"""``working/contracts/memory.md`` §9 — the five ``[memory]`` settings, in the
order the screen shows them. Fixed rather than built from what is on disk,
unlike :data:`RETENTION_LABELS`'s per-surface table: there is no open-ended
key here, so the row list can never grow one nobody typed."""

MEMORY_FIELD_PREFIX = "memory_"


def memory_rows(
    memory: Any, *, typed: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """One input per ``[memory]`` setting, with the operator's input preserved.

    Same shape and the same reason as :func:`retention_rows`: ``typed``
    outranks what is on disk so a refused save re-renders exactly what was
    submitted rather than silently reverting it.
    """
    section = memory if isinstance(memory, dict) else {}
    typed = typed or {}
    rows: list[dict[str, Any]] = []
    for key, field_label in MEMORY_LABELS.items():
        value: Any = typed[key] if key in typed else section.get(key, "")
        rows.append({"key": key, "label": field_label, "value": "" if value is None else value})
    return rows


def memory_payload_and_typed(
    settings: Mapping[str, Any], form: Mapping[str, Any] | Any
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The submitted ``[memory]`` fields as a section to merge into the save
    payload, plus what was typed for a refused save to re-render.

    Kept beside this screen rather than in ``core_form.py``'s own
    ``core_payload`` — this task's file list does not touch that module —
    but the behaviour matches retention's exactly: an empty box means
    "unset", so the field is left out of the section and the settings
    model's own default takes over. Passed through as **text**, unparsed,
    for the same reason retention and the Wyoming port are: the settings
    model already bounds each of these and says so in plain English, and a
    second opinion here would be a second place for that sentence to come
    from and to drift.
    """
    memory: dict[str, Any] = {}
    typed: dict[str, str] = {}
    for key in MEMORY_LABELS:
        raw = str(form.get(f"{MEMORY_FIELD_PREFIX}{key}") or "").strip()
        typed[key] = raw
        if raw:
            memory[key] = raw
    return (memory or None), typed


WORKSPACE_LABELS: dict[str, str] = {
    "tool_result_chars": "Most characters of one tool result the model receives",
    "long_item_chars": "Save a tool result as a file when longer than",
    "max_file_bytes": "Largest workspace file (bytes)",
    "max_workspace_bytes": "Largest workspace per conversation (bytes)",
}
"""``working/contracts/workspace.md`` §9 — the four ``[workspace]`` settings,
in the order the screen shows them. Fixed rather than built from what is on
disk, the same reason :data:`MEMORY_LABELS` is: there is no open-ended key
here, so the row list can never grow one nobody typed."""

WORKSPACE_FIELD_PREFIX = "workspace_"


def workspace_rows(
    workspace: Any, *, typed: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """One input per ``[workspace]`` setting, with the operator's input
    preserved — the same shape as :func:`memory_rows`, for the same reason:
    ``typed`` outranks what is on disk so a refused save re-renders exactly
    what was submitted rather than silently reverting it."""
    section = workspace if isinstance(workspace, dict) else {}
    typed = typed or {}
    rows: list[dict[str, Any]] = []
    for key, field_label in WORKSPACE_LABELS.items():
        value: Any = typed[key] if key in typed else section.get(key, "")
        rows.append({"key": key, "label": field_label, "value": "" if value is None else value})
    return rows


def workspace_payload_and_typed(
    settings: Mapping[str, Any], form: Mapping[str, Any] | Any
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The submitted ``[workspace]`` fields as a section to merge into the
    save payload, plus what was typed for a refused save to re-render.

    Matches :func:`memory_payload_and_typed` exactly: an empty box means
    "unset" so the settings model's own default takes over, and every value
    is passed through as text, unparsed — the settings model already bounds
    each of these and says so in plain English.
    """
    workspace: dict[str, Any] = {}
    typed: dict[str, str] = {}
    for key in WORKSPACE_LABELS:
        raw = str(form.get(f"{WORKSPACE_FIELD_PREFIX}{key}") or "").strip()
        typed[key] = raw
        if raw:
            workspace[key] = raw
    return (workspace or None), typed


def _and_list(parts: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c`` — a sentence, not a comma-joined list."""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def listen_address(server: Any) -> str:
    """``[server]`` as the one ``host:port`` box the design draws."""
    host = _text(server, "host")
    port = _text(server, "port")
    if host and port:
        return f"{host}:{port}"
    return host or port


def playback_choices(selected: str) -> list[dict[str, Any]]:
    """The household playback rule, in the design's radio shape (ADR-0030).

    Three, not two. "Everyone chooses" is the absence of a rule, and folding it
    into "never" would silence people who had asked for speech — a different
    outcome wearing the same label.
    """
    choices = (
        ("unset", "Everyone chooses", "Each person sets this on their own profile."),
        ("on", "Always play", "Replies play by themselves for everybody."),
        ("off", "Never play", "Nobody's replies play by themselves."),
    )
    return [
        {"value": value, "label": label, "help": help_text, "selected": value == selected}
        for value, label, help_text in choices
    ]


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"})
"""Addresses only this machine can reach.

Used to decide which of two true sentences to print, never to refuse anything:
an operator who widens the address has made the second decision
:mod:`personacore.config.wyoming` describes, and the screen's job is to say so
rather than to argue with it.
"""


def wyoming_section(
    stored: Any,
    *,
    typed: Mapping[str, str] | None = None,
    running: bool = False,
    bound_port: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """``[wyoming]`` as the three controls and the two facts around them.

    The facts are what a switch cannot say. **The effective address** is here
    because the thing an operator has to do next is type it into Home
    Assistant, and a screen that made them find that in a document has not
    finished the job. **Whether it is listening right now** is read off the
    running service rather than off the file, because the two can still differ
    — not because a save waits for a restart, which it no longer does, but
    because a bind can fail. A port already in use is the whole of that gap
    now, so it is reported with the reason rather than as a state of its own.

    ``typed`` outranks the stored document for the same reason
    :func:`retention_rows` honours it — a refused save re-renders what was
    submitted, not what is on disk.
    """
    from personacore.config.wyoming import WyomingSettings

    defaults = WyomingSettings()
    section = stored if isinstance(stored, dict) else {}
    typed = typed or {}

    if WYOMING_ENABLED_FIELD in typed:
        enabled = bool(typed[WYOMING_ENABLED_FIELD])
    else:
        enabled = bool(section.get("enabled") or False)
    host = typed.get(WYOMING_HOST_FIELD, _text(section, "host"))
    port = typed.get(WYOMING_PORT_FIELD, _text(section, "port"))

    # What the operator would type into Home Assistant. An empty box means the
    # default, so the address shown is what the core would actually bind rather
    # than a blank — and once it is listening, the port it really got, which is
    # not always the one asked for.
    effective_host = host or defaults.host
    effective_port = str(bound_port) if running and bound_port else (port or str(defaults.port))
    return {
        "enabled": enabled,
        "host": host,
        "port": port,
        "host_placeholder": defaults.host,
        "port_placeholder": str(defaults.port),
        "address": f"{effective_host}:{effective_port}",
        "running": running,
        "loopback": effective_host in LOOPBACK_HOSTS,
        # Why it is on and silent — only ever a bind that failed, and only
        # while it is still on and still silent. A reason left standing after
        # the operator fixed the port would send them chasing a fault that is
        # no longer there.
        "error": error if enabled and not running else None,
    }


WYOMING_SETTLE_SECONDS = 2.0
"""How long a save that moved the listener waits before drawing the switch.

The apply runs as a task of its own, so the save request would otherwise
overtake it and render the state the listener was in a moment ago — the switch
would come back saying "not listening" on the save that switched it on, which
is the same lie in a new place. Binding a socket takes microseconds; this is a
ceiling for the case where something has gone wrong, not a delay anyone waits
out.
"""


async def wyoming_settled(app: FastAPI, *, enabled: bool) -> None:
    """Wait, briefly, for the listener to be doing what was just saved.

    Returns as soon as the running state matches the saved switch, or as soon
    as the bind has failed and there is a reason to print. A core assembled
    without the service at all — the admin router mounted on a bare app in a
    unit test — has nothing to wait for and returns at once.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WYOMING_SETTLE_SECONDS
    while True:
        state = app.state
        service = getattr(state, "wyoming", None)
        if service is None:
            return
        if bool(getattr(service, "running", False)) == enabled:
            return
        if getattr(state, "wyoming_error", None) is not None:
            return
        if loop.time() >= deadline:
            return
        await asyncio.sleep(0.01)


def auth_method_choices(selected: str) -> list[dict[str, Any]]:
    """The doors an operator may pick between, in the order they are offered.

    Built from :data:`personacore.auth.method.CHOOSABLE` rather than listed
    here, so a door added to the core cannot fail to appear on the screen that
    is supposed to be the only place it is chosen. The words come from the same
    module, which is also where the refusals that tell somebody to come to this
    screen read them from.
    """
    return [
        {
            "value": method.value,
            "label": label(method),
            "help": help_text(method),
            "selected": method.value == selected,
        }
        for method in CHOOSABLE
    ]


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the Core settings page and its two saves."""
    templates = ctx.templates
    require_user = ctx.require_user
    save_config = ctx.save_config
    _shell = ctx.shell
    _current_config = partial(current_config, ctx.layout)


    # -- core settings -----------------------------------------------------

    def _core_context(
        request: Request,
        current: ConfigResponse,
        *,
        typed: Mapping[str, str] | None = None,
        retention_typed: Mapping[str, str] | None = None,
        memory_typed: Mapping[str, str] | None = None,
        workspace_typed: Mapping[str, str] | None = None,
        errors: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """The design's ``core`` object, built from the settings that exist.

        ``instance_name`` and ``log_level`` have no home in
        :class:`CoreSettings` and neither security *toggle* is a setting, so all
        four arrive marked not-editable and the template disables them and
        prints ``later``. Sign-in (``[auth] method``) is the exception and is
        genuinely editable. The purge banner is read from the running
        application's own status — the same dictionary ``/health`` reports —
        rather than being assumed healthy.
        """
        settings = current.settings
        bus = settings.get("bus")
        state = request.app.state
        running = getattr(state, "bus", None)
        live_bus: dict[str, Any] = (
            dict(running.health.as_dict()) if running is not None else {}
        )
        status_report = getattr(state, "retention_status", None)
        status_report = status_report if isinstance(status_report, dict) else {}
        failures = int(status_report.get("consecutive_failures") or 0)
        typed = typed or {}
        stored_playback = settings.get("playback")
        stored_playback = stored_playback if isinstance(stored_playback, dict) else {}
        playback_autoplay = str(stored_playback.get("autoplay") or "unset")
        if playback_autoplay not in {"unset", "on", "off"}:
            playback_autoplay = "unset"
        stored_dictation = settings.get("dictation")
        stored_dictation = stored_dictation if isinstance(stored_dictation, dict) else {}
        dictation_browser = bool(stored_dictation.get("browser") or False)
        # Absent on a core assembled without it, and on a bare `create_app`
        # under test. Defaulted rather than assumed present, the same bargain
        # the voice screens make with their registry.
        wyoming_service = getattr(state, "wyoming", None)

        def field(name: str, fallback: str) -> str:
            return typed[name] if name in typed else fallback

        # Which way in is open (ADR-0023, applied live per ADR-0010). `method`
        # is what the settings file says, `running` is the door the process is
        # actually using — read off `LiveAuth`, which is what the one seam
        # delegates to, so it is the running answer rather than a boot-time
        # copy of it.
        #
        # After a save the two agree, because the swap happens on the request
        # that saved it and before this renders. They differ in exactly two
        # cases, and both are worth showing: a refused save re-renders the
        # choice that was typed beside the door still in force, and a hand-edit
        # of core.toml is a choice nothing has applied. `chosen` rather than
        # `method` on the decision, so the comparison is configuration against
        # configuration and the break-glass bypass does not read as a
        # difference the operator caused.
        stored_method = _text(settings.get("auth"), "method") or DEFAULT_METHOD.value
        method = field(AUTH_METHOD_FIELD, stored_method)
        live = getattr(state, "live_auth", None)
        decision = live.decision if live is not None else getattr(state, "auth_decision", None)
        if decision is not None:
            running, bypassed = decision.chosen, decision.bypassed
        else:
            # No assembled application behind this render. `method` is the best
            # available answer, but it may be something a refused save typed
            # into the form, so it is coerced rather than trusted: a screen that
            # raised on the way to displaying an error message would replace the
            # error with a 500.
            try:
                running = coerce_method(method)
            except ValueError:
                running = DEFAULT_METHOD
            bypassed = False

        return {
            "instance_name": "",
            "instance_editable": False,
            "listen": field("listen", listen_address(settings.get("server"))),
            "log_level": "info",
            "log_level_editable": False,
            "auth_bypass": bool(_dev_admin_user(request)),
            "auth_bypass_editable": False,
            "playback": {
                "autoplay": playback_autoplay,
                "choices": playback_choices(playback_autoplay),
            },
            "dictation": {"browser": dictation_browser},
            # The Wyoming server (`[wyoming]`). Read alongside the running
            # service so the screen can separate "switched on in the file" from
            # "listening now" — see `wyoming_section`. The service object is
            # replaced when the setting changes, so it is fetched per render
            # rather than held.
            "wyoming": wyoming_section(
                settings.get("wyoming"),
                typed=typed,
                running=bool(getattr(wyoming_service, "running", False)),
                bound_port=getattr(wyoming_service, "bound_port", None),
                error=getattr(state, "wyoming_error", None),
            ),
            "auth": {
                "method": method,
                "choices": auth_method_choices(method),
                "running": running.value,
                "running_label": label(running),
                # The bypass is in force, so neither door is being used right
                # now whatever this control says, and a change made under it has
                # not been tried against anything. Worth a word: otherwise the
                # screen reads as though the choice below is what is letting
                # this very request through, and the operator removes the bypass
                # to find out it is not.
                "bypassed": bypassed,
                "bypass_note": BYPASS_NOT_A_TEST if bypassed else None,
            },
            # Read off the running bus, not off the document: this says whether
            # the connection is authenticated, and the two ways to supply a
            # password (`password` here, `password_secret` from the store) both
            # end up in the same place. Asking the document would have to know
            # about both and would still be guessing whether the secret
            # resolved.
            "bus_auth": bool(_text(bus, "username")) and bool(live_bus.get("password_set")),
            "bus_auth_editable": False,
            "bus": {
                "host": field("bus_host", _text(bus, "host")),
                "port": field("bus_port", _text(bus, "port")),
                "client_id": field("bus_client_id", _text(bus, "client_id")),
                "username": field("bus_username", _text(bus, "username")),
                # Whether, never what. The settings document this screen reads
                # already carries the redaction marker rather than the password
                # (config_io.WRITE_ONLY_PATHS), and even that never reaches the
                # template: a boolean cannot be un-masked by viewing source.
                "password_set": bool(_text(bus, "password")),
                # A leftover `password_secret` naming a secret that is not there
                # is what the operator has right now, and it reads as "no
                # password" rather than as a failure — typing one in the box is
                # the fix, and an error would send them looking for a different
                # one. The sentence itself still reaches Health.
                "password_secret": _text(bus, "password_secret"),
                "password_secret_broken": bool(
                    getattr(state, "bus_password_degraded", None)
                ),
            },
            "retention": retention_rows(
                settings.get("retention"), typed=retention_typed, errors=errors
            ),
            "memory": memory_rows(settings.get("memory"), typed=memory_typed),
            "workspace": workspace_rows(settings.get("workspace"), typed=workspace_typed),
            "purge": {
                "ok": failures == 0,
                "schedule": purge_schedule(),
                "last_success": status_report.get("last_success") or "never",
                "last_error": status_report.get("last_error") or "",
                "consecutive_failures": failures,
                # Re-running the purge on demand is the scheduler's business,
                # and this core exposes no way to ask it for a pass.
                "can_retry": False,
            },
        }

    def _core_form(
        request: Request, current: ConfigResponse, **kwargs: Any
    ) -> HTMLResponse:
        save_result = kwargs.pop("save_result", None)
        return templates.TemplateResponse(
            request=request,
            name="fragments/core_form.html",
            context={"core": _core_context(request, current, **kwargs), "save_result": save_result},
        )

    async def _core_page(
        request: Request,
        *,
        tab: str = "form",
        raw_error: str | None = None,
        raw_saved: str | None = None,
    ) -> HTMLResponse:
        current, config_error = _current_config()
        context: dict[str, Any] = {
            **await _shell(request, "core"),
            "active_tab": "raw" if tab == "raw" else "form",
            "config_error": config_error,
            "raw_error": raw_error,
            "raw_saved": raw_saved,
            "raw_json": "",
            "save_result": None,
        }
        if current is not None:
            context["core"] = _core_context(request, current)
            context["raw_json"] = json.dumps(current.settings, indent=2, sort_keys=True)
        return templates.TemplateResponse(request=request, name="core.html", context=context)

    @router.get("/core", response_class=HTMLResponse, summary="Core settings")
    async def core_page(request: Request, tab: str = "form") -> HTMLResponse:
        """Spec §9's config editing, as a form rather than a JSON textarea.

        The textarea is still there on the second tab, for everything the form
        has no field for — the LLM roles live on the Models screen, the default
        persona on Personas, and per-role timeouts nowhere else at all.

        Admin-only, same as both saves: a member who could open this would see
        a screen full of controls that exist only to refuse them.
        """
        require_admin(require_user(request))
        return await _core_page(request, tab=tab)

    @router.post("/core", response_class=HTMLResponse, summary="Save core settings")
    async def core_save(request: Request) -> HTMLResponse:
        """Validate, write, apply and audit — through the JSON API's own path.

        Retention is the reason this screen is a form (ADR-0004): the windows
        are a privacy control a household can be asked about, so they are
        editable one box at a time and a refused value comes back **in the box
        it came from**, with the API's own sentence under it.
        """
        user = require_admin(require_user(request))
        current, _unreadable = _current_config()
        if current is None:
            # The file stopped being readable between opening this screen and
            # saving it. That is a whole-page state and the form's swap target
            # no longer exists, so htmx is redirected at the page rather than
            # being handed a document to nest inside a form.
            page = await _core_page(request)
            page.headers["HX-Retarget"] = "body"
            page.headers["HX-Reswap"] = "outerHTML"
            return page

        form = await request.form()
        payload, typed, retention_typed = core_payload(current, form)
        memory_section, memory_typed = memory_payload_and_typed(current.settings, form)
        if memory_section is not None:
            payload["memory"] = memory_section
        else:
            payload.pop("memory", None)
        workspace_section, workspace_typed = workspace_payload_and_typed(current.settings, form)
        if workspace_section is not None:
            payload["workspace"] = workspace_section
        else:
            payload.pop("workspace", None)
        try:
            # The request, not just the payload: `[auth] method` is on this
            # form, and the API's save path asks the door named there whether
            # it would still admit the caller who sent this. Without it the
            # swap is refused rather than taken on trust.
            saved = await save_config(
                payload, user, action="config.update", request=request
            )
        except HTTPException as exc:
            errors, message = settings_problems(exc)
            return _core_form(
                request,
                current,
                typed=typed,
                retention_typed=retention_typed,
                memory_typed=memory_typed,
                workspace_typed=workspace_typed,
                errors=errors,
                save_result={"kind": "invalid", "message": message},
            )

        if saved.settings == current.settings:
            result = {
                "kind": "nothing",
                "message": "Nothing changed.",
            }
        else:
            # Named specifically rather than folded into "some settings": this
            # one changes how somebody gets in, and it is the change an operator
            # most needs to be told actually happened — the save it followed is
            # the last one they could make through the old door.
            method_changed = _section(saved.settings, "auth").get("method") != _section(
                current.settings, "auth"
            ).get("method")
            # The opposite case, and no longer a restart item: the listener
            # starts, stops or moves on save (ADR-0010). What it needs instead
            # is for the fragment to be rendered *after* it has moved, or the
            # state word beside the switch reports the port it had a moment
            # ago.
            wyoming_changed = _section(saved.settings, "wyoming") != _section(
                current.settings, "wyoming"
            )
            if wyoming_changed:
                await wyoming_settled(
                    request.app,
                    enabled=bool(_section(saved.settings, "wyoming").get("enabled")),
                )
            applied = [
                "The event bus",
                "the retention windows",
                "the memory settings",
                "the workspace settings",
            ]
            if wyoming_changed:
                applied.append("the Wyoming server")
            if method_changed:
                # Only reachable because the door proved it would still admit
                # this caller — `LiveAuth.refusal_for` runs before the write, so
                # a swap that reached here is one that has already happened.
                applied.append("the way in")
            result = {
                "kind": "saved",
                "message": (
                    f"Saved to {saved.source}. {_and_list(applied)} "
                    "apply now; the listen address applies when the core restarts."
                ),
            }
        return _core_form(request, saved, save_result=result)

    @router.post("/core/raw", response_class=HTMLResponse, summary="Save core settings as JSON")
    async def core_save_raw(request: Request) -> HTMLResponse:
        """The fallback editor, for anything the form cannot express.

        Takes the whole document, exactly as ``PUT /admin/api/config`` does, so
        a setting with no field of its own is still reachable without a shell
        on the container.
        """
        user = require_admin(require_user(request))
        form = await request.form()
        raw = str(form.get("raw") or "")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return await _core_page(
                request,
                tab="raw",
                raw_error=(
                    f"That is not valid JSON — {exc.msg}, line {exc.lineno}. Nothing was written."
                ),
            )
        if not isinstance(payload, dict):
            return await _core_page(
                request,
                tab="raw",
                raw_error="The settings document must be a JSON object. Nothing was written.",
            )
        try:
            # The request, not just the payload: `[auth] method` is on this
            # form, and the API's save path asks the door named there whether
            # it would still admit the caller who sent this. Without it the
            # swap is refused rather than taken on trust.
            saved = await save_config(
                payload, user, action="config.update", request=request
            )
        except HTTPException as exc:
            _, message = settings_problems(exc)
            return await _core_page(request, tab="raw", raw_error=message)
        # The form tab is rendered by this page too, so it needs the same wait
        # the form's own save takes: a document that moved the listener would
        # otherwise draw the switch as it was a moment ago.
        await wyoming_settled(
            request.app, enabled=bool(_section(saved.settings, "wyoming").get("enabled"))
        )
        return await _core_page(
            request, tab="raw", raw_saved=f"Saved to {saved.source}. Applied without a restart."
        )
