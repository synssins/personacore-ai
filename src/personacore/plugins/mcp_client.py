"""Speaking MCP to one plugin — spec section 5.1, both transports.

This module is the only place in the core that knows a plugin is an MCP
server. Everything above it (``supervisor.py``, ``host.py``) deals in three
small shapes — :class:`RemoteTool`, :class:`RemoteToolResult` and the
:class:`PluginSession` protocol — which is what lets the supervisor be tested
without spawning a subprocess, and what lets stdio and HTTP be genuinely
identical to callers (spec section 5.1: "the core treats both identically").

The protocol itself is never hand-rolled: the official ``mcp`` SDK does the
framing, the handshake and the request/response correlation. What this module
adds is the part the SDK deliberately leaves to the host — **what the child
process is allowed to see** (spec section 7).

Least privilege, the important part
-----------------------------------
A stdio plugin is a subprocess of the core, so by default it would inherit the
core's environment: the LLM host address, the core's own API key, every
``PERSONACORE_*`` setting, and anything else the operator exported into the
container. That is a complete bypass of section 5.1's "plugins receive only
the secrets their manifest declares" — the manifest would say ``secrets = []``
and the plugin would still be able to read the lot.

So the child environment is **built, never inherited**:
:func:`build_child_environment` starts from an explicit allowlist of variables
a program needs merely to run (:data:`BASE_ENV_KEYS`), and adds exactly the
secrets the manifest declared, read through
:meth:`personacore.config.secrets.SecretStore.scoped`. ``os.environ`` is never
passed through; the only values that reach the child are ones this function
put there by name.

One caveat worth knowing: the SDK merges our environment over its own
``get_default_environment()``, which re-adds the same class of "needed to run"
variables (PATH, TEMP, SYSTEMROOT and friends) from ``os.environ``. That set
is a fixed, hardcoded list in the SDK containing no application configuration
and no credentials, so the guarantee that matters — a plugin cannot see a
secret it did not declare, or any of the core's own settings — holds either
way. The allowlist here is still built explicitly rather than relying on that,
because a least-privilege boundary that depends on a dependency's default is
not a boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote, urlparse

from mcp import ClientSession, MCPError, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from personacore.agent.protocols import ToolFile
from personacore.audit import get_logger
from personacore.config.secrets import SecretError, SecretStore
from personacore.contracts.manifest import Transport
from personacore.plugins.discovery import PluginRecord
from personacore.plugins.health import PluginOutput
from personacore.workspaces import FILENAME_PATTERN

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PluginTransportError(RuntimeError):
    """The plugin could not be reached, or stopped making sense.

    Spawn failures, a dead subprocess, a timeout, a reply the SDK could not
    parse. The supervisor treats this as "recycle the plugin"; the message is
    written to be shown to a human (spec section 9).
    """


_LIVENESS_PING_SECONDS = 2.0
"""How long a liveness ping may take before the plugin counts as gone.

Short on purpose: this runs only after a call already failed, and the
question is merely "is anything still there", not "can it work".
"""


class PluginToolError(RuntimeError):
    """The plugin answered, and the answer was an error.

    A live, healthy server saying "no such tool" or "that argument is wrong".
    Distinguished from :class:`PluginTransportError` precisely so that a
    plugin is not restarted for correctly refusing a bad request.
    """


class ChildEnvironmentError(RuntimeError):
    """The child environment could not be built — almost always a declared
    secret that is not present in the secret store. Fail closed: a plugin that
    asked for a credential does not get started without it, because the
    failure mode otherwise is a plugin that runs and quietly does nothing."""


class MissingPluginSecrets(ChildEnvironmentError):
    """A declared secret has not been supplied yet — ADR-0025 section 4.

    Still fail-closed: the child is **not** started, for the reason its parent
    class gives. What is different is what the core does with it. A plugin
    nobody has given an API key to is not broken and is not going to be fixed
    by restarting it; it is *waiting for a credential*, which is a state an
    operator can resolve from the plugin's own page in one paste. So this is
    separated from every other way building the environment can fail, carried
    up as :attr:`~personacore.plugins.health.PluginHealth.waiting_for_secrets`,
    and shown as a request rather than a crash.

    :attr:`names` holds **names**, never values — as does the message, which is
    written to be shown to an operator verbatim (spec section 9).
    """

    def __init__(self, plugin: str, names: Sequence[str]) -> None:
        self.plugin = plugin
        self.names: tuple[str, ...] = tuple(names)
        listed = ", ".join(self.names)
        credential = "a credential" if len(self.names) == 1 else "credentials"
        super().__init__(
            f"Plugin {plugin!r} is waiting for {credential}: {listed}. It declares "
            "this in its manifest and nobody has supplied it yet, so it has not "
            "been started. Open the plugin's settings and paste the value into the "
            "field asking for it."
        )


class PluginContractMismatch(RuntimeError):
    """The manifest and the running server disagree about which tools exist.

    Spec section 5.1 is "manifest declares, core enforces": if the two sides
    do not describe the same set of tools, neither can be trusted, and the
    plugin does not load. Terminal — restarting cannot fix a manifest.
    """


# ---------------------------------------------------------------------------
# The child environment (spec section 7)
# ---------------------------------------------------------------------------

BASE_ENV_KEYS: tuple[str, ...] = (
    # POSIX and Windows both need a way to find an interpreter and a temp dir.
    # Nothing here carries application configuration or a credential.
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TZ",
)
"""The entire inherited surface. Adding to this list is a security decision:
every entry is something a plugin can read that its manifest never asked for.

Notably absent and deliberately so: ``PYTHONPATH`` (would let a plugin be
pointed at the core's own modules), ``HOME``/``USERPROFILE`` (a plugin's home
is its own folder), and anything ``PERSONACORE_*``.
"""

FORCED_ENV: dict[str, str] = {
    # stdio *is* the protocol channel; a buffered child looks like a hung one.
    "PYTHONUNBUFFERED": "1",
    # Plugin folders are read-mostly and often mounted read-only; scattering
    # __pycache__ into them fails noisily for no benefit.
    "PYTHONDONTWRITEBYTECODE": "1",
}


def build_child_environment(
    record: PluginRecord, secrets: SecretStore | None = None
) -> dict[str, str]:
    """The complete environment a stdio plugin will run with.

    Built from :data:`BASE_ENV_KEYS` plus :data:`FORCED_ENV` plus exactly the
    secrets ``record.manifest.permissions.secrets`` declares, **read from the
    plugin's own namespace** (ADR-0025). ``os.environ`` is read only through
    the allowlist — never copied.

    The namespace is the boundary, not the declared list: the plugin's name is
    part of the path, so a manifest naming a secret that exists in another
    plugin's namespace or in the core's does not reach it — that name is simply
    not supplied, and the plugin waits for a credential of its own.

    **Optional requests do not block a start** (ADR-0026). A request carrying
    ``required = false`` that nobody has supplied is simply absent from the
    environment: the plugin starts, and decides for itself what to do without
    it. Only a missing *required* credential holds the plugin back, because
    only that one is a plugin that genuinely cannot run.

    Raises:
        MissingPluginSecrets: one or more **required** secrets have not been
            supplied. Carries their names so the plugin can be shown as waiting
            for a credential rather than as broken.
        ChildEnvironmentError: a declared secret is present but unreadable, or
            the plugin declared secrets and no store was supplied.
    """
    env: dict[str, str] = {}
    for key in BASE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(FORCED_ENV)

    declared = record.manifest.permissions.secret_names
    required = set(record.manifest.permissions.required_secrets)
    if not declared:
        return env

    if secrets is None:
        if not required:
            # Every request is optional, so a core with no store is a core that
            # supplies none of them — which is a case the plugin already has to
            # handle (ADR-0026). Refusing to start it would be this core
            # inventing a requirement the manifest did not state.
            return env
        raise ChildEnvironmentError(
            f"Plugin {record.name!r} asks for the secrets "
            f"{', '.join(sorted(required))}, but this core was started without a "
            "secret store, so it cannot be given them. It has not been started."
        )

    try:
        scoped = secrets.scoped(record.name, declared)
    except SecretError as exc:
        # A manifest asking for something that could never be a secret name.
        # Discovery's own validation should have caught it; this is the second
        # lock, and it says which plugin rather than leaking a bare store error
        # into the supervisor's generic "it crashed" path.
        raise ChildEnvironmentError(
            f"Plugin {record.name!r} cannot start: {exc}"
        ) from None
    absent = scoped.missing()
    blocking = [name for name in absent if name in required]
    if blocking:
        # Asked as one question rather than one per name: an operator being
        # told "it needs A", supplying it, and then being told "it needs B" is
        # the same errand walked twice.
        raise MissingPluginSecrets(record.name, blocking)
    skip = set(absent)
    for name in declared:
        if name in skip:
            # An optional request nobody supplied. Not in the environment at
            # all rather than present-and-empty: a plugin testing for the
            # variable gets a clean "no", and "" is a value somebody could have
            # pasted on purpose (ADR-0026).
            continue
        try:
            env[name] = scoped.get(name).get_secret_value()
        except SecretError as exc:
            raise ChildEnvironmentError(
                f"Plugin {record.name!r} cannot start: it declares the secret "
                f"{name!r} in its manifest, and {exc}"
            ) from None
    return env


# ---------------------------------------------------------------------------
# The small shapes everything above this module deals in
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteTool:
    """One tool as the *running server* describes it.

    The risk level is deliberately not here: risk comes from the manifest and
    only from the manifest, so that a plugin cannot promote its own tool to
    ``safe`` at runtime (spec section 5.1).
    """

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteToolResult:
    """What one tool call produced, rendered to text.

    ``text`` is untrusted content from outside the core (spec section 7). It
    is fenced by ``personacore.agent.untrusted`` before it reaches the model;
    nothing here interprets it.
    """

    text: str = ""
    is_error: bool = False
    files: list[ToolFile] = field(default_factory=list)
    """Text resources the plugin handed back — workspace contract §3.
    Populated by :func:`render_call_result`; ``PluginHost.call_tool`` passes
    this straight onto the :class:`~personacore.agent.protocols.ToolResult`
    it returns."""


@runtime_checkable
class PluginSession(Protocol):
    """One live connection to one plugin, transport already forgotten."""

    async def list_tools(self) -> Sequence[RemoteTool]:
        ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, timeout_seconds: float
    ) -> RemoteToolResult:
        ...

    async def ping(self) -> None:
        """Cheap liveness check. Raises :class:`PluginTransportError` if the
        plugin is not answering."""
        ...


class SessionFactory(Protocol):
    """Opens a session for a discovered plugin.

    A protocol rather than a concrete class so the supervisor's whole state
    machine — backoff, restart, contract reconciliation, containment — can be
    tested against fakes that crash, hang or lie, without a subprocess in
    sight.
    """

    def connect(self, record: PluginRecord) -> AbstractAsyncContextManager[PluginSession]:
        ...


# ---------------------------------------------------------------------------
# Rendering an MCP result down to text
# ---------------------------------------------------------------------------

MAX_RESULT_CHARS = 64 * 1024
"""A tool result is fenced and handed to a model with a finite context. A
plugin returning a megabyte is a malfunction, not a long answer, so the result
is cut here rather than after it has been copied through four more layers."""


def _resource_filename(uri: object) -> str | None:
    """Workspace contract §3: the last path segment of a resource's uri, run
    through the workspace's own filename rule. ``None`` when nothing usable
    survives that — the resource is then omitted rather than guessed at, so
    a plugin's malformed uri costs a file, not a made-up name.
    """
    raw = str(uri).rstrip("/")
    if not raw:
        return None
    segment = unquote(raw.rsplit("/", 1)[-1])
    return segment if FILENAME_PATTERN.fullmatch(segment) else None


def _resource_pin(block: object) -> bool:
    """Workspace contract §13, C: a resource block asks to be pinned by
    carrying ``_meta: {"personacore": {"pin": true}}``.

    ``mcp`` 2.0's ``EmbeddedResource`` exposes this as the Python attribute
    ``meta`` (``_meta`` is only its wire/serialisation alias — confirmed with
    ``EmbeddedResource.model_fields["meta"].alias``). Anything else —
    ``meta`` absent, not a dict, ``personacore`` absent or not a dict, ``pin``
    absent or not literally ``True`` — means unpinned and never raises: a
    plugin's malformed metadata costs the pin, not the file.
    """
    meta = getattr(block, "meta", None)
    if not isinstance(meta, dict):
        return False
    own = meta.get("personacore")
    if not isinstance(own, dict):
        return False
    return own.get("pin") is True


def render_call_result(result: object) -> RemoteToolResult:
    """Turn an SDK ``CallToolResult`` into text, plus any files it carried.

    Raises:
        PluginTransportError: the server sent something that is not a tool
            result at all. A server that answers a tool call with the wrong
            message type is broken, not merely unhelpful.
    """
    if not isinstance(result, types.CallToolResult):
        raise PluginTransportError(
            "the plugin answered a tool call with a "
            f"{type(result).__name__} instead of a tool result"
        )

    parts: list[str] = []
    files: list[ToolFile] = []
    for block in result.content:
        if getattr(block, "type", None) == "resource":
            # Workspace contract §3: a text resource becomes a `ToolFile`
            # instead of the placeholder every other non-text block gets.
            # The 64 KiB cap below does not apply to it — only to `parts`,
            # which this never joins. A blob resource (or a resource with no
            # text at all) still falls through to the placeholder.
            resource = getattr(block, "resource", None)
            resource_text = getattr(resource, "text", None) if resource is not None else None
            if isinstance(resource_text, str):
                name = _resource_filename(getattr(resource, "uri", ""))
                if name is not None:
                    mime = getattr(resource, "mime_type", None) or "text/plain"
                    files.append(
                        ToolFile(name=name, mime=mime, text=resource_text, pin=_resource_pin(block))
                    )
                    continue
                logger.warning(
                    "mcp_resource_name_refused",
                    uri=str(getattr(resource, "uri", "")),
                )
            parts.append("[resource content omitted]")
            continue

        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            # Images and audio are not something the text agent loop can use
            # today. Say so rather than dropping silently.
            parts.append(f"[{getattr(block, 'type', 'unknown')} content omitted]")

    if not parts and not files and result.structured_content is not None:
        try:
            parts.append(json.dumps(result.structured_content, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(result.structured_content))

    rendered = "\n".join(parts)
    if len(rendered) > MAX_RESULT_CHARS:
        rendered = rendered[:MAX_RESULT_CHARS] + "\n[truncated]"
    return RemoteToolResult(text=rendered, is_error=bool(result.is_error), files=files)


# ---------------------------------------------------------------------------
# The real session, over the SDK
# ---------------------------------------------------------------------------


class McpPluginSession:
    """:class:`PluginSession` over an SDK ``ClientSession``.

    Its whole job is translating the SDK's failure vocabulary into this
    module's two-way split: "the plugin is broken" (:class:`PluginTransportError`,
    the supervisor recycles it) versus "the plugin said no"
    (:class:`PluginToolError`, the supervisor leaves it alone).
    """

    def __init__(self, session: ClientSession, stderr: _StderrCapture | None = None) -> None:
        self._session = session
        self._stderr = stderr

    @property
    def stderr_tail(self) -> str:
        """The last of whatever the plugin printed to stderr — which is where
        the template tells plugin authors to put their startup errors, so this
        is usually the sentence that explains a failure to a human."""
        return self._stderr.tail() if self._stderr else ""

    async def list_tools(self) -> Sequence[RemoteTool]:
        try:
            result = await self._session.list_tools()
        except MCPError as exc:
            raise PluginTransportError(
                f"the plugin refused to list its tools: {exc}"
            ) from exc
        except Exception as exc:
            raise PluginTransportError(f"could not read the plugin's tool list: {exc}") from exc
        return [
            RemoteTool(
                name=tool.name,
                description=tool.description,
                input_schema=dict(tool.input_schema or {}),
            )
            for tool in result.tools
        ]

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, timeout_seconds: float
    ) -> RemoteToolResult:
        try:
            result = await self._session.call_tool(
                name, dict(arguments), read_timeout_seconds=timeout_seconds
            )
        except MCPError as exc:
            # MCPError covers two very different things: a live server answering
            # "no", and the transport having died underneath us ("Connection
            # closed"). Treating both as a tool error left a killed plugin
            # marked healthy and never restarted — it looked like the plugin
            # politely declining, forever.
            #
            # So ask it. A plugin that answers a ping was alive and refused the
            # call; one that cannot is a transport failure and must be recycled.
            # The ping only happens on the error path, so a healthy call pays
            # nothing for it.
            try:
                async with asyncio.timeout(_LIVENESS_PING_SECONDS):
                    await self._session.send_ping()
            except Exception as ping_exc:
                raise PluginTransportError(
                    f"the plugin stopped answering: {exc}"
                ) from ping_exc
            raise PluginToolError(str(exc)) from exc
        except PluginTransportError:
            raise
        except Exception as exc:
            raise PluginTransportError(f"could not be called: {exc}") from exc
        return render_call_result(result)

    async def ping(self) -> None:
        try:
            await self._session.send_ping()
        except Exception as exc:
            raise PluginTransportError(f"stopped answering: {exc}") from exc


# ---------------------------------------------------------------------------
# stderr capture — bounded, because a broken plugin will fill anything
# ---------------------------------------------------------------------------


PLUGIN_OUTPUT_CHARS = 20_000
"""How much of one plugin's stderr the admin UI is offered (PC-279).

Far more than the 2000 characters an error message carries — the page exists to
be read rather than glanced at — and far less than the 256 KiB the capture file
allows, because a browser given a quarter of a megabyte of one plugin's chatter
stops being a diagnostic. Anything past it is reported as clipped rather than
silently dropped.
"""


MAX_UTF8_BYTES_PER_CHAR = 4
"""UTF-8's widest encoding. Used to turn a character budget into a byte budget
that is guaranteed to hold at least that many characters."""


class _StderrCapture:
    """A size-capped file for one plugin's stderr.

    The SDK hands the child's stderr straight to the OS, so it must be a real
    file with a real descriptor — an in-memory buffer cannot be used. That
    makes an unbounded log a genuine disk-exhaustion risk from a plugin stuck
    in a print loop, so the file is truncated once it passes ``limit``: a
    plugin can be noisy, it just cannot be noisy *and* persistent.

    All filesystem work happens in synchronous methods. Most of them are called
    from the supervisor's setup and teardown paths, off the event loop's
    critical path; :meth:`snapshot` is the exception — the admin UI's plugin
    output page calls it while rendering (PC-279). It reads a fixed number of
    bytes from the end of a local file and nothing else, which is the same
    order of work as the config read the plugin settings screen already does
    inline, so it does not want a thread. That bound is load-bearing rather
    than tidy: see :meth:`snapshot`.
    """

    def __init__(self, name: str, limit: int = 256 * 1024) -> None:
        self._name = name
        self._limit = limit
        self._dir = Path(tempfile.mkdtemp(prefix="personacore-plugin-"))
        self._path = self._dir / f"{name}.stderr.log"
        # Append mode: the write handle never seeks, so reading through a
        # second handle below cannot move the child's write position.
        self._file = self._path.open("a", encoding="utf-8", errors="replace")
        self._dropped = False
        """Set once :meth:`trim_if_oversized` has actually thrown output away.

        Remembered rather than inferred, because after the truncation there is
        nothing left on disk to infer it from — and "this plugin printed
        nothing" and "this plugin printed so much we deleted it" are opposite
        answers to the only question the page is asked (PC-279)."""

    @property
    def stream(self) -> Any:
        return self._file

    @property
    def path(self) -> Path:
        return self._path

    def tail(self, limit: int = 2000) -> str:
        return self.snapshot(limit=limit).text

    def snapshot(self, *, limit: int = 2000) -> PluginOutput:
        """The end of what the plugin has printed, and whether it is all of it.

        The *end*, because the last thing a dying plugin said is the sentence
        that explains it — and read from the end rather than read whole and
        then sliced. The size cap is only applied at teardown, so a capture
        belonging to a plugin that has been running and chattering for a week
        can be any size at all while it is open; this is reached from a page
        render, and "read it all, keep the tail" would let one noisy plugin
        turn every visit to that page into a multi-megabyte read. Bounded
        before it is used, like anything else from outside (spec section 7).

        Clipping is reported rather than done quietly: a partial tail presented
        as the whole output is the one way the page above this could actively
        mislead somebody who is debugging.
        """
        try:
            self._file.flush()
        except (OSError, ValueError):
            pass
        # Four bytes is UTF-8's widest character, so this many bytes always
        # contains at least `limit` characters — the tail is never short
        # because the plugin happened to print emoji.
        budget = limit * MAX_UTF8_BYTES_PER_CHAR
        try:
            with self._path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(size - budget, 0))
                raw = handle.read(budget)
        except OSError:
            return PluginOutput(name=self._name, dropped=self._dropped)
        # `errors="replace"`: seeking to a byte offset can land mid-character,
        # and one replacement mark at the very front of an already-clipped tail
        # is a better outcome than refusing to show any of it.
        text = raw.decode("utf-8", errors="replace").strip()
        long = len(text) > limit
        return PluginOutput(
            name=self._name,
            text=text[-limit:] if long else text,
            dropped=self._dropped,
            clipped=long or size > budget,
        )

    @property
    def dropped(self) -> bool:
        """Whether this capture has ever thrown output away."""
        return self._dropped

    def trim_if_oversized(self) -> None:
        try:
            if self._path.stat().st_size > self._limit:
                os.truncate(self._path, 0)
                self._dropped = True
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._file.close()
        except (OSError, ValueError):
            pass
        shutil.rmtree(self._dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


class McpSessionFactory:
    """Opens real MCP sessions — a subprocess for stdio, HTTP for HTTP."""

    def __init__(
        self,
        *,
        secrets: SecretStore | None = None,
        request_timeout: float = 30.0,
        stderr_limit: int = 256 * 1024,
    ) -> None:
        self._secrets = secrets
        self._request_timeout = request_timeout
        self._stderr_limit = stderr_limit
        self._open: dict[str, _StderrCapture] = {}
        """Captures belonging to sessions that are open right now."""

        self._remembered: dict[str, PluginOutput] = {}
        """The last output each plugin was seen to produce.

        Kept because the capture file is deleted with the session that owned it
        (:meth:`_StderrCapture.close`), and the plugin whose output somebody
        needs is nearly always one that has just died — reading only live
        captures would give an empty page for exactly the plugin the page is
        for. Only replaced by a *non-empty* reading, so a restart that is
        already in flight cannot blank out the sentence explaining why the
        previous one failed.
        """

    def output_for(self, plugin_name: str) -> PluginOutput | None:
        """What this plugin last printed to stderr, or ``None`` if unknown.

        ``None`` means "this factory has never opened a capture for that name"
        — an HTTP-transport plugin, or one that has not been started. It is not
        the same answer as an empty :class:`PluginOutput`, which means "it ran
        and printed nothing", and the admin UI says the two differently
        (PC-279).
        """
        capture = self._open.get(plugin_name)
        if capture is not None:
            live = capture.snapshot(limit=PLUGIN_OUTPUT_CHARS)
            if live.text or plugin_name not in self._remembered:
                self._remembered[plugin_name] = live
                return live
        return self._remembered.get(plugin_name)

    def forget(self, plugin_name: str) -> None:
        """Throw away everything kept about one plugin's output.

        Called when a plugin is *removed*, not when it stops. The remembered
        reading exists to outlive a session (see :attr:`_remembered`); it must
        not outlive the plugin. Without this, uninstalling a plugin left its
        last stderr readable — ``/admin/plugins/<gone>/logs`` answering 200 for
        something that is no longer installed — and a name that is not
        installed is one anybody may now install, so the output would be shown
        under somebody else's plugin.

        Both halves go: the remembered reading, and any live registration, so
        that a capture still winding down cannot put the reading back.
        Idempotent — forgetting a plugin that was never started is fine, which
        is what an HTTP plugin and a never-started one both are.
        """
        self._remembered.pop(plugin_name, None)
        self._open.pop(plugin_name, None)

    def _remember(self, name: str, capture: _StderrCapture) -> None:
        """Take a last reading before the capture file goes with the session.

        Read *then* trimmed, in that order: trimming empties the file, so a
        reading taken afterwards would report nothing at all for the noisiest
        plugin on the system. The trim's verdict is stamped back on, because
        that is the half the page has to say out loud.
        """
        final = capture.snapshot(limit=PLUGIN_OUTPUT_CHARS)
        capture.trim_if_oversized()
        final = replace(final, dropped=capture.dropped)
        if final.text or name not in self._remembered:
            self._remembered[name] = final

    def connect(self, record: PluginRecord) -> AbstractAsyncContextManager[PluginSession]:
        return self._connect(record)

    @asynccontextmanager
    async def _connect(self, record: PluginRecord) -> AsyncIterator[PluginSession]:
        transport = record.manifest.plugin.transport
        if transport is Transport.STDIO:
            async with self._connect_stdio(record) as session:
                yield session
        else:
            async with self._connect_http(record) as session:
                yield session

    # -- stdio -------------------------------------------------------------

    @asynccontextmanager
    async def _connect_stdio(self, record: PluginRecord) -> AsyncIterator[PluginSession]:
        env = build_child_environment(record, self._secrets)
        entry = record.manifest.plugin.entry or ""
        # Plain whitespace splitting, matching exactly what discovery
        # security-checked (`discovery._validate_entry`). Using a smarter
        # tokenizer here would mean the string that was validated and the
        # string that gets executed are tokenized by different rules, which is
        # how sandbox escapes are built. Quoted paths with spaces are therefore
        # not supported in `entry`, on purpose.
        tokens = entry.split()
        if not tokens:
            raise PluginTransportError(
                f"Plugin {record.name!r} has no start command in its manifest."
            )

        capture = _StderrCapture(record.name, limit=self._stderr_limit)
        # Registered before the child exists so the admin UI can read a live
        # capture while the plugin is running, rather than only after it dies.
        self._open[record.name] = capture
        params = StdioServerParameters(
            command=tokens[0],
            args=tokens[1:],
            env=env,
            cwd=str(record.directory),
        )
        try:
            async with AsyncExitStack() as stack:
                try:
                    streams = await stack.enter_async_context(
                        stdio_client(params, errlog=capture.stream)
                    )
                except PluginTransportError:
                    raise
                except Exception as exc:
                    raise PluginTransportError(f"could not start {entry!r}: {exc}") from exc
                yield await self._open_session(streams, stack, capture=capture)
        except BaseExceptionGroup as group:
            # The SDK runs its transport inside an anyio task group, so a
            # subprocess that dies during the handshake surfaces as an
            # ExceptionGroup on the way out — and an operator reading
            # "unhandled errors in a TaskGroup (1 sub-exception)" in the admin
            # UI has learned nothing. Unwrap it back to the sentence that
            # actually explains the failure, with whatever the plugin printed
            # to stderr on the end (spec section 9).
            raise PluginTransportError(
                f"{_flatten(group)}{_stderr_suffix(capture)}"
            ) from None
        except PluginTransportError as exc:
            raise PluginTransportError(f"{exc}{_stderr_suffix(capture)}") from None
        finally:
            # Only while this capture is still the registered one. A capture
            # that has been de-registered belongs to a plugin that has been
            # removed (:meth:`forget`), and a session winding down must not put
            # back output that was deliberately thrown away.
            if self._open.get(record.name) is capture:
                self._remember(record.name, capture)
                del self._open[record.name]
            capture.close()

    # -- http --------------------------------------------------------------

    @asynccontextmanager
    async def _connect_http(self, record: PluginRecord) -> AsyncIterator[PluginSession]:
        url = record.manifest.plugin.url or ""
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            # Fail closed: a manifest naming file:// or anything else is not a
            # network plugin, and the core will not open it.
            raise PluginTransportError(
                f"Plugin {record.name!r} declares the address {url!r}, which is not "
                "an http:// or https:// URL. HTTP plugins are reached over the "
                "network and nothing else."
            )
        try:
            async with AsyncExitStack() as stack:
                try:
                    streams = await stack.enter_async_context(streamable_http_client(url))
                except PluginTransportError:
                    raise
                except Exception as exc:
                    raise PluginTransportError(f"could not reach {url}: {exc}") from exc
                yield await self._open_session(streams, stack)
        except BaseExceptionGroup as group:
            raise PluginTransportError(f"{url}: {_flatten(group)}") from None

    # -- shared handshake --------------------------------------------------

    async def _open_session(
        self,
        streams: Any,
        stack: AsyncExitStack,
        *,
        capture: _StderrCapture | None = None,
    ) -> PluginSession:
        read_stream, write_stream = streams[0], streams[1]
        client = ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=self._request_timeout,
            client_info=types.Implementation(name="personacore", version=_core_version()),
        )
        await stack.enter_async_context(client)
        try:
            await client.initialize()
        except PluginTransportError:
            raise
        except Exception as exc:
            raise PluginTransportError(f"could not complete the MCP handshake: {exc}") from exc
        return McpPluginSession(client, capture)


def _flatten(group: BaseException) -> str:
    """The most useful sentence inside a (possibly nested) exception group.

    A transport error we raised ourselves wins, because it is the one written
    for a human; otherwise the first leaf, named by type so the message is not
    just an empty string.
    """
    leaves: list[BaseException] = []
    stack: list[BaseException] = [group]
    while stack:
        current = stack.pop()
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        else:
            leaves.append(current)
    for leaf in leaves:
        if isinstance(leaf, PluginTransportError):
            return str(leaf)
    if not leaves:
        return "the connection failed"
    first = leaves[0]
    text = str(first).strip()
    return f"{type(first).__name__}: {text}" if text else type(first).__name__


def _stderr_suffix(capture: _StderrCapture | None) -> str:
    if capture is None:
        return ""
    tail = capture.tail(400)
    return f" — the plugin said: {tail}" if tail else ""


def _core_version() -> str:
    from personacore import __version__

    return __version__


__all__ = [
    "BASE_ENV_KEYS",
    "FORCED_ENV",
    "PLUGIN_OUTPUT_CHARS",
    "ChildEnvironmentError",
    "McpPluginSession",
    "McpSessionFactory",
    "MissingPluginSecrets",
    "PluginContractMismatch",
    "PluginSession",
    "PluginToolError",
    "PluginTransportError",
    "RemoteTool",
    "RemoteToolResult",
    "SessionFactory",
    "build_child_environment",
    "render_call_result",
]
