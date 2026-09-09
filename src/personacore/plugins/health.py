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
class EndpointHealth:
    """One **machine's** status, inside one plugin's row — ADR-0048.

    A plugin that declares ``plugin.urls`` holds one connection per machine, and
    an operator looking at it needs to know which of them is up. This is that,
    per machine.

    **A machine is the unit; its addresses are alternatives for reaching it.**
    A workstation listening on an IPv4, an IPv6 and a hostname is three entries
    in the manifest and **one** of these rows, with all three in
    :attr:`addresses`. Counting rows counts machines.

    **It is not a plugin row and must never become one.** The machines belong
    to the plugin: they are listed inside its row and on its own settings page,
    never alongside the other plugins in the list. A plugin that declares only
    ``url`` has none of these at all — the field on
    :class:`PluginHealth` is empty for it, and its ``to_dict`` does not mention
    endpoints, so nothing that renders a plugin today learns a new key.

    Inert, for the same reason :class:`PluginHealth` is.
    """

    url: str
    """This machine's **first** declared address, exactly as the manifest wrote
    it. The identity of the row, and stable whichever route is currently up, so
    a row does not move about when a machine falls through to another address.
    Every way to reach it is in :attr:`addresses`."""

    state: PluginState
    tools: tuple[str, ...] = ()
    restart_count: int = 0
    last_error: str | None = None
    """Plain English, safe to show verbatim. Never contains a secret value, and
    never the pin or the bearer token this endpoint is reached with."""

    last_error_at: datetime | None = None
    started_at: datetime | None = None
    next_retry_at: datetime | None = None
    terminal: bool = False

    addresses: tuple[str, ...] = ()
    """Every address this machine can be reached at, in the order the manifest
    declared them — the owner's own preference order, never sorted here.

    Empty means "just :attr:`url`", which is what a machine with one address
    has and what a row built by hand gets. Any of these names this machine, so
    a screen indexing rows by address indexes all of them.
    """

    connected_url: str | None = None
    """The address this machine is currently reached at, or ``None`` when it is
    not connected. Which of several routes answered — the fact an operator with
    a machine on two networks needs and cannot work out from the state alone."""

    @property
    def is_callable(self) -> bool:
        """Whether a call may be routed to this machine — see
        :attr:`PluginHealth.is_callable`, which this mirrors."""
        return self.state in (PluginState.HEALTHY, PluginState.DEGRADED)

    @property
    def every_address(self) -> tuple[str, ...]:
        """:attr:`addresses` if it was filled in, otherwise just :attr:`url`.

        The one place that reconciles the two, so nothing downstream has to
        remember that a single-address machine may not bother listing itself.
        """
        return self.addresses or (self.url,)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, nested inside the plugin's own row.

        ``addresses`` and ``connected_url`` appear only for a machine that has
        more than one way in, following the same rule
        :meth:`PluginHealth.to_dict` follows for ``endpoints``: a reader that
        will never meet a multi-homed machine is never shown a key for one, and
        the row a single-address machine serialises to is exactly the row it
        serialised to before.
        """
        payload: dict[str, Any] = {
            "url": self.url,
            "state": self.state.value,
            "tools": list(self.tools),
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "terminal": self.terminal,
        }
        if len(self.every_address) > 1:
            payload["addresses"] = list(self.every_address)
            payload["connected_url"] = self.connected_url
        return payload


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

    endpoints: tuple[EndpointHealth, ...] = ()
    """One row per **machine** behind ``plugin.urls`` — ADR-0048. Empty for
    every plugin that does not declare an endpoint set, which is every plugin
    but one.

    Not one row per entry: a machine listening on several addresses writes
    several entries and is one row here, so the length of this tuple is the
    number of machines.

    :attr:`state`, :attr:`tools` and :attr:`restart_count` above still describe
    *the plugin*: whether the assistant can use it at all, the one flat tool
    catalogue it exposes, and how much trouble it has been. These say which
    machine behind it is answering.
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
        """JSON-ready form for the admin API (spec section 9).

        ``endpoints`` appears only when there are some. A plugin that declares
        one ``url`` — every plugin installed today — produces exactly the keys
        it produced before the endpoint set existed, so nothing reading this
        has to learn about a feature it will never see. That is the same rule
        the manifest field follows, applied where it is observable.
        """
        payload: dict[str, Any] = {
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
        if self.endpoints:
            payload["endpoints"] = [endpoint.to_dict() for endpoint in self.endpoints]
        return payload


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


__all__ = ["EndpointHealth", "PluginHealth", "PluginOutput", "PluginState"]
