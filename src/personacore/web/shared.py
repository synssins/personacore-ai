"""What more than one admin-UI screen needs (ADR-0020).

The screen modules under ``ui/screens/`` each own one surface and are meant to
be edited without reading each other. This module is the deliberate exception:
the constants and helpers that genuinely serve several screens live here once,
so there is one answer to "which handler answers this operation", one reading
of the settings document, and one set of words for a refusal - rather than a
copy per screen drifting out of step.

:class:`UIContext` is the other half of that. The factory in
:mod:`personacore.web.routes` builds every dependency once and hands the
bundle to each screen's ``register``. Nothing here decides who a caller is:
``require_user`` is carried as data and attached to the router by the factory,
never re-derived.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from starlette.routing import Match

from personacore.admin.authn import AuthContext
from personacore.admin.config_io import ConfigRejected, read_config
from personacore.admin.models import (
    AdminUser,
    ConfigResponse,
    HealthState,
    PluginListing,
    SystemHealth,
)
from personacore.admin.protocols import (
    AuditGateway,
    ChatRunner,
    EventBusSource,
    LLMHealthSource,
    PluginHealthSource,
)
from personacore.agent.personas import (
    PersonaStore,
)
from personacore.config.appdata import AppdataLayout
from personacore.plugins.packages import (
    PackageLimits,
)
from personacore.preferences import PreferenceStore

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see the factory
    from personacore.admin.routes import _PluginScanCache


@dataclass(frozen=True, slots=True)
class UIContext:
    """Everything a screen module is given, built once by the factory.

    Every field is passed in rather than rebuilt, and the reasons
    :func:`~personacore.web.routes.create_admin_ui_router` gives for each
    of them apply just as much now that the screens are separate files: a
    second scan cache, a second persona store or a second ``require_user`` is a
    second answer to a question the core has already answered.
    """

    templates: Jinja2Templates
    require_user: Callable[[Request], AdminUser]
    layout: AppdataLayout
    audit: AuditGateway
    llm: LLMHealthSource
    bus: EventBusSource
    scans: _PluginScanCache
    disk_warning_bytes: int
    save_config: Callable[..., Awaitable[ConfigResponse]]
    personas: PersonaStore
    #: Per-person settings (ADR-0030). Keyed by ``AdminUser.id``, so it works
    #: on all three identity doors and not only where accounts exist.
    preferences: PreferenceStore
    chat: ChatRunner | None
    plugin_health: PluginHealthSource | None
    auth_context: AuthContext | None
    package_limits: PackageLimits
    #: One health sweep the dashboard and the sidebar dot both read.
    health: Callable[[], Awaitable[tuple[SystemHealth, PluginListing]]]
    #: The context ``base.html`` needs on every page. Built by the factory
    #: because it reads that module's build constants at call time.
    shell: Callable[[Request, str], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Vocabulary translation — the API's states into the design's
# ---------------------------------------------------------------------------

#: The design's markup indexes a four-key dictionary with a component's state,
#: so anything outside these keys raises in the template rather than rendering.
#: ``unknown`` becomes ``degraded`` because "I could not tell" is a warning, not
#: an all-clear and not a failure (see :class:`HealthState`'s own docstring on
#: why the third value exists at all).
_STATE_WORD: dict[HealthState, str] = {
    HealthState.OK: "ok",
    HealthState.FAILING: "down",
    HealthState.UNKNOWN: "degraded",
}

PERSONA_UNRECORDED = "The assistant"
"""Who the design names as the speaker of the reply.

The persona a turn ran under is **not** written to the transcript store, so
this is a truthful generic rather than a guess at a name. See the module notes
in the slice report: recording it is a change to
:class:`personacore.audit.models.TranscriptRecord`, not to this file.
"""


# ---------------------------------------------------------------------------
# Collapsing the furniture — the sidebar, the conversation rail, its groups
# ---------------------------------------------------------------------------
#
# Three switches with one home. They are per-person settings, so they live in
# the ADR-0030 preference store beside autoplay rather than in a second
# mechanism: keyed by the door somebody came through and their `AdminUser.id`,
# they work on all three identity doors and they survive the browser.
#
# **Not `localStorage`, and the reason is the flash.** This surface is
# server-rendered with `hx-boost` (ADR-0020): a collapsed sidebar restored by a
# script runs *after* the browser has painted the expanded one, so every
# navigation would open the menu and snap it shut again. The state has to be in
# the markup the server sends, which means the server has to know it, which
# means it cannot live only in the browser. That it also follows an operator to
# a second machine is a bonus rather than the argument.
#
# **Not audited.** Spec §7 wants every admin change recorded; a sidebar width
# changes nothing about the household, and a record per click would bury the
# changes that matter under the ones that do not.

MENU_COLLAPSED_PREFERENCE = "ui.menu_collapsed"
"""Whether the left-hand menu is a bar of icons rather than icons and names."""

RAIL_COLLAPSED_PREFERENCE = "ui.chat_rail_collapsed"
"""Whether Chat's list of earlier conversations is a bare vertical bar."""

_GROUP_COLLAPSED_NAMED = "ui.chat_group_collapsed.name."
_GROUP_COLLAPSED_NONE = "ui.chat_group_collapsed.none"


def group_collapsed_preference(group: str | None) -> str:
    """The preference name for one conversation group's heading.

    ``None`` — the ungrouped bucket — gets a key of its own rather than a
    sentinel string appended to the named prefix. A group is free text somebody
    typed (``Conversation.group_name``), so any sentinel could itself be typed;
    two prefixes cannot collide however the group is spelled.

    The name is bounded because the group is: ``ConversationService.regroup``
    trims to ``MAX_TITLE_LENGTH`` before storing, so this cannot be used to
    write unbounded keys into the table.
    """
    return _GROUP_COLLAPSED_NONE if group is None else f"{_GROUP_COLLAPSED_NAMED}{group}"


#: Form values that mean "yes". The same set the profile screen reads, so a
#: checkbox and a hidden field are understood the same way everywhere.
_TRUTHY = frozenset({"on", "true", "1", "yes"})


def wants_collapsed(raw: object) -> bool:
    """What a submitted ``collapsed`` field is asking for.

    Anything that is not one of :data:`_TRUTHY` is "no". A missing field is
    "no" as well — an unchecked checkbox submits nothing, and the safe reading
    of silence is the expanded state, which is the one that shows everything.
    """
    return isinstance(raw, str) and raw.strip().lower() in _TRUTHY


ADMIN_HOME = "/admin/chat"
"""Where a toggle lands when it cannot tell where it came from. Chat is the
first thing in the menu and the front door of the product."""


def safe_admin_next(request: Request, raw: object, *, fallback: str | None = None) -> str:
    """Where to send the browser after a toggle, refusing anywhere else.

    A toggle is a POST that has to put the operator back on the page they were
    reading, so the page names itself in a hidden field. **That field is not
    trusted.** A redirect target taken from a form is an open redirect unless
    something says otherwise, and "somewhere on this admin surface" is the only
    honest allowlist: an absolute path under ``/admin/``, with no scheme and no
    host of its own, and never ``//elsewhere`` — which a browser reads as a
    different site.

    A page rendered in answer to a POST cannot name itself (its own URL only
    answers POST, so redirecting there is a 405), so it sends nothing and the
    ``Referer`` — the address bar, not the request — is tried next.

    **`fallback` is tried after `Referer` and before :data:`ADMIN_HOME`.** It
    exists for :func:`method_not_allowed`, where there is no hidden field and
    often no ``Referer`` either — a plain refresh, a bookmark, a typed URL. The
    caller computes it however it likes (a walk up the path, say); this
    function still decides whether it is usable, the same as any other
    candidate. Failing everything, :data:`ADMIN_HOME`.

    **And "on this surface" is not enough: the browser has to be able to land
    there.** The sidebar's collapse toggle produced
    ``{"detail":"Method Not Allowed"}`` at ``/admin/personas/default`` — a real
    path on this origin, under ``/admin/``, which answers POST and nothing
    else. It had arrived as the ``Referer`` because the persona picker had just
    posted to it. A redirect is a GET, so a target with no GET is refused here
    and every caller is protected by one rule rather than each remembering.
    """
    for candidate in (raw, request.headers.get("referer"), fallback):
        if not isinstance(candidate, str) or not candidate:
            continue
        parsed = urlsplit(candidate)
        if parsed.scheme or parsed.netloc:
            # A full URL is only usable if it is this very origin, which is
            # what a browser's own Referer looks like. Anything else is
            # somebody else's site wearing our form.
            if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
                continue
        path = parsed.path
        if not path.startswith("/admin/") or path.startswith("//"):
            continue
        if not answers_get(request, path):
            continue
        return f"{path}?{parsed.query}" if parsed.query else path
    return ADMIN_HOME


def answers_get(request: Request, path: str) -> bool:
    """Whether this application serves a GET at ``path``.

    Starlette's own matcher is asked rather than the path being compared to
    anything by hand: the routes are nested behind included routers and carry
    path parameters, so ``/admin/personas/default`` matches
    ``/admin/personas/{name}`` — which exists, and answers POST. ``matches``
    reports that as *partial*: the path is served, the method is not. Partial
    is precisely the 405 this refuses, and only ``Match.FULL`` is a page a
    browser can land on.

    Inspecting the scope does not run the endpoint, so this costs a walk of the
    route table and nothing else. A router that cannot be asked at all answers
    ``False``, which sends the operator to :data:`ADMIN_HOME` — the wrong page,
    never a broken one.
    """
    try:
        routes = request.app.routes
    except Exception:  # noqa: BLE001 - no router to ask is not a reason to 500
        return False
    return serves_get(routes, path)


def serves_get(routes: Any, path: str) -> bool:
    """The matching half of :func:`answers_get`, asked of a route list.

    Split out so the middleware below can ask the same question from raw ASGI,
    where there is no ``Request`` to hand. **One rule, asked twice** — a second
    reading of "can a browser land here" is exactly the kind of near-duplicate
    that drifts, and this one decides whether a screen strands somebody.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "root_path": "",
    }
    for route in routes or ():
        try:
            if route.matches(scope)[0] is Match.FULL:
                return True
        except Exception:  # noqa: BLE001, S112 - one odd route must not decide this
            continue
    return False


#: The JSON contract, which is not a screen. A caller here is tooling reading
#: the OpenAPI document (ADR-0020), and 405 as JSON is the correct answer to it.
_JSON_API_PREFIX = "/admin/api/"


def _nearest_get_ancestor(request: Request, path: str) -> str | None:
    """Walk ``path`` upward to the nearest ancestor that answers GET.

    ``/admin/chat/persona`` belongs to ``/admin/chat``; ``/admin/voice/engines``
    belongs to ``/admin/voice``. Neither route knows that — nothing is asked to
    remember it — so this reads it off the one thing that is always true: a
    screen's controls live under its own path. Deepest ancestor first, because
    a three-deep POST-only path (a plugin's install action, say) belongs to the
    detail page directly above it, not the plugin list above that.

    **Exact literal paths only — this does not ask Starlette to match.**
    ``/admin/plugins/install/review`` is POST-only, and so is
    ``/admin/plugins/install`` directly above it, but ``/admin/plugins/{name}``
    (the per-plugin page) answers GET and happily matches ``install`` as if it
    were a plugin's name. :func:`answers_get` asking Starlette "does anything
    answer GET here" said yes and this walk landed a refresh on a 404 for a
    plugin that does not exist. A path segment is not a plugin merely for
    appearing where one could go, so the candidate is checked against the
    literal path templates FastAPI actually registered — no ``{`` in
    them — rather than against what a wildcard route would accept. That skips
    straight past ``/admin/plugins/install`` to ``/admin/plugins``, which is a
    real screen and cannot be confused for one.

    Stops one level short of ``/admin`` itself: ``/admin`` is the front door,
    not a screen, and :func:`safe_admin_next` refuses a bare ``/admin`` anyway
    (it requires the ``/admin/`` prefix). No ancestor answering GET is reported
    as ``None`` rather than guessed at, so the caller falls back to
    :data:`ADMIN_HOME` the same way it always has.
    """
    try:
        screens = {
            route.path
            for route in _api_routes(request.app)
            if "GET" in (getattr(route, "methods", None) or ())
        }
    except Exception:  # noqa: BLE001 - no router to ask is not a reason to 500
        return None
    parts = path.strip("/").split("/")
    for depth in range(len(parts) - 1, 1, -1):
        candidate = "/" + "/".join(parts[:depth])
        if candidate in screens:
            return candidate
    return None


async def method_not_allowed(request: Request, exc: Exception) -> Response:
    """What a person gets for asking a form's target for a page.

    An address bar reading ``/admin/chat/persona``, refreshed, answers with
    that path's only method: POST. Starlette answered:

        {"detail":"Method Not Allowed"}

    — the whole application replaced by a string, with nothing on the page to
    press and no way back except retyping a URL the user does not know. The
    push that put the address there is fixed above; this is the other half,
    and it is the half actually seen. It covers every POST-only path on the
    surface at once, including the ones nobody has thought of, and it needs
    no route to remember anything.

    The user is sent to the page they came from — the surface's own rule for
    that (:func:`safe_admin_next`), which refuses anywhere off this surface
    and anywhere that answers no GET. ``Referer`` is tried first when it is
    there: it can carry more than a path ever could (the chat screen's ``?c=``
    names which conversation was open), and :func:`safe_admin_next` already
    vets it — on this surface, answers GET — so a hostile or stale one is
    refused rather than trusted. A plain refresh often sends no ``Referer`` at
    all, which is the gap this closes: failing that, :func:`_nearest_get_ancestor`
    walks the path itself up to the screen it belongs to, and only past that
    does this land on :data:`ADMIN_HOME`. A redirect rather than an error page
    invents no new copy: there is nothing to tell the user, because nothing
    went wrong.

    **Only for a browser asking for a page.** The JSON API is a contract with
    tooling and 405 is the honest answer to it, so it keeps the one it has.
    """
    path = request.url.path
    wants_a_page = "text/html" in request.headers.get("accept", "").lower()
    if wants_a_page and path.startswith("/admin/") and not path.startswith(_JSON_API_PREFIX):
        destination = safe_admin_next(request, None, fallback=_nearest_get_ancestor(request, path))
        return RedirectResponse(destination, status_code=303)
    detail = getattr(exc, "detail", "Method Not Allowed")
    headers = getattr(exc, "headers", None)
    return JSONResponse({"detail": detail}, status_code=405, headers=headers)


# ---------------------------------------------------------------------------
# The address bar, after a form post
# ---------------------------------------------------------------------------

#: Response headers that already say where the browser should be. htmx reads
#: each of these before it looks at any attribute, so a route that sets one has
#: made the decision itself and must not be second-guessed.
_ALREADY_DECIDED = (b"hx-push-url", b"hx-replace-url", b"hx-redirect", b"hx-location")

#: The one it is given when it has not. `"false"` is htmx's own word for "leave
#: the address bar alone", and it is honoured whatever the element is.
_NO_PUSH = (b"hx-push-url", b"false")


class KeepsTheAddressBar:
    """A POST that renders a page must not leave its own URL in the address bar.

    Changing persona on the chat screen, then pressing F5, produced:

        {"detail":"Method Not Allowed"}

    ``/admin/chat/persona`` answers POST and nothing else, so a refresh asked it
    for a GET and Starlette answered 405 — as JSON, replacing the whole
    application with an error string. It recurred twice: once on v0.11.x, and
    again on v0.13.1 **with the guard already in the markup.**

    The guard was ``hx-push-url="false"`` on the form, and it does nothing here.
    Every plain form on this surface is boosted (``hx-boost="true"`` on
    ``<body>``, ADR-0020), and htmx's own history code reads:

        let push = etc.push || getClosestAttributeValue(elt, "hx-push-url")
        if (push === "false") push = null
        ...  else if (boosted) { push(responsePath) }

    The attribute set to ``"false"`` becomes "nothing was asked for", and the
    next branch pushes the POST's own path anyway *because the element is
    boosted*. The one place the attribute was ever needed is the one place it is
    inert. The response header is read first and is obeyed unconditionally.

    So the rule lives here rather than in fourteen templates. **A screen cannot
    forget it**, which matters more than it sounds: the attribute was present
    and correct-looking on the exact form that broke, and a test read it and
    passed. Nobody was careless. The mechanism was.

    The rule, in full: a POST whose answer is an HTML page at a path that
    answers no GET is told not to push. Everything else is untouched —

    * a POST whose path also answers GET is reloadable already;
    * a redirect is a decision about where to land, and moving the address bar
      there is the whole point of it;
    * a route that sets its own ``HX-`` navigation header has decided;
    * anything that is not HTML is not a page a browser can be left sitting on,
      which is what keeps the JSON API and ``/v1`` out of this entirely — and
      why the cheap checks are made before the route table is walked.

    Written as plain ASGI rather than ``@app.middleware("http")``: that one is
    ``BaseHTTPMiddleware``, which stands between the endpoint and the client for
    the whole body, and the chat screen streams replies through here. This only
    ever edits the headers of ``http.response.start`` and never touches a body.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        async def guarded(message: Any) -> None:
            if message.get("type") == "http.response.start" and self._should_hold(
                scope, message
            ):
                message = {**message, "headers": [*message["headers"], _NO_PUSH]}
            await send(message)

        await self.app(scope, receive, guarded)

    def _should_hold(self, scope: Any, message: Any) -> bool:
        """Whether this answer would otherwise strand the browser."""
        if not 200 <= int(message.get("status", 0)) < 300:
            return False
        headers = message.get("headers") or []
        html = False
        for name, value in headers:
            lowered = name.lower()
            if lowered in _ALREADY_DECIDED:
                return False
            if lowered == b"content-type" and value.lower().startswith(b"text/html"):
                html = True
        if not html:
            return False
        app = scope.get("app")
        routes = getattr(app, "routes", None)
        if routes is None:
            return False
        return not serves_get(routes, scope.get("path", ""))


SHORT_COMMIT_LENGTH = 7
"""How much of a commit hash the footer shows. Seven characters is what git
itself abbreviates to and what a person can read back over a phone."""


def build_label(commit: str, date: str) -> str:
    """The build identity for the sidebar footer, or ``""`` when there is none.

    ``BUILD_COMMIT`` and ``BUILD_DATE`` are baked in when the image is built and
    are **empty everywhere else**. Empty must render as nothing at all: a UI
    showing a commit that is not the running commit is worse than one showing
    none, because the wrong answer is the one that gets acted on. So there is no
    placeholder, no "unknown", and no falling back to anything derived locally.

    The date alone is not an identity — two builds of the same day are not the
    same build — so it appears only alongside a commit.
    """
    short = commit.strip()[:SHORT_COMMIT_LENGTH]
    if not short:
        return ""
    stamped = date.strip()
    return f"{short} · {stamped}" if stamped else short


def _human_gap(delta: timedelta) -> str:
    """A duration as an operator would say it."""
    seconds = max(delta.total_seconds(), 0.0)
    if seconds < 1:
        return "under a second"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} m {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} m"


def _readable(exc: BaseException) -> str:
    """One line describing a failure, with no traceback in it.

    Spec §9: a failure is explained in plain English. Some exceptions
    (``TimeoutError`` most of all) carry an empty message, so the class name is
    the fallback — "the turn did not finish" with nothing after it is worse than
    a type name.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# ---------------------------------------------------------------------------
# Core settings — the form, and what has no setting behind it
# ---------------------------------------------------------------------------
#
# The design drew this screen against a settings model that does not exist:
# there is no instance name and no log level in :class:`CoreSettings`, and
# neither security toggle is a setting at all — the admin bypass is an
# environment variable read at startup, and "event bus authentication" is the
# presence of a user name and a password secret rather than a switch. Those
# four controls are rendered, disabled and marked ``later``, the same treatment
# the plugin screen gives its unbuilt actions.
#
# What *is* wired is everything the settings model really holds and this screen
# owns: the listen address (``[server]``), the event bus (``[bus]``), the
# retention windows (``[retention]``) and which way in is open (``[auth]``).
# The LLM roles belong to the Models screen, the default persona to Personas,
# and anything else — per-role timeouts, breaker thresholds — stays reachable
# through the raw tab.
#
# Sign-in is the one control in the Security section that is a real setting,
# and the reason the other two are not is worth keeping straight. Which door is
# open is a considered choice with two explanations beside it, so ADR-0010 puts
# it here. The bypass is break-glass and stays in the environment on purpose:
# a switch that could turn it off from inside this interface would be useless
# at the only moment it is wanted.

RETENTION_DEFAULT_KEY = "default"
"""The form field standing for ``retention.default_days``.

Not a surface name, and it must never collide with one: the POST handler routes
every ``retention_<name>`` field to ``per_surface_days[<name>]`` except this
one, which is exactly how a made-up surface reaches the API's own validator
instead of being quietly dropped here.
"""


def _section(settings: Mapping[str, Any], name: str) -> dict[str, Any]:
    """One sub-table of the settings document, or an empty one.

    So that a screen writing a single key into a section never drops the keys
    beside it that it has no field for.
    """
    value = settings.get(name)
    return dict(value) if isinstance(value, dict) else {}


def _text(section: Any, key: str) -> str:
    """One value out of a settings sub-table, as a string for an input box."""
    if not isinstance(section, dict):
        return ""
    value = section.get(key)
    return "" if value is None else str(value)


def settings_problems(exc: HTTPException) -> tuple[dict[str, str], str]:
    """The admin API's refusal, split into per-field errors and one sentence.

    **The API is the only validator.** Spec §9's plain-English messages are
    already written there — "there is no surface called 'x'. The surfaces are:
    …" comes from :class:`RetentionSettings` itself — so this reads them out of
    the error body and puts each one where the design has a slot for it. A
    problem with no field of its own is appended to the save note rather than
    dropped, because a refusal an operator cannot see is a save that looks like
    it worked.
    """
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    message = str(detail.get("error") or "Those settings were not saved.")
    problems = detail.get("problems")
    field_errors: dict[str, str] = {}
    rest: list[str] = []
    for problem in problems if isinstance(problems, list) else []:
        if not isinstance(problem, dict):
            continue
        key = str(problem.get("key") or "")
        text = " ".join(
            str(part) for part in (problem.get("problem"), problem.get("hint")) if part
        )
        if key.startswith("retention.per_surface_days."):
            field_errors[key.rsplit(".", 1)[-1]] = text
        elif key == "retention.default_days":
            field_errors[RETENTION_DEFAULT_KEY] = text
        else:
            rest.append(text)
    if rest:
        message = f"{message} {' '.join(rest)}"
    return field_errors, message


# ---------------------------------------------------------------------------
# Plugin operations — the JSON API's own handlers, reached rather than rebuilt
# ---------------------------------------------------------------------------

PLUGIN_API_HANDLERS: dict[str, str] = {
    "install": "install_plugin",
    "enable": "enable_plugin",
    "disable": "disable_plugin",
    "uninstall": "uninstall_plugin",
    "read_config": "get_plugin_config",
    "write_config": "put_plugin_config",
    "lookup": "plugin_config_lookup",
}
"""What the plugin screens do, and the ``/admin/api`` handler that does it.

Switching a plugin on, uninstalling it and writing its ``config.toml`` are not
small operations: each writes appdata, stops or starts a live subprocess in a
particular order, and records an audit line. **This surface performs none of
them itself.** It looks the JSON API's own handler up on the running
application and calls it, so a save clicked here and a ``PUT
/admin/api/plugins/{name}/config`` are the same code — atomic write, plugin
restart, audit record, same refusals in the same words.

That is the same rule ``save_config`` and ``require_user`` already follow on
this router; the only difference is how the callable is obtained. These
handlers are closures inside
:func:`personacore.admin.routes.create_admin_router`, and this router is built
by that function without being handed them. **The clean fix is six keyword
arguments at that call site**, and this lookup exists so the screen works
before they are added rather than being wired to nothing — see the slice
report. Nothing here bypasses a check: authentication is this router's own
``require_user`` dependency, and each handler re-derives the operator from the
request exactly as it does for a JSON caller.
"""


API_HANDLERS: dict[str, str] = {
    **PLUGIN_API_HANDLERS,
    # Personas. Only ``select`` is here: it writes ``default_persona`` into
    # core.toml, applies it to the live objects and audits the change, and doing
    # any of that a second way would be a second answer to "which persona is the
    # default". Listing is a *builder* rather than a handler
    # (``build_persona_listing``), reached directly for the same reason the
    # health screen reaches ``build_system_health`` directly.
    "select_persona": "select_persona",
    # Access keys. All three, because every one of them touches the credential
    # store or the audit log: issuing mints and hashes, revoking deletes, and
    # both write the audit record spec §7 requires. Nothing on this surface
    # hashes, compares or stores key material — see ``ApiKeyGateway``.
    "list_keys": "list_api_keys",
    "issue_key": "issue_api_key",
    "revoke_key": "revoke_api_key",
}
"""Every ``/admin/api`` handler an HTML screen drives, by the name this surface
calls it.

The plugin operations came first and :data:`PLUGIN_API_HANDLERS` keeps their
docstring; personas and keys join them under the same rule, which is the whole
point of the mechanism: **a control on this surface performs nothing itself.**
It finds the JSON API's own handler on the running application and calls it, so
a key issued from this screen and a ``POST /admin/api/keys`` are the same code —
same store, same refusals, same audit record, same words.
"""


MAX_ROUTER_DEPTH = 8
"""How far :func:`api_handler` walks into included routers.

FastAPI keeps an included router as one entry rather than flattening its
routes, so finding a handler means descending. Bounded because a walk with no
bound is a walk that hangs the day something holds a reference to itself, and
this one runs on every page render.
"""


def _api_routes(node: Any, depth: int = 0) -> Iterator[APIRoute]:
    """Every :class:`APIRoute` under one application or router.

    Depth-first through ``routes`` and through an included router's
    ``original_router``, because ``app.routes`` holds included routers as single
    entries in this FastAPI version and the endpoints are underneath them.
    """
    if depth > MAX_ROUTER_DEPTH:  # pragma: no cover - a cycle, not a real router tree
        return
    for route in getattr(node, "routes", ()):
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        if inner is not None:
            yield from _api_routes(inner, depth + 1)


def api_handler(app: Any, operation: str) -> Callable[..., Awaitable[Any]] | None:
    """The JSON API's handler for one operation in :data:`API_HANDLERS`, or ``None``.

    Matched on the route's *name* and its ``/admin/api/`` path, never on a path
    built from anything a request said. ``None`` means this application was
    assembled without the JSON API — in which case the control is rendered
    disabled and marked ``later`` rather than offered and then failing.
    """
    wanted = API_HANDLERS.get(operation)
    if wanted is None:  # pragma: no cover - a typo in this module, not in a request
        return None
    for route in _api_routes(app):
        if route.name == wanted and route.path.startswith("/admin/api/"):
            return route.endpoint
    return None


NO_PLUGIN_OPERATIONS = (
    "This core was assembled without its plugin API, so nothing here can install "
    "a plugin, switch one on or off, uninstall one, or write its settings."
)
"""Said when :func:`api_handler` finds nothing. Every control that needs one is
disabled and marked ``later`` — hiding them would hide the shape of the screen,
and offering them would offer something that cannot work."""

NO_PERSONA_OPERATIONS = (
    "This core was assembled without its persona API, so the default persona "
    "cannot be changed from here. The personas themselves still list and edit."
)

NO_KEY_OPERATIONS = (
    "API key management is not switched on in this core, so no key can be "
    "listed, issued or revoked from here."
)
"""Two states behind one sentence, deliberately.

A core assembled without the JSON key API and a core whose key *store* is absent
are the same thing to an operator looking at this screen: nothing here can issue
a key. The JSON API's own ``KEYS_UNAVAILABLE`` says the second half in its own
words and reaches the screen through :func:`refusal` when it is the one that
answered."""


def refusal(exc: HTTPException) -> str:
    """The API's own plain-English reason, out of its error body.

    Never a traceback and never a bare status code: the handler already
    wrote a sentence for an operator (spec section 9) and this screen shows
    it rather than rewording it.
    """
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("error"):
        return str(detail["error"])
    return str(detail)


def current_config(layout: AppdataLayout) -> tuple[ConfigResponse | None, str | None]:
    """The settings document, or why it could not be read.

    A refusal is a **whole-screen** state on both settings screens, not a
    note beside one field: with the file unreadable there is nothing to
    merge a change into, and writing one anyway would replace a document
    nobody has been shown.
    """
    try:
        return read_config(layout), None
    except ConfigRejected as exc:
        return None, exc.message



def _dev_admin_user(request: Request) -> str | None:
    """The development authentication bypass, if this application has one on.

    Read from the assembled application's own state — the same value
    ``/health`` reports (``personacore.server``) — rather than from the
    environment, so what the dashboard announces is what the running process
    actually did, not what a variable says it should have done. Absent on an
    application that never set it, which is why this is a ``getattr``.
    """
    return getattr(request.app.state, "dev_admin_user", None) or None
