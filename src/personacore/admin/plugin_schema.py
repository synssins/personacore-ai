"""Reading a plugin's ``config.schema.json`` into renderable fields — ADR-0015.

ADR-0015 gives a plugin one optional file, ``config.schema.json``, describing
the settings table its ``config.toml`` already holds, and gives the core the job
of rendering a form from "the subset it understands". This module is that
reader: JSON in, a list of :class:`SchemaField` out. It renders nothing and
writes nothing — see :mod:`personacore.admin.plugin_form` for validating
submitted values and :mod:`personacore.web.plugin_page` for the page.

**The schema is data and is never executed.** ADR-0015: "No expressions, no
callbacks, no remote ``$ref``. A ``$ref`` outside the document is refused; the
schema is data." So:

* Any ``$ref`` that does not begin with ``#`` is refused outright, which covers
  every remote reference (``http://…``, ``file://…``) and every reference to
  another file on disk. Nothing is fetched, ever.
* An in-document ``#/…`` pointer is resolved by walking this document's own
  keys, with a depth cap and a cycle guard, because a schema that points at
  itself must fail as a refusal rather than as a recursion error.
* Unknown keywords are ignored rather than interpreted. ``x-`` keywords are
  carried through untouched on :attr:`SchemaField.extensions`; the two this
  core knows (:data:`SECRET_MARKER` and :data:`LOOKUP_MARKER`) are *parsed*
  here into plain data and authorised elsewhere — a schema never decides its
  own permissions.

**The supported subset is small and stays small** (ADR-0015, "Bounded on
purpose"): every construct rendered is one that must also be validated and
explained in plain English. Anything outside it becomes an
:class:`UnrenderableField` — named, with the reason — and is left to the raw
TOML editor. It is never silently dropped: a field the form does not show and
does not save is an operator's setting quietly lost.

**Secrets.** ADR-0015: "Never render a secret value. A field may be marked as
holding the *name* of a secret, and the UI offers the names in the store." The
marker is :data:`SECRET_MARKER` — ``"x-personacore-secret": true`` on a string
property. A property whose *name* ends in
:data:`~personacore.admin.config_io.SECRET_REFERENCE_SUFFIX` counts too, because
that suffix is already what the core means by "this names a secret" everywhere
else (``llm.api_key_secret``) and a second, disagreeing convention would be a
hole rather than a feature. Either way the field offers names and never a value.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from personacore.admin.config_io import SECRET_REFERENCE_SUFFIX
from personacore.admin.models import ConfigProblem
from personacore.admin.plugin_config_io import PluginConfigUnsafe, plugin_config_path
from personacore.config.appdata import AppdataError, AppdataLayout

SCHEMA_FILENAME = "config.schema.json"
"""ADR-0015: "A plugin may ship ``config.schema.json`` beside its
``config.toml``". Beside it, in the plugin's own folder — spec section 5.1 keeps
everything a plugin owns inside that folder and nowhere else."""

SECRET_MARKER = "x-personacore-secret"  # noqa: S105 - a schema keyword, not a credential
"""The keyword marking a string property as holding the *name* of a secret.

An ``x-`` keyword rather than a ``format``: ``format`` is an annotation with
published meanings in JSON Schema, and minting a private one there invites a
validator elsewhere to try to interpret it. ``x-`` is unambiguously ours and is
ignored by everything else.
"""

LOOKUP_MARKER = "x-personacore-lookup"
"""The keyword marking a property as fillable by one of the plugin's own tools
— ADR-0016's search-and-fill, and an ``x-`` keyword for the same reason
:data:`SECRET_MARKER` is one.

The value is an object:

``tool`` (required)
    The bare tool name as the plugin's ``manifest.toml`` declares it. The core
    calls **only** a tool named here, and only if the manifest gives it
    ``risk = "safe"`` — ADR-0016: "A config form must never be a route to a
    ``confirm``- or ``restricted``-level action."

``fill`` (required)
    ``{setting name: key in the result}``. The setting names are properties of
    the object the marked field belongs to: for a repeating group they are the
    *entry's* own fields, and anywhere else they are the marked field and its
    siblings. A name that is not a renderable field there is refused, so a
    typo cannot silently fill nothing.

``query_argument`` (default ``"query"``)
    The tool argument the typed text is passed as.

``results`` (default ``"results"``)
    Where the list of matches sits in what the tool returned. Ignored when the
    tool returns a bare list.

``label`` (optional)
    The result key shown as that row's text in the list. Without one the row is
    shown as its filled values, which is data either way — ADR-0016: results
    "are rendered as data in a list, never interpreted".
"""

MAX_SCHEMA_BYTES = 256 * 1024
"""Ceiling on ``config.schema.json``. The same quarter-megabyte the config
itself gets (``models.MAX_PLUGIN_CONFIG_CHARS``): far larger than any real
schema, and a limit has to exist because this file is read into memory on every
page render and its author is not the core."""

MAX_REF_DEPTH = 8
"""How many in-document ``$ref`` hops are followed before the schema is
refused. Bounded rather than guarded only by a cycle check, because a schema
1,000 hops deep is not a schema anyone wrote by hand."""

MAX_PROPERTIES = 200
"""Ceiling on properties read from one schema. A settings page with more fields
than this is unusable anyway, and the cap keeps one plugin from making the page
unrenderable for every other."""

MAX_ENUM_CHOICES = 200
"""Ceiling on a dropdown's options, for the same reason."""

MAX_ENTRY_FIELDS = 20
"""Ceiling on the fields inside one repeating-group entry.

An entry is a row on a page that already repeats; twenty columns is past the
point where the form is the wrong tool, and the cap keeps one entry from
multiplying into a page with thousands of inputs on it.
"""

LONG_TEXT_CHARS = 200
"""Above this ``maxLength``, a string renders as a multi-line box.

The plan (item 5) asks for "multi-line text for long values"; an author who
wants one regardless says ``"format": "textarea"``. A string with *no*
``maxLength`` stays single-line: an author who declared no limit has said
nothing about length, and guessing "probably long" from silence would put a
20-row box under a field holding a city name.
"""


class FieldKind(StrEnum):
    """What control renders a field. One member per construct ADR-0015 lists.

    A closed set on purpose: the page has a branch per member, the validator
    has a branch per member, and both must be exhaustive. Repeating groups
    (the plan's item 7) become a new member here rather than a special case
    anywhere else.
    """

    TOGGLE = "toggle"
    CHOICE = "choice"
    NUMBER = "number"
    TEXT = "text"
    TEXTAREA = "textarea"
    STRING_LIST = "string_list"
    SECRET_NAME = "secret_name"  # noqa: S105 - a control name, not a credential
    ENTRY_GROUP = "entry_group"
    """The plan's item 7: an object whose ``additionalProperties`` describes one
    entry, rendered as a repeatable set of entries each keyed by a name the
    operator supplies (weather's ``locations``)."""


class FieldLookup(BaseModel):
    """A field's declaration that one of the plugin's own tools can fill it.

    ADR-0016: "In ``config.schema.json``, a field (or group of fields) names one
    of the plugin's own tools as its lookup." This is that declaration, parsed
    — see :data:`LOOKUP_MARKER` for the keyword's exact shape.

    Parsed here and nowhere else, but **not** authorised here: whether the named
    tool is ``safe`` enough to wire is a question about the plugin's manifest,
    which this module does not read (see
    :func:`personacore.admin.plugin_lookup.wire_lookup`). Keeping the two apart
    means a schema can never be the thing that decides its own risk.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    """The bare tool name from the plugin's manifest, e.g. ``search_locations``."""

    fill: dict[str, str] = Field(default_factory=dict)
    """``{setting name: result key}``, checked against the fields being filled."""

    query_argument: str = "query"
    results_key: str = "results"
    label_key: str = ""


class SchemaField(BaseModel):
    """One renderable setting, flattened out of the schema for the page.

    Flattened deliberately: the template asks "what kind, what label, what
    bounds" and never walks a schema itself. That keeps the one place that
    interprets a plugin's file — this module — the one place to audit.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    """The property name, which is also the TOML key written back."""

    kind: FieldKind
    label: str
    """``title`` if the author gave one, else the key. ADR-0015: "``title`` and
    ``description`` become the label and help text"."""

    help: str = ""
    required: bool = False
    default: Any = None
    """The schema's ``default``, "offered" (ADR-0015) rather than imposed: it
    pre-fills an empty field and is never written on the operator's behalf."""

    choices: list[str] = Field(default_factory=list)
    """Dropdown options for :attr:`FieldKind.CHOICE`. Empty otherwise; a secret
    field's options come from the store at render time and never from here."""

    integer_only: bool = False
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None

    min_length: int | None = None
    max_length: int | None = None

    min_items: int | None = None
    max_items: int | None = None
    item_min_length: int | None = None
    item_max_length: int | None = None

    entry_fields: list[SchemaField] = Field(default_factory=list)
    """For :attr:`FieldKind.ENTRY_GROUP`: the fields one entry holds.

    Full :class:`SchemaField` objects rather than a second, smaller type, so an
    entry's ``label`` renders through exactly the branch a top-level string
    renders through and is validated by exactly the same check. A second shape
    would be a second set of bugs.
    """

    min_entries: int | None = None
    max_entries: int | None = None
    """``minProperties``/``maxProperties`` on a repeating group. ``min_entries``
    is what makes "removing the last entry" refusable: weather needs at least
    one location, and the plugin will not start without one."""

    lookup: FieldLookup | None = None
    """ADR-0016's search-and-fill, when the schema declared it *and* the plugin's
    manifest permitted it. ``None`` on a field that declared none — and also on
    one whose declaration was refused, which is why the refusal is said out loud
    on the page rather than only here."""

    extensions: dict[str, Any] = Field(default_factory=dict)
    """Every ``x-`` keyword on the property, carried through unread.

    The seam for the plan's items 9 and 10 (ADR-0016's search-and-fill, offered
    to any plugin rather than special-cased to weather): that pass reads its own
    marker from here and adds its control, without this reader learning what the
    marker means.
    """


class UnrenderableField(BaseModel):
    """A property the form will not show, and why — never a silent omission.

    ADR-0015 falls back to the raw editor "for a fragment too complex to
    render", and the page has to say *which* fragment. An operator who cannot
    find a setting on the form needs to be told it is in the other tab, not left
    to conclude the plugin lost it.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    reason: str
    """Plain English (spec section 9), naming the construct."""

    hint: str | None = None
    """What kind of thing it is, when the core can tell.

    ``"repeating-group"`` marks the one case already scheduled: an object with
    ``additionalProperties``, which is the plan's item 7 and becomes a real
    control in the pass after this one. Named rather than described so that pass
    can find its own cases without re-deriving them.
    """


class PluginSchema(BaseModel):
    """One plugin's ``config.schema.json``, reduced to what the page needs."""

    model_config = ConfigDict(extra="forbid")

    plugin: str
    path: str
    title: str = ""
    description: str = ""
    fields: list[SchemaField] = Field(default_factory=list)
    unrenderable: list[UnrenderableField] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    """Plain-English remarks about the schema that belong to no single field.

    A refused lookup marker (ADR-0016) is the case that put this here: the field
    still renders, it simply has no search box, and an author whose typo cost
    them the feature has to be told rather than left to notice.
    """

    @property
    def renderable(self) -> bool:
        """Whether there is a form worth showing at all.

        A schema every one of whose properties is unrenderable is not a form; it
        is the raw editor with extra headings, and saying so is more use than
        rendering an empty ``<form>``.
        """
        return bool(self.fields)


class SchemaRefused(Exception):
    """The schema will not be read: it is malformed, oversized, or it points
    somewhere this core will not follow (ADR-0015).

    A refusal is *not* a failure of the page. The caller falls back to the raw
    TOML editor and shows :attr:`message`, because a plugin with a broken schema
    is still a plugin whose settings need fixing — which is the plan's item 22
    in miniature.
    """

    def __init__(self, message: str, problems: list[ConfigProblem] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems or [ConfigProblem(key=f"({SCHEMA_FILENAME})", problem=message)]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def plugin_schema_path(layout: AppdataLayout, name: str) -> Path:
    """``<plugin folder>/config.schema.json``, through the config file's own
    path checks.

    Derived from :func:`personacore.admin.plugin_config_io.plugin_config_path`
    rather than rebuilt: that function already resolves the plugin folder
    through ``require_inside``, refuses a name that is not a plugin name and
    refuses a file that leads outside the folder. Two path routines into the
    same directory is how one of them ends up missing a check the other has.
    """
    config = plugin_config_path(layout, name)
    candidate = config.with_name(SCHEMA_FILENAME)
    try:
        resolved = layout.require_inside(
            candidate, what=f"The settings schema for {name!r}"
        )
    except AppdataError as exc:
        raise PluginConfigUnsafe(
            str(exc), [ConfigProblem(key="(path)", problem=str(exc))]
        ) from exc
    if resolved.parent != config.parent:
        raise PluginConfigUnsafe(
            f"The settings schema for {name!r} does not sit inside the plugin's own "
            "folder, so it will not be read.",
            [
                ConfigProblem(
                    key="(path)",
                    problem=(
                        f"{SCHEMA_FILENAME} in {name!r} leads somewhere outside that "
                        "plugin's folder."
                    ),
                    hint=(
                        "A plugin's files live in the plugin's own folder and nowhere "
                        "else (spec section 5.1). Replace the link with a real file."
                    ),
                )
            ],
        )
    return resolved


def read_plugin_schema(layout: AppdataLayout, name: str) -> PluginSchema | None:
    """One plugin's schema, or ``None`` when it ships none.

    ``None`` rather than an exception because not shipping a schema is normal
    and costs the author nothing but the textarea (ADR-0015: "A plugin is never
    *required* to ship one — requiring it would break every plugin written
    before this decision"). A schema that exists but cannot be read is a
    :class:`SchemaRefused`, which is a different thing and gets said out loud.
    """
    path = plugin_schema_path(layout, name)
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:  # pragma: no cover - stat after is_file
        raise SchemaRefused(f"{SCHEMA_FILENAME} could not be read: {exc}.") from exc
    if size > MAX_SCHEMA_BYTES:
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} is {size} bytes, larger than the {MAX_SCHEMA_BYTES} "
            "bytes this core will read. Its settings are edited as text instead."
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} could not be read as UTF-8 text: {exc}."
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} is not valid JSON: {exc.msg}, line {exc.lineno}. "
            "Its settings are edited as text until that is fixed."
        ) from exc
    return parse_schema(document, plugin=name, path=path.as_posix())


def parse_schema(document: Any, *, plugin: str, path: str = SCHEMA_FILENAME) -> PluginSchema:
    """Turn a parsed schema document into fields. Never touches the filesystem.

    Separate from :func:`read_plugin_schema` so the whole of the interpretation
    can be exercised on a literal, which is also what makes the refusals
    testable without writing a file to provoke each one.
    """
    if not isinstance(document, dict):
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} must be a JSON object describing the settings, and "
            f"this one is a {type(document).__name__}."
        )
    root = _resolve(document, document, depth=0, seen=())
    declared = root.get("type")
    if declared is not None and declared != "object":
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} describes a {declared!r}, but a plugin's settings are "
            "an object of named settings. Its settings are edited as text instead."
        )
    properties = root.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} has a 'properties' that is not an object, so there is "
            "nothing to build fields from."
        )

    required = {
        str(item) for item in root.get("required", []) if isinstance(item, str)
    }
    fields: list[SchemaField] = []
    unrenderable: list[UnrenderableField] = []
    for index, (key, raw_property) in enumerate(sorted((properties or {}).items())):
        if index >= MAX_PROPERTIES:
            unrenderable.append(
                UnrenderableField(
                    key=str(key),
                    label=str(key),
                    reason=(
                        f"This schema declares more than {MAX_PROPERTIES} settings, "
                        "which is more than this page will render. The remaining ones "
                        "are edited as text."
                    ),
                )
            )
            break
        name = str(key)
        subject = _resolve(raw_property, document, depth=0, seen=())
        field, refusal = _field_from(
            name, subject, required=name in required, document=document
        )
        if field is not None:
            fields.append(field)
        else:
            assert refusal is not None  # noqa: S101 - one of the two is always set
            unrenderable.append(refusal)

    return PluginSchema(
        plugin=plugin,
        path=path,
        title=_text(root.get("title")),
        description=_text(root.get("description")),
        fields=fields,
        unrenderable=unrenderable,
        notes=_check_fill_targets(fields),
    )


def _check_fill_targets(fields: list[SchemaField]) -> list[str]:
    """Drop any lookup whose ``fill`` names a setting that is not there.

    Done once every field is known, because a lookup on a plain field fills its
    *siblings* (ADR-0016: "a field, or group of fields") and no single property
    can see them. Dropped rather than tolerated: a marker that fills nothing is
    a search box that appears to work and silently does not, which is worse than
    no search box and much harder to notice.
    """
    notes: list[str] = []
    by_key = {field.key: field for field in fields}
    for field in fields:
        for inner in field.entry_fields:
            # A lookup belongs to a whole entry, not to one box inside it: the
            # marked field's siblings are what a result fills, and a marker on a
            # single entry field has no siblings to name. Cleared rather than
            # half-supported, so there is one shape to write and one to review.
            if inner.lookup is not None:
                inner.lookup = None
                notes.append(
                    f"The search declared on '{field.key}.{inner.key}' is not offered: "
                    f"declare it on '{field.key}' itself and a result then fills the "
                    "entry's fields."
                )
        if field.lookup is None:
            if LOOKUP_MARKER in field.extensions:
                notes.append(
                    f"The search declared for '{field.key}' does not say which tool to "
                    "call and which settings a result fills, so it is not offered."
                )
            continue
        if field.kind is FieldKind.ENTRY_GROUP:
            available = {entry.key: entry for entry in field.entry_fields}
            where = f"an entry of '{field.key}'"
        else:
            available = by_key
            where = "this schema"
        missing = sorted(set(field.lookup.fill) - set(available))
        groups = sorted(
            name
            for name in field.lookup.fill
            if name in available and available[name].kind is FieldKind.ENTRY_GROUP
        )
        if missing:
            notes.append(
                f"The search for '{field.key}' says it fills {', '.join(missing)}, "
                f"which {'is not a setting' if len(missing) == 1 else 'are not settings'} "
                f"in {where}, so it is not offered."
            )
            field.lookup = None
        elif groups:
            notes.append(
                f"The search for '{field.key}' says it fills {', '.join(groups)}, "
                "which holds entries rather than a single value, so it is not "
                "offered."
            )
            field.lookup = None
    return notes


# ---------------------------------------------------------------------------
# $ref — refused unless it points inside this document (ADR-0015)
# ---------------------------------------------------------------------------


def _resolve(
    node: Any, document: dict[str, Any], *, depth: int, seen: tuple[str, ...]
) -> dict[str, Any]:
    """Follow an in-document ``$ref``, or refuse.

    The one place a schema could ask the core to *go and get something*, which
    is why ADR-0015 names it explicitly. Anything not starting with ``#`` is
    refused without being parsed, let alone fetched: no HTTP client is imported
    here and no path is opened, so a remote reference cannot be followed even by
    accident.
    """
    if not isinstance(node, dict):
        return {}
    reference = node.get("$ref")
    if reference is None:
        return node
    if not isinstance(reference, str) or not reference.startswith("#"):
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} points at something outside itself "
            f"({reference!r}). This core never fetches a schema from anywhere: a "
            "plugin's schema has to be complete on its own, so these settings are "
            "edited as text instead."
        )
    if depth >= MAX_REF_DEPTH or reference in seen:
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} refers back to itself through {reference!r}, so it "
            "cannot be read. Its settings are edited as text instead."
        )
    target = _pointer(document, reference)
    return _resolve(target, document, depth=depth + 1, seen=(*seen, reference))


def _pointer(document: dict[str, Any], reference: str) -> Any:
    """Walk a ``#/a/b`` JSON pointer over this document's own keys.

    Object keys only — no array indices, no escapes beyond the two JSON Pointer
    defines. A pointer that leads nowhere is a refusal rather than an empty
    schema, because "this field silently has no rules" is the failure mode a
    validator must never have.
    """
    fragment = reference[1:]
    if fragment.startswith("/"):
        fragment = fragment[1:]
    node: Any = document
    if fragment:
        for part in fragment.split("/"):
            token = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or token not in node:
                raise SchemaRefused(
                    f"{SCHEMA_FILENAME} refers to {reference!r}, which is not in the "
                    "file. Its settings are edited as text instead."
                )
            node = node[token]
    if not isinstance(node, dict):
        raise SchemaRefused(
            f"{SCHEMA_FILENAME} refers to {reference!r}, which is not a schema. Its "
            "settings are edited as text instead."
        )
    return node


# ---------------------------------------------------------------------------
# One property -> one field, or one stated refusal
# ---------------------------------------------------------------------------


def _field_from(
    key: str, subject: dict[str, Any], *, required: bool, document: dict[str, Any]
) -> tuple[SchemaField | None, UnrenderableField | None]:
    """Classify one property. Exactly one half of the pair is ever set.

    ``document`` travels with the property only so a nested ``$ref`` (a list's
    ``items``) goes through the same refusal as a top-level one. Nothing else
    reads it.
    """
    label = _text(subject.get("title")) or key
    common: dict[str, Any] = {
        "key": key,
        "label": label,
        "help": _text(subject.get("description")),
        "required": required,
        "default": subject.get("default"),
        "lookup": _lookup_from(subject),
        "extensions": {
            str(name): value
            for name, value in subject.items()
            if isinstance(name, str) and name.startswith("x-")
        },
    }

    def skip(reason: str, hint: str | None = None) -> tuple[None, UnrenderableField]:
        return None, UnrenderableField(key=key, label=label, reason=reason, hint=hint)

    for combinator in ("oneOf", "anyOf", "allOf", "not", "if"):
        if combinator in subject:
            return skip(
                f"This setting is described with '{combinator}', which this page does "
                "not render. Edit it in the raw config.toml tab."
            )

    declared = subject.get("type")
    if isinstance(declared, list):
        return skip(
            "This setting allows more than one kind of value, which this page does "
            "not render. Edit it in the raw config.toml tab."
        )
    if declared is not None and not isinstance(declared, str):
        return skip(
            "This setting's 'type' is not a type name, so there is no control to "
            "render for it. Edit it in the raw config.toml tab."
        )

    if _is_secret_field(key, subject, declared):
        return SchemaField(kind=FieldKind.SECRET_NAME, **common), None

    if declared == "boolean":
        return SchemaField(kind=FieldKind.TOGGLE, **common), None

    if declared in {"integer", "number"}:
        return (
            SchemaField(
                kind=FieldKind.NUMBER,
                integer_only=declared == "integer",
                minimum=_number(subject.get("minimum")),
                maximum=_number(subject.get("maximum")),
                exclusive_minimum=_number(subject.get("exclusiveMinimum")),
                exclusive_maximum=_number(subject.get("exclusiveMaximum")),
                **common,
            ),
            None,
        )

    if declared == "string":
        choices = subject.get("enum")
        if choices is not None:
            if not isinstance(choices, list) or not choices:
                return skip(
                    "This setting lists its choices in a way this page cannot read. "
                    "Edit it in the raw config.toml tab."
                )
            if not all(isinstance(choice, str) for choice in choices):
                return skip(
                    "Some of this setting's choices are not text, which this page does "
                    "not render. Edit it in the raw config.toml tab."
                )
            if len(choices) > MAX_ENUM_CHOICES:
                return skip(
                    f"This setting offers more than {MAX_ENUM_CHOICES} choices, which "
                    "is more than this page will list. Edit it in the raw config.toml "
                    "tab."
                )
            return (
                SchemaField(
                    kind=FieldKind.CHOICE, choices=[str(c) for c in choices], **common
                ),
                None,
            )
        maximum = _whole(subject.get("maxLength"))
        long_form = _text(subject.get("format")) == "textarea" or (
            maximum is not None and maximum > LONG_TEXT_CHARS
        )
        return (
            SchemaField(
                kind=FieldKind.TEXTAREA if long_form else FieldKind.TEXT,
                min_length=_whole(subject.get("minLength")),
                max_length=maximum,
                **common,
            ),
            None,
        )

    if declared == "array":
        items = subject.get("items")
        if not isinstance(items, dict):
            return skip(
                "This list does not say what it holds, so this page cannot render it. "
                "Edit it in the raw config.toml tab."
            )
        items = _resolve(items, document, depth=0, seen=())
        if items.get("type") != "string" or "enum" in items:
            return skip(
                "This is a list of something other than plain text, which this page "
                "does not render yet. Edit it in the raw config.toml tab.",
                hint="list-of-objects" if items.get("type") == "object" else None,
            )
        return (
            SchemaField(
                kind=FieldKind.STRING_LIST,
                min_items=_whole(subject.get("minItems")),
                max_items=_whole(subject.get("maxItems")),
                item_min_length=_whole(items.get("minLength")),
                item_max_length=_whole(items.get("maxLength")),
                **common,
            ),
            None,
        )

    if declared == "object":
        # The plan's item 7 — several fields per entry, e.g. weather's
        # locations.
        if isinstance(subject.get("additionalProperties"), dict):
            return _entry_group_from(
                subject, common=common, document=document, skip=skip
            )
        return skip(
            "This setting is a group of other settings, which this page does not "
            "render yet. Edit it in the raw config.toml tab.",
            hint="nested-object",
        )

    return skip(
        "This setting does not say what kind of value it holds, so there is no "
        "control to render for it. Edit it in the raw config.toml tab."
    )


def _entry_group_from(
    subject: dict[str, Any],
    *,
    common: dict[str, Any],
    document: dict[str, Any],
    skip: Callable[..., tuple[None, UnrenderableField]],
) -> tuple[SchemaField | None, UnrenderableField | None]:
    """The plan's item 7: ``additionalProperties`` describing one entry.

    Each of the entry's own properties goes back through :func:`_field_from`, so
    an entry's ``latitude`` is the same bounded number control a top-level
    ``latitude`` would be and is checked by the same validator. **One entry
    field this page cannot render makes the whole group unrenderable**, and
    deliberately: the alternative is a form that saves the fields it understood
    and leaves the rest of the entry behind, which is how an operator loses a
    setting without being told.
    """
    entry = _resolve(subject["additionalProperties"], document, depth=0, seen=())
    if entry.get("type") != "object":
        return skip(
            "This setting holds entries that are not groups of settings, which this "
            "page does not render. Edit it in the raw config.toml tab.",
            hint="repeating-group",
        )
    properties = entry.get("properties")
    if not isinstance(properties, dict) or not properties:
        return skip(
            "This setting holds entries whose fields the schema does not describe, so "
            "there is nothing to render for them. Edit it in the raw config.toml tab.",
            hint="repeating-group",
        )
    if len(properties) > MAX_ENTRY_FIELDS:
        return skip(
            f"Each entry of this setting has more than {MAX_ENTRY_FIELDS} fields, "
            "which is more than this page will render. Edit it in the raw "
            "config.toml tab.",
            hint="repeating-group",
        )
    required = {item for item in entry.get("required", []) if isinstance(item, str)}
    entry_fields: list[SchemaField] = []
    for name, raw_property in sorted(properties.items()):
        resolved = _resolve(raw_property, document, depth=0, seen=())
        field, refusal = _field_from(
            str(name), resolved, required=str(name) in required, document=document
        )
        if field is None:
            assert refusal is not None  # noqa: S101 - one of the two is always set
            return skip(
                f"Each entry of this setting has a field ({refusal.key}) this page "
                f"cannot render: {refusal.reason}",
                hint="repeating-group",
            )
        if field.kind is FieldKind.ENTRY_GROUP:
            # One level only. Entries inside entries is a tree, and a tree is a
            # different control with a different set of things to get wrong.
            return skip(
                f"Each entry of this setting has a field ({field.key}) that holds "
                "entries of its own, which this page does not render. Edit it in the "
                "raw config.toml tab.",
                hint="repeating-group",
            )
        entry_fields.append(field)
    return (
        SchemaField(
            kind=FieldKind.ENTRY_GROUP,
            entry_fields=entry_fields,
            min_entries=_whole(subject.get("minProperties")),
            max_entries=_whole(subject.get("maxProperties")),
            **common,
        ),
        None,
    )


def _lookup_from(subject: dict[str, Any]) -> FieldLookup | None:
    """ADR-0016's marker, parsed — or ``None`` when there is none or it is
    malformed.

    Silent about a malformed marker on purpose *here*: the property still
    renders as its ordinary control, and the sentence explaining why there is no
    search box is assembled in :func:`_check_fill_targets`, which sees the raw
    marker on :attr:`SchemaField.extensions` and can tell "this is unreadable"
    from "this names a setting that is not there".
    """
    marker = subject.get(LOOKUP_MARKER)
    if not isinstance(marker, dict):
        return None
    tool = _text(marker.get("tool"))
    raw_fill = marker.get("fill")
    if not tool or not isinstance(raw_fill, dict) or not raw_fill:
        return None
    fill = {
        str(target): value
        for target, value in raw_fill.items()
        if isinstance(target, str) and isinstance(value, str) and target and value
    }
    if len(fill) != len(raw_fill):
        return None
    return FieldLookup(
        tool=tool,
        fill=fill,
        query_argument=_text(marker.get("query_argument")) or "query",
        results_key=_text(marker.get("results")) or "results",
        label_key=_text(marker.get("label")),
    )


def _is_secret_field(key: str, subject: dict[str, Any], declared: str | None) -> bool:
    """Whether this property names a secret (ADR-0015), by marker or by suffix.

    The suffix is honoured as well as the marker because
    :data:`~personacore.admin.config_io.SECRET_REFERENCE_SUFFIX` is already the
    core's answer to "this field holds the name of a secret" — it is what
    ``read_plugin_config`` reports in ``secret_references`` and what the core's
    own settings use. A property the rest of the core already treats as a
    secret reference must not render as a free-text box here.
    """
    if declared not in (None, "string"):
        return False
    if subject.get(SECRET_MARKER) is True:
        return True
    return key.endswith(SECRET_REFERENCE_SUFFIX)


def _text(value: Any) -> str:
    """A schema annotation as a string, or empty. Never ``str(None)``."""
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    """A numeric keyword, or ``None`` if it is not a number.

    ``bool`` is excluded explicitly: it is an ``int`` in Python, and a
    ``"minimum": true`` silently becoming ``1`` would be a bound nobody wrote.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _whole(value: Any) -> int | None:
    """A non-negative integer keyword (``maxLength``, ``minItems``), or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


__all__ = [
    "LONG_TEXT_CHARS",
    "LOOKUP_MARKER",
    "MAX_ENTRY_FIELDS",
    "MAX_ENUM_CHOICES",
    "MAX_PROPERTIES",
    "MAX_SCHEMA_BYTES",
    "SCHEMA_FILENAME",
    "SECRET_MARKER",
    "FieldKind",
    "FieldLookup",
    "PluginSchema",
    "SchemaField",
    "SchemaRefused",
    "UnrenderableField",
    "parse_schema",
    "plugin_schema_path",
    "read_plugin_schema",
]
