"""Role resolution — contract §2: "Roles, not names."

Three small, pure pieces a runbook step needs, none of which know anything
about the LLM, a plugin tool, or the chat:

* :func:`substitute` — the *only* templating a runbook file gets:
  ``{{ input }}`` and ``{{ files.role }}``, nothing else. No expressions, no
  filters, no arithmetic — a runbook author who needs more than that is
  writing the wrong kind of file.
* :func:`resolve_new_file` — a step's ``files:`` pattern (contract §2, a
  glob such as ``"Character_*.md"``) against what a tool actually left
  behind: exactly one new match, or the step fails with the count.
* :func:`resolve_pins` — a step's ``pins:`` roles against every role
  produced so far, or the step fails naming the one role nobody produced.
* :func:`parse_items` — what a ``range``/``list`` input's text means as a
  list of items (contract §1.12), so ``foreach:`` has something to iterate.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from typing import Any

_TOKEN_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
"""``{{ ... }}``, whitespace-tolerant just inside the braces — ``{{ book }}``
and ``{{book}}`` are the same reference."""

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
"""An input name — the same shape ``schema.INPUT_NAME_RE`` already requires
of one, so a template can only ever match a name a runbook could actually
declare."""

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
"""A file role — the same shape ``schema.STEP_ID_RE`` already requires of a
step id, since a role is either a step id or a key from a step's own
``files:`` mapping, and both are written to that rule."""


class RoleError(Exception):
    """A step's roles or template could not be resolved. The message is
    plain English, safe to show a person as the reason a run parked."""


def substitute(template: str, inputs: Mapping[str, Any], files: Mapping[str, str]) -> str:
    """Replace every ``{{ input }}`` and ``{{ files.role }}`` in ``template``.

    Anything else inside ``{{ }}`` — an unknown input name, an unknown file
    role, or anything that is not one of those two plain shapes (contract
    §2: "no expressions") — raises :class:`RoleError` naming the exact
    reference that could not be resolved.
    """

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        inner = match.group(1)

        if inner.startswith("files."):
            role = inner[len("files.") :]
            if not _ROLE_RE.fullmatch(role):
                raise RoleError(
                    f"{raw!r} is not a plain file reference — only '{{{{ files.role }}}}' "
                    "is allowed, no expressions."
                )
            if role not in files:
                raise RoleError(
                    f"{raw!r} names file role {role!r}, which no earlier step produced."
                )
            return files[role]

        if not _NAME_RE.fullmatch(inner):
            raise RoleError(
                f"{raw!r} is not a plain input or file reference — only '{{{{ name }}}}' "
                "and '{{ files.role }}' are allowed, no expressions."
            )
        if inner not in inputs:
            raise RoleError(f"{raw!r} names input {inner!r}, which this runbook does not declare.")
        return str(inputs[inner])

    return _TOKEN_RE.sub(_replace, template)


def resolve_new_file(pattern: str, before: set[str], after: set[str]) -> str:
    """The one new file ``pattern`` matches.

    ``before``/``after`` are the workspace's file names immediately before
    and after the step ran; only the files that appeared during the step
    (``after - before``) are candidates, so a pattern can never accidentally
    pick up a file some earlier step left behind. Zero or several matches
    both raise :class:`RoleError` naming the count — contract §3, "a
    ``files:`` pattern must match exactly one new file."
    """
    new_files = after - before
    matches = sorted(name for name in new_files if fnmatch.fnmatch(name, pattern))
    if len(matches) != 1:
        raise RoleError(f"matched {len(matches)} files.")
    return matches[0]


# ---------------------------------------------------------------------------
# Iteration — what a `range`/`list` input's text means (contract §1.12)
# ---------------------------------------------------------------------------

MAX_ITEMS = 500
"""How many items one input may expand to.

Contract §1.12 puts no number on it, and none of the shapes a person actually
types ("1-12", "1,3,5") comes anywhere near this. The ceiling is here because
a runbook-level ``foreach`` starts **one conversation per item**, so a typo
with an extra zero in it is not a slow run — it is a few thousand
conversations and a few thousand workspaces, made one at a time, with nothing
in the middle to say "did you mean this?". Refusing at the point the text is
read is the only place that question can still be asked.
"""

_RANGE_PART_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
"""``1-12`` — two whole numbers, low to high. Deliberately digits only: a
minus sign inside a range would make ``1--3`` and ``-3-1`` questions nobody
should have to answer, and a chapter is never negative."""

_RANGE_REFUSAL = "write one number, a range like 1-12, or a list like 1,3,5."

_LIST_REFUSAL = "write the items separated by commas, like 1,3,5."


def parse_items(input_type: str, text: str) -> list[Any]:
    """The items a ``range``/``list`` input stands for, in the order given.

    ``range`` is whole numbers: ``"1-12"`` counts out 1 to 12, ``"1,3,5"`` is
    those three, ``"7"`` is one item, and the two spellings mix (``"1-3,7"``).
    ``list`` is text taken verbatim, comma-separated — no number is parsed out
    of it, because a list of chapter *names* is exactly what the type is for.

    Repeats are dropped, keeping the first of each: a runbook-level
    ``foreach`` runs one conversation per item, and asking for chapter 3 twice
    is a typo rather than a request for two identical runs.

    Raises :class:`RoleError` with a sentence a person can act on — the same
    plain-English contract every other refusal in this module keeps.
    """
    if input_type == "range":
        return _deduplicated(_range_items(text))
    if input_type == "list":
        return _deduplicated(_list_items(text))
    raise RoleError(
        f"an input of type {input_type!r} is one value, not several — "
        "only 'range' and 'list' can be iterated over."
    )


def _range_items(text: str) -> list[int]:
    parts = [part.strip() for part in str(text).split(",")]
    if not any(parts):
        raise RoleError(_RANGE_REFUSAL)
    values: list[int] = []
    for part in parts:
        if not part:
            # A stray comma ("1,,3") is a typo, not an empty chapter.
            raise RoleError(_RANGE_REFUSAL)
        found = _RANGE_PART_RE.fullmatch(part)
        if found is not None:
            first, last = int(found.group(1)), int(found.group(2))
            if last < first:
                raise RoleError(f"{part!r} counts backwards — a range is written low to high.")
            if last - first + 1 > MAX_ITEMS:
                raise RoleError(f"{part!r} is more than {MAX_ITEMS} items.")
            values.extend(range(first, last + 1))
            continue
        if not part.isdigit():
            raise RoleError(_RANGE_REFUSAL)
        values.append(int(part))
    return values


def _list_items(text: str) -> list[str]:
    values = [part.strip() for part in str(text).split(",")]
    kept = [part for part in values if part]
    if not kept:
        raise RoleError(_LIST_REFUSAL)
    return kept


def _deduplicated(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    kept: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        kept.append(value)
    if len(kept) > MAX_ITEMS:
        raise RoleError(f"that is {len(kept)} items, and {MAX_ITEMS} is as many as one run takes.")
    return kept


def resolve_pins(step_pins: list[str], outputs_by_role: Mapping[str, str]) -> dict[str, str]:
    """The step's ``pins:`` roles resolved to actual filenames.

    A role no earlier step produced raises :class:`RoleError` naming it —
    the validator (``validate.py``) already checks this against the whole
    runbook at upload time, but a ``foreach``/skipped-step run can still
    reach this at run time (contract §1.11/§1.12), so the runner needs the
    same refusal here, not just a promise it was checked once before.
    """
    resolved: dict[str, str] = {}
    for role in step_pins:
        if role not in outputs_by_role:
            raise RoleError(f"pins role {role!r}, which no earlier step produced.")
        resolved[role] = outputs_by_role[role]
    return resolved


__all__ = [
    "MAX_ITEMS",
    "RoleError",
    "parse_items",
    "resolve_new_file",
    "resolve_pins",
    "substitute",
]
