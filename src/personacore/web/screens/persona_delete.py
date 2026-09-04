"""Deleting a persona: the confirmation that names what goes, and the post
that does it (spec section 5.5).

The confirmation is specific on purpose. It names the persona, says what is
deleted with it, and - when the persona being deleted is the one that answers
by default - says which one takes over. A dialog that only asks "are you sure?"
is a dialog that gets clicked through.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.agent.errors import PersonaError
from personacore.agent.personas import (
    ensure_default_persona,
)
from personacore.audit.models import (
    AuditOutcome,
)
from personacore.web.screens.personas import (
    GENERIC_MODEL_ID,
    default_persona,
    key_views,
    persona_bindings,
    persona_dir,
    personas_page_with,
)
from personacore.web.shared import (
    NO_PERSONA_OPERATIONS,
    UIContext,
    _readable,
    api_handler,
    refusal,
)

PERSONA_DELETE_TITLE = "Delete {name}?"

PERSONA_DELETE_LABEL = "Delete {name}"


def persona_delete_body(
    *,
    display_name: str,
    is_default: bool,
    keys: Sequence[str],
    successor: str | None,
) -> str:
    """The deletion confirmation, naming what it breaks.

    Three facts, in the order they matter to whoever is about to click:

    1. **Which clients answer as this persona**, by name. "Delete Vex?" tells an
       operator nothing about the kitchen display that has been Vex for a month.
    2. **What is destroyed** — the folder and the prompt, unrecoverably.
    3. **The identity swap**, when this is the default. That case is not an error
       and must not be described as one: the core keeps working, a starter
       persona is written when nothing is left, and every client pinned to
       ``personacore`` starts speaking as somebody else with nothing announcing
       it. A dialog that says "are you sure?" and stops is a dialog that gets
       clicked through; this one says what happens afterwards.
    """
    lines: list[str] = []
    if keys:
        named = ", ".join(f"“{key}”" for key in keys)
        verb = "answers" if len(keys) == 1 else "answer"
        lines.append(
            f"{named} {verb} as {display_name}. Deleting it means they fall back to "
            "the default persona."
        )
    else:
        lines.append(f"No access key names {display_name}, so no client loses its character.")
    lines.append(
        f"The folder for {display_name} and the prompt inside it are deleted, and "
        "nothing here can bring them back."
    )
    if is_default and successor is None:
        lines.append(
            f"{display_name} is the default persona — the generic model id "
            f"“{GENERIC_MODEL_ID}” points at it — and it is the last one left. This is "
            "not an error: a starter persona is written in its place and becomes the "
            "default, so every client pinned to the default carries on working and "
            "silently starts speaking as that instead."
        )
    elif is_default:
        lines.append(
            f"{display_name} is the default persona — the generic model id "
            f"“{GENERIC_MODEL_ID}” points at it. This is not an error: “{successor}” "
            "becomes the default, so every client pinned to the default carries on "
            "working and silently starts speaking as that instead."
        )
    return " ".join(lines)


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the delete confirmation and the delete post."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import _record_change

    templates = ctx.templates
    audit = ctx.audit
    layout = ctx.layout
    personas = ctx.personas
    require_user = ctx.require_user
    _default_persona = partial(default_persona, ctx)
    _key_views = key_views
    _persona_dir = partial(persona_dir, ctx)
    _personas_page_with = partial(personas_page_with, ctx)
    _refusal = refusal


    async def _delete_facts(request: Request, slug: str) -> dict[str, Any]:
        """Everything the delete confirmation has to say, gathered once.

        Both the dialog and the deletion itself need the same three answers — who
        is bound to this persona, is it the default, and what becomes the default
        afterwards — and computing them twice is how a dialog ends up promising
        something the action does not do.
        """
        default = _default_persona()
        keys, _note = await _key_views(request)
        explicit, _following = persona_bindings(keys, default)
        remaining = [name for name in personas.available() if name != slug]
        successor: str | None = None
        if remaining:
            preferred = remaining[0]
            try:
                successor = personas.load(preferred).display_name
            except PersonaError:
                successor = preferred
        try:
            display_name = personas.load(slug).display_name
        except PersonaError:
            display_name = slug
        return {
            "slug": slug,
            "display_name": display_name,
            "is_default": slug == default,
            "keys": explicit.get(slug, []),
            "successor": successor,
            "successor_slug": remaining[0] if remaining else None,
        }

    async def _delete_confirm_context(request: Request, slug: str) -> dict[str, Any]:
        """The three things both the dialog and the page say — computed once
        so they cannot drift apart (see :func:`persona_delete_body`)."""
        facts = await _delete_facts(request, slug)
        return {
            "title": PERSONA_DELETE_TITLE.format(name=facts["display_name"]),
            "body": persona_delete_body(
                display_name=facts["display_name"],
                is_default=facts["is_default"],
                keys=facts["keys"],
                successor=facts["successor"],
            ),
            "confirm_label": PERSONA_DELETE_LABEL.format(name=facts["display_name"]),
        }

    @router.get(
        "/personas/{slug}/delete/confirm",
        response_class=HTMLResponse,
        summary="Confirm deleting one persona (page)",
    )
    async def persona_delete_confirm_page(request: Request, slug: str) -> HTMLResponse:
        """The no-script fallback (ADR-0020) for :func:`persona_delete_confirm`
        below — the same three facts, as a real page with a real form, reached
        by the same link the dialog's own `hx-get` decorates.
        """
        _persona_dir(slug)
        return templates.TemplateResponse(
            request=request,
            name="confirm_page.html",
            context={
                **await ctx.shell(request, "personas"),
                **await _delete_confirm_context(request, slug),
                "action": f"/admin/personas/{slug}/delete",
                "back_href": "/admin/personas",
                "back_label": "← Personas",
            },
        )

    @router.get(
        "/personas/{slug}/delete/confirm/fragment",
        response_class=HTMLResponse,
        summary="Confirm deleting one persona",
    )
    async def persona_delete_confirm(request: Request, slug: str) -> HTMLResponse:
        """The confirmation that names what it breaks — see
        :func:`persona_delete_body`."""
        _persona_dir(slug)
        return templates.TemplateResponse(
            request=request,
            name="fragments/confirm.html",
            context={
                **await _delete_confirm_context(request, slug),
                "action": f"/admin/personas/{slug}/delete",
                "target": "body",
            },
        )

    @router.post(
        "/personas/{slug}/delete",
        response_class=HTMLResponse,
        response_model=None,
        summary="Delete one persona",
    )
    async def persona_delete(request: Request, slug: str) -> HTMLResponse | RedirectResponse:
        """Remove the folder, and repoint the default if that is what went.

        The repointing is the part that is not tidying. ``default_persona`` in
        core.toml is a name, not a reference, so deleting the persona it names
        would otherwise leave every keyless and unpinned client failing on the
        next turn with "I don't have a persona called …". Instead: a starter
        persona is written when nothing is left (the same
        :func:`~personacore.agent.personas.ensure_default_persona` first run
        uses), the default moves to whatever is there, and the message says
        plainly that the character changed — because nothing else will.
        """
        user = require_user(request)
        directory = _persona_dir(slug)
        facts = await _delete_facts(request, slug)
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            return await _personas_page_with(
                request,
                {
                    "kind": "invalid",
                    "message": (
                        f"{facts['display_name']} was not deleted — its folder could not "
                        f"be removed: {_readable(exc)}"
                    ),
                },
            )
        personas.invalidate()
        await _record_change(
            audit,
            user,
            action="personas.delete",
            outcome=AuditOutcome.SUCCESS,
            detail={"persona": slug, "was_default": facts["is_default"]},
        )

        message = f"Deleted {facts['display_name']}."
        if facts["keys"]:
            named = ", ".join(f"“{key}”" for key in facts["keys"])
            message += f" {named} now answer as the default persona."
        if facts["is_default"]:
            message += " " + await _repoint_default(request, facts)

        if request.headers.get("HX-Request"):
            # Exactly as before this fix: the dialog's own form (target="body")
            # expects the whole page back to swap in, so it still gets one.
            return await _personas_page_with(request, {"kind": "saved", "message": message})

        # No script ran this POST — a real browser follows a real redirect
        # (spec §9's click-first). The address carries a slug and a flag,
        # never the sentence above: `personas_page` rebuilds it from current
        # state (`persona_delete_redirect_notice`), the same discipline
        # chat's own bulk-delete redirect keeps for the same reason.
        target = f"/admin/personas?deleted={quote(slug)}"
        if facts["is_default"]:
            target += "&was_default=1"
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)

    async def _repoint_default(request: Request, facts: Mapping[str, Any]) -> str:
        """Move ``default_persona`` off a persona that no longer exists.

        Writes through the JSON API's ``select_persona`` so core.toml, the live
        objects and the audit log all move together — the same path the "set as
        default" button uses, because this *is* setting a default, just one
        nobody chose.
        """
        successor_slug = facts["successor_slug"]
        recreated = False
        if successor_slug is None:
            recreated = ensure_default_persona(layout)
            personas.invalidate()
            available = personas.available()
            successor_slug = available[0] if available else None
        if successor_slug is None:  # pragma: no cover - ensure_default just wrote one
            return (
                "That was the default persona and nothing replaced it — the next turn "
                "will fail until a persona exists."
            )
        handler = api_handler(request.app, "select_persona")
        if handler is None:  # pragma: no cover - assembled without the JSON API
            return NO_PERSONA_OPERATIONS
        try:
            selected = await handler(name=successor_slug, request=request)
        except HTTPException as exc:
            return f"The default persona could not be moved: {_refusal(exc)}"
        origin = (
            "A starter persona was written in its place, and it"
            if recreated
            else f"“{successor_slug}”"
        )
        return (
            f"That was the default persona. {origin} is the default now, so every client "
            f"pinned to “{GENERIC_MODEL_ID}” has changed character. {selected.message}"
        )
