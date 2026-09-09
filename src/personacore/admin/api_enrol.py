"""``POST /enrol/workstation`` — the one route in this core with no door.

**Read this before changing anything here.** Every other route on this surface
is guarded, and it is guarded by being registered on a router that carries the
dependency (ADR-0032): a handler cannot forget the check because no handler
performs it. This route is registered on the *unguarded* router, beside the
sign-in endpoints, and it is the only one there that writes anything.

It is unauthenticated because it has to be. The workstation joining has no
credential yet — that is the whole problem enrolment exists to solve, and the
alternative was the owner carrying a token between two machines by hand, which
he refused. What stands in for a credential is a short pairing code the core is
displaying on its own screen at that moment, typed in by the person standing at
both machines. The code is the authorisation; everything below serves not
opening a hole underneath it.

What holds the line, in the order a request meets it:

* **A fixed-window failure throttle per source address**
  (:class:`personacore.auth.throttle.SignInThrottle`, reused rather than
  reinvented), so the code cannot be guessed at faster than the pairing store's
  own failure cap already allows.
* **The code is redeemed before any other field is read.** A caller without one
  learns only that the route exists — not a plugin name, not whether a code is
  live, not this core's version.
* **One refusal, one sentence, for every way a code can be wrong**, so nothing
  in the answer distinguishes expired from mistyped from already-spent.
* **The route's normal state is inert**, because no code is active. It is
  reachable and it refuses, which is the whole of what it does for all but a
  few minutes of a core's life.

The work itself is :mod:`personacore.enrolment.workstation`. This module is the
door, the body limit, the throttle and the audit record, and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from personacore.admin.api_shared import AdminApiContext, _fail
from personacore.admin.authn import client_address
from personacore.audit import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    Owner,
    Surface,
    get_correlation_id,
    get_logger,
)
from personacore.auth.throttle import SignInThrottle
from personacore.config.secrets import SecretStore
from personacore.enrolment.registry import enrolment_audit_detail
from personacore.enrolment.workstation import (
    ENROL_PATH,
    MAX_BODY_BYTES,
    RATE_LIMITED,
    Enrolled,
    EnrolmentRefused,
    EnrolmentService,
)
from personacore.plugins.packages import DEFAULT_PACKAGE_LIMITS

logger = get_logger(__name__)

THROTTLE_KEY = "enrol"
"""The throttle is keyed on ``(name, address)`` because it was built for
sign-in, where the name is an account. Enrolment has no account, so every
attempt from one address shares one bucket -- which is what is wanted: the
counter is per source, and somebody guessing codes locks out only themselves."""


class EnrolmentAccepted(BaseModel):
    """What the Agent is told. **Never the token, never the code.**"""

    model_config = ConfigDict(extra="forbid")

    plugin: str
    """The name it was enrolled under, derived from the name it sent."""

    display_name: str
    """That name as the core normalised it, so the Agent can show the owner
    what it will be called rather than the core applying it silently."""

    state: str
    """``ok``, ``failing`` or ``unknown`` — the health row as it stands a moment
    after the plugin was switched on."""

    message: str


def _default_redeem(code: str) -> bool:
    """Burn a pairing code, through the sibling module that owns them.

    Imported inside the function rather than at module scope on purpose: the
    pairing store and this route were built in parallel against a written
    signature, and a module-level import would make the order they land in
    matter. It also keeps :class:`EnrolmentService` testable with a stub
    without the store existing at all.
    """
    from personacore.enrolment.pairing import redeem

    return bool(redeem(code))


def build_service(ctx: AdminApiContext, **overrides: Any) -> EnrolmentService:
    """Assemble the service from the collaborators the surface already holds.

    Nothing is rebuilt that the router factory already built. The scan cache,
    the live toggle and the appdata layout are the ones the Plugins screen uses,
    so a workstation that joins appears in the same listing, through the same
    reload, as a plugin installed by hand.
    """
    secrets = ctx.secrets if isinstance(ctx.secrets, SecretStore) else SecretStore(ctx.layout)

    async def _installed_names() -> list[str]:
        listing = await ctx.scans.current()
        return [view.name for view in listing.plugins]

    settings: dict[str, Any] = {
        "layout": ctx.layout,
        "secrets": secrets,
        "redeem": _default_redeem,
        "reload": ctx.scans.reload,
        "set_enabled": ctx.live_toggle,
        "package_limits": ctx.package_limits or DEFAULT_PACKAGE_LIMITS,
        "installed_names": _installed_names,
    }
    settings.update(overrides)
    return EnrolmentService(**settings)


def register_public(
    router: APIRouter,
    ctx: AdminApiContext,
    *,
    service: EnrolmentService | None = None,
    throttle: SignInThrottle | None = None,
) -> None:
    """Mount enrolment on the **unguarded** router. Read the module docstring.

    Handed the same unprefixed router the sign-in routes are, so the path is
    top-level rather than under ``/admin`` — see
    :data:`personacore.enrolment.workstation.ENROL_PATH` for why that is an
    operational choice and not a cosmetic one.
    """
    enrolment = service or build_service(ctx)
    limiter = throttle or SignInThrottle()
    public = APIRouter(tags=["enrolment"])

    @public.post(
        ENROL_PATH,
        response_model=EnrolmentAccepted,
        status_code=status.HTTP_201_CREATED,
        summary="Join this core as a workstation, using a pairing code",
    )
    async def enrol_workstation(request: Request) -> EnrolmentAccepted:
        """One pairing code in, one working workstation out."""
        address = client_address(request)
        waiting = limiter.retry_after(THROTTLE_KEY, address)
        if waiting:
            raise _refuse(
                status.HTTP_429_TOO_MANY_REQUESTS,
                RATE_LIMITED.format(seconds=waiting),
                retry_after=waiting,
            )

        body = await _read_body(request)
        try:
            result = await enrolment.enrol(body)
        except EnrolmentRefused as exc:
            # `Retry-After` goes on the exception, never on an injected
            # `Response`: FastAPI merges that object's headers into a value the
            # handler *returns*, and this path raises. A header set there would
            # simply not be sent, which is the quiet kind of wrong.
            locked = limiter.record_failure(THROTTLE_KEY, address)
            # Logged, not audited. An audit row per refusal would let an
            # unauthenticated caller make this core write to appdata as fast as
            # it can send requests, which is the denial of service the throttle
            # exists to prevent rather than a record worth keeping. A refusal
            # that happens *after* a code was successfully redeemed is audited
            # below, and those are bounded by the codes the owner issued.
            # `reason` is a code, never `exc.message`. Both this log file and
            # the audit store below are core state that outlives any plugin, and
            # a refusal sentence is free text that can name a machine — the
            # collision refusal has to say *which* enrolled machine it clashed
            # with to be worth reading. The sentence goes back to the caller in
            # the response; a code is what is recorded.
            code = _refusal_code(exc.status_code)
            logger.warning(
                "workstation_enrol_refused",
                address=address,
                status=exc.status_code,
                reason=code,
            )
            if exc.redeemed:
                await _record(
                    ctx,
                    action="plugins.enrol",
                    outcome=AuditOutcome.FAILURE,
                    # The source and the refusal code, and no plugin name:
                    # nothing was installed, so naming one would be a claim
                    # about a change that did not happen. A refused attempt is
                    # not a machine, which is why the address stays here and
                    # does not stay on the success row below.
                    detail=enrolment_audit_detail(address=address, reason=code),
                )
            raise _refuse(
                exc.status_code, exc.message, retry_after=locked or None
            ) from None

        limiter.clear(THROTTLE_KEY, address)
        await _record(
            ctx,
            action="plugins.enrol",
            outcome=AuditOutcome.SUCCESS,
            # **That a machine joined, not which one.** The audit store is core
            # state and outlives any plugin, so a row naming the machine and its
            # address is a second copy of the registry in a place uninstalling
            # the plugin does not reach. What the row keeps is what an operator
            # actually asks it — did something get added that I did not add —
            # and :func:`enrolment_audit_detail` documents which fields may be
            # here and which may never be added.
            detail=enrolment_audit_detail(plugin=result.plugin, state=result.state),
        )
        return _accepted(result)

    router.include_router(public)


_REFUSAL_CODES = {
    400: "invalid_request",
    403: "code_refused",
    409: "conflict",
    413: "too_large",
    429: "rate_limited",
    500: "storage",
    502: "machine_unreachable",
}
"""What a refusal is recorded as. Coarse on purpose — it is derived from the
status rather than from the sentence, so no refusal added later can carry text
into the audit store by being worded differently.

An enrolment refusal does not yet carry a code of its own the way
:class:`personacore.enrolment.registry.MachineRejected` does. When the registry
is wired into enrolment, its ``reason`` is the better value to pass here and
this mapping becomes the fallback for the refusals that predate it."""


def _refusal_code(status_code: int) -> str:
    return _REFUSAL_CODES.get(status_code, "refused")


def _refuse(
    status_code: int, message: str, *, retry_after: int | None = None
) -> HTTPException:
    """The surface's one error shape, plus ``Retry-After`` when there is one.

    :func:`personacore.admin.api_shared._fail` builds the body every other route
    on this surface returns; this wraps it rather than replacing it so a caller
    parses one shape whichever door refused them.
    """
    failure = _fail(status_code, message)
    if retry_after is not None:
        failure.headers = {**(failure.headers or {}), "Retry-After": str(retry_after)}
    return failure


def _accepted(result: Enrolled) -> EnrolmentAccepted:
    return EnrolmentAccepted(
        plugin=result.plugin,
        display_name=result.display_name,
        state=result.state,
        message=result.message,
    )


async def _read_body(request: Request) -> bytes:
    """Read the request body, refusing anything over the cap.

    Streamed with a running total rather than ``await request.body()``, for the
    reason ``admin/api_plugins.py`` gives for its own upload reader: a limit
    checked after buffering is a limit that has already been exceeded. The
    declared ``Content-Length`` is a courtesy check for an honest client and is
    never the only one, because it is a header from outside.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _fail(status.HTTP_413_CONTENT_TOO_LARGE, _TOO_LARGE)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _fail(status.HTTP_413_CONTENT_TOO_LARGE, _TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


_TOO_LARGE = (
    "That enrolment request is too large. A workstation sends its name, its "
    "address, its fingerprint and its tool list, which is a few kilobytes."
)


async def _record(
    ctx: AdminApiContext, *, action: str, outcome: AuditOutcome, detail: dict[str, Any]
) -> None:
    """Record one enrolment. Never raises, for the reason ``_record_change`` does not.

    Attributed to the anonymous owner on the anonymous surface, because that is
    literally what happened: nobody was signed in. The category is
    ``admin_change`` all the same — a plugin was installed and switched on, and
    that is the thing an operator reading the trace is looking for.
    """
    audit = ctx.audit
    try:
        await audit.record_audit(
            AuditRecord(
                correlation_id=get_correlation_id() or uuid4().hex,
                timestamp=datetime.now(UTC),
                surface=Surface.ANONYMOUS,
                owner=Owner.anonymous(),
                category=AuditCategory.ADMIN_CHANGE,
                action=action,
                outcome=outcome,
                detail=detail,
            )
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.error("enrolment_audit_write_failed", action=action, error=repr(exc))


__all__ = [
    "EnrolmentAccepted",
    "build_service",
    "register_public",
]
