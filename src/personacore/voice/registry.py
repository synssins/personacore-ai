"""Which engines exist, which are on, and starting and stopping them.

ADR-0029 §2: every supported engine is built into the image and each has its
own, independent switch. This module is that sentence made real, and it holds
exactly three promises.

**Off means off.** Registering an engine does nothing to it. An engine that was
never switched on has never had :meth:`~personacore.voice.engine.Engine.start`
called, so it has loaded nothing; after :meth:`stop` it holds no model. That is
the entire justification for shipping four engines in one image, so
``tests/voice/test_registry.py`` measures it with a weak reference rather than
asserting it.

**Independence.** Every start and every stop is attempted on its own and its
failure is recorded against that engine alone. Turning Piper off does nothing
to Kokoro; Kokoro failing to start does nothing to Piper.

**Nothing here can stop the core.** No method on this class raises for anything
an engine does. An engine that throws on ``start`` is marked failed with the
reason attached and the core carries on — voice is the most detachable thing in
the system and must never be able to take the listener down with it.

The registry is synchronous on purpose. Starting an engine loads a model, which
is blocking CPU work; the server calls :meth:`apply` through
``asyncio.to_thread`` so the event loop is never held. Making it async would
have put ``await`` in front of blocking work and hidden that.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from personacore.config.voice import VoiceSettings
from personacore.voice.engine import Engine, looks_like_engine

log = structlog.get_logger(__name__)


def _name_of(engine: Engine) -> str:
    """What to call an engine in a sentence an operator reads.

    Falls back to the id, and never to an empty string: a warning that opens
    with a blank is a warning nobody can act on, and `display` is the one
    member of the protocol an engine can leave unset without breaking anything
    else.
    """
    return str(getattr(engine, "display", "") or getattr(engine, "id", "") or "An engine")


UNKNOWN_ENGINE_SWITCH = (
    "This core has no speech engine called '{engine}', so the switch in "
    "[voice.engines.{engine}] does nothing. Check the spelling, or remove the "
    "table if the engine is gone from this image. Every other engine is "
    "unaffected."
)
"""PC-341: a switch naming nothing is named, and it is not fatal.

Both halves matter. Named, because the plugin manifest refuses an unknown field
for exactly this reason — a typo is an error you can see instead of a permission
you silently never got — and a switch deserves the same. Not fatal, because
``[voice]`` is read inside ``create_app``: a config typo that stopped the core
booting would be the lockout class this project has already produced three
times. So the switch is dropped, defaulted off, and *said out loud* on
``/health`` and on the engines screen.
"""


def _as_settings(settings: VoiceSettings | None) -> VoiceSettings:
    """Whatever was handed in, as switches. Never raises.

    ``apply`` promises not to raise for anything, and "anything" has to include
    being called wrongly: the alternative is an ``AttributeError`` escaping into
    a settings save. Everything that is not already :class:`VoiceSettings` goes
    through the same forgiving validator ``core.toml`` does, and something
    unreadable means every engine is off — which is the safe direction.
    """
    if isinstance(settings, VoiceSettings):
        return settings
    if settings is None:
        return VoiceSettings()
    try:
        return VoiceSettings.model_validate(settings)
    except Exception as exc:  # noqa: BLE001 - a save must never fail on speech
        log.warning("voice_settings_unreadable", error=repr(exc))
        return VoiceSettings()


class EngineState(StrEnum):
    """What one engine is doing, as one word."""

    UNAVAILABLE = "unavailable"
    """This build or this hardware cannot run it. No switch is offered
    (PC-338)."""

    OFF = "off"
    """Available, and the operator has not switched it on. Loaded nothing."""

    RUNNING = "running"
    """On and holding its runtime. Its voices are speakable."""

    FAILED = "failed"
    """Switched on and could not start. Degraded, with a reason, and alone."""


@dataclass(frozen=True)
class EngineStatus:
    """One row of the engines screen, and the answer to "can this speak".

    ``available`` and ``enabled`` are both here and are not the same thing:
    available is fixed for the life of the process, enabled is the operator's
    switch. ``can_switch`` exists so no surface has to re-derive PC-338's rule
    that an unavailable engine offers no control at all.
    """

    id: str
    display: str
    available: bool
    unavailable_reason: str | None
    enabled: bool
    running: bool
    state: EngineState
    error: str | None

    @property
    def can_switch(self) -> bool:
        """PC-338: a control that appears to work and does not is the failure
        this project keeps reproducing. An unavailable engine gets no switch."""
        return self.available

    @property
    def speakable(self) -> bool:
        """Whether a voice belonging to this engine can be spoken right now."""
        return self.state is EngineState.RUNNING


@dataclass(frozen=True)
class ApplyReport:
    """What one save actually changed, for the log and for the UI.

    Returned rather than logged and forgotten because ADR-0010 promises a
    saved setting takes effect now: an operator who flipped a switch is owed
    the answer to "did it", and an engine that failed to start is owed a
    sentence rather than a silent no-op.
    """

    started: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    failed: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.started or self.stopped or self.failed)


@dataclass
class _Slot:
    """The registry's own bookkeeping for one engine. Never handed out."""

    engine: Engine
    running: bool = False
    error: str | None = None
    start_attempts: int = 0


class VoiceRegistry:
    """The engines this build has, and their switches.

    Construction starts nothing. :meth:`apply` is the only thing that ever
    calls ``start``, and it is called after the core is already serving.
    """

    def __init__(self, engines: Iterable[Engine] = ()) -> None:
        # Reentrant because `shutdown` calls `stop_engine`, and a plain lock
        # would deadlock on the way out rather than on the way in — the worst
        # place to find a locking bug.
        self._lock = threading.RLock()
        # Separate, and held for the whole of `apply`: starting an engine loads
        # a model, and holding the bookkeeping lock across that would block the
        # engines screen for as long as a model takes to load. Two saves
        # racing each other is the thing that must not happen; a save racing a
        # status read is fine.
        self._apply_lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._settings = VoiceSettings()
        self._problems: list[str] = []
        # Recomputed by every `apply` rather than accumulated like
        # `_problems`: a typo an operator has since corrected must stop being
        # reported the moment they save, or the screen keeps telling them off
        # for a line that is no longer in the file.
        self._unknown_switches: tuple[str, ...] = ()
        for engine in engines:
            self.register(engine)

    # -- registration -----------------------------------------------------

    def register(self, engine: Engine) -> str | None:
        """Add an engine. Returns ``None``, or why it was refused.

        Refuses rather than raises. Registration happens while ``create_app``
        is assembling, so an engine that is malformed — a bad id, a missing
        method, a duplicate — must cost that engine and nothing else. The
        reason is kept in :meth:`problems` so it reaches the operator instead
        of only a log nobody reads.
        """
        problem = looks_like_engine(engine)
        if problem is not None:
            reason = f"A speech engine was not registered because {problem}."
            self._note(reason)
            log.warning("voice_engine_rejected", reason=problem)
            return reason

        with self._lock:
            if engine.id in self._slots:
                reason = (
                    f"Two speech engines both call themselves '{engine.id}'. The "
                    "second was ignored; an engine id has to be unique because it "
                    "is also the folder its voices live in."
                )
                self._note(reason)
                log.warning("voice_engine_duplicate", engine=engine.id)
                return reason
            self._slots[engine.id] = _Slot(engine=engine)

        log.info(
            "voice_engine_registered",
            engine=engine.id,
            available=bool(getattr(engine, "available", True)),
        )
        return None

    def add_problem(self, problem: str) -> None:
        """Record something that cost this build an engine, in a sentence.

        Public because the thing that could not be loaded is sometimes the
        engine *module*, which fails before there is any engine object to
        refuse — ``create_app`` hands that reason here so it reaches the same
        screen as every other voice problem instead of only the log.
        """
        self._note(problem)

    def _note(self, problem: str) -> None:
        with self._lock:
            if problem not in self._problems:
                self._problems.append(problem)

    def problems(self) -> tuple[str, ...]:
        """Everything the registry had to drop, in sentences an operator can
        act on: refused engines, malformed ``[voice]`` settings, and switches
        naming an engine this core does not have."""
        with self._lock:
            return tuple(self._settings.problems) + self._unknown_switches + tuple(self._problems)

    # -- what exists ------------------------------------------------------

    def ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._slots))

    def get(self, engine_id: str) -> Engine | None:
        """The engine object, whatever state it is in, or ``None``.

        Handed out for listing voices, which a disabled engine still answers.
        The speak path asks :meth:`status` first — holding the object is not
        permission to use it.
        """
        with self._lock:
            slot = self._slots.get(engine_id)
            return slot.engine if slot else None

    def status(self) -> tuple[EngineStatus, ...]:
        """Every engine, in id order. The engines screen reads this."""
        with self._lock:
            return tuple(self._status(slot) for _, slot in sorted(self._slots.items()))

    def status_for(self, engine_id: str) -> EngineStatus | None:
        with self._lock:
            slot = self._slots.get(engine_id)
            return self._status(slot) if slot else None

    def is_running(self, engine_id: str) -> bool:
        with self._lock:
            slot = self._slots.get(engine_id)
            return bool(slot and slot.running)

    def _status(self, slot: _Slot) -> EngineStatus:
        engine = slot.engine
        available = bool(getattr(engine, "available", True))
        reason = getattr(engine, "unavailable_reason", None)
        enabled = self._settings.is_enabled(engine.id)
        if not available:
            state = EngineState.UNAVAILABLE
        elif slot.running:
            state = EngineState.RUNNING
        elif enabled and slot.error is not None:
            state = EngineState.FAILED
        else:
            state = EngineState.OFF
        return EngineStatus(
            id=engine.id,
            display=str(getattr(engine, "display", "") or engine.id),
            available=available,
            unavailable_reason=str(reason) if reason else None,
            enabled=enabled,
            running=slot.running,
            state=state,
            error=slot.error,
        )

    # -- the switches -----------------------------------------------------

    @property
    def settings(self) -> VoiceSettings:
        """The switches currently in force — what is running, not what is on
        disk. Read off the registry rather than re-read from ``core.toml`` for
        the same reason the bus reports its own address: a process holding
        yesterday's config and a wrong config look identical otherwise."""
        with self._lock:
            return self._settings

    def apply(self, settings: VoiceSettings | None) -> ApplyReport:
        """Make the switches say what ``settings`` says, now (PC-335).

        Called at startup and again on every save. Reconciles rather than
        replays: an engine whose switch did not move is not touched, so saving
        an unrelated setting never restarts a working engine.

        Never raises. Every engine is handled inside its own guard, so one
        engine that explodes on ``start`` is one engine that is marked failed.
        A switch naming an engine this core has not got is named as a problem
        and as a note on the report (PC-341), and refused as nothing else: this
        runs inside ``create_app``, where raising on a typo is a lockout.
        """
        settings = _as_settings(settings)
        started: list[str] = []
        stopped: list[str] = []
        unchanged: list[str] = []
        failed: dict[str, str] = {}
        notes: list[str] = []

        with self._apply_lock:
            with self._lock:
                self._settings = settings
                slots = dict(self._slots)
                unknown = self._unknown_ids(settings, slots)
                self._unknown_switches = unknown
            notes.extend(unknown)

            for engine_id, slot in sorted(slots.items()):
                engine = slot.engine
                wanted = settings.is_enabled(engine_id)
                available = bool(getattr(engine, "available", True))

                if not available:
                    # PC-338. Never started, whatever the file says — a switch
                    # that starts something which cannot work is worse than no
                    # switch at all.
                    if slot.running:
                        self._stop_one(slot, stopped, failed)
                    if wanted:
                        reason = (
                            getattr(engine, "unavailable_reason", None)
                            or "it cannot run on this build."
                        )
                        notes.append(
                            f"{_name_of(engine)} is switched on in the settings but "
                            f"cannot run here: {reason} It stays off."
                        )
                    continue

                if wanted and not slot.running:
                    self._start_one(slot, started, failed)
                elif not wanted and slot.running:
                    self._stop_one(slot, stopped, failed)
                elif not wanted:
                    # Off and staying off. Clear a stale failure so an engine
                    # the operator switched off does not keep reporting a fault
                    # from the last time they switched it on.
                    slot.error = None
                    unchanged.append(engine_id)
                else:
                    unchanged.append(engine_id)

        report = ApplyReport(
            started=tuple(started),
            stopped=tuple(stopped),
            unchanged=tuple(unchanged),
            failed=failed,
            notes=tuple(notes),
        )
        if report.changed:
            log.info(
                "voice_engines_applied",
                started=report.started,
                stopped=report.stopped,
                failed=sorted(report.failed),
            )
        return report

    @staticmethod
    def _unknown_ids(settings: VoiceSettings, slots: dict[str, _Slot]) -> tuple[str, ...]:
        """A sentence for every switch naming an engine this core has not got.

        Reads the **settings'** ids, which is the half ``apply`` used to skip:
        iterating the registered engines can only ever notice an engine that is
        present, so a name matching nothing was never looked at by anything.

        Silent when there are no engines at all. Then every switch names
        nothing, the empty engine list is the answer, and
        ``voice_common.NO_ENGINES`` already says it on the screen in better
        words — a line per switch on top of that is noise, and would claim the
        image never carried an engine when the truth is that loading them
        failed and has already been reported.
        """
        if not slots:
            return ()
        missing = [engine_id for engine_id in sorted(settings.engines) if engine_id not in slots]
        for engine_id in missing:
            log.warning("voice_switch_unknown_engine", engine=engine_id)
        return tuple(UNKNOWN_ENGINE_SWITCH.format(engine=engine_id) for engine_id in missing)

    def _start_one(self, slot: _Slot, started: list[str], failed: dict[str, str]) -> None:
        engine_id = slot.engine.id
        slot.start_attempts += 1
        try:
            slot.engine.start()
        except Exception as exc:  # noqa: BLE001 - one engine's failure, and only its own
            slot.running = False
            slot.error = (
                f"{_name_of(slot.engine)} did not start: {exc}. Voices "
                "from this engine cannot be spoken until it does; every other "
                "engine is unaffected."
            )
            failed[engine_id] = slot.error
            log.warning("voice_engine_start_failed", engine=engine_id, error=repr(exc))
            return
        slot.running = True
        slot.error = None
        started.append(engine_id)
        log.info("voice_engine_started", engine=engine_id)

    def _stop_one(self, slot: _Slot, stopped: list[str], failed: dict[str, str]) -> None:
        engine_id = slot.engine.id
        try:
            slot.engine.stop()
        except Exception as exc:  # noqa: BLE001 - off is off even when stopping complained
            # Still marked stopped. An engine that raised on the way out is not
            # a running engine, and leaving it marked running would make the
            # switch lie in the direction that keeps a model resident.
            slot.error = (
                f"{_name_of(slot.engine)} complained while stopping: "
                f"{exc}. It is switched off regardless."
            )
            failed[engine_id] = slot.error
            log.warning("voice_engine_stop_failed", engine=engine_id, error=repr(exc))
        else:
            slot.error = None
            log.info("voice_engine_stopped", engine=engine_id)
        slot.running = False
        stopped.append(engine_id)

    def shutdown(self) -> None:
        """Stop everything that is running. Safe to call twice, never raises."""
        with self._lock:
            slots = [slot for slot in self._slots.values() if slot.running]
        stopped: list[str] = []
        failed: dict[str, str] = {}
        for slot in slots:
            self._stop_one(slot, stopped, failed)


def builtin_engines() -> tuple[tuple[Engine, ...], tuple[str, ...]]:
    """The engines compiled into this image, and anything that stopped one
    being loaded.

    The engines live in ``personacore.voice.engines``, which this module
    imports **lazily and forgivingly**: a broken or absent engine module costs
    that engine and nothing else. An import error here would otherwise run
    inside ``create_app`` and produce the lockout class this project has
    already shipped three times.

    Three shapes are accepted, so an engine package can grow without this
    function becoming its editor: a ``build_engines_and_problems()`` callable
    returning both, a ``build_engines()`` callable returning engines, or an
    ``ENGINES`` sequence. The first is preferred because a package that loses
    one engine has something to say about it — an engine that failed to
    construct is simply *absent*, and an absent engine and an engine this image
    never carried look identical on the screen. Constructing an engine must not
    load anything (see :class:`~personacore.voice.engine.Engine`), so calling
    the builder at assembly time is free.
    """
    try:
        module = __import__("personacore.voice.engines", fromlist=["*"])
    except Exception as exc:  # noqa: BLE001 - no engine may stop the core starting
        log.warning("voice_engines_unavailable", error=repr(exc))
        if isinstance(exc, ModuleNotFoundError):
            # Nothing is installed yet. Not a fault: the core runs without
            # speech, which is the property ADR-0029 §6 is built on.
            return (), ()
        return (), (
            f"The built-in speech engines could not be loaded: {exc}. The "
            "assistant runs without speech until that is fixed.",
        )

    both = getattr(module, "build_engines_and_problems", None)
    factory = getattr(module, "build_engines", None)
    try:
        if callable(both):
            found, problems = both()
        elif callable(factory):
            found, problems = factory(), ()
        else:
            found, problems = getattr(module, "ENGINES", ()), ()
        engines = tuple(found)
    except Exception as exc:  # noqa: BLE001 - same bargain
        log.warning("voice_engines_build_failed", error=repr(exc))
        return (), (
            f"The built-in speech engines could not be listed: {exc}. The "
            "assistant runs without speech until that is fixed.",
        )
    return engines, tuple(problems)
