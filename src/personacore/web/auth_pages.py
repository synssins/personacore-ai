"""The sign-in, setup and account screens — PC-283, PC-286 to PC-291.

Two routers, and the split *is* the security boundary:

* :func:`create_public_auth_router` holds the three pages nobody can be signed
  in for — the sign-in page, the first-run setup page and sign-out. They are
  the only unauthenticated routes on the whole admin surface, they answer only
  while the built-in door is the one open, and they are listed in one place so
  "what can be reached without signing in" is a question with a short, readable
  answer.
* :func:`create_account_router` holds the pages you must be signed in for. It
  is mounted *inside* the designed UI's router, so it inherits that router's
  ``require_user`` — the one seam (see :mod:`personacore.admin.authn`) — rather
  than carrying a check of its own.

The JSON API for all of this lives in :mod:`personacore.admin.routes` and does
exactly the same things through exactly the same
:class:`~personacore.admin.authn.AuthContext`. These handlers are a second
*presentation*, never a second implementation: every one of them calls the same
``sign_in`` / ``first_account`` / ``end_all_for`` the API calls.

The two public pages deliberately do not extend ``base.html``. That template
draws the whole navigation, which is a map of the admin surface — and handing a
map of it to somebody who has not signed in is a free reconnaissance. They get
their own minimal shell and one stylesheet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from personacore.admin.authn import (
    NO_TLS_WARNING,
    SESSIONS_ENDED_ACTION,
    SETUP_DONE,
    SIGN_IN_UNAVAILABLE,
    USER_CREATED_ACTION,
    USER_MINOR_ACTION,
    AuthContext,
    SignInRefused,
    SignInRequired,
    require_admin,
)
from personacore.admin.models import AdminUser
from personacore.auth.accounts import AccountRejected, normalise_name
from personacore.auth.passwords import MIN_PASSWORD_CHARS, PasswordRejected
from personacore.auth.sessions import SESSION_COOKIE

SIGN_IN_PATH = "/admin/sign-in"
SETUP_PATH = "/admin/setup"
ACCOUNT_PATH = "/admin/account"
USERS_PATH = "/admin/users"
AFTER_SIGN_IN = "/admin/chat"
"""Where a successful sign-in lands. The same page ``/admin/`` redirects to, so
signing in and opening the interface arrive in the same place."""

PUBLIC_STYLESHEET_PATH = "/admin/sign-in.css"
"""The sign-in page's stylesheet, served without authentication.

The designed UI serves its assets from behind ``require_user`` on the grounds
that "a request that is not allowed to see the admin UI is not allowed to see
the admin UI's parts either" (ADR-0020). A sign-in page cannot follow that rule
and still be legible, so exactly one file is served publicly: the same
``nocturne.css`` every other page uses. It is our own stylesheet, it contains no
data and no structure of the interface behind it, and it is one named file
rather than a directory — nothing else can be reached through this route.
"""

SETUP_INTRO = (
    "Nobody has an account yet. Create the first one — it's an admin, and the "
    "one you'll use to add others. There's no default password; the one you "
    "set here is it."
)

SIGN_OUT_MESSAGE = "Signed out."
MINOR_NOTE = (
    "Marking somebody a minor changes nothing about what their account can do. "
    "It marks them for review."
)
"""The one line beside the minor switch.

The rule this file's neighbours keep — say what the control does, and stop —
applies here twice over, because the obvious reading of a flag on an account is
that it restricts something. It does not, so the label says so once.
"""

PASSWORDS_DIFFER = "Those two passwords don't match — type the new one twice."
NAME_AND_PASSWORD_REQUIRED = "Fill in both a name and a password."  # noqa: S105 - a prompt, not a credential

PASSWORD_BOXES_REQUIRED = (
    "Fill in all three boxes: the password you use now, and the new one twice."  # noqa: S105 - a prompt, not a credential
)
"""Said when the change-your-password form arrives with a box empty.

Lives here beside the other two because this is where the words for the
password forms are kept, and one file having the vocabulary is what stops two
screens refusing the same thing in two different sentences. Used by
:mod:`personacore.web.screens.profile`, which owns the form itself.
"""

CURRENT_PASSWORD_WRONG = (
    "That is not your current password. Type the one you sign in with now."  # noqa: S105 - a prompt, not a credential
)
"""Said when the current password on the change form does not match.

It may name the reason, unlike :data:`~personacore.admin.authn.BAD_CREDENTIALS`
next door, because there is no account to disclose: whoever is reading it is
already signed in as the account in question.
"""


def _form_text(form: Any, field: str) -> str:
    value = form.get(field) if hasattr(form, "get") else None
    return value if isinstance(value, str) else ""


def _form_switch(form: Any, field: str) -> bool:
    """One submitted checkbox as a boolean.

    A browser sends nothing at all for an unticked box, so absence is False.
    Written once because there are now two of these on the same form and two
    spellings of "is this on" would eventually disagree.
    """
    return str(form.get(field) or "").lower() in {"on", "true", "1", "yes"}


def _refusal_text(exc: HTTPException) -> str:
    """The plain-English sentence out of an API-shaped error body."""
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), str):
        return detail["error"]
    return str(detail)


def create_public_auth_router(
    *,
    templates: Jinja2Templates,
    context: AuthContext,
    stylesheet: Path,
) -> APIRouter:
    """The pages reachable without being signed in.

    Mounted whenever this core has accounts of its own, and **live**: each page
    asks :meth:`~personacore.admin.authn.AuthContext.require_builtin` whether
    the built-in door is the one open right now, so switching to it on the Core
    settings screen makes these pages answer without a restart, and switching
    away makes them stop. Under another door they are 404 with the sentence
    naming both ways back — which is exactly what they said before, by not
    existing.
    """

    router = APIRouter(tags=["admin-ui"], include_in_schema=False)

    def _page(
        request: Request,
        name: str,
        *,
        error: str | None = None,
        username: str = "",
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=name,
            status_code=status_code,
            context={
                "error": error,
                "username": username,
                "stylesheet": PUBLIC_STYLESHEET_PATH,
                "sign_in_path": SIGN_IN_PATH,
                "setup_path": SETUP_PATH,
                "setup_intro": SETUP_INTRO,
                "min_password_chars": MIN_PASSWORD_CHARS,
                # PC-286, on the page and only when it is true of this
                # connection. A permanent disclaimer is one people stop
                # reading; one that appears exactly when the password really is
                # about to cross the network in the clear is one they notice.
                "tls_warning": None if context.is_secure(request) else NO_TLS_WARNING,
            },
        )

    # Read once, at build time: the sign-in page is the one page an
    # unauthenticated caller can fetch, so it does no filesystem work per
    # request and cannot be used to make the core touch disk by hammering it.
    stylesheet_body = stylesheet.read_bytes()

    @router.get(PUBLIC_STYLESHEET_PATH, summary="Sign-in page stylesheet")
    async def sign_in_stylesheet() -> Response:
        return Response(
            content=stylesheet_body,
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get(SIGN_IN_PATH, response_class=HTMLResponse, summary="Sign in")
    async def sign_in_page(request: Request) -> Response:
        """The sign-in form, or the setup page while there is nobody to sign in
        as (PC-291). One redirect rather than a form that can only ever fail."""
        context.require_builtin()
        if context.setup_required():
            return RedirectResponse(SETUP_PATH, status_code=status.HTTP_303_SEE_OTHER)
        return _page(request, "sign_in.html")

    @router.post(SIGN_IN_PATH, response_class=HTMLResponse, summary="Sign in")
    async def sign_in_submit(request: Request) -> Response:
        # Before the form is even read: a password must not be taken by a door
        # that is not open. `context.sign_in` refuses again for the same reason
        # the API handler relies on it -- one check here for the empty-box path
        # that never reaches it, one there for everything that does.
        context.require_builtin()
        form = await request.form()
        username = _form_text(form, "username").strip()
        password = _form_text(form, "password")
        if not username or not password:
            return _page(
                request,
                "sign_in.html",
                error=NAME_AND_PASSWORD_REQUIRED,
                username=username,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token, _record = await context.sign_in(request, username, password)
        except SignInRefused as exc:
            # The name is echoed back so a mistyped password does not cost the
            # name too; the password never is. Nothing here writes it anywhere.
            return _page(
                request,
                "sign_in.html",
                error=_refusal_text(exc),
                username=username,
                status_code=exc.status_code,
            )
        response = RedirectResponse(AFTER_SIGN_IN, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(SESSION_COOKIE, token, **context.cookie_kwargs(request))
        return response

    @router.get(SETUP_PATH, response_class=HTMLResponse, summary="First-run setup")
    async def setup_page(request: Request) -> Response:
        """PC-291's first run. Closes itself the moment an account exists."""
        context.require_builtin()
        if not context.setup_required():
            return RedirectResponse(SIGN_IN_PATH, status_code=status.HTTP_303_SEE_OTHER)
        return _page(request, "setup.html")

    @router.post(SETUP_PATH, response_class=HTMLResponse, summary="First-run setup")
    async def setup_submit(request: Request) -> Response:
        context.require_builtin()
        if not context.setup_required():
            return _page(
                request,
                "sign_in.html",
                error=SETUP_DONE,
                status_code=status.HTTP_409_CONFLICT,
            )
        form = await request.form()
        username = _form_text(form, "username").strip()
        password = _form_text(form, "password")
        confirm = _form_text(form, "confirm_password")
        if not username or not password:
            return _page(
                request,
                "setup.html",
                error=NAME_AND_PASSWORD_REQUIRED,
                username=username,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if password != confirm:
            # Checked before anything is stored, because the one thing worse
            # than no account is an admin account whose password is a typo.
            return _page(
                request,
                "setup.html",
                error=PASSWORDS_DIFFER,
                username=username,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token, _record = await context.first_account(request, username, password)
        except HTTPException as exc:
            return _page(
                request,
                "setup.html",
                error=_refusal_text(exc),
                username=username,
                status_code=exc.status_code,
            )
        response = RedirectResponse(AFTER_SIGN_IN, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(SESSION_COOKIE, token, **context.cookie_kwargs(request))
        return response

    @router.post("/admin/sign-out", summary="Sign out")
    async def sign_out(request: Request) -> Response:
        """Ends this session and clears the cookie.

        Unauthenticated on purpose: a browser holding a token whose session has
        already been ended must still be able to put the cookie down.
        """
        await context.sign_out(request)
        response = RedirectResponse(SIGN_IN_PATH, status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router


def create_account_router(
    *,
    templates: Jinja2Templates,
    context: AuthContext,
    require_user: Callable[[Request], AdminUser],
    shell: Callable[[Request, str], Awaitable[dict[str, Any]]],
) -> APIRouter:
    """The accounts screen: everybody else, for an admin.

    Nothing personal is on it. "Your signed-in devices", the *End all my
    sessions* button and changing your own password all live on
    ``/admin/profile`` instead — see
    :mod:`personacore.web.screens.profile` — because this screen and its
    siblings under Security are admin-only, and a member has to be able to
    reach their own devices and their own password.

    Mounted inside the designed UI's router, so ``require_user`` is already
    attached to every route here by the parent. It is passed in as well because
    the handlers need the *identity*, not merely the guarantee that there is
    one — the same arrangement every other page on that router uses.
    """

    router = APIRouter(tags=["admin-ui"], include_in_schema=False)

    async def _account_context(
        request: Request, *, error: str | None = None, notice: str | None = None
    ) -> dict[str, Any]:
        user = require_user(request)
        # PC-290: your own account, always; the list of accounts, only if you
        # are an admin. `users` is None rather than empty for a non-admin, so
        # the template cannot render "no other users" at somebody who simply is
        # not allowed to know.
        users = context.user_views() if user.is_admin else None
        return {
            **await shell(request, "users"),
            "me": user,
            "users": users,
            "min_password_chars": MIN_PASSWORD_CHARS,
            "minor_note": MINOR_NOTE,
            "error": error,
            "notice": notice,
        }

    async def _account_page(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="account.html",
            status_code=status_code,
            context=await _account_context(request, error=error, notice=notice),
        )

    @router.get("/account", response_class=HTMLResponse, summary="Your account")
    async def account_page(request: Request) -> HTMLResponse:
        """Everybody else (PC-289, PC-290).

        Your own devices and your own password are **not** here: they are the
        two things on the admin surface that belong to the person rather than
        to the household, and they moved to ``/admin/profile`` when this screen
        became admin-only. A personal control on an admin-only page is a
        control the members who need it cannot reach.
        """
        return await _account_page(request)

    @router.post("/users", response_class=HTMLResponse, summary="Add an account")
    async def create_user(request: Request) -> HTMLResponse:
        """Admin only (PC-290), and the password is typed here rather than
        defaulted (PC-291)."""
        actor = require_admin(require_user(request))
        form = await request.form()
        username = _form_text(form, "username").strip()
        password = _form_text(form, "password")
        confirm = _form_text(form, "confirm_password")
        if not username or not password:
            return await _account_page(
                request,
                error=NAME_AND_PASSWORD_REQUIRED,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if password != confirm:
            return await _account_page(
                request,
                error=PASSWORDS_DIFFER,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        is_admin = _form_switch(form, "is_admin")
        is_minor = _form_switch(form, "is_minor")
        try:
            record = context.users.create(
                username, password, is_admin=is_admin, is_minor=is_minor
            )
        except (AccountRejected, PasswordRejected) as exc:
            return await _account_page(
                request,
                error=str(exc),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        await context.record_admin_action(
            request,
            action=USER_CREATED_ACTION,
            actor=actor.id,
            detail={
                "target": record.name,
                "is_admin": record.is_admin,
                "is_minor": record.is_minor,
            },
        )
        return await _account_page(
            request,
            notice=(
                f"Added '{record.name}'"
                + (" as an admin." if record.is_admin else ".")
                + " Tell them the password — it can't be shown again."
            ),
        )

    @router.post(
        "/users/{username}/minor",
        response_class=HTMLResponse,
        summary="Mark an account a minor, or clear it",
    )
    async def set_user_minor(request: Request, username: str) -> HTMLResponse:
        """Set or clear the review flag on somebody else's account.

        Admin only, like everything else on this screen. **It is not a
        permission**, so there is no last-admin rule and no confirmation
        dialog: nothing is destroyed and nothing is taken away, and the switch
        undoes itself by being clicked again.

        The submitted state is the state wanted, not a toggle of whatever is on
        disk. A toggle read from the page is a decision made from a copy that
        may be minutes old, and two admins on two tabs would flip it past each
        other.
        """
        actor = require_admin(require_user(request))
        try:
            target = normalise_name(username)
        except AccountRejected as exc:
            return await _account_page(
                request, error=str(exc), status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
            )
        form = await request.form()
        wanted = _form_switch(form, "is_minor")
        if not context.users.set_minor(target, wanted):
            return await _account_page(
                request,
                error=f"There is no account called '{target}' here.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        await context.record_admin_action(
            request,
            action=USER_MINOR_ACTION,
            actor=actor.id,
            detail={"target": target, "is_minor": wanted},
        )
        return await _account_page(
            request,
            notice=(
                f"'{target}' is marked as a minor."
                if wanted
                else f"'{target}' is no longer marked as a minor."
            ),
        )

    @router.post(
        "/users/{username}/sessions/end",
        response_class=HTMLResponse,
        summary="End all sessions for an account",
    )
    async def end_user_sessions(request: Request, username: str) -> HTMLResponse:
        """PC-289 — an admin revoking access without waiting for an expiry."""
        actor = require_admin(require_user(request))
        try:
            target = normalise_name(username)
        except AccountRejected as exc:
            return await _account_page(
                request, error=str(exc), status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
            )
        if context.users.get(target) is None:
            return await _account_page(
                request,
                error=f"There is no account called '{target}' here.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        ended = context.sessions.end_all_for(target)
        await context.record_admin_action(
            request,
            action=SESSIONS_ENDED_ACTION,
            actor=actor.id,
            detail={"target": target, "ended": ended, "self": target == actor.id},
        )
        if target == actor.id:
            # Including your own, which is a legitimate thing for an admin to
            # do to themselves — and it ends the session reading this page.
            response = RedirectResponse(SIGN_IN_PATH, status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response  # type: ignore[return-value]
        return await _account_page(
            request,
            notice=(
                f"Ended {ended} session(s) for '{target}'. Anything signed in as that "
                "account will need the password again."
            ),
        )

    return router


def redirect_when_not_signed_in(
    require_user: Callable[[Request], AdminUser],
    *,
    context: AuthContext,
) -> Callable[[Request], AdminUser]:
    """Wrap the one seam so a browser gets a sign-in page, not a 401 body.

    **This is not a second check.** It calls the same ``require_user`` the JSON
    API is guarded by and re-raises the same refusal in the shape a browser can
    act on. Only :class:`~personacore.admin.authn.SignInRequired` is converted —
    "you are signed in but this is admin-only" (403) stays a 403, because
    bouncing a signed-in person to a sign-in page tells them the wrong thing
    about why they were refused.

    A caller with no accounts yet is sent to setup rather than to sign-in, so a
    fresh container's first page is the one that can actually be completed
    (PC-291).
    """

    def dependency(request: Request) -> AdminUser:
        try:
            return require_user(request)
        except SignInRequired as exc:
            if not context.decision.uses_builtin:
                # No sign-in page exists under the other doors, so a redirect
                # would land the browser on a 404 and replace a refusal that
                # names the way out with one that names nothing. Asked per
                # request because the door can change while the core runs.
                raise
            target = SETUP_PATH if context.setup_required() else SIGN_IN_PATH
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                detail=exc.detail,
                # `HX-Redirect` as well as `Location`: an htmx request that gets
                # a redirect swaps the sign-in page into a fragment slot unless
                # it is told to navigate instead.
                headers={"Location": target, "HX-Redirect": target},
            ) from exc

    return dependency


__all__ = [
    "ACCOUNT_PATH",
    "AFTER_SIGN_IN",
    "CURRENT_PASSWORD_WRONG",
    "MINOR_NOTE",
    "NAME_AND_PASSWORD_REQUIRED",
    "PASSWORDS_DIFFER",
    "PASSWORD_BOXES_REQUIRED",
    "PUBLIC_STYLESHEET_PATH",
    "SETUP_INTRO",
    "SETUP_PATH",
    "SIGN_IN_PATH",
    "SIGN_IN_UNAVAILABLE",
    "SIGN_OUT_MESSAGE",
    "USERS_PATH",
    "create_account_router",
    "create_public_auth_router",
    "redirect_when_not_signed_in",
]
