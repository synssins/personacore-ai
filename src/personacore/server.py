"""The HTTP application — spec sections 5.4 and 9, assembled.

One listener serves both surfaces on different paths:

    /v1      the exposed OpenAI-compatible API (section 5.4)
    /admin   the admin API and the admin UI on it (section 9, ADR-0020)

TLS terminates at the reverse proxy (section 7), so this listener is plain HTTP
and is not safe to publish to a network you do not control.

The health endpoint is always served, whatever else is or is not. That is
deliberate: the container's health check must answer even when a surface is
missing, otherwise a partial deployment looks identical to a dead one and the
orchestrator restarts a container that is fine.

**This file wires. It does not implement** (ADR-0040). It reads settings, builds
the pieces, hands them to each other and starts. Everything that decides *how*
one of those pieces behaves lives in :mod:`personacore.boot`, one concern to a
module — the LLM roster, the chat runner, speech, the bus, the surfaces, the
identity guard, retention, ports. The test is mechanical: a function that would
still make sense with a different UI, a different store, or no HTTP at all does
not belong here. This is the boot path, so a mistake in it is a container that
does not start, and that has already happened once.

Every name this module exported before that split still resolves here — see
``__all__``. The code moved; the address did not.

Which pieces may fail to load without costing the container, and which may not,
is ADR-0040 §3 and :mod:`personacore.boot.degrade`. Settings, the audit store,
the admin surface and authentication are constructed directly below and are not
guarded by anything: a core that cannot have one of them refuses to start rather
than serving a health check that says it is fine.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse, RedirectResponse

from personacore import CONTRACT_VERSION, __version__, workspaces
from personacore.admin.authn import AuthContext, LiveAuth
from personacore.agent.loop import (
    AgentLoop,
    AgentLoopConfig,
)
from personacore.agent.personas import PersonaStore, ensure_default_persona
from personacore.attachments import purge_orphaned as purge_orphaned_attachments
from personacore.audit.logging import (
    LoggingConfig,
    configure_logging,
)
from personacore.audit.models import AuditStoreConfig
from personacore.audit.store import AuditStore
from personacore.auth.accounts import UserStore
from personacore.auth.method import resolve_auth
from personacore.auth.sessions import SessionStore
from personacore.auth.throttle import SignInThrottle
from personacore.boot.bus import _resolve_bus_password, apply_bus_settings
from personacore.boot.chat import (
    _aclose,
    _AdminChat,
    _AdminChatEvent,
    _AdminChatResult,
    _make_chat_runner,
    _offer_speech,
)
from personacore.boot.degrade import (
    DEGRADED_ATTRIBUTE,
    DegradedPieces,
    load_optional,
    optional,
)
from personacore.boot.identity import (
    DEFAULT_TRUSTED_USER_HEADER,
    DEV_ADMIN_USER_ENV,
    TRUSTED_PROXY_ENV,
    CorrelationIdMiddleware,
    _install_identity_guard,
    _parse_trusted_proxies,
    _setup_required,
    apply_auth_settings,
)
from personacore.boot.llm import (
    MAX_PERSONA_ENDPOINTS,
    PERSONA_LLM_KEY_MISSING,
    LiveLLM,
    LLMRoster,
    PersonaLLMRouter,
    _build_llm_client,
    _endpoint_digest,
    _resolve_llm_api_key,
    llm_key_unavailable,
)
from personacore.boot.plugins import _PluginHealthView, _PluginRuntimeStatus
from personacore.boot.ports import _checked_port, _override_port
from personacore.boot.retention import _resolve_retention
from personacore.boot.secrets import _migrate_secrets
from personacore.boot.speech import (
    _wyoming_bind_failure,
    apply_hearing_settings,
    apply_voice_settings,
    apply_wyoming_settings,
)
from personacore.boot.surfaces import (
    _api_key_store,
    _keyless_profile,
    _keyless_settings,
    _mount_admin,
    _mount_openai,
)
from personacore.bus.client import EventBus
from personacore.config import AppdataLayout, SecretStore, load_core_settings
from personacore.config.settings import (
    KEYLESS_PROFILE_ID,
    CoreSettings,
    LLMRole,
    ensure_core_config,
)
from personacore.hearing.registry import HearingRegistry
from personacore.hearing.registry import builtin_engines as builtin_recognisers
from personacore.llm import LLMResponseError
from personacore.memory.composite import CompositeToolProvider
from personacore.memory.embed import Embedder
from personacore.memory.provider import CoreMemoryProvider
from personacore.memory.review import (
    QuietConversationFinder,
    ReviewRunner,
    run_review_ticker,
)
from personacore.memory.store import MemoryStore
from personacore.memory.tools import MemoryTools
from personacore.plugins.bundled import install_bundled_plugins
from personacore.plugins.discovery import PluginDiscovery
from personacore.plugins.host import PluginHost
from personacore.plugins.packages import read_disabled_plugins
from personacore.preferences import PREFERENCES_FILENAME, PreferenceStore
from personacore.voice.library import VoiceLibrary, voice_health
from personacore.voice.registry import VoiceRegistry, builtin_engines
from personacore.voice.reply import SPEAKER_ATTRIBUTE, ReplySpeaker
from personacore.web.shared import KeepsTheAddressBar, method_not_allowed
from personacore.workspace_tools import WorkspaceTools
from personacore.wyoming import WyomingService

log = structlog.get_logger(__name__)

#: Every name this module exported before ADR-0040 still resolves here.
#:
#: The code moved into ``personacore.boot``; the address did not. Nothing that
#: says ``from personacore.server import X`` — a caller, a screen, a test — is
#: asked to learn where X went, which is what makes the split safe to do a
#: module at a time rather than in one commit nobody can bisect.
#:
#: The private names are listed for the same reason the public ones are: the
#: tests import them, and a re-export that ``ruff`` deletes as unused is a
#: broken import somebody finds at runtime.
__all__ = [
    "DEFAULT_TRUSTED_USER_HEADER",
    "DEV_ADMIN_USER_ENV",
    "MAX_PERSONA_ENDPOINTS",
    "PERSONA_LLM_KEY_MISSING",
    "TRUSTED_PROXY_ENV",
    "CorrelationIdMiddleware",
    "LLMRoster",
    "LiveLLM",
    "PersonaLLMRouter",
    "_AdminChat",
    "_AdminChatEvent",
    "_AdminChatResult",
    "_PluginHealthView",
    "_PluginRuntimeStatus",
    "_aclose",
    "_api_key_store",
    "_build_llm_client",
    "_checked_port",
    "_endpoint_digest",
    "_install_identity_guard",
    "_keyless_profile",
    "_keyless_settings",
    "_make_chat_runner",
    "_migrate_secrets",
    "_mount_admin",
    "_mount_openai",
    "_offer_speech",
    "_override_port",
    "_parse_trusted_proxies",
    "_resolve_bus_password",
    "_resolve_llm_api_key",
    "_resolve_retention",
    "_setup_required",
    "_wyoming_bind_failure",
    "create_app",
    "llm_key_unavailable",
    "serve",
]

RETENTION_SHUTDOWN_TIMEOUT_SECONDS = 30.0
"""How long shutdown waits for a purge that is already running.

Bounded on purpose: a purge sweeping a very large database is worth waiting
for, but an orchestrator killing the container because shutdown never returned
is worse than a purge that gets interrupted, and the next startup purges again
anyway."""

RETENTION_PURGE_INTERVAL_SECONDS = 6 * 60 * 60
"""How often the scheduled purge (ADR-0004) sweeps the audit store once
started, in seconds. Not a config field: CLAUDE.md's brief for this fix is
explicit that how often the sweep runs is an operational detail, and adding
one here would be an out-of-scope contract change."""


def create_app(appdata: Path | str | None = None) -> FastAPI:
    # ADR-0040 §3. An optional piece that will not load is recorded here,
    # skipped, and named on /health and on the Health screen with the exception
    # that stopped it. A fatal piece is not wrapped by anything: settings, the
    # audit store, the admin surface and authentication are constructed
    # directly below, so a core that cannot have one of them refuses to boot
    # rather than starting up looking fine without its own front door.
    #
    # Built before the application object, because the engines are loaded
    # before there is an `app` to hang anything on.
    pieces = DegradedPieces()
    layout = AppdataLayout(appdata or os.environ.get("PERSONACORE_APPDATA", "./appdata"))
    layout.ensure()
    configure_logging(LoggingConfig(log_dir=layout.audit, to_stdout=True))
    # Before anything reads a secret. The LLM key and the broker password are
    # read further down through `core_secrets()`, and a core upgraded across
    # ADR-0025 still has them lying flat in `secrets/`, so a migration that ran
    # after them would degrade a working install for one boot.
    _migrate_secrets(layout)
    # A container never runs `init`, so without this the config directory stays
    # empty and there is no discoverable way to point the assistant at an LLM.
    if ensure_core_config(layout):
        log.info("core_config_created", path=str(layout.core_config_file))
    # Same reasoning as the config file: a container never runs `init`, and
    # without a persona the very first turn fails on a missing file.
    if ensure_default_persona(layout):
        log.info("default_persona_created", path=str(layout.personas))
    # The reference plugin and template ship in the image; appdata is where
    # plugins are read from, so they have to be put there or they may as well
    # not exist on the machine running the assistant.
    seeded = load_optional(
        pieces,
        "the bundled plugins",
        lambda: install_bundled_plugins(layout),
        costs=(
            "the reference plugin and the plugin template were not copied into "
            "appdata, so they are not offered on the Plugins screen"
        ),
        fallback=(),
    )
    if seeded:
        log.info("bundled_plugins_installed", plugins=seeded)
    settings = load_core_settings(layout)

    # One client per distinct endpoint, one handle per role (ADR-0011). A
    # single-endpoint deployment gets exactly one client, as before.
    roster = LLMRoster(layout, settings)

    audit = AuditStore(
        AuditStoreConfig(
            database_path=layout.audit / "audit.db",
            # ADR-0004: the configured window, not the store's own default.
            # Without this the store always purges on its built-in 30-day
            # default regardless of what core.toml says.
            retention=_resolve_retention(settings.retention),
        )
    )
    personas = PersonaStore(layout, default_persona=settings.default_persona)
    # Per-person settings (ADR-0030). Its own database, not the audit one:
    # that file's contents are trimmed by the retention purge on a timer and
    # settings must outlive it.
    preferences = PreferenceStore(layout.state / PREFERENCES_FILENAME)
    discovery = PluginDiscovery(layout.root)

    # Speech (ADR-0029). Assembling the registry starts nothing: an engine that
    # was never switched on has loaded nothing, which is what makes shipping
    # several engines in one image defensible (PC-335). The switches are
    # applied in `_startup` below, after the listener is already up, and
    # `builtin_engines` swallows its own import failures -- an engine module
    # that will not import is the same lockout class as an unreadable plugin
    # file, and voice is the most detachable thing in the system.
    #
    # ADR-0040 §3 puts one more guard round it: `builtin_engines` already
    # swallows a module that will not import, and this catches the case it
    # cannot -- the call itself failing -- so that a build whose speech is
    # broken outright starts silent instead of not starting.
    voice_engines, voice_problems = load_optional(
        pieces,
        "the speech engines",
        builtin_engines,
        costs="the assistant has no voice; every reply is text",
        fallback=((), ()),
    )
    voice_registry = VoiceRegistry(voice_engines)
    for problem in voice_problems:
        voice_registry.add_problem(problem)
    voices = VoiceLibrary(voice_registry, layout)

    # The ears, assembled exactly like the mouth above and for the same
    # reasons: every recogniser is a switch, `builtin_engines` swallows its own
    # import failures, and nothing here is started until `_startup` has the
    # listener up. A recogniser that will not import costs listening and
    # nothing else.
    hearing_engines, hearing_problems = load_optional(
        pieces,
        "the speech recognisers",
        builtin_recognisers,
        costs="the assistant cannot listen; dictation and Wyoming are text only",
        fallback=((), ()),
    )
    hearing_registry = HearingRegistry(hearing_engines)
    for problem in hearing_problems:
        hearing_registry.add_problem(problem)

    # Home Assistant's speech, served from this core. Constructed always,
    # started only if `[wyoming] enabled` -- an unauthenticated port is not
    # something to open by accident. Replaced, not reconfigured, when the
    # setting changes: host and port are read once when the socket is opened,
    # so moving the listener means a new one (see `_apply_wyoming_settings`).
    #
    # Optional (ADR-0040 §3): a core that cannot build this listener still
    # answers on its own port. `None` is the "this core has not got one" state,
    # and `apply_wyoming_settings` reads it as nothing to stop.
    wyoming = load_optional(
        pieces,
        "the Wyoming listener",
        lambda: WyomingService(
            settings.wyoming,
            hearing=hearing_registry,
            voices=voices,
            version=__version__,
        ),
        costs="Home Assistant cannot reach this core's speech or transcription",
        fallback=None,
    )

    # What turns a finished reply into audio (PC-256). It holds no engine and
    # loads nothing -- it asks the library, per reply, whether this persona's
    # voice can speak, and keeps the words against a handle for the surface to
    # fetch. Assembling it therefore costs nothing on a core whose engines are
    # all switched off, which is every core until somebody flips a switch.
    # Optional for the same reason the engines are: every consumer already
    # treats a speaker it has not got as "this reply is text", which is what
    # makes `None` a state here rather than a hole.
    reply_speech = load_optional(
        pieces,
        "reply speech",
        lambda: ReplySpeaker(voices, personas),
        costs="replies cannot be played back; the text of every reply is unaffected",
        fallback=None,
    )

    bus_password, bus_password_error = _resolve_bus_password(layout, settings.bus)
    bus = EventBus(settings.bus, password=bus_password)

    app = FastAPI(
        title="PersonaCore",
        version=__version__,
        docs_url="/admin/api/docs",
        openapi_url="/admin/api/openapi.json",
    )
    app.state.layout = layout
    #: Every optional piece that would not load, for /health and the Health
    #: screen to name (ADR-0040 §3). Published as soon as there is an
    #: application, because everything above this line can already be in it.
    setattr(app.state, DEGRADED_ATTRIBUTE, pieces)
    app.state.settings = settings
    app.state.llm = roster
    app.state.bus = bus
    app.state.audit = audit
    app.state.surfaces = set()
    app.state.retention_task = None
    app.state.retention_purge = None
    # What /health reports about the purge: a sweep that has been failing since
    # the last restart is otherwise only visible in a log nobody reads, and
    # ADR-0004 makes age-out a promise rather than best effort.
    app.state.retention_status = {
        "last_success": None,
        "last_error": None,
        "consecutive_failures": 0,
    }
    # Workspace contract §13, D: the boot probe's own finding about whether
    # the interactive model actually honours `chat_template_kwargs:
    # {enable_thinking: false}` — read by the Health screen's LLM row.
    # `None` until `_startup` runs the probe (or forever, on a build with no
    # LLM configured at all); see `_probe_thinking_switch` below.
    app.state.llm_thinking_probe = None
    app.state.bus_password_degraded = bus_password_error
    app.state.voice_registry = voice_registry
    app.state.hearing_registry = hearing_registry
    app.state.hearing_task = None
    #: The listener that is current. Read through `app.state` rather than
    #: through the local name above, because applying a saved `[wyoming]`
    #: replaces the object.
    app.state.wyoming = wyoming
    #: Why nothing is listening on a core whose switch is on -- the bind that
    #: failed, in one sentence, for the Core settings screen to print. `None`
    #: whenever the listener is doing what the switch says.
    app.state.wyoming_error = None
    app.state.voices = voices
    setattr(app.state, SPEAKER_ATTRIBUTE, reply_speech)
    app.state.voice_task = None
    # What the last apply had to say that no engine's own status carries: a
    # switch that is on in the settings for an engine which cannot run here,
    # and a switch naming an engine this core has not got (PC-338, PC-341).
    # Kept on the application rather than logged and dropped, because the
    # operator who flipped that switch is expecting speech and the engines
    # screen is where they flipped it.
    app.state.voice_notes = ()

    # A form post that renders a page must not leave its own POST-only URL in
    # the address bar, or the next refresh is `{"detail":"Method Not Allowed"}`.
    # This was hit twice on the chat screen's persona picker; the reasoning,
    # and why the markup could not fix it, is on the class.
    #
    # Registered on the application rather than on the admin router because
    # middleware is an application-level thing in ASGI, and unconditionally
    # because its own conditions already exclude everything that is not a
    # rendered admin page.
    app.add_middleware(KeepsTheAddressBar)
    # And what a person gets if one reaches the address bar anyway — from a
    # bookmark, from history, or from a browser with no scripting at all. The
    # JSON API keeps its own 405; only a browser asking for a page is sent to
    # one. See `method_not_allowed`.
    app.add_exception_handler(status.HTTP_405_METHOD_NOT_ALLOWED, method_not_allowed)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Land on the admin UI. Nobody types ``/admin/chat`` from memory.

        The redirect lives here rather than on the admin router because the
        root path belongs to the *application*: the admin surface is mounted
        under ``/admin`` and must not claim a top-level path something else may
        want later (the same reasoning ADR-0020 gives for ``/admin/static``).

        307 rather than 302: the method is preserved, so nothing about this
        changes what a non-GET caller at the root would mean, and no browser
        caches it as permanent while the landing screen is still being chosen.

        It is declared **before** ``/health`` for readability only — the paths
        are distinct, so the liveness endpoint is untouched by it.
        """
        return RedirectResponse(url="/admin/chat", status_code=307)

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """Liveness, always present even when every other surface is not."""
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "contract": CONTRACT_VERSION,
                "surfaces": sorted(app.state.surfaces),
                # Surfaced so an auth bypass cannot quietly survive into a
                # deployment that has a proxy in front of it.
                "admin_auth_bypass": app.state.dev_admin_user,
                "trusted_proxies": app.state.trusted_proxies,
                # PC-294: "Health must state plainly which method is in force,
                # and say loudly when the bypass is on." One object rather than
                # a scattering of flags, so there is no reading of this that
                # leaves the door ambiguous. Unauthenticated, so it carries no
                # account names and no count of them - only which door, and
                # whether the break-glass is open.
                "admin_auth": {
                    **app.state.auth_health,
                    "setup_required": _setup_required(app),
                },
                # ADR-0018 §2 asked for this by name: an operator must be able
                # to see from one endpoint whether the API's door is open. It
                # sits beside `admin_auth` and not inside it because they are
                # different doors -- this one is `/v1` and nothing else.
                #
                # The tool list is here too, because "keyless is on" and "a
                # keyless caller can turn the lights off" are not the same
                # posture, and only one of them can be read off a boolean.
                "keyless_api": {
                    "enabled": _keyless_settings(app).enabled,
                    "profile": KEYLESS_PROFILE_ID,
                    "allowed_tools": list(_keyless_settings(app).allowed_tools),
                },
                # A purge that has been failing every pass since startup is
                # otherwise only visible in the log (ADR-0004).
                "retention": dict(app.state.retention_status),
                # The memory review pass: whether quiet conversations are
                # being read, and what the last pass did (memory contract §5.2).
                "memory_review": dict(app.state.memory_review_status),
                # Set when [bus].password_secret could not be read: the bus is
                # running unauthenticated, which is a degraded push channel,
                # not a healthy one.
                "bus_password_degraded": app.state.bus_password_degraded,
                # What the bus is ACTUALLY pointed at, read off the running
                # object rather than re-read from core.toml. "Name or service
                # not known" on its own cannot tell a wrong address from an
                # unreachable one from a process still holding yesterday's
                # config; the address beside it can, and that difference was an
                # hour of somebody's life.
                #
                # Raw values, exactly as configured — JSON quotes them, so a
                # trailing space shows here without any further dressing up.
                # `password_set` and no more: never the password, and never the
                # name of the secret it came from (spec section 7).
                "bus": app.state.bus.health.as_dict(),
                # ADR-0040 §3: every optional piece this core skipped, what
                # that costs, and the exception that stopped it. Empty on a
                # core with nothing missing. Here rather than only in the log
                # because a degradation nobody can see is an outage with
                # better manners -- and because the container's health check
                # is the one thing that reads this endpoint on every core.
                "degraded": pieces.as_health(),
                # Which engines this build has, which are on, anything that
                # cost one, and every voice folder that was skipped with the
                # reason it was skipped (ADR-0029).
                #
                # The skipped list is the snapshot, NOT a fresh walk: this
                # endpoint is polled by the container healthcheck every few
                # seconds and must not crawl `appdata/voices` to answer. The
                # walk happens where voices actually change —
                # `apply_voice_settings` in `boot.speech`, which runs at
                # startup and on every save — and on the Voices screen, which
                # install and remove both re-render. An operator who uploaded a
                # voice and cannot find it is owed the reason; a healthcheck is
                # not owed a disk read.
                "voice": voice_health(
                    voice_registry, listing=voices.snapshot()
                ).as_dict(),
            }
        )

    # ADR-0011 leaves WHICH role each internal caller uses to the moment that
    # caller exists. The agent loop is conversation, so it is `interactive` —
    # the one role that is always configured.
    # A plugin switched off in the UI must stay off across a restart, so the
    # persisted set is read here rather than after the supervisor has already
    # started everything it found.
    host = PluginHost(
        discovery,
        secrets=SecretStore(layout),
        audit=audit,
        disabled=read_disabled_plugins(layout),
    )
    app.state.plugin_host = host

    # Memory (``working/contracts/memory.md``, ADR-0045): a bundled ONNX
    # embedder if this image carries one, else no memory at all rather than a
    # core that refuses to start over an optional feature. `MemoryTools` is
    # folded into one `ToolProvider` with the plugin host through
    # `CompositeToolProvider` (contract §2: "The loop does not learn that
    # some tools are the core's own") — every consumer of `host` that lists
    # or calls tools for a real turn takes this composite instead, and only
    # the plugin-management surfaces below (`app.state.plugin_host`,
    # `_PluginHealthView`, `_mount_admin`'s own plugin routes, `host.start`/
    # `.stop`) still take the bare `PluginHost`, which is the only thing that
    # knows how to install, remove or health-check a plugin.
    embedder = Embedder.bundled() if Embedder.available() else None
    memory_store: MemoryStore | None = None
    memory_provider: CoreMemoryProvider | None = None
    memory_tools: MemoryTools | None = None
    if embedder is not None:
        memory_store = MemoryStore(layout.memory / "memory.db", embedder, settings.memory)
        memory_provider = CoreMemoryProvider(memory_store, settings.memory)
        memory_tools = MemoryTools(memory_store, personas)
    else:
        log.warning("memory_unavailable", reason="embedding model not bundled")
    app.state.memory_store = memory_store

    # Workspace (``working/contracts/workspace.md`` §5): a per-conversation
    # folder and three model-facing tools. Unlike memory this needs no
    # bundled model — only the appdata layout and its own settings — so it is
    # always built, independent of whether `memory_tools` above is `None`.
    workspace_tools = WorkspaceTools(layout, settings.workspace)
    app.state.workspace_tools = workspace_tools

    # `CompositeToolProvider` folds the plugin host and the core's own tool
    # families into one `ToolProvider` (contract §2: "The loop does not learn
    # that some tools are the core's own") — every consumer of `host` that
    # lists or calls tools for a real turn takes this composite instead, and
    # only the plugin-management surfaces below (`app.state.plugin_host`,
    # `_PluginHealthView`, `_mount_admin`'s own plugin routes, `host.start`/
    # `.stop`) still take the bare `PluginHost`, which is the only thing that
    # knows how to install, remove or health-check a plugin. Built
    # unconditionally now that workspace tools do not depend on `memory_tools`
    # being real; `CompositeToolProvider` itself tolerates either being
    # `None`.
    tools_provider = CompositeToolProvider(host, memory_tools, workspace=workspace_tools)

    # The review pass (memory contract §5.2): a conversation that has gone
    # quiet is read once by the triage role for facts worth keeping. Built
    # here beside the store because it is the store's second writer; started
    # in the lifespan below, the same way the retention purge is. The triage
    # handle is always returned by the roster; `unusable` is the roster's own
    # word for a role with nothing behind it, and the runner checks it on
    # every tick, so configuring the role later needs no restart.
    app.state.memory_review_task = None
    app.state.memory_review_status = {
        "last_success": None,
        "last_error": None,
        "reviewed": 0,
        "written": 0,
        "touched": 0,
        "dropped": 0,
        "skipped": 0,
    }
    memory_review_runner: ReviewRunner | None = None
    if memory_store is not None:
        triage_llm = roster.for_role(LLMRole.TRIAGE)
        if triage_llm.unusable is not None:
            log.warning("memory_review_unavailable", reason=triage_llm.unusable)
        elif triage_llm.falls_back_to is not None:
            # Contract §5.2: never on the interactive model. A triage role that
            # borrowed interactive's connection is, for this pass, no role.
            log.warning(
                "memory_review_unavailable",
                reason="the triage role has no connection of its own",
            )
        memory_review_runner = ReviewRunner(
            memory_store,
            QuietConversationFinder(
                audit,
                personas,
                # Read live, so a change on Core settings applies on the next
                # tick rather than after a restart.
                quiet_minutes_provider=lambda: app.state.settings.memory.quiet_minutes,
            ),
            triage_llm,
            personas,
            settings.memory,
        )

    # ADR-0036: a persona may carry a connection of its own, and the loop asks
    # the router which client answers for the character it just loaded. The
    # handle passed as `llm` is still what answers for every persona that has
    # no connection, which is all of them until somebody says otherwise.
    interactive_llm = roster.for_role(LLMRole.INTERACTIVE)
    agent = AgentLoop(
        llm=interactive_llm,
        personas=personas,
        audit=audit,
        tools=tools_provider,
        # `[memory] recall_limit` and `[workspace] tool_result_chars` reach the
        # loop's own tunables here; like the other loop tunables both are read
        # at boot, and the Core settings screen says so beside each box.
        config=AgentLoopConfig(
            memory_recall_limit=settings.memory.recall_limit,
            max_untrusted_chars=settings.workspace.tool_result_chars,
        ),
        persona_llm=PersonaLLMRouter(roster, interactive_llm),
        memory=memory_provider,
        # Workspace contract §3/§4/§6: the same `WorkspaceTools` instance
        # folded into `tools_provider` above, held here too because saving a
        # tool's own files and composing the manifest/pins happen in the loop
        # directly rather than through a tool call.
        workspace=workspace_tools,
    )
    app.state.agent = agent

    trusted_header = os.environ.get(
        "PERSONACORE_TRUSTED_USER_HEADER", DEFAULT_TRUSTED_USER_HEADER
    )
    dev_admin_user = os.environ.get(DEV_ADMIN_USER_ENV, "").strip()
    app.state.dev_admin_user = dev_admin_user or None

    # PC-294, decided once and in one place: which single way in is open. The
    # method comes from `[auth] method` in core.toml (ADR-0010: the operator
    # chooses it on the Core settings screen); only the break-glass bypass is
    # read from the environment, because it has to work when the settings
    # cannot be. Every surface below reads this object; nothing re-reads either
    # source, so what /health reports is what the running process is actually
    # doing -- and a method saved since boot is plainly pending a restart
    # rather than half in force.
    auth_decision = resolve_auth(settings.auth.method, dev_admin_user)
    app.state.auth_decision = auth_decision

    trusted_proxies = _parse_trusted_proxies(os.environ.get(TRUSTED_PROXY_ENV))
    app.state.trusted_proxies = sorted(trusted_proxies)
    app.state.auth_health = auth_decision.as_health(
        trusted_header=trusted_header, trusted_proxies=sorted(trusted_proxies)
    )
    _install_identity_guard(app, trusted_header, trusted_proxies, dev_admin_user or None)

    # The core's own accounts (PC-283). Assembled under every door, because the
    # door can now be changed without a restart (ADR-0010) and stores built only
    # under `builtin` could never be there when somebody switched to it.
    #
    # Constructing them opens nothing and writes nothing -- `UserStore` and
    # `SessionStore` both hold a path and read it lazily -- and they are not a
    # second way in: `LiveAuth` below hands the session cookie to nobody unless
    # `builtin` is the door in force, and every sign-in route asks
    # `AuthContext.require_builtin` per request. So there is still exactly one
    # door open at a time, which is what PC-294 forbids breaking.
    auth_context = AuthContext(
        decision=auth_decision,
        users=UserStore(layout),
        sessions=SessionStore(layout),
        throttle=SignInThrottle(),
        audit=audit,
    )
    app.state.auth_context = auth_context
    if auth_context.setup_required():
        log.warning(
            "first_run_setup_required",
            detail=(
                "This core uses its own sign-in and has no account yet. Open "
                "/admin/setup to create the first one. There is no default "
                "password."
            ),
        )
    # The holder every route asks, rather than the dependency every route closes
    # over -- the same shape `LiveLLM` uses (now in `boot.llm`), for the same
    # reason: saving `[auth] method` rebinds what is inside this object, and
    # nothing has to be re-mounted for the change to be in force on the next
    # request.
    live_auth = LiveAuth(trusted_header, decision=auth_decision, context=auth_context)
    app.state.live_auth = live_auth
    log.info(
        "auth_method_selected",
        method=auth_decision.method.value,
        chosen=auth_decision.chosen.value,
    )
    # Outermost, because it is registered last: every record any surface writes
    # while answering one request gets that request's correlation id (PC-012).
    app.add_middleware(CorrelationIdMiddleware)

    # What the running listener was built from, and the one save at a time that
    # gets to move it. On the application rather than in a local, because
    # `apply_wyoming_settings` owns the moving and this file no longer does:
    # comparing against the last applied settings keeps an unrelated save from
    # costing Home Assistant a transcription that is halfway through, and the
    # lock stops two overlapping saves leaving an orphan listener on a port
    # nothing in the process knows about.
    app.state.wyoming_applied = settings.wyoming
    app.state.wyoming_lock = asyncio.Lock()

    async def _apply_settings(new: CoreSettings) -> None:
        """Make a saved setting take effect now, per ADR-0010."""
        # Idempotent, and already done by `_apply_settings_sync` on the request
        # path. Here as well so the async entry point -- which tests and any
        # future caller use directly -- applies the whole document rather than
        # all of it but the door.
        apply_auth_settings(
            app,
            live_auth,
            new.auth,
            trusted_header=trusted_header,
            trusted_proxies=trusted_proxies,
        )
        # Re-resolves every role and swaps only what changed: a role whose
        # endpoint is untouched keeps its client, its pool and its breaker.
        await roster.apply(new)
        personas.set_default(new.default_persona)
        # Same bargain as the LLM roster: reconnect only what actually changed,
        # so saving an unrelated setting never drops a healthy broker.
        await apply_bus_settings(app, bus, layout, new.bus)
        # ADR-0010 wants a saved setting to take effect now, and says a setting
        # that cannot must say so in the UI. Retention can: the store reads its
        # window on every pass, so swapping the config is enough and nothing
        # has to wait for a restart.
        audit.set_retention(_resolve_retention(new.retention))
        # Before the engines rather than beside them: starting an engine loads
        # a model, and the listener Home Assistant is waiting on must not queue
        # behind that. It needs neither of them to bind -- it reads both
        # registries per request, so an engine that comes up a few seconds
        # later is simply available a few seconds later.
        await apply_wyoming_settings(app, hearing_registry, voices, new.wyoming)
        # ADR-0029 §2: saving a switch starts or stops that engine there and
        # then -- no restart, no compose edit, no shell. Each switch is
        # independent, so this touches only the engines that actually moved.
        await apply_voice_settings(app, voice_registry, voices, new.voice)
        await apply_hearing_settings(hearing_registry, new.hearing)
        app.state.settings = new
        log.info(
            "settings_applied",
            default_persona=new.default_persona,
            llm_roles_configured=[role.value for role in new.llm.configured_roles()],
        )

    # The live-apply path itself, not just the admin API's synchronous
    # wrapper around it: what a save actually changes without a restart is
    # worth being able to exercise directly (ADR-0010).
    app.state.apply_settings = _apply_settings

    def _apply_settings_sync(new: CoreSettings) -> None:
        # The door first and in this call, not in the task below: it costs a
        # dictionary lookup and a rebind, and it has to be true before the
        # response to the save is written. See `apply_auth_settings`.
        apply_auth_settings(
            app,
            live_auth,
            new.auth,
            trusted_header=trusted_header,
            trusted_proxies=trusted_proxies,
        )
        # The admin API's callback is synchronous; the rest of the swap is not.
        # Schedule it on the running loop rather than blocking the request that
        # saved it.
        asyncio.get_running_loop().create_task(_apply_settings(new))

    # One turn, non-streaming, with the finished reply offered to the voice
    # (PC-256). Kept on the application as well as handed to the admin surface
    # for the same reason `apply_settings` is: it is worth being able to take a
    # real turn through an assembled core without going through a screen that
    # is being redrawn.
    # `tools_provider`, not the bare `host`: the admin chat screen grants a
    # turn every safe tool currently listable (`_AdminChat._tools_for`), and
    # that has to include `memory.remember`/`memory.recall` when a persona
    # has memory on, or the composite would offer them to the loop while the
    # admin surface's own policy profile never allowed them.
    chat_runner = _make_chat_runner(agent, personas, tools_provider, reply_speech)
    app.state.chat_runner = chat_runner

    _mount_admin(
        app,
        layout,
        discovery,
        personas,
        preferences,
        audit,
        roster,
        bus,
        trusted_header,
        auth_decision,
        auth_context,
        live_auth,
        _apply_settings_sync,
        chat_runner,
        _PluginHealthView(host),
        host,
    )
    _mount_openai(app, layout, agent, audit)

    async def _run_retention_purge() -> None:
        """One purge pass (ADR-0004), recording its outcome for /health.

        A failure here must never take the service down -- it is background
        hygiene, not a request -- so it is logged and swallowed rather than
        left to kill the task. It is caught in here rather than in the loop
        because this runs as a task of its own (see below) and an exception
        nobody retrieves is reported as an error nobody can act on.
        """
        status = app.state.retention_status
        try:
            result = await audit.purge_older_than()
        except Exception as exc:  # noqa: BLE001 - a purge failure must never take the service down
            status["last_error"] = repr(exc)
            status["consecutive_failures"] += 1
            log.warning(
                "retention_purge_failed",
                error=repr(exc),
                consecutive_failures=status["consecutive_failures"],
            )
            return
        # The attachments belonging to whatever that pass just removed. A
        # second call rather than something `purge_older_than` does itself,
        # because that method knows a database path and nothing about the rest
        # of appdata -- it can orphan a row but cannot reach the bytes. Run
        # after, never before: a sweep that ran first would find nothing this
        # pass was about to orphan and the files would wait a whole cycle.
        #
        # Inside the same `try`, above, would let a failed file delete report
        # the whole purge as failed on /health when the rows really did go. So
        # it is caught on its own and counted the same way, and a purge that
        # removed rows still says so.
        try:
            swept = await purge_orphaned_attachments(layout, audit)
        except Exception as exc:  # noqa: BLE001 - background hygiene, never a request
            status["last_error"] = repr(exc)
            status["consecutive_failures"] += 1
            log.warning("attachment_purge_failed", error=repr(exc))
            return
        # Contract §7: a short-term memory unused for `[memory]
        # short_term_days` is purged; a promoted (long-term) one never is —
        # `MemoryStore.purge_short_term` enforces that by construction
        # (`holder != 'global'`), not by anything read here. `None` on a
        # build with no bundled embedder (see the store's construction
        # above), and then this counts zero rather than being skipped
        # silently — the same shape as every other count in this log line.
        # Read off `app.state.settings` rather than the `settings` this
        # closure captured at boot, so a value saved on the Core settings
        # screen takes effect on the very next pass, exactly as the voice
        # and hearing settings below already do.
        memories_deleted = 0
        if memory_store is not None:
            try:
                memories_deleted = await memory_store.purge_short_term(
                    older_than_days=app.state.settings.memory.short_term_days
                )
            except Exception as exc:  # noqa: BLE001 - background hygiene, never a request
                status["last_error"] = repr(exc)
                status["consecutive_failures"] += 1
                log.warning("memory_purge_failed", error=repr(exc))
                return
        # Workspace contract §2: a folder under `workspaces/` whose conversation
        # is gone or hidden is a stray — hide and delete already remove the
        # folder, so this only catches what a crash between the two left
        # behind. Same posture as the attachment sweep above: caught on its
        # own, counted, never a reason to call the row purge failed.
        workspaces_swept = 0
        try:
            visible = await audit.visible_conversation_ids()
            workspaces_swept = await asyncio.to_thread(workspaces.sweep, layout, visible)
        except Exception as exc:  # noqa: BLE001 - background hygiene, never a request
            log.warning("workspace_sweep_failed", error=repr(exc))
        status["last_success"] = datetime.now(UTC).isoformat()
        status["last_error"] = None
        status["consecutive_failures"] = 0
        log.info(
            "retention_purge_completed",
            workspaces_swept=workspaces_swept,
            audit_deleted=result.audit_deleted,
            transcript_deleted=result.transcript_deleted,
            attachments_removed=swept.removed,
            # Bytes still on disk with no row left pointing at them. Counted
            # rather than hidden: a file that would not delete is the one
            # thing here nothing will retry, because the sweep finds work by
            # reading the table it has just deleted from.
            attachment_files_left=swept.file_failures,
            memories_deleted=memories_deleted,
        )

    async def _retention_purge_loop() -> None:
        """Purge once at startup, then every RETENTION_PURGE_INTERVAL_SECONDS."""
        while True:
            purge = asyncio.create_task(_run_retention_purge(), name="retention-purge-pass")
            app.state.retention_purge = purge
            # Shielded, and a task in its own right, because the purge does its
            # work in a worker thread (AuditStore.purge_older_than ->
            # asyncio.to_thread) and a thread cannot be cancelled. Cancelling
            # this loop unwinds the await in microseconds while the sqlite work
            # carries on regardless, so shutdown needs something it can
            # genuinely wait for rather than a cancellation that only looks
            # complete.
            await asyncio.shield(purge)
            await asyncio.sleep(RETENTION_PURGE_INTERVAL_SECONDS)

    async def _probe_thinking_switch() -> None:
        """Workspace contract §13, D's own boot check: does the interactive
        model actually stop reasoning when asked to?

        One non-streaming request, `chat_template_kwargs: {enable_thinking:
        false}`, `max_tokens=8`. Three outcomes, each recorded on
        `app.state.llm_thinking_probe` for the Health screen's LLM row —
        this function never builds UI, only the fact:

        * The reply still carries a non-empty `reasoning_content` — the
          switch is ignored by this backend. Logged at `warning`.
        * The host answers with a 4xx — it does not know the field at all.
          Logged at `info`; not a warning, because "never heard of this"
          is a plainer, less alarming fact than "heard it and ignored it".
        * Anything else that goes wrong (timeout, connection refused, a
          5xx) — recorded as unreached and never raised. This is a boot
          courtesy, not a gate: nothing here may fail the boot, and it is
          skipped outright when the interactive role has no usable
          connection at all (no API key supplied, contract §13 note).
        """
        if interactive_llm.unusable is not None:
            return
        try:
            response = await interactive_llm.chat_completion(
                [{"role": "user", "content": "Say hi."}],
                max_tokens=8,
                chat_template_kwargs={"enable_thinking": False},
            )
        except LLMResponseError as exc:
            rejected = exc.status_code is not None and 400 <= exc.status_code < 500
            if rejected:
                log.info("thinking_switch_unsupported", model=interactive_llm.facts.get("model"))
                app.state.llm_thinking_probe = {
                    "checked": True,
                    "ignored": False,
                    "unsupported": True,
                    "model": interactive_llm.facts.get("model"),
                }
            else:
                log.info("thinking_switch_probe_failed", error=repr(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a boot courtesy, never fatal
            log.info("thinking_switch_probe_failed", error=repr(exc))
            return
        reasoning = response.choices[0].message.reasoning_content if response.choices else None
        ignored = bool(reasoning)
        if ignored:
            log.warning("thinking_switch_ignored", model=response.model)
        app.state.llm_thinking_probe = {
            "checked": True,
            "ignored": ignored,
            "unsupported": False,
            "model": response.model,
        }

    @app.on_event("startup")
    async def _startup() -> None:
        # The bus is a degradable dependency (spec §10): if no broker is reachable
        # the assistant loses its push channel and keeps working, reconnecting in
        # the background. Starting it must never block the listener coming up.
        with optional(
            pieces,
            "the event bus",
            costs="no push channel; nothing is notified of anything as it happens",
        ):
            await bus.start()
        # Plugins come up after the listener, so a slow or broken plugin delays
        # tools rather than the whole service (spec section 5.1).
        with optional(
            pieces,
            "the plugin host",
            costs="the assistant has no tools; conversation is unaffected",
        ):
            await host.start()
        # `memory_store` is `None` on a build with no bundled embedder — see
        # its construction above — and `open()` is never called on it then,
        # matching contract §3: "Memory off for every persona means the file
        # is never opened." A store that fails to open costs memory, not the
        # listener: the pattern every other optional piece here uses.
        if memory_store is not None:
            with optional(
                pieces,
                "the memory store",
                costs="no persona remembers or recalls anything this run",
            ):
                await memory_store.open()
        if memory_review_runner is not None:
            with optional(
                pieces,
                "the memory review pass",
                costs="nothing is kept after a conversation goes quiet; the tool still works",
            ):
                app.state.memory_review_task = asyncio.create_task(
                    run_review_ticker(
                        memory_review_runner, status=app.state.memory_review_status
                    ),
                    name="memory-review",
                )
        # Same reasoning as the bus above: creating the task returns
        # immediately, so a slow first purge (a large database, a slow disk)
        # never delays the listener coming up.
        with optional(
            pieces,
            "the retention purge",
            costs="nothing ages out of the audit store on its own (ADR-0004)",
        ):
            app.state.retention_task = asyncio.create_task(
                _retention_purge_loop(), name="retention-purge"
            )
        # Speech comes up last and on its own task, so loading a model never
        # delays the listener -- and an engine that cannot start costs speech
        # and nothing else (ADR-0029 §6). Not awaited here on purpose: the
        # first thing a broken engine must not do is hold up the port.
        app.state.voice_task = asyncio.create_task(
            apply_voice_settings(
                app, voice_registry, voices, app.state.settings.voice
            ),
            name="voice-engines",
        )
        app.state.hearing_task = asyncio.create_task(
            apply_hearing_settings(hearing_registry, app.state.settings.hearing),
            name="hearing-engines",
        )
        # Wyoming, if the operator opened it. Through the same path a save
        # takes, so "it starts at boot" and "it starts when you hit save" are
        # one behaviour with one set of failure handling rather than two that
        # can drift. Degraded rather than fatal: a port already in use must not
        # stop the core, because the admin UI is how an operator would fix the
        # setting that is wrong (ADR-0029 §6).
        with optional(
            pieces,
            "the Wyoming listener",
            costs="Home Assistant cannot reach this core's speech or transcription",
        ):
            await apply_wyoming_settings(
                app, hearing_registry, voices, app.state.settings.wyoming
            )
        # Workspace contract §13, D: awaited, not backgrounded — it is one
        # bounded request (the LLM client's own timeout) and the release
        # note (ADR-style discipline this build follows) is "boot the core
        # before every tag", which means knowing the answer before the port
        # opens, not finding out later from a Health screen nobody is
        # looking at yet. `_probe_thinking_switch` never raises.
        await _probe_thinking_switch()
        log.info(
            "personacore_started",
            version=__version__,
            appdata=str(layout.root),
            surfaces=sorted(app.state.surfaces),
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        # Before anything else it might be mid-conversation with. Off
        # `app.state`, not off the name bound at assembly: a saved `[wyoming]`
        # replaces the service, and stopping the one this core started with
        # would leave the current listener holding its port.
        try:
            # `None` on a core that could not build one (ADR-0040 §3).
            if app.state.wyoming is not None:
                await app.state.wyoming.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown never fails on speech
            log.warning("wyoming_stop_failed", error=repr(exc))
        task = app.state.hearing_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            app.state.hearing_task = None
        try:
            await asyncio.to_thread(hearing_registry.shutdown)
        except Exception as exc:  # noqa: BLE001 - ditto
            log.warning("hearing_shutdown_failed", error=repr(exc))
        await host.stop()
        await bus.stop()
        task = app.state.retention_task
        if task is not None:
            # Same pattern EventBus.stop() uses: cancel, then await the task
            # so cancellation actually lands before shutdown returns, with the
            # expected CancelledError suppressed rather than left to escape.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            app.state.retention_task = None
        purge = app.state.retention_purge
        if purge is not None and not purge.done():
            # Cancelling the loop stopped the await, not the worker thread the
            # purge is running in -- which still holds the store's sqlite
            # connection. Wait for it, bounded, so a pathological purge delays
            # shutdown instead of hanging it, and say so if the wait runs out
            # rather than reporting a clean stop that did not happen.
            _, still_running = await asyncio.wait(
                {purge}, timeout=RETENTION_SHUTDOWN_TIMEOUT_SECONDS
            )
            if still_running:
                log.warning(
                    "retention_purge_still_running_at_shutdown",
                    timeout_seconds=RETENTION_SHUTDOWN_TIMEOUT_SECONDS,
                )
        app.state.retention_purge = None
        review_task = app.state.memory_review_task
        if review_task is not None:
            review_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await review_task
            app.state.memory_review_task = None
        # Only now: the retention task and its (possibly still-running)
        # worker-thread pass are both stopped above, and that pass is what
        # holds the memory store's own sqlite connection open the same way
        # `AuditStore`'s connection is held — closing any earlier races the
        # purge exactly as the comment above this block describes.
        if memory_store is not None:
            try:
                await memory_store.close()
            except Exception as exc:  # noqa: BLE001 - shutdown never fails on memory
                log.warning("memory_store_close_failed", error=repr(exc))
        voice_task = app.state.voice_task
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await voice_task
        app.state.voice_task = None
        # Off means off, including on the way out: a stopped engine holds no
        # model. In a thread because releasing a runtime is blocking work, and
        # guarded because shutdown reporting a failure it cannot act on helps
        # nobody.
        try:
            await asyncio.to_thread(voice_registry.shutdown)
        except Exception as exc:  # noqa: BLE001 - shutdown never fails on speech
            log.warning("voice_shutdown_failed", error=repr(exc))
        await roster.aclose()

    return app


def serve(
    host: str | None = None, port: int | None = None, appdata: Path | str | None = None
) -> None:
    """Run the HTTP server -- the container's entry point.

    Bind address and port, in priority order: an explicit argument, then
    ``PERSONACORE_HOST`` / ``PERSONACORE_PORT`` (ADR-0010 -- the environment is
    allowed to set these because they must be known before core.toml can even
    be read), then ``[server]`` in core.toml (:class:`ServerSettings`), which
    is otherwise unused.
    """
    import uvicorn

    # Resolved BEFORE the app is assembled: a bad port is a config error, and
    # finding it out after the database is open and the plugin host is running
    # buries the one line that matters under a page of startup logging.
    overridden_port = _override_port(port, os.environ.get("PERSONACORE_PORT"))

    app = create_app(appdata)
    settings: CoreSettings = app.state.settings
    bind_host = host or os.environ.get("PERSONACORE_HOST") or settings.server.host
    bind_port = overridden_port if overridden_port is not None else settings.server.port
    uvicorn.run(app, host=bind_host, port=bind_port, log_config=None)
