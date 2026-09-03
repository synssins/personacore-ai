"""Submitted form values -> validated settings -> patched ``config.toml``.

The second half of ADR-0015. :mod:`personacore.admin.plugin_schema` says what
fields exist; this module takes what an operator typed into them, checks it
against the same schema, and turns the accepted values back into TOML text for
:func:`personacore.admin.plugin_config_io.write_plugin_config` to write.

Three rules, and the second is the one worth arguing about:

* **Validate before writing, and name the field.** ADR-0015: "A rejected value
  names the field and says what was wrong, in plain English (section 9). The
  file on disk is untouched." Every rejection here is raised *before* any text
  reaches the writer, so a refusal cannot half-save. Every rejection is
  collected, not the first one only: an operator with three wrong values should
  learn that once.

* **The file is patched, not regenerated.** A plugin's ``config.toml`` comments
  are its field help — ``plugins/_template/config.toml`` says so outright — and
  parsing the document and re-serialising it with ``tomli_w`` would delete every
  one of them. So the text is kept and only the lines belonging to the changed
  keys are replaced. Keys nobody edited, comments, blank lines, key order and
  the operator's own formatting all survive untouched, including any setting the
  form could not render (which is what makes "unrenderable fields are left to
  the raw editor" safe rather than lossy).

* **The bounded validator is written here on purpose.** There is no JSON Schema
  library on ``docs/p0-dependencies.md`` and CLAUDE.md forbids adding one
  without approval. That is not a workaround: ADR-0015 supports a deliberately
  small subset — "Every construct the core renders is one it must also validate
  and explain" — and a general validator would happily accept constructs the
  form cannot render, which is the drift the ADR exists to prevent. The
  keywords checked here are exactly the keywords
  :mod:`personacore.admin.plugin_schema` renders, and no others.

**Where the settings live.** ADR-0015: "The schema describes the same table
``config.toml`` already holds." Both bundled plugins keep their settings under a
single named table (``[weather]``, ``[random_prompt]``), so
:func:`settings_table` works out which table the schema is describing rather
than assuming the document root. When it cannot tell, it says so and the page
falls back to the raw editor — a form that wrote to the wrong table would be
worse than no form.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

import tomli_w

from personacore.admin.models import ConfigProblem
from personacore.admin.plugin_config_io import PluginConfigInvalid
from personacore.admin.plugin_schema import FieldKind, PluginSchema, SchemaField

FIELD_PREFIX: Final = "field."
"""Form input name prefix for a scalar field: ``field.forecast_days``."""

ITEM_PREFIX: Final = "item."
"""Form input name prefix for one row of a list field: ``item.subjects``,
repeated once per row."""

ENTRY_KEY_PREFIX: Final = "entrykey."
"""One repeating-group entry's *name*: ``entrykey.locations.0``.

Indexed rather than repeated (the way a list field's rows are) because an entry
carries several inputs that must stay together, and one unticked checkbox
submitting nothing would silently shift every later row's values onto the wrong
entry. The index is a position on the page and nothing else — the name the
operator typed in this box is the TOML key, and it is validated by
:func:`require_entry_key` before it is ever written.
"""

ENTRY_PREFIX: Final = "entry."
"""One field of one entry: ``entry.locations.0.latitude``."""

ENTRY_ITEM_PREFIX: Final = "entryitem."
"""One row of a list field inside an entry: ``entryitem.locations.0.aliases``,
repeated once per row."""

PRESENT_PREFIX: Final = "present."
"""Marks a field as having been on the form at all.

A cleared checkbox submits nothing, which is indistinguishable from "that field
was not on this page" unless something else says it was. Every rendered field
gets one of these hidden inputs, so "switched off" and "not asked about" stay
different facts — the same reason
:class:`~personacore.admin.models.PluginView` keeps ``enabled`` apart from
``state``.
"""

MAX_LIST_ITEMS: Final = 500
"""Ceiling on rows accepted for one list field. Not a guess at what a plugin
needs — it is far more than any settings list — but the body of this request is
about to become a file in the appdata volume the audit log also depends on
(spec section 7)."""

MAX_ENTRIES: Final = 200
"""Ceiling on entries accepted for one repeating group, for the same reason.
Each entry becomes a table of its own in the file, so the cost per entry is far
higher than a list row's."""

ENTRY_KEY_PATTERN: Final = r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$"
"""What an entry's name may be — and it is deliberately narrower than TOML.

The name an operator types here becomes a **bare TOML key in a header this core
writes** (``[weather.locations.home]``), so it is checked the way a plugin name
(``packages.require_plugin_name``) and a secret name
(``config.secrets._check_name``) are checked: against a pattern, before use,
rather than escaped into something plausible. Nothing outside this set can end a
header early, start a second table, open a comment or introduce a newline, so
there is no quoting to get right and no injection to miss (spec section 7).
"""

_ENTRY_KEY_RE: Final = re.compile(ENTRY_KEY_PATTERN)


class _Unset:
    """"This setting has no value", distinct from any value it could hold.

    ``None`` cannot do this job: TOML has no null, so a key is either present
    with a value or absent, and an empty box has to mean *absent* rather than
    the empty string — otherwise clearing an optional number would write
    ``forecast_days = ""`` and stop the plugin loading.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


UNSET: Final = _Unset()


class EntryGroup:
    """The accepted contents of one repeating group: ``{entry name: settings}``.

    A type of its own rather than a plain ``dict``, because the two mean
    completely different things to :func:`apply_values`. A dict is still refused
    there — it would need a table header of its own and no caller has said where
    that header belongs — whereas this carries exactly the information the
    surgical writer needs: which entries should exist afterwards, and what each
    one holds. Anything already in the file and not named here is an entry the
    operator removed.
    """

    __slots__ = ("entries",)

    def __init__(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        self.entries: dict[str, dict[str, Any]] = {
            str(name): dict(values) for name, values in entries.items()
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EntryGroup {sorted(self.entries)}>"


class EntryKeyRejected(ValueError):
    """An entry name that will not be written. Carries the operator's sentence."""


def require_entry_key(name: str) -> str:
    """Refuse an entry name that would not be a safe TOML key (spec section 7).

    The same shape of check as ``packages.require_plugin_name``: a pattern, one
    plain-English sentence naming the rule, and no attempt to repair the input.
    Repairing it is what turns ``a.b`` into ``ab`` and writes an operator's
    settings under a name they did not choose.
    """
    if not isinstance(name, str) or not _ENTRY_KEY_RE.match(name):
        raise EntryKeyRejected(
            f"{name!r} cannot be an entry name. Names are 1-64 characters of "
            "letters, digits, hyphens and underscores, starting with a letter or "
            "a digit — they become the name of a section in config.toml."
        )
    return name


# ---------------------------------------------------------------------------
# Which table the schema describes
# ---------------------------------------------------------------------------


def settings_table(
    document: Mapping[str, Any], schema: PluginSchema, plugin: str
) -> str | None:
    """The TOML table the schema's properties live in, or ``None`` if unclear.

    ADR-0015 says the schema "describes the same table ``config.toml`` already
    holds" without naming that table, and both bundled plugins put their
    settings under one named table rather than at the document root. So this
    works it out from the file instead of assuming, in the order that gets it
    right for a file that already exists:

    1. If any property the schema declares is already a top-level key, the
       schema describes the document root.
    2. Otherwise, if the document holds exactly one top-level key and that key
       is a table, the schema describes that table — the shape every bundled
       plugin and the template use.
    3. Otherwise, if the document is empty, the plugin's own name with hyphens
       turned into underscores (``random-prompt`` -> ``random_prompt``), which
       is what both bundled plugins named theirs.

    Anything else — several top-level tables, none of them matching — is
    ``None``: ambiguous, so the form is not offered and the raw editor takes it.
    Guessing here would write an operator's settings into a table the plugin
    never reads, which looks exactly like the save silently failing.
    """
    declared = {field.key for field in schema.fields}
    declared.update(item.key for item in schema.unrenderable)
    if declared & set(document):
        return ""
    tables = [key for key, value in document.items() if isinstance(value, dict)]
    if len(document) == 1 and len(tables) == 1:
        return str(tables[0])
    if not document:
        return plugin.replace("-", "_")
    return None


def current_values(document: Mapping[str, Any], table: str) -> dict[str, Any]:
    """The settings table's contents, or an empty mapping if it is not there.

    Never raises for a missing table: a plugin whose ``config.toml`` has not
    been written yet is exactly the plugin whose form an operator wants, and it
    renders from the schema's defaults with nothing filled in.
    """
    if not table:
        return {key: value for key, value in document.items() if not isinstance(value, dict)}
    section = document.get(table)
    return dict(section) if isinstance(section, dict) else {}


# ---------------------------------------------------------------------------
# Submitted values -> validated values
# ---------------------------------------------------------------------------


def field_submitted(field: SchemaField, submitted: Mapping[str, Sequence[str]]) -> bool:
    """Whether this submission was asked about this field at all.

    The hidden :data:`PRESENT_PREFIX` marker is the *reliable* answer and the
    page always sends one. It is not the only answer, and treating it as the
    only one was a real defect: a request that carried
    ``entry.locations.0.latitude`` and no marker had its entries silently
    ignored, and the page then reported a save that had written nothing.

    So a field also counts as submitted when a value that could only belong to
    it is present:

    * a repeating group, when any ``entrykey.``/``entry.``/``entryitem.`` input
      names it — those are text inputs, and a browser always sends them;
    * a list, when a row was sent;
    * anything else, when its own ``field.`` input was sent.

    **A toggle with neither is still "not asked".** That is the whole reason the
    marker exists: an unticked checkbox sends nothing, so without the marker
    "off" and "not on the page" are the same request, and the safe reading of an
    ambiguous request is to leave the file alone.
    """
    if PRESENT_PREFIX + field.key in submitted:
        return True
    if field.kind is FieldKind.ENTRY_GROUP:
        prefixes = (ENTRY_KEY_PREFIX, ENTRY_PREFIX, ENTRY_ITEM_PREFIX)
        return any(
            name.startswith(f"{prefix}{field.key}.")
            for prefix in prefixes
            for name in submitted
        )
    if field.kind is FieldKind.STRING_LIST:
        return ITEM_PREFIX + field.key in submitted
    return FIELD_PREFIX + field.key in submitted


def validate_submission(
    schema: PluginSchema,
    submitted: Mapping[str, Sequence[str]],
    *,
    table: str,
    secret_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Coerce and check every rendered field, or refuse naming all of them.

    Returns ``{key: value}`` where a value of :data:`UNSET` means "remove this
    key from the file" — an optional setting whose box was cleared. Fields the
    submission never mentioned are absent from the result and are left exactly
    as they are on disk, which is what makes a partial form (one that skipped
    the fields it could not render) safe.

    Raises :class:`~personacore.admin.plugin_config_io.PluginConfigInvalid` so
    the existing route mapping answers ``422`` and the settings screen prints
    the sentence, rather than growing a second way to say "that value is wrong".
    """
    values: dict[str, Any] = {}
    problems: list[ConfigProblem] = []
    for field in schema.fields:
        if not field_submitted(field, submitted):
            continue
        if field.kind is FieldKind.ENTRY_GROUP:
            group, group_problems = _entry_group_value(
                field, submitted, table=table, secret_names=secret_names
            )
            if group_problems:
                problems.extend(group_problems)
            else:
                values[field.key] = group
            continue
        try:
            values[field.key] = _one_value(field, submitted, secret_names=secret_names)
        except _Rejected as exc:
            problems.append(exc.as_problem(field, table))
    if problems:
        raise PluginConfigInvalid(
            "Those settings were not saved, so nothing on disk has changed. "
            + _summary(problems),
            problems,
        )
    return values


class _Rejected(Exception):
    """One field's value refused, with the sentence an operator reads."""

    def __init__(self, problem: str, hint: str | None = None) -> None:
        super().__init__(problem)
        self.problem = problem
        self.hint = hint

    def as_problem(self, field: SchemaField, table: str) -> ConfigProblem:
        """Attach the field's key and label.

        The key is the dotted path as it appears in the file
        (``weather.forecast_days``) so an operator who switches to the raw tab
        can find the line; the sentence leads with the label they actually saw
        on the form.
        """
        return ConfigProblem(
            key=f"{table}.{field.key}" if table else field.key,
            problem=f"{field.label}: {self.problem}",
            hint=self.hint,
        )


def _summary(problems: list[ConfigProblem]) -> str:
    """The refusal as one sentence, every problem in it (spec section 9)."""
    if len(problems) == 1:
        return problems[0].problem
    return f"{len(problems)} settings need attention: " + " ".join(
        problem.problem for problem in problems
    )


def _one_value(
    field: SchemaField,
    submitted: Mapping[str, Sequence[str]],
    *,
    secret_names: Sequence[str],
) -> Any:
    """One field's submitted text, coerced and checked. Raises :class:`_Rejected`."""
    if field.kind is FieldKind.STRING_LIST:
        return _list_value(field, submitted.get(ITEM_PREFIX + field.key, ()))

    raw = submitted.get(FIELD_PREFIX + field.key, ())
    text = str(raw[0]) if raw else ""

    if field.kind is FieldKind.TOGGLE:
        # A checkbox submits its value only when ticked, so presence is the
        # answer. The PRESENT_PREFIX marker above is what makes an absent box
        # mean "off" rather than "not asked".
        return bool(raw)

    text = text.replace("\r\n", "\n")
    if field.kind is not FieldKind.TEXTAREA:
        text = text.strip()

    if field.kind is FieldKind.CHOICE:
        if not text:
            return _unset_or_required(field)
        if text not in field.choices:
            raise _Rejected(
                f"{text!r} is not one of the choices this setting allows.",
                "Choose one of: " + ", ".join(field.choices) + ".",
            )
        return text

    if field.kind is FieldKind.SECRET_NAME:
        if not text:
            return _unset_or_required(field)
        if text not in secret_names:
            # The name, never a value: this message is rendered into a page and
            # written to the audit log (spec section 7).
            raise _Rejected(
                f"there is no secret called {text!r} in this core's secret store.",
                "Add the secret to the secrets folder first, then pick it here. "
                "This field holds a secret's name; the value never leaves the store.",
            )
        return text

    if field.kind is FieldKind.NUMBER:
        if not text:
            return _unset_or_required(field)
        return _number_value(field, text)

    if not text:
        return _unset_or_required(field)
    return _string_value(field, text)


def _unset_or_required(field: SchemaField) -> Any:
    """An empty box: refuse it if the setting is required, otherwise remove it.

    "Empty means remove" rather than "empty means write an empty string",
    because a required setting the plugin reads with a fallback wants the key
    *gone* to get that fallback, and an empty string is a value that would
    override it.
    """
    if field.required:
        raise _Rejected(
            "this setting is required and cannot be left empty.",
            "Give it a value, or edit the file in the raw config.toml tab if the "
            "plugin expects something this form cannot express.",
        )
    return UNSET


def _number_value(field: SchemaField, text: str) -> int | float:
    """Parse and bound-check a number field (``minimum``/``maximum`` and the
    exclusive pair — the only numeric keywords the reader renders)."""
    try:
        value: int | float = int(text) if field.integer_only else float(text)
    except ValueError as exc:
        expected = "whole number" if field.integer_only else "number"
        raise _Rejected(
            f"{text!r} is not a {expected}.",
            f"Type a {expected}" + _range_hint(field) + ".",
        ) from exc
    if field.minimum is not None and value < field.minimum:
        raise _Rejected(
            f"{_plain(value)} is below the smallest allowed, {_plain(field.minimum)}.",
            f"Give a value{_range_hint(field)}.",
        )
    if field.maximum is not None and value > field.maximum:
        raise _Rejected(
            f"{_plain(value)} is above the largest allowed, {_plain(field.maximum)}.",
            f"Give a value{_range_hint(field)}.",
        )
    if field.exclusive_minimum is not None and value <= field.exclusive_minimum:
        raise _Rejected(
            f"{_plain(value)} must be more than {_plain(field.exclusive_minimum)}.",
            f"Give a value{_range_hint(field)}.",
        )
    if field.exclusive_maximum is not None and value >= field.exclusive_maximum:
        raise _Rejected(
            f"{_plain(value)} must be less than {_plain(field.exclusive_maximum)}.",
            f"Give a value{_range_hint(field)}.",
        )
    return value


def _string_value(field: SchemaField, text: str) -> str:
    """Length-check a text field (``minLength``/``maxLength``)."""
    if field.max_length is not None and len(text) > field.max_length:
        raise _Rejected(
            f"that is {len(text)} characters long, and the most allowed is "
            f"{field.max_length}.",
            "Shorten it and save again.",
        )
    if field.min_length is not None and len(text) < field.min_length:
        raise _Rejected(
            f"that is {len(text)} characters long, and at least {field.min_length} "
            "are needed.",
            "Give it more text and save again.",
        )
    return text


def _list_value(field: SchemaField, rows: Sequence[str]) -> Any:
    """A list of plain strings, blank rows dropped, then bound-checked.

    Blank rows are dropped rather than rejected because "add" puts an empty row
    on the page: an operator who adds one and changes their mind should be able
    to save, not be told off about a box they never typed in.
    """
    items = [str(row).replace("\r\n", "\n").strip() for row in rows[:MAX_LIST_ITEMS]]
    items = [item for item in items if item]
    if not items and not field.required:
        return UNSET
    if field.min_items is not None and len(items) < field.min_items:
        raise _Rejected(
            f"this list has {len(items)} entr{'y' if len(items) == 1 else 'ies'}, and "
            f"at least {field.min_items} {'is' if field.min_items == 1 else 'are'} "
            "needed.",
            "Use Add to put another one in, then save.",
        )
    if field.max_items is not None and len(items) > field.max_items:
        raise _Rejected(
            f"this list has {len(items)} entries, and the most allowed is "
            f"{field.max_items}.",
            "Remove some entries and save again.",
        )
    for position, item in enumerate(items, start=1):
        if field.item_max_length is not None and len(item) > field.item_max_length:
            raise _Rejected(
                f"entry {position} is {len(item)} characters long, and the most "
                f"allowed is {field.item_max_length}.",
                "Shorten that entry and save again.",
            )
        if field.item_min_length is not None and len(item) < field.item_min_length:
            raise _Rejected(
                f"entry {position} is {len(item)} characters long, and at least "
                f"{field.item_min_length} are needed.",
                "Lengthen or remove that entry and save again.",
            )
    return items


_ENTRY_INDEX_RE: Final = re.compile(r"^[0-9]{1,6}$")


def entry_indices(field: SchemaField, submitted: Mapping[str, Sequence[str]]) -> list[int]:
    """Which entry positions this submission carried, in page order.

    Read from the name inputs rather than from any single field's, because a
    name box is a text input and a browser always submits one — a checkbox or an
    empty list inside an entry submits nothing at all, and an entry discovered
    only through those would come and go depending on what was ticked.
    """
    prefix = f"{ENTRY_KEY_PREFIX}{field.key}."
    found = {
        int(name[len(prefix) :])
        for name in submitted
        if name.startswith(prefix) and _ENTRY_INDEX_RE.match(name[len(prefix) :])
    }
    # One past the ceiling, not the ceiling: the page must not try to render a
    # hostile submission's ten thousand entries, and the validator must still be
    # able to see that there were too many rather than silently accepting the
    # first :data:`MAX_ENTRIES` of them.
    return sorted(found)[: MAX_ENTRIES + 1]


def entry_inputs(
    field: SchemaField, index: int, submitted: Mapping[str, Sequence[str]]
) -> dict[str, list[str]]:
    """One entry's inputs, renamed as though the entry were a form of its own.

    That renaming is the whole trick: an entry's ``latitude`` then goes through
    exactly the same :func:`_one_value` a top-level ``latitude`` goes through,
    with the same bounds check and the same sentence when it is wrong. A second
    validator for "the same field, but indented" is a second validator to keep
    in step, and it would not stay in step.
    """
    row: dict[str, list[str]] = {}
    for sub in field.entry_fields:
        row[PRESENT_PREFIX + sub.key] = ["1"]
        if sub.kind is FieldKind.STRING_LIST:
            name = f"{ENTRY_ITEM_PREFIX}{field.key}.{index}.{sub.key}"
            target = ITEM_PREFIX + sub.key
        else:
            name = f"{ENTRY_PREFIX}{field.key}.{index}.{sub.key}"
            target = FIELD_PREFIX + sub.key
        values = submitted.get(name)
        if values is not None:
            row[target] = [str(value) for value in values]
    return row


def _entry_has_content(field: SchemaField, row: Mapping[str, Sequence[str]]) -> bool:
    """Whether anything was typed into an entry at all.

    An "Add an entry" click puts an empty entry on the page; an operator who
    changes their mind should be able to save rather than be told off about a
    row they never filled in — the same reasoning that drops blank list rows.
    """
    return any(
        str(value).strip()
        for name, values in row.items()
        if not name.startswith(PRESENT_PREFIX)
        for value in values
    )


def _entry_group_value(
    field: SchemaField,
    submitted: Mapping[str, Sequence[str]],
    *,
    table: str,
    secret_names: Sequence[str],
) -> tuple[EntryGroup, list[ConfigProblem]]:
    """Every entry of one repeating group, validated — the plan's item 7.

    Returns the group *and* its problems rather than raising, because an
    operator with a wrong latitude in one entry and a missing name in another
    should be told both at once, each against the entry it belongs to. A
    non-empty problem list means nothing from this group is written.
    """
    where = f"{table}.{field.key}" if table else field.key
    problems: list[ConfigProblem] = []
    entries: dict[str, dict[str, Any]] = {}

    def refuse(problem: str, hint: str | None = None, key: str = where) -> None:
        problems.append(
            ConfigProblem(key=key, problem=f"{field.label}: {problem}", hint=hint)
        )

    indices = entry_indices(field, submitted)
    if len(indices) > MAX_ENTRIES:
        refuse(
            f"this submission holds {len(indices)} entries, and the most this form "
            f"accepts is {MAX_ENTRIES}.",
            "Remove some entries and save again.",
        )
        return EntryGroup({}), problems

    for index in indices:
        raw = submitted.get(f"{ENTRY_KEY_PREFIX}{field.key}.{index}", ())
        name = str(raw[0]).strip() if raw else ""
        row = entry_inputs(field, index, submitted)
        if not name:
            if not _entry_has_content(field, row):
                continue
            refuse(
                "one entry has been filled in but not named.",
                "Give the entry a short name — it is how config.toml refers to it "
                "— or clear its boxes to drop it.",
            )
            continue
        try:
            require_entry_key(name)
        except EntryKeyRejected as exc:
            refuse(str(exc), "Rename that entry and save again.")
            continue
        if name in entries:
            refuse(
                f"there are two entries called {name!r}, and a name can only be used "
                "once.",
                "Rename one of them and save again.",
            )
            continue
        values: dict[str, Any] = {}
        for sub in field.entry_fields:
            try:
                values[sub.key] = _one_value(sub, row, secret_names=secret_names)
            except _Rejected as exc:
                refuse(
                    f"entry {name!r} — {sub.label}: {exc.problem}",
                    exc.hint,
                    key=f"{where}.{name}.{sub.key}",
                )
        entries[name] = values

    floor = field.min_entries if field.min_entries is not None else 0
    if not entries and (field.required or floor > 0):
        refuse(
            f"at least {floor or 1} entr{'y' if (floor or 1) == 1 else 'ies'} "
            f"{'is' if (floor or 1) == 1 else 'are'} needed, and this would leave "
            "none.",
            "Use Add an entry to put one back, then save.",
        )
    elif entries and field.min_entries is not None and len(entries) < field.min_entries:
        refuse(
            f"this has {len(entries)} entries, and at least {field.min_entries} are "
            "needed.",
            "Use Add an entry to put another one in, then save.",
        )
    if field.max_entries is not None and len(entries) > field.max_entries:
        refuse(
            f"this has {len(entries)} entries, and the most allowed is "
            f"{field.max_entries}.",
            "Remove some entries and save again.",
        )
    return EntryGroup(entries), problems


def _plain(value: float) -> str:
    """A number as an operator wrote it — ``7`` rather than ``7.0``.

    Bounds arrive from the schema as floats so one comparison covers integers
    and numbers alike, and a message reading "above the largest allowed, 7.0"
    for a field the schema calls an integer looks like a different rule.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _range_hint(field: SchemaField) -> str:
    """" from 1 to 7", or "" when the schema gave no bounds."""
    low = field.minimum if field.minimum is not None else field.exclusive_minimum
    high = field.maximum if field.maximum is not None else field.exclusive_maximum
    if low is not None and high is not None:
        return f" from {_plain(low)} to {_plain(high)}"
    if low is not None:
        return f" of at least {_plain(low)}"
    if high is not None:
        return f" of at most {_plain(high)}"
    return ""


# ---------------------------------------------------------------------------
# Validated values -> patched TOML text
# ---------------------------------------------------------------------------


def apply_values(content: str, table: str, values: Mapping[str, Any]) -> str:
    """Return ``content`` with ``values`` written into ``table``, comments kept.

    Only the lines belonging to the named keys move. A key already in the table
    has its assignment replaced in place — so the comment above it, which is the
    plugin author's field help, stays where it is. A key that is not there yet is
    appended to the end of that table. A key whose value is :data:`UNSET` is
    removed. Everything else in the file, including settings this form cannot
    render, is carried through byte for byte.

    A value that is an :class:`EntryGroup` is a repeating group (the plan's item
    7) and is written by :func:`apply_group` instead: its entries are tables of
    their own, so adding, editing and removing one is a different edit from
    replacing an assignment — and the one edit that must not take an author's
    comments with it.

    ``content`` must already parse as TOML; the caller has it from
    :func:`~personacore.admin.plugin_config_io.parse_plugin_toml` and falls back
    to the raw editor when it does not.
    """
    if not values:
        return content
    scalars = {
        key: value for key, value in values.items() if not isinstance(value, EntryGroup)
    }
    text = _apply_scalars(content, table, scalars) if scalars else content
    for key, value in values.items():
        if isinstance(value, EntryGroup):
            text = apply_group(text, table, key, value)
    return text


def _apply_scalars(content: str, table: str, values: Mapping[str, Any]) -> str:
    """The assignment-level half of :func:`apply_values`."""
    lines = content.splitlines()
    ends_with_newline = content.endswith(("\n", "\r"))
    start, end = _table_span(lines, table)
    if start is None:
        return _append_table(content, table, values)
    _patch_span(lines, start, end, values)
    text = "\n".join(lines)
    return text + "\n" if ends_with_newline or text else text


def _patch_span(
    lines: list[str], start: int, end: int, values: Mapping[str, Any]
) -> int:
    """Write ``values`` into the body span ``[start, end)``, in place.

    Returns where the span now ends, because an edit can make it shorter or
    longer and the caller may have another span after this one.

    Shared by the table body and by one repeating-group entry's body, so an
    entry's ``latitude`` is replaced by the same code that replaces a top-level
    ``forecast_days``: in place, keeping the line's indentation and the comment
    above it, appending only what was genuinely not there.
    """
    assignments = _assignments(lines, start, end)
    edits: list[tuple[int, int, list[str]]] = []
    appended: list[str] = []
    for key, value in values.items():
        rendered = [] if isinstance(value, _Unset) else _render(key, value)
        span = assignments.get(key)
        if span is not None:
            if _same_value(span[2], value):
                # Already what it should be. Left alone rather than rewritten,
                # so saving a form does not reflow the author's own alignment on
                # every line that happened to be submitted unchanged — and so
                # "adding one entry touched nothing else" is literally true.
                continue
            first = lines[span[0]]
            indent = first[: len(first) - len(first.lstrip())]
            edits.append((span[0], span[1], [indent + line for line in rendered]))
        elif rendered:
            appended.extend(rendered)

    # Bottom-up so an earlier edit cannot shift a later one's line numbers.
    for first, last, replacement in sorted(edits, reverse=True):
        lines[first:last] = replacement
        end += len(replacement) - (last - first)

    if appended:
        # After the table's last real line: trailing blank lines separate this
        # table from the next, and a new setting belongs under the last one
        # rather than after the gap.
        insert_at = end
        while insert_at > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = appended
        end += len(appended)
    return end


def apply_group(content: str, table: str, key: str, group: EntryGroup) -> str:
    """Write one repeating group's entries into ``content`` — the plan's item 7.

    Three edits, and every one of them is deliberately the *smallest* edit that
    achieves the change, because a plugin's ``config.toml`` comments are its
    field help and this is the operation most likely to eat them:

    * **Editing** an entry patches the assignments inside its own
      ``[table.key.name]`` block through :func:`_patch_span`. Nothing outside
      that block moves, and inside it only the changed keys do.
    * **Removing** an entry deletes its header and body — but stops short of any
      blank lines and comments sitting at the end of the block, because those
      belong to whatever comes next. An author's comment introducing the entry
      *below* must not disappear because the entry above it was removed.
    * **Adding** an entry appends a new block after the last existing one, or at
      the end of the file when there is none. Nothing already in the file is
      touched at all, including the author's comments above the first entry.

    Refuses, rather than guesses, when the group is written in a shape this
    writer cannot patch safely (an inline table, an array of tables, a table
    deeper than one entry). The refusal is a :class:`ValueError` carrying a
    sentence for the operator, and the page sends them to the raw editor — where
    the file is theirs and this writer is not involved.
    """
    lines = content.splitlines()
    ends_with_newline = content.endswith(("\n", "\r"))
    prefix = (table, key) if table else (key,)
    # Found once and passed down: deciding whether a line starting with "[" is a
    # header means parsing everything above it, so scanning for them repeatedly
    # is the one thing here that could be slow on a large file.
    headers = _header_lines(lines)
    _refuse_unpatchable(lines, table, key, prefix, headers)

    blocks = _entry_blocks(lines, prefix, headers)
    existing = {name for name, _, _, _ in blocks}
    # Bottom-up: an edit or a deletion higher in the file would shift every
    # block below it.
    for name, header, body_start, block_end in reversed(blocks):
        if name in group.entries:
            _patch_span(lines, body_start, block_end, group.entries[name])
        else:
            del lines[header : _keep_from(lines, header, block_end)]

    added = [name for name in group.entries if name not in existing]
    if added:
        remaining = _entry_blocks(lines, prefix, _header_lines(lines))
        insert_at = remaining[-1][3] if remaining else len(lines)
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        appended: list[str] = []
        for name in added:
            # Checked again here, at the point the name becomes a section
            # header. The submitted values were validated long before this, but
            # the one line in this codebase that builds a TOML header out of a
            # name should not depend on a caller elsewhere having done so.
            require_entry_key(name)
            body: list[str] = []
            for sub_key, value in group.entries[name].items():
                if not isinstance(value, _Unset):
                    body.extend(_render(sub_key, value))
            # A blank line before each new section, except at the very top of an
            # otherwise empty file, where it would just be a stray first line.
            separator = [] if insert_at == 0 and not appended else [""]
            appended.extend([*separator, "[" + ".".join([*prefix, name]) + "]", *body])
        lines[insert_at:insert_at] = appended

    text = "\n".join(lines)
    return text + "\n" if ends_with_newline or text else text


def _keep_from(lines: Sequence[str], header: int, block_end: int) -> int:
    """Where a removed entry's deletion must stop.

    Trailing blank lines and comments at the end of a block are, to a reader,
    the introduction to whatever comes after it. Deleting them with the entry is
    how an operator removing one location loses the paragraph the plugin author
    wrote about the next one.
    """
    cut = block_end
    while cut > header + 1 and (
        not lines[cut - 1].strip() or lines[cut - 1].lstrip().startswith("#")
    ):
        cut -= 1
    return cut


def _refuse_unpatchable(
    lines: Sequence[str],
    table: str,
    key: str,
    prefix: tuple[str, ...],
    headers: Sequence[int],
) -> None:
    """Refuse a group written in a shape this writer will not touch.

    Each of these is legal TOML that the form *could* read and could not safely
    rewrite. Refusing keeps the promise that matters more than the feature: a
    save either does exactly what the operator asked or does nothing at all.
    """
    start, end = _table_span(lines, table)
    if start is not None and key in _assignments(lines, start, end):
        raise ValueError(
            f"{key!r} is written on one line in this config.toml, and this form only "
            "edits entries written as their own [sections]. Edit it in the raw "
            "config.toml tab."
        )
    for position, index in enumerate(headers):
        parsed = _header_path(lines[index])
        if parsed is None:
            continue
        path, is_array = parsed
        if tuple(path[: len(prefix)]) != prefix:
            continue
        if is_array:
            raise ValueError(
                f"{key!r} is written as a list of sections ([[...]]) in this "
                "config.toml, which this form does not edit. Edit it in the raw "
                "config.toml tab."
            )
        following = headers[position + 1] if position + 1 < len(headers) else len(lines)
        if len(path) == len(prefix) and _assignments(lines, index + 1, following):
            raise ValueError(
                f"{key!r} has its entries written on single lines in this "
                "config.toml, and this form only edits entries written as their own "
                "[sections]. Edit it in the raw config.toml tab."
            )
        if len(path) > len(prefix) + 1:
            raise ValueError(
                f"{key!r} has an entry with sections of its own in this config.toml, "
                "which this form does not edit. Edit it in the raw config.toml tab."
            )


def _entry_blocks(
    lines: Sequence[str], prefix: tuple[str, ...], headers: Sequence[int]
) -> list[tuple[str, int, int, int]]:
    """``(entry name, header line, first body line, one past the last)`` per entry.

    In file order, which is also the order the page lists them in: an operator
    who arranged their locations in a file has arranged them, and a form that
    re-sorted them would be rewriting a decision it was not asked about.
    """
    blocks: list[tuple[str, int, int, int]] = []
    for position, index in enumerate(headers):
        parsed = _header_path(lines[index])
        if parsed is None:
            continue
        path, is_array = parsed
        if is_array or len(path) != len(prefix) + 1 or tuple(path[:-1]) != prefix:
            continue
        following = headers[position + 1] if position + 1 < len(headers) else len(lines)
        blocks.append((path[-1], index, index + 1, following))
    return blocks


def _header_path(line: str) -> tuple[tuple[str, ...], bool] | None:
    """``[weather.locations.home]`` -> ``(("weather", "locations", "home"), False)``.

    Parsed by ``tomllib`` rather than by splitting on dots, because a header may
    quote a part (``[a."b.c"]``) and a hand-written splitter would read that as
    two names — which, for code that decides whether to *delete* a block, is the
    kind of wrong that removes the wrong settings.
    """
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    try:
        parsed: Any = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    node: Any = parsed
    while isinstance(node, dict) and len(node) == 1:
        name, value = next(iter(node.items()))
        path.append(str(name))
        node = value
    if not path:
        return None
    return tuple(path), stripped.startswith("[[")


def _render(key: str, value: Any) -> list[str]:
    """One key's assignment as TOML lines.

    ``tomli_w`` rather than hand-built quoting: escaping a string correctly is
    exactly the kind of thing that looks done and is not, and the dependency is
    already approved for writing ``core.toml``.

    Refuses a value that would need a table header of its own — a repeating
    group (the plan's item 7). That is not a limitation of the writer so much as
    a decision deferred: where a new ``[[table]]`` belongs in a file full of an
    author's comments is the group editor's problem, and it will replace this
    guard when it is built.
    """
    text = tomli_w.dumps({key: value})
    rendered = text.splitlines()
    if any(line.startswith("[") for line in rendered):
        raise ValueError(
            f"{key!r} holds a group of settings, which this form does not write. "
            "Edit it in the raw config.toml tab."
        )
    return rendered


def _table_span(lines: Sequence[str], table: str) -> tuple[int | None, int]:
    """``(first body line, one past the last)`` for ``table``.

    ``(None, 0)`` when the table has no header in the file. The document root is
    everything before the first header, which is where a schema-less flat file
    keeps its settings.
    """
    headers = _header_lines(lines)
    if not table:
        return (0, headers[0] if headers else len(lines))
    for position, index in enumerate(headers):
        if _header_name(lines[index]) != table:
            continue
        following = headers[position + 1] if position + 1 < len(headers) else len(lines)
        return (index + 1, following)
    return (None, 0)


def _header_lines(lines: Sequence[str]) -> list[int]:
    """Every line that really starts a table, in order.

    "Really" is the work: a ``[`` at the start of a line is usually a header and
    is also every line of a multi-line array of arrays, which is what
    :func:`_inside_value` settles.
    """
    return [
        index
        for index, line in enumerate(lines)
        if line.lstrip().startswith("[") and not _inside_value(lines, index)
    ]


def _header_name(line: str) -> str | None:
    """``[weather]`` -> ``weather``. ``None`` for anything else, including
    ``[[array.of.tables]]`` and a dotted header, neither of which this form
    writes into."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    closing = stripped.find("]")
    if closing < 0:
        return None
    name = stripped[1:closing].strip()
    if name.startswith(('"', "'")) and name.endswith(('"', "'")) and len(name) >= 2:
        name = name[1:-1]
    return name or None


def _inside_value(lines: Sequence[str], index: int) -> bool:
    """Whether line ``index`` is inside a multi-line value rather than starting
    one.

    A ``[`` at the start of a line is usually a table header, but it is also
    every line of a multi-line array of arrays. Deciding by parsing — see
    :func:`_assignments` — is the only way that does not turn into a hand-rolled
    TOML lexer, so this asks whether everything above parses *and* leaves the
    parser mid-value.
    """
    prefix = "\n".join(lines[:index])
    if not prefix.strip():
        return False
    try:
        tomllib.loads(prefix)
    except tomllib.TOMLDecodeError:
        return True
    return False


def _same_value(existing: Any, value: Any) -> bool:
    """Whether the file already holds this value, so the line need not move.

    ``bool`` is compared apart from the numbers on purpose: Python says
    ``True == 1``, and treating a toggle as "already correct" because the file
    holds ``1`` would leave a plugin reading an integer where its schema
    promises a boolean.
    """
    if isinstance(existing, bool) != isinstance(value, bool):
        return False
    try:
        return bool(existing == value)
    except Exception:  # noqa: BLE001 - an exotic value is simply not equal
        return False


def _assignments(
    lines: Sequence[str], start: int, end: int
) -> dict[str, tuple[int, int, Any]]:
    """``key -> (first line, one past the last, the value it holds)`` per assignment.

    Found by parsing rather than by pattern-matching: lines are accumulated from
    the start of an assignment until ``tomllib`` accepts them, which is exactly
    where that assignment ends. A multi-line array, a triple-quoted string and a
    trailing comment all fall out of that for free, and none of them needs a
    regular expression that would be wrong about one of the three.
    """
    found: dict[str, tuple[int, int]] = {}
    cursor = start
    while cursor < end:
        stripped = lines[cursor].strip()
        if not stripped or stripped.startswith("#"):
            cursor += 1
            continue
        for last in range(cursor + 1, end + 1):
            chunk = "\n".join(lines[cursor:last])
            try:
                parsed = tomllib.loads(chunk)
            except tomllib.TOMLDecodeError:
                continue
            for key, value in parsed.items():
                found.setdefault(str(key), (cursor, last, value))
            cursor = last
            break
        else:
            # Nothing from here on parses on its own. The document as a whole
            # does (the caller checked), so this is a construct outside what
            # this patcher understands; the keys already found are still right,
            # and anything after is appended rather than replaced.
            break
    return found


def _append_table(content: str, table: str, values: Mapping[str, Any]) -> str:
    """Add a table that is not in the file yet, with its settings under it.

    Reached for a plugin whose ``config.toml`` does not exist or does not hold
    the table the schema describes — a fresh install, saved for the first time.
    """
    body: list[str] = []
    for key, value in values.items():
        if not isinstance(value, _Unset):
            body.extend(_render(key, value))
    if not body:
        return content
    header = f"[{table}]" if table else ""
    prefix = content if not content or content.endswith("\n") else content + "\n"
    if prefix.strip():
        prefix += "\n"
    return prefix + ("\n".join([header, *body]) if header else "\n".join(body)) + "\n"


_MISSING: Final = object()
""""This key is not in the file", for comparing two versions of a table."""


def changed_keys(
    before: str, after: str, table: str, values: Mapping[str, Any]
) -> list[str]:
    """Which of the submitted settings actually read differently now.

    Submitted is not the same as changed. A form posts every field it shows, and
    :func:`_patch_span` deliberately leaves a key alone when the file already
    holds that value — so a page that reported every submitted key as "saved"
    would be naming settings it did not touch. That is a smaller version of the
    same lie as reporting a save that wrote nothing, and it is fixed the same
    way: by comparing the file with the file.

    Falls back to the submitted keys if either version does not parse, which
    cannot happen from this module's own output but would otherwise turn a
    reporting detail into a failed save.
    """
    try:
        old = current_values(tomllib.loads(before), table)
        new = current_values(tomllib.loads(after), table)
    except tomllib.TOMLDecodeError:  # pragma: no cover - both sides are our own
        return sorted(values)
    return sorted(
        key
        for key in values
        if not _same_value(old.get(key, _MISSING), new.get(key, _MISSING))
    )


def removed_keys(values: Mapping[str, Any]) -> list[str]:
    """Which submitted settings mean "take this key out of the file".

    Used by the page to say so — an operator who clears a box and gets no word
    about it cannot tell a removal from a save that quietly did nothing.
    """
    return sorted(key for key, value in values.items() if isinstance(value, _Unset))


def written_keys(values: Mapping[str, Any]) -> list[str]:
    """Which submitted settings carry a value, for the audit record.

    Names only. What the values *are* is the operator's document and this list
    goes into a store that is backed up (spec section 7).
    """
    return sorted(key for key, value in values.items() if not isinstance(value, _Unset))


def is_unset(value: Any) -> bool:
    """Whether a validated value means "remove the key"."""
    return isinstance(value, _Unset)


def form_inputs(raw: Iterable[tuple[str, Any]]) -> dict[str, list[str]]:
    """A Starlette ``FormData`` flattened into ``name -> [value, ...]``.

    Kept here rather than in the page so this module can be tested against a
    plain dictionary, and so the one place that decides what a repeated input
    name means is the one place that reads them.
    """
    values: dict[str, list[str]] = {}
    for name, value in raw:
        values.setdefault(str(name), []).append(value if isinstance(value, str) else "")
    return values


__all__ = [
    "ENTRY_ITEM_PREFIX",
    "ENTRY_KEY_PATTERN",
    "ENTRY_KEY_PREFIX",
    "ENTRY_PREFIX",
    "FIELD_PREFIX",
    "ITEM_PREFIX",
    "MAX_ENTRIES",
    "MAX_LIST_ITEMS",
    "PRESENT_PREFIX",
    "UNSET",
    "EntryGroup",
    "EntryKeyRejected",
    "apply_group",
    "apply_values",
    "changed_keys",
    "current_values",
    "entry_indices",
    "entry_inputs",
    "field_submitted",
    "form_inputs",
    "is_unset",
    "removed_keys",
    "require_entry_key",
    "settings_table",
    "validate_submission",
    "written_keys",
]
