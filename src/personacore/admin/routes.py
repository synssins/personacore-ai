"""The admin HTTP API — spec section 9, ADR-0007.

ADR-0007 deferred the admin *UI* until Claude Design mockups were approved, and
built the API underneath it properly first: "it is a contract, it is versioned
and documented like one, and it is what actually survives." Everything on this
surface is written to that standard. The designed UI
(``personacore.web``) is a consumer of this API like any other, and the
throwaway test surface it replaced was deleted without changing a line of it —
which is the whole of what API-first bought.

**Shape.** :func:`create_admin_router` is a factory returning an
``APIRouter``. It never constructs a ``FastAPI`` app, never runs a server, and
has no module-level state — whoever assembles the app mounts this router
alongside the OpenAI-compatible router (spec section 5.4) and owns the
lifecycle of every collaborator passed in. Collaborators are Protocols (see
``protocols.py``), so this module imports no broker, database or HTTP client.

**Errors.** Every failure leaves as an HTTP error whose ``detail`` is an
:class:`personacore.admin.models.ApiError` — one shape for the UI to render,
always plain English (spec section 9), and ``problems`` when several keys are
wrong at once so an operator fixes them in one pass rather than three.

**Blocking work.** Plugin scanning, persona loading and config reads all touch
the filesystem. Each is handed to a worker thread, because the same event loop
serves voice turns whose latency budget is ~2 seconds (spec section 10).

**This module is now the wiring rather than the implementation** (ADR-0040).
It builds the collaborators once, builds *one* guarded router, and asks each
concern to register its own routes on it. Each piece below is a file somebody
can open and read whole, and a change to one of them is a change nobody has to
read the other nine to make.

* :mod:`personacore.admin.api_shared` — the one error shape, the one audit
  write, and :class:`~personacore.admin.api_shared.AdminApiContext`, which is
  the joint every register function builds against.
* :mod:`personacore.admin.api_plugin_listing` — what is installed and how each
  one is getting on, plus the scan that reload controls.
* :mod:`personacore.admin.api_health` — the dashboard's probes and its route.
* :mod:`personacore.admin.api_plugins` — installing, switching on, removing.
* :mod:`personacore.admin.api_plugin_config` — a plugin's own ``config.toml``
  and ADR-0016's search-and-fill.
* :mod:`personacore.admin.api_personas` — the picker and the default.
* :mod:`personacore.admin.api_trace` — the merged timeline.
* :mod:`personacore.admin.api_config` — core settings, and the one function
  that writes them.
* :mod:`personacore.admin.api_keys` — issue, list, revoke.
* :mod:`personacore.admin.api_accounts` — who this request is, and the core's
  own sign-in.

**The split moved code and changed no route.** ADR-0032 is why that matters
more here than in the other three splits: authorisation on this surface is a
router-level dependency, so a handler registered on the wrong router is
reachable by anyone, and this surface exposes one household member's private
conversations to another. **What guards it is now an access key** carrying
``ADMIN_API_SCOPE`` (:class:`personacore.admin.authn.AdminApiKeyDoor`) rather
than the sign-in: being signed in was enough, and that was the hole — a member
could mint themselves a key to ``/v1`` and read the trace. There is exactly one
guarded router and exactly one unguarded one, both built below, and
``tests/server/test_admin_api_admin_only.py`` drives every route on the surface
as a signed-out caller to prove it.

Everything importable from here before the split is still importable from
here, because callers should not have to know which of those files a name
landed in.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.templating import Jinja2Templates

# Aliased for one reason worth saying out loud: `api_keys` is also the name of
# a factory argument below, and a module bound to that name would shadow the key
# store. The rest follow the same shape so the register calls read alike.
from personacore.admin import api_accounts as accounts_api
from personacore.admin import api_config as config_api
from personacore.admin import api_health as health_api
from personacore.admin import api_keys as keys_api
from personacore.admin import api_personas as personas_api
from personacore.admin import api_plugin_config as plugin_config_api
from personacore.admin import api_plugins as plugins_api
from personacore.admin import api_trace as trace_api
from personacore.admin.api_config import (
    config_reader,  # noqa: F401 - kept importable from this module; see the note below
    config_saver,  # noqa: F401 - kept importable from this module; see the note below
)
from personacore.admin.api_health import (
    DEFAULT_DISK_WARNING_BYTES,
    _audit_health,  # noqa: F401 - kept importable from this module; see the note below
    _bus_health,  # noqa: F401 - kept importable from this module; see the note below
    _disk_health,  # noqa: F401 - kept importable from this module; see the note below
    _human_bytes,  # noqa: F401 - kept importable from this module; see the note below
    _llm_components,  # noqa: F401 - kept importable from this module; see the note below
    _llm_role_health,  # noqa: F401 - kept importable from this module; see the note below
    _plugin_components,  # noqa: F401 - kept importable from this module; see the note below
    build_system_health,
)
from personacore.admin.api_keys import (
    ANONYMOUS_KEY_REFUSED,
    KEY_ID_PATTERN,
    KEYS_UNAVAILABLE,
    build_api_key_listing,
    build_api_key_view,
)
from personacore.admin.api_personas import build_persona_listing
from personacore.admin.api_plugin_config import (
    LOOKUP_ACTION,  # noqa: F401 - kept importable from this module; see the note below
)
from personacore.admin.api_plugin_listing import (
    DISABLED_PLUGIN_DETAIL,
    WAITING_PLUGIN_DETAIL,  # noqa: F401 - kept importable from this module; see the note below
    _PluginScanCache,
    _read_disabled,  # noqa: F401 - kept importable from this module; see the note below
    build_plugin_listing,
    missing_secrets_source,
    waiting_plugin_detail,  # noqa: F401 - kept importable from this module; see the note below
)
from personacore.admin.api_plugins import (
    MULTIPART_NOT_ACCEPTED,
    UPLOAD_TOO_LARGE,
    _install_failure,  # noqa: F401 - kept importable from this module; see the note below
    _installed_view,  # noqa: F401 - kept importable from this module; see the note below
    _read_upload,  # noqa: F401 - kept importable from this module; see the note below
    _upload_label,  # noqa: F401 - kept importable from this module; see the note below
)
from personacore.admin.api_shared import (
    AdminApiContext,
    _fail,  # noqa: F401 - kept importable from this module; see the note below
    _record_change,  # noqa: F401 - kept importable from this module; see the note below
)
from personacore.admin.api_trace import (
    MAX_TRACE_LIMIT,
    MAX_TRACE_WINDOW,  # noqa: F401 - kept importable from this module; see the note below
    _audit_entry,  # noqa: F401 - kept importable from this module; see the note below
    _normalise_moment,  # noqa: F401 - kept importable from this module; see the note below
    _transcript_entry,  # noqa: F401 - kept importable from this module; see the note below
    build_trace_page,
)
from personacore.admin.authn import (
    AdminApiKeyDoor,
    AuthContext,
    LiveAuth,
    make_admin_user_dependency,
    require_admin,  # noqa: F401 - kept importable from this module; see the note below
)
from personacore.admin.protocols import (
    ApiKeyGateway,
    AuditGateway,
    ChatRunner,
    EventBusSource,
    LLMHealthSource,
    PluginHealthSource,
    PluginToggle,
    PluginToolCaller,
    SecretNameSource,
    SettingsApplier,
)
from personacore.agent.personas import PersonaStore
from personacore.audit import get_logger
from personacore.auth.method import AuthDecision
from personacore.config.appdata import AppdataLayout
from personacore.plugins.discovery import PluginDiscovery
from personacore.plugins.packages import DEFAULT_PACKAGE_LIMITS, PackageLimits
from personacore.preferences import PreferenceStore
from personacore.runbooks import RunbookStore

# Every name above that this file no longer uses itself is re-exported on
# purpose. Moving code must not move a name: `ruff check --fix` would delete an
# import nothing here calls, and the callers that import it from this module —
# the designed UI's screens, and the tests — would break for a reason that has
# nothing to do with what they were asking for. The `noqa` is the whole of what
# keeps that from happening, so it is written per name rather than per file.

# Nothing in this file logs any more — every line that did went out with the
# code it belonged to, and each new module has its own logger under its own
# name. Kept for the same reason as the imports above: it was importable from
# here, so it stays importable from here.
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Authentication — spec section 7
# ---------------------------------------------------------------------------


# The dependency itself lives in :mod:`personacore.admin.authn`, and is
# re-exported here because this module's factory is where it is built and
# because every existing caller imports it from this name.
#
# **It is the one authorisation seam.** Read that module before adding any
# check anywhere on this surface: the core's own sign-in (PC-283) was added by
# widening that one dependency rather than by putting a second check beside it,
# and PC-294 is the requirement that says why.


def create_admin_router(
    *,
    layout: AppdataLayout,
    discovery: PluginDiscovery,
    personas: PersonaStore,
    preferences: PreferenceStore,
    audit: AuditGateway,
    llm: LLMHealthSource,
    bus: EventBusSource,
    trusted_user_header: str,
    auth: AuthDecision | None = None,
    auth_context: AuthContext | None = None,
    auth_live: LiveAuth | None = None,
    api_keys: ApiKeyGateway | None = None,
    plugin_health: PluginHealthSource | None = None,
    plugin_toggle: PluginToggle | None = None,
    call_plugin_tool: PluginToolCaller | None = None,
    apply_settings: SettingsApplier | None = None,
    chat: ChatRunner | None = None,
    secrets: SecretNameSource | None = None,
    disk_warning_bytes: int = DEFAULT_DISK_WARNING_BYTES,
    package_limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
    runbooks: RunbookStore | None = None,
) -> APIRouter:
    """Build the admin router — the JSON API and the designed admin UI on it.

    Mount it with ``app.include_router(create_admin_router(...))``; the paths
    (``/admin/api/...`` and ``/admin/``) are part of the contract and are not
    caller-configurable, so the designed UI and the API cannot drift apart.

    Args:
        layout: Appdata paths (Appendix B). The only place this module learns
            where anything lives.
        discovery: Plugin scanner (spec section 5.1).
        personas: Persona store (spec section 5.5).
        audit: Audit/transcript store — read for the trace view, written for
            every admin change (spec section 7).
        llm: Anything with ``health_check()``; in production the ``LLMClient``.
        bus: Anything exposing ``.health``; in production the ``EventBus``.
        trusted_user_header: Header the reverse proxy sets to the signed-in
            user. Deliberately has no default — see
            :func:`personacore.admin.authn.make_admin_user_dependency` for why
            this is only safe behind that proxy, and read it before choosing a
            value. Ignored entirely under the ``builtin`` door, where a header
            is not a credential.
        auth: **Which single way in is open** — the decision
            :func:`personacore.auth.method.resolve_auth` made once at startup
            (PC-294). ``None`` means the trusted-header door, which is what
            every assembly did before the core had accounts of its own, so an
            existing caller keeps exactly the behaviour it had.
        auth_context: The account and session stores, the failed-sign-in
            throttle and the audit store, bundled by
            :class:`personacore.admin.authn.AuthContext`. Required when ``auth``
            names the ``builtin`` door and unused otherwise; the sign-in page,
            the setup page and the JSON sign-in endpoints are only mounted when
            it is present, so a core assembled without one has no sign-in
            surface to probe rather than one that answers 503.
        api_keys: The exposed API's key store (spec section 5.4: keys are
            "issued and revoked in the admin UI"). ``None`` means this core was
            assembled without one, so the router still builds and the three
            ``/keys`` endpoints answer ``503`` with a plain-English reason
            rather than vanishing — a missing endpoint and a switched-off
            feature look identical from the outside, and only one of them is
            worth telling an operator about.
        plugin_health: The plugin supervisor's live view, when one exists.
            Without it plugins report ``unknown`` rather than a guess.
        plugin_toggle: Something that can start and stop one plugin in the
            *running* core — in production the ``PluginHost`` itself (ADR-0013).
            ``None`` means the enable/disable endpoints still record the choice
            in appdata, so it takes effect at the next start, and say so instead
            of claiming an effect they did not have. If it is absent, the
            supervisor's own ``set_enabled`` is used when it has one.
        call_plugin_tool: Calls one plugin tool by name — in production the
            ``PluginHost``'s own ``call_tool``, which is the point: ADR-0016
            requires a settings-page lookup to be "a tool call like any other",
            audited and subject to the plugin's declared permissions, and
            reusing that path is the only way it actually is one. ``None`` means
            this core cannot call a plugin, so a field that declares a search
            says the search is unavailable and stays editable by hand. Nothing a
            request says chooses the tool: the plugin comes from the path, the
            tool from that plugin's ``config.schema.json``, and it runs only if
            the plugin's manifest declares it ``safe``.
        apply_settings: Called with the new ``CoreSettings`` after a successful
            write, so the live objects built from config (default persona, LLM
            base URL, bus connection) can be updated by whoever owns them. This
            module never reaches into them itself.
        chat: Runs one turn through the agent loop, for the admin UI's chat
            screen only — no API endpoint takes it, because the turn API is
            the exposed OpenAI-compatible one (spec section 5.4) and a second
            way in would be a second thing to secure. ``None`` means the page
            says chat is unavailable and everything else on it still works.
        secrets: Lists the *names* of the secrets this core holds, for a plugin
            setting marked as naming one (ADR-0015: "the UI offers the names in
            the store. Values never leave it"). ``None`` means no store was
            wired in and the field says there is nothing to choose — this
            module never reads a secret's value and has no way to. Defaulted so
            an assembly that does not care still builds; the appdata layout on
            its own is not enough, because whoever owns the store owns whether
            the admin surface may list it at all. A store that can also delete a
            namespace (ADR-0025) has it emptied on uninstall, so a plugin
            reinstalled under the same name does not inherit the last one's
            credentials.
        disk_warning_bytes: Appdata free-space floor for the dashboard.
        package_limits: Ceilings on an uploaded plugin package — archive size,
            uncompressed total and entry count (ADR-0013's zip-bomb refusals).
        runbooks: The runbook file store (``working/contracts/runbook.md``
            §6). ``None`` means this assembly never built one, in which case
            installing a plugin that bundles ``runbooks/`` simply does not
            copy them in — there is nowhere to put them.
    """
    # PC-294: one call, one decision, one seam. Everything below — the JSON
    # API, the designed UI and its static assets — hangs off this one object.
    #
    # A *holder*, not the dependency itself (ADR-0010, and see
    # `personacore.admin.authn.LiveAuth`): every route closes over
    # `require_user` at build time, so a door that lived in that closure could
    # never be changed without a restart. Bound to the holder instead, the
    # routes ask it per request and saving `[auth] method` rebinds what it
    # delegates to.
    live_auth = auth_live or LiveAuth(
        trusted_user_header, decision=auth, context=auth_context
    )
    require_user = live_auth.identify
    # The JSON API's own door, and the only credential it takes (ADR-0041). It is
    # built beside `live_auth` rather than instead of it because the two guard
    # different surfaces: the browser's admin screens keep the sign-in above,
    # and `/admin/api` takes an access key carrying `ADMIN_API_SCOPE`. See
    # `AdminApiKeyDoor` for why being signed in is no longer enough there — in
    # short, it was how a household member could mint themselves a key to `/v1`
    # and read everybody else's conversations.
    admin_api_door = AdminApiKeyDoor(api_keys, live_auth=live_auth)
    # ADR-0025 section 4, through the one seam this surface has to the store,
    # built where the listing that consumes it lives.
    scans = _PluginScanCache(
        discovery,
        plugin_health,
        layout,
        missing_secrets_source(secrets),
    )
    # The bound method, not the object holding it: an assembly may pass an
    # explicit toggle, or a health source that happens to be able to act, and
    # the endpoints below want one callable either way.
    live_toggle = getattr(plugin_toggle, "set_enabled", None) or getattr(
        plugin_health, "set_enabled", None
    )

    # Built before the context because the context carries them: the settings
    # screens, `PUT /admin/api/config` and `POST /personas/{name}/select` all
    # save through this one function, or "saved" means three things.
    _read_config = config_api.config_reader(layout)
    _save_config = config_api.config_saver(
        layout=layout,
        audit=audit,
        live_auth=live_auth,
        apply_settings=apply_settings,
        _read_config=_read_config,
    )

    # The joint, written down before anyone builds to it. Every register call
    # below is handed this and nothing else, so no two of them can invent
    # different answers to the same question.
    ctx = AdminApiContext(
        layout=layout,
        personas=personas,
        audit=audit,
        llm=llm,
        bus=bus,
        # The key's identity when a key opened the door, the signed-in person
        # when the rendered UI called one of these handlers directly. One
        # callable so no handler has to know which of the two happened.
        require_user=admin_api_door.identify,
        live_auth=live_auth,
        auth_context=auth_context,
        api_keys=api_keys,
        plugin_health=plugin_health,
        plugin_toggle=plugin_toggle,
        live_toggle=live_toggle,
        call_plugin_tool=call_plugin_tool,
        secrets=secrets,
        scans=scans,
        disk_warning_bytes=disk_warning_bytes,
        package_limits=package_limits,
        read_config=_read_config,
        save_config=_save_config,
        runbooks=runbooks,
    )

    api = APIRouter(
        prefix="/admin/api",
        tags=["admin"],
        dependencies=[Depends(admin_api_door.require)],
    )

    # **One router, one dependency, every route.** ADR-0032 makes this surface
    # default-deny, and it is default-deny because the guard is on the router
    # rather than in the handlers: a route registered here cannot forget it.
    #
    # The dependency is the access-key door and no longer the sign-in. A
    # signed-in household member reached every route here — including the trace,
    # which is everybody's conversations, and the endpoint that issues keys to
    # `/v1`. The designed UI hid those screens from a member (ADR-0032) and this
    # was the way round that gate.
    #
    # Each call below is handed that same router and registers one concern on
    # it, in the order the paths were declared in before the split.
    health_api.register(api, ctx)
    plugins_api.register(api, ctx)
    plugin_config_api.register(api, ctx)
    personas_api.register(api, ctx)
    trace_api.register(api, ctx)
    config_api.register(api, ctx)
    keys_api.register(api, ctx)
    accounts_api.register(api, ctx)

    # -- assembly ----------------------------------------------------------

    router = APIRouter()
    router.include_router(api)

    # The three unauthenticated routes, on an unguarded router of their own.
    # `register_public` mounts nothing when this core has no account store, so
    # a core assembled without one has no sign-in surface to probe rather than
    # one that answers 503.
    accounts_api.register_public(router, ctx)

    # The designed admin UI (ADR-0020). It is mounted here, from the same
    # factory, so it is built with **the same `require_user`** the API above is
    # guarded by — one dependency, from one configured header, in front of both
    # presentations. Mounting it from the application instead would mean a
    # second construction of the same check, and a second thing to keep in step.
    from personacore.web.auth_pages import (
        create_public_auth_router,
        redirect_when_not_signed_in,
    )
    from personacore.web.routes import (
        STATIC_DIR,
        TEMPLATE_DIR,
        create_admin_ui_router,
    )

    if auth_context is not None:
        # Mounted BEFORE the guarded UI router and outside it: these are the
        # pages nobody can be signed in for. They are the only unauthenticated
        # HTML on the admin surface, and they answer only while the built-in
        # door is open — each one asks `AuthContext.require_builtin` per
        # request, so switching the door on the Core settings screen turns them
        # on and off without a restart.
        router.include_router(
            create_public_auth_router(
                templates=Jinja2Templates(directory=str(TEMPLATE_DIR)),
                context=auth_context,
                stylesheet=STATIC_DIR / "nocturne.css",
            )
        )

    router.include_router(
        create_admin_ui_router(
            # The same dependency the JSON API is guarded by, wrapped so a
            # browser is sent to the sign-in page instead of being handed a 401
            # body it cannot act on. The check is not repeated — only its
            # refusal is presented differently (see
            # `redirect_when_not_signed_in`).
            require_user=(
                redirect_when_not_signed_in(require_user, context=auth_context)
                if auth_context is not None
                else require_user
            ),
            auth_context=auth_context,
            layout=layout,
            audit=audit,
            llm=llm,
            bus=bus,
            scans=scans,
            disk_warning_bytes=disk_warning_bytes,
            # The API's own persist-and-apply helper, for the same reason
            # `require_user` is passed rather than rebuilt: the settings
            # screens and `PUT /admin/api/config` must validate, write, apply
            # live and audit through one path, or "saved" means two things.
            save_config=_save_config,
            # The core's own persona store — the same object the agent loop
            # loads a persona from on every turn. The Personas screen creates,
            # edits and deletes the folders it reads, so a second store here
            # would be a second cache and a second answer to "what is on disk".
            personas=personas,
            preferences=preferences,
            # The agent loop's own runner, the same one the assembly hands
            # everything else: a turn taken on the admin chat screen is the
            # turn the core takes anywhere, with the same persona, policy and
            # audit trail behind it (PC-152).
            chat=chat,
            # The same supervisor view `_PluginScanCache` above already holds,
            # for the plugin health and plugin output screens (PC-279, PC-280).
            # Passed rather than reached for through the cache: the cache's copy
            # is a private detail of how a listing gets built, and two screens
            # reading a supervisor is not that.
            plugin_health=plugin_health,
            # The same ceilings `POST /admin/api/plugins/install` installs
            # under. The UI route hands its upload to that endpoint's own
            # handler, which enforces them; this is passed so the page can
            # refuse an obviously oversized file before it is spooled to disk,
            # against the same number rather than a second one.
            package_limits=package_limits,
        )
    )

    return router


__all__ = [
    "ANONYMOUS_KEY_REFUSED",
    "DEFAULT_DISK_WARNING_BYTES",
    "DISABLED_PLUGIN_DETAIL",
    "KEYS_UNAVAILABLE",
    "KEY_ID_PATTERN",
    "MAX_TRACE_LIMIT",
    "MULTIPART_NOT_ACCEPTED",
    "UPLOAD_TOO_LARGE",
    "build_api_key_listing",
    "build_api_key_view",
    "build_persona_listing",
    "build_plugin_listing",
    "build_system_health",
    "build_trace_page",
    "create_admin_router",
    "make_admin_user_dependency",
]
