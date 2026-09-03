"""Core settings — reading the document, and the one path that writes it.

Split out of :mod:`personacore.admin.routes` (ADR-0040).

The two builders below are the reason this module exists rather than the two
routes: :func:`config_saver` produces the *only* function that writes
``core.toml``, and the designed UI's settings screens are handed the same one
(ADR-0034). Two ways of saving is how one of them stops applying live, stops
auditing, or stops asking the new door whether it would still let the operator
in.

Secrets are named, never valued, on the way out — ``config_io.read_config``
enforces that — and the redaction marker is resolved back into the stored value
on the way in, so a caller that read the document and saved it back does not
write three asterisks over a working credential.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, status

from personacore.admin.api_shared import AdminApiContext, _fail, _record_change
from personacore.admin.authn import LiveAuth, require_admin
from personacore.admin.config_io import (
    ConfigRejected,
    check_secret_references,
    read_config,
    restore_write_only_values,
    validate_settings,
    write_config,
)
from personacore.admin.models import (
    AdminUser,
    ConfigResponse,
    ConfigUpdateRequest,
    ConfigUpdateResponse,
)
from personacore.admin.protocols import AuditGateway, SettingsApplier
from personacore.audit import AuditOutcome, get_logger
from personacore.config.appdata import AppdataLayout
from personacore.config.settings import CoreSettings

logger = get_logger(__name__)

ConfigReader = Callable[[], Awaitable[ConfigResponse]]
ConfigSaver = Callable[..., Awaitable[ConfigResponse]]


def config_reader(layout: AppdataLayout) -> ConfigReader:
    """The one read of the settings document."""

    async def _read_config() -> ConfigResponse:
        try:
            return await asyncio.to_thread(read_config, layout)
        except ConfigRejected as exc:
            raise _fail(
                status.HTTP_500_INTERNAL_SERVER_ERROR, exc.message, exc.problems
            ) from exc

    return _read_config


def config_saver(
    *,
    layout: AppdataLayout,
    audit: AuditGateway,
    live_auth: LiveAuth,
    apply_settings: SettingsApplier | None,
    _read_config: ConfigReader,
) -> ConfigSaver:
    """The one write of the settings document — see the module docstring.

    ``_read_config`` keeps its underscore, which is not a style slip: the write
    ends by reading the file back, and naming the parameter what the closure
    below already called it is what let this move be a move. Renaming it would
    have edited the body of the one function on this surface that must not be
    edited by accident.
    """

    async def _save_config(
        payload: dict[str, Any],
        user: AdminUser,
        *,
        action: str,
        request: Request | None = None,
    ) -> ConfigResponse:
        """Validate, write, tell whoever owns the live objects, then audit it.

        ``request`` is the one the operator is saving with, and it is here for
        exactly one setting: ``[auth] method`` decides which door lets people
        in, so before it is written the new door is asked whether it would
        admit *this caller* (:meth:`personacore.admin.authn.LiveAuth.
        refusal_for`). A "no" refuses the save — nothing reaches disk, the old
        door keeps working, and the answer is the sentence saying what to do
        about it. Every path that can carry that setting passes a request; one
        that does not is refused rather than trusted, because a door swapped
        without the check is the lockout the check exists to prevent.

        The redaction marker is resolved back into the stored value *first*
        (ADR: the broker password lives in ``[bus]`` and never leaves it). Every
        read of the config renders that password as ``***``, so a caller that
        read the document and saved it back — the raw editor, a ``GET`` then
        ``PUT``, the Core form carrying a field the operator did not touch —
        would otherwise write three asterisks over a working credential and
        break the broker on a save about something else entirely.
        """
        payload = await asyncio.to_thread(restore_write_only_values, layout, payload)

        def validated(document: dict[str, Any]) -> CoreSettings:
            """Shape first, then whether the secrets it names actually exist.

            Both are refusals of the same save, so both go through the one
            ``ConfigRejected`` path below and produce the one audited FAILURE.
            The existence check is second because a document that is not valid
            settings has no reliable ``*_secret`` fields to look up.
            """
            settings = validate_settings(document)
            check_secret_references(layout, settings)
            return settings

        try:
            settings = await asyncio.to_thread(validated, payload)
        except ConfigRejected as exc:
            await _record_change(
                audit,
                user,
                action=action,
                outcome=AuditOutcome.FAILURE,
                # Keys only. The rejected document is the operator's, and this
                # record goes into backups (spec section 7).
                detail={"keys": sorted(str(k) for k in payload)},
            )
            raise _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message, exc.problems) from exc

        # BEFORE the write, so a refused door swap leaves core.toml exactly as
        # it was. Checking after would mean the file said one thing and the
        # running process another, which is the state ADR-0010 forbids.
        refusal = live_auth.refusal_for(request, settings.auth.method)
        if refusal is not None:
            await _record_change(
                audit,
                user,
                action=action,
                outcome=AuditOutcome.REFUSED,
                detail={
                    "keys": sorted(str(k) for k in payload),
                    "auth_method_refused": settings.auth.method,
                    "auth_method_in_force": live_auth.decision.chosen.value,
                },
            )
            raise _fail(status.HTTP_409_CONFLICT, refusal)

        try:
            await asyncio.to_thread(write_config, layout, settings)
        except ConfigRejected as exc:
            raise _fail(
                status.HTTP_500_INTERNAL_SERVER_ERROR, exc.message, exc.problems
            ) from exc

        if apply_settings is not None:
            apply_settings(settings)
        await _record_change(
            audit,
            user,
            action=action,
            outcome=AuditOutcome.SUCCESS,
            detail={"keys": sorted(str(k) for k in payload)},
        )
        return await _read_config()

    return _save_config


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register ``GET`` and ``PUT /admin/api/config`` on the guarded router."""
    api = router
    require_user = ctx.require_user
    _read_config = ctx.read_config
    _save_config = ctx.save_config

    @api.get("/config", response_model=ConfigResponse, summary="Read core settings")
    async def get_config() -> ConfigResponse:
        """Spec section 7: secrets are named here, never valued. See
        ``config_io.read_config``, which enforces that on the way out."""
        return await _read_config()

    @api.put("/config", response_model=ConfigUpdateResponse, summary="Write core settings")
    async def put_config(body: ConfigUpdateRequest, request: Request) -> ConfigUpdateResponse:
        """Spec section 9: validation with plain-English errors.

        A rejected write returns ``422`` with one problem per offending key,
        each naming the key and saying what to do about it, and **nothing is
        written** — the file on disk is only replaced once the whole document
        validates.
        """
        user = require_admin(require_user(request))
        saved = await _save_config(
            body.settings, user, action="config.update", request=request
        )
        return ConfigUpdateResponse(
            saved=True,
            config=saved,
            message=(
                "Settings saved. The listen address takes effect when the core "
                "restarts; everything else is in force now."
            ),
        )


__all__ = [
    "config_reader",
    "config_saver",
    "register",
]
