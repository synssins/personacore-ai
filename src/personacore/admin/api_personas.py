"""Personas — listing them, reading one, and choosing the default.

Split out of :mod:`personacore.admin.routes` (ADR-0040). Spec section 5.5's
hot-swap with no restart, and spec section 9's picker.

Selecting a default is a settings write, so it goes through the same
``save_config`` every other settings write does
(:mod:`personacore.admin.api_config`) rather than touching the document itself
— two ways of saving is how one of them stops applying live.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, status

from personacore.admin.api_shared import AdminApiContext, _fail
from personacore.admin.models import (
    PersonaDetail,
    PersonaListing,
    PersonaSelected,
    PersonaSummary,
)
from personacore.agent.errors import PersonaError, PersonaInvalidError, PersonaNotFoundError
from personacore.agent.personas import Persona, PersonaStore
from personacore.audit import get_logger

logger = get_logger(__name__)


def build_persona_listing(personas: PersonaStore, default_persona: str) -> PersonaListing:
    """The section 9 persona picker's data.

    A persona whose files are broken is listed with its reason rather than
    dropped, for the same reason a broken plugin is (spec sections 5.1 and 9):
    "it isn't there" is never a useful answer about something you can see on
    disk.
    """
    summaries: list[PersonaSummary] = []
    for name in personas.available():
        try:
            persona = personas.load(name)
        except PersonaError as exc:
            summaries.append(
                PersonaSummary(
                    name=name,
                    display_name=name,
                    is_default=name == default_persona,
                    loadable=False,
                    problem=exc.spoken_message,
                )
            )
            continue
        summaries.append(
            PersonaSummary(
                name=persona.name,
                display_name=persona.display_name,
                description=persona.description,
                voice_engine=persona.voice_engine,
                voice_name=persona.voice_name,
                is_default=persona.name == default_persona,
            )
        )
    return PersonaListing(personas=summaries, default_persona=default_persona)


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register the three persona routes on the guarded router."""
    api = router
    personas = ctx.personas
    require_user = ctx.require_user
    _read_config = ctx.read_config
    _save_config = ctx.save_config

    async def _load_persona(name: str) -> Persona:
        try:
            return await asyncio.to_thread(personas.load, name)
        except PersonaNotFoundError as exc:
            raise _fail(status.HTTP_404_NOT_FOUND, exc.spoken_message) from exc
        except PersonaInvalidError as exc:
            raise _fail(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.spoken_message) from exc

    @api.get("/personas", response_model=PersonaListing, summary="Available personas")
    async def list_personas() -> PersonaListing:
        """Spec section 9's persona picker."""
        settings = await _read_config()
        default = str(settings.settings.get("default_persona", personas.default_persona))
        return await asyncio.to_thread(build_persona_listing, personas, default)

    @api.get(
        "/personas/{name}",
        response_model=PersonaDetail,
        summary="One persona, with its prompt",
    )
    async def get_persona(name: str) -> PersonaDetail:
        """The prompt is included so the picker can preview what swapping to
        this persona actually changes (spec section 5.5)."""
        settings = await _read_config()
        default = str(settings.settings.get("default_persona", personas.default_persona))
        persona = await _load_persona(name)
        return PersonaDetail(
            name=persona.name,
            display_name=persona.display_name,
            description=persona.description,
            voice_engine=persona.voice_engine,
            voice_name=persona.voice_name,
            is_default=persona.name == default,
            system_prompt=persona.system_prompt,
            prompt_prefix=persona.prompt_prefix,
            metadata=persona.metadata,
        )

    @api.post(
        "/personas/{name}/select",
        response_model=PersonaSelected,
        summary="Make this persona the default",
    )
    async def select_persona(name: str, request: Request) -> PersonaSelected:
        """Spec section 5.5: hot-swappable, no restarts.

        The persona is loaded first and the config written second: a default
        that points at a persona which cannot be loaded is a core that will not
        answer, and finding that out at the next turn rather than at the click
        is the wrong order.
        """
        user = require_user(request)
        persona = await _load_persona(name)
        current = await _read_config()
        payload = dict(current.settings)
        payload["default_persona"] = persona.name
        await _save_config(payload, user, action="personas.select", request=request)
        return PersonaSelected(
            default_persona=persona.name,
            message=f"{persona.display_name} is now the default persona.",
        )


__all__ = [
    "build_persona_listing",
    "register",
]
