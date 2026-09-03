"""``[hearing]`` and ``[hearing.engines.<id>]`` in ``core.toml``.

The mirror of :mod:`personacore.config.voice`, and its reasoning applies here
word for word. One table per recogniser, holding one switch::

    [hearing.engines.whisper-onnx]
    enabled = true

    [hearing.engines.none]
    enabled = false

Independent by construction: the recognisers are separate tables, so turning
one off is a change to that table and touches nothing of another's. Saving
takes effect immediately; nothing here waits for a restart.

**This module deliberately breaks the house rule that config forbids unknown
keys**, for the reason voice's does. Everywhere else, a typo refused at the
admin boundary is a typo caught while the operator is still looking at the
screen. Here, a refusal is raised by ``load_core_settings``, which runs inside
``create_app`` — so a bad value in ``[hearing]`` would stop the core starting,
which is the lockout this project has already produced three times. Hearing is
as detachable as speech; it must not be able to take the core down with it.

So a malformed hearing setting is **dropped, named, and defaulted to off**. The
name goes into :attr:`HearingSettings.problems`, which the registry reports and
the admin UI shows — the operator still learns about their typo, they just
learn about it from a running core rather than from a container that will not
start. Off is the safe default in both directions: a recogniser that was never
understood was never started, and one that was never started loaded nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# `personacore.hearing.engine` is NOT imported here. `personacore.config` is
# imported by almost everything, the hearing registry imports this module for
# `HearingSettings`, and a module-scope import in this direction closes that
# loop — the same reason `config/voice.py` defers its one symbol and
# `settings.py` defers `personacore.auth.method`. It is fetched inside the
# validator instead.


class HearingEngineSettings(BaseModel):
    """One recogniser's switch.

    ``extra="ignore"`` rather than ``forbid`` for the reason in the module
    docstring; an ignored key is reported through
    :attr:`HearingSettings.problems` rather than silently swallowed.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    """Off until an operator says otherwise.

    Defaulting to on would mean an upgrade that adds a recogniser starts it on
    every install that pulls the image — the "present and idle" cost the
    switches exist to refuse.
    """


class HearingSettings(BaseModel):
    """The whole of ``[hearing]``."""

    model_config = ConfigDict(extra="ignore")

    engines: dict[str, HearingEngineSettings] = Field(default_factory=dict)
    """Keyed by engine id. An id this build does not have is kept, not dropped:
    an operator who rolls back to an earlier image should not silently lose the
    switch they set, and re-pinning the previous tag is the documented rollback
    path (CLAUDE.md, CI/CD)."""

    problems: tuple[str, ...] = Field(default=(), exclude=True, repr=False)
    """What was dropped on the way in, in sentences.

    ``exclude=True`` because ``config_io.write_config`` dumps this model
    straight back into ``core.toml``: a diagnostic written into the file it
    diagnoses would be read back as configuration on the next start.
    """

    @model_validator(mode="before")
    @classmethod
    def _drop_what_cannot_be_read(cls, value: Any) -> Any:
        """Sanitise rather than refuse. See the module docstring.

        Runs before field validation, so nothing malformed ever reaches
        pydantic's own error path — which is what would otherwise raise
        :class:`~personacore.config.settings.ConfigError` and stop the core.
        """
        from personacore.hearing.engine import ENGINE_ID_PATTERN

        if isinstance(value, HearingSettings):
            # Already validated once — `CoreSettings(hearing=HearingSettings(...))`
            # and `model_copy` both arrive here. Re-sanitising would discard
            # `problems`, which is how a diagnostic gets lost between the load
            # that found it and the screen that shows it.
            return value

        if not isinstance(value, dict):
            # A `[hearing]` that is not a table at all (`hearing = "on"`).
            # Nothing to salvage; every recogniser defaults to off and the
            # operator is told why.
            if value is None:
                return {}
            return {
                "engines": {},
                "problems": (
                    "[hearing] is not a table, so nothing is listening. Remove "
                    "the line and set switches under [hearing.engines].",
                ),
            }

        problems: list[str] = []
        raw = dict(value)
        # Never readable from the file: it is this validator's output, and a
        # document round-tripped through the admin API must not be able to
        # forge it.
        raw.pop("problems", None)

        for key in sorted(set(raw) - {"engines"}):
            problems.append(
                f"[hearing] has no setting called '{key}', so it was ignored. "
                "The only thing under [hearing] is [hearing.engines.<id>]."
            )
            raw.pop(key)

        engines_in = raw.get("engines", {})
        if not isinstance(engines_in, dict):
            problems.append(
                "[hearing.engines] is not a table, so nothing is listening. Each "
                "recogniser gets its own table, e.g. [hearing.engines.whisper-onnx]."
            )
            engines_in = {}

        engines_out: dict[str, Any] = {}
        for name, entry in engines_in.items():
            engine_id = str(name)
            if not ENGINE_ID_PATTERN.match(engine_id):
                problems.append(
                    f"'{engine_id}' is not a usable engine name, so its switch was "
                    "ignored. An engine name is lowercase letters, digits and "
                    "dashes, e.g. 'whisper-onnx'."
                )
                continue
            if isinstance(entry, HearingEngineSettings):
                engines_out[engine_id] = entry
                continue
            if not isinstance(entry, dict):
                problems.append(
                    f"[hearing.engines.{engine_id}] is not a table, so {engine_id} "
                    f"is off. Write it as [hearing.engines.{engine_id}] with "
                    "enabled = true."
                )
                continue
            enabled = entry.get("enabled", False)
            if not isinstance(enabled, bool):
                problems.append(
                    f"[hearing.engines.{engine_id}] enabled is {enabled!r}, which "
                    f"is not true or false, so {engine_id} is off."
                )
                enabled = False
            for extra in sorted(set(entry) - {"enabled"}):
                problems.append(
                    f"[hearing.engines.{engine_id}] has no setting called "
                    f"'{extra}', so it was ignored. An engine table holds enabled "
                    "and nothing else."
                )
            engines_out[engine_id] = {"enabled": enabled}

        return {"engines": engines_out, "problems": tuple(problems)}

    def is_enabled(self, engine_id: str) -> bool:
        """Whether the operator has switched this recogniser on.

        The single reading of "on", so the registry, the UI and a log line
        cannot disagree about a recogniser with no table of its own — which is
        off, because one nobody enabled costs disk and nothing else.
        """
        entry = self.engines.get(engine_id)
        return bool(entry and entry.enabled)

    def enabled_ids(self) -> tuple[str, ...]:
        """Every recogniser id switched on, in a stable order."""
        return tuple(sorted(name for name in self.engines if self.is_enabled(name)))

    def with_engine(self, engine_id: str, enabled: bool) -> HearingSettings:
        """A copy with one switch moved, leaving every other recogniser alone.

        Independence written as code rather than trusted to each caller: a save
        that rebuilt the whole ``engines`` table from a form would be one
        missing checkbox away from switching off something nobody touched.
        """
        engines = dict(self.engines)
        engines[engine_id] = HearingEngineSettings(enabled=enabled)
        return HearingSettings(engines=engines)
