"""What the admin UI shows next to each plugin — spec sections 9 and 10.

Spec section 9 requires a plugin list "with live health status"; section 10
requires that a dead plugin degrades the assistant rather than stopping it.
Both need the same thing: one small, boring, serialisable snapshot per plugin
that anybody can read without knowing how the supervisor works.

Deliberately a plain dataclass with no behaviour and no references to live
objects (no session, no task, no subprocess handle). It is copied out of the
supervisor under no lock and handed to the API layer, so it must stay
inert — anything holding a live handle here would turn "show me the plugin
list" into a way to reach into a running subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class PluginState(StrEnum):
    """The five states a plugin can be in, as an operator would describe them.

    Kept to five on purpose: every extra state is another row of the admin UI
    that somebody has to learn the meaning of.
    """

    STARTING = "starting"
    """Being connected for the first time, or waiting out a restart backoff."""

    HEALTHY = "healthy"
    """Connected, handshake done, and its tools match its manifest."""

    DEGRADED = "degraded"
    """Loaded but not currently answering — a call failed or the connection
    dropped, and a restart is on its way. Tools stay listed, because the very
    next call may well succeed; section 10's "say so plainly" happens at call
    time rather than by hiding the tool."""

    FAILED = "failed"
    """Not running and not coming back on its own: it never loaded, its
    manifest disagrees with the server, or it crashed once too often. Always
    carries ``last_error`` saying which."""

    DISABLED = "disabled"
    """Deliberately not running — switched off in the admin UI, or stopped as
    part of an orderly shutdown. Never an error."""


@dataclass(frozen=True)
class PluginHealth:
    """One plugin's status, as of the moment it was asked for."""

    name: str
    state: PluginState
    transport: str | None = None
    tools: tuple[str, ...] = ()
    """Tool names the plugin currently exposes, unqualified. Empty unless it
    got far enough to be interrogated."""

    restart_count: int = 0
    last_error: str | None = None
    """Plain English, safe to show verbatim in the admin UI (spec section 9).
    Never contains a secret value — see ``personacore.audit.logging``."""

    last_error_at: datetime | None = None
    started_at: datetime | None = None
    """When the current connection was established. None if there isn't one."""

    next_retry_at: datetime | None = None
    """When the next restart attempt is due, while backing off."""

    terminal: bool = False
    """True when nothing further will be attempted without human action. The
    difference between "wait a moment" and "go and fix it"."""

    waiting_for_secrets: tuple[str, ...] = ()
    """Secret **names** the manifest declared that nobody has supplied yet,
    sorted (ADR-0025 section 4).

    Non-empty is a state, not a fault: the plugin is *waiting for a
    credential*, and the thing that fixes it is an operator pasting a value
    into the field on the plugin's own page — not a restart, and not a bug
    report. A page rendering this says so in those words and offers the field,
    rather than showing the generic red row it would show for a crash.

    Names only. A secret's value never reaches this object, its ``to_dict``,
    the page it is rendered on, a log line or an audit record (ADR-0025
    section 5), and the whole point of naming the secret here is that the
    operator can be asked for it without anything ever reading one back.

    Empty for every other kind of plugin, including one that declared secrets
    and was given all of them.
    """

    @property
    def is_waiting_for_secrets(self) -> bool:
        """Whether this plugin is held up purely waiting for a credential.

        The single question a renderer asks before choosing between "this
        plugin needs something from you" and "this plugin is broken".
        """
        return bool(self.waiting_for_secrets)

    @property
    def is_callable(self) -> bool:
        """Whether the host should route a tool call here at all.

        ``DEGRADED`` is included: the connection may already be back by the
        time the call lands, and refusing outright would turn one bad answer
        into a permanently missing capability.
        """
        return self.state in (PluginState.HEALTHY, PluginState.DEGRADED)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for the admin API (spec section 9)."""
        return {
            "name": self.name,
            "state": self.state.value,
            "transport": self.transport,
            "tools": list(self.tools),
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "terminal": self.terminal,
            "waiting_for_secrets": list(self.waiting_for_secrets),
        }


@dataclass(frozen=True)
class PluginOutput:
    """Whatever one plugin last printed to stderr — spec section 9 (PC-279).

    A plugin author debugging their own plugin should not need shell access to
    the container to read its own error messages, so the bounded capture file
    ``mcp_client`` already keeps is handed out through here.

    Inert for the same reason :class:`PluginHealth` is: it is copied out of the
    session factory and rendered by the admin UI, so it holds text and two
    booleans and no handle on anything live.

    **The text is untrusted.** It is whatever third-party code wrote to its own
    stderr, so everything downstream renders it as escaped text and never as
    markup (spec section 7).
    """

    name: str
    text: str = ""
    """The captured output, oldest line first. Empty is a perfectly normal
    state: plenty of plugins print nothing at all."""

    dropped: bool = False
    """True once the bounded capture file has overflowed and been emptied, so
    output the plugin really produced no longer exists anywhere."""

    clipped: bool = False
    """True when :attr:`text` is only the end of a longer capture. Separate
    from :attr:`dropped` because one means "we are showing you the tail" and
    the other means "the earlier part is gone" — and a page that presented a
    partial tail as the whole would be lying about both."""

    @property
    def complete(self) -> bool:
        """Whether what is held is everything the plugin printed."""
        return not (self.dropped or self.clipped)


__all__ = ["PluginHealth", "PluginOutput", "PluginState"]
