"""Search-and-fill: a config field asks a plugin's own tool to populate it.

ADR-0016, and the plan's items 9 and 10. A field in ``config.schema.json``
names one of the plugin's tools (:data:`~personacore.admin.plugin_schema.LOOKUP_MARKER`);
the admin form offers a search box; the core calls that tool; the results are
listed; picking one fills the fields. The core "learns nothing about geography.
It learns 'this field can be filled by calling that tool, and the result maps
onto these keys'" — which is why nothing in this module knows what weather is,
and why the same three functions give a music plugin a device picker.

This module holds the parts with rules in them, kept away from both the page and
the router so the rules can be read in one place:

* :func:`authorise_lookups` is **the** gate. ADR-0016 sets two conditions — the
  tool must be ``safe`` risk, and it must be one "the plugin nominated in its
  schema" — and both are decided here, against the plugin's own manifest, by
  *removing* the declaration from every field that does not qualify. A refused
  lookup therefore does not exist for anything downstream: the page cannot draw
  a search box for it and the endpoint cannot find a tool to call. There is no
  second code path where a check might be forgotten.
* :func:`find_lookup` answers "which tool does this named field authorise?" for
  the endpoint, so a *request* never names a tool.
* :func:`results_from` turns what the tool said into rows of data. Everything it
  touches is untrusted (spec section 7): it is JSON from a plugin, which got it
  from a third party. Values are read by the keys the schema asked for, coerced
  to text, truncated and counted — nothing is executed, nothing becomes a path
  or a URL, and a key the schema did not ask for never leaves this function.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from personacore.admin.models import PluginLookupResult
from personacore.admin.plugin_schema import FieldLookup, PluginSchema, SchemaField
from personacore.contracts.manifest import RiskLevel

MAX_RESULTS = 25
"""Rows kept from one lookup. A picker longer than this is not a picker, and the
list is rendered into a page whose size an operator did not choose."""

MAX_RESULT_CHARS = 200
"""Ceiling on one value or label out of a result. Long enough for a full place
name with its region and country, short enough that a hostile payload cannot
make a page out of one row."""

MAX_CONTENT_BYTES = 256 * 1024
"""Ceiling on the text a tool may return for a lookup, matching the ceiling the
weather plugin puts on its own upstream reply. A tool that answers with a
megabyte is malfunctioning, and reading it into JSON first is how that becomes
this core's problem."""


class LookupRefused(Exception):
    """This lookup will not run, or its answer will not be shown.

    Carries :attr:`message` — one plain-English sentence for the operator (spec
    section 9), never a traceback and never the raw payload.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def authorise_lookups(schema: PluginSchema, tools: Mapping[str, str]) -> list[str]:
    """Strip every lookup the plugin's manifest does not permit; say why.

    ``tools`` is the manifest's own ``{tool name: risk}``, as the plugin listing
    reports it (spec section 5.1: "manifest declares, core enforces").

    ADR-0016's two constraints, both enforced by deletion rather than by a flag
    somebody has to remember to read:

    * **Lookup tools must be ``safe`` risk.** "A config form must never be a
      route to a ``confirm``- or ``restricted``-level action. The core refuses
      to wire one."
    * **Only a tool the plugin nominated in its schema** — and, here, only one
      its manifest actually declares. A schema naming a tool the manifest does
      not declare is naming a tool the core has no risk level for, which spec
      section 5.1 already treats as uncallable.

    Returns the sentences to put on the page. An author whose lookup was refused
    has to be told, or they will report the missing search box as a mystery.
    """
    notes: list[str] = []
    for field in _all_fields(schema):
        if field.lookup is None:
            continue
        declared = tools.get(field.lookup.tool)
        if declared is None:
            notes.append(
                f"The search for '{field.key}' calls a tool this plugin does not "
                f"declare ({field.lookup.tool}), so it is not offered. A tool the "
                "manifest does not declare has no risk level, and the core will not "
                "call one."
            )
            field.lookup = None
            continue
        if declared != RiskLevel.SAFE.value:
            notes.append(
                f"The search for '{field.key}' calls {field.lookup.tool}, which this "
                f"plugin declares as '{declared}' rather than 'safe', so it is not "
                "offered. A settings page never runs anything that needs "
                "confirmation or permission (ADR-0016)."
            )
            field.lookup = None
    return notes


def find_lookup(schema: PluginSchema, field_key: str) -> tuple[SchemaField, FieldLookup]:
    """The field named in a request, and the lookup it authorises.

    Call :func:`authorise_lookups` on ``schema`` first — this function trusts
    what is left on the field, and that is the point: the risk decision is made
    once, in one place, over the manifest.
    """
    field = next((one for one in schema.fields if one.key == field_key), None)
    if field is None:
        raise LookupRefused(
            f"{field_key!r} is not a setting this plugin's config.schema.json "
            "describes, so there is nothing to search for."
        )
    if field.lookup is None:
        raise LookupRefused(
            f"{field.label} does not offer a search, so nothing was called. Only a "
            "setting whose schema nominates one of the plugin's own safe tools can "
            "be filled this way (ADR-0016)."
        )
    return field, field.lookup


def results_from(lookup: FieldLookup, content: str) -> list[PluginLookupResult]:
    """What the tool returned, as rows of data — ADR-0016's "rendered as data".

    Untrusted from the first character. The payload is parsed as JSON and then
    *read*, never interpreted: only the keys the schema's ``fill`` map names are
    taken, each is coerced to text and truncated, and a row that fills nothing is
    dropped rather than shown as an empty option. Nothing here builds a path, a
    URL or a command out of any of it.
    """
    if len(content.encode("utf-8", "replace")) > MAX_CONTENT_BYTES:
        raise LookupRefused(
            f"{lookup.tool} answered with more text than this page will read, so "
            "nothing is shown. Try a more specific search."
        )
    try:
        payload: Any = json.loads(content) if content.strip() else []
    except ValueError:
        raise LookupRefused(
            f"{lookup.tool} answered with something this page cannot read as a list "
            "of results, so nothing is shown."
        ) from None
    rows = _rows(lookup, payload)
    results: list[PluginLookupResult] = []
    for row in rows[:MAX_RESULTS]:
        if not isinstance(row, dict):
            continue
        values = {
            target: text
            for target, source in lookup.fill.items()
            if (text := _as_text(row.get(source)))
        }
        if not values:
            continue
        results.append(
            PluginLookupResult(label=_label(lookup, row, values), values=values)
        )
    return results


def _rows(lookup: FieldLookup, payload: Any) -> list[Any]:
    """The list of matches inside whatever the tool returned.

    Two shapes accepted and no more: the payload *is* the list, or the payload is
    an object holding it under the schema's ``results`` key. A plugin whose tool
    returns something else is told so plainly rather than having its reply
    guessed at.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        found = payload.get(lookup.results_key)
        if isinstance(found, list):
            return found
        if found is None:
            return []
    raise LookupRefused(
        f"{lookup.tool} answered with something this page cannot read as a list of "
        f"results — it has no '{lookup.results_key}' list in it, so nothing is shown."
    )


def _label(lookup: FieldLookup, row: Mapping[str, Any], values: Mapping[str, str]) -> str:
    """The one line a result shows in the list.

    The schema's ``label`` key when it named one and the row has it; otherwise
    the values themselves, which is honest — an operator picking between two
    rows needs to see what makes them different, and inventing a description
    would mean interpreting the result.
    """
    if lookup.label_key:
        text = _as_text(row.get(lookup.label_key))
        if text:
            return text
    return ", ".join(f"{name}: {value}" for name, value in values.items())


def _as_text(value: Any) -> str:
    """One result value as the text a form field would hold, or ``""``.

    ``7`` rather than ``7.0`` and ``true`` rather than ``True``, for the same
    reason the page renders a stored value that way: what goes in the box has to
    be what an operator would have typed. A value that is a list or an object
    yields nothing — a coordinate is not a structure, and flattening one would be
    interpreting the result.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))[:MAX_RESULT_CHARS]
    if isinstance(value, int | float):
        return str(value)[:MAX_RESULT_CHARS]
    if isinstance(value, str):
        return value.strip()[:MAX_RESULT_CHARS]
    return ""


def _all_fields(schema: PluginSchema) -> list[SchemaField]:
    """Every field this sweep looks at.

    Only a top-level property may declare a lookup — the schema reader clears
    one found anywhere else — so the entry fields are here purely so that a gap
    in that rule could never become an unauthorised call.
    """
    fields: list[SchemaField] = []
    for field in schema.fields:
        fields.append(field)
        fields.extend(field.entry_fields)
    return fields


__all__ = [
    "MAX_CONTENT_BYTES",
    "MAX_RESULTS",
    "MAX_RESULT_CHARS",
    "LookupRefused",
    "authorise_lookups",
    "find_lookup",
    "results_from",
]
