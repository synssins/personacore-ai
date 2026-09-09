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
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from personacore.audit import get_logger
from personacore.contracts.manifest import EndpointDeclaration
from personacore.enrolment.registry import is_machine_secret_name
from personacore.plugins.discovery import PluginRecord
from personacore.plugins.health import EndpointHealth, PluginHealth, PluginState
from personacore.plugins.mcp_client import (
    ChildEnvironmentError,
    EndpointSessionFactory,
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
# The second path: one plugin, several endpoints (ADR-0048)
# ---------------------------------------------------------------------------

MACHINE_CREDENTIAL_MISSING = (
    "This core has no credential for this machine, so it cannot connect to it. "
    "There is nothing to type in — a machine's credential is created by this "
    "core and sent to the machine when it joins. Remove the machine here and "
    "join it again from the machine itself."
)
"""What a machine whose token has gone missing says, instead of asking for it.

The plain-English half of the rule in
:meth:`EndpointSetSupervisor._endpoint_error`: it names what is wrong, says why
there is nothing to supply, and gives the one action that fixes it. It names
neither the secret nor its value.
"""


def group_endpoints_by_machine(
    endpoints: Sequence[EndpointDeclaration],
) -> list[tuple[str, tuple[EndpointDeclaration, ...]]]:
    """Group a plugin's endpoint set into **machines**, in declared order.

    A machine is the unit; its addresses are alternatives for reaching it. One
    workstation that listens on an IPv4, an IPv6 and a hostname writes three
    entries into ``plugin.urls`` and is one machine — so grouping happens here,
    once, and everything above counts, lists and connects to what this returns.

    **The key is the entry's own ``auth_secret``.** No new manifest field: the
    per-machine identity is already in the document, because each machine mints
    its own bearer token and enrolment writes that token's name onto every
    address of that machine (``machine_<id>_token``). Two entries carrying the
    same token name are the same machine by construction — the token is the
    thing only that machine will accept.

    An entry with no ``auth_secret`` of its own is keyed by its address, so it
    is a machine on its own. That is not a fallback, it is the documented
    meaning of the field: an entry naming no token is sent the plugin's own
    (:attr:`~personacore.contracts.manifest.EndpointDeclaration.auth_secret`),
    which is what a set of *separate* machines standing behind one shared
    credential looks like. Folding those together would merge machines that
    have nothing to do with each other.

    Returns ``(key, entries)`` pairs. Machines come out in the order their first
    address was declared, and a machine's addresses in the order they were
    declared — the owner's own preference order, chosen in the Agent's address
    picker, which is never sorted or reordered anywhere on this path.
    """
    groups: dict[str, list[EndpointDeclaration]] = {}
    for endpoint in endpoints:
        key = (
            f"token:{endpoint.auth_secret}"
            if endpoint.auth_secret
            else f"address:{endpoint.url}"
        )
        groups.setdefault(key, []).append(endpoint)
    return [(key, tuple(entries)) for key, entries in groups.items()]


class _MachineFactory:
    """A :class:`SessionFactory` for **one machine**, whose addresses are tried
    in turn.

    The adapter that lets :class:`EndpointSetSupervisor` be built out of real
    :class:`PluginSupervisor` instances instead of a parallel lifecycle. The
    child asks for "the session for this record" exactly as every supervisor in
    the house does, and this turns that into "the session for this record, at
    the first of this machine's addresses that answers".

    **One connection per machine, not per address.** Falling through happens
    inside a single connection attempt, so a machine that bridges two networks
    is one child, one health row and one MCP session however many ways there
    are to reach it.

    Note which way the reuse runs. The *set* is expressed in terms of the single
    connection, one per machine; the single connection is not expressed in terms
    of a set. A plugin that declares one ``url`` is built by
    :class:`PluginSupervisor` directly, through the factory's own
    :meth:`~personacore.plugins.mcp_client.SessionFactory.connect`, with nothing
    on its path that knows an endpoint set exists.
    """

    def __init__(
        self,
        factory: EndpointSessionFactory,
        endpoints: tuple[EndpointDeclaration, ...],
    ) -> None:
        self._factory = factory
        self._endpoints = endpoints
        self.connected_url: str | None = None
        """The address this machine is currently reached at, or ``None``. Set
        when an attempt succeeds and cleared when the connection closes, so a
        machine with several addresses can say which one it landed on."""

    @property
    def addresses(self) -> tuple[str, ...]:
        """Every way to reach this machine, in the order the manifest wrote
        them."""
        return tuple(endpoint.url for endpoint in self._endpoints)

    def connect(self, record: PluginRecord) -> Any:
        return self._connect(record)

    @asynccontextmanager
    async def _connect(self, record: PluginRecord) -> AsyncIterator[PluginSession]:
        """Open the first address that answers, and hold it.

        Addresses are tried in declared order and the first that connects wins;
        the rest are not dialled at all. A machine asleep at its IPv4 and awake
        at its IPv6 is online, once.

        Three failures are **not** worth a second address, and are raised as
        they arrive: a missing credential, a manifest that disagrees with the
        server, and an unreadable child environment. All three belong to the
        machine rather than to the route — every address of a machine is sent
        the same token and measured against the same manifest — so trying the
        next one would produce the identical failure and bury the first, which
        is the one the operator needs to read.
        """
        problems: list[tuple[str, BaseException]] = []
        for endpoint in self._endpoints:
            async with AsyncExitStack() as stack:
                try:
                    session = await stack.enter_async_context(
                        self._factory.connect_endpoint(record, endpoint)
                    )
                except (
                    MissingPluginSecrets,
                    PluginContractMismatch,
                    ChildEnvironmentError,
                    asyncio.CancelledError,
                ):
                    raise
                except Exception as exc:  # noqa: BLE001 - recorded, then the next address
                    problems.append((endpoint.url, exc))
                    continue
                self.connected_url = endpoint.url
                try:
                    yield session
                finally:
                    self.connected_url = None
                return
        raise _nothing_answered(problems)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"_MachineFactory({', '.join(self.addresses)})"


def _nothing_answered(problems: list[tuple[str, BaseException]]) -> BaseException:
    """What a machine reports when none of its addresses answered.

    One address keeps the exception it raised, untouched: a machine reachable
    one way is the ordinary case, and rewording its failure would lose the
    sentence the transport wrote. Several addresses need a sentence of their
    own, because "connection refused" for the third address is not the story —
    the story is that every route was tried and each one failed.
    """
    if len(problems) == 1:
        return problems[0][1]
    detail = "; ".join(f"{url} ({_describe(exc)})" for url, exc in problems)
    return PluginTransportError(
        f"none of this machine's {len(problems)} addresses answered: {detail}"
    )


@dataclass(frozen=True)
class _Machine:
    """One machine of an endpoint set: its addresses, and the child that owns
    the single connection to it."""

    key: str
    """What grouped it — see :func:`group_endpoints_by_machine`. Opaque, and
    never shown to anybody: it can carry the *name* of a secret."""

    endpoints: tuple[EndpointDeclaration, ...]
    supervisor: PluginSupervisor
    factory: _MachineFactory

    @property
    def addresses(self) -> tuple[str, ...]:
        return self.factory.addresses

    @property
    def url(self) -> str:
        """The machine's first declared address — its identity on a screen.

        Stable whichever route is currently up, so a row does not move about
        under an operator when a machine falls through to its second address.
        """
        return self.endpoints[0].url

    @property
    def is_callable(self) -> bool:
        return self.supervisor.is_callable

    def describe(self) -> str:
        """Every way in, for the list the owner answers from."""
        return " or ".join(self.addresses)


class EndpointSetSupervisor:
    """Owns the lifecycle of one plugin that fronts **several machines**.

    Built only for a plugin whose manifest declares ``plugin.urls``, which is
    the workstation plugin and nothing else. It presents the same surface to
    :class:`~personacore.plugins.host.PluginHost` as
    :class:`PluginSupervisor` — ``record``, ``name``, ``tools``, ``state``,
    ``health()``, ``is_callable``, ``start``, ``stop``, ``call`` — so the host
    branches once, when it decides which to build, and never again.

    **The unit is the machine, not the address.** A workstation that listens on
    an IPv4, an IPv6 and a hostname writes three entries into ``plugin.urls``
    and is one machine here: one connection, one health row, counted once,
    listed once, and its addresses tried in the order the manifest declares them
    until one answers. Grouping is done once, by
    :func:`group_endpoints_by_machine`, so ``len(self._machines)`` genuinely
    means machines and every count, question and listing below is right by
    construction rather than by a correction applied at each site.

    **This is a second path, not a generalisation of the first.** The owner
    approved adding an endpoint set beside the single ``url`` and refused the
    tidier alternative of making every plugin a set with one entry (ADR-0048).
    So :class:`PluginSupervisor` is untouched, and a plugin declaring one
    ``url`` never reaches this class — it is not a set of one here, it is the
    thing it always was.

    What is genuinely shared is what should be: every machine's connection,
    backoff, restart ceiling, contract reconciliation and containment are a
    real :class:`PluginSupervisor`, because those are properties of *one
    connection to one MCP server* and there is no second implementation of them
    anywhere.
    """

    def __init__(
        self,
        record: PluginRecord,
        factory: EndpointSessionFactory,
        *,
        config: SupervisorConfig | None = None,
    ) -> None:
        endpoints = record.manifest.plugin.urls
        if not endpoints:
            raise ValueError(
                f"plugin {record.name!r} has no endpoint set; a plugin that declares "
                "one url is supervised by PluginSupervisor"
            )
        if not hasattr(factory, "connect_endpoint"):
            raise TypeError(
                f"plugin {record.name!r} declares an endpoint set, which needs a "
                "session factory that can open one endpoint (connect_endpoint)"
            )
        self._record = record
        self._config = config or SupervisorConfig()
        self._disabled = False
        self._machines: dict[str, _Machine] = {}
        """One entry per machine, in the order its first address was declared.
        The set's addresses are grouped into these before anything else looks at
        them, so nothing above this line counts an address as a machine."""

        self._by_address: dict[str, _Machine] = {}
        """Every declared address, mapped to the machine it reaches. Several
        addresses of one machine all land on the same entry — which is how a
        caller naming any of them gets that machine's one connection."""

        for key, entries in group_endpoints_by_machine(endpoints):
            machine_factory = _MachineFactory(factory, entries)
            machine = _Machine(
                key=key,
                endpoints=entries,
                supervisor=PluginSupervisor(record, machine_factory, config=self._config),
                factory=machine_factory,
            )
            self._machines[key] = machine
            for entry in entries:
                self._by_address[entry.url] = machine

    # -- read-only surface -------------------------------------------------

    @property
    def record(self) -> PluginRecord:
        return self._record

    @property
    def name(self) -> str:
        return self._record.name

    @property
    def machines(self) -> Sequence[_Machine]:
        """The machines, in declared order. The unit everything else counts."""
        return tuple(self._machines.values())

    @property
    def endpoints(self) -> Mapping[str, PluginSupervisor]:
        """``{address -> the supervisor of the machine it reaches}``. For a
        resolver that has picked one.

        Several addresses of one machine map to the **same** supervisor, because
        there is one connection per machine. Reading this as a machine list
        counts a workstation with three addresses three times, which is the
        defect this class was reshaped to remove: :attr:`machines` is the list.

        **Not the source of what a screen shows.** A child's own ``health()``
        is a plugin row written for a plugin, and for a missing credential it
        says to paste the value into the field asking for it — which is right
        for an API key and wrong for a machine token nobody can supply. The rows
        meant for rendering are ``health().endpoints``, which have been through
        :meth:`_endpoint_error`.
        """
        return {url: machine.supervisor for url, machine in self._by_address.items()}

    @property
    def tools(self) -> Mapping[str, RemoteTool]:
        """One flat catalogue for the plugin, merged across machines.

        Owner decision: the machine is a tool *argument*, not part of the tool
        name, so five machines running the same server publish one set of
        tools, not five. Merged from the machines that are answering — a
        machine that is offline does not remove the tool, because another
        machine can still run it.
        """
        merged: dict[str, RemoteTool] = {}
        for machine in self._machines.values():
            if machine.is_callable:
                merged.update(machine.supervisor.tools)
        return merged

    @property
    def state(self) -> PluginState:
        return self._plugin_state()

    @property
    def is_callable(self) -> bool:
        return any(machine.is_callable for machine in self._machines.values())

    def health(self) -> PluginHealth:
        """One plugin row, carrying one endpoint row per **machine**.

        The plugin row deliberately does **not** name an address. Machines
        belong to the plugin and appear on the plugin's own settings page, not
        in the plugin list (reshape plan decision 0.1), so the list says how
        many are not answering and the page says which.

        **A missing machine token is not a credential anybody can supply**, and
        it is filtered out of ``waiting_for_secrets`` here rather than rendered
        like one — see :meth:`_endpoint_error`.
        """
        # Paired, not keyed by address: the machine is the identity here, and a
        # document that somehow wrote one address under two machines would
        # otherwise lose a row to a dictionary collision rather than show the
        # operator two machines fighting over an address.
        children = [
            (machine, machine.supervisor.health()) for machine in self._machines.values()
        ]
        rows = tuple(
            EndpointHealth(
                url=machine.url,
                addresses=machine.addresses,
                connected_url=machine.factory.connected_url,
                state=row.state,
                tools=row.tools,
                restart_count=row.restart_count,
                last_error=self._endpoint_error(row),
                last_error_at=row.last_error_at,
                started_at=row.started_at,
                next_retry_at=row.next_retry_at,
                terminal=row.terminal,
            )
            for machine, row in children
        )
        # "Not answering" is asked of the live supervisor, not of the row's
        # state: a machine waiting out a restart backoff reads DEGRADED,
        # which is usable-if-it-comes-back on a plugin row and is plainly
        # *down* when the question is how many machines are up right now.
        down = sum(1 for machine in self._machines.values() if not machine.is_callable)
        plugin_rows = [row for _, row in children]
        stamps = [row.last_error_at for row in plugin_rows if row.last_error_at]
        return PluginHealth(
            name=self._record.name,
            state=self._plugin_state(),
            transport=self._record.manifest.plugin.transport.value,
            tools=tuple(sorted(self.tools)),
            restart_count=sum(row.restart_count for row in plugin_rows),
            last_error=self._plugin_error(down, len(rows)),
            last_error_at=max(stamps, default=None),
            started_at=min(
                (row.started_at for row in plugin_rows if row.started_at is not None),
                default=None,
            ),
            next_retry_at=min(
                (row.next_retry_at for row in plugin_rows if row.next_retry_at is not None),
                default=None,
            ),
            terminal=all(row.terminal for row in plugin_rows),
            waiting_for_secrets=tuple(
                sorted(
                    {
                        name
                        for row in plugin_rows
                        for name in row.waiting_for_secrets
                        if not is_machine_secret_name(name)
                    }
                )
            ),
            endpoints=rows,
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Connect every machine at once.

        Concurrently, because one unreachable machine must not hold up the
        others: ``PluginSupervisor.start`` already returns as soon as its own
        first attempt has resolved either way, so the slowest machine sets the
        pace rather than the sum of them. A machine's *own* addresses are tried
        one after another inside that attempt — they are alternatives, and
        dialling them at once would open connections nobody asked for.
        """
        self._disabled = False
        await asyncio.gather(
            *(machine.supervisor.start() for machine in self._machines.values())
        )

    async def stop(self, *, disable: bool = False) -> None:
        self._disabled = disable
        await asyncio.gather(
            *(
                machine.supervisor.stop(disable=disable)
                for machine in self._machines.values()
            )
        )

    async def call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        endpoint: str | None = None,
    ) -> RemoteToolResult:
        """One tool call, against one machine.

        ``endpoint`` names an address of the machine to run it on — **any** of
        that machine's addresses names the same machine, and the call goes down
        the one connection it already has, not down the address given. Choosing
        that machine from what the caller said is a resolver's job and is not
        done here — this raises rather than guessing, because a wrong machine is
        a worse answer than a question (reshape plan decision 0.3: never a
        default, never a silent nearest-match).

        **Ambiguity is a property of what is configured, not of what is
        reachable.** Written out because the obvious version of this method is
        wrong in a way that looks right: filter the machines down to the ones
        currently answering, and if exactly one is left, use it. Three machines
        with two asleep then reduces to one, the guard never fires, and a shell
        command runs on whichever desktop happened to be awake — a default
        chosen by chance, which is precisely what was ruled out. So the count
        that decides is ``len(self._machines)``, and being offline never
        promotes a machine to the one that was meant.

        Raises:
            PluginTransportError: more than one machine is configured and none
                was named; the named one is not reachable; or none is. Three
                different things to the person on the other end, so three
                different sentences — the host turns whichever it gets into
                what the assistant says, and that is where the owner's "show
                the list and ask" comes from.
        """
        if endpoint is not None:
            machine = self._by_address.get(endpoint)
            if machine is None:
                raise PluginTransportError(
                    f"the {self._record.name} plugin has nothing at {endpoint}"
                )
            if not machine.is_callable:
                raise PluginTransportError(self._machine_offline_message(tool, endpoint))
            return await machine.supervisor.call(
                tool, arguments, timeout_seconds=timeout_seconds
            )

        if len(self._machines) > 1:
            # Asked before "is anything up", because with several machines
            # configured the answer is a question either way — but which
            # question depends on whether any of them can be reached.
            if not any(machine.is_callable for machine in self._machines.values()):
                raise PluginTransportError(self._nothing_reachable_message(tool))
            raise PluginTransportError(self._which_machine_message(tool))

        (only,) = self._machines.values()
        if not only.is_callable:
            raise PluginTransportError(self._nothing_reachable_message(tool))
        return await only.supervisor.call(tool, arguments, timeout_seconds=timeout_seconds)

    # -- a missing machine token is not a credential to ask for -------------

    def _endpoint_error(self, row: PluginHealth) -> str | None:
        """One machine's error, with the one sentence that must never be shown.

        A supervisor that cannot find a plugin's declared secret says so by
        name and tells the operator to paste the value into the field asking for
        it. That is exactly right for an API key somebody signed up for, and it
        is exactly wrong here: **a machine's bearer token is minted by this core
        and pushed to the machine, so there is nothing for anybody to paste**,
        no field asking for it, and an interface that asks for one is asking the
        owner to repair by hand the thing enrolment exists to stop him repairing
        by hand.

        So a machine whose token is missing is reported as what it is — a broken
        machine, with the one action that fixes it — and its name is kept out of
        ``waiting_for_secrets`` so no row above renders it as a credential
        request. The secret's *name* is not in the sentence either: it is a
        lookup key nobody types, and printing it invites a search for a box to
        type it into.
        """
        if any(is_machine_secret_name(name) for name in row.waiting_for_secrets):
            return MACHINE_CREDENTIAL_MISSING
        return row.last_error

    # -- the three refusals, which are three different things ---------------

    def _listing(self) -> str:
        """Every configured **machine** and whether it is answering.

        One item per machine, however many ways there are to reach it: a
        workstation on an IPv4 and an IPv6 is one thing to choose, not two, and
        listing it twice asks the owner to pick between two addresses of the
        same PC. Its alternatives are shown beside it because any of them names
        it, and this is the list he answers from.

        Both halves are useful and neither is the core's to choose between: the
        owner may want the one that is up, or may want to know the one he had
        in mind is down. This is the list the assistant shows him, so the
        addresses are in it — unlike the plugin list row, where a machine does
        not belong.
        """
        return ", ".join(
            f"{machine.describe()} ({'online' if machine.is_callable else 'offline'})"
            for machine in self._machines.values()
        )

    def _which_machine_message(self, tool: str) -> str:
        """More than one machine configured, and none named."""
        return (
            f"the {self._record.name} plugin has {len(self._machines)} machines, so "
            f"{tool} needs to say which one. Machines: {self._listing()}"
        )

    def _machine_offline_message(self, tool: str, endpoint: str) -> str:
        """A machine was named, and it is not answering.

        Named back with the address the caller used, not with the machine's
        whole address list: he asked about that one, and the answer is about the
        machine he meant.
        """
        return (
            f"the {self._record.name} plugin cannot reach {endpoint} — that machine "
            f"is offline, so {tool} did not run. It was not run anywhere else."
        )

    def _nothing_reachable_message(self, tool: str) -> str:
        """No machine is answering at all."""
        return (
            f"the {self._record.name} plugin cannot reach any of its machines, so "
            f"{tool} did not run. Machines: {self._listing()}"
        )

    # -- the plugin's own state, from its machines' -------------------------

    def _plugin_state(self) -> PluginState:
        states = [machine.supervisor.state for machine in self._machines.values()]
        if self._disabled or all(state is PluginState.DISABLED for state in states):
            return PluginState.DISABLED
        if all(state is PluginState.HEALTHY for state in states):
            return PluginState.HEALTHY
        if any(state in (PluginState.HEALTHY, PluginState.DEGRADED) for state in states):
            # Some machine is answering, so the plugin is usable and the
            # assistant should keep offering its tools — the same reason
            # `PluginHealth.is_callable` includes DEGRADED.
            return PluginState.DEGRADED
        if any(state is PluginState.STARTING for state in states):
            return PluginState.STARTING
        return PluginState.FAILED

    def _plugin_error(self, down: int, total: int) -> str | None:
        """The plugin row's one sentence — a count of machines, never an address.

        Machines belong to the plugin's own settings page (reshape plan
        decision 0.1). An address in the plugin list would put a machine in the
        one place the owner said machines do not appear, so the list says how
        many and the page says which.
        """
        if not down:
            return None
        if down == total:
            return (
                f"The {self._record.name} plugin cannot reach any of its "
                f"{total} machines. Open its settings to see why."
            )
        return (
            f"The {self._record.name} plugin cannot reach {down} of its "
            f"{total} machines. Open its settings to see which."
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
    "EndpointSetSupervisor",
    "PluginSupervisor",
    "SupervisorConfig",
    "group_endpoints_by_machine",
    "reconcile_tools",
]
