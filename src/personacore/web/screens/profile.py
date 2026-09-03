"""Your profile — everything that belongs to you rather than to the core.

ADR-0030. Deliberately a different screen from ``/admin/account``, which is the
admin's view of everybody else. This one is yours, and it exists on all three
identity doors: preferences key on the door and ``AdminUser.id`` together,
which the core's own sign-in, a trusted proxy header and the development bypass
all produce.

Three concerns, in the order a person meets them:

* **Your settings.** One today (autoplay). The page is the mechanism, not the
  setting — whatever lands next reads the same way.
* **Your password.** Changing your own credential, which existed nowhere: the
  only password boxes in the interface were for creating somebody *else's*
  account.
* **Your signed-in devices** (PC-288), and ending them all at once.

The last two moved here from ``/admin/account`` because that screen is
admin-only: everything on it is about other people, and a member who could not
reach it lost the one personal control the application had.

**Every route here lives under ``/admin/profile``.** The admin surface is gated
by a default-deny allowlist that permits this prefix and everything beneath it,
so a personal control registered anywhere else would be refused for exactly the
members who need it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from personacore.admin.authn import (
    SESSIONS_ENDED_ACTION,
    AuthContext,
    client_address,
)
from personacore.admin.models import AdminUser
from personacore.audit.models import AuditOutcome
from personacore.auth.accounts import AccountRejected
from personacore.auth.passwords import MIN_PASSWORD_CHARS, PasswordRejected
from personacore.auth.sessions import SESSION_COOKIE
from personacore.auth.throttle import LOCKED_OUT
from personacore.preferences import Override
from personacore.web.auth_pages import (
    CURRENT_PASSWORD_WRONG,
    PASSWORD_BOXES_REQUIRED,
    PASSWORDS_DIFFER,
    SIGN_IN_PATH,
)
from personacore.web.shared import (
    MENU_COLLAPSED_PREFERENCE,
    UIContext,
    safe_admin_next,
    wants_collapsed,
)

PROFILE_PATH = "/admin/profile"
"""Where the page lives, for links and redirects. The router this screen
registers on already carries the ``/admin`` prefix, so the routes below are
declared relative to it — see :data:`PROFILE_ROUTE`."""

PROFILE_ROUTE = "/profile"

PASSWORD_PATH = "/admin/profile/password"  # noqa: S105 - a URL, not a credential
PASSWORD_ROUTE = "/profile/password"  # noqa: S105 - a URL, not a credential

SESSIONS_END_PATH = "/admin/profile/sessions/end"
SESSIONS_END_ROUTE = "/profile/sessions/end"

MENU_PATH = "/admin/profile/menu"
MENU_ROUTE = "/profile/menu"
"""The sidebar's collapse toggle.

**Under ``/admin/profile/`` because that prefix is a member's** (ADR-0032's
allowlist): the menu belongs to whoever is looking at it, and a household
member who could not fold it would be refused their own furniture. It is also
where it belongs by subject — this is a setting about you, like the one above
it, and it is on every page because the sidebar is.
"""

AUTOPLAY_PREFERENCE = "speech.autoplay"
"""The setting's name in the preference table. Namespaced from the first one so
the table never has to be migrated to make room for a second subsystem."""

AUTOPLAY_DEFAULT = True
"""What a new person gets before they have chosen anything.

On, because the requirement is that a reply speaks itself for somebody who
has never opened this page: a household assistant that stays silent until each
person finds a checkbox is not the product.
"""

SESSION_LIFETIME_NOTE = (
    "A session lasts seven days, then expires — sign in again after "
    "that. Ending sessions signs out every device at once, including "
    "this one."
)
"""PC-288's note, moved here word for word from the account screen. The
sentence about ending *every* device is the whole difference between this
control and the password change below it, which deliberately keeps this one."""

PASSWORD_KEEPS_THIS_DEVICE = (
    "Your other signed-in devices are signed out. This one stays."  # noqa: S105 - a sentence, not a credential
)
"""What changing your password does besides changing your password.

Kept because no label on the form says it, and the effect is otherwise
invisible until somebody picks up a phone that has silently signed out.
"""


def builtin_auth(ctx: UIContext) -> AuthContext | None:
    """The accounts and sessions behind the door that is open **right now**.

    ``None`` under a login proxy or the development bypass, where there is no
    credential here to change and no session here to end.

    **Asked per request, never captured when the routes are registered.**
    ``[auth] method`` swaps the door live: ``LiveAuth.adopt`` rebinds
    :attr:`AuthContext.decision` on the save, and nothing is re-mounted. An
    answer read once at register time is therefore an answer about the door the
    container booted with, and it was wrong in both directions — booted on
    ``builtin`` and swapped to ``proxy`` it left a *Change your password* form
    on the page that could only ever refuse, because ``users.verify`` has no
    account for a proxy identity; booted on ``proxy`` and swapped to
    ``builtin`` it hid that form and the device list until a restart, under the
    very door they belong to.

    The store itself is not asked whether it exists per request — that never
    changes. ``personacore.server`` builds one under every door precisely so a
    swap moves the door and not the accounts.
    """
    auth = ctx.auth_context
    if auth is None or not auth.decision.uses_builtin:
        return None
    return auth


def override_from(request: Request) -> Override:
    """What the administrator has decided for everybody, right now.

    Read from ``app.state.settings`` on each request rather than captured at
    router-build time, because saving core settings replaces that object — a
    captured copy would keep answering with the value from container start.
    """
    settings = getattr(request.app.state, "settings", None)
    playback = getattr(settings, "playback", None)
    raw = getattr(playback, "autoplay", Override.UNSET.value)
    try:
        return Override(raw)
    except ValueError:
        # An unreadable value is not an instruction. Fall back to letting
        # people choose rather than silently forcing the household either way.
        return Override.UNSET


SAVE_ACTION = "profile.update"
"""The audit action name for a change to your own settings.

Spec §7 wants "every admin change — timestamped, per-user, viewable in the UI,
included in backups", and the global override already gets one through
``_save_config``. The per-person half had none, so a setting turned off was a
change nobody could attribute. Today that is "did my reply speak"; on the
settings ADR-0030 names as inheritors it is "the child lock was turned off at
23:40 and nobody can tell by whom".
"""

PASSWORD_ACTION = "profile.password"  # noqa: S105 - an audit action name, not a credential
"""The audit action name for changing your own password.

**The record says that a password changed and nothing about the password.** Not
the old value, not the new one, not a hash of either, and not a length or any
other measurement of one — a refusal that recorded "too short" would be a
length oracle written into the log every admin can read. The refusals carry a
short reason code from a fixed set instead, none of which is derived from what
was typed.
"""


def _form_text(form: Any, field: str) -> str:
    """One submitted field as text, or ``""``.

    A local copy of the helper the sign-in pages use: a form value can be an
    upload rather than a string, and treating one as text is how a handler ends
    up hashing a file object.
    """
    value = form.get(field) if hasattr(form, "get") else None
    return value if isinstance(value, str) else ""


def _end_other_sessions(auth: AuthContext, request: Request, user_id: str) -> int:
    """End every session for this person except the one asking. Returns how many.

    The one place the "except this one" rule lives. ``end_all_for`` would take
    the current session with it, which is right for the *End all my sessions*
    button and wrong here: somebody who has just proved they know their
    password should not be thrown out by their own action.
    """
    current = auth.sessions.session_id_for_token(auth.token(request))
    ended = 0
    for record in auth.sessions.for_user(user_id):
        if record.session_id != current and auth.sessions.end(record.session_id):
            ended += 1
    return ended


def _password_changed_note(ended: int) -> str:
    """What the page says after a successful change. Facts, no reassurance."""
    if ended == 1:
        return "Your password is changed. One other signed-in device was signed out."
    if ended:
        return f"Your password is changed. {ended} other signed-in devices were signed out."
    return "Your password is changed."


def register(router: APIRouter, ctx: UIContext) -> None:
    # Imported inside `register`, as the other screens that audit do: the
    # factory in `personacore.admin.routes` imports this module, so a
    # module-level import would close the cycle.
    from personacore.admin.routes import _record_change

    # Whether this assembly has accounts and sessions at all — a bare router in
    # a test may not. That is the only thing decided here: *which door is open*
    # is asked per request by `builtin_auth`, because it can change while the
    # core is running.
    store: AuthContext | None = ctx.auth_context

    async def _context(
        request: Request, *, error: str | None = None, notice: str | None = None
    ) -> dict[str, Any]:
        user = ctx.require_user(request)
        override = override_from(request)
        # The door as it is for *this* request, so the two cards below appear
        # and disappear with it rather than with the boot.
        auth = builtin_auth(ctx)
        return {
            **await ctx.shell(request, "profile"),
            "me": user,
            "autoplay": ctx.preferences.resolve_bool(
                user.door,
                user.id,
                AUTOPLAY_PREFERENCE,
                override=override,
                default=AUTOPLAY_DEFAULT,
            ),
            # The template needs to know the control is not the person's to
            # change, and which way it was set — a disabled switch with no
            # reason reads as a bug.
            "autoplay_locked": override is not Override.UNSET,
            # `None` rather than an empty list where this core has no accounts
            # of its own: an empty list would render as "you are signed in
            # nowhere", which is a false statement rather than an absent
            # section.
            "sessions": auth.session_views(request, user.id) if auth else None,
            "session_lifetime_note": SESSION_LIFETIME_NOTE,
            "can_change_password": auth is not None,
            "min_password_chars": MIN_PASSWORD_CHARS,
            "password_keeps_this_device": PASSWORD_KEEPS_THIS_DEVICE,
            "error": error,
            "notice": notice,
        }

    async def _page(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        return ctx.templates.TemplateResponse(
            request=request,
            name="profile.html",
            status_code=status_code,
            context=await _context(request, error=error, notice=notice),
        )

    @router.get(PROFILE_ROUTE, response_class=HTMLResponse, summary="Your profile")
    async def profile_page(request: Request) -> HTMLResponse:
        return await _page(request)

    @router.post(PROFILE_ROUTE, summary="Save your profile")
    async def profile_save(request: Request) -> Response:
        """Save the one setting, unless an administrator has taken it away.

        The lock is enforced here and not only in the template. A disabled
        input submits nothing, but a disabled input is a statement to a
        browser, not to the server, and this endpoint is reachable without one.

        Both outcomes are audited. A refusal is recorded rather than dropped
        because the template renders no Save button at all while the setting is
        locked, so a post that arrives anyway was never a browser following the
        page — and spec §7 asks for "every confirmation given or refused" in
        the same breath as every admin change.
        """
        user = ctx.require_user(request)
        override = override_from(request)
        form = await request.form()
        wanted = str(form.get("autoplay") or "").lower() in {"on", "true", "1", "yes"}
        allowed = override is Override.UNSET
        if allowed:
            # A SQLite INSERT and a commit, and a WAL commit fsyncs. On the
            # event loop that is the whole core — `/health` and every in-flight
            # chat turn — stalled once per request, at a rate an authenticated
            # caller chooses. Offloaded like every other SQLite write here.
            # The matching *read* above is a dict lookup and deliberately is
            # not (ADR-0030).
            await asyncio.to_thread(
                ctx.preferences.set_bool,
                user.door,
                user.id,
                AUTOPLAY_PREFERENCE,
                wanted,
            )
        await _record_change(
            ctx.audit,
            user,
            action=SAVE_ACTION,
            outcome=AuditOutcome.SUCCESS if allowed else AuditOutcome.FAILURE,
            detail={
                "setting": AUTOPLAY_PREFERENCE,
                "value": wanted,
                # Which door this identity came through — the same word
                # `/health` and the sign-in records use. The id alone does not
                # name a person (see `AdminUser.door`), so a record carrying
                # only the id would be ambiguous between the three.
                "method": user.door.value,
                # Why a refusal was a refusal, and "unset" on the successes, so
                # the household rule at the moment of the change is in the
                # record rather than inferred from core.toml months later.
                "override": override.value,
            },
        )
        return RedirectResponse(PROFILE_PATH, status_code=status.HTTP_303_SEE_OTHER)

    @router.post(MENU_ROUTE, summary="Fold the menu down to icons, or open it")
    async def set_menu(request: Request) -> Response:
        """Save whether the sidebar shows names, and go back to the page.

        A form and a redirect, which is the whole of the no-JavaScript story.
        With scripting it is the same form, boosted like every other one here,
        so htmx posts it and swaps the body the redirect answers with — the
        control does not change, only how the page is fetched.

        **Not audited, deliberately.** Spec §7 asks for every admin change; the
        width of somebody's menu is not one. It changes nothing about the
        household, nothing another person can see, and a record per press would
        bury the changes that do matter under a stream of ones that do not.

        No administrator override, for the same reason there is no audit line:
        there is nothing here for an administrator to have an opinion about.
        """
        user = ctx.require_user(request)
        form = await request.form()
        await asyncio.to_thread(
            ctx.preferences.set_bool,
            user.door,
            user.id,
            MENU_COLLAPSED_PREFERENCE,
            wants_collapsed(form.get("collapsed")),
        )
        return RedirectResponse(
            safe_admin_next(request, form.get("next")),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if store is None:
        return

    async def _refuse(
        request: Request,
        user: AdminUser,
        *,
        message: str,
        reason: str,
        status_code: int,
    ) -> HTMLResponse:
        """Say no, on the page, and write the refusal down.

        The message is the one shown to whoever typed it; ``reason`` is the
        fixed code that goes in the audit record. They are two different
        strings on purpose — the sentence can be reworded without changing what
        a log reader is matching on, and the code can never accidentally start
        quoting the input.
        """
        await _record_change(
            ctx.audit,
            user,
            action=PASSWORD_ACTION,
            outcome=AuditOutcome.REFUSED,
            detail={"method": user.door.value, "reason": reason},
        )
        return await _page(request, error=message, status_code=status_code)

    @router.post(
        PASSWORD_ROUTE, response_class=HTMLResponse, summary="Change your password"
    )
    async def change_my_password(request: Request) -> HTMLResponse:
        """Change your own password — the current one, then the new one twice.

        **The current password is required.** A signed-in session is not
        authorisation to replace the credential that session was created with:
        the ordinary case this defends against is a browser somebody left
        unlocked, where the attacker already has the session and would
        otherwise be one click away from owning the account outright.

        Verified through ``UserStore.verify``, which is the same call the
        sign-in door makes — a second way to check a password is a second
        answer to whether one is right. It runs on the event loop rather than
        in a worker thread for the same reason ``AuthContext.sign_in`` does:
        one scrypt run is a deliberate ~50 ms, and two call sites treating the
        same cost differently is how they drift.

        The attempt is counted by the *sign-in* throttle, under the same
        ``(name, address)`` key. Anything else would leave a signed-in caller
        an unlimited, unthrottled oracle for the password the sign-in page
        rate-limits — and, on a CPU-only core, an unlimited scrypt generator.

        **Mounted on the store existing, refused per request under another
        door** — the arrangement the sign-in pages use, for the same reason:
        the door swaps live, so this route has to start and stop answering with
        it. Under ``proxy`` or the bypass there is no account behind the
        identity, so verifying anything against it would count a failure
        against a name that has no password here.
        """
        # Before the form is read and before the throttle is touched: a
        # password must not be taken by a door that is not open. 404 with the
        # sentence naming both ways back, exactly as the sign-in pages refuse.
        store.require_builtin()
        auth = store
        user = ctx.require_user(request)
        address = client_address(request)
        form = await request.form()
        current = _form_text(form, "current_password")
        new_password = _form_text(form, "new_password")
        confirm = _form_text(form, "confirm_password")

        waiting = auth.throttle.retry_after(user.id, address)
        if waiting:
            return await _refuse(
                request,
                user,
                message=LOCKED_OUT.format(seconds=waiting),
                reason="locked_out",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if not current or not new_password or not confirm:
            return await _refuse(
                request,
                user,
                message=PASSWORD_BOXES_REQUIRED,
                reason="missing_fields",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if new_password != confirm:
            # Checked before anything is verified or stored, for the reason
            # first-run setup checks it there: the one thing worse than an
            # unchanged password is one that is a typo nobody can reproduce.
            return await _refuse(
                request,
                user,
                message=PASSWORDS_DIFFER,
                reason="passwords_differ",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if auth.users.verify(user.id, current) is None:
            auth.throttle.record_failure(user.id, address)
            return await _refuse(
                request,
                user,
                message=CURRENT_PASSWORD_WRONG,
                reason="wrong_current_password",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        try:
            changed = auth.users.set_password(user.id, new_password)
        except (PasswordRejected, AccountRejected) as exc:
            # The store owns the wording (spec §9's plain English is already
            # written in `check_password_quality`), so it is shown rather than
            # reworded. The *record* gets the code and not the sentence: the
            # sentence says which rule was broken, and which rule was broken is
            # a fact about the password.
            return await _refuse(
                request,
                user,
                message=str(exc),
                reason="rejected_by_policy",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        if not changed:  # pragma: no cover - the account went while we held it
            return await _refuse(
                request,
                user,
                message=(
                    "That account is no longer here, so its password cannot be "
                    "changed. Sign out and back in."
                ),
                reason="no_account",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # Every other session goes. A password change is what somebody does
        # when they think another person has their account, and leaving that
        # person signed in on the device they took is theatre. This session
        # survives so the change does not throw out the person who made it.
        ended = _end_other_sessions(auth, request, user.id)
        auth.throttle.clear(user.id, address)
        await _record_change(
            ctx.audit,
            user,
            action=PASSWORD_ACTION,
            outcome=AuditOutcome.SUCCESS,
            detail={"method": user.door.value, "sessions_ended": ended},
        )
        return await _page(request, notice=_password_changed_note(ended))

    @router.post(SESSIONS_END_ROUTE, summary="End all your sessions")
    async def end_my_sessions(request: Request) -> Response:
        """PC-288, moved from ``/admin/account``. Ends this one too, so the
        browser is bounced to sign-in.

        Refused per request under another door, like the password route above:
        there are no sessions of ours behind a proxy identity, and the sign-in
        page this redirects to does not exist there either.
        """
        store.require_builtin()
        auth = store
        user = ctx.require_user(request)
        ended = auth.sessions.end_all_for(user.id)
        await auth.record_admin_action(
            request,
            action=SESSIONS_ENDED_ACTION,
            actor=user.id,
            detail={"target": user.id, "ended": ended, "self": True},
        )
        response = RedirectResponse(SIGN_IN_PATH, status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response


__all__ = [
    "AUTOPLAY_DEFAULT",
    "AUTOPLAY_PREFERENCE",
    "MENU_PATH",
    "MENU_ROUTE",
    "PASSWORD_ACTION",
    "PASSWORD_KEEPS_THIS_DEVICE",
    "PASSWORD_PATH",
    "PASSWORD_ROUTE",
    "PROFILE_PATH",
    "PROFILE_ROUTE",
    "SAVE_ACTION",
    "SESSIONS_END_PATH",
    "SESSIONS_END_ROUTE",
    "SESSION_LIFETIME_NOTE",
    "builtin_auth",
    "override_from",
    "register",
]
