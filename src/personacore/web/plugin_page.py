"""Turning one plugin's schema, config and health into the design's context.

The routes in :mod:`personacore.web.routes` decide *what happens*; this
module decides *what the page is handed*. Everything here is a pure function
over data the router already has, so the hardest part of the biggest screen —
which of three possible values goes in a box, and which of four words describes
it — can be read and tested without an HTTP request.

**The vocabulary is closed.** ``_controls.html`` renders eight controls and
nothing else, and :data:`CONTROL_NAMES` is the whole of the translation from
:class:`~personacore.admin.plugin_schema.FieldKind` to it. A plugin supplies
data that selects one of eight macros; it never supplies markup, a template
name, an attribute or an expression. That boundary is the security decision
ADR-0015 makes, and it lives here because this is the only place a plugin's own
file reaches the page.

**Nothing in this module reads a secret.** A secret field is rendered from a
list of *names* the caller obtained from the store's name-only API, and the
value it holds is the name — see :func:`field_context`'s ``secret_names``.
There is no code path here that could put a secret's value on the page, which
is deliberate: spec section 7 is easier to keep when the function that would
have to break it does not exist.

**A default is not a saved value.** :func:`field_state` keeps "the plugin is
using this" and "the plugin would use this if you saved" apart, because they
are different facts and an operator acts differently on each. The design draws
the difference as a dashed border plus the words *plugin default*; this is the
half that decides which fields get it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from personacore.admin.models import (
    ConfigProblem,
    PluginFailureView,
    PluginListing,
    PluginLookupResult,
    PluginView,
)
from personacore.admin.plugin_form import (
    ENTRY_ITEM_PREFIX,
    ENTRY_KEY_PREFIX,
    ENTRY_PREFIX,
    FIELD_PREFIX,
    ITEM_PREFIX,
    entry_indices,
)
from personacore.admin.plugin_schema import FieldKind, SchemaField
from personacore.admin.protocols import PluginOutputView, PluginRuntimeStatus

CONTROL_NAMES: dict[FieldKind, str] = {
    FieldKind.TOGGLE: "toggle",
    FieldKind.CHOICE: "dropdown",
    FieldKind.NUMBER: "number",
    FieldKind.TEXT: "text",
    FieldKind.TEXTAREA: "multiline",
    FieldKind.STRING_LIST: "list",
    FieldKind.SECRET_NAME: "secret",
    FieldKind.ENTRY_GROUP: "group",
}
"""Schema kind -> the design's control name, and the whole of the mapping.

Exhaustive by construction: :func:`control_name` raises on a kind that is not
here, so a member added to ``FieldKind`` without a control fails loudly at the
first render rather than rendering as nothing at all. A setting that silently
disappears from a form is a setting the operator believes they have set.
"""

STATE_DEFAULT = "default"
STATE_SAVED = "saved"
STATE_UNSAVED = "unsaved"
STATE_INVALID = "invalid"

NO_SCHEMA_NOTE = (
    "This plugin ships no config.schema.json, so its settings are edited as "
    "text on the Raw tab."
)

NO_TABLE_NOTE = (
    "The core cannot tell which part of this config.toml the schema describes. "
    "Edit the file on the Raw tab instead."
)

BROKEN_CONFIG_NOTE = (
    "This config.toml does not currently parse as TOML. Fix it on the Raw tab "
    "and save; the form comes back as soon as it parses."
)
"""The whole reason this screen exists, in one sentence.

A plugin whose settings stopped it starting is the plugin an operator most
needs this page for. So the page loses the *form* and keeps everything else —
health, the reason, the switch, the raw editor — rather than refusing to open.
"""

NOTHING_RENDERABLE_NOTE = (
    "This plugin's schema describes no setting this page can render, so every one "
    "of them is edited as text on the Raw tab."
)

SECRET_HELP = (
    "Stored secret names. Values are never shown anywhere in this interface."  # noqa: S105
)

LOOKUP_UNLOADED_NOTE = (
    "{fields} would offer a search, but this plugin is not loaded. The fields "
    "are still editable by hand."
)


# ---------------------------------------------------------------------------
# The two plugin-wide screens — health (PC-280) and output (PC-279)
# ---------------------------------------------------------------------------
#
# Both are lists of every installed plugin, and both are **alphabetical**
# (PC-281). Discovery order is filesystem order: which of `plugins/` and
# `plugins-http.d/` a folder sits in, then whatever the directory hands back.
# That is arbitrary, and it is not even stable between two installs of the same
# set of plugins — so a row moves between one machine and the next for no reason
# a person can see. Sorting here rather than in `build_plugin_listing` leaves the
# JSON API's ordering exactly as it was, and case-folding is belt-and-braces: a
# manifest name is lowercase by rule today, and an ordering that quietly depended
# on that would be a trap for whoever relaxes the rule.

#: The **only** vocabulary these screens use for a plugin's state, and it is not
#: a new one: it is the set of words ``server._PluginHealthAdapter`` already
#: translates the supervisor's five states into, which is what the system health
#: screen has always shown. ``degraded`` is deliberately ``crashed`` there — a
#: plugin that has never started successfully is not "slightly unwell", it is not
#: running — and re-deriving that here from
#: :class:`~personacore.plugins.health.PluginState` would be a second opinion
#: about the same five states.
PLUGIN_STATE_WORDS = ("running", "starting", "crashed", "stopped", "unknown")

PLUGIN_STATE_TONES: dict[str, str] = {
    "running": "ok",
    "starting": "warn",
    "crashed": "down",
    "stopped": "off",
    "unknown": "warn",
}
"""State word -> the design's four dot and badge colours. ``unknown`` takes the
warning colour for the reason :class:`HealthState` gives for having a third
value at all: "I could not tell" is not an all-clear and is not a failure."""

STATE_UNREPORTED = "unknown"
"""What a plugin's state is when nothing is supervising it.

Not a sixth state of a plugin — a statement about this core. It happens when the
admin router was built without a health source, and saying "running" or
"crashed" there would be a guess presented as a reading.
"""

NO_RUNTIME_NOTE = (
    "This core was assembled without a plugin supervisor, so plugin state here "
    "is unknown. What is listed is what is installed."
)

TOOLS_NONE_NOTE = "Declares no tools."

TOOLS_NOTE = (
    "Declared in the plugin's manifest, with its risk level. The core enforces "
    "it — a plugin cannot widen it at runtime."
)

UPTIME_LATER = "uptime"
"""A fact the design's health card wants and no seam carries.

:class:`~personacore.plugins.health.PluginHealth` records ``started_at``, but
:class:`~personacore.admin.protocols.PluginRuntimeStatus` — the protocol this
surface reads through — does not, so the value genuinely is not available here.
Rendered disabled and marked ``later`` rather than left off, because a fact that
is missing on purpose and one that was forgotten look identical once it is gone.
"""

NO_OUTPUT_NOTE = "This plugin has printed nothing — a perfectly normal state."

HTTP_OUTPUT_NOTE = (
    "This plugin runs over HTTP, in a container of its own. Its output goes to "
    "that container's log — look there instead."
)

NEVER_RAN_NOTE = (
    "The core has not started this plugin. Its row on the plugin health page "
    "says why."
)

NO_OUTPUT_SOURCE_NOTE = (
    "This core was assembled without a plugin host, so nothing is capturing "
    "plugin output."
)

OUTPUT_CLIPPED_NOTE = (
    "Only the most recent {chars} characters are shown — the earlier part is "
    "not on this page."
)

OUTPUT_DROPPED_NOTE = (
    "This plugin printed more than the core keeps — the earlier output is gone "
    "for good. What is below is only what it has printed since."
)

OUTPUT_UNTRUSTED_NOTE = (
    "Everything below was written by the plugin itself and is shown as plain "
    "text, never interpreted."
)


def by_name(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rows in alphabetical order by ``name``, case-insensitively (PC-281).

    The exact name is the tie-break rather than nothing at all, so two names
    differing only in case have one fixed order instead of whichever the sort
    happened to meet first.
    """
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: (str(row["name"]).casefold(), str(row["name"])),
    )


def plugin_state(
    view: PluginView | None,
    failure: PluginFailureView | None,
    status: PluginRuntimeStatus | None,
) -> str:
    """One plugin's state, in the words the system health screen already uses.

    The supervisor's own word wins when there is one, because it is the only
    source that can tell ``starting`` from ``crashed`` — the listing's
    :class:`~personacore.admin.models.HealthState` has already collapsed those
    two into ``failing`` by the time this surface sees it, and un-collapsing
    them here would be guesswork.

    Without a supervisor the fallbacks are ordered by how sure they are: a
    plugin that never loaded is ``crashed``, one switched off is ``stopped``,
    and anything else is :data:`STATE_UNREPORTED` rather than an assumption that
    silence means health.
    """
    if failure is not None:
        return "crashed"
    if status is not None:
        word = str(getattr(status, "state", "") or "")
        return word if word in PLUGIN_STATE_WORDS else STATE_UNREPORTED
    if view is not None and not view.enabled:
        return "stopped"
    return STATE_UNREPORTED


def plugin_health_rows(
    listing: PluginListing,
    status_for: Callable[[str], PluginRuntimeStatus | None] | None = None,
) -> list[dict[str, Any]]:
    """Every installed plugin as one row of the plugin health screen (PC-280).

    One row per plugin and one plugin per row: state, transport, the tools it
    declares, how many times it has been restarted and the last thing that went
    wrong, together. The system dashboard has a row per plugin too, but it has
    it among the disk and the broker and the language model, and "which of my
    plugins is unhappy" is a different question from "is this machine well".

    A plugin that never loaded gets a row of its own from ``listing.failures``,
    for the reason spec section 5.1 gives: absent from the list is the worst
    possible answer about a folder somebody can see on disk.
    """
    rows: list[dict[str, Any]] = []
    for view in listing.plugins:
        status = status_for(view.name) if status_for is not None else None
        state = plugin_state(view, None, status)
        # The supervisor's sentence when there is one, the listing's otherwise.
        # They are the same string in an assembled core — the listing copies it
        # from the same place — so this only matters to a core with no
        # supervisor at all.
        detail = (getattr(status, "detail", None) if status is not None else None) or view.detail
        restarts = getattr(status, "restarts", None) if status is not None else None
        rows.append(
            {
                "name": view.name,
                "version": view.version,
                "state": state,
                "tone": PLUGIN_STATE_TONES[state],
                "enabled": view.enabled,
                "transport": view.transport,
                "tools": [
                    {"name": tool, "risk": risk} for tool, risk in sorted(view.tools.items())
                ],
                "restarts": view.restarts if restarts is None else restarts,
                # A reason and a *fault* are not the same thing, and the design
                # colours them differently. "Switched off in the admin
                # interface" is the truthful explanation of a plugin that is
                # not running and is nobody's problem; painting it the same red
                # as a crash is how people learn to ignore red.
                "last_error": detail if state in ("crashed", "unknown") else None,
                "note": detail if state in ("stopped", "starting") else None,
                "loaded": True,
            }
        )
    for failure in listing.failures:
        rows.append(
            {
                "name": failure.name or failure.source,
                "version": "",
                "state": "crashed",
                "tone": PLUGIN_STATE_TONES["crashed"],
                "enabled": True,
                # Not "stdio": a manifest that would not parse never said which
                # transport it wanted, and filling one in would be a fact this
                # code invented about a plugin it could not read.
                "transport": "",
                "tools": [],
                "restarts": 0,
                "last_error": failure.reason,
                "note": None,
                "loaded": False,
            }
        )
    return by_name(rows)


def plugin_output_rows(
    listing: PluginListing,
    output_for: Callable[[str], PluginOutputView | None] | None = None,
    *,
    chars: int = 0,
) -> list[dict[str, Any]]:
    """Every installed plugin and what it has printed (PC-279).

    Four different emptinesses, and most of the value of this page is telling
    them apart. Somebody staring at a blank box learns nothing; each of these
    says which blank it is:

    * **No source at all** — this core has no plugin host, so nothing anywhere
      is capturing output. Marked ``later``, like every other control on this
      surface with no backend behind it.
    * **An HTTP plugin** — it runs in a container this core did not start, so
      its stderr is that container's. Said outright rather than shown as an
      empty box that looks broken.
    * **Never started** — nothing has run, so nothing could have printed.
    * **Ran and said nothing** — the ordinary case, and explicitly not an error.

    Truncation is reported in the same spirit: the capture is bounded and
    truncating, so a tail presented as the whole output would be the one way
    this page could actively mislead the person reading it.
    """
    named: list[tuple[str, str]] = [(view.name, view.transport) for view in listing.plugins]
    named += [(failure.name or failure.source, "") for failure in listing.failures]

    rows: list[dict[str, Any]] = []
    for name, transport in named:
        output = output_for(name) if output_for is not None else None
        text = str(getattr(output, "text", "") or "") if output is not None else ""
        dropped = bool(getattr(output, "dropped", False)) if output is not None else False
        # Notes sit *above* the output, so they are only notes when there is
        # output to sit above. With nothing to show, the same fact is the whole
        # explanation and belongs in `empty_reason` instead — otherwise a
        # plugin whose entire capture was thrown away would be described as
        # having printed nothing, which is the opposite of what happened.
        notes: list[str] = []
        if text:
            if dropped:
                notes.append(OUTPUT_DROPPED_NOTE)
            if getattr(output, "clipped", False):
                notes.append(OUTPUT_CLIPPED_NOTE.format(chars=f"{chars:,}"))
        rows.append(
            {
                "name": name,
                "transport": transport,
                "text": text,
                "notes": notes,
                "empty_reason": _empty_reason(
                    text,
                    transport,
                    has_source=output_for is not None,
                    ran=output is not None,
                    dropped=dropped,
                ),
                # ``later`` when the whole capability is missing, never when one
                # plugin happens to be quiet — those are not the same fact.
                "unavailable": output_for is None,
            }
        )
    return by_name(rows)


def _empty_reason(
    text: str, transport: str, *, has_source: bool, ran: bool, dropped: bool = False
) -> str:
    """Which emptiness this is, or ``""`` when there is output to show.

    Ordered from "nothing here could ever have output" outwards, so the most
    specific true answer wins. ``dropped`` comes before "printed nothing"
    because they are opposite claims about the same blank box: one plugin never
    said a word, the other said so much the core threw all of it away.
    """
    if text:
        return ""
    if not has_source:
        return NO_OUTPUT_SOURCE_NOTE
    if transport and transport != "stdio":
        return HTTP_OUTPUT_NOTE
    if dropped:
        return OUTPUT_DROPPED_NOTE
    if not ran:
        return NEVER_RAN_NOTE
    return NO_OUTPUT_NOTE


def control_name(kind: FieldKind) -> str:
    """The design's control for one schema kind.

    Raises rather than returning a default: an unrecognised kind means the
    schema reader grew a construct the form cannot draw, and drawing nothing
    would hide a setting the plugin is really using.
    """
    try:
        return CONTROL_NAMES[kind]
    except KeyError as exc:  # pragma: no cover - guards a future FieldKind
        raise KeyError(f"{kind!r} has no control in _controls.html") from exc


# ---------------------------------------------------------------------------
# Health — rendered in every state, including "it never loaded"
# ---------------------------------------------------------------------------


def health_context(
    name: str,
    view: PluginView | None,
    failure: PluginFailureView | None,
) -> dict[str, Any]:
    """The header and health card, for a plugin in any state at all.

    Three sources and none of them may be required: a plugin that loaded has a
    :class:`PluginView`, one that did not has a :class:`PluginFailureView`, and
    a folder the scan has not caught up with has neither. All three render.

    ``state`` is the design's word, not the API's: ``failed`` covers both "the
    manifest was refused" and "it loaded and then died", because from the
    operator's side those are the same sentence — it is not running and it
    needs looking at. ``enabled`` is kept separate from it, since a plugin that
    failed to load is still *switched on*, and offering to switch it on again
    would be offering to do nothing.
    """
    if failure is not None:
        return {
            "name": name,
            "version": "",
            "state": "failed",
            "enabled": True,
            "fail_reason": failure.reason,
            "restarts": 0,
            "recent_errors": [f"Reported by {failure.source}."] if failure.source else [],
        }
    if view is None:
        return {
            "name": name,
            "version": "",
            "state": "failed",
            "enabled": True,
            "fail_reason": (
                f"The core's last scan did not find {name}. Settings are still "
                "editable; a reload will pick it up."
            ),
            "restarts": 0,
            "recent_errors": [],
        }
    running = view.enabled and view.state.value != "failing"
    return {
        "name": view.name,
        "version": view.version,
        "state": "running" if running else ("off" if not view.enabled else "failed"),
        "enabled": view.enabled,
        "fail_reason": view.detail if not running and view.enabled else None,
        "restarts": view.restarts,
        # The supervisor keeps one last error, not a history (see
        # `PluginHealth.last_error`), so this is a list of at most one. It is
        # still a list because the design draws several and a page that can
        # only ever show one would quietly become the reason nobody adds the
        # rest.
        "recent_errors": [view.detail] if view.detail and running else [],
    }


# ---------------------------------------------------------------------------
# Errors — the validator's problems, put back beside the boxes
# ---------------------------------------------------------------------------


def field_errors(
    problems: Sequence[ConfigProblem], table: str
) -> tuple[dict[str, str], dict[tuple[str, str, str], str]]:
    """Split a refusal into "this field" and "this entry's field".

    :func:`~personacore.admin.plugin_form.validate_submission` keys a problem by
    where it is *in the file* — ``weather.forecast_days``, or
    ``weather.locations.home.latitude`` — because that is what an operator needs
    when they switch to the Raw tab. The form needs the same problem beside the
    box instead, so this reverses the key.

    Returns ``({field key: sentence}, {(group, entry name, sub key): sentence})``.
    A problem whose key matches nothing on the form is dropped from both and is
    still shown whole in the save banner, which is the honest fallback: an error
    with nowhere to point is not an error worth hiding.
    """
    prefix = f"{table}." if table else ""
    scalar: dict[str, str] = {}
    entry: dict[tuple[str, str, str], str] = {}
    for problem in problems:
        key = problem.key
        if prefix and key.startswith(prefix):
            key = key[len(prefix) :]
        elif prefix:
            continue
        parts = key.split(".")
        if len(parts) == 1:
            scalar.setdefault(parts[0], problem.problem)
        elif len(parts) == 3:  # noqa: PLR2004 - group.entry.field, and nothing else
            entry.setdefault((parts[0], parts[1], parts[2]), problem.problem)
    return scalar, entry


# ---------------------------------------------------------------------------
# Values — what actually goes in the box
# ---------------------------------------------------------------------------


def plain_text(value: Any) -> str:
    """A stored TOML value as the operator would have typed it.

    ``7`` rather than ``7.0`` and ``true`` rather than ``True``. A box holding
    Python's idea of a value invites an operator to save it back in that shape.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def field_state(*, error: str | None, submitted: bool, present: bool, default: Any) -> str:
    """Which of the design's four words describes this box.

    Order matters. ``invalid`` beats everything, because a refused value is what
    the operator has to deal with first. ``unsaved`` comes next: a box that
    holds what was just typed rather than what is on disk must say so, or a
    failed save looks like a successful one. Then ``default`` — the fact this
    screen exists to make visible — and finally ``saved``.
    """
    if error:
        return STATE_INVALID
    if submitted:
        return STATE_UNSAVED
    if not present and default is not None:
        return STATE_DEFAULT
    return STATE_SAVED


def field_context(
    field: SchemaField,
    *,
    values: Mapping[str, Any],
    submitted: Mapping[str, Sequence[str]] | None = None,
    secret_names: Sequence[str] = (),
    errors: Mapping[str, str] | None = None,
    entry_errors: Mapping[tuple[str, str, str], str] | None = None,
    can_search: bool = True,
) -> dict[str, Any]:
    """One field, as ``_controls.html`` wants it.

    Three possible sources for the value and a fixed order between them:
    what was just submitted, then what the file holds, then the schema's
    ``default``. The first wins because a refused save that redraws the file
    throws away what the operator typed, and a form that does that is a form
    nobody edits twice.
    """
    errors = errors or {}
    error = errors.get(field.key)
    present = field.key in values
    stored = values.get(field.key, field.default)
    row: dict[str, Any] = {
        "key": field.key,
        "label": field.label,
        "type": control_name(field.kind),
        "help": field.help,
        "error": error,
        "optional": not field.required,
    }

    if field.kind is FieldKind.ENTRY_GROUP:
        row["state"] = STATE_INVALID if error else STATE_SAVED
        row["value"] = ""
        row["entry_label"] = _entry_label(field)
        row["searchable"] = can_search and field.lookup is not None
        row["search_placeholder"] = f"Search to fill this {_entry_label(field)}"
        row["entries"] = group_entries(
            field,
            stored,
            submitted=submitted,
            secret_names=secret_names,
            entry_errors=entry_errors or {},
        )
        return row

    was_submitted = submitted is not None
    row["state"] = field_state(
        error=error, submitted=was_submitted, present=present, default=field.default
    )
    if field.kind is FieldKind.TOGGLE:
        row["value"] = (
            bool(submitted.get(FIELD_PREFIX + field.key))
            if submitted is not None
            else bool(stored)
        )
    elif field.kind is FieldKind.STRING_LIST:
        if submitted is not None:
            row["value"] = [str(item) for item in submitted.get(ITEM_PREFIX + field.key, ())]
        else:
            row["value"] = [str(item) for item in stored] if isinstance(stored, list) else []
        row["add_placeholder"] = "add one"
    else:
        row["value"] = _scalar_text(field, stored, submitted)

    if field.kind is FieldKind.CHOICE:
        row["options"] = list(field.choices)
    if field.kind is FieldKind.SECRET_NAME:
        # Names, and only ever names. The value in the box is the name of a
        # secret; the store's value never enters this process's page context.
        row["secret_names"] = list(secret_names)
    if field.max_length is not None:
        row["maxlen"] = field.max_length
    if field.minimum is not None and field.maximum is not None:
        row["min"] = plain_text(field.minimum)
        row["max"] = plain_text(field.maximum)
    return row


def _scalar_text(
    field: SchemaField, stored: Any, submitted: Mapping[str, Sequence[str]] | None
) -> str:
    if submitted is not None:
        typed = submitted.get(FIELD_PREFIX + field.key)
        return str(typed[0]) if typed else ""
    return "" if stored is None else plain_text(stored)


def _entry_label(field: SchemaField) -> str:
    """What one entry of a repeating group is called, in the singular.

    Derived from the group's own label rather than configured, because a plugin
    author who wrote ``Locations`` has already said what one of them is.
    """
    label = field.label.strip()
    if label.lower().endswith("ies"):
        return f"{label[:-3]}y".lower()
    if label.lower().endswith("s"):
        return label[:-1].lower()
    return label.lower() or "entry"


# ---------------------------------------------------------------------------
# Repeating groups — more code than the rest of the form put together
# ---------------------------------------------------------------------------


def group_entries(
    field: SchemaField,
    stored: Any,
    *,
    submitted: Mapping[str, Sequence[str]] | None = None,
    secret_names: Sequence[str] = (),
    entry_errors: Mapping[tuple[str, str, str], str] = {},  # noqa: B006 - read only
) -> list[dict[str, Any]]:
    """Every entry of one group, from the file or from the submission.

    ``index`` is a **position on this page** and nothing else: it holds one
    entry's inputs together across a round trip and never reaches the file. The
    key that reaches the file is the name the operator typed, and
    :func:`~personacore.admin.plugin_form.require_entry_key` checks it against a
    pattern before it is written into a TOML header.

    The two sources are kept in one function because they must agree about
    everything except where the values came from — an entry rendered from a
    submission and the same entry rendered from disk have to name their inputs
    identically, or the round trip loses them.
    """
    if submitted is not None:
        return [
            entry_context(
                field,
                index,
                key=_first(submitted, f"{ENTRY_KEY_PREFIX}{field.key}.{index}").strip(),
                values={},
                submitted=submitted,
                secret_names=secret_names,
                entry_errors=entry_errors,
                state=STATE_UNSAVED,
            )
            for index in entry_indices(field, submitted)
        ]
    entries = stored if isinstance(stored, dict) else {}
    return [
        entry_context(
            field,
            index,
            key=str(key),
            values=dict(value) if isinstance(value, dict) else {},
            submitted=None,
            secret_names=secret_names,
            entry_errors=entry_errors,
            state=STATE_SAVED,
        )
        for index, (key, value) in enumerate(entries.items())
    ]


def entry_context(
    field: SchemaField,
    index: int,
    *,
    key: str,
    values: Mapping[str, Any],
    submitted: Mapping[str, Sequence[str]] | None,
    secret_names: Sequence[str] = (),
    entry_errors: Mapping[tuple[str, str, str], str] = {},  # noqa: B006 - read only
    state: str = STATE_SAVED,
) -> dict[str, Any]:
    """One entry: its name, its own fields, and its own search.

    An entry's fields are named ``entry.<group>.<index>.<field>`` — the shape
    :func:`~personacore.admin.plugin_form.entry_inputs` renames back into a form
    of its own, so an entry's ``latitude`` is validated by exactly the check a
    top-level ``latitude`` gets. There is no second validator for "the same
    field, but indented".
    """
    fields: list[dict[str, Any]] = []
    for sub in field.entry_fields:
        error = entry_errors.get((field.key, key, sub.key)) if key else None
        is_list = sub.kind is FieldKind.STRING_LIST
        name = (
            f"{ENTRY_ITEM_PREFIX}{field.key}.{index}.{sub.key}"
            if is_list
            else f"{ENTRY_PREFIX}{field.key}.{index}.{sub.key}"
        )
        raw = list(submitted.get(name, ())) if submitted is not None else None
        if is_list:
            if raw is not None:
                items = [str(item) for item in raw]
            else:
                found = values.get(sub.key)
                items = [str(item) for item in found] if isinstance(found, list) else []
            value: Any = items
        elif raw is not None:
            value = str(raw[0]) if raw else ""
        else:
            found = values.get(sub.key, sub.default)
            value = "" if found is None else plain_text(found)
        fields.append(
            {
                "key": sub.key,
                "label": sub.label,
                "name": name,
                "list": is_list,
                "value": value,
                "error": error,
            }
        )
    return {
        "index": index,
        "key": key,
        "title": key,
        "state": state if key else "new",
        "fields": fields,
        "name_field": f"{ENTRY_KEY_PREFIX}{field.key}.{index}",
    }


def _first(inputs: Mapping[str, Sequence[str]], name: str) -> str:
    found = inputs.get(name)
    return str(found[0]) if found else ""


def next_entry_index(field: SchemaField, submitted: Mapping[str, Sequence[str]]) -> int:
    """Where a newly added entry goes.

    One past the highest index already on the page rather than the count, so a
    new entry cannot land on top of one the operator has already filled in when
    an earlier one was removed.
    """
    used = entry_indices(field, submitted)
    return (used[-1] + 1) if used else 0


# ---------------------------------------------------------------------------
# Search-and-fill — general, and never weather-specific
# ---------------------------------------------------------------------------


def search_rows(results: Sequence[PluginLookupResult]) -> list[dict[str, str]]:
    """One search's matches, as rows of text.

    ADR-0016: results "are rendered as data in a list, never interpreted". The
    detail line is built from whatever keys the *schema* asked to fill, joined
    generically — this core does not know that a place has a latitude, and a
    template that named one would make search-and-fill a weather feature rather
    than the mechanism every plugin gets.
    """
    return [
        {
            "label": hit.label,
            "detail": ", ".join(f"{key}: {value}" for key, value in hit.values.items()),
        }
        for hit in results
    ]


def fill_entry_inputs(
    field: SchemaField,
    index: int,
    picked: PluginLookupResult,
    submitted: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """The entry's inputs with the picked result merged over them.

    Only keys the schema's ``fill`` map names, and only ones that are really
    fields of this entry: a result key the schema did not ask for cannot reach
    a box, and a fill target that is not a field of this group is dropped. What
    lands is ordinary form input — validated on save exactly as typing it would
    be, which is what makes a picked result no more trusted than a typed one.
    """
    merged = {name: list(value) for name, value in submitted.items()}
    if field.lookup is None:
        return merged
    known = {sub.key for sub in field.entry_fields}
    for target in field.lookup.fill:
        value = picked.values.get(target)
        if not value or target not in known:
            continue
        merged[f"{ENTRY_PREFIX}{field.key}.{index}.{target}"] = [value]
    return merged


__all__ = [
    "BROKEN_CONFIG_NOTE",
    "CONTROL_NAMES",
    "HTTP_OUTPUT_NOTE",
    "LOOKUP_UNLOADED_NOTE",
    "NEVER_RAN_NOTE",
    "NOTHING_RENDERABLE_NOTE",
    "NO_OUTPUT_NOTE",
    "NO_OUTPUT_SOURCE_NOTE",
    "NO_RUNTIME_NOTE",
    "NO_SCHEMA_NOTE",
    "NO_TABLE_NOTE",
    "OUTPUT_CLIPPED_NOTE",
    "OUTPUT_DROPPED_NOTE",
    "OUTPUT_UNTRUSTED_NOTE",
    "PLUGIN_STATE_TONES",
    "PLUGIN_STATE_WORDS",
    "SECRET_HELP",
    "STATE_DEFAULT",
    "STATE_INVALID",
    "STATE_SAVED",
    "STATE_UNREPORTED",
    "STATE_UNSAVED",
    "TOOLS_NONE_NOTE",
    "TOOLS_NOTE",
    "UPTIME_LATER",
    "by_name",
    "control_name",
    "entry_context",
    "field_context",
    "field_errors",
    "field_state",
    "fill_entry_inputs",
    "group_entries",
    "health_context",
    "next_entry_index",
    "plain_text",
    "plugin_health_rows",
    "plugin_output_rows",
    "plugin_state",
    "search_rows",
]
