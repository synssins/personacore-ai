"""The composer's own "+" picker: "Run a runbook…" — WAVE2.md's ``picker``
subtask, contract ``working/contracts/runbook.md`` §1.11-§1.13 and §3
(the flat runbook list).

**This module never imports anything from ``personacore.runbooks.*``**, for
the same reason :mod:`personacore.web.screens.runbooks` and
:mod:`personacore.web.screens.chat_run` do not (see either module's own
docstring): the ``store``/``gates``/``iteration`` subtasks are somebody
else's work landing in this same tree at the same time. Everything here
reads ``app.state.runbooks`` duck-typed, in the exact shape
:mod:`personacore.web.screens.runbooks` already reads it in
(``list()``, ``plugin_enabled()``), and never raises for a store that is
absent, older than a field this reads, or simply wrong about one record —
one bad row costs that row, never the composer.

It registers no routes of its own. The picker's own two reads
(:func:`runbook_picker_enabled`, :func:`runbook_picker_rows`) are called
from :mod:`personacore.web.screens.chat`'s own screen-building functions and
folded into the same context ``chat.html`` already renders, because the "+"
sheet is that page's own markup and there is nothing here that needs a
second route to answer.

Choosing a row is a plain link to the Run… form
(``/admin/runbooks/{plugin}/{id}/run``) that
:mod:`personacore.web.screens.runbooks` already serves — WAVE2.md is
explicit that this form is shared ("The Run… form on the Runbooks screen
stays and gains the same step preview and skip"), so the picker does not
duplicate it; it only decides which rows are offered and how they read
before anyone clicks one.
"""

from __future__ import annotations

from typing import Any

from personacore.config.appdata import AppdataLayout
from personacore.web.shared import current_config


def runbook_picker_enabled(layout: AppdataLayout) -> bool:
    """``[runbooks] enabled`` off ``core.toml`` — read the document the same
    way :mod:`personacore.web.screens.runbooks`'s own
    ``_core_runbooks_enabled`` does, so the composer's "+" entry and the
    Runbooks screen's own banner can never disagree about whether the core
    switch is on. Unreadable configuration reads as off, the safe default
    for a switch that gates starting something.
    """
    current, _unreadable = current_config(layout)
    if current is None:
        return False
    section = current.settings.get("runbooks")
    if isinstance(section, dict):
        return bool(section.get("enabled", False))
    return False


def runbook_picker_rows(store: Any) -> list[dict[str, Any]]:
    """The composer's own flat runbook list (WAVE2.md's picker: "runbooks
    whose plugin is enabled for runbooks, flat list 'Title vX.Y.Z plugin',
    greyed with the verdict").

    **Unlike the Runbooks screen's own table**, a runbook whose plugin has
    switched runbooks off is left out of this list entirely rather than
    shown greyed — the acceptance line is explicit: "the list shows no
    plugin whose runbooks are off". Greying here is only for a runbook that
    is itself invalid or not compatible, same wording
    :func:`personacore.web.screens.runbooks.runbook_row` already uses for
    the same two states, so the same runbook reads the same way in both
    places.

    ``store`` is ``None`` (no runbooks package wired into this core) or any
    exception any one record raises both cost that row and nothing else —
    the same tolerance every other duck-typed read of this store already
    gives it (see the module docstring).
    """
    if store is None:
        return []
    rows: list[dict[str, Any]] = []
    try:
        records = list(store.list())
    except Exception:  # noqa: BLE001 - an unreadable store shows no rows, not a 500
        return []
    for record in records:
        try:
            plugin_enabled = bool(store.plugin_enabled(record.plugin))
        except Exception:  # noqa: BLE001 - one bad plugin flag costs this row only
            plugin_enabled = False
        if not plugin_enabled:
            continue
        valid = bool(getattr(record, "valid", False))
        verdict = getattr(record, "verdict", None) if valid else None
        ok = bool(getattr(verdict, "ok", False)) if verdict is not None else False
        reasons = list(getattr(verdict, "reasons", None) or []) if verdict is not None else []
        if not valid:
            verdict_text = ""
        elif ok:
            verdict_text = ""
        else:
            verdict_text = " ".join(reasons) if reasons else "Not compatible."
        title = getattr(record, "title", None) or record.id
        rows.append(
            {
                "plugin": record.plugin,
                "id": record.id,
                "label": f"{title} v{record.version} {record.plugin}",
                "run_url": f"/admin/runbooks/{record.plugin}/{record.id}/run",
                "greyed": valid and not ok,
                "invalid": not valid,
                "verdict_text": verdict_text,
            }
        )
    rows.sort(key=lambda row: row["label"])
    return rows


__all__ = [
    "runbook_picker_enabled",
    "runbook_picker_rows",
]
