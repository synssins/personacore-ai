"""The Personas screen (spec section 5.5, ADR-0017): what is installed, who
answers as what, and which one answers when a client names none.

The pieces the editing and deleting screens also need are module-level
functions here rather than closures, because those two files are the same
screen split for size and reading them together is the point.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from personacore.admin.models import (
    ApiKeyView,
    PersonaListing,
)
from personacore.agent.errors import PersonaError
from personacore.web.screens.voice_common import (
    persona_voice_warning,
    voice_library,
)
from personacore.web.shared import (
    NO_KEY_OPERATIONS,
    NO_PERSONA_OPERATIONS,
    UIContext,
    api_handler,
    current_config,
    refusal,
)

# ---------------------------------------------------------------------------
# Personas — the character, and the string that selects it
# ---------------------------------------------------------------------------
#
# The first design pass drew a screen that showed personas existing and never
# showed how anything *selects* one. That is the whole point of ADR-0017: a
# persona is chosen by putting its identifier in a client's **model** field,
# because the model dropdown in LobeChat or Home Assistant is a control every
# OpenAI client already has. A card that shows "Vex" and not ``vex`` leaves an
# operator with no idea what to type, so the identifier is on every card, in a
# code span, with a copy button beside it.
#
# The other thing that pass got wrong was deletion. A bare ``delete`` hides the
# consequence, and deleting a persona has two of them: the keys bound to it lose
# their character, and — if it is the *default* — every client pinned to the
# generic id changes character with nothing announcing it. Both are named in the
# confirmation, in words, before anything is removed.

GENERIC_MODEL_ID = "personacore"
"""The id that means "whatever the default persona is" (ADR-0017).

It stays advertised forever: a client pinned to it predates per-persona ids and
must keep working. On the default persona's card that is worth saying out loud,
because it is the reason deleting *that* persona is an identity swap rather than
an error."""

PERSONA_MODEL_FIELD_NOTE = (
    "Put this in your client's model field to make it answer as this persona."
)

PERSONA_GENERIC_NOTE = (
    "The generic id “{generic}” resolves to whichever persona is default — right "
    "now, this one. Clients pinned to that id follow the default wherever it goes."
)

PERSONA_MODEL_SELECTION_LATER = (
    "Selecting a persona by model name is not switched on yet: /v1/models still "
    "advertises only “{generic}”, so today a client's character comes from the "
    "persona on its access key. The identifiers below are the names that will be "
    "advertised, and the ones an access key already takes."
)
"""ADR-0017's primary mechanism, honestly labelled.

The identifiers are real — ``PolicyProfile.persona`` takes exactly these strings
today — but the ``/v1/models`` half of ADR-0017 is not built, and a screen that
told an operator to type ``vex`` into a model box that will refuse it would be
worse than one that says nothing. So the identifier is shown, and the mechanism
that does not work yet is marked rather than hidden."""

PERSONA_VOICE_NONE = "(no voice — replies in text)"
"""What a card says about a persona nobody has given a voice.

Names the consequence rather than the absence: a persona with no voice is not
half-configured, it is one that answers in text, which is a working state."""

PERSONA_VOICE_EDIT_NOTE = "Set it on the persona's own page."
"""The card shows the voice; the edit screen chooses it.

One control per decision, and the card is a recognition aid rather than a
second form — the same reason it shows a prompt excerpt rather than the
prompt."""

PERSONA_INSTALL_LATER = (
    "Installing a persona from a .zip is not built yet. Create one here instead."
)

PROMPT_EXCERPT_CHARS = 240
"""How much of a prompt a card shows. Enough to recognise the character by, short
enough that six cards still fit on a screen."""

MAX_PERSONA_NAME_CHARS = 64
"""Matches ``personas._NAME_PATTERN``'s own bound, so a name refused here and a
name refused there are refused for the same reason."""


def persona_slug(name: str) -> str:
    """A display name reduced to a persona identifier.

    Lowercased, with every run of anything outside ``[a-z0-9._-]`` collapsed to a
    single dash, and leading punctuation trimmed — so "Vex (the sharp one)"
    becomes ``vex-the-sharp-one``. Returns ``""`` when nothing usable is left,
    which the caller turns into :data:`PERSONA_NAME_UNUSABLE` rather than a
    folder called ``-``.

    The result is re-checked by :meth:`~personacore.agent.personas.PersonaStore.resolve_dir`
    before anything is opened. This function makes a *likely* name; that method
    is what makes it a *safe* one, and the order matters — nothing here is a
    security boundary.
    """
    lowered = name.strip().lower()
    collapsed = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-._")
    return collapsed[:MAX_PERSONA_NAME_CHARS]


def prompt_excerpt(text: str, *, limit: int = PROMPT_EXCERPT_CHARS) -> str:
    """One paragraph of a prompt, on one line, for a card.

    Whitespace is collapsed rather than preserved: a card is a recognition aid,
    and a prompt's own line breaks turn six of them into a page of scrolling.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


def persona_voice_label(engine: str | None, voice: str | None) -> str:
    """A persona's suggested voice in ADR-0002's one-list spelling, or ``""``.

    ``VOICENAME (Engine)`` because ADR-0002 requires one field listing every
    voice from every engine that way — so the value shown here reads the same as
    the option that will one day set it.
    """
    if voice and engine:
        return f"{voice} ({engine})"
    return voice or engine or ""


def persona_bindings(
    keys: Sequence[ApiKeyView], default_persona: str
) -> tuple[dict[str, list[str]], list[str]]:
    """Which keys name which persona, and which ones just follow the default.

    Two answers rather than one, because the difference is what a deletion turns
    on. A key that names ``vex`` **loses its character** when ``vex`` goes and
    falls back to the default. A key that names nothing is already following the
    default and is unaffected by any deletion except the default's own — where it
    is affected completely, silently, and is exactly who the warning is for.

    Keys are named by their note, because that is the string an operator wrote to
    recognise the client by; a key with no note falls back to its public id,
    which is the only other handle there is.
    """
    explicit: dict[str, list[str]] = {}
    following: list[str] = []
    for key in keys:
        label = key.note.strip() or key.key_id
        if key.profile.persona:
            explicit.setdefault(key.profile.persona, []).append(label)
        else:
            following.append(label)
    return explicit, following


def persona_rows(
    listing: PersonaListing,
    *,
    prompts: Mapping[str, str],
    explicit_keys: Mapping[str, Sequence[str]],
    default_followers: Sequence[str] = (),
    warning: Callable[[str | None, str | None], str] | None = None,
) -> list[dict[str, Any]]:
    """One card per persona, in the shape ``personas.html`` renders.

    ``prompts`` is separate from the listing because
    :class:`~personacore.admin.models.PersonaSummary` deliberately has no prompt
    in it — the JSON listing is a picker, and shipping every prompt in it would
    make a list endpoint carry kilobytes nobody asked for. A persona whose files
    are broken is still a card, with its reason where the excerpt would be: the
    operator opening this screen is most likely the one whose persona stopped
    working.

    ``warning`` says why a persona is not speaking, when it is not (PC-336) —
    a switched-off engine, or one this core does not have. Optional so this
    function stays callable with a listing and nothing else, the same shape
    :func:`~personacore.web.screens.plugins.plugin_rows` gives its
    ``waiting`` argument.
    """
    rows: list[dict[str, Any]] = []
    for summary in listing.personas:
        bound = list(explicit_keys.get(summary.name, ()))
        if summary.is_default:
            bound += [f"{label} (follows the default)" for label in default_followers]
        rows.append(
            {
                "slug": summary.name,
                "name": summary.display_name,
                "is_default": summary.is_default,
                "loadable": summary.loadable,
                "problem": summary.problem,
                "prompt_excerpt": (
                    prompt_excerpt(prompts.get(summary.name, ""))
                    if summary.loadable
                    else (summary.problem or "This persona could not be read.")
                ),
                "voice": (
                    persona_voice_label(summary.voice_engine, summary.voice_name)
                    or PERSONA_VOICE_NONE
                ),
                # Why this persona is not speaking, when it is not (PC-336).
                # Empty for every persona that is: a card carrying a warning
                # about a working thing is a card people stop reading.
                "voice_warning": (
                    warning(summary.voice_engine, summary.voice_name)
                    if warning is not None
                    else ""
                ),
                "bound_keys": bound,
                "explicit_keys": list(explicit_keys.get(summary.name, ())),
            }
        )
    return rows


def persona_dir(ctx: UIContext, slug: str) -> Path:
    """One persona's directory, or a 404.

    ``resolve_dir`` is the store's own check and the only one that matters:
    it refuses anything that is not a plain name, refuses anything that
    resolves outside appdata, and refuses a symlink pointing somewhere else
    in appdata. :func:`persona_slug` above makes a *likely* name; this is
    what makes a name safe to open, and it runs on every request that names
    one.
    """
    try:
        directory = ctx.personas.resolve_dir(slug)
    except PersonaError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.spoken_message) from exc
    if not directory.is_dir():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"There is no persona called “{slug}”."
        )
    return directory


def default_persona(ctx: UIContext) -> str:
    """Which persona answers when a turn names none.

    Read from the settings document rather than from ``personas``' own
    attribute: the document is what survives a restart, and the two disagree
    for exactly as long as it takes an apply to land.
    """
    current, _unreadable = current_config(ctx.layout)
    if current is None:
        return ctx.personas.default_persona
    return str(current.settings.get("default_persona", ctx.personas.default_persona))


def persona_prompts(ctx: UIContext, listing: PersonaListing) -> dict[str, str]:
    """Each loadable persona's prompt, for the card excerpts."""
    prompts: dict[str, str] = {}
    for summary in listing.personas:
        if not summary.loadable:
            continue
        try:
            prompts[summary.name] = ctx.personas.load(summary.name).system_prompt
        except PersonaError:  # pragma: no cover - loadable said otherwise
            continue
    return prompts


async def key_views(request: Request) -> tuple[list[ApiKeyView], str | None]:
    """Every issued key, or why there are none to show.

    A core assembled without a key store is not an error on the Personas
    screen — it just means no card can say which clients answer as it, and
    the screen says that rather than showing an empty "Answers on:" line that
    looks like a fact.
    """
    handler = api_handler(request.app, "list_keys")
    if handler is None:
        return [], NO_KEY_OPERATIONS
    try:
        listing = await handler()
    except HTTPException as exc:
        return [], refusal(exc)
    return list(listing.keys), None


async def personas_context(
    ctx: UIContext, request: Request, *, save_result: dict[str, str] | None = None
) -> dict[str, Any]:
    default = default_persona(ctx)
    listing = await persona_listing(ctx, default)
    keys, keys_note = await key_views(request)
    explicit, following = persona_bindings(keys, default)
    return {
        **await ctx.shell(request, "personas"),
        "personas": persona_rows(
            listing,
            prompts=persona_prompts(ctx, listing),
            explicit_keys=explicit,
            default_followers=following,
            warning=lambda engine, name: persona_voice_warning(
                engine, name, voice_library(request)
            ),
        ),
        "generic_model_id": GENERIC_MODEL_ID,
        "model_field_note": PERSONA_MODEL_FIELD_NOTE,
        "generic_note": PERSONA_GENERIC_NOTE.format(generic=GENERIC_MODEL_ID),
        "selection_later": PERSONA_MODEL_SELECTION_LATER.format(
            generic=GENERIC_MODEL_ID
        ),
        "voice_note": PERSONA_VOICE_EDIT_NOTE,
        "install_later": PERSONA_INSTALL_LATER,
        "keys_note": keys_note,
        "save_result": save_result,
    }


async def persona_listing(ctx: UIContext, default: str) -> PersonaListing:
    """The persona listing, off the event loop.

    Every entry is a directory read and a file read, the same reason the JSON
    API's own ``list_personas`` goes through a thread (see this module's
    header on blocking work). Built with the API's own builder, not a second
    walk of the same folder.
    """
    # Imported inside the function: `admin/routes.py` builds this router, so a
    # top-level import back into it would be a cycle.
    from personacore.admin.routes import build_persona_listing

    return await asyncio.to_thread(build_persona_listing, ctx.personas, default)


async def personas_page_with(
    ctx: UIContext, request: Request, result: dict[str, str]
) -> HTMLResponse:
    return ctx.templates.TemplateResponse(
        request=request,
        name="personas.html",
        context=await personas_context(ctx, request, save_result=result),
    )


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the personas list and the change-the-default post."""
    templates = ctx.templates
    _personas_context = partial(personas_context, ctx)
    _personas_page_with = partial(personas_page_with, ctx)
    _persona_dir = partial(persona_dir, ctx)
    _refusal = refusal


    @router.get("/personas", response_class=HTMLResponse, summary="Installed personas")
    async def personas_page(request: Request) -> HTMLResponse:
        """Spec §5.5's personas, and ADR-0017's answer to "how does a client pick
        one?".

        The identifier is the part that had to be right: a persona is selected by
        name, so a screen that shows characters without showing their names shows
        an operator nothing they can act on.
        """
        return templates.TemplateResponse(
            request=request,
            name="personas.html",
            context=await _personas_context(request),
        )

    @router.post(
        "/personas/{slug}/default",
        response_class=HTMLResponse,
        summary="Make one persona the default",
    )
    async def persona_set_default(request: Request, slug: str) -> HTMLResponse:
        """Spec §5.5's hot swap — through the JSON API's own ``select_persona``.

        That handler loads the persona *before* writing the config, so a default
        pointing at something unloadable is refused at the click rather than
        discovered at the next turn.
        """
        _persona_dir(slug)
        handler = api_handler(request.app, "select_persona")
        if handler is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NO_PERSONA_OPERATIONS)
        try:
            selected = await handler(name=slug, request=request)
        except HTTPException as exc:
            return await _personas_page_with(
                request, {"kind": "invalid", "message": f"Not changed: {_refusal(exc)}"}
            )
        return await _personas_page_with(
            request, {"kind": "saved", "message": selected.message}
        )
