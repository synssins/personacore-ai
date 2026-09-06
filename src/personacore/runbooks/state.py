"""A run's state — ``.run.json``, contract §5 (resume) and §3 (running).

A **run** belongs to one conversation and its workspace. This module is the
whole of that state: the shapes (:class:`StepState`, :class:`RunState`), how
they are written and read (:func:`write_state`, :func:`read_state`), and the
pure transitions a run goes through (:func:`new_state`, :func:`advance`,
:func:`fail`, :func:`stop`, :func:`park`, :func:`mark_interrupted`,
:func:`plan_resume`). **Nothing here talks to the LLM, a plugin tool, or the
chat** — that is :mod:`personacore.runbooks.runner`'s job; this module only
knows about the dataclasses and the one file they live in.

``.run.json`` follows the hidden-file convention :mod:`personacore.workspaces`
already established for ``.sources.json`` and ``.pins.json``: a leading dot,
so :meth:`~personacore.workspaces.Workspace.list` never shows it and no tool
argument can ever name it. This module works directly against a workspace
*directory* (a bare :class:`~pathlib.Path`), not a
:class:`~personacore.workspaces.Workspace` instance — the runner already
holds one of those for its own reasons, and passing the raw directory keeps
this module from needing to know anything about that class at all.

**Atomic write.** Contract §5: a torn ``.run.json`` is reported, never
silently reset. :func:`write_state` writes the full document to
``.run.json.tmp`` first, ``fsync``s it, then :func:`os.replace` renames it
onto ``.run.json`` — a rename is atomic on every filesystem this project
targets, so a reader can never observe a half-written file. :func:`read_state`
never looks at the ``.tmp`` name at all.

**Pure transitions.** Every transition function takes a :class:`RunState` and
returns a new one; none of them touch disk. The caller (the runner) decides
when to persist a transition with :func:`write_state`, and in what order, so
a step that fails partway through does not leave a state file half-updated.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from personacore.runbooks.schema import Runbook

STATE_FILENAME = ".run.json"
"""Hidden — never listed, never addressable, exactly like ``.sources.json``
and ``.pins.json`` in :mod:`personacore.workspaces`."""

_TMP_SUFFIX = ".tmp"

STATE_FORMAT = 1
"""The run-state format this build writes and understands, contract §5's
own forward-compatibility field — the same idea as a runbook's own
``format:`` (``schema.py``): a file written by a newer core than this one
is refused rather than misread."""

RunStatus = Literal["running", "parked", "stopped", "done", "failed", "interrupted"]


class StateUnreadable(Exception):
    """``.run.json`` exists but cannot be trusted: torn JSON, a missing
    field, or a ``format`` this build does not understand.

    Never silently reset — contract §5. ``reason`` is a plain-English
    sentence safe to show a person in the chat ("run state unreadable: …").
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    PARKED = "parked"


@dataclass
class GateState:
    """One step's gate progress — questions, answers, and loop count."""

    mode: Literal["questions", "text", "auto"]
    """The kind of gate: model-generated questions, text-answer mode, or auto."""
    questions: list[dict] = field(default_factory=list)
    """Question dicts as parsed from the model or file."""
    answers: dict[str, str] = field(default_factory=dict)
    """question id -> answer line."""
    loops: int = 0
    """Auto-gate loop count."""
    source_file: str | None = None
    """The ``from:`` step's own output filename, added contract §4
    2026-09-05 — what the chat's gate card reads to show the flag line a
    question came from and to link the file. ``None`` for a gate this build
    parked before the field existed, or for an auto gate, which has no card."""
    source_role: str | None = None
    """The ``from:`` role :attr:`source_file` was pinned under — kept beside
    the filename because a role is what a runbook author reads and a
    filename is what the workspace reads, and the card needs to say both."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "questions": list(self.questions),
            "answers": dict(self.answers),
            "loops": self.loops,
            "source_file": self.source_file,
            "source_role": self.source_role,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GateState:
        return cls(
            mode=str(data["mode"]),
            questions=list(data.get("questions", [])),
            answers={str(k): str(v) for k, v in dict(data.get("answers", {})).items()},
            loops=int(data.get("loops", 0)),
            # `.get(..., None)` rather than `["source_file"]`: contract §5,
            # "keep old files readable" — a `.run.json` written before this
            # field existed has neither key at all.
            source_file=data.get("source_file"),
            source_role=data.get("source_role"),
        )


@dataclass
class StepState:
    """One step's own progress inside a run."""

    id: str
    kind: str
    status: StepStatus
    outputs: dict[str, str | list[str]]
    """Role -> the filename(s) this step actually produced, so far.
    A list only when the step iterated."""
    started: str | None
    finished: str | None
    reason: str | None
    gate: GateState | None = None
    """Gate progress if this step has one, otherwise None."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": str(self.status),
            "outputs": dict(self.outputs),
            "started": self.started,
            "finished": self.finished,
            "reason": self.reason,
            "gate": self.gate.to_dict() if self.gate else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StepState:
        outputs_raw = dict(data.get("outputs", {}))
        outputs = {}
        for k, v in outputs_raw.items():
            if isinstance(v, list):
                outputs[str(k)] = [str(item) for item in v]
            else:
                outputs[str(k)] = str(v)

        gate_data = data.get("gate")
        gate = GateState.from_dict(gate_data) if gate_data else None

        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            status=StepStatus(data["status"]),
            outputs=outputs,
            started=data.get("started"),
            finished=data.get("finished"),
            reason=data.get("reason"),
            gate=gate,
        )


@dataclass
class ItemState:
    """One item in a parent run's foreach iteration."""

    value: Any
    """The item value."""
    conversation_id: str | None = None
    """Conversation where this item's run happened."""
    status: str = "pending"
    """Item status: pending, done, parked, failed."""
    reason: str | None = None
    """Reason the item parked or failed, if applicable."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ItemState:
        return cls(
            value=data.get("value"),
            conversation_id=data.get("conversation_id"),
            status=str(data.get("status", "pending")),
            reason=data.get("reason"),
        )


@dataclass
class RunState:
    """The whole of one run's state — the document ``.run.json`` holds."""

    runbook: str
    version: str
    plugin: str
    persona: str
    inputs: dict[str, Any]
    steps: list[StepState]
    status: RunStatus
    current: str | None
    started: str
    updated: str
    reason: str | None
    parent: str | None = None
    """Conversation id of the parent run, when this run is one item."""
    item: Any = None
    """This run's item value, when one."""
    items: list[ItemState] = field(default_factory=list)
    """Items in a parent run; only present on a parent run."""

    def step(self, step_id: str) -> StepState | None:
        """The step with this id, or ``None``. A small convenience the
        transition functions below all need."""
        for candidate in self.steps:
            if candidate.id == step_id:
                return candidate
        return None

    def to_json(self) -> str:
        doc: dict[str, Any] = {
            "format": STATE_FORMAT,
            "runbook": self.runbook,
            "version": self.version,
            "plugin": self.plugin,
            "persona": self.persona,
            "inputs": dict(self.inputs),
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status,
            "current": self.current,
            "started": self.started,
            "updated": self.updated,
            "reason": self.reason,
            "parent": self.parent,
            "item": self.item,
            "items": [i.to_dict() for i in self.items],
        }
        return json.dumps(doc, indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> RunState:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateUnreadable(f"the run state file is not valid JSON ({exc}).") from exc
        if not isinstance(doc, dict):
            raise StateUnreadable("the run state file is not a JSON object.")
        fmt = doc.get("format")
        if fmt != STATE_FORMAT:
            raise StateUnreadable(
                f"the run state file's format ({fmt!r}) is not one this core understands."
            )
        try:
            steps = [StepState.from_dict(item) for item in doc["steps"]]
            items = [ItemState.from_dict(item) for item in doc.get("items", [])]
            return cls(
                runbook=str(doc["runbook"]),
                version=str(doc["version"]),
                plugin=str(doc["plugin"]),
                persona=str(doc["persona"]),
                inputs=dict(doc["inputs"]),
                steps=steps,
                status=doc["status"],
                current=doc.get("current"),
                started=str(doc["started"]),
                updated=str(doc["updated"]),
                reason=doc.get("reason"),
                parent=doc.get("parent"),
                item=doc.get("item"),
                items=items,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateUnreadable(
                f"the run state file is missing or has a bad field ({exc})."
            ) from exc


def _now_iso() -> str:
    """An ISO-8601 UTC timestamp, the same shape :mod:`personacore.workspaces`
    already uses for file modification times."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# I/O — the only place this module touches disk
# ---------------------------------------------------------------------------


def read_state(workspace_dir: Path) -> RunState | None:
    """The run state for one workspace, or ``None`` when there is none.

    Raises :class:`StateUnreadable` for a file that exists but cannot be
    trusted — torn JSON, a missing field, or a ``format`` too new. Never
    resets or repairs anything; that decision belongs to whoever is running
    the boot scan (contract §5).
    """
    path = workspace_dir / STATE_FILENAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateUnreadable(f"the run state file could not be read ({exc}).") from exc
    return RunState.from_json(text)


def write_state(workspace_dir: Path, state: RunState) -> None:
    """Write ``state`` atomically: ``.run.json.tmp``, ``fsync``, then
    :func:`os.replace` onto ``.run.json`` (contract §5). A reader can never
    observe a half-written file — the rename either has not happened yet
    (the old file, if any, is still whole) or has already happened (the new
    file is whole)."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    final_path = workspace_dir / STATE_FILENAME
    tmp_path = workspace_dir / f"{STATE_FILENAME}{_TMP_SUFFIX}"
    text = state.to_json()
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            tmp_path.unlink()
        raise
    os.replace(tmp_path, final_path)


# ---------------------------------------------------------------------------
# Pure transitions
# ---------------------------------------------------------------------------


def new_state(
    runbook: Runbook,
    *,
    plugin: str,
    persona: str,
    inputs: Mapping[str, Any],
) -> RunState:
    """The state for a brand-new run: every step ``pending``, the run
    ``running``, ``current`` pointing at the first step. Nothing has
    actually started yet — the runner marks the first step ``running``
    itself, once it has begun."""
    now = _now_iso()
    steps = [
        StepState(
            id=step.id,
            kind=step.kind,
            status=StepStatus.PENDING,
            outputs={},
            started=None,
            finished=None,
            reason=None,
        )
        for step in runbook.steps
    ]
    return RunState(
        runbook=runbook.runbook,
        version=runbook.version,
        plugin=plugin,
        persona=persona,
        inputs=dict(inputs),
        steps=steps,
        status="running",
        current=steps[0].id if steps else None,
        started=now,
        updated=now,
        reason=None,
    )


def mark_interrupted(state: RunState) -> RunState:
    """``running`` -> ``interrupted``. ``current`` is kept exactly as it
    was — the boot scan found the run mid-step, and that step is where
    :func:`plan_resume` will restart it."""
    new = _clone(state)
    new.status = "interrupted"
    new.updated = _now_iso()
    return new


def plan_resume(state: RunState) -> tuple[str, list[str]]:
    """Where a resume restarts from: the current step's id, and the names
    of whatever it had already recorded as output before the run died
    (contract §5, "resume discards the interrupted attempt's partial
    output"). The caller renames each of those to ``<name>.interrupted.N``
    — this module does no file I/O beyond ``.run.json`` itself, so it
    cannot do the renaming.
    """
    step_id = state.current
    if step_id is None:
        raise ValueError("this run has no current step to resume.")
    step = state.step(step_id)
    if step is None:
        raise ValueError(f"this run's current step {step_id!r} is not one of its own steps.")
    return step_id, sorted(step.outputs.values())


def advance(state: RunState, step_id: str, outputs: Mapping[str, str]) -> RunState:
    """One step finished cleanly: mark it ``done`` with its actual output
    filenames, then move ``current`` on to the next ``pending`` step — or,
    if there is none, mark the whole run ``done``.

    The next step's own status is left ``pending``; the runner marks it
    ``running`` itself once it actually starts it, the same as the first
    step after :func:`new_state`.
    """
    new = _clone(state)
    now = _now_iso()
    step = new.step(step_id)
    if step is None:
        raise ValueError(f"{step_id!r} is not one of this run's steps.")
    step.status = StepStatus.DONE
    step.outputs = dict(outputs)
    step.finished = now
    if step.started is None:
        step.started = now

    next_step = _next_pending(new, step_id)
    if next_step is None:
        new.status = "done"
        new.current = None
    else:
        new.current = next_step.id
    new.updated = now
    return new


def fail(state: RunState, step_id: str, reason: str) -> RunState:
    """A step could not finish: mark it ``failed`` with ``reason``, and park
    the whole run on it (contract §3's watchdog, and any other step
    failure)."""
    new = _clone(state)
    now = _now_iso()
    step = new.step(step_id)
    if step is None:
        raise ValueError(f"{step_id!r} is not one of this run's steps.")
    step.status = StepStatus.FAILED
    step.reason = reason
    step.finished = now
    new.status = "parked"
    new.current = step_id
    new.reason = reason
    new.updated = now
    return new


def stop(state: RunState) -> RunState:
    """The person pressed Stop. The run stops where it is — every file
    stays, and nothing about any step's own status changes."""
    new = _clone(state)
    new.status = "stopped"
    new.updated = _now_iso()
    return new


def park(state: RunState, reason: str) -> RunState:
    """Park the run for a reason that is not a step failure — a gate step
    not built yet, a switch turned off mid-run, and the like."""
    new = _clone(state)
    new.status = "parked"
    new.reason = reason
    new.updated = _now_iso()
    return new


def _clone(state: RunState) -> RunState:
    """A deep-enough copy for a pure transition: new ``StepState`` objects
    (so mutating one does not reach back into the caller's copy) sharing
    immutable leaf values."""
    return RunState(
        runbook=state.runbook,
        version=state.version,
        plugin=state.plugin,
        persona=state.persona,
        inputs=dict(state.inputs),
        steps=[
            StepState(
                id=s.id,
                kind=s.kind,
                status=s.status,
                outputs=dict(s.outputs),
                started=s.started,
                finished=s.finished,
                reason=s.reason,
                gate=GateState(
                    mode=s.gate.mode,
                    questions=list(s.gate.questions),
                    answers=dict(s.gate.answers),
                    loops=s.gate.loops,
                    source_file=s.gate.source_file,
                    source_role=s.gate.source_role,
                )
                if s.gate
                else None,
            )
            for s in state.steps
        ],
        status=state.status,
        current=state.current,
        started=state.started,
        updated=state.updated,
        reason=state.reason,
        parent=state.parent,
        item=state.item,
        items=[
            ItemState(
                value=i.value,
                conversation_id=i.conversation_id,
                status=i.status,
                reason=i.reason,
            )
            for i in state.items
        ],
    )


def _next_pending(state: RunState, after_step_id: str) -> StepState | None:
    """The first ``pending`` step after ``after_step_id`` in declared order."""
    ids = [s.id for s in state.steps]
    try:
        index = ids.index(after_step_id)
    except ValueError:
        return None
    for candidate in state.steps[index + 1 :]:
        if candidate.status == StepStatus.PENDING:
            return candidate
    return None


__all__ = [
    "STATE_FILENAME",
    "STATE_FORMAT",
    "GateState",
    "ItemState",
    "RunStatus",
    "RunState",
    "StateUnreadable",
    "StepState",
    "StepStatus",
    "advance",
    "fail",
    "mark_interrupted",
    "new_state",
    "park",
    "plan_resume",
    "read_state",
    "stop",
    "write_state",
]
