"""``WorkspaceTools`` — the model-facing workspace tools, contract §5 and
§13's ``pin``/``unpin`` addition.

Modelled directly on :mod:`personacore.memory.tools` (:class:`MemoryTools`):
one ``specs()`` returning plain JSON-schema tool descriptions and one async
``call()`` that never raises, every failure becoming a
:class:`~personacore.agent.protocols.ToolResult` with ``ok=False``. Where
``MemoryTools`` is keyed by *owner* (who is speaking), this is keyed by
*conversation* — a workspace belongs to the conversation, not the person, so
the one piece of identity every call needs is the conversation id, carried
the same way ``memory.*`` carries it: on the turn's ``caller_detail``
(``agent/loop.py:_handle_tool_call``), read by
:class:`personacore.memory.composite.CompositeToolProvider` and handed on.

**Descriptions are written for a small local model.** Each one says exactly
what the tool returns, not just what it does, because a model that has never
seen ``workspace.read_file`` before has only the schema and the description to
go on — the same discipline ``memory/tools.py`` follows for
``memory.remember``/``memory.recall``.

**Never lists, reads, or writes outside one conversation's own folder.**
Every operation goes through :class:`personacore.workspaces.Workspace`, which
owns the filename rule, the containment check and the never-overwrite
versioning (contract §1). This module adds no path handling of its own.
"""

from __future__ import annotations

import structlog

from personacore.agent.protocols import ToolResult, ToolSpec
from personacore.config.appdata import AppdataLayout
from personacore.config.workspace import WorkspaceSettings
from personacore.contracts import RiskLevel
from personacore.workspaces import Workspace, WorkspaceError

logger = structlog.get_logger(__name__)

LIST_FILES_TOOL = "workspace.list_files"
READ_FILE_TOOL = "workspace.read_file"
WRITE_FILE_TOOL = "workspace.write_file"
PIN_TOOL = "workspace.pin"
UNPIN_TOOL = "workspace.unpin"

_LIST_FILES_DESCRIPTION = (
    "List every file already saved in this conversation's workspace: its name, how "
    "many bytes it holds, and when it was last changed. Returns 'The workspace "
    "is empty.' when there is nothing there yet. Use this before reading or writing "
    "a file, to see what already exists and under what name."
)
_READ_FILE_DESCRIPTION = (
    "Read a file already saved in this conversation's workspace. Give the exact "
    "file name shown by workspace.list_files. Returns the file's text — the whole "
    "file if it is short enough, otherwise the first part plus a line telling you "
    "the character offset to pass as 'start' to read the rest. Optional 'start' and "
    "'end' read only the characters between those two offsets."
)
_WRITE_FILE_DESCRIPTION = (
    "Save text as a file in this conversation's workspace, so it can be read back "
    "later in this conversation. Never overwrites an existing file: writing under a "
    "name that is already used creates a new version instead, and the reply tells "
    "you the exact name it was saved under. Set 'append' to true to add text to the "
    "end of a file you yourself wrote earlier this conversation, instead of creating "
    "a new version — this fails on a file that came from a tool. Returns a short "
    "confirmation naming the file and its size."
)

_PIN_DESCRIPTION = (
    "Pin a file already saved in this conversation's workspace, so its whole text is "
    "put in front of you at the start of every later turn in this conversation, "
    "without having to call workspace.read_file for it again. Give the exact file "
    "name shown by workspace.list_files."
)
_UNPIN_DESCRIPTION = (
    "Unpin a file in this conversation's workspace, so it stops being shown whole "
    "on every turn and goes back to being read on request with workspace.read_file. "
    "Unpinning a file that is not pinned is not an error."
)

_NO_CONVERSATION_ERROR = "There's no conversation to keep a workspace for."


class WorkspaceTools:
    """``workspace.list_files``, ``workspace.read_file``, ``workspace.write_file``,
    ``workspace.pin`` and ``workspace.unpin``, over one :class:`AppdataLayout`
    and one :class:`WorkspaceSettings`.

    One instance serves every conversation: a :class:`Workspace` is a cheap,
    stateless view over one folder, built fresh for whichever conversation a
    call names — see :meth:`workspace_for`, also used by
    :class:`personacore.agent.loop.AgentLoop` for the manifest, the pins, and
    saving a tool's own files (contract §3, §4, §6), so the ceilings and the
    root are read from exactly one place.
    """

    def __init__(self, layout: AppdataLayout, settings: WorkspaceSettings) -> None:
        self._layout = layout
        self._settings = settings

    @property
    def settings(self) -> WorkspaceSettings:
        return self._settings

    def workspace_for(self, conversation_id: str) -> Workspace:
        """The :class:`Workspace` for one conversation, ceilings from
        :attr:`settings`. Raises :class:`WorkspaceError` for an id that is
        not shaped like one this core minted — callers here catch it; the
        agent loop, which controls when this is ever called with a
        conversation id at all, does not need to."""
        return Workspace(
            self._layout,
            conversation_id,
            max_file_bytes=self._settings.max_file_bytes,
            max_workspace_bytes=self._settings.max_workspace_bytes,
        )

    def specs(self) -> list[ToolSpec]:
        """All five tools, ``RiskLevel.SAFE`` (contract §5: "they touch one
        jailed folder and nothing else")."""
        return [
            ToolSpec(
                name=LIST_FILES_TOOL,
                risk=RiskLevel.SAFE,
                description=_LIST_FILES_DESCRIPTION,
                parameters={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name=READ_FILE_TOOL,
                risk=RiskLevel.SAFE,
                description=_READ_FILE_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The exact file name, from workspace.list_files.",
                        },
                        "start": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Character offset to start reading from. Leave out to "
                                "start at the beginning of the file."
                            ),
                        },
                        "end": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Character offset to stop reading at. Leave out to read "
                                "to the end of the file."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                name=WRITE_FILE_TOOL,
                risk=RiskLevel.SAFE,
                description=_WRITE_FILE_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The file name to save the content under.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The text to save.",
                        },
                        "append": {
                            "type": "boolean",
                            "description": (
                                "True to add this text to the end of a file you already "
                                "wrote this conversation, instead of saving a new "
                                "version. Defaults to false."
                            ),
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            ToolSpec(
                name=PIN_TOOL,
                risk=RiskLevel.SAFE,
                description=_PIN_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The exact file name, from workspace.list_files.",
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                name=UNPIN_TOOL,
                risk=RiskLevel.SAFE,
                description=_UNPIN_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The exact file name, from workspace.list_files.",
                        },
                    },
                    "required": ["path"],
                },
            ),
        ]

    async def call(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        conversation_id: str | None,
    ) -> ToolResult:
        """Run one workspace tool. Never raises: every failure — a bad
        argument, a name shaped wrong, a ceiling hit — becomes a
        ``ToolResult(ok=False, ...)`` (contract §5, and the same "say it,
        don't raise it" rule every tool in this core follows).
        """
        if not conversation_id:
            return ToolResult(ok=False, error=_NO_CONVERSATION_ERROR)
        try:
            workspace = self.workspace_for(conversation_id)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        try:
            if name == LIST_FILES_TOOL:
                return self._list_files(workspace)
            if name == READ_FILE_TOOL:
                return self._read_file(arguments, workspace)
            if name == WRITE_FILE_TOOL:
                return self._write_file(arguments, workspace)
            if name == PIN_TOOL:
                return self._pin(arguments, workspace)
            if name == UNPIN_TOOL:
                return self._unpin(arguments, workspace)
        except Exception as exc:  # noqa: BLE001 - a workspace failure never fails the turn
            logger.error("workspace_tool_failed", tool=name, error=repr(exc))
            return ToolResult(ok=False, error="I couldn't do that with the workspace just now.")
        return ToolResult(ok=False, error=f"{name} is not a workspace tool.")

    # -- the five tools -----------------------------------------------------

    def _list_files(self, workspace: Workspace) -> ToolResult:
        entries = workspace.list()
        if not entries:
            return ToolResult(ok=True, content="The workspace is empty.")
        lines = [
            f"{entry.name} — {entry.size_bytes:,} bytes — {entry.modified.strftime('%H:%M')}"
            + (" (pinned)" if entry.pinned else "")
            for entry in entries
        ]
        return ToolResult(ok=True, content="\n".join(lines))

    def _read_file(self, arguments: dict[str, object], workspace: Workspace) -> ToolResult:
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            return ToolResult(ok=False, error="I need a file name to read.")
        start_raw = arguments.get("start")
        start = (
            start_raw
            if isinstance(start_raw, int) and not isinstance(start_raw, bool) and start_raw >= 0
            else 0
        )
        end_raw = arguments.get("end")
        end = (
            end_raw
            if isinstance(end_raw, int) and not isinstance(end_raw, bool) and end_raw >= 0
            else None
        )

        try:
            whole = workspace.read(path)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        total = len(whole)
        sliced = whole[start:end]
        cap = self._settings.tool_result_chars
        if len(sliced) <= cap:
            return ToolResult(ok=True, content=sliced)

        shown_end = start + cap
        note = (
            f"\nShowing characters {start + 1}–{shown_end} of {total}. "
            f"Call read_file with start={shown_end} for the rest."
        )
        return ToolResult(ok=True, content=sliced[:cap] + note)

    def _write_file(self, arguments: dict[str, object], workspace: Workspace) -> ToolResult:
        path = arguments.get("path")
        content = arguments.get("content")
        append = arguments.get("append", False)
        if not isinstance(path, str) or not path.strip():
            return ToolResult(ok=False, error="I need a file name to write to.")
        if not isinstance(content, str):
            return ToolResult(ok=False, error="I need some content to write.")
        if not isinstance(append, bool):
            append = False

        try:
            final_name = workspace.write(path, content, append=append)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        verb = "Appended to" if append else "Wrote"
        return ToolResult(ok=True, content=f"{verb} {final_name} ({len(content):,} chars).")

    def _pin(self, arguments: dict[str, object], workspace: Workspace) -> ToolResult:
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            return ToolResult(ok=False, error="I need a file name to pin.")
        try:
            workspace.pin(path)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, content=f"Pinned {path}.")

    def _unpin(self, arguments: dict[str, object], workspace: Workspace) -> ToolResult:
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            return ToolResult(ok=False, error="I need a file name to unpin.")
        try:
            workspace.unpin(path)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=True, content=f"Unpinned {path}.")


__all__ = [
    "LIST_FILES_TOOL",
    "PIN_TOOL",
    "READ_FILE_TOOL",
    "UNPIN_TOOL",
    "WRITE_FILE_TOOL",
    "WorkspaceTools",
]
