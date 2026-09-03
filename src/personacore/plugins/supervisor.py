"""One plugin, kept alive — spec sections 5.1, 9 and 10.

Spec section 5.1: "subprocesses that crash are killed, logged, restarted with
backoff, and surfaced in the admin UI — **a bad plugin never takes the core
down**." That sentence is this module's entire remit, and it is written
defensively because "bad" covers more than "crashes":

===========================  =====================================================
What a bad plugin does       What happens here
===========================  =====================================================
Fails to start               Attempt recorded, backoff, retry, terminal after N.
Crashes mid-conversation     The call becomes a failure *value*; plugin recycled.
Hangs forever                Every call is under ``asyncio.timeout``; then recycled.
Never answers the handshake  Startup is under a timeout too; treated as a crash.
Floods stderr                Bounded, truncating capture file (``mcp_client``).
Speaks malformed MCP         Parse failures surface as transport errors → recycle.
Refuses to die               Escalated: stop, cancel, then abandoned and marked
                             failed. The core keeps running with it written off.
Declares tools it lacks      Terminal load failure naming both sides — no restart.
Needs a secret nobody gave   Not started: reported as waiting for a credential,
                             by name (ADR-0025). No restart; a paste fixes it.
===========================  =====================================================

The shape that makes this tractable: **one task owns the connection**. The
transport context managers from ``mcp_client`` are entered and exited in the
same task (anyio requires it), that task holds them open, and callers use the
published session object concurrently. Nothing outside that task ever touches
the subprocess, so teardown has exactly one owner and cannot race.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from personacore.audit import get_logger
from personacore.plugins.discovery import PluginRecord
from personacore.plugins.health import PluginHealth, PluginState
from personacore.plugins.mcp_client import (
    ChildEnvironmentError,
    MissingPluginSecrets,
    PluginContractMismatch,
    PluginSession,
    PluginToolError,
    PluginTransportError,
    RemoteTool,
    RemoteToolResult,
    SessionFactory,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class SupervisorConfig:
    """Timeouts and restart policy. Every value is a defence, not a tuning knob.

    The defaults are chosen for a household assistant: a plugin gets a
    reasonable moment to answer, and a plugin that keeps dying is written off
    within about a minute rather than restarting forever. An endless restart
    loop is worse than a dead plugin — it hides the fault and burns the CPU
    the rest of the house is sharing (the stack is CPU-only).
    """

    startup_timeout: float = 20.0
    """Spawn, handshake and tool listing, together."""

    call_timeout: float = 30.0
    shutdown_timeout: float = 10.0
    """Per escalation step: graceful, then cancelled, then abandoned."""

    backoff_initial: float = 1.0
    backoff_factor: float = 2.0
    backoff_max: float = 60.0
    max_restarts: int = 5
    """After this many failed attempts in a row the plugin is terminal:
    ``FAILED``, with the reason kept, and nothing further is attempted until a
    human reloads or fixes it."""

    heartbeat_interval: float = 30.0
    """How often an idle plugin is pinged, so a plugin that died quietly shows
    as unhealthy in the admin UI before somebody asks it for something. 0
    disables it."""


class PluginSupervisor:
    """Owns the lifecycle of exactly one plugin."""

    def __init__(
        self,
        record: PluginRecord,
        factory: SessionFactory,
        *,
        config: SupervisorConfig | None = None,
    ) -> None:
        self._record = record
        self._factory = factory
        self._config = config or SupervisorConfig()

        self._state = PluginState.STARTING
        self._session: PluginSession | None = None
        self._tools: dict[str, RemoteTool] = {}
        self._restart_count = 0
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._started_at: datetime | None = None
        self._next_retry_at: datetime | None = None
        self._terminal = False
        self._abandoned = False
        self._waiting_for_secrets: tuple[str, ...] = ()
        """Declared secrets nobody has supplied — ADR-0025 section 4.

        Held separately from ``_last_error`` because it is not an error: it is
        the one failure to start that an operator fixes by pasting a value into
        a field, and the page has to be able to tell that apart from a crash
        without parsing a sentence. Names only, never a value."""

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._recycle = asyncio.Event()
        self._settled = asyncio.Event()
        """Set once an attempt has resolved either way. What ``start()`` waits
        for — startup must not block on a plugin that is busy failing."""

    # -- read-only surface -------------------------------------------------

    @property
    def record(self) -> PluginRecord:
        return self._record

    @property
    def name(self) -> str:
        return self._record.name

    @property
    def tools(self) -> Mapping[str, RemoteTool]:
        return dict(self._tools)

    @property
    def state(self) -> PluginState:
        return self._state

    def health(self) -> PluginHealth:
        """An inert snapshot for the admin UI (spec section 9)."""
        return PluginHealth(
            name=self._record.name,
            state=self._state,
            transport=self._record.manifest.plugin.transport.value,
            tools=tuple(sorted(self._tools)),
            restart_count=self._restart_count,
            last_error=self._last_error,
            last_error_at=self._last_error_at,
            started_at=self._started_at,
            next_retry_at=self._next_retry_at,
            terminal=self._terminal,
            waiting_for_secrets=self._waiting_for_secrets,
        )

    @property
    def is_callable(self) -> bool:
        return self._session is not None and self._state in (
            PluginState.HEALTHY,
            PluginState.DEGRADED,
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin supervising, and wait only for the *first* attempt to resolve.

        A plugin that fails to start is a health row, not an exception, and
        startup of the core is never held hostage by a plugin that is going to
        spend the next minute backing off: this returns as soon as the first
        attempt has succeeded or failed, and the runner keeps retrying behind
        it.
        """
        if self._task is not None:
            return
        self._stop.clear()
        self._recycle.clear()
        self._settled.clear()
        self._state = PluginState.STARTING
        self._task = asyncio.create_task(self._run(), name=f"plugin:{self._record.name}")
        try:
            await asyncio.wait_for(
                self._settled.wait(), timeout=self._config.startup_timeout + 5.0
            )
        except TimeoutError:
            # The runner has its own timeout; if we are here it did not even
            # manage to record an outcome. Say so and carry on — the runner
            # keeps working in the background.
            self._note_failure(
                "took longer than expected to start, and is still being watched",
                terminal=False,
            )

    async def stop(self, *, disable: bool = False) -> None:
        """Wind the plugin down: graceful, then cancelled, then abandoned.

        A plugin that will not die does not get to hold shutdown open. After
        both escalation steps the task is let go of, the plugin is marked
        ``FAILED``, and the core continues — the SDK's own teardown has already
        tried terminate-then-kill on the whole process tree by this point, so
        what is left is an OS-level zombie the core cannot do anything about
        except refuse to wait for it.
        """
        self._stop.set()
        self._recycle.set()
        task = self._task
        self._task = None
        self._session = None

        if task is not None and not task.done():
            if not await _await_task(task, self._config.shutdown_timeout):
                task.cancel()
                if not await _await_task(task, self._config.shutdown_timeout):
                    self._abandoned = True
                    logger.error(
                        "plugin_abandoned",
                        plugin=self._record.name,
                        reason="did not stop when asked, or when cancelled",
                    )
                    self._note_failure(
                        "did not shut down when asked. It has been abandoned and "
                        "is no longer used; restart the core to clear it.",
                        terminal=True,
                    )
                    return

        self._tools = {}
        self._started_at = None
        self._next_retry_at = None
        if not self._terminal or disable:
            self._state = PluginState.DISABLED
            self._terminal = disable

    async def call(
        self, tool: str, arguments: Mapping[str, Any], *, timeout_seconds: float | None = None
    ) -> RemoteToolResult:
        """One tool call, always bounded in time.

        Raises:
            PluginTransportError: the plugin is unavailable, hung or broken.
                Recycling has already been requested by the time this is
                raised, so the next call has a chance of working.
            PluginToolError: the plugin answered with an error. It is left
                running — refusing a bad request is correct behaviour.
        """
        session = self._session
        if session is None or not self.is_callable:
            raise PluginTransportError(
                f"the {self._record.name} plugin is not running"
                + (f" ({self._last_error})" if self._last_error else "")
            )

        limit = timeout_seconds if timeout_seconds is not None else self._config.call_timeout
        try:
            async with asyncio.timeout(limit):
                result = await session.call_tool(tool, arguments, timeout_seconds=limit)
        except TimeoutError as exc:
            self._note_failure(f"did not answer {tool} within {limit:g} seconds")
            self._request_recycle()
            raise PluginTransportError(
                f"the {self._record.name} plugin did not answer within {limit:g} seconds"
            ) from exc
        except PluginToolError:
            # A live plugin answering "no". Nothing to recycle.
            raise
        except Exception as exc:
            self._note_failure(f"failed while running {tool}: {exc}")
            self._request_recycle()
            raise PluginTransportError(
                f"the {self._record.name} plugin failed while running {tool}: {exc}"
            ) from exc

        if self._state is PluginState.DEGRADED:
            # It answered. Whatever was wrong is over.
            self._state = PluginState.HEALTHY
        return result

    # -- the one task that owns the connection -----------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._recycle.clear()
            try:
                await self._attempt()
            except asyncio.CancelledError:
                raise
            except MissingPluginSecrets as exc:
                # Not a fault, and not fixed by trying again: the plugin is
                # waiting for a credential (ADR-0025 section 4). Recorded as
                # its own state, naming what it is waiting for, so its page can
                # ask for it instead of showing a plugin that appears broken.
                # Terminal in the mechanical sense only — supplying the secret
                # and reloading starts it.
                self._waiting_for_secrets = tuple(exc.names)
                listed = ", ".join(exc.names)
                credential = "a credential" if len(exc.names) == 1 else "credentials"
                self._note_failure(
                    f"is waiting for {credential} before it can start: {listed}. "
                    "Open its settings and paste the value into the field asking "
                    "for it.",
                    terminal=True,
                )
                self._settled.set()
                return
            except (PluginContractMismatch, ChildEnvironmentError) as exc:
                # Neither is fixed by trying again: one is a manifest defect,
                # the other a secret that is present but unreadable. Terminal,
                # with the reason kept.
                self._note_failure(str(exc), terminal=True)
                self._settled.set()
                return
            except Exception as exc:
                self._note_failure(_describe(exc))
                self._session = None
                self._tools = {}
                self._settled.set()
                if self._stop.is_set():
                    break
                if not await self._back_off():
                    return
            else:
                self._session = None
                self._settled.set()
                if self._stop.is_set():
                    break
                # A clean exit that we did not ask for means the plugin closed
                # the connection on its own. Treated as a crash, because from
                # the household's point of view it is one.
                if not self._recycle.is_set():
                    self._note_failure("closed its connection")
                if not await self._back_off():
                    return

        self._session = None
        self._tools = {}

    async def _attempt(self) -> None:
        """Connect, reconcile against the manifest, then stay up."""
        async with self._factory.connect(self._record) as session:
            async with asyncio.timeout(self._config.startup_timeout):
                tools = await session.list_tools()
            reconcile_tools(self._record, tools)

            self._tools = {tool.name: tool for tool in tools}
            self._session = session
            self._started_at = datetime.now(UTC)
            self._next_retry_at = None
            self._restart_count = 0
            self._waiting_for_secrets = ()
            self._state = PluginState.HEALTHY
            self._settled.set()
            logger.info(
                "plugin_started",
                plugin=self._record.name,
                transport=self._record.manifest.plugin.transport.value,
                tools=sorted(self._tools),
            )
            try:
                await self._stay_up(session)
            finally:
                self._session = None

    async def _stay_up(self, session: PluginSession) -> None:
        """Hold the connection open until asked to stop, recycle, or the
        heartbeat says the plugin has gone quiet."""
        interval = self._config.heartbeat_interval
        while not self._stop.is_set() and not self._recycle.is_set():
            if interval and interval > 0:
                if await _wait_any((self._stop, self._recycle), seconds=interval):
                    return
                async with asyncio.timeout(self._config.call_timeout):
                    await session.ping()
            else:
                await _wait_any((self._stop, self._recycle), seconds=None)
                return

    async def _back_off(self) -> bool:
        """Sleep out the restart delay. False means "give up for good"."""
        self._restart_count += 1
        if self._restart_count > self._config.max_restarts:
            self._note_failure(
                f"has been restarted {self._config.max_restarts} times and keeps "
                f"failing, so it has been switched off. Last problem: "
                f"{self._last_error or 'unknown'}",
                terminal=True,
            )
            logger.error(
                "plugin_gave_up",
                plugin=self._record.name,
                restarts=self._restart_count - 1,
            )
            self._settled.set()
            return False

        delay = min(
            self._config.backoff_initial
            * (self._config.backoff_factor ** (self._restart_count - 1)),
            self._config.backoff_max,
        )
        self._state = PluginState.DEGRADED
        self._next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        logger.warning(
            "plugin_restarting",
            plugin=self._record.name,
            attempt=self._restart_count,
            delay_seconds=round(delay, 3),
            reason=self._last_error,
        )
        await _wait_any((self._stop,), seconds=delay)
        self._next_retry_at = None
        if self._stop.is_set():
            return False
        self._state = PluginState.STARTING
        return True

    # -- bookkeeping -------------------------------------------------------

    def _request_recycle(self) -> None:
        self._state = PluginState.DEGRADED
        self._recycle.set()

    def _note_failure(self, message: str, *, terminal: bool = False) -> None:
        self._last_error = f"The {self._record.name} plugin {message}"
        self._last_error_at = datetime.now(UTC)
        if terminal:
            self._terminal = True
            self._state = PluginState.FAILED
            self._tools = {}
            self._started_at = None
            self._next_retry_at = None
        elif self._state is not PluginState.FAILED:
            self._state = PluginState.DEGRADED
        logger.warning(
            "plugin_unhealthy",
            plugin=self._record.name,
            terminal=terminal,
            reason=self._last_error,
        )


# ---------------------------------------------------------------------------
# Manifest / server reconciliation — spec section 5.1
# ---------------------------------------------------------------------------


def reconcile_tools(record: PluginRecord, remote: list[RemoteTool] | Any) -> None:
    """Check that the manifest and the running server describe the same tools.

    Reported by the plugin-template author as a real contract defect: today a
    manifest that names a tool the server does not implement (or a server that
    exposes one the manifest never declared) fails *silently* — the tool simply
    never appears, and the author has nothing to read.

    Both directions are a load failure, because in both the manifest has
    stopped being a truthful description of the plugin, and the manifest is the
    thing the core enforces against (spec section 5.1). Fail closed and name
    both sides.

    Raises:
        PluginContractMismatch: with a message written for a plugin author who
            is not holding the core's source open.
    """
    declared = set(record.manifest.tools)
    try:
        actual = {tool.name for tool in remote}
    except (AttributeError, TypeError) as exc:
        raise PluginContractMismatch(
            f"Plugin {record.name!r} sent a tool list the core could not read "
            f"({exc}). It has not been loaded."
        ) from None

    if declared == actual:
        return

    missing = sorted(declared - actual)
    undeclared = sorted(actual - declared)
    problems: list[str] = []
    if missing:
        problems.append(
            "declared in manifest.toml but not offered by the plugin: " + ", ".join(missing)
        )
    if undeclared:
        problems.append(
            "offered by the plugin but not declared in manifest.toml: " + ", ".join(undeclared)
        )

    raise PluginContractMismatch(
        f"Plugin {record.name!r} does not match its own manifest. "
        f"manifest.toml declares [{', '.join(sorted(declared)) or 'nothing'}]; "
        f"the plugin offers [{', '.join(sorted(actual)) or 'nothing'}]. "
        + "; ".join(problems)
        + ". Fix whichever side is wrong — the core will not load a plugin whose "
        "manifest it cannot trust (spec 5.1)."
    )


# ---------------------------------------------------------------------------
# Small async helpers
# ---------------------------------------------------------------------------


async def _wait_any(events: tuple[asyncio.Event, ...], *, seconds: float | None) -> bool:
    """Wait until any of ``events`` is set, or ``timeout`` elapses.

    True if an event fired, False on timeout. Pending waiters are always
    cancelled, so a long-lived supervisor does not accumulate them.
    """
    if any(event.is_set() for event in events):
        return True
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        done, pending = await asyncio.wait(
            waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for waiter in waiters:
            waiter.cancel()
    del pending
    return bool(done)


async def _await_task(task: asyncio.Task[None], seconds: float) -> bool:
    """Wait for a task, shielded so the timeout does not cancel it implicitly.

    Cancellation of a plugin's runner task is an explicit escalation step in
    :meth:`PluginSupervisor.stop`, never a side effect of a timeout — otherwise
    the graceful step and the forceful step would be the same step.
    """
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=seconds)
    except (TimeoutError, asyncio.CancelledError):
        return task.done()
    except Exception:
        # The runner raising on the way out is still the runner having stopped.
        return True
    return True


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    if isinstance(exc, PluginTransportError):
        return text or "could not be reached"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


__all__ = [
    "PluginSupervisor",
    "SupervisorConfig",
    "reconcile_tools",
]
