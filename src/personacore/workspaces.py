"""Storage and lifecycle for one conversation's workspace —
``working/contracts/workspace.md`` §1 and §2.

This is the whole of §1 and §2's "your half": where a conversation's files
live on disk, what a bare filename is allowed to look like, and what happens
to the folder when the conversation it belongs to is gone. **No tools, no
prompt assembly, no cards** — those are §3, §5 and §6/§7, built against the
:class:`Workspace` and :func:`remove`/:func:`sweep` this module returns.
Modelled directly on :mod:`personacore.attachments`, one level up: there the
folder is one attachment; here the folder is one conversation, and the same
three rules apply for the same reasons.

**Flat, and validated before anything touches a path.** Contract §1: a file
inside a workspace is a bare filename — ``^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$``
— never a path. That one regex already refuses every shape contract §1
names (``/``, ``\\``, a leading dot, ``..``, an absolute path, a drive
letter): none of those characters ever appear in a string the pattern
accepts, because the first character must be alphanumeric and every
character after it comes from the same narrow set. The conversation id gets
the same treatment against :data:`CONVERSATION_ID_PATTERN`, the UUID4 shape
:func:`personacore.conversations.models.new_conversation_id` mints.

**No database table.** Contract §1: the folder is the truth for what files
exist; the ``conversations`` row is the truth for whether the folder should
exist. :func:`remove` and :func:`sweep` are what keeps the second following
the first.

**Never overwrite.** :meth:`Workspace.write` never replaces a file that is
already there — a versioned name is minted instead (`§5`) — so a fetched
source is never clobbered by an editing pass that used the same name back.
Appending is the one path that changes existing bytes, and it is refused
outright on a file recorded as a tool's own in the hidden ``.sources.json``
sidecar: the model can never address that file directly (it is not listed,
and its name cannot be typed as an argument to any tool), so it is the one
place this module keeps a fact the folder's contents alone cannot show.

**Pins per conversation** (contract §13, C) are a second hidden sidecar,
``.pins.json`` — the same treatment as ``.sources.json`` for the same reason:
never listed, never addressable, the one place :meth:`Workspace.pin`,
:meth:`Workspace.unpin` and :meth:`Workspace.pinned` keep a fact about a file
that the file itself cannot show.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from personacore.audit.logging import get_logger
from personacore.config.appdata import AppdataError, AppdataLayout

if TYPE_CHECKING:
    from collections.abc import Iterable

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shapes — contract §1
# ---------------------------------------------------------------------------

WORKSPACES_DIRNAME = "workspaces"
"""The appdata subdirectory name. See :attr:`AppdataLayout.workspaces`."""

FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
"""A bare filename inside one workspace: alphanumeric first, up to 120
characters total, and drawn from a set with no ``/``, no ``\\``, no ``..``
segment and no leading dot possible — see the module docstring."""

CONVERSATION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
"""What :func:`personacore.conversations.models.new_conversation_id` mints,
and nothing else — the same "an allowlist of exactly what we mint" approach
:data:`personacore.attachments.ATTACHMENT_ID_PATTERN` takes."""

_SOURCES_FILENAME = ".sources.json"
"""The sidecar recording which files were written by a tool rather than by
the persona. Hidden (a leading dot), so it can never be listed, read or
addressed as a workspace file by the model — see :meth:`Workspace.write`."""

_PINS_FILENAME = ".pins.json"
"""The sidecar recording which files are pinned for this conversation
(contract §13, C) — a JSON array of names, e.g. ``["B1_Ch1.stamped.md"]``.
Hidden for the same reason :data:`_SOURCES_FILENAME` is: it must never be
listed, read or addressed as a workspace file by the model."""

_MAX_VERSION_ATTEMPTS = 10_000
"""A generous ceiling on ``write``'s versioning loop, so a directory nobody
could plausibly fill this way still terminates rather than looping forever."""

MAX_FILES = 200
"""The most files one conversation's workspace may hold. Checked only when a
write is about to add a new directory entry — an append to a file that
already exists changes no count and is never refused for this reason."""

_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
"""Windows reserved device names (case-insensitive), refused as a filename
stem whether or not an extension follows — ``NUL.txt`` is exactly as
unusable on that filesystem as ``NUL`` itself."""


class WorkspaceError(Exception):
    """Something about this workspace operation is refused.

    The message is plain English, safe to show a person on a screen or hand
    back to a model as a tool result — never a traceback, and never a path.
    """


@dataclass(frozen=True)
class FileEntry:
    """One file in a workspace listing."""

    name: str
    size_bytes: int
    modified: datetime
    source: str | None
    """The tool that produced this file, or ``None`` when the persona wrote
    it itself."""
    pinned: bool = False
    """Whether this file is in this conversation's pin sidecar (contract
    §13, C) — see :meth:`Workspace.pinned`. Defaults to ``False`` so a
    caller building a :class:`FileEntry` directly (tests, mostly) is not
    forced to know about pins."""


def _require_filename(name: str) -> str:
    """A bare filename, refused rather than repaired (contract §1)."""
    value = (name or "").strip()
    if not value or not FILENAME_PATTERN.fullmatch(value):
        raise WorkspaceError(
            f"{name!r} is not a name this workspace can use for a file. A file "
            "name is letters, numbers, '.', '_' or '-', up to 120 characters, "
            "and cannot start with a dot."
        )
    stem = value.split(".", 1)[0]
    if stem.upper() in _RESERVED_DEVICE_NAMES:
        raise WorkspaceError(f"{value} is a name Windows reserves; pick another.")
    return value


def _require_conversation_id(conversation_id: str) -> str:
    """A conversation id shaped like one this core minted, refused otherwise."""
    value = (conversation_id or "").strip()
    if not value or not CONVERSATION_ID_PATTERN.fullmatch(value):
        raise WorkspaceError("That is not a conversation this core recognises.")
    return value


def _human_bytes(count: int) -> str:
    return f"{count:,} bytes"


# ---------------------------------------------------------------------------
# One conversation's workspace
# ---------------------------------------------------------------------------


class Workspace:
    """One conversation's files, over one appdata layout.

    ``path`` is computed and validated here but **never created by this
    constructor** — contract §1: the folder is created lazily, on the first
    write, and never merely because a conversation started.
    """

    def __init__(
        self,
        layout: AppdataLayout,
        conversation_id: str,
        *,
        max_file_bytes: int,
        max_workspace_bytes: int,
    ) -> None:
        checked = _require_conversation_id(conversation_id)
        candidate = layout.workspaces / checked
        try:
            layout.require_inside(candidate.parent, what="The workspaces folder")
        except AppdataError as exc:
            raise WorkspaceError(str(exc)) from exc
        self._layout = layout
        self.path = candidate
        self._max_file_bytes = max_file_bytes
        self._max_workspace_bytes = max_workspace_bytes

    # -- containment for one file inside this workspace --------------------

    def _resolve(self, checked_name: str) -> Path:
        candidate = self.path / checked_name
        try:
            return self._layout.require_inside(candidate, what="A workspace file")
        except AppdataError as exc:
            raise WorkspaceError(str(exc)) from exc

    # -- sidecar: which files came from a tool ------------------------------

    def _sources_path(self) -> Path:
        return self.path / _SOURCES_FILENAME

    def _read_sources(self) -> dict[str, str]:
        path = self._sources_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _write_sources(self, sources: dict[str, str]) -> None:
        self._sources_path().write_text(json.dumps(sources), encoding="utf-8")

    # -- sidecar: which files are pinned (contract §13, C) -------------------

    def _pins_path(self) -> Path:
        return self.path / _PINS_FILENAME

    def _read_pins(self) -> list[str]:
        path = self._pins_path()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        seen: set[str] = set()
        names: list[str] = []
        for item in data:
            if not isinstance(item, str) or item in seen:
                continue
            seen.add(item)
            names.append(item)
        return names

    def _write_pins(self, names: list[str]) -> None:
        self._pins_path().write_text(json.dumps(names), encoding="utf-8")

    def pin(self, name: str) -> None:
        """Mark an existing file pinned for this conversation.

        Refuses a name shaped wrong or one with no file behind it, the same
        way :meth:`read` does — a pin names something real, not a promise
        about a file that might arrive later. Pinning an already-pinned name
        is not an error: it simply stays pinned.
        """
        checked = _require_filename(name)
        target = self.path / checked
        if target.is_symlink() or not target.is_file():
            raise WorkspaceError(
                f"There is no file called {checked} in this conversation's workspace."
            )
        pins = self._read_pins()
        if checked not in pins:
            pins.append(checked)
            self._write_pins(pins)

    def unpin(self, name: str) -> None:
        """Clear a pin. Never raises for a name that was not pinned, or that
        does not exist — unpinning something already unpinned is a no-op,
        not a refusal."""
        checked = _require_filename(name)
        pins = self._read_pins()
        if checked in pins:
            pins.remove(checked)
            self._write_pins(pins)

    def pinned(self) -> list[str]:
        """Every name currently pinned for this conversation, in the order
        they were pinned — dropping any whose file is no longer there. A
        conversation that removed a file some other way (there is no delete
        tool today, but the sidecar makes no assumption that there never will
        be) does not keep reporting it pinned forever."""
        return [
            name
            for name in self._read_pins()
            if not (self.path / name).is_symlink() and (self.path / name).is_file()
        ]

    # -- reading -------------------------------------------------------------

    def exists(self) -> bool:
        return self.path.is_dir()

    def list(self) -> list[FileEntry]:
        """Every file in this workspace, sorted by name.

        Hidden files (a leading dot — ``.sources.json`` and ``.pins.json``)
        are never listed: the model must not be able to address, or even see
        the name of, either sidecar that records something about its
        siblings it is not allowed to touch directly.

        Reads no file's content — only ``stat()`` — so a listing costs the
        same whether the workspace holds a kilobyte or the whole ceiling,
        and a file that is not valid UTF-8 still appears (it may still fail
        to *read* later; that is :meth:`read`'s refusal to make, not this
        one's).
        """
        if not self.path.is_dir():
            return []
        sources = self._read_sources()
        pins = set(self._read_pins())
        entries: list[FileEntry] = []
        for child in self.path.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_symlink() or not child.is_file():
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                FileEntry(
                    name=child.name,
                    size_bytes=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    source=sources.get(child.name),
                    pinned=child.name in pins,
                )
            )
        entries.sort(key=lambda entry: entry.name)
        return entries

    def read(self, name: str, *, start: int = 0, end: int | None = None) -> str:
        """The text of one file, sliced by character offset.

        Refuses a name that is not shaped like a bare filename, one that does
        not exist, and — contract §1 — a symlink, checked on the
        unresolved candidate so a link that would still land inside appdata
        is caught too, not only one that would not.
        """
        checked = _require_filename(name)
        raw = self.path / checked
        if raw.is_symlink():
            raise WorkspaceError(
                f"{checked} cannot be read: it is a symlink, which a workspace refuses."
            )
        target = self._resolve(checked)
        if not target.is_file():
            raise WorkspaceError(
                f"There is no file called {checked} in this conversation's workspace."
            )
        text = target.read_text(encoding="utf-8")
        return text[start:end]

    # -- writing ---------------------------------------------------------

    def _next_versioned_name(self, name: str) -> str:
        """The next free name for ``name``, versioned before its last
        extension (contract §1's example: ``B1_Ch1.edited.md`` →
        ``B1_Ch1.edited.2.md``; a name with no extension gets ``.2``
        appended)."""
        base, dot, ext = name.rpartition(".")
        version = 2
        while version <= _MAX_VERSION_ATTEMPTS:
            candidate = f"{base}.{version}.{ext}" if dot else f"{name}.{version}"
            if not (self.path / candidate).exists():
                return candidate
            version += 1
        raise WorkspaceError(  # pragma: no cover - not reachable in practice
            f"Could not find a free name for {name}: too many versions already exist."
        )

    def _check_ceilings(
        self,
        name: str,
        resulting_file_size: int,
        *,
        added_to_total: int,
        is_new_file: bool,
    ) -> None:
        if resulting_file_size > self._max_file_bytes:
            raise WorkspaceError(
                f"{name} would be {_human_bytes(resulting_file_size)}, over the "
                f"{_human_bytes(self._max_file_bytes)} limit for one workspace file. "
                "Nothing was written."
            )
        projected_total = self.total_bytes() + added_to_total
        if projected_total > self._max_workspace_bytes:
            raise WorkspaceError(
                f"Writing {name} would put this conversation's workspace over its "
                f"{_human_bytes(self._max_workspace_bytes)} limit. Nothing was written."
            )
        if is_new_file:
            existing_files = sum(
                1
                for child in self.path.iterdir()
                if not child.name.startswith(".") and not child.is_symlink() and child.is_file()
            )
            if existing_files >= MAX_FILES:
                raise WorkspaceError(
                    f"This workspace already holds {MAX_FILES} files, which is the limit."
                )

    def write(
        self, name: str, text: str, *, append: bool = False, source: str | None = None
    ) -> str:
        """Write ``text`` under ``name``. Returns the name it was actually
        written under.

        **Never overwrites.** With ``append`` false, a name that already
        exists is written under the next free versioned name instead — see
        :meth:`_next_versioned_name`. With ``append`` true, an existing file
        that a tool produced (recorded in the hidden sources sidecar) refuses
        outright; a missing name is simply created, exactly as a fresh write
        would.
        """
        checked = _require_filename(name)
        self.path.mkdir(parents=True, exist_ok=True)
        raw = self.path / checked
        if raw.is_symlink():
            raise WorkspaceError(
                f"{checked} cannot be written: it is a symlink, which a workspace refuses."
            )
        existing = raw.is_file()
        sources = self._read_sources()

        if append:
            if existing and sources.get(checked) is not None:
                raise WorkspaceError(
                    f"{checked} came from a tool; write your version under another name."
                )
            target = self._resolve(checked)
            added = len(text.encode("utf-8"))
            current_size = target.stat().st_size if existing else 0
            self._check_ceilings(
                checked, current_size + added, added_to_total=added, is_new_file=not existing
            )
            with target.open("a", encoding="utf-8") as handle:
                handle.write(text)
            final_name = checked
        else:
            final_name = checked
            if existing:
                final_name = self._next_versioned_name(checked)
            target = self._resolve(final_name)
            content_bytes = text.encode("utf-8")
            self._check_ceilings(
                final_name,
                len(content_bytes),
                added_to_total=len(content_bytes),
                is_new_file=True,
            )
            target.write_text(text, encoding="utf-8")

        if source is not None:
            sources[final_name] = source
            self._write_sources(sources)
        return final_name

    # -- totals ---------------------------------------------------------

    def total_bytes(self) -> int:
        """This conversation's whole workspace, in bytes — visible files only;
        the hidden sources sidecar is bookkeeping, not workspace content."""
        if not self.path.is_dir():
            return 0
        total = 0
        for child in self.path.iterdir():
            if child.name.startswith(".") or child.is_symlink() or not child.is_file():
                continue
            total += child.stat().st_size
        return total


# ---------------------------------------------------------------------------
# Deleting and sweeping — contract §2
# ---------------------------------------------------------------------------


def remove(layout: AppdataLayout, conversation_id: str) -> bool:
    """Remove one conversation's whole workspace folder.

    ``True`` if something was actually removed; ``False`` for a bad id, a
    folder outside appdata, or one that was never there — never raises. The
    same non-distinction :func:`personacore.attachments.delete` makes: a
    caller here has already decided the conversation is gone, and does not
    need this function to explain which of those reasons applied.
    """
    try:
        checked = _require_conversation_id(conversation_id)
    except WorkspaceError:
        return False
    candidate = layout.workspaces / checked
    if candidate.is_symlink():
        # A symlink named for a conversation is never something this wrote —
        # `Workspace` never creates one. Resolving it (as `require_inside`
        # below would) and removing the resolved target would delete
        # whatever the link points at instead of the link itself, so this is
        # refused before any resolution happens.
        return False
    try:
        target = layout.require_inside(candidate, what="The workspaces folder")
    except AppdataError:
        return False
    if not target.exists():
        return False
    try:
        shutil.rmtree(target)
    except OSError as exc:
        _logger.error("workspace_remove_failed", conversation_id=checked, error=str(exc))
        return False
    return True


def sweep(layout: AppdataLayout, keep_ids: Iterable[str]) -> int:
    """Remove every folder under ``workspaces/`` that should not be there.

    A folder survives only if its name is a valid conversation id *and* that
    id is in ``keep_ids`` — a folder named for a conversation that no longer
    exists (or is hidden past what the caller considers visible) or one
    whose name was never a conversation id at all is removed either way.
    Returns the count removed, and logs it when it is not zero.
    """
    keep = {value for value in keep_ids if CONVERSATION_ID_PATTERN.fullmatch(value)}
    root = layout.workspaces
    if not root.is_dir():
        return 0
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if CONVERSATION_ID_PATTERN.fullmatch(child.name) and child.name in keep:
            continue
        try:
            layout.require_inside(child, what="The workspaces folder")
        except AppdataError:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except OSError as exc:
            _logger.error("workspace_sweep_item_failed", name=child.name, error=str(exc))
    if removed:
        _logger.info("workspaces_swept", removed=removed)
    return removed


__all__ = [
    "CONVERSATION_ID_PATTERN",
    "FILENAME_PATTERN",
    "MAX_FILES",
    "WORKSPACES_DIRNAME",
    "FileEntry",
    "Workspace",
    "WorkspaceError",
    "remove",
    "sweep",
]
