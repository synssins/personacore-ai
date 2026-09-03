"""The one authorisation seam — PC-283 to PC-294.

Everything on the admin surface, HTML page and JSON endpoint alike, arrives
through the dependency built here. There is deliberately no second check
anywhere: ADR-0020 already named that as the thing most likely to go wrong with
having two presentations of one API, and adding a door for the core's own
sign-in without going through this one would have proved it right.

**Which door is open is decided once**, by
:func:`personacore.auth.method.resolve_auth`, and read here as an
:class:`~personacore.auth.method.AuthDecision`. This module never consults the
environment, never falls back, and never accepts two credentials at once:

* ``bypass`` and ``proxy`` — the identity arrives in the trusted header. The
  guard in ``personacore.server`` has already stripped that header from every
  peer that is not an allowlisted proxy, and, when the bypass is on, has
  overwritten it with the bypass user. So both doors read one header, and the
  header is only ever believed from somewhere it was allowed to come from
  (PC-294's "a trusted-header path accepting an identity header from any
  address").
* ``builtin`` — the identity arrives as a session cookie, and **the header is
  not consulted at all**. Not "consulted and usually absent": not read. Under
  this door a header is not a credential, so no proxy misconfiguration can turn
  it back into one.

The bypass wins over both, on purpose (PC-294). It is the way back into a
container whose admin password is lost, and taking it away the moment the
built-in login is switched on would remove it at exactly the moment it is
needed.

**Which door is open can change while the core is running**, per ADR-0010: the
owner decided that enabling or disabling any feature or service must take
effect live, without a restart. :class:`LiveAuth` is what makes that possible, and
:meth:`LiveAuth.refusal_for` is what makes it safe: the new door is asked
whether it would admit the operator who just clicked Save, using the request
they clicked it with, and a "no" refuses the save instead of installing a door
they cannot open.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, Request, status

from personacore.admin.models import (
    AdminUser,
    ApiError,
    SessionView,
    UserView,
)
from personacore.admin.protocols import ApiKeyGateway, AuditGateway
from personacore.api.keys import ApiKeyRecord
from personacore.audit.logging import get_correlation_id, get_logger
from personacore.audit.models import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    Owner,
    Surface,
)
from personacore.auth.accounts import AccountRejected, UserRecord, UserStore, normalise_name
from personacore.auth.method import (
    BREAK_GLASS_HINT,
    CHANGE_IT_HERE,
    AuthDecision,
    AuthMethod,
    coerce_method,
    label,
)
from personacore.auth.passwords import PasswordRejected
from personacore.auth.sessions import (
    SESSION_COOKIE,
    SessionRecord,
    SessionStore,
    cookie_settings,
    request_is_secure,
)
from personacore.auth.throttle import LOCKED_OUT, SignInThrottle
from personacore.contracts.policy import ADMIN_API_SCOPE

logger = get_logger(__name__)

SIGN_IN_ACTION = "auth.sign_in"
SIGN_OUT_ACTION = "auth.sign_out"
SETUP_ACTION = "auth.setup"
SESSIONS_ENDED_ACTION = "auth.sessions_ended"
USER_CREATED_ACTION = "auth.user_created"
USER_MINOR_ACTION = "auth.user_minor"
"""Marking an account a minor, or clearing it.

Audited like every other change an admin makes to somebody else's account. It
is the one fact on the review screen that a person set rather than the system
recorded, so "who decided this, and when" has to be answerable.
"""

BAD_CREDENTIALS = (
    "That name and password do not match an account here. Check both and try again."
)
"""The **only** thing a failed sign-in says.

One message for "no such account" and for "wrong password" alike. Telling them
apart hands whoever is guessing the household's member list, which is the
privacy failure PC-290 names, arrived at from the sign-in page instead of the
user list."""

NOT_SIGNED_IN = (
    "You are not signed in. Sign in to use the admin interface."
)

NOT_SIGNED_IN_PROXY = (
    "You are not signed in. This core is set to take the signed-in user from a "
    "login proxy in front of it, so open the interface at the proxy's address "
    "rather than connecting to the core directly. If there is no proxy, this "
    f"core can use accounts of its own instead: {CHANGE_IT_HERE}. "
    f"{BREAK_GLASS_HINT}"
)
"""Said under ``proxy`` when no identity arrived.

The last two sentences exist because the first was a dead end: somebody who
chose the proxy door without a proxy in front lands here, is told to use a
proxy they do not have, and is given no way out. A message that names only the
road you are not on is a wall.

And the way out has to include the bypass, because the rest of it is a circle:
the setting that fixes this is on a screen you have to be signed in to reach.
The bypass is the one thing that does not depend on being able to sign in,
which is exactly why it is not a setting."""

HEADER_UNUSABLE = (
    "Your sign-in details did not arrive in a form I can use. Sign out and back in; "
    "if it keeps happening, the login proxy needs looking at."
)

NOT_AN_ADMIN = (
    "That is an admin-only part of the interface. Your account can see and change "
    "its own settings, and nothing that belongs to anybody else."
)

SIGN_IN_UNAVAILABLE = (
    "This core is not using its own sign-in. It is set to take the signed-in user "
    "from a login proxy in front of it, so there is no sign-in page here. To use "
    f"this core's own accounts instead, {CHANGE_IT_HERE}; it will then offer a "
    f"setup page for the first account. {BREAK_GLASS_HINT}"
)
"""Said when somebody asks for the sign-in or setup page under another door.

Same two obligations as :data:`NOT_SIGNED_IN_PROXY`: name the setting that
changes it, and name the bypass, because somebody reading this cannot reach the
screen that holds the setting."""

SETUP_DONE = (
    "This assistant already has an account, so first-run setup is closed. Sign in, "
    "or ask an admin to add an account for you."
)

NO_TLS_WARNING = (
    "This page is not using HTTPS. Your password will cross the network as typed, "
    "readable by anything else on it. That is usually acceptable on a home network "
    "you trust and is not acceptable over the internet - put a proxy with a "
    "certificate in front before opening this up."
)
"""PC-286, said on the page rather than in a document nobody reads.

Shown only when the request did not arrive over HTTPS, so it is a statement
about this connection rather than a permanent disclaimer people learn to skip.
"""

# ---------------------------------------------------------------------------
# Refusing a door swap that would lock the operator out
# ---------------------------------------------------------------------------

NO_ACCOUNT_TO_ADMIT_YOU = (
    "The way in has not been changed. This core has no account of its own yet, so "
    "switching to its own sign-in would leave nobody able to get in. Add an account "
    f"on the Accounts screen first, then choose '{label(AuthMethod.BUILTIN)}' again."
)
"""Refused: ``proxy`` → ``builtin`` with an empty account store.

The guaranteed lockout, and the one a validation error would not have caught:
the setting is a perfectly valid value naming a door with nobody behind it.
"""

NO_ACCOUNTS_HERE = (
    "The way in has not been changed. This core was assembled without accounts of "
    "its own, so its own sign-in has nobody to let in."
)
"""The same refusal for a core built with no :class:`AuthContext` at all —
a bare router in a test, not a container."""

NO_TRUSTED_HEADER_TO_ADMIT_YOU = (
    "The way in has not been changed. This request did not arrive with a trusted "
    "identity header, so a login proxy in front would have nobody to let in and you "
    "would be locked out the moment it took effect. Put the proxy in front, list its "
    "address in PERSONACORE_TRUSTED_PROXY_IPS, and save this again from a page the "
    "proxy served."
)
"""Refused: anything → ``proxy`` from a request the proxy door would not admit.

Checked against **the request in hand**, because that request is the operator
standing in the doorway: if the header a proxy would have set is not on it, the
proxy door does not know who they are.
"""

DOOR_CHANGE_NEEDS_THE_SCREEN = (
    "The way in has not been changed. Choose it under Sign-in on the Core settings "
    "screen: before it swaps the door the core checks that the one you picked will "
    "still let you in, and it cannot check that from here."
)
"""Refused: a save that changed ``[auth] method`` without a request to test.

Fails closed. Everything that can reach the door — the Core settings form and
``PUT /admin/api/config`` — passes its request; a caller that does not is one
the check cannot be run for, and a door swapped without the check is the
lockout this whole module exists to prevent.
"""

BYPASS_NOT_A_TEST = (
    "The development bypass is letting this request in, so neither door was tried. "
    "Unset PERSONACORE_ADMIN_DEV_USER and sign in to find out whether the one you "
    "picked works."
)
"""Said on the Core settings screen while the bypass is on.

Not a refusal — the bypass admits regardless, so no choice made under it can
lock anybody out. It is the other half of that: the swap was safe *and* proved
nothing, and an operator who thinks they have tested the new door is going to
remove the bypass and find out otherwise.
"""


class SignInRequired(HTTPException):
    """Nobody is signed in.

    A distinct type so the HTML surface can turn it into a redirect to the
    sign-in page while the JSON API turns it into a 401 — one check, two
    presentations of its refusal, which is the same arrangement the rest of the
    admin surface already uses.
    """


class SignInRefused(HTTPException):
    """A sign-in attempt was made and rejected. Carries the plain-English
    reason, which is deliberately the same sentence for every cause."""


def _fail(status_code: int, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail=ApiError(error=message, problems=[]).model_dump()
    )


def client_address(request: Request) -> str:
    """Where a request came from, for the audit record and the throttle key.

    The socket peer, never a forwarded-for header. A header would let whoever
    is guessing passwords pick their own throttle bucket and their own line in
    the audit log, which is worse than having neither.
    """
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# The context the sign-in surfaces share
# ---------------------------------------------------------------------------


@dataclass
class AuthContext:
    """Everything the core's own sign-in needs, assembled once.

    Passed to the dependency, to the JSON endpoints and to the HTML pages, so
    all three verify a password, mint a session and write an audit record
    through the same objects. A second ``UserStore`` would be a second answer
    to "does this account exist".

    **Not frozen, and only for one field.** ``decision`` is rebound by
    :class:`LiveAuth` when the operator changes ``[auth] method``, so every
    handler that asks this object which door is open gets the answer for the
    request it is answering rather than the one the process booted with. The
    stores beside it never change: a swap moves the door, not the accounts.
    """

    decision: AuthDecision
    users: UserStore
    sessions: SessionStore
    throttle: SignInThrottle
    audit: AuditGateway | None = None

    # -- state the pages ask about ----------------------------------------

    def setup_required(self) -> bool:
        """Whether first run has not happened yet (PC-291).

        Only meaningful under the built-in door; under the other two there are
        no accounts to create and the setup page does not exist.
        """
        return self.decision.uses_builtin and self.users.count() == 0

    def token(self, request: Request) -> str | None:
        return request.cookies.get(SESSION_COOKIE)

    def require_builtin(self) -> None:
        """Refuse anything that only makes sense under the built-in door.

        Asked per request rather than at mount time, because the door can be
        changed while the process is running: the sign-in surface has to appear
        the moment somebody switches to ``builtin`` and stop answering the
        moment they switch away. A 404 with the sentence naming both ways out,
        which is what these paths said before by not existing at all.

        Raises:
            HTTPException: 404, carrying :data:`SIGN_IN_UNAVAILABLE`.
        """
        if not self.decision.uses_builtin:
            raise _fail(status.HTTP_404_NOT_FOUND, SIGN_IN_UNAVAILABLE)

    # -- sign in -----------------------------------------------------------

    async def sign_in(
        self, request: Request, username: str, password: str
    ) -> tuple[str, UserRecord]:
        """Verify a name and password and mint a session.

        Returns ``(token, record)``. The token is the only copy that will ever
        exist outside the caller's browser.

        Raises:
            HTTPException: The built-in door is not the one that is open, so
                there is nothing here to sign in to (404).
            SignInRefused: Wrong credentials, or locked out. Both carry a
                plain-English reason; neither says which account exists.
        """
        # Here rather than in each caller: the JSON endpoint and the HTML form
        # are two presentations of this one method, and a door check written
        # twice is a door check that can disagree with itself.
        self.require_builtin()
        address = client_address(request)
        try:
            name = normalise_name(username)
        except AccountRejected:
            # An unusable name is still an attempt, and still counts towards
            # the lockout: otherwise "aaaa!", "aaab!", ... is a free way to
            # guess forever. It is bucketed under a fixed key rather than the
            # raw text so the throttle cannot be filled with junk.
            name = "\x00invalid"
        waiting = self.throttle.retry_after(name, address)
        if waiting:
            await self._record(
                request,
                action=SIGN_IN_ACTION,
                outcome=AuditOutcome.REFUSED,
                username=name,
                detail={"reason": "locked_out", "retry_after_seconds": waiting},
            )
            raise SignInRefused(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=ApiError(error=LOCKED_OUT.format(seconds=waiting), problems=[]).model_dump(),
                headers={"Retry-After": str(waiting)},
            )

        record = self.users.verify(name, password) if not name.startswith("\x00") else None
        if record is None:
            locked = self.throttle.record_failure(name, address)
            await self._record(
                request,
                action=SIGN_IN_ACTION,
                outcome=AuditOutcome.REFUSED,
                username=name,
                detail={"reason": "bad_credentials", "locked_out_seconds": locked},
            )
            # Never the password, never the attempted password's length.
            logger.warning("sign_in_refused", user=name, address=address)
            raise SignInRefused(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ApiError(error=BAD_CREDENTIALS, problems=[]).model_dump(),
            )

        self.throttle.clear(name, address)
        token, session = self.sessions.create(record.name)
        await self._record(
            request,
            action=SIGN_IN_ACTION,
            outcome=AuditOutcome.SUCCESS,
            username=record.name,
            detail={"session_id": session.session_id},
        )
        return token, record

    async def sign_out(self, request: Request) -> bool:
        """End the session this request is carrying. Never raises."""
        token = self.token(request)
        session_id = self.sessions.session_id_for_token(token)
        ended = self.sessions.end_by_token(token)
        if ended:
            await self._record(
                request,
                action=SIGN_OUT_ACTION,
                outcome=AuditOutcome.SUCCESS,
                username=self._user_for(request) or "unknown",
                detail={"session_id": session_id},
            )
        return ended

    async def first_account(
        self, request: Request, username: str, password: str
    ) -> tuple[str, UserRecord]:
        """Create the first account and sign in as it — PC-291.

        The first account is an admin, because a system whose only account
        cannot administer it is a system nobody can administer. Refused once
        any account exists, so this is not a permanent open door.

        Raises:
            HTTPException: Setup is closed, or the name/password was refused.
        """
        self.require_builtin()
        if self.users.count() != 0:
            raise _fail(status.HTTP_409_CONFLICT, SETUP_DONE)
        try:
            record = self.users.create(username, password, is_admin=True)
        except (AccountRejected, PasswordRejected) as exc:
            raise _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        token, session = self.sessions.create(record.name)
        await self._record(
            request,
            action=SETUP_ACTION,
            outcome=AuditOutcome.SUCCESS,
            username=record.name,
            detail={"is_admin": True, "session_id": session.session_id},
        )
        logger.info("first_account_created", user=record.name)
        return token, record

    # -- the cookie --------------------------------------------------------

    def cookie_kwargs(self, request: Request) -> dict[str, object]:
        """Flags for this request's ``Set-Cookie`` — see
        :func:`personacore.auth.sessions.cookie_settings`."""
        return cookie_settings(
            secure=request_is_secure(
                request.url.scheme, request.headers.get("x-forwarded-proto")
            )
        )

    def is_secure(self, request: Request) -> bool:
        return request_is_secure(
            request.url.scheme, request.headers.get("x-forwarded-proto")
        )

    # -- listings ----------------------------------------------------------

    def session_views(self, request: Request, username: str) -> list[SessionView]:
        """One user's live sessions, with the current one marked (PC-288)."""
        current = self.sessions.session_id_for_token(self.token(request))
        return [
            SessionView(
                session_id=record.session_id,
                started_at=record.created_at,
                last_seen_at=record.last_seen_at,
                expires_at=record.expires_at,
                current=record.session_id == current,
            )
            for record in self.sessions.for_user(username)
        ]

    def user_views(self) -> list[UserView]:
        """Every account. **Admin-only at the call site** (PC-290)."""
        return [
            UserView(
                username=record.name,
                is_admin=record.is_admin,
                is_minor=record.is_minor,
                created_at=record.created_at,
                sessions=self.sessions.count_for(record.name),
            )
            for record in self.users.records()
        ]

    # -- audit -------------------------------------------------------------

    def _user_for(self, request: Request) -> str | None:
        session = self.sessions.lookup(self.token(request))
        return session.user if session else None

    async def _record(
        self,
        request: Request,
        *,
        action: str,
        outcome: AuditOutcome,
        username: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Write one sign-in record — "who, when, from where, success or
        failure", which is the record that matters after something goes wrong.

        Never raises, for the same reason
        ``personacore.admin.routes._record_change`` does not: an audit store
        that cannot be written must not also take away the sign-in page needed
        to fix it. Never carries a password, and none of its callers is given
        one.
        """
        if self.audit is None:
            return
        owner = (
            Owner.anonymous()
            if username.startswith("\x00") or not username
            else Owner.profile(username)
        )
        body: dict[str, object] = {
            "address": client_address(request),
            "method": self.decision.method.value,
            **(detail or {}),
        }
        try:
            await self.audit.record_audit(
                AuditRecord(
                    correlation_id=get_correlation_id() or uuid4().hex,
                    timestamp=datetime.now(UTC),
                    surface=Surface.ADMIN_UI,
                    owner=owner,
                    category=AuditCategory.ACCESS,
                    action=action,
                    outcome=outcome,
                    detail=body,
                )
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.error("sign_in_audit_write_failed", action=action, error=repr(exc))

    async def record_admin_action(
        self,
        request: Request,
        *,
        action: str,
        actor: str,
        detail: dict[str, object],
    ) -> None:
        """Record an admin acting on somebody else's account (PC-289)."""
        await self._record(
            request,
            action=action,
            outcome=AuditOutcome.SUCCESS,
            username=actor,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# The dependency
# ---------------------------------------------------------------------------


def make_admin_user_dependency(
    header_name: str,
    *,
    auth: AuthDecision | None = None,
    context: AuthContext | None = None,
) -> Callable[[Request], AdminUser]:
    """Build the dependency that identifies the caller — the one seam.

    Args:
        header_name: The header a login proxy sets to the signed-in user.
            Believed only under the ``proxy`` and ``bypass`` doors, and only
            from an allowlisted peer (the guard in ``personacore.server``
            strips it from everywhere else before a route sees it).
        auth: Which door is open. ``None`` means ``proxy``, which is what every
            assembly did before the core had accounts of its own — so an
            existing caller that passes only a header name keeps exactly the
            behaviour it had.
        context: The account and session stores, required for the ``builtin``
            door and unused by the other two.

    It fails **closed** in every branch. A missing header, an unprintable one,
    a missing cookie, an unknown token and an expired session are all "you are
    not signed in", never an anonymous session: a misconfiguration must lock
    the door rather than open it.
    """
    decision = auth or AuthDecision(
        chosen=AuthMethod.PROXY, method=AuthMethod.PROXY, bypass_user=None
    )
    # Which door every identity from this dependency came through, stamped on
    # the `AdminUser` so per-person state can key on it (see `AdminUser.door`).
    #
    # `decision.method` and not `decision.chosen`: `chosen` is what the
    # operator configured *ignoring the bypass*, and is never `bypass` — so it
    # would file the break-glass identity under the proxy's key space, which is
    # precisely the conflation this field exists to prevent. `method` is what
    # is actually in force for the next request, which is what "which door did
    # this person come through" means. It is `builtin` in the branch that
    # returns `from_session` and `bypass` or `proxy` in the one that returns
    # `from_header` — the same split `AuthDecision.uses_builtin` and
    # `AuthDecision.bypassed` express as booleans.
    door = decision.method

    def from_header(request: Request) -> AdminUser:
        raw = request.headers.get(header_name)
        if raw is None:
            raise SignInRequired(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ApiError(error=NOT_SIGNED_IN_PROXY, problems=[]).model_dump(),
            )
        value = raw.strip()
        # Header values are untrusted input (spec section 7) even when the
        # proxy is trusted: they end up in audit records and in HTML, so
        # anything that is not a plain printable identifier is refused rather
        # than sanitised.
        if not value or len(value) > 256 or not value.isprintable():
            raise _fail(status.HTTP_401_UNAUTHORIZED, HEADER_UNUSABLE)
        # An operator who arrived through a proxy, or through the break-glass
        # bypass, is an admin: neither door carries a "which parts of this may
        # you see" answer, and guessing one would be inventing a permission
        # nobody granted. See `AdminUser.is_admin`.
        return AdminUser(id=value, door=door, is_admin=True)

    def from_session(request: Request) -> AdminUser:
        assert context is not None  # noqa: S101 - guarded at build time below
        session = context.sessions.lookup(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise SignInRequired(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ApiError(error=NOT_SIGNED_IN, problems=[]).model_dump(),
            )
        record = context.users.get(session.user)
        if record is None:
            # The account was deleted while a session for it was still live.
            # The session is stale, so it goes, rather than becoming a caller
            # with a name and no permissions.
            context.sessions.end(session.session_id)
            raise SignInRequired(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ApiError(error=NOT_SIGNED_IN, problems=[]).model_dump(),
            )
        return AdminUser(id=record.name, door=door, is_admin=record.is_admin)

    if decision.uses_builtin:
        if context is None:
            raise ValueError(
                "the built-in sign-in needs an AuthContext (accounts and sessions); "
                "this assembly asked for it and supplied neither"
            )
        return from_session
    return from_header


# ---------------------------------------------------------------------------
# The door, held rather than closed over
# ---------------------------------------------------------------------------


class LiveAuth:
    """Which door is open **right now**, asked per request.

    ADR-0010's rule is that a saved setting takes effect now, and ADR-0024
    exempted ``[auth] method`` from it because the dependency guarding the
    admin surface was built once and closed over by every route. This is the
    indirection that removes the exemption — the same shape
    ``personacore.server.LiveLLM`` uses for the LLM client: the routers are
    handed this object once and call :meth:`identify`; saving a new method
    rebinds the dependency *inside* it.

    **Nothing here decides on its own.** The method still comes from
    :func:`personacore.auth.method.resolve_auth` at startup and from
    :func:`~personacore.auth.method.coerce_method` on a save, and the bypass
    still outranks both — :meth:`adopt` recomputes the whole
    :class:`AuthDecision` rather than editing one field of it.

    **Swapping is guarded, and the guard is the point** (:meth:`refusal_for`).
    Changing this setting is the one change that can put the operator making it
    outside the machine, so the new door is asked whether it would admit *this
    caller* before it is installed, and a "no" refuses the save rather than
    applying it. The order is: build the candidate, prove it, then swap.

    **Sessions issued under the built-in door are neither honoured nor
    destroyed by a swap.** They stay in the session store, they stay valid
    while ``builtin`` is the door, and under ``proxy`` or ``bypass`` they are
    inert because :func:`make_admin_user_dependency` does not read the cookie
    at all under those doors — not "reads it and usually rejects it". Two
    reasons for leaving them rather than ending them: a session that a
    different door silently honoured would be exactly PC-294's "two
    authentication methods both accepting a request", and an operator who
    switches to a proxy for ten minutes and back should not have logged out
    the whole household to find out it did not work. The seven-day absolute
    lifetime still applies, and an admin can still end them by hand.
    """

    def __init__(
        self,
        header_name: str,
        *,
        decision: AuthDecision | None = None,
        context: AuthContext | None = None,
    ) -> None:
        self._header_name = header_name
        self._context = context
        # Whether anybody actually told this assembly which door is open. A
        # `None` decision is the legacy default `make_admin_user_dependency`
        # documents -- the trusted header, which is what every assembly did
        # before the core had accounts of its own -- and an assembly that never
        # named a door has no door to swap and nobody who could be locked out
        # of one. So the guard and the swap both stand down for it (see
        # `refusal_for` and `adopt`); without that, a bare router would refuse
        # every settings save, because the default `[auth] method` a document
        # with no `[auth]` section validates to is `builtin` and that reads as
        # a change away from the proxy default it was never told to use.
        self._declared = decision is not None
        self._decision = decision or AuthDecision(
            chosen=AuthMethod.PROXY, method=AuthMethod.PROXY, bypass_user=None
        )
        self._dependency = self._build(self._decision)
        if context is not None:
            context.decision = self._decision

    # -- what is in force --------------------------------------------------

    @property
    def decision(self) -> AuthDecision:
        """The door in force for the next request."""
        return self._decision

    @property
    def context(self) -> AuthContext | None:
        return self._context

    @property
    def header_name(self) -> str:
        return self._header_name

    def identify(self, request: Request) -> AdminUser:
        """The one seam. Every route on the admin surface arrives here.

        A method rather than the dependency itself, so ``Depends`` holds *this*
        object and the function it delegates to can be replaced underneath it.
        """
        return self._dependency(request)

    def _build(self, decision: AuthDecision) -> Callable[[Request], AdminUser]:
        return make_admin_user_dependency(
            self._header_name, auth=decision, context=self._context
        )

    # -- changing it -------------------------------------------------------

    def refusal_for(self, request: Request | None, method_setting: str) -> str | None:
        """Would the door named by ``method_setting`` still let this caller in?

        ``None`` means yes — install it. A string is the plain-English sentence
        saying why not, for the screen and for the API body alike; the caller
        must not write the setting when it gets one. Also ``None`` when this
        assembly was never told which door is open, because then there is no
        door to change (see ``__init__``).

        Called with the request that is *asking* for the change, which is the
        whole design: the person clicking Save is the person who would be
        locked out, and the request they clicked it with is the only honest
        evidence about whether the new door knows who they are.

        Each door is proved the way that door actually admits somebody:

        * **bypass in force** — it admits regardless of the setting, so nothing
          can be locked out and nothing has been tested. Allowed; the screen
          says so with :data:`BYPASS_NOT_A_TEST`.
        * **proxy** — the real dependency is run against this request. The
          identity guard in ``personacore.server`` has already stripped the
          header from any peer that is not an allowlisted proxy, so what the
          request still carries *is* the answer to "would the proxy door know
          who this is".
        * **builtin** — it admits an account holder with a session, so what has
          to exist is an account. Not a session: the operator arriving from the
          proxy door has none and cannot have one, and the sign-in page they
          are sent to next is a door they can open. With no account at all
          there is no such page, only a setup page that a stranger would
          complete.
        """
        if not self._declared:
            return None
        try:
            chosen = coerce_method(method_setting)
        except ValueError:
            # Not this function's refusal to make: `AuthSettings` has already
            # said which methods exist, in its own words, before anything gets
            # here. Nothing is being swapped, so nothing needs guarding.
            return None
        if chosen is self._decision.chosen:
            return None
        if self._decision.bypass_user:
            return None
        if chosen is AuthMethod.BUILTIN:
            if self._context is None:
                return NO_ACCOUNTS_HERE
            return None if self._context.users.count() else NO_ACCOUNT_TO_ADMIT_YOU
        if request is None:
            return DOOR_CHANGE_NEEDS_THE_SCREEN
        candidate = AuthDecision(chosen=chosen, method=chosen, bypass_user=None)
        try:
            self._build(candidate)(request)
        except HTTPException:
            return NO_TRUSTED_HEADER_TO_ADMIT_YOU
        return None

    def adopt(self, method_setting: str) -> AuthDecision:
        """Install the door named by ``method_setting`` and return what is now
        in force.

        Called **after** :meth:`refusal_for` said nothing and after the setting
        reached disk, so this never decides anything — it swaps. Idempotent: a
        save that did not move the door rebinds nothing, which is what lets the
        synchronous path and the background apply both call it.
        """
        if not self._declared:
            # See `__init__`: this assembly never named a door, so it keeps the
            # one it was built with rather than adopting one out of a document
            # it was not told to read.
            return self._decision
        chosen = coerce_method(method_setting)
        bypass = self._decision.bypass_user
        decision = AuthDecision(
            chosen=chosen,
            method=AuthMethod.BYPASS if bypass else chosen,
            bypass_user=bypass,
        )
        if decision == self._decision:
            return self._decision
        self._decision = decision
        self._dependency = self._build(decision)
        if self._context is not None:
            self._context.decision = decision
        logger.info(
            "auth_method_swapped",
            method=decision.method.value,
            chosen=decision.chosen.value,
        )
        return decision


# ---------------------------------------------------------------------------
# The admin JSON API's own door — an access key, and only an access key
# ---------------------------------------------------------------------------


ADMIN_API_NEEDS_A_KEY = (
    "The admin JSON API needs an access key. Send it as "
    "'Authorization: Bearer <key>'. Being signed in is not enough on its own — "
    "this surface reads everybody's conversations and issues the keys to /v1, "
    "so it takes a credential an administrator handed out on purpose. The "
    "admin screens in the browser are unaffected and need no key."
)
"""Said to a caller with no key, and to one whose key is unknown or switched
off — one sentence for all three, so nobody learns which keys exist by reading
error messages (the same rule ``/v1`` follows)."""

ADMIN_API_KEY_LACKS_THE_SCOPE = (
    f"That access key does not carry the '{ADMIN_API_SCOPE}' scope, so it "
    "cannot reach the admin JSON API. Keys are issued without it deliberately: "
    "a key given to a display so it can hold a conversation must not also be "
    "able to read the trace and issue more keys. Issue a new key with the admin "
    "API box ticked, and revoke this one if it was meant to have it."
)
"""Said to a real, working key that was simply not granted this surface.

A different sentence from :data:`ADMIN_API_NEEDS_A_KEY`, and a 403 rather than
a 401, because it is a different problem with a different fix and the person
reading it already holds the key — there is nothing to disclose about it that
they do not have in their hand.
"""

ADMIN_API_HAS_NO_KEY_STORE = (
    "This core was assembled without an API-key store, so nothing can open the "
    "admin JSON API — a key is the only credential it takes. The admin screens "
    "in the browser still work and can do everything this API can."
)
"""A core with no key store has no way to authenticate this surface at all.

Answered as 503 with the reason rather than 401: a switched-off feature and a
refused credential look identical from outside, and only one of them is worth
telling an operator about (the same distinction ``KEYS_UNAVAILABLE`` makes).
"""


def presented_key(authorization: str | None) -> str | None:
    """The key out of an ``Authorization: Bearer`` header, or ``None``.

    Six lines rather than the ``/v1`` surface's own ``_bearer``, which does the
    same job: importing it here would make :mod:`personacore.admin` depend on
    the OpenAI wire layer and, through it, on the agent loop — a large coupling
    to buy a header split. The two must agree on the *format* a key is
    presented in, and that is a one-line convention rather than shared logic.
    """
    if not authorization or not authorization.strip():
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


class AdminApiKeyDoor:
    """The credential ``/admin/api`` takes, held so the router can carry it.

    **Why this exists.** The admin JSON API authenticated and did not
    authorise: its one dependency asked "are you signed in", so any household
    member with an account could reach ``GET /admin/api/trace`` — everybody's
    conversations — and ``POST /admin/api/keys``, which mints credentials to
    ``/v1``. The rendered UI hid those screens from a member (ADR-0032), so the
    JSON API was the way round that gate. A minor with an account could issue
    themselves a key and read the house.

    **What it does instead.** This surface now takes an access key carrying
    :data:`~personacore.contracts.policy.ADMIN_API_SCOPE`, and takes nothing
    else. A browser session is neither necessary nor sufficient: not sufficient,
    because that is the hole; not necessary, because the default door is the
    core's own sign-in and a machine client would otherwise need a household
    member's password to talk to an API that exists for machines.

    **The key is the caller.** :meth:`identify` mints an
    :class:`~personacore.admin.models.AdminUser` from the verified key, so the
    handlers' audit records and ``require_admin`` checks work unchanged. The id
    is ``key:<key_id>`` — prefixed because a key's profile id is free text and
    an audit line reading ``alice`` must never be ambiguous about whether a
    person or a key did it. The door is
    :attr:`~personacore.auth.method.AuthMethod.API_KEY` for the same reason.

    It is an **administrator**, because that is what granting the scope means:
    the surface's routes do not divide into harmless and dangerous halves, and
    an admin ticked the box deliberately. Nothing has to be admin *and* hold the
    key; holding the key is the decision.

    **The rendered UI is untouched by this.** It never calls ``/admin/api`` over
    HTTP — it looks the JSON handlers up on the running app and calls them
    directly (``personacore.web.shared.api_handler``), which does not run a
    router dependency. Those calls carry a browser request with no key, so
    :meth:`identify` falls through to the session and the audit record names the
    person who clicked, which is what it should say.
    """

    def __init__(self, keys: ApiKeyGateway | None, *, live_auth: LiveAuth) -> None:
        self._keys = keys
        self._live_auth = live_auth

    def require(self, request: Request) -> None:
        """The router dependency. Verifies the key or refuses the request.

        Attached to the router that carries the whole of ``/admin/api``, never
        to a handler: ADR-0032's default-deny is a property of the router
        precisely so a route added next month cannot forget it.
        """
        presented = presented_key(request.headers.get("authorization"))
        if presented is None:
            # **Credential first, availability second**, which is the order the
            # key endpoints already keep. A caller who brought nothing learns
            # nothing — not even whether this core has a key store — and only
            # somebody who at least presented a key is told why it could not be
            # checked.
            raise self._no_credential()
        if self._keys is None:
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, ADMIN_API_HAS_NO_KEY_STORE)

        record = self._keys.verify(presented)
        if record is None:
            # Absent, malformed, unknown, revoked and switched off are one
            # answer. `verify` already refuses a disabled key, so nothing here
            # re-reads `profile.enabled` and gets a second opinion.
            raise self._no_credential()
        if ADMIN_API_SCOPE not in record.profile.scopes:
            logger.warning("admin_api_key_lacks_scope", key_id=record.key_id)
            raise _fail(status.HTTP_403_FORBIDDEN, ADMIN_API_KEY_LACKS_THE_SCOPE)

        # Kept for `identify`, so the key is verified once per request rather
        # than once per handler that wants to know who is calling.
        request.state.admin_api_key = record

    @staticmethod
    def _no_credential() -> HTTPException:
        """One 401 for no key, a malformed one, an unknown one and a switched-off
        one alike — anything that varied between them would say which keys
        exist."""
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ApiError(error=ADMIN_API_NEEDS_A_KEY, problems=[]).model_dump(),
            headers={"WWW-Authenticate": "Bearer"},
        )

    def identify(self, request: Request) -> AdminUser:
        """Who this request is — the key that opened the door, or the session.

        Handed to the JSON API's modules as their ``require_user``. It answers
        from the key whenever :meth:`require` has run, which on the guarded
        router is always; the fall-through to the session exists for the
        rendered UI, which calls these same handlers directly with a browser
        request (see the class docstring).
        """
        record: ApiKeyRecord | None = getattr(request.state, "admin_api_key", None)
        if record is None:
            return self._live_auth.identify(request)
        return AdminUser(
            id=f"key:{record.key_id}", door=AuthMethod.API_KEY, is_admin=True
        )


def require_admin(user: AdminUser) -> AdminUser:
    """Refuse a non-admin — PC-290's "only an admin sees the list of users".

    A 403 with a reason rather than a 404 or an empty list. Pretending the
    users page does not exist would be a lie a household member could disprove
    in one click, and an empty list is the worse lie: it says there is nobody
    else here.
    """
    if not user.is_admin:
        raise _fail(status.HTTP_403_FORBIDDEN, NOT_AN_ADMIN)
    return user


def session_summary(record: SessionRecord, *, current: bool) -> SessionView:
    return SessionView(
        session_id=record.session_id,
        started_at=record.created_at,
        last_seen_at=record.last_seen_at,
        expires_at=record.expires_at,
        current=current,
    )


__all__ = [
    "ADMIN_API_HAS_NO_KEY_STORE",
    "ADMIN_API_KEY_LACKS_THE_SCOPE",
    "ADMIN_API_NEEDS_A_KEY",
    "BAD_CREDENTIALS",
    "BYPASS_NOT_A_TEST",
    "DOOR_CHANGE_NEEDS_THE_SCREEN",
    "HEADER_UNUSABLE",
    "NOT_AN_ADMIN",
    "NOT_SIGNED_IN",
    "NOT_SIGNED_IN_PROXY",
    "NO_ACCOUNTS_HERE",
    "NO_ACCOUNT_TO_ADMIT_YOU",
    "NO_TLS_WARNING",
    "NO_TRUSTED_HEADER_TO_ADMIT_YOU",
    "SESSIONS_ENDED_ACTION",
    "SETUP_ACTION",
    "SETUP_DONE",
    "SIGN_IN_ACTION",
    "SIGN_IN_UNAVAILABLE",
    "SIGN_OUT_ACTION",
    "USER_CREATED_ACTION",
    "USER_MINOR_ACTION",
    "AdminApiKeyDoor",
    "AuthContext",
    "LiveAuth",
    "SignInRefused",
    "SignInRequired",
    "client_address",
    "make_admin_user_dependency",
    "presented_key",
    "require_admin",
    "session_summary",
]
