"""API keys for the exposed OpenAI-compatible surface — issue, list, revoke.

Spec section 5.4: keys are "issued and revoked in the admin UI". Split out of
:mod:`personacore.admin.routes` (ADR-0040).

**The plaintext key leaves this core exactly once**, in the body of the issue
response, and :func:`build_api_key_view` is the reason the listing cannot leak:
it names the four fields that may leave and the hash is simply not one of them.
Nothing in the split changed either of those; if a field ever appears in a
response that is not named in that function, it was put there on purpose.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Annotated

from fastapi import APIRouter, Request, Response, status
from fastapi import Path as PathParam

from personacore.admin.api_shared import AdminApiContext, _fail, _record_change
from personacore.admin.models import (
    ApiKeyIssued,
    ApiKeyIssueRequest,
    ApiKeyListing,
    ApiKeyView,
)
from personacore.admin.protocols import ApiKeyGateway
from personacore.api.keys import ApiKeyError, ApiKeyRecord
from personacore.audit import AuditOutcome, get_logger
from personacore.contracts.policy import ProfileKind

logger = get_logger(__name__)


KEY_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
"""Shape a key id has to have to reach a handler.

Key ids are ``secrets.token_hex(8)``, so this is generous rather than tight.
It exists because ``{key_id}`` lands in an audit record and in the test
surface's HTML: refusing the junk at the door is cheaper than escaping it
everywhere afterwards (spec section 7, "treat everything from outside as
untrusted input").
"""

ANONYMOUS_KEY_REFUSED = (
    "An API key cannot be issued against an anonymous profile. The exposed API "
    "has no anonymous tier — spec section 5.4 requires a key for every caller, "
    "even on the LAN. Issue this key against a profile of kind 'api_key' "
    "instead, or turn the anonymous chat tier on separately if that is what you "
    "meant."
)
"""Plain English for the one refusal the issue endpoint makes on purpose
(spec section 9: errors an operator can act on)."""

KEYS_UNAVAILABLE = (
    "API key management is not switched on in this core, so keys cannot be "
    "listed, issued or revoked from here. The core was started without an "
    "API-key store; check the deployment configuration."
)


def build_api_key_view(record: ApiKeyRecord) -> ApiKeyView:
    """Render one stored key for the admin API.

    This function is the whole reason the listing cannot leak: it names the
    four fields that may leave, and ``record.key_hash`` is simply not one of
    them. Building the view by hand rather than by ``model_dump`` is
    deliberate — a dump would carry whatever the store adds next, and the next
    field it adds might be one nobody wants on a screen (spec section 7).
    """
    return ApiKeyView(
        key_id=record.key_id,
        note=record.note,
        created_at=record.created_at,
        enabled=record.profile.enabled,
        profile=record.profile,
    )


def build_api_key_listing(records: Iterable[ApiKeyRecord]) -> ApiKeyListing:
    """``GET /admin/api/keys``' body — spec section 9's "API-key issuance"
    screen needs to show what is already out there before it issues more.

    Ordered oldest first, then by id, so the list does not reshuffle under an
    operator between two page loads.
    """
    views = [build_api_key_view(record) for record in records]
    views.sort(key=lambda view: (view.created_at, view.key_id))
    return ApiKeyListing(keys=views, count=len(views))


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register the three key routes on the guarded router."""
    api = router
    api_keys = ctx.api_keys
    audit = ctx.audit
    require_user = ctx.require_user

    def _keys() -> ApiKeyGateway:
        """The key store, or a 503 saying so in words."""
        if api_keys is None:
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, KEYS_UNAVAILABLE)
        return api_keys

    @api.get("/keys", response_model=ApiKeyListing, summary="Issued API keys")
    async def list_api_keys() -> ApiKeyListing:
        """Spec section 9's "API-key issuance" screen, read half.

        **Neither the key nor its hash appears here.** The core keeps only a
        one-way hash, so there is nothing to show even if someone wanted to;
        the hash itself stays in the store because a fingerprint of a
        credential on a screen is one screenshot away from being a hint (spec
        section 7). A revoked key is gone from this list rather than flagged in
        it — see :class:`~personacore.admin.models.ApiKeyListing`.
        """
        store = _keys()
        try:
            listing = await asyncio.to_thread(store.records)
        except ApiKeyError as exc:
            # The store did not load. **Not an empty listing**, which is the
            # whole point: an operator shown "no keys" issues a replacement,
            # and that write is exactly what used to destroy the file it had
            # failed to read. Issue and revoke already answer this way; the
            # listing was the one path that still turned it into a 500 with no
            # explanation, so the screen showed nothing rather than the
            # sentence the migration wrote about which file is damaged.
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return build_api_key_listing(listing)

    @api.post(
        "/keys",
        response_model=ApiKeyIssued,
        status_code=status.HTTP_201_CREATED,
        summary="Issue an API key (the key is shown once, here)",
    )
    async def issue_api_key(body: ApiKeyIssueRequest, request: Request) -> ApiKeyIssued:
        """Mint a key for a policy profile — spec section 5.4.

        **The plaintext key is in this response and nowhere else, ever.** Not
        in ``GET /admin/api/keys``, not in the audit record this write
        produces, not in any log line, and not recoverable by any later call:
        the store keeps a SHA-256 and no more. Losing the value costs one
        re-issue; storing it would cost the household its front door. The
        response field is called ``api_key_shown_once`` for that reason and
        carries the sentence the UI must print beside it.

        Refused with ``400`` for an ``anonymous`` profile. Spec section 5.4
        allows no anonymous access on this surface even on the LAN, and
        ADR-0003's anonymous tier is a different surface entirely — a key
        attributing its traffic to it would launder one into the other. The
        store refuses this itself; the check here exists so the operator gets
        that sentence instead of a validation stack trace.
        """
        user = require_user(request)
        store = _keys()

        if body.profile.kind is ProfileKind.ANONYMOUS:
            await _record_change(
                audit,
                user,
                action="api_keys.issue",
                outcome=AuditOutcome.REFUSED,
                detail={"profile_id": body.profile.id, "profile_kind": body.profile.kind.value},
            )
            raise _fail(status.HTTP_400_BAD_REQUEST, ANONYMOUS_KEY_REFUSED)

        try:
            issued = await asyncio.to_thread(
                store.issue, profile=body.profile, note=body.note
            )
        except ValueError as exc:
            # Belt and braces behind the explicit check above: ApiKeyRecord may
            # refuse a profile for a reason this router has not learned about
            # yet, and that must still reach the operator as a 400 with a
            # reason rather than a 500 with none. pydantic's ValidationError is
            # a ValueError, so this catches it too.
            await _record_change(
                audit,
                user,
                action="api_keys.issue",
                outcome=AuditOutcome.REFUSED,
                detail={"profile_id": body.profile.id, "profile_kind": body.profile.kind.value},
            )
            raise _fail(
                status.HTTP_400_BAD_REQUEST,
                f"That profile cannot carry an API key: {exc}",
            ) from exc
        except ApiKeyError as exc:
            # The key file could not be written. The store's message already
            # says what to check, so it is passed through unedited.
            raise _fail(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

        await _record_change(
            audit,
            user,
            action="api_keys.issue",
            outcome=AuditOutcome.SUCCESS,
            # Identifiers only. The key is absent for the obvious reason; the
            # note is absent because it is operator free text that ends up in
            # backups (spec section 7), and the listing already carries it.
            detail={
                "key_id": issued.record.key_id,
                "profile_id": issued.record.profile.id,
                "profile_kind": issued.record.profile.kind.value,
            },
        )
        return ApiKeyIssued(
            api_key_shown_once=issued.key.get_secret_value(),
            key=build_api_key_view(issued.record),
        )

    @api.delete(
        "/keys/{key_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Revoke an API key",
    )
    async def revoke_api_key(
        key_id: Annotated[str, PathParam(pattern=KEY_ID_PATTERN)], request: Request
    ) -> Response:
        """Revoke a key — spec section 5.4's other half.

        **Always ``204``, never ``404``**, once the caller is authenticated.
        Two reasons, and neither is tidiness:

        1. ``DELETE`` promises a state, not an event: after this call that key
           does not open the door. A second click, a browser retry or a
           replayed request must therefore agree with the first one. Returning
           ``404`` would make the retry that an anxious operator *should*
           perform look like a failure, which is how a revocation ends up
           half-believed.
        2. A ``404`` here answers "does this key id exist?" for anyone who can
           reach the endpoint. That is a small oracle behind the login proxy,
           but spec section 7's least-privilege line does not have a size
           threshold, and there is nothing to gain by handing it out.

        The audit record is where the difference is kept: ``existed`` says
        whether anything was actually removed.
        """
        user = require_user(request)
        store = _keys()
        try:
            existed = await asyncio.to_thread(store.revoke, key_id)
        except ApiKeyError as exc:
            raise _fail(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
        await _record_change(
            audit,
            user,
            action="api_keys.revoke",
            # SUCCESS either way: the operator asked for a state and got it.
            # `existed` is what an investigator actually needs later.
            outcome=AuditOutcome.SUCCESS,
            detail={"key_id": key_id, "existed": existed},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "ANONYMOUS_KEY_REFUSED",
    "KEYS_UNAVAILABLE",
    "KEY_ID_PATTERN",
    "build_api_key_listing",
    "build_api_key_view",
    "register",
]
