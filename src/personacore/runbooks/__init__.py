"""Runbooks — ``working/contracts/runbook.md``.

A runbook is a self-describing YAML file: an ordered list of steps the core
can run inside one conversation, with progress reported in the chat as it
goes. This package (alpha.17) is the format, the validator, compatibility
against installed plugins, and the file store — **no runner yet**: a runbook
can be uploaded, listed, validated and deleted, and the two switches
(``settings.runbooks.enabled`` and the per-plugin one) exist, but nothing
executes.

* :mod:`personacore.runbooks.schema` — the file, as pydantic models.
* :mod:`personacore.runbooks.validate` — parsing plus every structural check
  contract §6 asks for.
* :mod:`personacore.runbooks.compat` — whether an already-valid runbook can
  run against *this* household's plugins right now.
* :mod:`personacore.runbooks.store` — where runbooks live in appdata.
"""

from __future__ import annotations

from personacore.runbooks.compat import PluginFacts, Verdict, check
from personacore.runbooks.schema import Runbook, ValidationError
from personacore.runbooks.store import RunbookRecord, RunbookStore, RunbookStoreError
from personacore.runbooks.validate import validate_runbook

__all__ = [
    "PluginFacts",
    "Runbook",
    "RunbookRecord",
    "RunbookStore",
    "RunbookStoreError",
    "ValidationError",
    "Verdict",
    "check",
    "validate_runbook",
]
