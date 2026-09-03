"""Reading a plugin package's manifest without installing it (ADR-0013, §7, §9).

The manifest declares what a plugin asks for, but a real operator installing a
stranger's zip is not going to read the manifest file before installing it. A
declaration nobody is shown is not a disclosure, it is a document nobody
opens — so the admin UI shows it, before the archive is unpacked into the
plugins directory, and this module is where the archive is read to produce it.

**Nothing here writes to disk and nothing here runs.** The zip is opened from
the bytes in memory, the archive's own entry list is checked, and exactly one
member is decompressed: ``manifest.toml``. It is parsed as TOML and validated
against :class:`~personacore.contracts.manifest.PluginManifest`. No other member
is read, opened, imported or executed, and no member is written anywhere. If the
operator cancels, there is nothing to clean up because nothing was created.

**The caps are the installer's, not a second set.** ``_require_archive_size``,
the entry-count limit and the uncompressed-total limit come from
:mod:`personacore.plugins.packages` with the same limits object and the same
refusal sentences, and every entry goes through that module's ``_check_member``
against a notional root. The names are private to that module and used here for
the reason it uses ``discovery``'s: two ideas of "an entry this core refuses" is
one too many, and a package that inspects clean and then fails the same check at
install would be this surface telling the operator something the installer
disagrees with.

**What comes out is a declaration, not a permission set.** The core does not
fence a stdio plugin's network or filesystem access (ADR-0012), so
:class:`PackageDisclosure` deliberately keeps the author's declared hosts and
paths in fields named for what they are. What the core *does* hold a plugin to
is the secret names it is handed and the risk level of each tool; the admin UI
is where that difference is said in words, and it must stay said.
"""

from __future__ import annotations

import io
import os
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError

from personacore.contracts.manifest import (
    PluginManifest,
    RiskLevel,
    SecretRequest,
    ServiceKind,
    Transport,
)
from personacore.plugins.discovery import MANIFEST_FILENAME
from personacore.plugins.errors import describe_validation_error
from personacore.plugins.packages import (
    _IGNORED_TOP_LEVEL,
    DEFAULT_PACKAGE_LIMITS,
    PackageInvalid,
    PackageLimits,
    PackageTooLarge,
    _check_member,
    _human_bytes,
    _require_archive_size,
)

MAX_MANIFEST_BYTES = 256 * 1024
"""The only member this module decompresses, and how far it will go.

A manifest is a few dozen lines of TOML. A member *named* ``manifest.toml`` that
decompresses to a gigabyte is a zip bomb aimed at the one file this module opens,
and the archive-wide total is not enough on its own: this is the single member
read before that total has been measured against anything real.
"""

INSPECTION_ROOT = Path(os.path.normpath(os.sep + "personacore-package-inspection"))
"""A directory that does not exist and is never created.

``_check_member`` decides whether an entry would land outside the folder it is
being unpacked into, which needs a folder to be outside *of*. Nothing is
unpacked here, so the answer is computed against a name rather than a place.
"""


class DeclaredTool(BaseModel):
    """One tool the package's manifest exposes, as the operator will read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    risk: RiskLevel
    description: str = ""

    @property
    def acts(self) -> bool:
        """Whether this is one of the tools that *does* something.

        ``safe`` runs silently; ``confirm`` and ``restricted`` are the levels
        that exist because the tool can affect the world. The disclosure makes
        those stand out, and this is the one place that decides which they are.
        """
        return self.risk is not RiskLevel.SAFE


class PackageDisclosure(BaseModel):
    """What an uploaded package says about itself, before it is installed.

    Every field is the author's own declaration. Two of them the core enforces
    — ``secrets`` (the plugin is handed those and no others) and each tool's
    ``risk`` (the core holds a tool to the level declared here) — and the rest
    are description. The field names say which is which; the admin UI says it
    in sentences.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    contract: str
    description: str = ""
    transport: Transport
    entry: str | None = None
    url: str | None = None

    provides: tuple[ServiceKind, ...] = ()
    """What kind of service the package registers as being (contract 2.1).

    Usually empty, and empty is the ordinary case — a plugin that only offers
    tools declares nothing here. When it is not empty it belongs beside the
    name rather than down with the tools: "this is the voice the assistant
    speaks in" is a different sort of fact from "this is a tool the assistant
    can call", and it is one an operator is agreeing to.
    """

    tools: tuple[DeclaredTool, ...] = ()
    declared_hosts: tuple[str, ...] = ()
    """Hostnames the author says the plugin talks to. Not enforced for stdio
    plugins (ADR-0012)."""

    declared_paths: tuple[str, ...] = ()
    """Filesystem paths the author says the plugin needs beyond its own folder.
    Not enforced (ADR-0012)."""

    secrets: tuple[str, ...] = ()
    """Secret names the plugin will be handed. **Enforced** — it receives these
    and nothing else from the store."""

    secret_requests: tuple[SecretRequest, ...] = ()
    """The same requests with the author's words and their ``required`` flag
    (ADR-0026).

    Carried alongside the bare names rather than replacing them: the review
    screen prints the names as a row of tags *and* draws a described box per
    request, and the two want different shapes. The description is the author's
    own text and is rendered **escaped, never as markup** — this is a stranger's
    zip talking to a page the operator trusts.
    """

    folder: str
    """The plugin folder's name inside the archive, or ``""`` when the manifest
    sits at the top of the zip."""

    @property
    def acting_tools(self) -> tuple[DeclaredTool, ...]:
        return tuple(tool for tool in self.tools if tool.acts)


def inspect_package(
    package: bytes, *, limits: PackageLimits = DEFAULT_PACKAGE_LIMITS
) -> PackageDisclosure:
    """Read the manifest out of an uploaded zip, touching nothing else.

    Args:
        package: The uploaded bytes, exactly as they arrived.
        limits: The same ceilings the installer will apply. Passed rather than
            defaulted by callers that have them, so the archive an operator is
            shown is one the installer would also accept.

    Returns:
        The manifest's own declarations, ready to render.

    Raises:
        PackageRejected: Every refusal is one of these and every message is a
            sentence written for an operator (spec section 9) — the same
            sentence the installer would have given, wherever the check is
            shared with it.
    """
    _require_archive_size(len(package), limits)
    if not package:
        raise PackageInvalid(
            "The upload was empty. Choose the .zip file containing the plugin folder "
            "and try again."
        )

    try:
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            members = archive.infolist()
            _require_shape(members, limits)
            for member in members:
                _check_member(member, INSPECTION_ROOT)
            member, folder = _find_manifest(members)
            raw = _read_manifest(archive, member)
    except zipfile.BadZipFile as exc:
        raise PackageInvalid(
            f"That file is not a readable zip archive ({exc}). A plugin package is "
            "the plugin's folder, zipped."
        ) from exc

    manifest = _parse_manifest(raw, folder)
    return _disclose(manifest, folder)


def _require_shape(members: list[zipfile.ZipInfo], limits: PackageLimits) -> None:
    """The entry-count and uncompressed-total caps, in the installer's words."""
    if len(members) > limits.max_entries:
        raise PackageTooLarge(
            f"The package holds {len(members)} entries, over the "
            f"{limits.max_entries} allowed. A plugin is a folder of code and "
            "a manifest, not an archive of one."
        )
    declared = sum(member.file_size for member in members)
    if declared > limits.max_uncompressed_bytes:
        raise PackageTooLarge(
            f"The package says it unpacks to {_human_bytes(declared)}, over "
            f"the {_human_bytes(limits.max_uncompressed_bytes)} limit. "
            "Refusing to unpack it."
        )


def _parts(member: zipfile.ZipInfo) -> tuple[str, ...]:
    """A member's path segments, read the way the installer reads them."""
    return tuple(
        part for part in PurePosixPath(member.filename.replace("\\", "/")).parts if part
    )


def _find_manifest(members: list[zipfile.ZipInfo]) -> tuple[zipfile.ZipInfo, str]:
    """The one ``manifest.toml``, at the top of the archive or one level down.

    Both shapes are what people actually produce — "zip this folder" and "zip
    the contents of this folder" — and :func:`~personacore.plugins.packages
    ._find_plugin_directory` accepts both, so this accepts both, with its
    refusals worded the same way.
    """
    at_top: zipfile.ZipInfo | None = None
    nested: dict[str, zipfile.ZipInfo] = {}
    for member in members:
        if member.is_dir():
            continue
        parts = _parts(member)
        if parts == (MANIFEST_FILENAME,):
            at_top = member
        elif (
            len(parts) == 2
            and parts[1] == MANIFEST_FILENAME
            and parts[0] not in _IGNORED_TOP_LEVEL
        ):
            nested[parts[0]] = member

    if at_top is not None:
        return at_top, ""
    if len(nested) == 1:
        folder, member = next(iter(nested.items()))
        return member, folder
    if not nested:
        raise PackageInvalid(
            f"The package has no {MANIFEST_FILENAME}. A plugin package is the "
            "plugin's own folder, zipped — the manifest belongs either at the top "
            "of the archive or in the single folder inside it."
        )
    names = ", ".join(repr(name) for name in sorted(nested))
    raise PackageInvalid(
        f"The package contains more than one plugin folder ({names}). Install them "
        "one at a time — a package is one plugin."
    )


def _read_manifest(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> str:
    """Decompress the manifest, and only the manifest, with a ceiling on it."""
    if member.file_size > MAX_MANIFEST_BYTES:
        raise PackageTooLarge(_manifest_too_large())
    with archive.open(member) as handle:
        data = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        # The header said one thing and the stream another, which is what a zip
        # bomb looks like. Unreachable while CPython also stops a member's read
        # at its declared size — which is exactly why it must not be the only
        # check.
        raise PackageTooLarge(_manifest_too_large())
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackageInvalid(
            f"The package's {MANIFEST_FILENAME} is not readable text ({exc.reason}). "
            "A manifest is a small UTF-8 TOML file; this one is something else."
        ) from exc


def _manifest_too_large() -> str:
    return (
        f"The package's {MANIFEST_FILENAME} is larger than "
        f"{_human_bytes(MAX_MANIFEST_BYTES)}, which no real manifest is. Nothing "
        "was read from the package."
    )


def _parse_manifest(raw: str, folder: str) -> PluginManifest:
    """TOML in, validated manifest out — with the load-time wording, not a traceback."""
    where = Path(f"{folder}/{MANIFEST_FILENAME}" if folder else MANIFEST_FILENAME)
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise PackageInvalid(
            f"The package's {where.as_posix()} is not valid TOML — {exc}. Nothing "
            "was installed."
        ) from exc
    try:
        return PluginManifest.model_validate(parsed)
    except ValidationError as exc:
        raise PackageInvalid(
            f"The package's manifest is not one this core can read: "
            f"{describe_validation_error(where, exc)}"
        ) from exc


def _disclose(manifest: PluginManifest, folder: str) -> PackageDisclosure:
    """The manifest, rearranged into the order the operator reads it in.

    Tools come out sorted with the ones that act first, because that is the half
    of the list the decision turns on: a package whose thirtieth tool is the
    ``restricted`` one must not hide it below the fold.
    """
    tools = [
        DeclaredTool(name=name, risk=tool.risk, description=tool.description or "")
        for name, tool in manifest.tools.items()
    ]
    order = {RiskLevel.RESTRICTED: 0, RiskLevel.CONFIRM: 1, RiskLevel.SAFE: 2}
    tools.sort(key=lambda tool: (order[tool.risk], tool.name))
    return PackageDisclosure(
        name=manifest.plugin.name,
        version=manifest.plugin.version,
        contract=manifest.plugin.contract,
        description=manifest.plugin.description,
        transport=manifest.plugin.transport,
        entry=manifest.plugin.entry,
        url=manifest.plugin.url,
        provides=tuple(manifest.plugin.provides),
        tools=tuple(tools),
        declared_hosts=tuple(manifest.permissions.network),
        declared_paths=tuple(manifest.permissions.paths),
        secrets=tuple(manifest.permissions.secret_names),
        secret_requests=tuple(manifest.permissions.secrets),
        folder=folder,
    )


__all__ = [
    "MAX_MANIFEST_BYTES",
    "DeclaredTool",
    "PackageDisclosure",
    "inspect_package",
]
