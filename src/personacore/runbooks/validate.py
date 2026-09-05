"""Turning a runbook file into a :class:`~personacore.runbooks.schema.Runbook`,
or a list of plain-English reasons it cannot be — ``working/contracts/runbook.md``
§6.

Three passes, each one only reached if the one before it found nothing:

1. **Is it YAML, and is the top level a mapping.**
2. **Does no key anywhere spell the thinking budget** (contract §2: "There is
   no budget key; the budget is the model server's.") — checked over the raw
   document, ahead of schema validation, because ``args:`` is an open mapping
   and a schema field cannot see into it.
3. **Does it match the schema** (:mod:`personacore.runbooks.schema`).
4. **Is it internally consistent** — unique step ids, every pinned role
   produced by an earlier step, every ``goto``/``then``/``from`` target that
   exists, every template that names an input or a produced file role, every
   referenced prompt file actually supplied, and a supported ``format``.

Compatibility against *installed plugins* is deliberately not here — see
:mod:`personacore.runbooks.compat`. A runbook can be entirely well-formed and
still unusable because a plugin is missing, off, or too old, and those are
different failures shown in different places (contract §1.8 vs §6).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from personacore.runbooks.schema import (
    BUDGET_REFUSAL,
    RESERVED_BUDGET_KEYS,
    GateStep,
    ModelStep,
    Runbook,
    Scalar,
    ToolStep,
    ValidationError,
)

SUPPORTED_FORMAT = 1
"""The newest runbook ``format`` this core understands (contract §2: "the
core refuses a newer one"). A version bump to the format itself is a code
change here, never a setting."""

TEMPLATE_PREFIX = "files."


def validate_runbook(text: str, prompts: Mapping[str, str]) -> Runbook:
    """Parse and validate one runbook file. Raises :class:`ValidationError`.

    Args:
        text: The runbook YAML, as uploaded or read from appdata.
        prompts: Every prompt file that came with it, keyed by the *relative
            path a step's* ``prompt:`` *field would name* (e.g.
            ``"prompts/p1.md"``), valued by its content. A runbook with no
            prompt files (every model step uses ``prompt_text``, or there are
            no model steps) is validated against an empty mapping.
    """
    raw = _parse_yaml(text)
    if _has_reserved_key(raw):
        raise ValidationError([BUDGET_REFUSAL])

    try:
        runbook = Runbook.model_validate(raw)
    except PydanticValidationError as exc:
        raise ValidationError(_translate(exc)) from exc

    problems = _check_structure(runbook, prompts)
    if problems:
        raise ValidationError(problems)
    return runbook


# ---------------------------------------------------------------------------
# Pass 1 — YAML, and a mapping at the top
# ---------------------------------------------------------------------------


def _parse_yaml(text: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError([f"the runbook file is not valid YAML: {exc}"]) from exc
    if raw is None:
        raise ValidationError(["the runbook file is empty."])
    if not isinstance(raw, dict):
        raise ValidationError(
            ["a runbook file must be a YAML mapping of 'key: value' pairs at the top level."]
        )
    return raw


# ---------------------------------------------------------------------------
# Pass 2 — no key anywhere spells the thinking budget
# ---------------------------------------------------------------------------


def _has_reserved_key(node: Any) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key in RESERVED_BUDGET_KEYS:
                return True
            if _has_reserved_key(value):
                return True
        return False
    if isinstance(node, list):
        return any(_has_reserved_key(item) for item in node)
    return False


# ---------------------------------------------------------------------------
# Pass 3 — schema, translated to plain English
# ---------------------------------------------------------------------------


def _translate(exc: PydanticValidationError) -> list[str]:
    """A pydantic error, as sentences — the same translation
    :func:`personacore.config.settings._describe` does for ``core.toml``,
    kept separate because that function is private to a different module and
    the two error shapes (a settings document vs. a runbook) are not the
    same thing wearing the same clothes by accident."""
    messages = []
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(top level)"
        message = str(err["msg"])
        prefix = "Value error, "
        if message.startswith(prefix):
            message = message[len(prefix) :]
        messages.append(f"{where}: {message}")
    return messages


# ---------------------------------------------------------------------------
# Pass 4 — structural checks, contract §6
# ---------------------------------------------------------------------------


def _check_structure(runbook: Runbook, prompts: Mapping[str, str]) -> list[str]:
    problems: list[str] = []

    step_ids = [step.id for step in runbook.steps]
    all_ids = set(step_ids)
    seen_ids: set[str] = set()
    for step_id in step_ids:
        if step_id in seen_ids:
            problems.append(f"step id {step_id!r} is used more than once.")
        seen_ids.add(step_id)

    # The full set of things a `pins`/`pin` entry, or a `{{ files.role }}`
    # template, may legally name at this point in the file — contract §2:
    # "the reserved roles `canon` and inputs" plus whatever an earlier step
    # produced (its `files` keys, or its own id for a model or gate step's
    # single output).
    produced: set[str] = {"canon"} | {item.name for item in runbook.inputs}
    input_names = {item.name for item in runbook.inputs}

    for step in runbook.steps:
        if isinstance(step, ToolStep):
            _check_templates(step.id, step.args, input_names, produced, problems)
            _check_templates(step.id, step.files, input_names, produced, problems)
            # A tool step's `pin` names roles to carry forward, and — unlike
            # a model step's `pins` — that is allowed to include roles this
            # SAME step's own `files:` just produced (contract §2's own
            # example: the `fetch` step declares `files: {stamped: ..., ...}`
            # and immediately pins `[stamped, sheets]`). So its own files are
            # added to `produced` before, not after, the pin check — the one
            # place in this function where a step's own output is eligible
            # for its own pin.
            produced.update(step.files)
            _check_pins(step.id, step.pin, produced, problems)
            produced.add(step.id)
        elif isinstance(step, ModelStep):
            _check_pins(step.id, step.pins, produced, problems)
            # `output` and `prompt_text` are file-name/text templates the
            # same way a tool step's `args`/`files` are, and `pins` entries
            # are checked the same way too (harmless when, as usual, they
            # are bare role names with no `{{ }}` in them at all).
            template_fields: dict[str, Scalar] = {"output": step.output}
            if step.prompt_text is not None:
                template_fields["prompt_text"] = step.prompt_text
            for index, role in enumerate(step.pins):
                template_fields[f"pins[{index}]"] = role
            _check_templates(step.id, template_fields, input_names, produced, problems)
            if step.prompt is not None and step.prompt not in prompts:
                problems.append(
                    f"step {step.id}'s prompt file {step.prompt!r} was not "
                    "uploaded with this runbook."
                )
            if step.then is not None and step.then not in all_ids:
                problems.append(
                    f"step {step.id}'s 'then' names step {step.then!r}, which does not exist."
                )
            if step.apply is not None:
                _check_reads(step.id, step.apply.onto, produced, problems)
            produced.add(step.id)
        elif isinstance(step, GateStep):
            _check_reads(step.id, step.from_, produced, problems)
            if step.answer is not None:
                _check_templates(step.id, {"answer": step.answer}, input_names, produced, problems)
            if step.auto is not None and step.auto.else_.goto not in all_ids:
                problems.append(
                    f"step {step.id}'s 'goto' names step "
                    f"{step.auto.else_.goto!r}, which does not exist."
                )
            produced.add(step.id)

    if runbook.format > SUPPORTED_FORMAT:
        problems.append(
            f"this runbook is written for format {runbook.format}, and this "
            f"core supports up to format {SUPPORTED_FORMAT}."
        )

    return problems


def _check_pins(step_id: str, roles: list[str], produced: set[str], problems: list[str]) -> None:
    for role in roles:
        if role not in produced:
            problems.append(f"step {step_id} pins role {role}, which nothing produces")


def _check_reads(step_id: str, role: str, produced: set[str], problems: list[str]) -> None:
    """A gate's ``from`` and a model step's ``apply.onto`` both *read* one
    role a previous step produced — a tool step's ``files`` key, a model or
    gate step's own id standing for its single output, or the reserved
    ``canon`` — never a step id as such. Contract §2/§6: these are role
    reads, the same shape as a pinned role, not a jump to a step."""
    if role not in produced:
        problems.append(f"step {step_id} reads role {role}, which nothing before it produces")


def _check_templates(
    step_id: str,
    mapping: Mapping[str, Scalar],
    input_names: set[str],
    produced: set[str],
    problems: list[str],
) -> None:
    for value in mapping.values():
        if not isinstance(value, str):
            continue
        for name in _template_names(value):
            if name.startswith(TEMPLATE_PREFIX):
                role = name[len(TEMPLATE_PREFIX) :]
                if role not in produced:
                    problems.append(
                        f"step {step_id} uses '{{{{ {name} }}}}', which nothing has produced yet."
                    )
            elif name not in input_names:
                problems.append(
                    f"step {step_id} uses '{{{{ {name} }}}}', which is not an "
                    "input or a pinned file role."
                )


def _template_names(value: str) -> list[str]:
    """Every ``{{ name }}`` in ``value``, contract §2: "Templates are
    ``{{ input }}`` and ``{{ files.role }}`` only. No expressions." — so this
    reads the token whole rather than trying to parse an expression out of
    it."""
    return re.findall(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}", value)


__all__ = ["SUPPORTED_FORMAT", "validate_runbook"]
