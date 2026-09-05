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
    """Plugin name -> minimum-version specifier, e.g. ``{"vesmark": ">=1.4.3"}``.

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
                    "'<plugin>.<tool>' (for example 'vesmark.lint')."
                )
        return value


# ---------------------------------------------------------------------------
# inputs:
# ---------------------------------------------------------------------------

InputType = Literal["integer", "string", "boolean"]

_PYTHON_TYPE_FOR: dict[str, type] = {
    "integer": int,
    "string": str,
    "boolean": bool,
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
    tool: str
    args: dict[str, Scalar] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    """Role -> the filename (or glob pattern) this step is expected to leave.
    Named once here; every later step refers to it by role only."""

    pin: list[str] = Field(default_factory=list)
    """Roles from this step's own ``files`` (or an earlier step's) that the
    *next* step needs pinned. Named ``pin`` — singular — for a tool step,
    ``pins`` for a model step; both are contract §2's own spelling."""

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
                "(for example 'vesmark.fetch')."
            )
        return value


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


# ---------------------------------------------------------------------------
# steps: gate
# ---------------------------------------------------------------------------

QuestionsSource = Literal["model", "file"]


class AutoElse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goto: str
    max_loops: int = Field(ge=1)


class AutoGate(BaseModel):
    """``gate.auto`` — no person: pass or loop on a parsed condition."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pass_when: str
    else_: AutoElse = Field(alias="else")


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
