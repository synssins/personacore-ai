"""Workstation pairing — issue, show, cancel.

``working/team/enrolment/PLAN.md`` §1 and ``s1-pairing/SPEC.md``. The owner
opens Plugins, clicks "+ Add a workstation", and this router is what puts a
code on his screen, tells the screen when to stop showing it, and lets him
close the dialog early. It does **not** redeem a code — that is a separate,
unauthenticated route (S2) built against
:func:`personacore.enrolment.pairing.redeem` as the one thing this module and
that one agree on. This module never imports it and never will.

Modelled on :mod:`personacore.admin.api_keys`: a value that leaves exactly
once in an issue response and is never readable again (see that module's
docstring). ``ctx.require_user`` guards every route the same way it guards
every other admin route; there is no separate check here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from personacore.admin.api_shared import AdminApiContext, _record_change
from personacore.admin.models import (
    PairingCurrent,
    PairingIssued,
    PairingRefusalView,
)
from personacore.audit import AuditOutcome
from personacore.enrolment import pairing


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register the three pairing routes on the guarded router."""
    api = router
    audit = ctx.audit
    require_user = ctx.require_user

    @api.post(
        "/pairings",
        response_model=PairingIssued,
        status_code=status.HTTP_201_CREATED,
        summary="Issue a pairing code (the code is shown once, here)",
    )
    async def issue_pairing(request: Request) -> PairingIssued:
        """Mint a code for the owner to type into a workstation's Agent.

        **The code is in this response and nowhere else, ever.** Not in
        ``GET .../current``, not in the audit record this write produces, and
        not recoverable by any later call. Supersedes whatever code was
        already live — issuing twice leaves only the newest one able to be
        redeemed.
        """
        user = require_user(request)
        issued = pairing.issue()
        await _record_change(
            audit,
            user,
            action="pairings.issue",
            outcome=AuditOutcome.SUCCESS,
            # The code itself is never a detail field — same rule
            # api_keys.issue_api_key follows for the key it mints.
            detail={"expires_at": issued.expires_at.isoformat()},
        )
        return PairingIssued(
            code=issued.code,
            expires_at=issued.expires_at,
            expires_in_s=issued.expires_in_s,
        )

    @api.get(
        "/pairings/current",
        response_model=PairingCurrent,
        summary="The pairing screen's poll",
    )
    async def get_current_pairing(request: Request) -> PairingCurrent:
        """What the screen renders while it waits — never the code itself."""
        require_user(request)
        snapshot = pairing.current()
        refusal = snapshot.refusal
        return PairingCurrent(
            status=snapshot.status,
            expires_at=snapshot.expires_at,
            expires_in_s=snapshot.expires_in_s,
            claimed_by=snapshot.claimed_by,
            # Mapped field by field rather than handed over whole: the store's
            # own dataclass is free to grow something this surface has not
            # agreed to publish, and a spread would publish it the day it did.
            refusal=(
                PairingRefusalView(reason=refusal.reason, machine=refusal.machine)
                if refusal is not None
                else None
            ),
        )

    @api.delete(
        "/pairings/current",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Cancel the live pairing code",
    )
    async def cancel_pairing(request: Request) -> Response:
        """The owner closing the dialog kills the code.

        Always ``204``, whether or not anything was actually live — same
        reasoning as ``api_keys.revoke_api_key``: this promises a state
        ("no code is waiting now"), not an event, so a retry must not look
        like a failure. The audit record is where "existed" is kept for
        anyone investigating later.
        """
        user = require_user(request)
        existed = pairing.cancel()
        await _record_change(
            audit,
            user,
            action="pairings.cancel",
            outcome=AuditOutcome.SUCCESS,
            detail={"existed": existed},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["register"]
