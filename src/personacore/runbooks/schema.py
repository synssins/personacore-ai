"""The runbook file, as pydantic models — ``working/contracts/runbook.md`` §2.

A runbook is a self-describing YAML file: what it does, which plugins it
needs, and an ordered list of steps the core runs inside one conversation.
This module is the schema **only** — every key in contract §2, verbatim, with
``extra="forbid"`` everywhere so a typo in a runbook is refused rather than
silently ignored (the same reasoning every settings model in this codebase
already follows, see :mod:`personacore.config.settings`).

What is deliberately **not** here: the structural checks contract §6 asks for
(unique step ids, a pinned role produced by an earlier step, a `goto` target
that exists, a prompt file that was actually uploaded) live in
:mod:`personacore.runbooks.validate`, because they need the *whole* runbook
at once and cannot be expressed as one field's own constraint. Compatibility
against installed plugins — the other half of contract §1.8 — lives in
:mod:`personacore.runbooks.compat`, because it needs facts about installed
plugins this module has no business knowing.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from personacore.runbooks.gates import GateError, check_pass_when

# The runbook's own id, and the file name it is stored under (contract §2).
RUNBOOK_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")

# A step id, and the token a template or a `pins`/`goto`/`from`/`then` entry
# names. Kept separate from RUNBOOK_ID_RE because a step id is also used as a
# template-safe identifier (`{{ files.role }}`), which a bare digit-and-hyphen
# id would not need to be, but restricting it the same way costs nothing and
# keeps one rule instead of two.
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# An input name — used verbatim inside `{{ ... }}`, so it has to be a token a
# template can match unambiguously.
INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# `<plugin>.<tool>` — the same two shapes the manifest itself enforces
# (`contracts/manifest.py`), joined by exactly one dot.
_PLUGIN_TOOL_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}\.[a-z][a-z0-9_-]*$")

# Loose but real semver: major.minor.patch, optional prerelease/build. This
# module only needs to know a runbook's own `version` LOOKS like a version —
# comparing two of them is `compat.py`'s job, against a plugin's specifier,
# never against another runbook's version.
_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

RESERVED_BUDGET_KEYS = frozenset({"reasoning_budget", "budget"})
"""Contract §2: **"There is no budget key; the budget is the model server's."**

Checked recursively over the raw document by :mod:`personacore.runbooks.
validate` before this module ever sees it — a schema field could refuse the
key at the top of a step, but ``args`` is an open mapping by design (a tool's
own arguments), so a schema-level refusal could never see one hiding inside
it. Named here, once, so the validator's refusal and this comment agree on
the two spellings."""

BUDGET_REFUSAL = "the thinking budget is set on the model server, not in a runbook."

MAX_STEP_TITLE = 60
"""SPEC alpha.22, "every step says what it is for": the cap on a step's own
``title:``. Short by design — it is the whole of what a progress row adds to
a step id ("p5 · Audit — running")."""

MAX_STEP_DESCRIPTION = 200
"""SPEC alpha.22's cap on a step's own ``description:`` — the sentence the
gate card and the run form's step preview show, written (per the wiki) for
the person reading it, not for the model."""


def _check_step_text(step_id: str, title: str | None, description: str | None) -> None:
    """Refuse a step's ``title``/``description`` only for being too long —
    both are optional, so ``None`` never trips this — with a sentence naming
    the step and the field, which pydantic's own default message for a
    length cap ("String should have at most 60 characters") names neither.

    Shared by every step kind's own ``model_validator`` rather than a mixin
    base class: the three step models already differ enough (a discriminated
    union keyed on ``kind``) that one more shared ancestor would cost more to
    read than the three near-identical lines it would save.
    """
    if title is not None and len(title) > MAX_STEP_TITLE:
        raise ValueError(
            f"step {step_id!r}'s title is {len(title)} characters; "
            f"{MAX_STEP_TITLE} is the most a title may be."
        )
    if description is not None and len(description) > MAX_STEP_DESCRIPTION:
        raise ValueError(
            f"step {step_id!r}'s description is {len(description)} characters; "
            f"{MAX_STEP_DESCRIPTION} is the most a description may be."
        )


class ValidationError(Exception):
    """The runbook could not be accepted. Never one problem — every one found.

    Distinct from :class:`pydantic.ValidationError`, on purpose: this is the
    shape :func:`personacore.runbooks.validate.validate_runbook` raises, with
    messages already translated into the plain English contract §6 and spec
    section 9 both ask for. Nothing downstream should have to read a pydantic
    traceback to show an operator what is wrong with their file.
    """

    def __init__(self, messages: list[str]) -> None:
        self.messages: list[str] = list(messages)
        super().__init__("; ".join(self.messages) if self.messages else "the runbook is invalid")


# ---------------------------------------------------------------------------
# requires:
# ---------------------------------------------------------------------------


class RunbookRequires(BaseModel):
    """``requires:`` — every plugin and tool a runbook needs (contract §2)."""

    model_config = ConfigDict(extra="forbid")

    plugins: dict[str, str] = Field(default_factory=dict)
    """Plugin name -> minimum-version specifier, e.g. ``{"weather": ">=1.4.3"}``.

    The specifier is parsed with :class:`packaging.specifiers.SpecifierSet` at
    validation time — here, so a runbook with an unparseable specifier is
    refused at upload rather than at the moment somebody tries to start it.
    """

    tools: list[str] = Field(default_factory=list)
    """``<plugin>.<tool>`` names this runbook calls. Each must be declared by
    the plugin named before the dot — checked in
    :mod:`personacore.runbooks.compat`, which is the module that actually
    knows what a plugin declares."""

    @field_validator("plugins")
    @classmethod
    def _check_specifiers(cls, value: dict[str, str]) -> dict[str, str]:
        for plugin, specifier in value.items():
            try:
                SpecifierSet(specifier)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"'requires.plugins.{plugin}' names {specifier!r}, which is not "
                    "a version specifier a plugin's version can be checked against "
                    "(a PEP 440 specifier such as '>=1.4.3')."
                ) from exc
        return value

    @field_validator("tools")
    @classmethod
    def _check_tool_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _PLUGIN_TOOL_RE.fullmatch(name):
                raise ValueError(
                    f"'requires.tools' names {name!r}, which is not written as "
                    "'<plugin>.<tool>' (for example 'weather.forecast')."
                )
        return value


# ---------------------------------------------------------------------------
# inputs:
# ---------------------------------------------------------------------------

InputType = Literal["integer", "string", "boolean", "range", "list"]

ITERABLE_INPUT_TYPES = frozenset({"range", "list"})
"""Contract §1.12: the two input types that stand for *several* values, and
so the only two a ``foreach:`` may name. Both are written as text in the
picker ("1-12", "1,3,5") and turned into items by
:func:`personacore.runbooks.roles.parse_items` when the run starts — which is
why both are ``str`` below: what a person types is text, and what it means is
not this module's question."""

_PYTHON_TYPE_FOR: dict[str, type] = {
    "integer": int,
    "string": str,
    "boolean": bool,
    "range": str,
    "list": str,
}


class RunbookInput(BaseModel):
    """One question asked in the picker before the run starts (contract §2)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: InputType
    prompt: str
    default: Any = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not INPUT_NAME_RE.fullmatch(value):
            raise ValueError(
                f"input name {value!r} must be lowercase letters, digits and "
                "underscores, starting with a letter — it is used verbatim inside "
                "'{{ }}' templates."
            )
        return value

    @field_validator("prompt")
    @classmethod
    def _check_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an input's 'prompt' must say what is being asked.")
        return value

    @model_validator(mode="after")
    def _default_matches_type(self) -> RunbookInput:
        if self.default is None:
            return self
        expected = _PYTHON_TYPE_FOR[self.type]
        # bool is an int subclass in Python, so an integer input's default of
        # `true`/`false` would otherwise pass an `isinstance(int)` check —
        # exactly backwards for a type this explicit about what it is.
        matches = (
            isinstance(self.default, bool)
            if self.type == "boolean"
            else isinstance(self.default, expected) and not isinstance(self.default, bool)
        )
        if not matches:
            raise ValueError(
                f"input {self.name!r} declares type {self.type!r} but its default "
                f"{self.default!r} is not one."
            )
        return self


# ---------------------------------------------------------------------------
# steps: tool
# ---------------------------------------------------------------------------

Scalar = str | int | float | bool


class ToolStep(BaseModel):
    """``kind: tool`` — one call to a plugin tool with fixed arguments."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool"]
    id: str
    title: str | None = None
    """SPEC alpha.22: what this step is for, in a few words — shown in
    progress rows ("p5 · Audit — running") and the run form's step preview.
    Optional; a step with none reads exactly as one always has."""
    description: str | None = None
    """SPEC alpha.22: a sentence for the person reading a progress row or
    the run form, not for the model. Optional, and never sent to a model."""
    tool: str
    args: dict[str, Scalar] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    """Role -> the filename (or glob pattern) this step is expected to leave.
    Named once here; every later step refers to it by role only."""

    pin: list[str] = Field(default_factory=list)
    """Roles from this step's own ``files`` (or an earlier step's) that the
    *next* step needs pinned. Named ``pin`` — singular — for a tool step,
    ``pins`` for a model step; both are contract §2's own spelling."""

    foreach: str | None = None
    """Contract §1.12: run *this step* once per item of the named ``range``/
    ``list`` input (stamp every chapter of a book), with ``{{ item }}``
    available in ``args`` and ``files``. The step's roles then hold one
    filename per item. Only a tool step may iterate: a model step repeated
    per item is a different conversation each time, which is the *runbook*-
    level ``foreach`` below, and a gate repeated per item is the same gate
    asked again. That the named input exists and is one of those two types is
    checked in :mod:`personacore.runbooks.validate`, which can see the whole
    file at once."""

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(_step_id_message(value))
        return value

    @field_validator("tool")
    @classmethod
    def _check_tool(cls, value: str) -> str:
        if not _PLUGIN_TOOL_RE.fullmatch(value):
            raise ValueError(
                f"step tool {value!r} is not written as '<plugin>.<tool>' "
                "(for example 'weather.forecast')."
            )
        return value

    @model_validator(mode="after")
    def _check_text(self) -> ToolStep:
        _check_step_text(self.id, self.title, self.description)
        return self


# ---------------------------------------------------------------------------
# steps: model
# ---------------------------------------------------------------------------


class Watchdog(BaseModel):
    """A model step's per-turn ceiling (contract §3, "Watchdog")."""

    model_config = ConfigDict(extra="forbid")

    max_output_tokens: int | None = Field(default=None, gt=0)
    max_output_ratio: float | None = Field(default=None, gt=0)
    max_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _exactly_one_output_bound(self) -> Watchdog:
        both = self.max_output_tokens is not None and self.max_output_ratio is not None
        neither = self.max_output_tokens is None and self.max_output_ratio is None
        if both or neither:
            raise ValueError(
                "a 'watchdog' names exactly one of 'max_output_tokens' or "
                "'max_output_ratio', never both and never neither."
            )
        return self


class ApplySpec(BaseModel):
    """A model step's ``apply:`` — patching an earlier step's file in place."""

    model_config = ConfigDict(extra="forbid")

    anchors: str
    onto: str


class ModelStep(BaseModel):
    """``kind: model`` — one scripted turn (contract §2)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["model"]
    id: str
    title: str | None = None
    """SPEC alpha.22: what this step is for — see :class:`ToolStep`'s own."""
    description: str | None = None
    """SPEC alpha.22: a sentence for the person reading it. See
    :class:`ToolStep`'s own."""
    prompt: str | None = None
    prompt_text: str | None = None
    thinking: Literal["on", "off"]
    temperature: float | None = Field(default=None, ge=0)
    endpoint: str | None = None
    pins: list[str] = Field(default_factory=list)
    output: str
    watchdog: Watchdog | None = None
    apply: ApplySpec | None = None
    then: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(_step_id_message(value))
        return value

    @model_validator(mode="after")
    def _exactly_one_prompt(self) -> ModelStep:
        both = self.prompt is not None and self.prompt_text is not None
        neither = self.prompt is None and self.prompt_text is None
        if both or neither:
            raise ValueError(
                f"step {self.id!r} names exactly one of 'prompt' or 'prompt_text', "
                "never both and never neither."
            )
        return self

    @model_validator(mode="after")
    def _check_text(self) -> ModelStep:
        _check_step_text(self.id, self.title, self.description)
        return self


# ---------------------------------------------------------------------------
# steps: gate
# ---------------------------------------------------------------------------

QuestionsSource = Literal["model", "file"]


class AutoElse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goto: str
    max_loops: int = Field(ge=1)


class AutoGate(BaseModel):
    """``gate.auto`` — no person: pass or loop on a **typed** condition.

    ``pass_when:`` is a mapping with exactly one key, naming one of the five
    conditions :data:`personacore.runbooks.gates.PASS_WHEN_CONDITIONS` holds.
    Contract §2's example wrote it as an English sentence (``"no line
    contains '| high |'"``); that would need a parser for English on the one
    step whose whole job is deciding whether a run carries on, and a parser
    for English is wrong occasionally and silently. The sentence form is
    refused by name in :mod:`personacore.runbooks.validate`, so a runbook
    written against the older shape is told what to write instead rather than
    told its file is not a mapping.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pass_when: dict[str, Any]
    else_: AutoElse = Field(alias="else")

    @field_validator("pass_when")
    @classmethod
    def _one_known_condition(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            check_pass_when(value)
        except GateError as exc:
            raise ValueError(exc.message) from exc
        return value


class GateStep(BaseModel):
    """``kind: gate`` — one of two shapes, never both (contract §2, §4).

    Either a *human* gate (``questions`` + ``answer``) or an *auto* gate
    (``auto``); :meth:`_exactly_one_shape` refuses a file that mixes them or
    supplies neither, because a gate with no way to resolve is a run that can
    never leave it.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["gate"]
    id: str
    title: str | None = None
    """SPEC alpha.22: what this step is for — see :class:`ToolStep`'s own.
    A human gate's title/description are shown as the gate card's own
    heading (``web/screens/chat_run.py``); an auto gate has no card and
    these are simply never read for one."""
    description: str | None = None
    """SPEC alpha.22: a sentence for the person answering the gate, not for
    the model. See :class:`ToolStep`'s own."""
    from_: str = Field(alias="from")
    questions: QuestionsSource | None = None
    answer: str | None = None
    auto: AutoGate | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(_step_id_message(value))
        return value

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> GateStep:
        human = self.questions is not None or self.answer is not None
        if human and self.auto is not None:
            raise ValueError(
                f"gate {self.id!r} names both a person's questions and an 'auto' "
                "condition. A gate is answered one way or the other, never both."
            )
        if human and (self.questions is None or self.answer is None):
            raise ValueError(
                f"gate {self.id!r} names one of 'questions'/'answer' without the "
                "other. A human gate needs both: where the questions come from, "
                "and the role the answer file is pinned under."
            )
        if not human and self.auto is None:
            raise ValueError(
                f"gate {self.id!r} names neither a person's questions nor an "
                "'auto' condition, so nothing could ever resolve it."
            )
        return self

    @model_validator(mode="after")
    def _check_text(self) -> GateStep:
        _check_step_text(self.id, self.title, self.description)
        return self


def _step_id_message(value: str) -> str:
    return (
        f"step id {value!r} must be lowercase letters, digits, hyphens and "
        "underscores, starting with a letter."
    )


Step = Annotated[ToolStep | ModelStep | GateStep, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# The runbook itself
# ---------------------------------------------------------------------------


class Runbook(BaseModel):
    """The whole file, contract §2 verbatim."""

    model_config = ConfigDict(extra="forbid")

    runbook: str
    version: str
    format: int = Field(ge=1)
    title: str
    description: str
    requires: RunbookRequires = Field(default_factory=RunbookRequires)
    persona: str | None = None
    foreach: str | None = None
    """Contract §1.12: run the **whole runbook** once per item of the named
    ``range``/``list`` input. One conversation per item (never one
    conversation holding every item's turns), driven by a parent run that
    holds the item list — see
    :meth:`personacore.runbooks.runner.Runner.start`."""

    inputs: list[RunbookInput] = Field(default_factory=list)
    steps: list[Step]

    @field_validator("runbook")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not RUNBOOK_ID_RE.fullmatch(value):
            raise ValueError(
                f"'runbook' {value!r} must be lowercase letters, digits and "
                "hyphens, 1-40 characters — it is also the file name."
            )
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not _SEMVER_RE.fullmatch(value.strip()):
            raise ValueError(
                f"'version' {value!r} does not look like a semantic version (for example '1.1.0')."
            )
        return value

    @field_validator("title", "description")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank.")
        return value

    @field_validator("steps")
    @classmethod
    def _at_least_one_step(cls, value: list[Step]) -> list[Step]:
        if not value:
            raise ValueError("'steps' is empty — a runbook with nothing to run.")
        return value


__all__ = [
    "BUDGET_REFUSAL",
    "ITERABLE_INPUT_TYPES",
    "MAX_STEP_DESCRIPTION",
    "MAX_STEP_TITLE",
    "RESERVED_BUDGET_KEYS",
    "RUNBOOK_ID_RE",
    "STEP_ID_RE",
    "ApplySpec",
    "AutoElse",
    "AutoGate",
    "GateStep",
    "InputType",
    "ModelStep",
    "Runbook",
    "RunbookInput",
    "RunbookRequires",
    "Scalar",
    "Step",
    "ToolStep",
    "ValidationError",
    "Watchdog",
]
