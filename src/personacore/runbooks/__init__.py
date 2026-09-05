"""Runbooks — ``working/contracts/runbook.md``.

A runbook is a self-describing YAML file: an ordered list of steps the core
can run inside one conversation, with progress reported in the chat as it
goes. This package (alpha.17-18) is the format, the validator, compatibility
against installed plugins, the file store, and now (alpha.19) a run's own
state and its role resolution — the runner that actually executes a step
against the LLM and a plugin tool is :mod:`personacore.runbooks.runner`,
built separately.

* :mod:`personacore.runbooks.schema` — the file, as pydantic models.
* :mod:`personacore.runbooks.validate` — parsing plus every structural check
  contract §6 asks for.
* :mod:`personacore.runbooks.compat` — whether an already-valid runbook can
  run against *this* household's plugins right now.
* :mod:`personacore.runbooks.store` — where runbooks live in appdata.
* :mod:`personacore.runbooks.state` — a run's own state, ``.run.json``, and
  the pure transitions it goes through (contract §3, §5).
* :mod:`personacore.runbooks.roles` — ``{{ }}`` templating and role
  resolution (contract §2, "roles, not names").
* :mod:`personacore.runbooks.gates` — what a gate step *is*: the questions, the
  answer lines, and the typed conditions an auto gate is decided by
  (contract §4).
"""

from __future__ import annotations

from personacore.runbooks.compat import PluginFacts, Verdict, check
from personacore.runbooks.roles import RoleError, resolve_new_file, resolve_pins, substitute
from personacore.runbooks.schema import Runbook, ValidationError
from personacore.runbooks.state import (
    STATE_FILENAME,
    STATE_FORMAT,
    GateState,
    ItemState,
    RunState,
    RunStatus,
    StateUnreadable,
    StepState,
    StepStatus,
    advance,
    fail,
    mark_interrupted,
    new_state,
    park,
    plan_resume,
    read_state,
    stop,
    write_state,
)
from personacore.runbooks.store import RunbookRecord, RunbookStore, RunbookStoreError
from personacore.runbooks.validate import validate_runbook

__all__ = [
    "STATE_FILENAME",
    "STATE_FORMAT",
    "GateState",
    "ItemState",
    "PluginFacts",
    "RoleError",
    "Runbook",
    "RunbookRecord",
    "RunbookStore",
    "RunbookStoreError",
    "RunState",
    "RunStatus",
    "StateUnreadable",
    "StepState",
    "StepStatus",
    "ValidationError",
    "Verdict",
    "advance",
    "check",
    "fail",
    "mark_interrupted",
    "new_state",
    "park",
    "plan_resume",
    "read_state",
    "resolve_new_file",
    "resolve_pins",
    "stop",
    "substitute",
    "validate_runbook",
    "write_state",
]
