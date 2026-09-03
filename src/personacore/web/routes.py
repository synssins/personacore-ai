"""The designed admin UI — server-rendered pages and HTMX fragments (ADR-0020).

This is the *second* presentation of data the JSON admin API already returns,
and ADR-0020 names the thing most likely to go wrong with it:

    it must go through the same authorisation as the JSON API rather than
    beside it.

So this module never decides who a caller is. The ``require_user`` dependency
is built once by :func:`personacore.admin.routes.create_admin_router` from the
configured ``trusted_user_header`` and handed here, and it is attached to the
router itself — every route on this surface, HTML and static asset alike, is
behind the same callable that guards ``/admin/api/...``. There is deliberately
no second check to drift out of step with the first.

*Who* a caller is and *what they may reach* are different questions, and the
second one is also answered once, on the same router (ADR-0032). This is an
administration interface, so it is **admin-only by default**: a household
member reaches Chat, their own profile, and the assets those two are drawn
with — :data:`MEMBER_PATHS` and :data:`MEMBER_PREFIXES` are the whole list.
Everything else needs an admin without any screen having to remember to ask,
and a screen added later is admin-only until somebody deliberately opens it.

Everything rendered comes from the same builder functions the API handlers call
(``build_system_health``, ``build_trace_page``, the plugin scan cache), for the
same reason ADR-0007 gave for building the API first: two code paths that
answer "what is the state of this system" differently is one code path too
many.

**Templates are the specification.** ``web/templates/`` arrived from Claude
Design as the approved design; visual drift from it is a defect (CLAUDE.md).
Where a control in the design has no backend yet, it is disabled and marked
``later`` rather than removed — the shape of the finished product is part of
what the design is for.

**One screen deliberately departs from it.** The design drew Chat as "one
message, one reply — a diagnostic, not a conversation"; the owner asked for a
conversation after seeing it. The exchange the design drew is kept exactly as
drawn and becomes the unit that repeats. That deviation is CLAUDE.md's "written
up and approved" kind, not drift — the reasoning is under *Chat* below, and it
is the only place this module knowingly differs from the canvas.

Static assets are served from ``/admin/static/...`` by this application, never
from a CDN (ADR-0020). The path sits under ``/admin`` rather than at the root so
the admin UI does not claim a top-level path something else may want later.

**One screen per file.** Every page and fragment lives in ``ui/screens/``, one
module per screen, and each registers its routes on the router this module
builds by calling ``register(router, ctx)`` with the bundle in
:class:`~personacore.web.shared.UIContext`. They are registered on *this*
router rather than included as routers of their own, deliberately: the
dependency above is attached here, and a screen that built its own router would
be a second place for the guard to be forgotten.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.templating import Jinja2Templates

from personacore import BUILD_COMMIT, BUILD_DATE, __version__
from personacore.admin.authn import AuthContext, require_admin
from personacore.admin.models import (
    AdminUser,
    ConfigResponse,
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
    DEFAULT_PACKAGE_LIMITS,
    PackageLimits,
)
from personacore.preferences import PreferenceStore
from personacore.web.auth_pages import create_account_router
from personacore.web.screens import chat as chat_screen
from personacore.web.screens import chat_attachments as chat_attachments_screen
from personacore.web.screens import chat_audio as chat_audio_screen
from personacore.web.screens import core as core_screen
from personacore.web.screens import health as health_screen
from personacore.web.screens import hearing as hearing_screen
from personacore.web.screens import keys as keys_screen
from personacore.web.screens import logs as logs_screen
from personacore.web.screens import models as models_screen
from personacore.web.screens import persona_delete as persona_delete_screen
from personacore.web.screens import persona_edit as persona_edit_screen
from personacore.web.screens import personas as personas_screen
from personacore.web.screens import plugin_detail as plugin_detail_screen
from personacore.web.screens import plugin_entries as plugin_entries_screen
from personacore.web.screens import plugin_install as plugin_install_screen
from personacore.web.screens import plugin_status as plugin_status_screen
from personacore.web.screens import plugins as plugins_screen
from personacore.web.screens import profile as profile_screen
from personacore.web.screens import review as review_screen
from personacore.web.screens import voice as voice_screen
from personacore.web.screens import voice_library as voice_library_screen
from personacore.web.screens.chat import (
    CHAT_HISTORY_MESSAGES,
    CHAT_TRANSCRIPT_WINDOW,
    CHAT_UNAVAILABLE,
    MAX_TOOL_NAMES_SHOWN,
    ChatHistoryMessage,
    chat_exchange,
    conversation_history,
    conversation_start,
)
from personacore.web.screens.core import (
    RETENTION_LABELS,
    auth_method_choices,
    listen_address,
    purge_schedule,
    retention_rows,
)
from personacore.web.screens.core_form import (
    CORE_FORM_FIELDS,
    core_payload,
    split_listen,
)
from personacore.web.screens.health import (
    health_notices,
    health_rows,
)
from personacore.web.screens.key_policy import (
    KIND_CHOICES,
    MEMORY_CHOICES,
    RISK_CHOICES,
    TOOLS_ALL,
    generated_profile_id,
    key_rows,
    policy_refusal,
    profile_from_form,
    profile_summary,
    tool_names,
)
from personacore.web.screens.keys import (
    KEY_ISSUE_TABS,
    KEY_NOTE_REQUIRED,
    KEY_REVOKE_LABEL,
    KEY_REVOKE_TITLE,
    KEYLESS_CONSEQUENCE,
    MAX_KEY_NOTE_CHARS,
    TOOLS_ALL_NOTE,
    key_revoke_body,
)
from personacore.web.screens.logs import (
    LOG_EXCHANGES,
    LOG_RECORD_WINDOW,
    SURFACE_FILTERS,
    log_exchanges,
)
from personacore.web.screens.model_listing import (
    MODEL_FETCH_EMPTY,
    MODEL_FETCH_HOST_ERROR,
    MODEL_FETCH_KEY_UNREADABLE,
    MODEL_FETCH_NEEDS_KEY,
    MODEL_FETCH_NO_ADDRESS,
    MODEL_FETCH_NO_LISTING,
    MODEL_FETCH_NOT_A_LISTING,
    MODEL_FETCH_UNAVAILABLE,
    MODEL_FETCH_UNREACHABLE,
    MODEL_NOT_ADVERTISED,
    model_choices,
    model_fetch_summary,
)
from personacore.web.screens.models import (
    INTERACTIVE_NOT_CLEARABLE,
    LLM_ROLE_LABELS,
    MODEL_ADDRESS_REQUIRED,
    MODEL_ADDRESS_REQUIRED_OPTIONAL,
    connection_differs,
    model_roles,
)
from personacore.web.screens.persona_delete import (
    PERSONA_DELETE_LABEL,
    PERSONA_DELETE_TITLE,
    persona_delete_body,
)
from personacore.web.screens.persona_edit import (
    MAX_PROMPT_CHARS,
    PERSONA_EXISTS,
    PERSONA_IDENTIFIER_FIXED,
    PERSONA_NAME_REQUIRED,
    PERSONA_NAME_UNUSABLE,
    PERSONA_PROMPT_REQUIRED,
    PERSONA_PROMPT_TOO_LONG,
)
from personacore.web.screens.personas import (
    GENERIC_MODEL_ID,
    MAX_PERSONA_NAME_CHARS,
    PERSONA_GENERIC_NOTE,
    PERSONA_INSTALL_LATER,
    PERSONA_MODEL_FIELD_NOTE,
    PERSONA_MODEL_SELECTION_LATER,
    PERSONA_VOICE_EDIT_NOTE,
    PROMPT_EXCERPT_CHARS,
    persona_bindings,
    persona_rows,
    persona_slug,
    persona_voice_label,
    prompt_excerpt,
)
from personacore.web.screens.plugin_common import (
    MAX_INDEX_DIGITS,
    PLUGIN_SCREEN,
)
from personacore.web.screens.plugin_detail import (
    saved_message,
    unchanged_message,
)
from personacore.web.screens.plugin_install import (
    INSTALL_NEXT_STEPS,
    INSTALL_NO_FILE,
    INSTALL_UNREADABLE,
    MULTIPART_SLACK_BYTES,
)
from personacore.web.screens.plugins import (
    ROW_REFUSALS,
    UNINSTALL_BODY,
    UNINSTALL_TITLE,
    plugin_rows,
)
from personacore.web.screens.review import (
    CONVERSATIONS_SHOWN,
    HIDDEN_UNAVAILABLE,
    MESSAGES_SHOWN,
    NO_ACCOUNTS,
    NOBODY_PICKED,
    NOTHING_TO_SHOW,
    REVIEW_PATH,
    conversation_rows,
    message_rows,
    retention_days,
    store_reports_hidden,
)
from personacore.web.shared import (
    _STATE_WORD,
    API_HANDLERS,
    MAX_ROUTER_DEPTH,
    MENU_COLLAPSED_PREFERENCE,
    NO_KEY_OPERATIONS,
    NO_PERSONA_OPERATIONS,
    NO_PLUGIN_OPERATIONS,
    PLUGIN_API_HANDLERS,
    RETENTION_DEFAULT_KEY,
    SHORT_COMMIT_LENGTH,
    UIContext,
    _dev_admin_user,
    api_handler,
    build_label,
    settings_problems,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see the factory
    from personacore.admin.routes import _PluginScanCache

TEMPLATE_DIR = Path(__file__).parent / "templates"
"""The approved design (ADR-0020) — every page and fragment the UI renders."""

STATIC_DIR = Path(__file__).resolve().parent / "static"
"""``web/static/`` — vendored htmx, the one stylesheet, and admin.js."""

STATIC_PREFIX = "/admin/static"

STATIC_FILES: dict[str, str] = {
    "nocturne.css": "text/css; charset=utf-8",
    "htmx.min.js": "text/javascript; charset=utf-8",
    "admin.js": "text/javascript; charset=utf-8",
    # The chat screen's own script — the optimistic echo, the scroll rule and
    # the microphone. A file of its own rather than more of admin.js because it
    # is loaded by one page and nothing else on this surface has any use for a
    # speech recogniser.
    "chat.js": "text/javascript; charset=utf-8",
}
"""The exact filenames this surface serves, and what each one is.

An allowlist rather than a directory mount: the name arrives in the URL path,
and a lookup that can only ever succeed for one of a few literal keys cannot be
walked out of the directory however the request is spelled. It also keeps the
README and the licence file beside them from being served as assets.
"""


# ---------------------------------------------------------------------------
# Who this surface is for (ADR-0032)
# ---------------------------------------------------------------------------

MEMBER_PATHS: frozenset[str] = frozenset(
    {
        # `/admin/` redirects to Chat, and it is the address every document and
        # every bookmark has. A member who typed it would otherwise be refused
        # on the way to the one screen they are allowed.
        "/admin/",
        "/admin/chat",
        "/admin/profile",
        # Registered on the *public* auth router rather than this one, so this
        # entry gates nothing today. It is written down because a household
        # member must always be able to end their own session, and if that
        # route is ever moved onto this router the default below would take it
        # away silently. Naming it costs one line and removes that trapdoor.
        "/admin/sign-out",
    }
)
"""Exactly the paths a non-admin household member may reach, matched whole."""

MEMBER_PREFIXES: tuple[str, ...] = (
    # Everything the Chat screen is made of: the message list, the reply's
    # audio, sending a turn, choosing who answers, deleting their own thread.
    "/admin/chat/",
    # Their own profile, and whatever else is added under it. A prefix rather
    # than a list of endpoints on purpose: this path is "settings that belong
    # to the person signed in", so a control added there is a member's control
    # by construction and does not need a second decision made about it here.
    "/admin/profile/",
    # The stylesheet, htmx and the chat script. Not a screen — the parts every
    # screen is drawn with. A member allowed onto Chat but refused its
    # stylesheet and its htmx has a page that neither looks right nor sends,
    # which is the "gate that refuses everybody" in a subtler costume. Serving
    # them is safe: `STATIC_FILES` is itself an allowlist of four vendored
    # front-end files, and none of them is data about the household.
    "/admin/static/",
)
"""Path prefixes a member may reach, each including everything beneath it."""


def member_may_reach(path: str) -> bool:
    """Is ``path`` on the household member's side of the admin surface?

    Whole-path or whole-prefix, never a substring: ``/admin/chatter`` is not
    ``/admin/chat``, and a screen named to look like one of these does not
    inherit its permission.
    """
    return path in MEMBER_PATHS or path.startswith(MEMBER_PREFIXES)


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


def create_admin_ui_router(
    *,
    require_user: Callable[[Request], AdminUser],
    layout: AppdataLayout,
    audit: AuditGateway,
    llm: LLMHealthSource,
    bus: EventBusSource,
    scans: _PluginScanCache,
    disk_warning_bytes: int,
    save_config: Callable[..., Awaitable[ConfigResponse]],
    personas: PersonaStore,
    preferences: PreferenceStore,
    chat: ChatRunner | None = None,
    plugin_health: PluginHealthSource | None = None,
    auth_context: AuthContext | None = None,
    package_limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
) -> APIRouter:
    """Build the designed admin UI's router (ADR-0020).

    Args:
        require_user: **The JSON API's own authentication dependency**, built by
            :func:`personacore.admin.routes.create_admin_router` from the
            configured ``trusted_user_header`` and passed in rather than
            rebuilt. It is attached to this router, so it runs before every
            route here — pages, fragments and static assets. Nothing on this
            surface decides identity for itself.
        layout: Appdata paths, for the health dashboard's disk and audit rows.
        audit: The audit/transcript store, read for the log view.
        llm: Anything with ``health_check()`` — the roster in production.
        bus: Anything exposing ``.health``.
        scans: The router's plugin scan cache, shared so a reload done through
            the API is immediately what this surface shows.
        disk_warning_bytes: Appdata free-space floor for the dashboard.
        save_config: **The JSON API's own persist-and-apply helper**, passed in
            for the same reason ``require_user`` is: ``PUT
            /admin/api/config`` and the two settings screens must validate,
            write, apply live and audit through one path or "saved" means two
            different things depending on which control was clicked. It raises
            :class:`~fastapi.HTTPException` carrying the API's plain-English
            problems, which :func:`settings_problems` renders — this surface
            never validates a setting itself.
        personas: **The core's own persona store**, the same object the agent
            loop reads a persona out of on every turn, passed rather than rebuilt
            for the reason everything else here is: a second store would be a
            second cache, and an edit saved on this screen would take effect for
            one of them. The Personas screen uses it for three things and nothing
            else — listing what is on disk, reading a prompt back, and resolving
            a name to a directory before anything is opened
            (``resolve_dir``, which is the path check, not this module's
            slugger). Changing the *default* still goes through the JSON API's
            ``select_persona``, because that writes config and audits.
        chat: The core's own :class:`~personacore.admin.protocols.ChatRunner`
            — the same runner every other caller gets, so a turn taken here uses
            the persona, the policy and the audit trail the core would use
            anywhere. ``None`` means this core cannot run a turn at all, and the
            screen says so rather than offering a box that cannot work.
        plugin_health: **The same supervisor view the JSON API is given**, not a
            second one, for the reason everything else on this router is passed
            in rather than rebuilt. Two screens read it: the plugin health page
            (PC-280) takes its state word from ``status_for``, because that is
            where the supervisor's five states have already been translated into
            the four operator-facing words the system dashboard uses — deriving
            them again here would be a second vocabulary for the same facts. The
            plugin output page (PC-279) takes what a plugin printed from
            ``output_for``, discovered with ``getattr`` exactly as the scan
            cache discovers ``reload``: a source that only reports status is
            still a valid source, and a core without one says so on the page
            instead of showing an empty box.
        package_limits: **The same ceilings the JSON API installs under**, passed
            for the same reason everything else here is. The cap is enforced by
            the installer on the real bytes; this surface holds the number only
            so an upload that is obviously over it can be refused before
            Starlette spools it to a temporary file (see
            :data:`MULTIPART_SLACK_BYTES`).

    Returns:
        A router mounted under ``/admin``.
    """
    # Imported inside the factory: routes.py builds this router, so a top-level
    # import back into it would be a cycle.
    from personacore.admin.routes import build_system_health

    def admin_unless_the_members_own(request: Request) -> None:
        """Default-deny for this whole surface (ADR-0032).

        A household member account, on v0.7.1, was shown the entire admin
        interface — every screen, every switch. The narrow fix
        would have been ``require_admin`` in fourteen screen modules, and the
        fifteenth screen written next month would have shipped open, because
        nothing about adding a page makes anybody think about this.

        So the decision is made once, here, on the router that already carries
        ``require_user``: a member reaches the short list above and nothing
        else. A new screen is admin-only until somebody deliberately puts it on
        that list, which is the correct default for a surface whose whole
        purpose is administering the household.

        ``require_user`` is called again rather than declared as a parameter,
        the same way :func:`_shell` calls it: it is the one place identity is
        decided (``admin/authn.py``) and it has already run as the router
        dependency ahead of this one, so this is a second read of a decision,
        not a second decision.

        The path is compared as the router matched it. If it ever arrived
        spelled differently — a mount prefix, an encoding — the comparison
        fails to find an allowlist entry and the caller is asked for an admin.
        Wrong in that direction is a member sent to an admin; wrong in the
        other direction is v0.7.1.
        """
        if member_may_reach(request.url.path):
            return
        require_admin(require_user(request))

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    router = APIRouter(
        prefix="/admin",
        tags=["admin-ui"],
        # The OpenAPI document describes the JSON contract other tooling uses
        # (ADR-0020). HTML pages are not part of it.
        include_in_schema=False,
        # Order matters and is the readable one: who are you, then may you.
        # Both are router-level so they cover pages, fragments, static assets
        # and the account router included below alike — there is nowhere on
        # this surface for a route to be added in front of them.
        dependencies=[Depends(require_user), Depends(admin_unless_the_members_own)],
    )
    asset_cache: dict[str, bytes] = {}

    async def _health() -> tuple[SystemHealth, PluginListing]:
        listing = await scans.current()
        health = await build_system_health(
            layout=layout,
            llm=llm,
            bus=bus,
            audit=audit,
            listing=listing,
            disk_warning_bytes=disk_warning_bytes,
        )
        return health, listing

    def _here(request: Request) -> str:
        """The address this page is at, for a toggle to come back to.

        Empty on anything but a GET — see the note beside ``here`` below. The
        value is checked again when it is submitted (``safe_admin_next``); this
        is where it is *produced*, and producing it is not what makes it safe.
        """
        if request.method != "GET":
            return ""
        query = request.url.query
        return f"{request.url.path}?{query}" if query else request.url.path

    async def _shell(request: Request, active: str) -> dict[str, Any]:
        """The context ``base.html`` needs on every page.

        The sidebar carries a health dot and a Security warning dot, so every
        page render costs one health sweep. That is the design's choice and it
        is the honest implementation of it: the alternative is a nav dot fed
        from a cached guess, which would say "fine" while something was down.
        """
        health, _ = await _health()
        user = require_user(request)
        return {
            "nav_active": active,
            "health_state": _STATE_WORD[health.state],
            "security_alert": bool(_dev_admin_user(request)),
            # Who is signed in, on every page (PC-283's other half: an
            # interface that authenticates but never says who you are leaves
            # somebody administering a household as the wrong person). Read
            # from `require_user`, never from a form or a cookie this template
            # could be handed.
            "signed_in_as": user.id,
            # Whether the nav should offer admin-only screens at all (Core
            # settings). Same call as `signed_in_as` above, not a second
            # lookup — an admin flag re-derived from a form or cookie would be
            # exactly the identity hole this shell already refuses to open.
            "is_admin": user.is_admin,
            # Whether there is a session to end. False under the proxy and
            # bypass doors, where a Sign out button could not sign anybody out.
            "can_sign_out": auth_context is not None and auth_context.decision.uses_builtin,
            # Which core is running, on every page: `base.html` renders the
            # footer, so the shell is the only place these belong. Read from
            # the module globals rather than captured at import so a build
            # value can be exercised without rebuilding the package.
            "version": __version__,
            "build": build_label(BUILD_COMMIT, BUILD_DATE),
            # Whether this person folded the menu down to icons. Read here so
            # it is in the markup the browser receives: `hx-boost` replaces the
            # body on every navigation, and a menu narrowed by a script after
            # paint would open and snap shut each time. Defaults to expanded —
            # somebody who has never pressed it sees every name.
            "nav_collapsed": bool(
                preferences.get_bool(user.door, user.id, MENU_COLLAPSED_PREFERENCE)
            ),
            # Where the toggle sends the browser back to. Only a GET page can
            # name itself: a page rendered in answer to a POST has a URL that
            # only answers POST, and redirecting there is a 405. `safe_admin_next`
            # falls back to the Referer and then to Chat, and refuses anything
            # that is not a path on this surface — or that is one and still
            # answers no GET, which is how the 405 arrived anyway: the Referer
            # was the persona picker's own POST-only address.
            "here": _here(request),
        }

    # -- static assets -----------------------------------------------------

    @router.get("/static/{filename}", summary="Vendored admin UI asset")
    async def static_asset(filename: str) -> Response:
        """Serve one of the three vendored front-end files (ADR-0020).

        Behind the same authentication as every page, because a request that is
        not allowed to see the admin UI is not allowed to see the admin UI's
        parts either — and because "authenticated everywhere except here" is
        the shape of gap this surface exists to avoid.
        """
        media_type = STATIC_FILES.get(filename)
        if media_type is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such admin UI asset.")
        body = asset_cache.get(filename)
        if body is None:
            body = (STATIC_DIR / filename).read_bytes()
            asset_cache[filename] = body
        return Response(
            content=body,
            media_type=media_type,
            # Revalidate rather than cache: these files change on a deliberate
            # upgrade commit, and a stale htmx served out of a browser cache
            # after one is a bug nobody would think to look for.
            headers={"Cache-Control": "no-cache"},
        )

    ctx = UIContext(
        templates=templates,
        require_user=require_user,
        layout=layout,
        audit=audit,
        llm=llm,
        bus=bus,
        scans=scans,
        disk_warning_bytes=disk_warning_bytes,
        save_config=save_config,
        personas=personas,
        preferences=preferences,
        chat=chat,
        plugin_health=plugin_health,
        auth_context=auth_context,
        package_limits=package_limits,
        health=_health,
        shell=_shell,
    )

    # -- the screens -------------------------------------------------------
    #
    # Each one adds its routes to the router above, in the order they are
    # listed. Nothing here re-checks who the caller is: `require_user` is on
    # the router, so it runs in front of every route any of these add.
    chat_screen.register(router, ctx)
    # The reply's audio (PC-256). Registered beside the Chat screen and
    # deliberately not inside it: it answers with a media file rather than a
    # fragment, and the screen is redrawn with the design while this socket
    # under it is not.
    chat_audio_screen.register(router, ctx)
    # An attachment's bytes (attachments.md contract §6) — the same reason
    # the audio socket above is separate from the screen: this answers with a
    # file, never a fragment, and is under `/admin/chat/` so `MEMBER_PREFIXES`
    # already covers it.
    chat_attachments_screen.register(router, ctx)
    health_screen.register(router, ctx)
    plugins_screen.register(router, ctx)
    plugin_install_screen.register(router, ctx)
    plugin_status_screen.register(router, ctx)
    plugin_detail_screen.register(router, ctx)
    plugin_entries_screen.register(router, ctx)
    models_screen.register(router, ctx)
    core_screen.register(router, ctx)
    personas_screen.register(router, ctx)
    persona_edit_screen.register(router, ctx)
    persona_delete_screen.register(router, ctx)
    keys_screen.register(router, ctx)
    logs_screen.register(router, ctx)
    # Per-user conversation review (the chat-room contract, section 7.2).
    # Registered like every other screen and deliberately absent from
    # `MEMBER_PATHS`, so the default-deny above is the whole of its
    # authorisation: it shows one person's private conversations to another
    # person, and a check written inside the screen would be a second
    # answer to a question this router has already answered.
    review_screen.register(router, ctx)
    profile_screen.register(router, ctx)
    # Voice: the engines and their switches (ADR-0029 section 2), and the
    # voices themselves — installing one, filling it in, exporting it as a
    # pack. Registered as two screens because they are two errands: "which
    # engines run" and "what can this assistant sound like".
    voice_screen.register(router, ctx)
    voice_library_screen.register(router, ctx)
    # Hearing: the recognisers and their switches, the listening mirror of
    # the engines screen above. Registered beside it rather than inside it
    # because they are two errands with two registries behind them.
    hearing_screen.register(router, ctx)

    # -- accounts and sessions (PC-288, PC-289, PC-290) --------------------
    #
    # Included into *this* router, so `require_user` above is already attached
    # to every route it carries. There is no second check on those pages, and
    # there is nowhere for one to be added without it being obvious.
    # On the store existing, not on the door being open right now: adding an
    # account is how an operator prepares to switch to the built-in door, so a
    # screen that only appeared once that door was open would be behind the
    # thing it is needed for.
    if auth_context is not None:
        router.include_router(
            create_account_router(
                templates=templates,
                context=auth_context,
                require_user=require_user,
                shell=_shell,
            )
        )

    return router



__all__ = [
    "store_reports_hidden",
    "retention_days",
    "message_rows",
    "conversation_rows",
    "REVIEW_PATH",
    "NO_ACCOUNTS",
    "NOTHING_TO_SHOW",
    "NOBODY_PICKED",
    "MESSAGES_SHOWN",
    "HIDDEN_UNAVAILABLE",
    "CONVERSATIONS_SHOWN",
    "API_HANDLERS",
    "CHAT_HISTORY_MESSAGES",
    "CHAT_TRANSCRIPT_WINDOW",
    "CHAT_UNAVAILABLE",
    "CORE_FORM_FIELDS",
    "GENERIC_MODEL_ID",
    "KEYLESS_CONSEQUENCE",
    "KEY_ISSUE_TABS",
    "KEY_NOTE_REQUIRED",
    "KEY_REVOKE_LABEL",
    "KEY_REVOKE_TITLE",
    "KIND_CHOICES",
    "INTERACTIVE_NOT_CLEARABLE",
    "LLM_ROLE_LABELS",
    "LOG_EXCHANGES",
    "LOG_RECORD_WINDOW",
    "MODEL_ADDRESS_REQUIRED",
    "MODEL_ADDRESS_REQUIRED_OPTIONAL",
    "MODEL_FETCH_EMPTY",
    "MODEL_FETCH_HOST_ERROR",
    "MODEL_FETCH_KEY_UNREADABLE",
    "MODEL_FETCH_NEEDS_KEY",
    "MODEL_FETCH_NOT_A_LISTING",
    "MODEL_FETCH_NO_ADDRESS",
    "MODEL_FETCH_NO_LISTING",
    "MODEL_FETCH_UNAVAILABLE",
    "MODEL_FETCH_UNREACHABLE",
    "MAX_INDEX_DIGITS",
    "MAX_TOOL_NAMES_SHOWN",
    "MAX_KEY_NOTE_CHARS",
    "MAX_PERSONA_NAME_CHARS",
    "MAX_PROMPT_CHARS",
    "MEMBER_PATHS",
    "MEMBER_PREFIXES",
    "MEMORY_CHOICES",
    "MAX_ROUTER_DEPTH",
    "INSTALL_NEXT_STEPS",
    "INSTALL_NO_FILE",
    "INSTALL_UNREADABLE",
    "MULTIPART_SLACK_BYTES",
    "MODEL_NOT_ADVERTISED",
    "NO_KEY_OPERATIONS",
    "NO_PERSONA_OPERATIONS",
    "NO_PLUGIN_OPERATIONS",
    "PERSONA_DELETE_LABEL",
    "PERSONA_DELETE_TITLE",
    "PERSONA_EXISTS",
    "PERSONA_GENERIC_NOTE",
    "PERSONA_IDENTIFIER_FIXED",
    "PERSONA_INSTALL_LATER",
    "PERSONA_MODEL_FIELD_NOTE",
    "PERSONA_MODEL_SELECTION_LATER",
    "PERSONA_NAME_REQUIRED",
    "PERSONA_NAME_UNUSABLE",
    "PERSONA_PROMPT_REQUIRED",
    "PERSONA_PROMPT_TOO_LONG",
    "PERSONA_VOICE_EDIT_NOTE",
    "PROMPT_EXCERPT_CHARS",
    "RISK_CHOICES",
    "ROW_REFUSALS",
    "TOOLS_ALL",
    "TOOLS_ALL_NOTE",
    "PLUGIN_API_HANDLERS",
    "PLUGIN_SCREEN",
    "RETENTION_DEFAULT_KEY",
    "RETENTION_LABELS",
    "STATIC_FILES",
    "STATIC_PREFIX",
    "SURFACE_FILTERS",
    "TEMPLATE_DIR",
    "SHORT_COMMIT_LENGTH",
    "UNINSTALL_BODY",
    "UNINSTALL_TITLE",
    "ChatHistoryMessage",
    "api_handler",
    "auth_method_choices",
    "build_label",
    "chat_exchange",
    "connection_differs",
    "generated_profile_id",
    "conversation_history",
    "conversation_start",
    "core_payload",
    "create_admin_ui_router",
    "health_notices",
    "health_rows",
    "key_revoke_body",
    "key_rows",
    "listen_address",
    "log_exchanges",
    "member_may_reach",
    "model_choices",
    "model_fetch_summary",
    "model_roles",
    "persona_bindings",
    "persona_delete_body",
    "persona_rows",
    "persona_slug",
    "persona_voice_label",
    "policy_refusal",
    "profile_from_form",
    "profile_summary",
    "prompt_excerpt",
    "plugin_rows",
    "purge_schedule",
    "retention_rows",
    "saved_message",
    "settings_problems",
    "split_listen",
    "tool_names",
    "unchanged_message",
]
