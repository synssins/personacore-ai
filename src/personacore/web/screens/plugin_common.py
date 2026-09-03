"""What the plugin screens share.

The plugins list, the per-plugin page, its repeating-group fragments and the
plugin-wide health and output pages are separate files, but they name the same
plugin, read the same schema and reach the same JSON API handlers. These are
the pieces that would otherwise be written out once per file.
"""

from __future__ import annotations

import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status

from personacore.admin.config_io import ConfigRejected
from personacore.admin.models import (
    PluginConfigResponse,
)
from personacore.admin.plugin_config_io import (
    PluginConfigUnsafe,
    plugin_config_path,
)
from personacore.admin.plugin_form import (
    MAX_ENTRIES,
)
from personacore.admin.plugin_lookup import authorise_lookups
from personacore.admin.plugin_schema import (
    FieldKind,
    PluginSchema,
    SchemaField,
    SchemaRefused,
    read_plugin_schema,
)
from personacore.config.appdata import AppdataError, AppdataLayout
from personacore.config.secrets import SecretError, SecretStore
from personacore.plugins.discovery import MANIFEST_FILENAME
from personacore.plugins.packages import (
    require_plugin_name,
)
from personacore.web.plugin_page import (
    LOOKUP_UNLOADED_NOTE,
)
from personacore.web.shared import (
    NO_PLUGIN_OPERATIONS,
    api_handler,
)

PLUGIN_SCREEN = "/admin/plugins/{name}"
"""Where the per-plugin screen lives — the design's own path.

It sat one segment deeper, at ``.../settings``, for as long as ADR-0007's test
surface answered ``/admin/plugins/<name>``: taking the path while that page was
still mounted would have silently deleted a screen that had to keep working.
The test surface is gone, so the screen is where the design draws it.

The generated form saves to ``POST /admin/plugins/<name>/settings`` — the
screen's own path plus the noun for what the form writes. The raw editor keeps
``POST .../settings/raw`` and the fragments keep ``.../settings/<key>/...``,
because those are about the settings rather than about the screen.
"""


MAX_INDEX_DIGITS = 6
"""How long a repeating-group position may be in a fragment URL.

A position is a place on one page, so six digits is already far more entries
than :data:`~personacore.admin.plugin_form.MAX_ENTRIES` allows. The bound is
here because the value arrives in a URL, and something from outside gets a
bound before it gets a use (spec section 7)."""


def plugin_name_or_404(name: str) -> str:
    """The manifest's own name rule, before the name reaches a path.

    A name that is not a plugin name is a 404 rather than a 422: from
    outside, "that is not a plugin" and "there is no such plugin" are the
    same answer, and the difference is only useful to somebody probing.
    """
    try:
        return require_plugin_name(name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


def op(request: Request, operation: str) -> Callable[..., Awaitable[Any]]:
    handler = api_handler(request.app, operation)
    if handler is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NO_PLUGIN_OPERATIONS)
    return handler


def can(request: Request, *operations: str) -> bool:
    return all(api_handler(request.app, one) is not None for one in operations)


def secret_names(layout: AppdataLayout, plugin: str) -> list[str]:
    """The names in **one plugin's own** secret namespace, or none.

    ADR-0015's picker: a settings field may hold the *name* of a secret, and
    this is the list the page offers for it. Under ADR-0025 that list is the
    plugin's namespace and nothing else.

    **It used to be the whole store, and that was the vulnerability the ADR
    exists to remove.** ``SecretStore.available`` now takes an owner and
    defaults to the core's namespace, so the call that had no argument was
    quietly showing every core-owned name — the LLM API key among them — in a
    dropdown on a third-party plugin's settings page, and
    :func:`~personacore.admin.plugin_form.validate_submission` was accepting
    those names as valid references. That is precisely the enumeration ADR-0025
    §1 kills: "plugin B cannot name plugin A's secret, because the name is not
    global". Passing the plugin is what makes the sentence true here.

    ``plugin`` comes from the screen's own already-validated path segment, never
    from a form field, so nothing a caller submits can steer which namespace is
    listed. Names only — there is no call in this module that returns a value
    (spec section 7, ADR-0025 §5). A namespace that cannot be listed yields an
    empty list rather than an error page: the rest of the plugin's settings are
    still editable, which is the whole point of the screen.
    """
    try:
        return list(SecretStore(layout).available(plugin))
    except Exception:  # noqa: BLE001 - one unreadable folder, not a dead page
        return []


def plugin_schema(
    layout: AppdataLayout, name: str
) -> tuple[PluginSchema | None, str]:
    """The plugin's schema, or ``(None, why not)``.

    A refusal is a sentence rather than an exception the page dies on: a
    plugin whose schema is broken is usually a plugin that failed to load,
    which is exactly when this screen is needed.
    """
    try:
        return read_plugin_schema(layout, name), ""
    except SchemaRefused as exc:
        return None, exc.message
    except ConfigRejected as exc:
        return None, exc.message


def authorise(schema: PluginSchema, view: Any) -> list[str]:
    """ADR-0016's gate, applied before anything is rendered.

    A lookup the plugin's manifest does not permit is removed from the
    schema here, so the template has no branch that could draw a search box
    for it. A plugin the scan does not know has no manifest to check
    against, so every lookup is removed and the reason said out loud - on
    the one page an operator opens *because* the plugin is broken, silence
    would be the worst answer.
    """
    if view is not None:
        return authorise_lookups(schema, view.tools)
    declared = [field.key for field in schema.fields if field.lookup is not None]
    for field in schema.fields:
        field.lookup = None
    if not declared:
        return []
    return [LOOKUP_UNLOADED_NOTE.format(fields=", ".join(declared))]


async def plugin_config(request: Request, name: str) -> PluginConfigResponse | None:
    """The plugin's ``config.toml``, through the JSON API's own reader.

    ``None`` when this application has no plugin API to read it with. The
    page then renders its health, its switches and the reason the settings
    are not editable, rather than answering 503 — a screen that disappears
    because one control has no backend is the shape of failure this
    surface's ``later`` marking exists to avoid.
    """
    handler = api_handler(request.app, "read_config")
    if handler is None:
        return None
    return await handler(name=name)


def group_field(schema: PluginSchema, key: str) -> SchemaField:
    """The repeating group a fragment URL named, or 404.

    The key arrives in a path segment, so it is matched against the schema's
    own field names before it is used for anything at all (spec section 7).
    """
    field = next(
        (
            one
            for one in schema.fields
            if one.key == key and one.kind is FieldKind.ENTRY_GROUP
        ),
        None,
    )
    if field is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{key!r} is not a set of entries on this plugin's form.",
        )
    return field


def entry_index(raw: str) -> int:
    """A page position out of a fragment URL, bounded.

    A position is a short run of ASCII digits and nothing else. It names
    inputs on one page and never reaches the file - the key that does is the
    name the operator typed, checked by ``require_entry_key`` on save.
    """
    if not (raw.isascii() and raw.isdigit()) or len(raw) > MAX_INDEX_DIGITS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That is not an entry.")
    index = int(raw)
    if index > MAX_ENTRIES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A form accepts at most {MAX_ENTRIES} entries.",
        )
    return index


# ---------------------------------------------------------------------------
# Credentials a plugin asked for — ADR-0025 section 4
# ---------------------------------------------------------------------------
#
# The manifest naming a secret is a **request**, not an entitlement, so the
# screens treat it as one: the name is shown, a field appears beside it, and
# what the operator pastes is stored in that plugin's own namespace. Nothing on
# these screens reads a value back — every function below deals in names and in
# the boolean "is there one yet", which is the whole of what
# :meth:`~personacore.config.secrets.SecretStore.has` will tell a caller that is
# not the plugin itself (ADR-0025 section 5).
#
# **Why the manifest and the store, rather than the supervisor.** Part B carries
# the same fact as `PluginHealth.waiting_for_secrets`, and that is the right
# source for a *running* core. It is not a source these screens can use on its
# own: a plugin that is switched off, one this core was assembled without a
# supervisor for, and one whose folder arrived a moment ago all have no
# supervisor answer at all, and every one of them can still be missing a
# credential the operator is standing in front of a field for. Deriving it from
# the two things that are always on disk — what the manifest asked for, and what
# the namespace holds — means the field that is drawn and the state word beside
# it can never disagree, because they are computed from one list.

SECRET_FIELD_PREFIX = "secret."  # noqa: S105 - a form field prefix, not a credential
"""How a credential field is named in a form post.

The suffix is the secret's own name. It is never trusted on the way back in:
:func:`store_secrets` matches each posted field against the names *this
plugin's manifest declared* and ignores anything else, so a field somebody
added to the page cannot write into a namespace under a name the plugin never
asked for.
"""

WAITING_WORD = "waiting"
"""The word an operator sees on a row or a page for this state.

Not "failed", not "error", not "missing". A plugin nobody has given an API key
to has not gone wrong — it is waiting for something only a person can supply,
and the fix is a paste rather than a bug report (ADR-0025 section 4). The
sentence beside the word always names *what* it is waiting for, because
"waiting" on its own is the kind of status that gets ignored.
"""

WAITING_ONE = (
    "Waiting for a credential: {names} — nobody has supplied it yet. Paste the "
    "value into the field below and it starts."
)

WAITING_MANY = (
    "Waiting for credentials: {names} — nobody has supplied them yet. Paste "
    "the values into the fields below and it starts."
)

SECRET_STORED = "A credential is stored."  # noqa: S105 - a sentence, not a credential
"""What the page says about a secret that exists. Deliberately not its length,
not a masked stand-in and not the first characters — every one of those is the
value leaking at a lower resolution (ADR-0025 section 5)."""

SECRET_NONE_YET = "None yet."  # noqa: S105 - a sentence, not a credential
"""And about one that does not."""

SECRET_OPTIONAL_WORD = "optional"  # noqa: S105 - a word, not a credential
"""Drawn beside the label of a request carrying ``required = false``.

Without it the two kinds of box look identical, and an operator who does not
have the value would have no way to tell that leaving it empty costs them
nothing (ADR-0026)."""

SECRET_OPTIONAL_NOTE = "Optional — fill it in only if you need it."  # noqa: S105
"""And the sentence under that box, in place of the waiting state.

An optional credential nobody supplied is never a *waiting* plugin, so the row
must not read like one: "None yet." beside a box on a page full of things that
are waiting is the same word doing two jobs."""

SECRET_NO_DESCRIPTION = "The plugin's author did not say what this is for."  # noqa: S105, E501 - a sentence, not a credential
"""When there is no description to show. Said out loud rather than left blank:
an unexplained box asking for a credential is one an operator should hesitate
over, and the honest thing is to tell them the author explained nothing."""

SECRETS_STORED_NOTE = (
    "Stored {names} for {plugin}. Never shown again — paste a new value to "
    "replace it."
)

SECRETS_NONE_SUBMITTED = "Nothing was stored — every field was left empty."


def plugin_manifest_path(layout: AppdataLayout, name: str) -> Path:
    """``<plugin folder>/manifest.toml``, through the config file's own checks.

    Derived from
    :func:`~personacore.admin.plugin_config_io.plugin_config_path` for exactly
    the reason :func:`~personacore.admin.plugin_schema.plugin_schema_path` is:
    that function already refuses a name that is not a plugin name and resolves
    the folder through ``require_inside``, which follows symlinks. This adds the
    same "directly inside the resolved folder" check on top. A second path
    routine into the same directory is how one of them ends up missing a check
    the other has.
    """
    config = plugin_config_path(layout, name)
    candidate = config.with_name(MANIFEST_FILENAME)
    try:
        resolved = layout.require_inside(candidate, what=f"The manifest for {name!r}")
    except AppdataError as exc:
        raise PluginConfigUnsafe(str(exc), []) from exc
    if resolved.parent != config.parent:
        raise PluginConfigUnsafe(
            f"The manifest for {name!r} does not sit inside that plugin's folder.", []
        )
    return resolved


def declared_secret_requests(layout: AppdataLayout, name: str) -> list[dict[str, Any]]:
    """One plugin's credential requests, in the manifest's order.

    ``[{"name", "description", "required"}]``, read straight from
    ``permissions.secrets`` rather than from the scan, because the scan's
    :class:`~personacore.admin.models.PluginView` does not carry them and a
    plugin whose manifest was refused has no view at all — while still being a
    plugin whose folder is on disk with a request in it.

    Read as raw TOML rather than through
    :class:`~personacore.contracts.manifest.PluginManifest` for the same
    reason: a manifest with an unrelated field wrong still has a credential
    somebody is standing in front of a box for. That means the shapes below
    cannot be assumed, so each key is checked — a table with no ``description``
    is drawn with :data:`SECRET_NO_DESCRIPTION` rather than dropped, and
    ``required`` defaults to ``True`` here exactly as it does in the contract.

    A bare string is a contract-1.x manifest (ADR-0026) and is **skipped**: the
    loader has already refused the plugin in words that say what to edit, and
    drawing a box for a request this core will not honour would be the page
    contradicting the refusal above it.

    A ``required`` that is not a genuine TOML boolean is skipped for exactly the
    same reason (the 2026-08 security review). The loader is the authority on what ``required``
    means, and since
    :data:`~personacore.contracts.manifest.REQUIRED_MUST_BE_BOOLEAN` it refuses
    the manifest outright rather than coercing — so ``required = "no"`` is a
    plugin that is not running, and there is no honest way to draw its
    credential as either required or optional. Before that, pydantic read the
    string as ``False`` and started the plugin while this function read it as
    "not literally ``false``, so required" and drew *waiting for a credential*
    over a plugin that was already serving. The three values a manifest can
    express are now ``true``, ``false`` and *refused*, and both sides read all
    three the same way.

    Anything unreadable is an empty list. A manifest that will not parse is
    already reported on this page by the scan, in its own words; a second
    rendering of the same failure as "no credentials" would be a page arguing
    with itself.
    """
    try:
        path = plugin_manifest_path(layout, name)
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - see docstring
        return []
    permissions = raw.get("permissions")
    if not isinstance(permissions, dict):
        return []
    declared = permissions.get("secrets")
    if not isinstance(declared, list):
        return []
    requests: list[dict[str, Any]] = []
    for item in declared:
        if not isinstance(item, dict):
            continue
        secret = item.get("name")
        if not isinstance(secret, str) or not secret:
            continue
        required = item.get("required", True)
        if not isinstance(required, bool):
            # See the docstring: the loader refuses this manifest, so the
            # request will never be honoured and there is nothing truthful to
            # draw. `isinstance` and not `is not False`, because the coercion
            # that gap allowed is the defect.
            continue
        description = item.get("description")
        text = description.strip() if isinstance(description, str) else ""
        requests.append(
            {
                "name": secret,
                "description": text or SECRET_NO_DESCRIPTION,
                "required": required,
            }
        )
    return requests


def declared_secrets(layout: AppdataLayout, name: str) -> list[str]:
    """The secret **names** one plugin's manifest asks for, in order.

    Required and optional alike: this is the gate
    :func:`store_secrets` writes through, and an operator whose instance does
    need the optional credential must be able to supply it.
    """
    return [request["name"] for request in declared_secret_requests(layout, name)]


def secret_requests(layout: AppdataLayout, name: str) -> list[dict[str, Any]]:
    """Every credential this plugin asked for, and whether it has one yet.

    ``[{"name", "description", "required", "stored"}]``, in the manifest's own
    order. This is the single list the field, the state word and the sentence
    are all drawn from, so the page cannot offer a box for something it says is
    supplied, or say "waiting" about a name it never draws a box for.

    **The description is the author's own, out of the manifest** (ADR-0026).
    It used to be scavenged from ``config.schema.json`` because
    ``permissions.secrets`` was a list of bare names with nowhere to write a
    sentence; contract 2.0 gave the request a ``description`` field and made it
    required, so there is one place to read it from and it is the place the
    author was asked to fill in.

    **No value is read.** ``stored`` comes from
    :meth:`~personacore.config.secrets.SecretStore.missing`, which answers in
    names and nothing else.
    """
    requests = declared_secret_requests(layout, name)
    if not requests:
        return []
    names = [request["name"] for request in requests]
    try:
        absent = set(SecretStore(layout).missing(names, plugin=name))
    except Exception:  # noqa: BLE001 - an unreadable store is not a dead page
        absent = set(names)
    return [{**request, "stored": request["name"] not in absent} for request in requests]


def waiting_for(requests: list[dict[str, Any]]) -> list[str]:
    """Which credentials are holding this plugin back — the names only.

    **Missing and required** (ADR-0026). An optional request nobody supplied is
    not in this list, because the plugin starts without it: a plugin shown as
    waiting for something it is already running without would be the page
    describing a state that does not exist.
    """
    return [
        item["name"]
        for item in requests
        if not item["stored"] and item.get("required", True)
    ]


def waiting_sentence(names: list[str]) -> str:
    """"Waiting for a credential: openweather_key", written out for a person."""
    template = WAITING_ONE if len(names) == 1 else WAITING_MANY
    return template.format(names=", ".join(names))


def store_secrets(
    layout: AppdataLayout, name: str, submitted: dict[str, str], declared: list[str]
) -> tuple[list[str], list[str]]:
    """Write the pasted credentials into this plugin's own namespace.

    Returns ``(names stored, refusals)`` — **names**, never values, because what
    comes back from here is rendered on a page and written to a log.

    ``declared`` is the gate. A field is stored only when its name is one this
    plugin's manifest asked for, so a form field added from outside cannot write
    into the namespace under a name the plugin never requested; and the
    namespace is the plugin's own, so it could not reach another plugin's
    credential even if it could name one (ADR-0025 section 1).

    An empty box is a skip rather than a delete: the operator who has one of two
    keys fills one field, and clearing the other would be an instruction they
    did not give.
    """
    allowed = set(declared)
    store = SecretStore(layout)
    stored: list[str] = []
    refusals: list[str] = []
    for field, value in submitted.items():
        if not field.startswith(SECRET_FIELD_PREFIX):
            continue
        secret = field[len(SECRET_FIELD_PREFIX) :]
        if secret not in allowed or not value.strip():
            continue
        try:
            store.set(secret, value, plugin=name)
        except SecretError as exc:
            # The store's own sentence, which names the secret and never its
            # value (ADR-0025 section 5).
            refusals.append(str(exc))
            continue
        stored.append(secret)
    return stored, refusals


def secret_fields_from(form: Any) -> dict[str, str]:
    """The credential fields out of a submitted form, as ``{field: value}``.

    Pulled out here so the install post and the plugin's own page read the same
    fields the same way. Values are held only long enough to be handed to
    :func:`store_secrets` and never put anywhere a renderer or a logger reaches.
    """
    return {
        key: value
        for key, value in form.multi_items()
        if isinstance(key, str)
        and key.startswith(SECRET_FIELD_PREFIX)
        and isinstance(value, str)
    }
