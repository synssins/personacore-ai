"""Writing a persona: the new-persona form, the edit form, and the two posts
that save them (spec section 5.5).

The identifier is the part that had to be right. It is made from the name once,
on creation, and never again - a renamed persona would be a different persona
to every client that had named the old one.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

import tomli_w
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from personacore.agent.errors import PersonaError
from personacore.agent.personas import (
    LLM_TABLE,
    METADATA_FILENAME,
    PAUSES_KEY,
    PROMPT_FILENAMES,
    SPEECH_TABLE,
    SpeechPause,
    parse_pause_lines,
    pause_lines,
    pause_table,
)
from personacore.audit.models import (
    AuditOutcome,
)
from personacore.config.settings import LLMSettings
from personacore.plugins.packages import PackageRejected
from personacore.plugins.voice_packages import (
    installed_voices,
    require_engine_id,
    require_voice_id,
)
from personacore.web.screens.connection_key import (
    MAX_KEY_CHARS,
    KeySubmission,
    forget_key,
    key_status,
    persona_key_name,
    read_key_form,
    store_key,
)
from personacore.web.screens.personas import (
    MAX_PERSONA_NAME_CHARS,
    PERSONA_MODEL_FIELD_NOTE,
    persona_dir,
    persona_slug,
    persona_voice_label,
    personas_page_with,
)
from personacore.web.screens.voice_common import (
    PERSONA_VOICE_NONE_INSTALLED,
    PERSONA_VOICE_NOTE,
    persona_voice_options,
    persona_voice_warning,
    split_voice_value,
    voice_library,
    voice_value,
)
from personacore.web.shared import (
    UIContext,
)

MAX_PROMPT_CHARS = 64_000
"""Ceiling on a submitted prompt. A system prompt is instructions, not a corpus,
and something arriving from outside gets a bound before it gets a use (spec §7).
Generous by an order of magnitude against any real persona."""

PERSONA_NAME_REQUIRED = "Give the persona a name — it is what the identifier is made from."

PERSONA_NAME_UNUSABLE = (
    "“{name}” has no letters or digits in it, so there is no identifier to make "
    "from it. Persona identifiers are letters, digits, dots, dashes and "
    "underscores."
)

PERSONA_PROMPT_REQUIRED = (
    "A persona is its prompt, so it cannot be empty — write how this character "
    "speaks and what it does."
)

PERSONA_PROMPT_TOO_LONG = (
    "That prompt is longer than {limit} characters, which is far more than a "
    "system prompt should ever be. Nothing was written."
)

MAX_PREFIX_CHARS = 4_000
"""Ceiling on the prompt-prefix box (prompt-prefix contract §3). A prefix is a
short tone adjustment, not a prompt — the persona prompt's own cap is sixteen
times this — and it is still a bound on something arriving from outside
(spec §7)."""

PERSONA_PREFIX_TOO_LONG = (
    "That prompt prefix is longer than {limit} characters, which is far more "
    "than a tone adjustment should ever be. Nothing was written."
)

PERSONA_MEMORY_LABEL = "Memory"

PERSONA_MEMORY_HELP = (
    "This character remembers what people tell it, and what the review pass "
    "finds worth keeping. Off means no memory is read or written for it; "
    "anything already kept is left alone. New personas start with memory off."
)
"""Memory contract §9 — the persona switch. The checkbox is always on the
form; **unticked by default for a new persona** (thinking contract §13 E —
an existing persona still shows its own file's value), so a save always
knows the intended state: absence of the ``memory`` key already means off
(`Persona.memory_enabled`'s own default), so ticked writes ``memory =
true`` explicitly and unticked removes the key."""

PERSONA_WORKSPACE_LABEL = "Workspace"

PERSONA_WORKSPACE_HELP = (
    "Gives this persona a folder per conversation. Files a tool fetches and "
    "files the persona writes are kept there, and the persona can list, "
    "read and write them."
)
"""Workspace contract §5 — the persona switch. **Opposite default from
Memory above.** The box is always on the form, so a save always knows the
intended state — but unlike Memory, unticked writes nothing (absence already
means off, per contract §0.1) and ticked writes ``workspace = true``."""

MAX_WORKSPACE_PINS_CHARS = 2_000
"""Ceiling on the pinned-files box. A few dozen comma-separated patterns is a
few hundred characters; this is roomy against that and still a bound on
something arriving from outside (spec §7)."""

PERSONA_WORKSPACE_PINS_LABEL = "Pinned files"

PERSONA_WORKSPACE_PINS_HELP = (
    "Filename patterns, comma separated, e.g. B*_Ch*.md. Matching workspace "
    "files are shown whole to the persona every turn."
)
"""Workspace contract §6 — ``workspace_pins``. Comma-separated on the form,
a list in the file; an empty box removes the key rather than writing an
empty list, matching every other optional field on this form."""

_WORKSPACE_PIN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._*?-]{0,119}$")
"""One pinned-file pattern: the same shape
``personacore.workspaces.FILENAME_PATTERN`` requires of a bare workspace
filename, with ``*`` and ``?`` additionally allowed — a pin is a glob, and a
bare filename is only ever a glob with no wildcard in it."""

PERSONA_WORKSPACE_PIN_INVALID = (
    "“{pattern}” is not a usable file pattern. A pattern is letters, "
    "digits, '.', '_', '-', '*' or '?', up to 120 characters, and cannot "
    "start with a dot."
)


PERSONA_THINKING_LABEL = "Thinking"

PERSONA_THINKING_HELP = (
    "The model reasons before it answers. Off is faster and cheaper; on is "
    "better for review and editing."
)
"""Thinking contract §13 D — the persona switch. Same shape as Memory's
above it: the checkbox is always on the form, checked by default, so a save
always knows the intended state. Ticked removes the ``thinking`` key (which
already means on); unticked writes ``thinking = false``."""


def parse_workspace_pins(raw: str) -> tuple[list[str], str | None]:
    """Comma-separated glob patterns, cleaned — or the first thing wrong.

    Trimmed, empties dropped — a trailing comma or doubled spaces are not
    errors, they are how a person edits a list in one text box. Each
    surviving pattern is checked against :data:`_WORKSPACE_PIN_PATTERN`
    before any of them is written, so a save either takes every pattern or
    none of them rather than silently dropping the one that was wrong.
    """
    patterns: list[str] = []
    for piece in raw.split(","):
        pattern = piece.strip()
        if not pattern:
            continue
        if not _WORKSPACE_PIN_PATTERN.fullmatch(pattern):
            return [], PERSONA_WORKSPACE_PIN_INVALID.format(pattern=pattern)
        patterns.append(pattern)
    return patterns, None


MAX_PAUSES_CHARS = 8_000
"""Ceiling on the speech-pauses box. A hundred rules of ``word = 120, 180`` is
a couple of thousand characters; this is roomy against that and still a bound
on something arriving from outside (spec §7)."""

PERSONA_PAUSES_LABEL = "Speech pauses — words this character says with pauses of their own"

PERSONA_PAUSES_HELP = (
    "One word per line, then how long to wait before and after it in "
    "milliseconds: Hmm = 120, 180. The words themselves are never changed — "
    "the chat, the log and the reply keep exactly what was written, and only "
    "the silence around the word when it is spoken changes. Use this when a "
    "short word like “Hmm.” sounds announced: on its own it is a whole "
    "sentence, so it gets a full sentence pause on both sides. Zero means no "
    "pause at all, and a persona with an empty box speaks exactly as it does "
    "now."
)

PERSONA_VOICE_UNKNOWN = (
    "That is not one of the installed voices. Pick one from the list, or choose no voice."
)
"""Said when the submitted option value is not a pair of valid ids.

Only reachable by something other than this form: the list is built from what
is on disk, so a browser cannot produce it. It is here because a value that
arrives from outside gets a bound and a sentence before it gets a use, not
because anybody is expected to see it."""

PERSONA_EXISTS = (
    "There is already a persona with the identifier “{slug}”. Pick a different "
    "name, or edit the one that is there."
)

# ---------------------------------------------------------------------------
# This persona's own model connection (ADR-0036)
# ---------------------------------------------------------------------------
#
# Two radio buttons: the system's model, or one of this persona's own. The
# radios and the two boxes are ALWAYS in the markup and always submitted — the
# radio decides which is used, here on the server, and the reveal below the
# custom button is a stylesheet rule and nothing else. That is what makes the
# form work with no scripting at all: with no CSS the boxes are simply visible,
# which is a form that still works rather than one whose fields can never be
# reached.
#
# **Choosing the system default removes the section rather than copying it in.**
# Absence is the only thing that means "follow the system" (see
# `personacore.agent.personas.read_connection`), so a persona that spelled out
# today's default would stay on those values when the Models screen moves — and
# that is exactly what an operator pinning a character to a model asked for, so
# it must not be what happens to somebody who never asked for anything.
#
# The connection also has an API key box, shared with the Models screen — see
# `personacore.web.screens.connection_key`. It is write-only in both
# directions: a typed key goes into the secret store and `persona.toml` goes on
# holding only the NAME, and a stored key is never rendered back to the page.
# An empty box leaves an existing key alone, because an operator correcting a
# model name has said nothing whatever about the credential.

CONNECTION_MODE_DEFAULT = "default"
CONNECTION_MODE_CUSTOM = "custom"

MAX_CONNECTION_ADDRESS_CHARS = 500
MAX_CONNECTION_MODEL_CHARS = 200
"""Bounds on two boxes that arrive from outside (spec §7). Generous against any
real address or model name and still a bound."""

PERSONA_CONNECTION_LABEL = "Model connection"

PERSONA_CONNECTION_HELP = (
    "Which model this character thinks with. The system default is the "
    "connection on the Models screen, and a persona following it moves when it "
    "moves. Its own connection does not: it stays on the address and model "
    "written here until they are changed here."
)

PERSONA_CONNECTION_DEFAULT_LABEL = "Use the system default"

PERSONA_CONNECTION_CUSTOM_LABEL = "Use its own connection"

PERSONA_CONNECTION_INCOMPLETE = (
    "Give this persona's own connection both an address and a model name, or "
    "choose “Use the system default”. Nothing was written."
)

PERSONA_CONNECTION_REFUSED = (
    "That connection could not be read — {why}. Nothing was written."
)

# There used to be a `PERSONA_CONNECTION_KEY_NOTE` here, telling the operator to
# put a key on the volume by hand and name it in `persona.toml`, because this
# screen had no box for one. There is a box now — see
# `personacore.web.screens.connection_key`, which is the one place that
# knows how a typed key becomes a stored secret, so that this screen and the
# Models screen cannot grow two conventions for the same credential. The key is
# still stored by name and never written into `persona.toml`; all that changed
# is that the value can be put in place without a shell.


class ConnectionSubmission(NamedTuple):
    """The connection half of a submitted form.

    ``typed_*`` are kept alongside the parsed value so a refused save re-renders
    what was actually typed rather than an emptied or tidied version of it — the
    same rule the pauses box follows, for the same reason.
    """

    mode: str
    typed_address: str
    typed_model: str
    connection: LLMSettings | None
    refusal: str | None


def read_connection_form(form: Mapping[str, Any] | Any) -> ConnectionSubmission:
    """The two boxes and the radio, as one connection or one refusal.

    The value is built by :class:`~personacore.config.settings.LLMSettings`,
    which is the same model a role's connection is built by — so a pasted
    ``.../v1/chat/completions`` is trimmed back to its root here exactly as it is
    on the Models screen, by the one normaliser rather than by a second opinion.
    """
    mode = str(form.get("connection_mode") or CONNECTION_MODE_DEFAULT).strip()
    address = str(form.get("connection_address") or "").strip()[
        :MAX_CONNECTION_ADDRESS_CHARS
    ]
    model = str(form.get("connection_model") or "").strip()[:MAX_CONNECTION_MODEL_CHARS]
    if mode != CONNECTION_MODE_CUSTOM:
        # Anything that is not the custom button is the system default,
        # including a value this form never offered: a radio that arrived
        # misspelled is not a third state to invent behaviour for.
        return ConnectionSubmission(CONNECTION_MODE_DEFAULT, address, model, None, None)
    if not address or not model:
        return ConnectionSubmission(
            CONNECTION_MODE_CUSTOM, address, model, None, PERSONA_CONNECTION_INCOMPLETE
        )
    try:
        connection = LLMSettings(base_url=address, model=model)
    except ValidationError as exc:
        why = "; ".join(str(error["msg"]) for error in exc.errors())
        return ConnectionSubmission(
            CONNECTION_MODE_CUSTOM,
            address,
            model,
            None,
            PERSONA_CONNECTION_REFUSED.format(why=why),
        )
    return ConnectionSubmission(CONNECTION_MODE_CUSTOM, address, model, connection, None)


def connection_fields(connection: LLMSettings | None) -> dict[str, Any]:
    """One persona's connection in the shape the form renders."""
    if connection is None:
        return {
            "connection_mode": CONNECTION_MODE_DEFAULT,
            "connection_address": "",
            "connection_model": "",
            "connection_key_secret": "",
        }
    return {
        "connection_mode": CONNECTION_MODE_CUSTOM,
        "connection_address": connection.base_url,
        "connection_model": connection.model,
        # The NAME of the secret, which is all this file ever holds. The screen
        # turns it into "a key is set" and never into a value.
        "connection_key_secret": connection.api_key_secret or "",
    }


def connection_from_file(directory: Path) -> dict[str, Any]:
    """The connection boxes for a persona that will not load.

    Read straight off the file rather than through the store, because the reason
    the store refused may BE this section — and a form that quietly showed "use
    the system default" for a persona whose file says otherwise would throw the
    operator's connection away on the next save without ever mentioning it.
    Anything unreadable comes back as the system default, which is what an
    empty file means anyway.
    """
    path = directory / METADATA_FILENAME
    if not path.is_file():
        return connection_fields(None)
    try:
        with path.open("rb") as handle:
            metadata = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return connection_fields(None)
    table = metadata.get(LLM_TABLE)
    if not isinstance(table, Mapping):
        return connection_fields(None)
    address = str(table.get("base_url") or "")
    model = str(table.get("model") or "")
    key_secret = str(table.get("api_key_secret") or "")
    if not address and not model:
        return connection_fields(None)
    return {
        "connection_mode": CONNECTION_MODE_CUSTOM,
        "connection_address": address,
        "connection_model": model,
        "connection_key_secret": key_secret,
    }


PERSONA_IDENTIFIER_FIXED = (
    "Its identifier is “{slug}” — the string a client or an access key names to "
    "select it. **The identifier does not change when you rename it.** Only the "
    "display name changes here."
)
"""What the edit screen says about renaming.

The approved design said renaming changes the identifier and that clients pinned
to the old one "fall back to the default persona". They do not — a
``PolicyProfile.persona`` naming a missing folder raises
:class:`~personacore.agent.errors.PersonaNotFoundError` on the next turn, which
is a client that stops answering rather than one that changes character. Rather
than build a rename that breaks keys, the identifier is fixed at creation and the
screen says so. Recorded here because it is a deliberate departure from the
canvas, not drift."""


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the two persona forms and the two posts that save them."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import _record_change

    templates = ctx.templates
    audit = ctx.audit
    personas = ctx.personas
    require_user = ctx.require_user
    _shell = ctx.shell
    _persona_dir = partial(persona_dir, ctx)
    _personas_page_with = partial(personas_page_with, ctx)


    def _edit_context(
        request: Request,
        *,
        persona: dict[str, Any] | None,
        save_result: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """The form's context, including the one voice list (PC-202).

        Every voice from every engine, in one field, spelled ``GLaDOS
        (vits-onnx)``. Choosing an engine is not a step: the engine is how a
        voice is spoken rather than a decision the operator navigates
        (ADR-0029 §5). Voices whose engine is switched off are still listed —
        hiding them would make selecting one conditional on remembering to
        switch the engine on first — and the warning beside the field says what
        that costs (PC-336).
        """
        voices = installed_voices(ctx.layout)
        chosen = persona.get("voice_value", "") if persona else ""
        engine, name = split_voice_value(chosen)
        return {
            "persona": persona,
            "voice_options": persona_voice_options(voices),
            "voice_value": chosen,
            "voice_note": PERSONA_VOICE_NOTE if voices else PERSONA_VOICE_NONE_INSTALLED,
            "voice_warning": persona_voice_warning(engine, name, voice_library(request)),
            "identifier_note": (
                PERSONA_IDENTIFIER_FIXED.format(slug=persona["slug"]) if persona else ""
            ),
            "model_field_note": PERSONA_MODEL_FIELD_NOTE,
            "connection_label": PERSONA_CONNECTION_LABEL,
            "connection_help": PERSONA_CONNECTION_HELP,
            "connection_default_label": PERSONA_CONNECTION_DEFAULT_LABEL,
            "connection_custom_label": PERSONA_CONNECTION_CUSTOM_LABEL,
            # Whether a key is set, and never the key. A new persona has no
            # file and therefore names nothing, which is the same "no key is
            # set" an existing one shows.
            "connection_key": {
                **key_status(
                    ctx.layout, persona.get("connection_key_secret", "") if persona else ""
                ),
                "id": "persona",
                "maxlen": MAX_KEY_CHARS,
            },
            "connection_mode_default": CONNECTION_MODE_DEFAULT,
            "connection_mode_custom": CONNECTION_MODE_CUSTOM,
            "max_connection_address_chars": MAX_CONNECTION_ADDRESS_CHARS,
            "max_connection_model_chars": MAX_CONNECTION_MODEL_CHARS,
            "max_prompt_chars": MAX_PROMPT_CHARS,
            "max_prefix_chars": MAX_PREFIX_CHARS,
            "memory_label": PERSONA_MEMORY_LABEL,
            "memory_help": PERSONA_MEMORY_HELP,
            "workspace_label": PERSONA_WORKSPACE_LABEL,
            "workspace_help": PERSONA_WORKSPACE_HELP,
            "workspace_pins_label": PERSONA_WORKSPACE_PINS_LABEL,
            "workspace_pins_help": PERSONA_WORKSPACE_PINS_HELP,
            "max_workspace_pins_chars": MAX_WORKSPACE_PINS_CHARS,
            "thinking_label": PERSONA_THINKING_LABEL,
            "thinking_help": PERSONA_THINKING_HELP,
            "pauses_label": PERSONA_PAUSES_LABEL,
            "pauses_help": PERSONA_PAUSES_HELP,
            "max_pauses_chars": MAX_PAUSES_CHARS,
            "save_result": save_result,
        }

    @router.get(
        "/personas/new", response_class=HTMLResponse, summary="Write a new persona"
    )
    async def persona_new(request: Request) -> HTMLResponse:
        """The create form — the same template the edit screen uses, with no
        persona in it. One screen rather than two, because "name, prompt, voice"
        is the same three questions either way."""
        return templates.TemplateResponse(
            request=request,
            name="persona_edit.html",
            context={
                **await _shell(request, "personas"),
                **_edit_context(request, persona=None),
            },
        )

    @router.get(
        "/personas/{slug}/edit",
        response_class=HTMLResponse,
        summary="Edit one persona's prompt",
    )
    async def persona_edit(request: Request, slug: str) -> HTMLResponse:
        """Edit the prompt — the thing a persona actually is.

        A persona whose ``persona.toml`` is broken still opens here, with the
        prompt file's text if it has one: the operator who came to fix it is the
        one who most needs the box.
        """
        _persona_dir(slug)
        return templates.TemplateResponse(
            request=request,
            name="persona_edit.html",
            context={
                **await _shell(request, "personas"),
                **_edit_context(request, persona=_persona_form(slug)),
            },
        )

    def _persona_form(
        slug: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        pauses: str | None = None,
        prefix: str | None = None,
        memory: bool | None = None,
        workspace: bool | None = None,
        workspace_pins: str | None = None,
        thinking: bool | None = None,
    ) -> dict[str, Any]:
        """One persona in the shape the edit form renders.

        ``name`` and ``prompt`` override what is on disk so a refused save
        re-renders what was typed rather than silently reverting it — the same
        rule :func:`retention_rows` follows, for the same reason.
        """
        try:
            loaded = personas.load(slug)
        except PersonaError as exc:
            directory = personas.resolve_dir(slug)
            text = ""
            for filename in PROMPT_FILENAMES:
                candidate = directory / filename
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    break
            return {
                "slug": slug,
                "name": name if name is not None else slug,
                "prompt": prompt if prompt is not None else text,
                "voice": "",
                "voice_value": "",
                # A persona that will not load has no readable metadata, so the
                # box is empty rather than wrong. Saving from here writes what
                # is in it, which is the operator's own copy of the file.
                "pauses": pauses if pauses is not None else "",
                # A persona that will not load has no readable prefix either,
                # so the box is empty rather than wrong, for the same reason.
                "prefix": prefix if prefix is not None else "",
                # A persona whose metadata will not parse has no readable
                # switch either. Defaulting to off matches what the switch
                # itself means when it cannot be read at all (absent or
                # broken both mean off, per `Persona.memory_enabled`).
                "memory": memory if memory is not None else False,
                # A persona that will not load has no readable workspace switch
                # either. Defaulting to off matches what the switch itself
                # means when it cannot be read at all (absent means off, per
                # contract §0.1) — the opposite of memory's own default here,
                # because the two contracts made opposite decisions.
                "workspace": workspace if workspace is not None else False,
                "workspace_pins": workspace_pins if workspace_pins is not None else "",
                # A persona whose metadata will not parse has no readable
                # thinking switch either — defaulting to on for the same
                # reason `memory` above does (contract §13 D: absent or
                # broken both mean on).
                "thinking": thinking if thinking is not None else True,
                # Read off the file, not through the store that just refused it:
                # the broken thing may be this very section, and the form has to
                # show what is there or the next save would silently drop it.
                **connection_from_file(directory),
                "problem": exc.spoken_message,
            }
        return {
            "slug": slug,
            "name": name if name is not None else loaded.display_name,
            "prompt": prompt if prompt is not None else loaded.system_prompt,
            "voice": persona_voice_label(loaded.voice_engine, loaded.voice_name),
            "voice_value": voice_value(loaded.voice_engine, loaded.voice_name),
            "pauses": pauses if pauses is not None else pause_lines(loaded.speech_pauses),
            "prefix": prefix if prefix is not None else loaded.prompt_prefix,
            "memory": memory if memory is not None else loaded.memory_enabled,
            # `getattr` with a default rather than a plain attribute read:
            # workspace contract §5/§6 add these to `Persona` as a separate
            # change (this screen only writes and re-displays the two keys),
            # so a store built before that change still has a persona with
            # neither attribute, and the switch reads as off / no pins —
            # exactly what an absent key already means.
            "workspace": (
                workspace if workspace is not None else getattr(loaded, "workspace_enabled", False)
            ),
            "workspace_pins": (
                workspace_pins
                if workspace_pins is not None
                else ", ".join(getattr(loaded, "workspace_pins", ()) or ())
            ),
            # Same `getattr` reasoning as `workspace` above: thinking
            # contract §13 D adds this to `Persona` as a separate change, so
            # a store built before that lands still has a persona with no
            # such attribute — reading as on, exactly what an absent key
            # already means.
            "thinking": (
                thinking if thinking is not None else getattr(loaded, "thinking_enabled", True)
            ),
            **connection_fields(loaded.connection),
            "problem": None,
        }

    def _write_persona(
        directory: Path,
        *,
        display_name: str,
        prompt: str,
        voice: str | None = None,
        pauses: tuple[SpeechPause, ...] | None = None,
        prefix: str | None = None,
        memory: bool = False,
        workspace: bool = False,
        workspace_pins: tuple[str, ...] = (),
        thinking: bool = True,
        connection: LLMSettings | None = None,
        own_connection: bool = False,
        key_secret: str | None = None,
    ) -> str:
        """Write a persona's prompt and its display name. Returns the file used.

        The prompt goes into whichever of :data:`PROMPT_FILENAMES` already exists,
        so editing a persona that ships ``prompt.md`` does not leave two prompt
        files with the loader silently preferring the other one. A new persona
        gets the first name on that list.

        ``persona.toml`` is read, updated and written back rather than replaced:
        the file is the operator's, and a key this form has no box for — a voice
        suggestion, a description — must survive a save that said nothing about
        it.
        """
        directory.mkdir(parents=True, exist_ok=True)
        target = next(
            (directory / name for name in PROMPT_FILENAMES if (directory / name).is_file()),
            directory / PROMPT_FILENAMES[0],
        )
        target.write_text(prompt.strip() + "\n", encoding="utf-8")

        metadata_path = directory / METADATA_FILENAME
        existing: dict[str, Any] = {}
        if metadata_path.is_file():
            try:
                with metadata_path.open("rb") as handle:
                    existing = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError):
                # A metadata file that cannot be parsed is exactly what the
                # operator came here to fix. Starting from an empty table
                # replaces the broken one rather than refusing the save.
                existing = {}
        existing["display_name"] = display_name
        if voice is not None:
            # ``None`` means the form said nothing about the voice; an empty
            # string means the operator chose "no voice", which removes the
            # table rather than writing an engine with no name in it.
            engine, name = split_voice_value(voice)
            if engine and name:
                existing["voice"] = {"engine": engine, "name": name}
            else:
                existing.pop("voice", None)
        if pauses is not None:
            # An emptied box removes the table rather than writing an empty
            # one, so a persona with no pauses is the same file it was before
            # the box existed — and reads back as today's pacing exactly.
            speech = existing.get(SPEECH_TABLE)
            speech = dict(speech) if isinstance(speech, dict) else {}
            if pauses:
                speech[PAUSES_KEY] = pause_table(pauses)
            else:
                speech.pop(PAUSES_KEY, None)
            if speech:
                existing[SPEECH_TABLE] = speech
            else:
                existing.pop(SPEECH_TABLE, None)
        if prefix is not None:
            # An emptied box removes the key rather than writing an empty
            # string, so a persona with no prefix is the same file it was
            # before the box existed (prompt-prefix contract §3).
            stripped = prefix.strip()
            if stripped:
                existing["prompt_prefix"] = stripped
            else:
                existing.pop("prompt_prefix", None)
        # MEMORY (memory contract §9). The checkbox is always on the form, so
        # every save knows the intended state. Absence already means off
        # (`Persona.memory_enabled`'s own default), so a ticked box writes
        # `memory = true` explicitly and an unticked one writes nothing —
        # there is no "leave alone" state to preserve here, the same shape
        # the workspace switch just below already has.
        if memory:
            existing["memory"] = True
        else:
            existing.pop("memory", None)
        # WORKSPACE (workspace contract §5/§0.1). Same shape as memory just
        # above: absence already means off, so a ticked box writes
        # `workspace = true` and an unticked one writes nothing — there is
        # no "leave alone" state to preserve here either, the checkbox is
        # always on the form.
        if workspace:
            existing["workspace"] = True
        else:
            existing.pop("workspace", None)
        # Pins (contract §6): a comma-separated box, a list in the file. An
        # emptied box removes the key rather than writing an empty list, the
        # same rule the pauses and prefix boxes follow, for the same reason —
        # a persona with no pins is the same file it was before the box
        # existed.
        if workspace_pins:
            existing["workspace_pins"] = list(workspace_pins)
        else:
            existing.pop("workspace_pins", None)
        # THINKING (thinking contract §13 D). Same shape as Memory above:
        # the checkbox is always on the form, checked writes nothing —
        # absence already means on — and unchecked writes `thinking = false`
        # explicitly.
        if thinking:
            existing.pop("thinking", None)
        else:
            existing["thinking"] = False
        # The connection, written as the three fields the form has controls for
        # and nothing else; whatever else is in the section survives a save that
        # said nothing about it, because `persona.toml` is the operator's file.
        #
        # `key_secret` is a NAME. The value went into the secret store before
        # this function was called and is not written here, is not written to
        # `core.toml`, and is not written anywhere else: there is one convention
        # for a credential and it is a name (ADR-0025 §1). ``None`` removes the
        # reference, which is what the remove control produces.
        #
        # "Use the system default" REMOVES the section rather than writing the
        # system's values into it. Absence is the only thing that means "follow
        # the system", so copying today's default in would pin the persona to it.
        if own_connection and connection is not None:
            section = existing.get(LLM_TABLE)
            section = dict(section) if isinstance(section, dict) else {}
            section["base_url"] = connection.base_url
            section["model"] = connection.model
            if key_secret:
                section["api_key_secret"] = key_secret
            else:
                section.pop("api_key_secret", None)
            existing[LLM_TABLE] = section
        elif not own_connection:
            existing.pop(LLM_TABLE, None)
        metadata_path.write_text(tomli_w.dumps(existing), encoding="utf-8")
        return target.name

    class _Submission(NamedTuple):
        """One submitted form, parsed but not yet written.

        A named tuple rather than a six-then-eight element one: the connection
        added three more values to carry, and a positional unpack of eight is
        how the wrong one ends up in the wrong box.
        """

        display_name: str
        prompt: str
        voice: str
        typed_pauses: str
        pauses: tuple[SpeechPause, ...]
        connection: ConnectionSubmission
        #: The key box. Its value is a ``SecretStr``, so this tuple's ``repr``
        #: — and any traceback or log line that carries it — shows asterisks.
        key: KeySubmission
        typed_prefix: str
        memory: bool
        workspace: bool
        typed_workspace_pins: str
        workspace_pins: tuple[str, ...]
        thinking: bool
        refusal: str | None

    class _KeyPlan(NamedTuple):
        """What this save does about the key: write, keep, or forget."""

        secret_name: str | None
        """The name ``[llm].api_key_secret`` should hold afterwards."""

        forget: str | None
        """A stored secret to delete once the file has been written."""

        refusal: str | None

    def _plan_key(slug: str, stored_name: str, mode: str, key: KeySubmission) -> _KeyPlan:
        """Work out the key half of a save, storing a typed value as it goes.

        The value is written **before** ``persona.toml`` is, never after: the
        other order leaves the file naming a secret that is not there yet, and
        the worst this order can leave behind is a secret nothing references —
        which costs nothing and is picked up by the retry.
        """
        if mode != CONNECTION_MODE_CUSTOM:
            # No connection of its own means no key of its own: the section
            # goes and takes the reference with it, and a key typed into a box
            # the stylesheet had hidden goes nowhere — exactly as the address
            # and model boxes already do. The stored value is left on the
            # volume unless the remove control was ticked, because choosing the
            # system default says nothing about a credential.
            return _KeyPlan(None, stored_name if key.clearing else None, None)
        if key.typed:
            # A typed key wins over a ticked remove: the two together are
            # contradictory, and the reading that keeps a working credential is
            # the safe one when either outcome is plainly on the page after the
            # save. A connection that already names a secret keeps that name,
            # so an operator's hand-written one is not orphaned by a rename.
            name = stored_name or persona_key_name(slug)
            refusal = store_key(ctx.layout, name, key.value)
            if refusal is not None:
                return _KeyPlan(None, None, refusal)
            return _KeyPlan(name, None, None)
        if key.clearing:
            return _KeyPlan(None, stored_name or None, None)
        # An empty box has said nothing about the key, so the name already in
        # the file is carried straight through. That is the whole point of the
        # field: saving a corrected prompt must not silently unset the
        # credential and leave the character unable to reach its model.
        return _KeyPlan(stored_name or None, None, None)

    def _persona_submission(form: Mapping[str, Any] | Any) -> _Submission:
        """Everything the form said, and the first thing wrong with it.

        The speech pauses come back **both** ways on purpose: as the operator's
        own lines, so a refused save re-renders what they wrote rather than a
        tidied version of it, and as the parsed list, so the writer never
        parses anything twice. The connection carries its typed boxes for the
        same reason.

        The voice is one option value, ``engine:voice``, and **both halves are
        checked against the voice module's own id rules before either is
        written**. A value that is not a pair of ids is not a voice, and an
        empty one is the operator choosing none — never a path, never anything
        that reaches a join.
        """
        display_name = str(form.get("name") or "").strip()[:MAX_PERSONA_NAME_CHARS]
        prompt = str(form.get("prompt") or "")
        typed_pauses = str(form.get("pauses") or "")[:MAX_PAUSES_CHARS]
        typed_prefix = str(form.get("prompt_prefix") or "")
        typed_workspace_pins = str(form.get("workspace_pins") or "")[:MAX_WORKSPACE_PINS_CHARS]
        # A checkbox that is absent from the POST means unchecked — there is
        # no marker field here, unlike the Core settings switches: this form
        # always renders the box, so its presence or absence already says
        # everything a save needs to know. Same rule for the workspace switch
        # below, whose default now matches memory's (both absent means off),
        # and for the thinking switch, whose own default is still the
        # opposite (absent means on).
        memory_checked = bool(str(form.get("memory") or ""))
        workspace_checked = bool(str(form.get("workspace") or ""))
        thinking_checked = bool(str(form.get("thinking") or ""))
        connection = read_connection_form(form)
        key = read_key_form(form)
        raw_voice = str(form.get("voice") or "").strip()
        voice = ""
        if raw_voice:
            engine, name = split_voice_value(raw_voice)
            try:
                voice = voice_value(require_engine_id(engine or ""), require_voice_id(name or ""))
            except PackageRejected:
                return _Submission(
                    display_name,
                    prompt,
                    "",
                    typed_pauses,
                    (),
                    connection,
                    key,
                    typed_prefix,
                    memory_checked,
                    workspace_checked,
                    typed_workspace_pins,
                    (),
                    thinking_checked,
                    PERSONA_VOICE_UNKNOWN,
                )
        # Parsed before the other checks so that a refusal from this box is
        # reported whichever else is also wrong; it says nothing about the
        # prompt and nothing here is written until every check has passed.
        pauses, pauses_refusal = parse_pause_lines(typed_pauses)
        workspace_pins, workspace_pins_refusal = parse_workspace_pins(typed_workspace_pins)
        made = partial(
            _Submission,
            display_name,
            prompt,
            voice,
            typed_pauses,
            pauses,
            connection,
            key,
            typed_prefix,
            memory_checked,
            workspace_checked,
            typed_workspace_pins,
            workspace_pins,
            thinking_checked,
        )
        if not display_name:
            return made(PERSONA_NAME_REQUIRED)
        if not prompt.strip():
            return made(PERSONA_PROMPT_REQUIRED)
        if len(prompt) > MAX_PROMPT_CHARS:
            return made(PERSONA_PROMPT_TOO_LONG.format(limit=MAX_PROMPT_CHARS))
        if len(typed_prefix) > MAX_PREFIX_CHARS:
            return made(PERSONA_PREFIX_TOO_LONG.format(limit=MAX_PREFIX_CHARS))
        # After the prompt checks, because a persona is its prompt and that is
        # the refusal worth reading first when both boxes are wrong.
        return made(pauses_refusal or workspace_pins_refusal or connection.refusal or key.refusal)

    async def _edit_page(
        request: Request,
        persona: dict[str, Any] | None,
        result: dict[str, str],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="persona_edit.html",
            context={
                **await _shell(request, "personas"),
                **_edit_context(request, persona=persona, save_result=result),
            },
        )

    @router.post("/personas", response_class=HTMLResponse, summary="Create a persona")
    async def persona_create(request: Request) -> HTMLResponse:
        """Write a new persona folder — a prompt file and a ``persona.toml``.

        There is no ``POST /admin/api/personas``: the JSON API lists, reads and
        selects, and spec §5.5 defines a persona as *files in a folder*, which is
        what this writes. The name check is the store's own ``resolve_dir``, so a
        name that could escape the personas directory is refused by the same code
        that refuses it to the agent loop.
        """
        user = require_user(request)
        form = await request.form()
        sub = _persona_submission(form)
        display_name, prompt, voice = sub.display_name, sub.prompt, sub.voice
        refusal = sub.refusal
        slug = persona_slug(display_name)
        typed = {
            "slug": slug,
            "name": display_name,
            "prompt": prompt,
            "voice": "",
            "voice_value": voice,
            "pauses": sub.typed_pauses,
            "prefix": sub.typed_prefix,
            "memory": sub.memory,
            "workspace": sub.workspace,
            "workspace_pins": sub.typed_workspace_pins,
            "thinking": sub.thinking,
            "connection_mode": sub.connection.mode,
            "connection_address": sub.connection.typed_address,
            "connection_model": sub.connection.typed_model,
            # A persona that does not exist yet names no secret, so the box
            # re-renders empty on a refusal — which is also the only honest
            # thing it could say. The typed key is not carried back: this form
            # never renders a key, not even one it has just been handed.
            "connection_key_secret": "",
            "problem": None,
        }

        if refusal is None and not slug:
            refusal = PERSONA_NAME_UNUSABLE.format(name=display_name)
        if refusal is None:
            try:
                directory = personas.resolve_dir(slug)
            except PersonaError as exc:
                refusal = exc.spoken_message
            else:
                if directory.exists():
                    refusal = PERSONA_EXISTS.format(slug=slug)
        # Last, because it is the only check that writes anything: a persona
        # refused for its name must not leave a secret behind for a folder that
        # was never created.
        plan = _KeyPlan(None, None, None)
        if refusal is None:
            plan = _plan_key(slug, "", sub.connection.mode, sub.key)
            refusal = plan.refusal
        if refusal is not None:
            return await _edit_page(request, typed, {"kind": "invalid", "message": refusal})

        _write_persona(
            directory,
            display_name=display_name,
            prompt=prompt,
            voice=voice,
            pauses=sub.pauses,
            prefix=sub.typed_prefix,
            memory=sub.memory,
            workspace=sub.workspace,
            workspace_pins=tuple(sub.workspace_pins),
            thinking=sub.thinking,
            connection=sub.connection.connection,
            own_connection=sub.connection.mode == CONNECTION_MODE_CUSTOM,
            key_secret=plan.secret_name,
        )
        forget_key(ctx.layout, plan.forget)
        personas.invalidate()
        await _record_change(
            audit,
            user,
            action="personas.create",
            outcome=AuditOutcome.SUCCESS,
            # The identifier, not the prompt: a persona's text is the operator's
            # writing and belongs in the file, not duplicated into an audit
            # record that ends up in backups (spec §7).
            detail={"persona": slug},
        )
        return await _personas_page_with(
            request,
            {
                "kind": "saved",
                "message": (
                    f"Created {display_name}. Its identifier is “{slug}” — that is what "
                    "a client or an access key names to select it."
                ),
            },
        )

    @router.post(
        "/personas/{slug}", response_class=HTMLResponse, summary="Save one persona"
    )
    async def persona_save(request: Request, slug: str) -> HTMLResponse:
        """Save the prompt and the display name. **The identifier does not move.**

        See :data:`PERSONA_IDENTIFIER_FIXED`: an access key stores the identifier,
        and renaming the folder underneath one would leave that key naming a
        persona that no longer exists — a client that stops answering, not one
        that changes character.
        """
        user = require_user(request)
        directory = _persona_dir(slug)
        form = await request.form()
        sub = _persona_submission(form)
        if sub.refusal is not None:
            typed = _persona_form(
                slug,
                name=sub.display_name,
                prompt=sub.prompt,
                pauses=sub.typed_pauses,
                prefix=sub.typed_prefix,
                memory=sub.memory,
                workspace=sub.workspace,
                workspace_pins=sub.typed_workspace_pins,
                thinking=sub.thinking,
            )
            # The boxes keep what was typed; the key state stays whatever
            # `_persona_form` read off the file, because nothing was written.
            typed["connection_mode"] = sub.connection.mode
            typed["connection_address"] = sub.connection.typed_address
            typed["connection_model"] = sub.connection.typed_model
            typed["voice_value"] = sub.voice
            return await _edit_page(
                request, typed, {"kind": "invalid", "message": sub.refusal}
            )
        # Read off the file rather than through the store, for the reason
        # `connection_from_file` gives: the persona may be one that will not
        # load, and the name of its key is still the name of its key.
        stored_key = str(connection_from_file(directory)["connection_key_secret"])
        plan = _plan_key(slug, stored_key, sub.connection.mode, sub.key)
        if plan.refusal is not None:
            typed = _persona_form(slug, name=sub.display_name, prompt=sub.prompt)
            typed["connection_mode"] = sub.connection.mode
            typed["connection_address"] = sub.connection.typed_address
            typed["connection_model"] = sub.connection.typed_model
            typed["voice_value"] = sub.voice
            typed["pauses"] = sub.typed_pauses
            typed["prefix"] = sub.typed_prefix
            typed["memory"] = sub.memory
            typed["workspace"] = sub.workspace
            typed["workspace_pins"] = sub.typed_workspace_pins
            typed["thinking"] = sub.thinking
            return await _edit_page(
                request, typed, {"kind": "invalid", "message": plan.refusal}
            )
        _write_persona(
            directory,
            display_name=sub.display_name,
            prompt=sub.prompt,
            voice=sub.voice,
            pauses=sub.pauses,
            prefix=sub.typed_prefix,
            memory=sub.memory,
            workspace=sub.workspace,
            workspace_pins=tuple(sub.workspace_pins),
            thinking=sub.thinking,
            connection=sub.connection.connection,
            own_connection=sub.connection.mode == CONNECTION_MODE_CUSTOM,
            key_secret=plan.secret_name,
        )
        # After the write and never before: the other order would leave
        # `persona.toml` naming a file that is no longer there.
        forget_key(ctx.layout, plan.forget)
        personas.invalidate()
        await _record_change(
            audit,
            user,
            action="personas.update",
            outcome=AuditOutcome.SUCCESS,
            detail={"persona": slug},
        )
        return await _edit_page(
            request,
            _persona_form(slug),
            {
                "kind": "saved",
                "message": (
                    "Saved. A persona reloads from disk on the next turn, so this "
                    "applies without a restart."
                ),
            },
        )
