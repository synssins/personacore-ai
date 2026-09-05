"""The MCP plugin host — spec sections 5.1, 7, 9 and 10.

This is the component that turns "a folder full of plugins" into "the
assistant can do things". It is the agent loop's
:class:`personacore.agent.protocols.ToolProvider`: two verbs in, tools out,
and every unpleasant possibility in between contained on this side of the
seam.

What lives here, and only here
------------------------------
* **The tool namespace.** One flat catalogue of ``"<plugin>.<tool>"`` names —
  the same spelling ``PolicyProfile.allowed_tools`` uses, so the loop's
  allowlist check stays a plain set-membership test.
* **The enforcement boundary (spec section 7).** Three refusals, in order,
  before a plugin is invoked at all: the tool must be declared in the
  manifest; its declared risk must be within the caller's ceiling; and the
  plugin must actually be running. All three fail closed.
* **Containment (spec section 5.1).** ``call_tool`` has no failure path that
  raises. Crashes, hangs, malformed replies and dead plugins all come back as
  ``ToolResult(ok=False, error=...)`` carrying a sentence a persona can say
  out loud (spec section 10).
* **The audit record (spec section 7).** Every call, refused or not: plugin,
  tool, arguments, outcome, duration, risk, correlation id. Arguments go to
  the audit *store* only — never to the log stream, which gets the metadata
  and no conversation-derived content.

What deliberately does not live here
------------------------------------
Discovery and manifest validation (``discovery.py``), MCP itself
(``mcp_client.py``), and per-plugin lifecycle (``supervisor.py``). This module
is the aggregator and the gate, nothing else.

Two risk gates, on purpose
--------------------------
:meth:`personacore.agent.loop.AgentLoop.gate_tool_call` already checks risk
before it calls here, including confirmation, and it is the *authoritative*
gate — it is the one that knows about the human. This module checks again
because it is the last thing before a plugin actually runs, and every caller
of the host is not necessarily the agent loop: the exposed API (section 5.4),
the event-bus rules (section 5.2) and anything added later all arrive here.
A ceiling that only exists in the caller is not a boundary.

The ceiling is therefore a *parameter*, defaulting to
``PluginHostConfig.default_risk_ceiling``, which itself defaults to ``safe``
— the fail-closed value. Wiring the host to a caller that does its own
confirmation (the agent loop) means passing that caller's ceiling per call,
or constructing the host with a higher default and accepting that the loop's
gate is the one doing the work.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from personacore.agent.protocols import ToolResult, ToolSpec
from personacore.audit import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    Owner,
    Surface,
    get_correlation_id,
    get_logger,
)
from personacore.config.secrets import SecretStore
from personacore.contracts import RiskLevel
from personacore.plugins.discovery import PluginDiscovery, PluginRecord
from personacore.plugins.errors import PluginLoadFailure
from personacore.plugins.health import PluginHealth, PluginOutput, PluginState
from personacore.plugins.mcp_client import (
    McpSessionFactory,
    PluginToolError,
    PluginTransportError,
    SessionFactory,
)
from personacore.plugins.supervisor import PluginSupervisor, SupervisorConfig

logger = get_logger(__name__)

TOOL_SEPARATOR = "."
"""``"<plugin>.<tool>"``. Safe as a separator because neither half can contain
a dot: plugin names are ``[a-z][a-z0-9-]*`` and tool names are letters, digits,
hyphens and underscores (``contracts/manifest.py``)."""

MAX_ERROR_EXCERPT = 200
"""How much of a plugin's own error text is quoted back. It is untrusted
content (spec section 7) — the loop fences it before the model sees it, but it
is also spoken aloud, so it is short and stripped of control characters."""


@dataclass(frozen=True)
class PluginHostConfig:
    """Everything the host needs that is not a collaborator."""

    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    default_risk_ceiling: RiskLevel = RiskLevel.SAFE
    """The ceiling applied when a caller does not pass one. ``safe`` because
    the alternative is a caller that forgets and silently gets everything (see
    the module docstring)."""

    audit_surface: Surface = Surface.SYSTEM
    """Where a call is recorded as having come from when the caller does not
    say. ``system`` rather than a guess at a conversational surface."""


class PluginHost:
    """Every loaded plugin, as one :class:`ToolProvider`."""

    def __init__(
        self,
        discovery: PluginDiscovery,
        *,
        secrets: SecretStore | None = None,
        audit: Any | None = None,
        config: PluginHostConfig | None = None,
        session_factory: SessionFactory | None = None,
        disabled: Sequence[str] = (),
    ) -> None:
        self._discovery = discovery
        self._config = config or PluginHostConfig()
        self._audit = audit
        self._factory = session_factory or McpSessionFactory(
            secrets=secrets, request_timeout=self._config.supervisor.call_timeout
        )
        self._supervisors: dict[str, PluginSupervisor] = {}
        self._load_failures: list[PluginLoadFailure] = []
        self._disabled: set[str] = set(disabled)
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Scan the plugin directories and start everything found."""
        await self.reload()

    async def reload(self) -> list[PluginHealth]:
        """Rescan and converge — spec section 5.1's admin-UI reload action.

        A diff, not a restart: plugins whose folder is unchanged are left
        strictly alone, including their subprocess and their uptime. Adding a
        plugin is copying a folder and pressing reload; it must not interrupt
        the music.
        """
        async with self._lock:
            result = await asyncio.to_thread(self._discovery.scan)
            self._load_failures = list(result.failures)
            found = result.by_name()

            running = set(self._supervisors)
            # A name the scan no longer knows about at all is not installed,
            # and something that is not installed cannot be "switched off".
            # Without this the live switched-off set only ever grows: uninstall
            # resets the state *file* (``packages.uninstall_package``) while the
            # set kept here remembered the name for the life of the process, so
            # reinstalling the same plugin installed it and never started it,
            # with nothing on screen saying why. Belt to :meth:`forget`'s
            # braces, so the leak cannot come back through a caller that
            # forgets to say a plugin has gone.
            known = set(found) | {
                failure.name or failure.source.name for failure in self._load_failures
            }
            self._disabled &= known

            wanted = {name for name in found if name not in self._disabled}

            to_stop = sorted((running - wanted) | (running & self._disabled))
            to_start = sorted(wanted - running)
            to_restart = sorted(
                name
                for name in running & wanted
                if _record_changed(self._supervisors[name].record, found[name])
                # A plugin waiting for a credential is the one case where
                # nothing in its folder has to change for a retry to be worth
                # making: what it is waiting for lives in the secret store, and
                # the operator has just been to the page that asks for it
                # (ADR-0025 section 4). Cheap when it is still missing — the
                # check happens before anything is spawned — and the
                # alternative is a plugin that stays "waiting" after the thing
                # it waited for arrived, which teaches an operator to ignore
                # the row.
                or self._supervisors[name].health().is_waiting_for_secrets
            )

            if to_stop or to_start or to_restart:
                logger.info(
                    "plugin_reload",
                    started=to_start,
                    stopped=to_stop,
                    restarted=to_restart,
                    unchanged=sorted((running & wanted) - set(to_restart)),
                )

            await asyncio.gather(
                *(self._stop_one(name) for name in to_stop + to_restart),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(self._start_one(found[name]) for name in to_start + to_restart),
                return_exceptions=True,
            )
            return self._health_locked()

    async def stop(self) -> None:
        """Stop every plugin: graceful, then forceful, never blocking forever."""
        async with self._lock:
            names = sorted(self._supervisors)
            await asyncio.gather(
                *(self._stop_one(name) for name in names), return_exceptions=True
            )

    async def set_enabled(self, name: str, enabled: bool) -> PluginHealth | None:
        """The admin UI's per-plugin toggle (spec section 9)."""
        async with self._lock:
            if enabled:
                self._disabled.discard(name)
            else:
                self._disabled.add(name)
                supervisor = self._supervisors.get(name)
                if supervisor is not None:
                    await supervisor.stop(disable=True)
                    self._supervisors.pop(name, None)
                    return PluginHealth(name=name, state=PluginState.DISABLED, terminal=True)
        if enabled:
            await self.reload()
        async with self._lock:
            supervisor = self._supervisors.get(name)
            return supervisor.health() if supervisor else None

    async def forget(self, name: str) -> None:
        """Drop every trace of a plugin that has been *removed* (ADR-0013).

        Uninstall deletes a folder and resets the switched-off state file. Both
        of those are on disk; this is the half that lives in memory, and it is
        the half that was missing. ``set_enabled(name, False)`` — which the
        uninstall path calls first, so the subprocess is not holding its own
        files open while they are deleted — put the name in the switched-off
        set, and nothing ever took it out: :meth:`reload` then excluded that
        name from ``wanted`` for the life of the process. Installing the same
        plugin again installed a plugin that never started.

        So this is called by whatever performs an uninstall, immediately after
        the folder goes:

        * the supervisor is stopped, if anything is still running;
        * the name leaves the switched-off set, so a reinstall starts;
        * any load-failure row for it goes, so the admin list stops showing a
          red row for a plugin that is not there;
        * and the session factory forgets the plugin's last stderr, which
          otherwise kept answering the plugin's log page after it was removed
          — output belonging to a name that anybody may now reinstall.

        Idempotent, and safe for a name that was never installed.
        """
        async with self._lock:
            await self._stop_one(name)
            self._disabled.discard(name)
            self._load_failures = [
                failure
                for failure in self._load_failures
                if (failure.name or failure.source.name) != name
            ]
            # Structural, like `plugin_output`: a factory with nothing to
            # forget is still a valid factory (the fakes, and any transport
            # with no stderr of ours to capture).
            forget = getattr(self._factory, "forget", None)
            if forget is not None:
                forget(name)

    async def _start_one(self, record: PluginRecord) -> None:
        supervisor = PluginSupervisor(record, self._factory, config=self._config.supervisor)
        self._supervisors[record.name] = supervisor
        try:
            await supervisor.start()
        except Exception as exc:
            # start() is written not to raise; this is the belt on top of the
            # braces, because one plugin failing to start must never abort the
            # reload of the others (spec 5.1).
            logger.error("plugin_start_failed", plugin=record.name, error=repr(exc))

    async def _stop_one(self, name: str) -> None:
        supervisor = self._supervisors.pop(name, None)
        if supervisor is None:
            return
        try:
            await supervisor.stop()
        except Exception as exc:
            logger.error("plugin_stop_failed", plugin=name, error=repr(exc))

    # -- health (spec section 9) -------------------------------------------

    def health(self) -> list[PluginHealth]:
        """One row per plugin, running or not, sorted by name.

        Load failures are included as ``FAILED`` rows: a plugin whose manifest
        will not parse is exactly the plugin an operator is looking for, and
        leaving it out of the list is how it stays broken for a month.
        """
        return self._health_locked()

    def plugin_output(self, name: str) -> PluginOutput | None:
        """What one plugin last printed to stderr — spec section 9 (PC-279).

        Answered by the session factory, which owns the bounded capture file,
        and asked for structurally rather than declared on
        :class:`~personacore.plugins.mcp_client.SessionFactory`: a factory that
        does not capture output (the fakes the supervisor is tested against,
        any future transport that has no stderr) is still a valid factory, and
        requiring the method would make the narrow case describe itself as the
        broad one — the same arrangement ``reload`` gets on the admin side.

        ``None`` means "nothing is known about that plugin's output", which is
        what an HTTP-transport plugin gets: it runs in its own container, so
        its stderr is that container's, not this core's.
        """
        reader = getattr(self._factory, "output_for", None)
        if reader is None:
            return None
        return reader(name)

    def _health_locked(self) -> list[PluginHealth]:
        rows = [supervisor.health() for supervisor in self._supervisors.values()]
        seen = {row.name for row in rows}
        for failure in self._load_failures:
            name = failure.name or failure.source.name
            if name in seen:
                continue
            seen.add(name)
            rows.append(
                PluginHealth(
                    name=name,
                    state=PluginState.FAILED,
                    last_error=failure.message,
                    terminal=True,
                )
            )
        for name in sorted(self._disabled):
            if name not in seen:
                rows.append(PluginHealth(name=name, state=PluginState.DISABLED, terminal=True))
        return sorted(rows, key=lambda row: row.name)

    # -- ToolProvider ------------------------------------------------------

    async def list_tools(self) -> Sequence[ToolSpec]:
        """Every tool currently callable, across all running plugins.

        Risk comes from the manifest and never from the server: a plugin
        cannot describe its own tool as ``safe`` at runtime. The schema and
        description come from the server, because that is where they are
        accurate.
        """
        specs: list[ToolSpec] = []
        for name, supervisor in sorted(self._supervisors.items()):
            if not supervisor.is_callable:
                continue
            remote_tools = supervisor.tools
            for tool_name, declaration in supervisor.record.manifest.tools.items():
                remote = remote_tools.get(tool_name)
                if remote is None:
                    # Cannot normally happen: reconciliation refuses to load a
                    # plugin whose tools disagree with its manifest. Skipped
                    # rather than trusted, in case that ever stops being true.
                    continue
                specs.append(
                    ToolSpec(
                        name=f"{name}{TOOL_SEPARATOR}{tool_name}",
                        risk=declaration.risk,
                        description=declaration.description or remote.description,
                        parameters=remote.input_schema or {},
                    )
                )
        return specs

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        risk_ceiling: RiskLevel | None = None,
        correlation_id: str | None = None,
        owner: Owner | None = None,
        surface: Surface | None = None,
        caller_detail: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Invoke one tool. Never raises.

        Every return path is a :class:`ToolResult`, and every unhappy one
        carries an ``error`` written to be spoken aloud (spec section 10). An
        exception escaping into the agent loop would end a turn in silence,
        which is the one outcome section 5.1 forbids.
        """
        started = time.perf_counter()
        plugin_name, _, tool_name = name.partition(TOOL_SEPARATOR)
        ceiling = risk_ceiling if risk_ceiling is not None else self._config.default_risk_ceiling
        risk: RiskLevel | None = None
        try:
            supervisor = self._supervisors.get(plugin_name)
            if not tool_name or supervisor is None:
                return await self._refuse(
                    name,
                    plugin_name,
                    tool_name,
                    arguments,
                    risk=None,
                    ceiling=ceiling,
                    reason=f"I don't have a tool called {name}, so I couldn't do that.",
                    log_reason="unknown plugin",
                    started=started,
                    correlation_id=correlation_id,
                    owner=owner,
                    surface=surface,
                    caller_detail=caller_detail,
                )

            manifest = supervisor.record.manifest
            try:
                risk = manifest.risk_of(tool_name)
            except KeyError:
                # Spec 5.1: a tool the manifest does not declare has no risk
                # level, so there is nothing to enforce and it is not callable.
                return await self._refuse(
                    name,
                    plugin_name,
                    tool_name,
                    arguments,
                    risk=None,
                    ceiling=ceiling,
                    reason=(
                        f"The {plugin_name} plugin doesn't offer a tool called "
                        f"{tool_name}, so I couldn't do that."
                    ),
                    log_reason="tool not declared in manifest",
                    started=started,
                    correlation_id=correlation_id,
                    owner=owner,
                    surface=surface,
                    caller_detail=caller_detail,
                )

            if not _within(risk, ceiling):
                return await self._refuse(
                    name,
                    plugin_name,
                    tool_name,
                    arguments,
                    risk=risk,
                    ceiling=ceiling,
                    reason=(
                        f"I'm not allowed to run {name} here — it's marked "
                        f"'{risk.value}' and this conversation only permits "
                        f"'{ceiling.value}' actions."
                    ),
                    log_reason="risk above the caller's ceiling",
                    started=started,
                    correlation_id=correlation_id,
                    owner=owner,
                    surface=surface,
                    caller_detail=caller_detail,
                )

            result = await supervisor.call(tool_name, arguments, timeout_seconds=timeout_seconds)
        except PluginToolError as exc:
            return await self._finish(
                name,
                plugin_name,
                tool_name,
                arguments,
                risk=risk,
                outcome=AuditOutcome.FAILURE,
                started=started,
                correlation_id=correlation_id,
                owner=owner,
                surface=surface,
                caller_detail=caller_detail,
                result=ToolResult(
                    ok=False,
                    error=(
                        f"The {plugin_name} plugin couldn't do that"
                        f"{_excerpt(str(exc))}."
                    ),
                ),
            )
        except PluginTransportError as exc:
            return await self._finish(
                name,
                plugin_name,
                tool_name,
                arguments,
                risk=risk,
                outcome=AuditOutcome.FAILURE,
                started=started,
                correlation_id=correlation_id,
                owner=owner,
                surface=surface,
                caller_detail=caller_detail,
                result=ToolResult(
                    ok=False,
                    error=f"I can't reach the {plugin_name} plugin right now, so I "
                    "couldn't do that.",
                ),
                log_reason=str(exc),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The catch-all that keeps spec 5.1's promise. Anything at all —
            # a bug here, a plugin returning something absurd — is a failed
            # tool call, not a failed turn.
            logger.error(
                "plugin_call_crashed", plugin=plugin_name, tool=tool_name, error=repr(exc)
            )
            return await self._finish(
                name,
                plugin_name,
                tool_name,
                arguments,
                risk=risk,
                outcome=AuditOutcome.FAILURE,
                started=started,
                correlation_id=correlation_id,
                owner=owner,
                surface=surface,
                caller_detail=caller_detail,
                result=ToolResult(
                    ok=False,
                    error=f"Something went wrong talking to the {plugin_name} plugin, "
                    "so I couldn't do that.",
                ),
            )

        if result.is_error:
            return await self._finish(
                name,
                plugin_name,
                tool_name,
                arguments,
                risk=risk,
                outcome=AuditOutcome.FAILURE,
                started=started,
                correlation_id=correlation_id,
                owner=owner,
                surface=surface,
                caller_detail=caller_detail,
                result=ToolResult(
                    ok=False,
                    error=f"The {plugin_name} plugin couldn't do that"
                    f"{_excerpt(result.text)}.",
                ),
            )

        return await self._finish(
            name,
            plugin_name,
            tool_name,
            arguments,
            risk=risk,
            outcome=AuditOutcome.SUCCESS,
            started=started,
            correlation_id=correlation_id,
            owner=owner,
            surface=surface,
            caller_detail=caller_detail,
            result=ToolResult(ok=True, content=result.text, files=result.files),
        )

    # -- audit (spec section 7) --------------------------------------------

    async def _refuse(
        self,
        name: str,
        plugin_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        risk: RiskLevel | None,
        ceiling: RiskLevel,
        reason: str,
        log_reason: str,
        started: float,
        correlation_id: str | None,
        owner: Owner | None,
        surface: Surface | None,
        caller_detail: Mapping[str, Any] | None,
    ) -> ToolResult:
        logger.warning(
            "plugin_call_refused",
            plugin=plugin_name,
            tool=tool_name,
            reason=log_reason,
            risk=risk.value if risk else None,
            ceiling=ceiling.value,
        )
        return await self._finish(
            name,
            plugin_name,
            tool_name,
            arguments,
            risk=risk,
            outcome=AuditOutcome.REFUSED,
            started=started,
            correlation_id=correlation_id,
            owner=owner,
            surface=surface,
            caller_detail=caller_detail,
            result=ToolResult(ok=False, error=reason),
            detail={"refused_because": log_reason, "risk_ceiling": ceiling.value},
        )

    async def _finish(
        self,
        name: str,
        plugin_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        risk: RiskLevel | None,
        outcome: AuditOutcome,
        started: float,
        correlation_id: str | None,
        owner: Owner | None,
        surface: Surface | None,
        caller_detail: Mapping[str, Any] | None,
        result: ToolResult,
        detail: dict[str, Any] | None = None,
        log_reason: str | None = None,
    ) -> ToolResult:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        if log_reason:
            # The technical reason belongs in the trace view (spec section 9);
            # `result.error` is the version a persona says out loud.
            detail = {**(detail or {}), "reason": log_reason}
        logger.info(
            "plugin_tool_call",
            plugin=plugin_name,
            tool=tool_name,
            outcome=outcome.value,
            risk=risk.value if risk else None,
            duration_ms=duration_ms,
            error=log_reason or result.error,
        )
        written = await self._write_audit(
            action=name,
            plugin=plugin_name,
            tool=tool_name,
            arguments=arguments,
            risk=risk,
            outcome=outcome,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            owner=owner,
            surface=surface,
            caller_detail=caller_detail,
            result=result,
            detail=detail,
        )
        # Told to the caller so exactly one record exists for this call: the
        # agent loop writes its own only when this boundary did not.
        return result.model_copy(update={"audited": written})

    async def _write_audit(
        self,
        *,
        action: str,
        plugin: str,
        tool: str,
        arguments: Mapping[str, Any],
        risk: RiskLevel | None,
        outcome: AuditOutcome,
        duration_ms: float,
        correlation_id: str | None,
        owner: Owner | None,
        surface: Surface | None,
        caller_detail: Mapping[str, Any] | None,
        result: ToolResult,
        detail: dict[str, Any] | None,
    ) -> bool:
        """One audit row per call, best effort. Returns whether a row was written.

        This is *the* record for a tool call the host ran: it is the only place
        that knows how long the call took, so the agent loop stands down when
        this returns true rather than writing a second, duration-less row
        (PC-014). ``caller_detail`` is therefore merged in — whatever the caller
        knew and this boundary cannot see has nowhere else to land.

        Arguments are written here and *only* here — never to the structured
        log stream, which carries the metadata above and nothing derived from
        what somebody said (``personacore.audit.logging``).

        A failing audit store is logged and swallowed, matching the agent
        loop's reasoning: a full disk should not leave the house without an
        assistant. It is reported as "not written", so the loop records the call
        instead of both boundaries deferring to each other.
        """
        if self._audit is None:
            return False
        payload: dict[str, Any] = {
            "plugin": plugin,
            "tool": tool,
            "arguments": dict(arguments),
            "duration_ms": duration_ms,
            "ok": result.ok,
            "boundary": "plugin-host",
        }
        if caller_detail:
            # Under, not over: the fields above are what this boundary
            # observed, and a caller cannot overwrite them.
            payload = {**dict(caller_detail), **payload}
        if result.error:
            payload["error"] = result.error
        if detail:
            payload.update(detail)
        record = AuditRecord(
            # A fresh id rather than a constant when nothing is bound: a literal
            # like "plugin-host" is not an id — it groups every untied tool call
            # in the system into one meaningless bundle in the trace view
            # (PC-012). In the running application the request middleware has
            # bound one, so this fallback is for a host driven directly.
            correlation_id=correlation_id or get_correlation_id() or uuid4().hex,
            timestamp=datetime.now(UTC),
            surface=surface or self._config.audit_surface,
            owner=owner or Owner.household(),
            category=AuditCategory.TOOL_CALL,
            action=action,
            risk_level=risk,
            outcome=outcome,
            detail=payload,
        )
        try:
            await self._audit.record_audit(record)
        except Exception as exc:
            logger.error("audit_write_failed", action=action, error=repr(exc))
            return False
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.SAFE: 0,
    RiskLevel.CONFIRM: 1,
    RiskLevel.RESTRICTED: 2,
}


def _within(risk: RiskLevel, ceiling: RiskLevel) -> bool:
    """Fail closed: a risk or ceiling this function cannot rank is refused,
    never assumed harmless."""
    rank = _RISK_ORDER.get(risk)
    limit = _RISK_ORDER.get(ceiling)
    if rank is None or limit is None:
        return False
    return rank <= limit


def _excerpt(text: str) -> str:
    """A short, sanitised quote of a plugin's own error message.

    Untrusted content: newlines and control characters are stripped so it
    cannot forge log or transcript structure, and it is cut short because it
    may be spoken aloud.
    """
    cleaned = " ".join(ch for ch in text.split() if ch).strip()
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    if not cleaned:
        return ""
    if len(cleaned) > MAX_ERROR_EXCERPT:
        cleaned = cleaned[:MAX_ERROR_EXCERPT].rstrip() + "…"
    return f": {cleaned}"


def _record_changed(old: PluginRecord, new: PluginRecord) -> bool:
    """Whether a rescan found a materially different plugin under the same name.

    Manifest, config and location. Anything else — the plugin's own source
    files changing — is not visible to discovery, so "reload" restarting a
    plugin whose code changed is the operator's job to ask for by toggling it.
    """
    return (
        old.manifest != new.manifest
        or old.config != new.config
        or old.directory != new.directory
    )


__all__ = ["PluginHost", "PluginHostConfig"]
