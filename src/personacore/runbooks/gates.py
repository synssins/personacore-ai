"""Gate steps — ``working/contracts/runbook.md`` §4, and §2's two gate shapes.

A **gate** is the step where a run stops. Either it stops for a *person* —
questions with click options and an Other box, whose answers become the step's
answer file — or it stops for a *condition*, which passes or sends the run
back round a loop without asking anybody anything.

This module is the whole of what a gate *is*, and none of what a gate *does*:
the questions' shape (:class:`Question`, :class:`GateQuestions`), the fixed
prompt that asks a model for them (:func:`questions_prompt`), the parser that
gets JSON out of a reply written by something that likes to explain itself
(:func:`parse_questions`), the one line an answer becomes
(:func:`answer_line`), and the typed conditions an auto gate is decided by
(:func:`evaluate`). Nothing here talks to the LLM, the workspace or the chat —
that is :mod:`personacore.runbooks.runner`'s half, exactly as
:mod:`personacore.runbooks.state` and :mod:`personacore.runbooks.roles` are
split from it for the same reason.

**Why the conditions are typed.** Contract §2's example wrote an auto gate's
condition as a sentence (``pass_when: "no line contains '| high |'"``), which
would need a parser for English — and a parser for English is a thing that is
wrong occasionally and silently, on a step whose whole job is to decide
whether a run carries on. So ``pass_when:`` is a mapping with exactly one key
naming one of five conditions; anything else is refused at upload, in plain
words, by :mod:`personacore.runbooks.validate`. This module holds the names
(:data:`PASS_WHEN_CONDITIONS`) so the schema's refusal and the evaluation
below can never drift apart.

**Why the question models are lenient about extra keys.** Every other model in
this package sets ``extra="forbid"``, because every other model is reading a
file a *person* wrote and a typo there is worth refusing. These two read JSON
a *model* wrote, and the cost of refusing is not a corrected file — it is the
person losing the click options and being handed a text box instead
(:data:`~personacore.runbooks.runner.WAITING_FOR_ANSWERS`, text mode). An
extra key nobody asked for changes nothing about the questions, so it is
ignored rather than paid for.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

MAX_QUESTIONS = 20
"""A gate asks about the flags in one file. Twenty is not a rule anybody
stated; it is the point past which a model has stopped turning flags into
questions and started inventing them, and a person facing a hundred click
boxes would rather have the file and a text box."""

MIN_OPTIONS = 2
MAX_OPTIONS = 4
"""Contract §4: "two to four options". Two, because one option is not a
question; four, because the Other box is always there for the answer the
model did not think of."""

MAX_REF_CHARS = 40
MAX_QUOTE_CHARS = 600
"""Contract §4, added 2026-09-05: a passage's ``ref`` and ``quote`` are each
capped rather than refused — a model that quotes a paragraph a little too
generously is still handing over the evidence a person needs to answer, and
refusing it would fall the gate back to the raw-file card the whole change
exists to avoid."""

MAX_ROLE_CHARS = 40
"""Contract §4, added 2026-09-06: a passage's ``role`` is capped the same
way its ``ref`` already is (:data:`MAX_REF_CHARS`) and for the same
reason — a role name a little too long is still a role name, and refusing
it would cost the person the comparison card over a formatting slip."""

OTHER = "Other"
"""How a typed answer is labelled in the answer file — contract §4's own
example line, ``q1: Other — the visitor is the neighbour's cousin; they have only just met.``"""


class GateError(Exception):
    """A gate's questions could not be read, or a condition could not be
    evaluated, and here is the sentence to show.

    ``message`` is plain English and safe to put in front of a person: it is
    what the chat says when a gate falls back to a text answer, and what a
    refused answer is refused with.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------


def _capped(text: str, limit: int) -> str:
    """``text``, cut to ``limit`` characters with a trailing ellipsis when it
    was longer. Never refused — see :data:`MAX_QUOTE_CHARS`."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


class Passage(BaseModel):
    """One passage a question is about — contract §4, added 2026-09-05:
    "every question carries the passages it is about, quoted word for word
    from the file the gate reads." ``ref`` is a ``¶N`` mark or, for a file
    with none, the passage's first three words; both fields are capped
    rather than refused (:func:`_capped`)."""

    ref: str
    quote: str
    role: str | None = None
    """Which pinned role this passage was quoted from — contract §4, added
    2026-09-06: a comparison question quotes the same passage from two
    roles, and the web (``web/screens/chat_run.py``'s gate card) needs to
    know which is which to open the right file and label each pane.
    ``None`` for a question about the one file the gate itself reads, where
    a role is not needed to tell two passages apart, and for a ``.run.json``
    gate state written before this field existed — old gate states without
    ``role`` still load (contract §5)."""

    @field_validator("ref")
    @classmethod
    def _cap_ref(cls, value: str) -> str:
        return _capped(value, MAX_REF_CHARS)

    @field_validator("quote")
    @classmethod
    def _cap_quote(cls, value: str) -> str:
        return _capped(value, MAX_QUOTE_CHARS)

    @field_validator("role")
    @classmethod
    def _cap_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _capped(value, MAX_ROLE_CHARS)


class Question(BaseModel):
    """One question at a gate — contract §4's own JSON, field for field."""

    id: str
    text: str
    options: list[str] = Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)
    other: bool = True
    context: list[Passage] = Field(default_factory=list)
    """The passages this question is about, added 2026-09-05 so a question
    never asks about evidence the card does not show. Optional — a question
    with none renders exactly as before."""

    @model_validator(mode="before")
    @classmethod
    def _drop_named_other(cls, data: Any) -> Any:
        """Contract §4, added 2026-09-06: "An option the model named
        'Other' (any case) is dropped; the only Other is the one that lets
        the author type." Done on the raw options the model wrote, before
        the field's own two-to-four-option count is checked, so a model
        that (redundantly) listed its own "Other" is not refused for a
        count that only looks short once the duplicate is gone.

        Forces ``other: true`` whenever an option was actually dropped — a
        model that named its own "Other" and also, wrongly, turned the
        built-in one off must not cost the person the ability to type,
        which is the one case dropping the option could otherwise take
        away.
        """
        if not isinstance(data, Mapping):
            return data
        options = data.get("options")
        if not isinstance(options, list):
            return data
        kept = [
            option
            for option in options
            if not (isinstance(option, str) and option.strip().casefold() == OTHER.casefold())
        ]
        if len(kept) == len(options):
            return data
        updated = dict(data)
        updated["options"] = kept
        updated["other"] = True
        return updated

    @field_validator("id", "text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("options")
    @classmethod
    def _options_not_blank(cls, value: list[str]) -> list[str]:
        if any(not option.strip() for option in value):
            raise ValueError("every option must say something")
        return value


class GateQuestions(BaseModel):
    """Every question one gate asks, in the order they are asked.

    ``questions`` may be **empty** (contract §4, added 2026-09-05): a flag
    file where nothing needs a decision is a pass, not a malformed reply, so
    the gate itself decides what an empty list means rather than this shape
    refusing it before that choice can be made."""

    questions: list[Question] = Field(max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> GateQuestions:
        seen: set[str] = set()
        for question in self.questions:
            if question.id in seen:
                raise ValueError(f"question id {question.id!r} is used more than once")
            seen.add(question.id)
        return self


QUESTIONS_PROMPT = """\
Below is a file of flags raised about a piece of work. Turn each flag that \
needs a decision into one question for the person who decides it.

Reply with JSON and nothing else. No explanation, no code fence, no heading:

{"questions": [{"id": "q1", "text": "Is this deliberate?", "options": \
["Keep as written", "Change it"], "other": true, "context": \
[{"ref": "¶24", "quote": "the passage, quoted word for word", "role": \
"p4"}]}]}

- One decision per question, in the order raised, at most 20; ids q1, q2, ...
- Two to four options: the actions this file makes possible, in the \
author's own words ("Keep as written", "Change to ..."), never a bare \
Yes/No pair. Never name one of them "Other" — the person always has a box \
of their own to type in; do not offer it again as a choice.
- Quote every passage the question is about, word for word, as a "context" \
entry naming its paragraph mark, or its first three words when the file has \
none. Name the role you quoted it from — the pinned blocks below are each \
headed "pinned as [role] (file)" — as that entry's "role". For a question \
comparing two versions of one passage, quote it once from each role, as two \
separate "context" entries.
- No question for a flag that already says it is consistent, clean, \
verified or needs no change. If none needs a decision, reply \
{"questions": []}.
- Set "other" to true on every question, so a person can type an answer of \
their own.

Ask about nothing the file does not raise, and answer none of it yourself.

The file:
"""
"""The fixed core prompt of contract §4, rewritten 2026-09-05 so a question
never arrives without the evidence it is about: one decision per question,
options in the author's own words, every passage quoted word for word, and
no question for a flag that needs no decision.

Fixed in the core and not in the runbook on purpose. A gate's questions are
the one turn in a run whose *output shape* the core has to be able to rely on
— everything else a runbook asks for lands in a file nobody parses — so the
instruction that produces that shape belongs to the code that parses it,
where a runbook author cannot reword it into something that no longer
answers in JSON."""


def questions_prompt(file_text: str) -> str:
    """The whole user turn for a ``questions: model`` gate.

    The file is pinned as well (the runner does that); it is repeated inline
    here because the prompt has to end by pointing at something, and a pinned
    block is not somewhere a sentence can point.
    """
    return f"{QUESTIONS_PROMPT}\n{file_text}"


def parse_questions(text: str) -> GateQuestions:
    """The questions out of a reply, or :class:`GateError` saying why not.

    A model asked for JSON very often supplies JSON *and* an apology, or JSON
    inside a code fence, or both. So the reply is read from its first ``{`` to
    its last ``}`` — which strips a fence, a heading and a closing remark in
    one move without needing to know which of them happened — and only that
    slice is parsed. Everything the slice is not (no braces at all, invalid
    JSON, or JSON of the wrong shape) is one failure with one sentence,
    because a person facing the fallback only needs to know that the questions
    could not be read, not which of three ways it went wrong.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise GateError("the questions did not come back as JSON.")
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GateError(f"the questions were not valid JSON ({exc.msg}).") from exc
    try:
        return GateQuestions.model_validate(doc)
    except PydanticValidationError as exc:
        raise GateError(f"the questions were not the right shape ({_first(exc)}).") from exc


def _first(exc: PydanticValidationError) -> str:
    """The first pydantic complaint, as a phrase rather than a traceback."""
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "it did not validate"
    err = errors[0]
    where = ".".join(str(part) for part in err["loc"])
    message = str(err["msg"])
    prefix = "Value error, "
    if message.startswith(prefix):
        message = message[len(prefix) :]
    return f"{where}: {message}" if where else message


# ---------------------------------------------------------------------------
# The answer file
# ---------------------------------------------------------------------------


def answer_line(
    question: Question,
    choice: str | None,
    other: str | None,
    *,
    compare: tuple[str, str] | None = None,
) -> str:
    """One line of the answer file — contract §4's own shape.

    ``q1: Yes`` for a click, ``q1: Other — <what they typed>`` for the box.
    A typed answer wins whenever there is one, because the box only opens
    when a person chose to type instead of click, and the click that opened
    it is not the answer.

    ``compare``, added contract §4 2026-09-06: ``(earlier_role, later_role)``
    when this question is a comparison — the passage exists in two of the
    files the review step read. A ``choice`` of ``left`` or ``right`` then
    means which pane was picked, and the line is the contract's own
    comparison phrasing (``q4: use the original version (text)`` /
    ``q4: keep the current version (p4)``) rather than one of the question's
    own options. The caller works out ``compare`` from the gate's own state
    — never from anything a form could claim — and passes ``None`` for
    every other question, where ``left``/``right`` are refused exactly like
    any option the question did not offer.

    Refuses, in a sentence, an answer that is neither: a choice the question
    did not offer (a stale page, or something posting at the API directly),
    or typed text on a question whose ``other`` is off.
    """
    typed = (other or "").strip()
    if typed:
        if not question.other:
            raise GateError(f"{question.id} does not take a typed answer.")
        return f"{question.id}: {OTHER} — {typed}"
    picked = (choice or "").strip()
    if not picked:
        raise GateError(f"{question.id} was not answered.")
    if compare is not None and picked in ("left", "right"):
        earlier_role, later_role = compare
        if picked == "left":
            return f"{question.id}: use the original version ({earlier_role})"
        return f"{question.id}: keep the current version ({later_role})"
    if picked not in question.options:
        raise GateError(f"{picked!r} is not one of the answers to {question.id}.")
    return f"{question.id}: {picked}"


# ---------------------------------------------------------------------------
# The auto gate's conditions
# ---------------------------------------------------------------------------

PASS_WHEN_CONDITIONS: dict[str, str] = {
    "no_line_contains": "text",
    "any_line_contains": "text",
    "file_empty": "flag",
    "file_exists": "role",
    "line_count_at_most": "count",
}
"""The five conditions an ``auto`` gate may be written with, and the kind of
value each takes. :mod:`personacore.runbooks.schema` refuses anything else at
upload; :func:`evaluate` refuses it again here, because a condition that was
valid when it was uploaded is still being read by a different build of this
core every time the container is updated."""

_VALUE_WORD = {
    "text": "the text to look for, in quotes",
    "flag": "true or false",
    "role": "the role of a file, in quotes",
    "count": "a whole number",
}


def condition_refusal(reason: str) -> str:
    """The plain sentence a badly written ``pass_when:`` is refused with.

    One function so the schema's refusal, the validator's refusal of the old
    sentence form, and :func:`evaluate`'s own last line of defence all end the
    same way — with the list of what a person could have written instead.
    """
    named = ", ".join(f"'{name}'" for name in PASS_WHEN_CONDITIONS)
    return f"{reason} A gate's 'pass_when' names exactly one of {named}."


def check_pass_when(pass_when: Any) -> tuple[str, Any]:
    """One condition's name and value, or :class:`GateError`.

    Shared by the schema (at upload) and :func:`evaluate` (at run time), so
    "is this a condition" has one answer written once.
    """
    if not isinstance(pass_when, Mapping):
        raise GateError(condition_refusal("'pass_when' is written as one condition and its value."))
    if len(pass_when) != 1:
        counted = "nothing" if not pass_when else f"{len(pass_when)} things"
        raise GateError(condition_refusal(f"'pass_when' names {counted}."))
    key, value = next(iter(pass_when.items()))
    kind = PASS_WHEN_CONDITIONS.get(str(key))
    if kind is None:
        raise GateError(condition_refusal(f"'pass_when' names {key!r}, which is not a condition."))
    if not _value_fits(kind, value):
        raise GateError(f"'pass_when: {key}' takes {_VALUE_WORD[kind]}.")
    return str(key), value


def _value_fits(kind: str, value: Any) -> bool:
    if kind == "flag":
        return isinstance(value, bool)
    if kind == "count":
        # `bool` is an `int` subclass in Python, which is exactly backwards
        # for a count — the same trap `schema.RunbookInput` already refuses.
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return isinstance(value, str) and bool(value)


def evaluate(pass_when: Mapping[str, Any], file_text: str, files: Mapping[str, Any]) -> bool:
    """Whether an ``auto`` gate passes.

    ``file_text`` is the content of the file the gate's ``from:`` role names;
    ``files`` is every role produced so far, which only ``file_exists`` looks
    at. Line conditions are substring matches on whole lines, deliberately —
    contract §2's own example is ``| high |``, a fragment of a Markdown table
    row, and a person writing that is matching text and not a pattern.
    """
    key, value = check_pass_when(pass_when)
    lines = file_text.splitlines()
    if key == "no_line_contains":
        return not any(value in line for line in lines)
    if key == "any_line_contains":
        return any(value in line for line in lines)
    if key == "file_empty":
        return bool(value) == (not file_text.strip())
    if key == "file_exists":
        return value in files
    return _content_lines(file_text) <= int(value)


def _content_lines(text: str) -> int:
    """Lines that say something. Blank lines are spacing a person put in for
    readability, and counting them would make ``line_count_at_most`` a
    condition about formatting rather than about content."""
    return sum(1 for line in text.splitlines() if line.strip())


__all__ = [
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "MAX_QUOTE_CHARS",
    "MAX_REF_CHARS",
    "MAX_ROLE_CHARS",
    "MIN_OPTIONS",
    "OTHER",
    "PASS_WHEN_CONDITIONS",
    "QUESTIONS_PROMPT",
    "GateError",
    "GateQuestions",
    "Passage",
    "Question",
    "answer_line",
    "check_pass_when",
    "condition_refusal",
    "evaluate",
    "parse_questions",
    "questions_prompt",
]
