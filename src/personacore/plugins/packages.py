"""Plugin packages — install from a zip, enable, disable, uninstall (ADR-0013).

Spec section 5.1 describes installing a plugin as "copy a folder, hit reload",
which is true of the mechanism and useless as an experience: it needs shell
access to the machine. ADR-0013 replaces it with **a zip uploaded through the
admin UI**, and this module is the half of that decision that touches disk.

Three promises, in the order they matter:

**Nothing from the package is ever executed, imported or installed.** No setup
script, no ``pip``, no import of anything inside the archive. Extraction here is
inert byte copying. A plugin runs when the supervisor starts it, under the
permissions its manifest declares — not while it is being unpacked, when nothing
has been checked yet (ADR-0013).

**An uploaded zip is untrusted input (spec section 7)**, and it is the most
exposed surface in the product: a path from "a file" to "code the core will
run". Every check in :func:`_check_member` is a refusal, not a warning — path
traversal in any spelling, symlinks and other non-regular members, names
Windows resolves to a device rather than to a file, and size or count limits on
both the archive and its *uncompressed* total. The wording of those refusals is
the caller's (:class:`PackageWords`) because the voice installer reaches the
same checks and its operator is not installing a plugin; the checks themselves
exist once.

**Validation is reused, not re-implemented.** The staged directory is handed to
:class:`~personacore.plugins.discovery.PluginDiscovery` — the same scanner that
loads plugins from appdata — and the package is accepted only if that scanner
would load it. There is therefore exactly one idea in the core of what a valid
plugin folder is, and a package cannot be installable and unloadable at once.
The three raw-path helpers imported from ``discovery`` are private to that
module and used here for the same reason: two ideas of "unsafe path string" is
one too many, and this module is in the same package.

Enable/disable state lives in ``<appdata>/config/plugins-disabled.toml`` so it
survives a restart (ADR-0013: "state lives in appdata, survives restarts"). It
is deliberately *not* stored inside the plugin's own folder — uninstalling would
take it with it, and "switched off" has to outlive the thing it is about.
"""

from __future__ import annotations

import os
import shutil
import stat
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from secrets import token_hex

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from personacore.audit import get_logger
from personacore.config.appdata import AppdataError, AppdataLayout
from personacore.config.secrets import SecretError, SecretStore
from personacore.contracts.manifest import _NAME_RE as _PLUGIN_NAME_RE
from personacore.contracts.manifest import Transport
from personacore.plugins.discovery import (
    CONFIG_FILENAME,
    HTTP_DIRNAME,
    MANIFEST_FILENAME,
    STDIO_DIRNAME,
    PluginDiscovery,
    PluginRecord,
    _has_raw_drive_prefix,
    _has_raw_traversal_segment,
    _is_absolute_path_string,
)

logger = get_logger(__name__)

PLUGIN_NAME_PATTERN = _PLUGIN_NAME_RE.pattern
"""The manifest's own name rule, borrowed rather than restated, so an endpoint
that takes a plugin name in its path accepts exactly what a manifest may
declare."""

STAGING_DIRNAME = ".staging"
"""Uploads are unpacked in ``<appdata>/plugins/.staging/<token>/``.

Inside appdata, never a shared temp location (ADR-0013): the machine's ``/tmp``
may be a different volume, world-readable, or absent in a container. Being a
sibling of the destination also makes the final move a rename rather than a copy
across filesystems. The leading dot keeps discovery out of it — it skips folders
starting with ``.`` or ``_`` — so a half-unpacked upload can never be scanned as
a plugin.
"""

REPLACED_DIRNAME_PREFIX = ".replaced-"
"""Prefix of the folder an installed plugin is set aside under while it is being
replaced (spec section 7: an upgrade must never discard appdata content).

It is a sibling of the plugin's own folder rather than a child of the staging
directory, and both halves of that matter. Staging is deleted unconditionally
when :func:`install_package` returns, so a backup kept there is a working plugin
one un-taken ``except`` branch away from being gone for good; and the plugins
root is guaranteed to be the same filesystem as the destination, so setting the
folder aside is a rename rather than a copy that can half-finish.

The leading dot keeps discovery out of it — it skips folders starting with ``.``
or ``_`` — so a backup that outlives a crashed install is inert rather than a
second plugin of the same name."""

DISABLED_STATE_FILENAME = "plugins-disabled.toml"
DISABLED_STATE_KEY = "disabled"

_IGNORED_TOP_LEVEL = frozenset({"__MACOSX"})
"""Archive folders that are packaging debris rather than a plugin. macOS adds
this to every zip made in Finder, and a package rejected for "more than one
folder" because of it would be a refusal nobody can act on."""

_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)
"""Names Windows resolves to a device rather than to a folder, in whatever
directory they are written — case-insensitively, and before the extension, so
``con`` and ``CON.txt`` are the same device.

Refused on every platform rather than only on Windows, and for a *member of an
archive* as much as for the name a package calls itself. A plugin named ``con``
would install on Linux and then be unstorable, unreadable and unremovable on a
Windows host, and a name that means different things on different machines is
not a name (spec section 7). A member named ``CON.json`` is worse: on Windows
the write goes to the console device, so the file never lands and an install
that reported it is describing something that is not there.
"""


def _reserved_device_segment(parts: tuple[str, ...] | list[str]) -> str | None:
    """The first path segment that names a device, or ``None``.

    The extension is not part of the name Windows resolves, and neither are
    trailing spaces or dots, so ``CON``, ``con.json`` and ``"con "`` are all the
    same device. Every segment is examined because a device name resolves in
    whatever directory it is written, not only at the top of a path.
    """
    for part in parts:
        if part.split(".")[0].strip(" .").lower() in _RESERVED_DEVICE_NAMES:
            return part
    return None


class PackageWords(BaseModel):
    """The words a *shared* refusal uses for the thing being installed.

    The checks below are the only copy of themselves: a voice pack is unpacked
    by the same :func:`_extract_safely` and refused by the same
    :func:`_check_member` as a plugin package, because a second copy of a
    security rule is a copy that drifts and the one nobody reads is the one that
    stops being true. But an operator installing a voice must not be told to
    "zip the plugin folder" — spec section 9 asks for plain English, and an
    error naming the wrong kind of thing is not plain (D2).

    So the noun is a parameter rather than a literal, and the caller supplies
    it. Everything here has the plugin installer's own wording as its default,
    so a plugin refusal reads exactly as it always did and only a caller that
    passes different words gets different words.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    noun: str = "plugin"
    """What one of these is, as it appears mid-sentence: "the *plugin*'s own
    folder"."""

    package: str = "plugin package"
    """What the whole upload is: "a *plugin package* holds only files and
    folders"."""

    oversize_advice: str = (
        "A plugin is a folder of code and a manifest; if yours needs more than "
        "this, it wants a model or a dataset that belongs outside the package."
    )
    entry_count_advice: str = (
        "A plugin is a folder of code and a manifest, not an archive of one."
    )
    unreadable_advice: str = "A plugin package is the plugin's folder, zipped."


PLUGIN_WORDS = PackageWords()
"""The plugin installer's own wording, and the default everywhere below."""


class PackageLimits(BaseModel):
    """Ceilings on what one upload may cost the machine that runs the house.

    A zip bomb is a denial-of-service against the assistant (ADR-0013), and the
    uncompressed total is the number that matters: 40KB of zip can be 4GB of
    disk. The count limit is separate because a million empty files costs
    inodes and syscalls rather than bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_archive_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    """The upload itself. Generous for a folder of scripts and a manifest."""

    max_uncompressed_bytes: int = Field(default=128 * 1024 * 1024, gt=0)
    """Enforced twice: against the sum the archive's headers declare, and again
    against the bytes actually written, because a header can lie."""

    max_entries: int = Field(default=2000, gt=0)


DEFAULT_PACKAGE_LIMITS = PackageLimits()


# ---------------------------------------------------------------------------
# Errors — every message is written to be shown to an operator verbatim
# ---------------------------------------------------------------------------


class PackageRejected(ValueError):
    """The package was refused. Never a traceback, always a sentence.

    Spec section 9: a failure names the problem and says what to do about it.
    Subclasses exist so the admin API can choose a status code without parsing
    text; nothing else should branch on the subclass.
    """


class PackageTooLarge(PackageRejected):
    """The archive, its uncompressed total, or its entry count is over a limit."""


class PackageUnsafe(PackageRejected):
    """A spec section 7 violation inside the archive: an entry that would land
    outside the staging directory, a symlink, or a member that is not a plain
    file or folder."""


class PackageInvalid(PackageRejected):
    """It unpacked, and what came out is not a plugin the core would load."""


class PackageConflict(PackageRejected):
    """A plugin of that name is already installed and ``replace`` was not
    chosen (ADR-0013)."""


class PackageNotInstalled(PackageRejected):
    """Asked to uninstall or toggle something that is not there."""


class PackageInstallFailed(RuntimeError):
    """The package was fine; the filesystem was not. Distinct from
    :class:`PackageRejected` because the operator did nothing wrong and the
    remedy is about the volume, not about the file they uploaded."""


class PluginStateError(RuntimeError):
    """``plugins-disabled.toml`` could not be read or written.

    Raised rather than swallowed: the file records that somebody switched a
    plugin *off*, and quietly treating an unreadable one as "nothing is
    disabled" would silently switch it back on.
    """


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class InstalledPackage(BaseModel):
    """What one successful install put on disk."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    transport: str
    directory: Path
    replaced: bool
    """True when this overwrote an installed plugin of the same name."""

    config_preserved: bool
    """True when the replaced plugin had a ``config.toml`` and it was carried
    over. An upgrade must not discard an operator's settings (spec section 7),
    so the *installed* config always wins over the one in the package."""

    files: int
    """How many files the installed folder holds, counted from the folder
    itself. Never the archive's tally: a package can carry members that are not
    installed, and a member that never became a file."""

    bytes_written: int


class UninstalledPackage(BaseModel):
    """What one successful uninstall removed."""

    model_config = ConfigDict(extra="forbid")

    name: str
    directory: Path
    config_removed: bool
    """The plugin's ``config.toml`` lives inside its folder (spec section 5.1:
    config is never central), so removing the folder always removes it. This
    says whether there was one to remove, because the operator has to be told
    what they are about to lose before they confirm (ADR-0013)."""

    files_removed: int


# ---------------------------------------------------------------------------
# Enable / disable — persisted in appdata
# ---------------------------------------------------------------------------


def disabled_state_path(layout: AppdataLayout) -> Path:
    """Where the switched-off list lives. One place knows, as with every other
    appdata path (``config/appdata.py``)."""
    return layout.config / DISABLED_STATE_FILENAME


def read_disabled_plugins(layout: AppdataLayout) -> set[str]:
    """Plugin names the operator has switched off.

    Absent file means nothing is disabled, which is the normal first-run state.
    A file that exists and cannot be parsed raises — see
    :class:`PluginStateError`.
    """
    path = disabled_state_path(layout)
    if not path.is_file():
        return set()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PluginStateError(
            f"The list of switched-off plugins at {path} could not be read: {exc}. "
            "Delete the file to switch every plugin back on."
        ) from exc
    raw = document.get(DISABLED_STATE_KEY, [])
    if not isinstance(raw, list):
        raise PluginStateError(
            f"The list of switched-off plugins at {path} is not a list of plugin "
            f"names. Delete the file to switch every plugin back on, or correct "
            f"the '{DISABLED_STATE_KEY}' entry."
        )
    return {value for value in raw if isinstance(value, str) and value}


def write_disabled_plugins(layout: AppdataLayout, names: set[str]) -> None:
    """Replace the switched-off list, atomically.

    Written through a temporary file and ``os.replace`` for the reason
    ``core.toml`` is: a half-written state file is a plugin whose on/off state
    is now a coin toss.
    """
    path = disabled_state_path(layout)
    temporary = path.with_name(path.name + ".new")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as handle:
            tomli_w.dump({DISABLED_STATE_KEY: sorted(names)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - the real failure is already raised
            pass
        raise PluginStateError(
            f"The list of switched-off plugins could not be saved to {path}: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and "
            "writable by the user the core runs as."
        ) from exc


def set_plugin_enabled(layout: AppdataLayout, name: str, *, enabled: bool) -> bool:
    """Record that ``name`` is on or off. Returns whether anything changed.

    Only the record. Stopping a running plugin is
    :meth:`personacore.plugins.host.PluginHost.set_enabled`'s job — this module
    never touches a process (ADR-0013 keeps installation inert).
    """
    require_plugin_name(name)
    current = read_disabled_plugins(layout)
    updated = current - {name} if enabled else current | {name}
    if updated == current:
        return False
    write_disabled_plugins(layout, updated)
    logger.info("plugin_state_changed", plugin=name, enabled=enabled)
    return True


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def require_plugin_name(name: str) -> str:
    """Refuse anything a manifest could not have declared.

    A plugin name is about to be joined onto the plugins directory, so it is
    checked against the manifest's own rule (spec section 7: everything from
    outside is untrusted) rather than sanitised into something plausible.
    """
    if not isinstance(name, str) or not _PLUGIN_NAME_RE.fullmatch(name):
        raise PackageRejected(
            f"{name!r} is not a plugin name. Plugin names are 2-64 characters of "
            "lowercase letters, digits and hyphens, starting with a letter."
        )
    return name


def install_package(
    layout: AppdataLayout,
    package: bytes | bytearray | memoryview | str | Path,
    *,
    replace: bool = False,
    limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
    secrets: SecretStore | None = None,
) -> InstalledPackage:
    """Install one uploaded plugin package. ADR-0013's whole flow, in order.

    1. Stage the upload inside appdata, never a shared temp location.
    2. Extract it there, refusing every hostile member (:func:`_check_member`).
    3. Validate what came out with the real
       :class:`~personacore.plugins.discovery.PluginDiscovery` — **before**
       anything is moved into the plugins directory.
    4. Move the validated folder into place, keeping the installed
       ``config.toml`` if this is a replacement.

    Nothing from the package is run at any step. The caller reloads the
    supervisor afterwards; this function never starts a process.

    Args:
        layout: Appdata paths. The only place this module learns where the
            plugins directory is.
        package: The uploaded bytes, or a path to a zip already on disk.
        replace: Overwrite an installed plugin of the same name. Without it a
            collision is refused, because silently replacing a working plugin
            with an upload is not a decision the core gets to make.
        limits: Zip-bomb ceilings.
        secrets: The secret store, so an orphaned namespace can be cleared
            before a package of that name is installed into it (ADR-0025; see
            :func:`_clear_orphaned_secrets`). Defaults to the store for
            ``layout``; passed only by a caller that already holds one.

    Raises:
        PackageRejected: With a sentence naming the problem. Nothing has been
            installed and the staging directory is gone.
        PackageInstallFailed: The volume could not be written.
    """
    plugins_root = layout.plugins
    try:
        plugins_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PackageInstallFailed(
            f"The plugins directory {plugins_root} could not be created: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and writable."
        ) from exc

    staging = _make_staging(plugins_root)
    try:
        archive = _stage_archive(staging, package, limits)
        extract_root = staging / "extract"
        extract_root.mkdir()
        extraction = _extract_safely(archive, extract_root, limits)
        candidate, wrapped = _find_plugin_directory(extract_root)
        record = _validate_staged(staging, candidate, wrapped=wrapped)
        return _move_into_place(
            layout,
            staging,
            record.directory,
            name=record.name,
            version=record.manifest.plugin.version,
            transport=record.manifest.plugin.transport,
            replace=replace,
            extraction=extraction,
            secrets=secrets,
        )
    finally:
        # Every path, including failure: a staging directory left behind is an
        # unvalidated archive sitting inside appdata, which is the one place
        # this module exists to keep clean.
        shutil.rmtree(staging, ignore_errors=True)


def uninstall_package(layout: AppdataLayout, name: str) -> UninstalledPackage:
    """Remove an installed plugin's folder, and everything in it.

    Refuses to delete anything that is not a direct child of one of the two
    plugin directories: the name is resolved, containment inside appdata is
    checked with :meth:`AppdataLayout.require_inside`, and the resolved parent
    must be the plugins directory itself. A plugin folder that is a symlink is
    refused outright rather than followed — deleting through it would delete
    somebody else's files (spec section 7).

    The plugin's ``config.toml`` lives inside the folder, so it always goes with
    it; :attr:`UninstalledPackage.config_removed` says whether there was one, so
    the UI can tell the operator what they are losing before they confirm.
    """
    require_plugin_name(name)
    for root in (layout.plugins, layout.plugins_http):
        directory = root / name
        if directory.exists():
            break
    else:
        raise PackageNotInstalled(
            f"No plugin named {name!r} is installed, so there is nothing to remove."
        )

    if directory.is_symlink():
        raise PackageUnsafe(
            f"The folder for {name!r} is a symbolic link rather than a real folder. "
            "Removing it would delete whatever it points at, so the core refuses. "
            "Remove the link by hand if that is really what you want."
        )
    try:
        resolved = layout.require_inside(directory, what=f"The folder for plugin {name!r}")
    except AppdataError as exc:
        raise PackageUnsafe(str(exc)) from exc
    if resolved.parent != root.resolve():
        raise PackageUnsafe(
            f"The folder for {name!r} resolves to {resolved.as_posix()!r}, which is "
            f"not directly inside {root.as_posix()!r}. The core only removes plugin "
            "folders it put there."
        )

    config_removed = (resolved / CONFIG_FILENAME).is_file()
    files_removed = sum(1 for path in resolved.rglob("*") if path.is_file())
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        raise PackageInstallFailed(
            f"The plugin folder {resolved} could not be removed: {exc.strerror or exc}. "
            "Check the appdata volume is writable by the user the core runs as."
        ) from exc

    # A name that is no longer installed must not stay on the switched-off list:
    # a later install of the same name would arrive already off, with nothing on
    # screen explaining why.
    try:
        set_plugin_enabled(layout, name, enabled=True)
    except PluginStateError as exc:  # pragma: no cover - the folder is already gone
        logger.error("plugin_state_cleanup_failed", plugin=name, error=str(exc))

    logger.info("plugin_uninstalled", plugin=name, files=files_removed)
    return UninstalledPackage(
        name=name,
        directory=resolved,
        config_removed=config_removed,
        files_removed=files_removed,
    )


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def _make_staging(plugins_root: Path) -> Path:
    """A fresh, unguessable staging directory inside appdata (ADR-0013)."""
    staging = plugins_root / STAGING_DIRNAME / token_hex(8)
    try:
        staging.mkdir(parents=True)
    except OSError as exc:
        raise PackageInstallFailed(
            f"A staging directory could not be created under {plugins_root}: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and writable."
        ) from exc
    return staging


def _stage_archive(
    staging: Path,
    package: bytes | bytearray | memoryview | str | Path,
    limits: PackageLimits,
    words: PackageWords = PLUGIN_WORDS,
) -> Path:
    """Put the upload on disk in the staging directory, size-checked first."""
    destination = staging / "upload.zip"
    if isinstance(package, str | Path):
        origin = Path(package)
        try:
            size = origin.stat().st_size
        except OSError as exc:
            raise PackageInvalid(
                f"The package file {origin} could not be read: {exc.strerror or exc}."
            ) from exc
        _require_archive_size(size, limits, words)
        try:
            shutil.copyfile(origin, destination)
        except OSError as exc:
            raise PackageInstallFailed(
                f"The package could not be copied into the staging directory: "
                f"{exc.strerror or exc}."
            ) from exc
        return destination

    data = bytes(package)
    _require_archive_size(len(data), limits, words)
    if not data:
        raise PackageInvalid(
            f"The upload was empty. Choose the .zip file containing the {words.noun} "
            "folder and try again."
        )
    try:
        destination.write_bytes(data)
    except OSError as exc:
        raise PackageInstallFailed(
            f"The upload could not be written to the staging directory: "
            f"{exc.strerror or exc}."
        ) from exc
    return destination


def _require_archive_size(
    size: int, limits: PackageLimits, words: PackageWords = PLUGIN_WORDS
) -> None:
    if size > limits.max_archive_bytes:
        raise PackageTooLarge(
            f"The package is {_human_bytes(size)}, which is over the "
            f"{_human_bytes(limits.max_archive_bytes)} limit for a {words.package}. "
            f"{words.oversize_advice}"
        )


# ---------------------------------------------------------------------------
# Extraction — the refusals (ADR-0013, "Refusing hostile archives")
# ---------------------------------------------------------------------------


class _Extraction(BaseModel):
    """How much came out, for the audit record.

    Deliberately **no file count**. A tally kept as the loop goes round counts
    members, not files, and the two differ exactly when it matters: a member
    named for a Windows device is written to the device and never becomes a
    file, two members of one name are one file, and the archive can carry
    members that are never installed at all. An install that says it wrote two
    files into a folder holding one is how an operator concludes a package
    installed correctly when it did not (D1), so the number each installer
    reports is counted from what is on disk — see :func:`_count_files` — at the
    point where it knows which folder it actually installed.
    """

    model_config = ConfigDict(extra="forbid")

    bytes_written: int


def _count_files(directory: Path) -> int:
    """How many files are really under ``directory``.

    Never raises: it is called on the way out of a step that has already
    succeeded, and a count that could turn a completed install into a traceback
    would be a worse lie than the one it exists to prevent. ``os.walk`` skips
    what it cannot read rather than raising, and does not descend through
    symlinks — of which there are none, because they are refused on the way in.
    """
    return sum(len(names) for _root, _dirs, names in os.walk(directory))


def _extract_safely(
    archive: Path,
    destination: Path,
    limits: PackageLimits,
    words: PackageWords = PLUGIN_WORDS,
) -> _Extraction:
    """Unpack every member, having refused every member worth refusing.

    ``ZipFile.extractall`` is deliberately not used. It sanitises names quietly
    rather than refusing them, which turns "this archive tried to write to
    ``/etc``" into "this archive installed fine" — and a refusal is the only
    outcome that tells the operator their package is hostile or broken.
    """
    try:
        with zipfile.ZipFile(archive) as zipped:
            members = zipped.infolist()
            if len(members) > limits.max_entries:
                raise PackageTooLarge(
                    f"The package holds {len(members)} entries, over the "
                    f"{limits.max_entries} allowed. {words.entry_count_advice}"
                )
            declared = sum(member.file_size for member in members)
            if declared > limits.max_uncompressed_bytes:
                raise PackageTooLarge(
                    f"The package says it unpacks to {_human_bytes(declared)}, over "
                    f"the {_human_bytes(limits.max_uncompressed_bytes)} limit."
                )

            root = Path(os.path.normpath(destination))
            for member in members:
                _check_member(member, root, words)

            written = 0
            for member in members:
                target = _target_of(member, root)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member) as source, target.open("wb") as handle:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limits.max_uncompressed_bytes:
                            # Belt on top of the header check above: a size
                            # header is a claim, and this is the only number
                            # that is a measurement. Unreachable while CPython
                            # also stops a member's read at its declared size —
                            # which is exactly why it must not be the only
                            # defence.
                            raise PackageTooLarge(
                                "The package unpacks to more than "
                                f"{_human_bytes(limits.max_uncompressed_bytes)}, though "
                                "its own size headers said otherwise. Nothing was "
                                "installed."
                            )
                        handle.write(chunk)
    except zipfile.BadZipFile as exc:
        raise PackageInvalid(
            f"That file is not a readable zip archive ({exc}). "
            f"{words.unreadable_advice}"
        ) from exc
    except OSError as exc:
        raise PackageInstallFailed(
            f"The package could not be unpacked: {exc.strerror or exc}. Check there "
            "is free space on the appdata volume."
        ) from exc
    return _Extraction(bytes_written=written)


def _member_parts(member: zipfile.ZipInfo) -> list[str]:
    """A member's name as path segments, in both separators' spellings."""
    return [part for part in PurePosixPath(member.filename.replace("\\", "/")).parts if part]


def _target_of(member: zipfile.ZipInfo, root: Path) -> Path:
    """Where a member lands, built from its parts rather than by joining the
    raw name — so a name the platform would read as drive-relative or rooted
    cannot re-anchor the join."""
    return root.joinpath(*_member_parts(member))


def _check_member(
    member: zipfile.ZipInfo, root: Path, words: PackageWords = PLUGIN_WORDS
) -> None:
    """Refuse one hostile archive member. ADR-0013's list, in order.

    Every branch here is a refusal because the alternative — sanitising the
    name and carrying on — installs the package that just tried to write
    outside its own folder.
    """
    name = member.filename
    if not name or "\x00" in name:
        raise PackageUnsafe(
            "The package contains an entry with no usable name. Nothing was "
            "installed."
        )

    # Only the file-type bits decide, and only when the archive set them.
    # Plenty of honest zips carry permission bits with no type bits at all
    # (``ZipFile.writestr`` stores ``0o600``, and archives written on Windows
    # store FAT attributes instead), so "no type recorded" has to mean "a plain
    # file" or every ordinary package would be refused.
    file_type = stat.S_IFMT(member.external_attr >> 16)
    if file_type == stat.S_IFLNK:
        raise PackageUnsafe(
            f"The package contains a symbolic link ({name!r}). Links are refused. "
            f"Zip the {words.noun} folder with links resolved, or without them."
        )
    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise PackageUnsafe(
            f"The package entry {name!r} is not a plain file or folder. A "
            f"{words.package} holds only files and folders."
        )

    if _is_absolute_path_string(name):
        raise PackageUnsafe(
            f"The package entry {name!r} is an absolute path. Every entry must be "
            f"relative to the {words.noun}'s own folder; nothing was installed."
        )
    if _has_raw_drive_prefix(name):
        raise PackageUnsafe(
            f"The package entry {name!r} carries a drive letter, which makes it an "
            f"absolute path on Windows. Every entry must be relative to the "
            f"{words.noun}'s own folder; nothing was installed."
        )
    if _has_raw_traversal_segment(name):
        raise PackageUnsafe(
            f"The package entry {name!r} contains a '..' segment, which points out "
            "of the folder it is being unpacked into. Nothing was installed."
        )

    # Refused by name, on every platform, before a byte is written. On Windows
    # such a member is written to the device rather than to a file, so the
    # install both loses the file and reports it (D1); on Linux it is an
    # ordinary name, and an archive built to attack a Windows host must be
    # refused wherever it is uploaded — the same reasoning as the drive-letter
    # check above.
    reserved = _reserved_device_segment(_member_parts(member))
    if reserved is not None:
        named = (
            f"The package entry {name!r} is a name Windows"
            if reserved == name
            else f"The package entry {name!r} contains {reserved!r}, a name Windows"
        )
        raise PackageUnsafe(
            f"{named} reserves for a device rather than for a file or folder. "
            f"Rename it inside the {words.noun} and zip it again; nothing was "
            "installed."
        )

    target = Path(os.path.normpath(_target_of(member, root)))
    if target != root and root not in target.parents:
        raise PackageUnsafe(
            f"The package entry {name!r} would be written outside the folder it is "
            "being unpacked into. Nothing was installed."
        )


# ---------------------------------------------------------------------------
# Validation — reusing discovery, not re-implementing it
# ---------------------------------------------------------------------------


def _find_plugin_directory(extract_root: Path) -> tuple[Path, bool]:
    """The one plugin folder in the archive, and whether it was wrapped.

    ADR-0013: "a zip archive containing one plugin directory, with the manifest
    at its root or one level down". Both shapes are what people actually
    produce — "zip this folder" and "zip the contents of this folder" — and
    refusing either would be a rule nobody can remember.
    """
    if (extract_root / MANIFEST_FILENAME).is_file():
        return extract_root, False

    folders = [
        child
        for child in sorted(extract_root.iterdir())
        if child.is_dir() and child.name not in _IGNORED_TOP_LEVEL
    ]
    with_manifest = [child for child in folders if (child / MANIFEST_FILENAME).is_file()]
    if len(with_manifest) == 1:
        return with_manifest[0], True
    if not with_manifest:
        raise PackageInvalid(
            f"The package has no {MANIFEST_FILENAME}. A plugin package is the "
            "plugin's own folder, zipped — the manifest belongs either at the top "
            "of the archive or in the single folder inside it."
        )
    names = ", ".join(repr(child.name) for child in with_manifest)
    raise PackageInvalid(
        f"The package contains more than one plugin folder ({names}). Install them "
        "one at a time — a package is one plugin."
    )


def _peek_declared(directory: Path) -> tuple[str | None, Transport]:
    """The manifest's name and transport, read only to decide where to stage.

    Deliberately not validation: it answers "which of the two plugin
    directories does this belong in" and nothing else. Anything it cannot
    answer falls back to stdio, and the real scan below produces the error the
    operator sees.
    """
    try:
        raw = tomllib.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, Transport.STDIO
    section = raw.get("plugin")
    if not isinstance(section, dict):
        return None, Transport.STDIO
    name = section.get("name")
    transport = Transport.HTTP if section.get("transport") == "http" else Transport.STDIO
    return (name if isinstance(name, str) else None), transport


STAGING_FALLBACK_NAME = "package"
"""The folder name the core stages an upload under when the package has not
given it a usable one.

A constant chosen here, never a string out of the archive — see
:func:`_staging_folder_name`. It is only ever reached by a package whose
manifest is already invalid, and the scan that follows reports *that*, which is
the problem the operator actually has."""

def _staging_folder_name(raw: str | None) -> str:
    """The folder name a staged package is checked under — **chosen by the
    core**, never taken on trust from the package.

    ``raw`` is untrusted in the fullest sense: it is either ``plugin.name`` as
    :func:`_peek_declared` read it out of a manifest nothing has validated yet,
    or the name of a folder inside the archive. It is about to be joined onto
    the staging directory, and ``Path.__truediv__`` does not *join* an absolute
    path — it replaces everything to its left with it. A manifest declaring
    ``name = "/etc/cron.d"`` therefore moved the whole extracted tree there, as
    the user the core runs as, before one check had run: a write anywhere on the
    host, and code execution at the next start if it was aimed at an importable
    directory.

    So nothing path-shaped is repaired into something plausible here; it is
    refused. The only strings that survive to be joined are those matching the
    manifest's own name rule — 2-64 characters of lowercase letters, digits and
    hyphens — which cannot express a separator, a drive prefix, a ``..`` or a
    leading dot however they are read. Anything that is merely *not a plugin
    name* falls back to :data:`STAGING_FALLBACK_NAME`, because such a package's
    manifest is invalid and the scan below says so far better than a second
    opinion here would.
    """
    if raw is None:
        return STAGING_FALLBACK_NAME
    if (
        "/" in raw
        or "\\" in raw
        or "\x00" in raw
        or _is_absolute_path_string(raw)
        or _has_raw_drive_prefix(raw)
        or _has_raw_traversal_segment(raw)
        or raw in (".", "..")
        or raw != raw.strip(" .")
    ):
        raise PackageUnsafe(
            f"The package calls itself {raw!r}, which is a path and not a plugin "
            "name. A plugin name is 2-64 characters of lowercase letters, digits "
            "and hyphens; it never contains a folder separator, a drive letter or "
            "a '..' segment. Nothing was installed."
        )
    if _reserved_device_segment([raw]) is not None:
        raise PackageInvalid(
            f"The package calls itself {raw!r}, which Windows reserves for a "
            "device rather than a folder. Rename the plugin and repackage it; "
            "nothing was installed."
        )
    if not _PLUGIN_NAME_RE.fullmatch(raw):
        return STAGING_FALLBACK_NAME
    return raw


def _validate_staged(staging: Path, candidate: Path, *, wrapped: bool) -> PluginRecord:
    """Run the real scanner over the staged folder, and accept only what it
    would load (ADR-0013: "validates the manifest before anything is moved").

    The folder is arranged into a miniature appdata layout inside staging so
    :class:`~personacore.plugins.discovery.PluginDiscovery` can be pointed at
    it unchanged. That is the point: manifest schema, the folder-name rule, the
    contract-version rule and every spec section 7 path check are the ones the
    core already enforces at load time, not a second copy of them that could
    drift.

    **The destination of that arranging move is built by the core**, from
    :func:`_staging_folder_name`. Both names that could otherwise reach it come
    out of the upload — ``plugin.name`` from an unvalidated manifest when the
    archive is not wrapped in a folder, and the folder's own name when it is —
    and neither is allowed to influence a path before it has been proved to be a
    name rather than a path.
    """
    declared_name, transport = _peek_declared(candidate)
    raw_name = candidate.name if wrapped else declared_name
    folder_name = _staging_folder_name(raw_name)

    if (raw_name or "").startswith(("_", ".")):
        raise PackageInvalid(
            f"The folder inside the package is named {raw_name!r}. A leading "
            "underscore or dot marks a folder the core ignores, so it would "
            "never load. Rename it to the plugin's own name."
        )

    check_root = staging / "check"
    transport_dirname = HTTP_DIRNAME if transport is Transport.HTTP else STDIO_DIRNAME
    transport_root = check_root / transport_dirname
    staged = transport_root / folder_name
    if Path(os.path.normpath(staged)).parent != Path(os.path.normpath(transport_root)):
        # Unreachable while `_staging_folder_name` refuses everything path-shaped
        # above, and kept because the guarantee this function makes is about the
        # destination, not about a name: if the two ever disagree the move must
        # not happen.
        raise PackageUnsafe(  # pragma: no cover - refused above
            f"The package calls itself {raw_name!r}, which does not name a folder "
            "inside the staging directory. Nothing was installed."
        )
    try:
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate), str(staged))
    except OSError as exc:
        raise PackageInstallFailed(
            f"The package could not be prepared for checking: {exc.strerror or exc}."
        ) from exc

    result = PluginDiscovery(check_root).scan()
    if result.failures:
        raise PackageInvalid(
            "The package was refused: " + _without_staging(result.failures[0].message, check_root)
        )
    if not result.plugins:  # pragma: no cover - unreachable while a manifest exists
        raise PackageInvalid(
            "The package does not contain a plugin folder the core can load."
        )
    return result.plugins[0]


def _without_staging(message: str, check_root: Path) -> str:
    """Strip the staging path out of a discovery message.

    The message is shown to an operator, and a path inside a directory that no
    longer exists by the time they read it is worse than no path at all. What
    is left — ``plugins/kitchen-timer/manifest.toml`` — is where the file is
    inside their package.
    """
    return message.replace(check_root.as_posix() + "/", "").replace(
        str(check_root) + os.sep, ""
    )


# ---------------------------------------------------------------------------
# Moving into place
# ---------------------------------------------------------------------------


def _clear_orphaned_secrets(
    layout: AppdataLayout, name: str, store: SecretStore | None
) -> None:
    """Empty the secret namespace of a plugin that is **not installed**, before
    a package claiming that name is put there.

    A namespace is keyed on a name a manifest chose, and only the admin
    uninstall route clears one. So a namespace can outlive the plugin it
    belonged to — a folder removed over a shell, a volume restored from a
    backup, an uninstall that half-finished — and the next package to declare
    that name inherits credentials it was never given.

    **The core cannot tell a genuine reinstall from a stranger's zip claiming
    the name.** Nothing in an archive binds it to the plugin that used to be
    there: there is no signature, and the manifest is the author's own words.
    That is exactly why the install review deliberately says nothing about
    stored state, and it is why this is not a judgement call: when the two cases
    cannot be told apart, the credentials are not handed over.

    Silent by design. Nothing about whether a namespace existed reaches the
    caller — no message, no field on :class:`InstalledPackage`, no difference in
    the sentence the operator is shown — because an installer able to learn
    that has the enumeration oracle ADR-0025 section 1 exists to remove. A
    server-side log line records it, since destroying a credential with no trace
    at all is its own kind of wrong.

    A failure to clear refuses the install. The alternative is installing the
    package anyway *and* leaving it the credentials, which is the vulnerability.
    """
    store = store or SecretStore(layout)
    try:
        cleared = store.delete_namespace(name)
    except (SecretError, AppdataError, OSError) as exc:
        raise PackageInstallFailed(
            f"The secret store could not be prepared for installing {name!r}: "
            f"{exc}. Nothing was installed — a package is never handed credentials "
            "the core could not first account for."
        ) from exc
    if cleared:
        logger.info("plugin_orphaned_secrets_cleared", plugin=name, secrets=cleared)


def _move_into_place(
    layout: AppdataLayout,
    staging: Path,
    staged_directory: Path,
    *,
    name: str,
    version: str,
    transport: Transport,
    replace: bool,
    extraction: _Extraction,
    secrets: SecretStore | None = None,
) -> InstalledPackage:
    """The last step: collision rules, config preservation, then one move."""
    # `name` is the validated manifest's, so it already matches the manifest's
    # own rule. Checked again here anyway, because this is the line that joins a
    # name onto the plugins directory and the guarantee belongs to the join, not
    # to the confidence of whoever calls it.
    require_plugin_name(name)
    root = layout.plugins_http if transport is Transport.HTTP else layout.plugins
    other = layout.plugins if transport is Transport.HTTP else layout.plugins_http
    target = root / name

    if (other / name).exists():
        # Discovery refuses a name declared in both directories and loads
        # neither, so installing into this state would break the plugin that
        # already works. `replace` cannot help: it is a different folder.
        raise PackageConflict(
            f"A plugin named {name!r} is already installed under "
            f"{other.name}/, with a different transport. Remove that one first — "
            "two plugins may not share a name."
        )

    try:
        layout.require_inside(target, what=f"The folder for plugin {name!r}")
    except AppdataError as exc:  # pragma: no cover - the name is already pattern-checked
        raise PackageUnsafe(str(exc)) from exc

    existing = target.exists()
    if existing and not replace:
        raise PackageConflict(
            f"A plugin named {name!r} is already installed. Choose 'replace the "
            "installed plugin' to upgrade it — its config.toml settings are kept — "
            "or uninstall it first."
        )

    if not existing:
        # Nothing of that name is installed, so any namespace under it is
        # orphaned and this package does not inherit it (ADR-0025). Before the
        # move, so a store that cannot be cleared refuses the install rather
        # than completing one that hands over somebody else's credentials.
        _clear_orphaned_secrets(layout, name, secrets)

    config_preserved = False
    if existing:
        installed_config = target / CONFIG_FILENAME
        if installed_config.is_file():
            # Spec section 7: an upgrade must never discard appdata content. The
            # settings an operator typed outrank whatever the package ships.
            try:
                shutil.copyfile(installed_config, staged_directory / CONFIG_FILENAME)
            except OSError as exc:
                raise PackageInstallFailed(
                    f"The installed settings for {name!r} could not be carried over: "
                    f"{exc.strerror or exc}. Nothing was replaced."
                ) from exc
            config_preserved = True

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PackageInstallFailed(
            f"The plugins directory {root} could not be created: "
            f"{exc.strerror or exc}. Nothing was changed."
        ) from exc

    # Set the installed plugin aside *before* anything is moved on top of it,
    # into a sibling folder that outlives this function (see
    # `REPLACED_DIRNAME_PREFIX`). Until the new folder is in place, the working
    # plugin exists somewhere on disk with every one of its files — there is no
    # window in which a failure, a crash or a full volume leaves the operator
    # with neither.
    backup: Path | None = None
    if existing:
        backup = root / f"{REPLACED_DIRNAME_PREFIX}{name}-{token_hex(4)}"
        try:
            shutil.move(str(target), str(backup))
        except OSError as exc:
            raise PackageInstallFailed(
                f"The installed plugin {name!r} could not be set aside before "
                f"replacing it: {exc.strerror or exc}. Nothing was changed and the "
                "plugin you have is untouched."
            ) from exc

    try:
        shutil.move(str(staged_directory), str(target))
    except BaseException as exc:
        # Every exception, not only `OSError`: whatever went wrong, a failed
        # replacement must never also be an uninstall. `except OSError` was the
        # whole guard before, so anything else skipped the rollback and the
        # staging sweep took the operator's plugin with it.
        if backup is not None and not _restore_replaced(backup, target):
            # Logged as well as raised: an interrupt is re-raised untouched
            # below, and where the plugin went has to survive that.
            logger.error("plugin_replace_left_a_copy", plugin=name, copy=str(backup))
            if isinstance(exc, Exception):
                raise PackageInstallFailed(
                    f"The plugin {name!r} could not be moved into {root} "
                    f"({getattr(exc, 'strerror', None) or exc}), and the copy that "
                    f"was already installed could not be put back automatically. It "
                    f"is safe and complete in {backup} — rename that folder to "
                    f"{name!r} to have it back, then reload."
                ) from exc
            raise
        if isinstance(exc, OSError):
            raise PackageInstallFailed(
                f"The plugin {name!r} could not be moved into {root}: "
                f"{exc.strerror or exc}. Nothing was changed."
            ) from exc
        raise

    if backup is not None:
        # The replacement is in place, so the copy set aside has done its job.
        # `ignore_errors` because a backup that will not delete is untidy and
        # inert, and turning it into a failed install would be a lie about an
        # install that succeeded.
        shutil.rmtree(backup, ignore_errors=True)

    if not existing:
        # Clear any stale switched-off entry, so a fresh install is not born off
        # because something of that name was disabled months ago.
        try:
            set_plugin_enabled(layout, name, enabled=True)
        except PluginStateError as exc:
            logger.error("plugin_state_cleanup_failed", plugin=name, error=str(exc))

    # The folder is on disk now, so the number the operator and the audit record
    # are given is counted from it rather than from the archive (D1). They differ
    # whenever the package carried anything that was not installed — packaging
    # debris beside the plugin folder, two members of one name — and a count
    # that claims a file the folder does not hold is how somebody concludes an
    # install went further than it did.
    files = _count_files(target)
    logger.info(
        "plugin_package_installed",
        plugin=name,
        version=version,
        transport=transport.value,
        replaced=existing,
        config_preserved=config_preserved,
        files=files,
    )
    return InstalledPackage(
        name=name,
        version=version,
        transport=transport.value,
        directory=target,
        replaced=existing,
        config_preserved=config_preserved,
        files=files,
        bytes_written=extraction.bytes_written,
    )


def _restore_replaced(backup: Path, target: Path) -> bool:
    """Put a plugin that was set aside back where it was. True if it is back.

    Anything sitting at ``target`` is a fragment of the package that just failed
    to move — the installed folder is in ``backup`` and the operator's
    ``config.toml`` was copied into the staged folder before any of this — so
    clearing it is not data loss, and leaving it would block the restore.

    Never raises. It runs while another exception is on its way out, and a
    failure here has to become a *sentence naming where the plugin is* rather
    than a second traceback that buries the first.
    """
    try:
        if target.exists() or target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        if target.exists() or target.is_symlink():
            logger.error("plugin_replace_rollback_blocked", target=str(target))
            return False
        shutil.move(str(backup), str(target))
    except OSError as exc:
        logger.error("plugin_replace_rollback_failed", backup=str(backup), error=str(exc))
        return False
    return True


def _human_bytes(count: int) -> str:
    """Bytes as something an operator can read in a refusal."""
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - the loop always returns


__all__ = [
    "DEFAULT_PACKAGE_LIMITS",
    "DISABLED_STATE_FILENAME",
    "PLUGIN_NAME_PATTERN",
    "REPLACED_DIRNAME_PREFIX",
    "STAGING_DIRNAME",
    "STAGING_FALLBACK_NAME",
    "InstalledPackage",
    "PackageConflict",
    "PackageInstallFailed",
    "PackageInvalid",
    "PackageLimits",
    "PackageNotInstalled",
    "PackageRejected",
    "PackageTooLarge",
    "PackageUnsafe",
    "PackageWords",
    "PLUGIN_WORDS",
    "PluginStateError",
    "UninstalledPackage",
    "disabled_state_path",
    "install_package",
    "read_disabled_plugins",
    "require_plugin_name",
    "set_plugin_enabled",
    "uninstall_package",
    "write_disabled_plugins",
]
