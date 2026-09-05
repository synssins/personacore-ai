"""Runbooks' master switch — ``[runbooks]`` in ``core.toml``.

``working/contracts/runbook.md`` §1.9: a runbook is "Ansible for the
assistant" — an uploaded or bundled YAML file the core can step through in a
conversation. This alpha (17) ships the format, the validator and the file
store; nothing executes yet (no runner). The switch exists anyway, ahead of
the runner, because §1.9 is explicit that it is a *condition* of the whole
feature existing at all: "no plugin declaration, bundled runbook or upload can
override it."

**Default off**, same reasoning as :class:`personacore.config.wyoming.
WyomingSettings` and :class:`personacore.config.image.ImageSettings`: this
switches on a whole feature surface, not a knob within one already on, so an
unconfigured core does not silently grow a "run a runbook" entry nobody asked
for.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RunbooksSettings(BaseModel):
    """``[runbooks]`` — the one household-wide switch (contract §1.9).

    Per-plugin switches are a different thing and live beside the plugin's
    own runbooks in appdata (``RunbookStore.plugin_enabled`` /
    ``set_plugin_enabled``), not here — this model is only ever the core
    switch, the one nothing else may override.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    """Whether a runbook may ever be started. Off: no *Run a runbook* entry,
    no run can start, a parked run stays parked and is not offered at boot.
    The Runbooks screen still lists files and their validation regardless —
    uploading and validating a runbook is not "running" one."""


__all__ = ["RunbooksSettings"]
