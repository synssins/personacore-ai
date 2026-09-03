"""Who this request is, whose sessions those are, and the core's own sign-in.

PC-283 to PC-294, split out of :mod:`personacore.admin.routes` (ADR-0040).

**This module registers on two routers and the difference is the security
boundary, not tidiness.** :func:`register` puts everything on the guarded
router, where ADR-0032's default-deny applies. :func:`register_public` puts
three routes — sign in, sign out, create the first account — on an unguarded
one, because nobody can be signed in while signing in. Those three are the only
unauthenticated JSON on the admin surface. Each is throttled and audited, each
asks ``AuthContext.require_builtin`` per request, and none returns the session
token in a body: it goes back in a ``Set-Cookie`` and nowhere else.

If a route ever moves between those two functions, it changes who can reach it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from personacore.admin.api_shared import AdminApiContext, _fail
from personacore.admin.authn import (
    SESSIONS_ENDED_ACTION,
    USER_CREATED_ACTION,
    require_admin,
)
from personacore.admin.models import (
    CreateUserRequest,
    SessionListing,
    SessionsEnded,
    SetupRequest,
    SignedIn,
    SignInRequest,
    UserListing,
    UserView,
    WhoAmI,
)
from personacore.audit import get_logger
from personacore.auth.accounts import AccountRejected, normalise_name
from personacore.auth.passwords import PasswordRejected
from personacore.auth.sessions import SESSION_COOKIE

logger = get_logger(__name__)


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register the identity and account routes on the **guarded** router."""
    api = router
    require_user = ctx.require_user
    live_auth = ctx.live_auth
    auth_context = ctx.auth_context

    # -- the core's own sign-in (PC-283 to PC-291) --------------------------
    #
    # Two routers, and the split is the security boundary rather than tidiness:
    # `api` above is guarded by `require_user`, so everything on it is behind
    # the one seam; the sign-in and setup endpoints cannot be, because nobody is
    # signed in yet when they are called. They are therefore the only
    # unauthenticated routes on the admin surface, they are mounted only when
    # this core actually has the built-in door open, and each one is its own
    # audited, throttled decision.

    @api.get("/auth/me", response_model=WhoAmI, summary="Who this request is")
    async def who_am_i(request: Request) -> WhoAmI:
        """Whoever the one seam says is calling, and by which door.

        Present under every door, because "who does this core think I am" is
        the first question asked when a proxy is misconfigured, and an endpoint
        that only exists under one door cannot answer it.
        """
        user = require_user(request)
        return WhoAmI(
            username=user.id,
            is_admin=user.is_admin,
            # Off the holder, so "who does this core think I am" answers for
            # the door that is open now rather than the one it booted with.
            method=live_auth.decision.method.value,
            can_sign_out=live_auth.decision.uses_builtin,
        )

    # Mounted on the store existing, not on the door being open right now: the
    # door can change while the core runs (ADR-0010), and a route that had to be
    # mounted to answer could never come alive. Each of these is behind
    # `require_user` already, and each is meaningless-but-harmless under another
    # door -- no sessions to list, and an account list only an admin sees.
    if auth_context is not None:

        @api.get(
            "/auth/sessions",
            response_model=SessionListing,
            summary="Your own signed-in devices",
        )
        async def list_my_sessions(request: Request) -> SessionListing:
            """PC-288 — **your own**, never anybody else's.

            The user comes from ``require_user``, never from the request, so
            there is no parameter here to point at somebody else's sessions.
            """
            user = require_user(request)
            return SessionListing(
                username=user.id, sessions=auth_context.session_views(request, user.id)
            )

        @api.post(
            "/auth/sessions/end",
            response_model=SessionsEnded,
            summary="End all your sessions",
        )
        async def end_my_sessions(request: Request) -> SessionsEnded:
            """PC-288's control — the one that matters after losing a device.

            Ends **all** of them, including the one that asked. Leaving the
            current session alive would mean a stolen phone stays signed in if
            the thief happens to be the one holding it.

            Refused unless the core's own sign-in is the door that is open, and
            asked **per request** because that door now swaps live (ADR-0034).
            Sessions belong to built-in accounts; under a proxy the id is the
            raw header value, so ending "your" sessions would end the sessions
            of an identically-named local account on a proxy identity's say-so.
            The same refusal the profile screen gives, since ADR-0020 wants one
            behaviour behind two presentations rather than two answers.
            """
            auth_context.require_builtin()
            user = require_user(request)
            ended = auth_context.sessions.end_all_for(user.id)
            await auth_context.record_admin_action(
                request,
                action=SESSIONS_ENDED_ACTION,
                actor=user.id,
                detail={"target": user.id, "ended": ended, "self": True},
            )
            return SessionsEnded(
                username=user.id,
                ended=ended,
                message=(
                    f"Ended {ended} session(s), including this one. Everything signed "
                    "in as you will need the password again."
                ),
            )

        @api.get("/users", response_model=UserListing, summary="Accounts (admin only)")
        async def list_users(request: Request) -> UserListing:
            """PC-290 — **only an admin sees the list**.

            A non-admin gets 403 with a reason. Not an empty list: an empty
            list says there is nobody else in this household, which is a lie
            about other people rather than a refusal about this caller.
            """
            require_admin(require_user(request))
            return UserListing(users=auth_context.user_views())

        @api.post(
            "/users",
            response_model=UserView,
            status_code=status.HTTP_201_CREATED,
            summary="Add an account (admin only)",
        )
        async def create_user(request: Request, body: CreateUserRequest) -> UserView:
            """Only an admin adds accounts, and the password is set here rather
            than defaulted (PC-291: there is no default password anywhere)."""
            actor = require_admin(require_user(request))
            try:
                record = auth_context.users.create(
                    body.username,
                    body.password,
                    is_admin=body.is_admin,
                    is_minor=body.is_minor,
                )
            except (AccountRejected, PasswordRejected) as exc:
                raise _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
            await auth_context.record_admin_action(
                request,
                action=USER_CREATED_ACTION,
                actor=actor.id,
                detail={
                    "target": record.name,
                    "is_admin": record.is_admin,
                    "is_minor": record.is_minor,
                },
            )
            return UserView(
                username=record.name,
                is_admin=record.is_admin,
                is_minor=record.is_minor,
                created_at=record.created_at,
                sessions=0,
            )

        @api.post(
            "/users/{username}/sessions/end",
            response_model=SessionsEnded,
            summary="End all sessions for an account (admin only)",
        )
        async def end_user_sessions(request: Request, username: str) -> SessionsEnded:
            """PC-289 — the other half of PC-288.

            An operator revoking somebody's access must not have to wait for a
            session to expire. Same store call as ending your own, so the two
            cannot come to mean different things.
            """
            actor = require_admin(require_user(request))
            try:
                target = normalise_name(username)
            except AccountRejected as exc:
                raise _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
            if auth_context.users.get(target) is None:
                raise _fail(
                    status.HTTP_404_NOT_FOUND,
                    f"There is no account called '{target}' here.",
                )
            ended = auth_context.sessions.end_all_for(target)
            await auth_context.record_admin_action(
                request,
                action=SESSIONS_ENDED_ACTION,
                actor=actor.id,
                detail={"target": target, "ended": ended, "self": target == actor.id},
            )
            return SessionsEnded(
                username=target,
                ended=ended,
                message=(
                    f"Ended {ended} session(s) for '{target}'. Anything signed in as "
                    "that account will need the password again."
                ),
            )


def register_public(router: APIRouter, ctx: AdminApiContext) -> None:
    """Mount the sign-in routes on an **unguarded** router of their own.

    Read the module docstring before adding anything here. This is the one
    place on the admin surface where a route is reachable without the door.
    """
    auth_context = ctx.auth_context

    if auth_context is not None:
        # **The only unauthenticated routes on the admin surface.** They exist
        # because nobody can be signed in while signing in. Everything about
        # them is deliberately narrow: three paths, answering only while the
        # built-in door is the one open, each one throttled and audited, and
        # none of them returning the session token in a body — it goes back in
        # a ``Set-Cookie`` and nowhere else. `AuthContext.sign_in` and
        # `first_account` both call `require_builtin` per request, so under
        # another door these are 404 rather than a password box.
        public_api = APIRouter(prefix="/admin/api", tags=["admin"])

        @public_api.post(
            "/auth/sign-in", response_model=SignedIn, summary="Sign in to the core"
        )
        async def api_sign_in(
            request: Request, response: Response, body: SignInRequest
        ) -> SignedIn:
            """PC-283 and PC-287 — a session, not HTTP Basic.

            Wrong name and wrong password give the same 401 with the same
            sentence; repeated failures from one address give 429 with a
            ``Retry-After`` (see
            :class:`personacore.auth.throttle.SignInThrottle`).
            """
            token, record = await auth_context.sign_in(request, body.username, body.password)
            session = auth_context.sessions.for_user(record.name)[0]
            response.set_cookie(
                SESSION_COOKIE, token, **auth_context.cookie_kwargs(request)
            )
            return SignedIn(
                username=record.name,
                is_admin=record.is_admin,
                expires_at=session.expires_at,
            )

        @public_api.post("/auth/sign-out", summary="End this session")
        async def api_sign_out(request: Request, response: Response) -> dict[str, bool]:
            """Ends the session this request carries and clears the cookie.

            Unauthenticated on purpose: a token whose session has already been
            ended, or one that never existed, must still be able to clear the
            cookie rather than leaving a browser holding a credential it cannot
            get rid of.
            """
            ended = await auth_context.sign_out(request)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return {"signed_out": ended}

        @public_api.post(
            "/auth/setup",
            response_model=SignedIn,
            status_code=status.HTTP_201_CREATED,
            summary="Create the first account",
        )
        async def api_setup(
            request: Request, response: Response, body: SetupRequest
        ) -> SignedIn:
            """PC-291 — the first account, which is an admin.

            Answers 409 the instant one account exists, so this is a one-shot
            door rather than a standing one. There is no default password to
            supersede: the core ships without one.
            """
            token, record = await auth_context.first_account(
                request, body.username, body.password
            )
            session = auth_context.sessions.for_user(record.name)[0]
            response.set_cookie(
                SESSION_COOKIE, token, **auth_context.cookie_kwargs(request)
            )
            return SignedIn(
                username=record.name,
                is_admin=record.is_admin,
                expires_at=session.expires_at,
            )

        router.include_router(public_api)


__all__ = [
    "register",
    "register_public",
]
