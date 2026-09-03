"""Plugin discovery and manifest loading -- spec section 5.1.

This is the layer that turns a directory on disk into validated,
ready-to-launch plugin records. It does **not** launch anything and does
**not** speak MCP -- process supervision and the MCP client are separate
components that consume `PluginRecord`s from here.

Two sources are scanned (spec 5.1, Appendix B):

- `<appdata>/plugins/<name>/` -- stdio plugins, spawned as a subprocess.
- `<appdata>/plugins-http.d/<name>/` -- HTTP-transport registrations, for
  plugins that run as their own service. The core treats both identically
  once discovered, so this module mirrors the stdio layout for HTTP
  registrations too: `manifest.toml` plus the plugin's own `config.toml`,
  just without a `<code>` payload. Appendix A/B are illustrative rather than
  final on this point; if a different convention is wanted, that is a
  contract question for the manager, not something to silently diverge on
  per-caller.

Every plugin folder is treated as **untrusted input** (spec 7): a directory
whose name doesn't match its own declared identity, or whose manifest points
outside itself via an absolute path, a `..` segment, or a symlink, is
rejected rather than followed.

`scan()` never raises for a single bad plugin. It always returns every
success **and** every failure it found (`DiscoveryResult`), so a broken
plugin can be listed in the admin UI next to its error instead of taking the
whole scan down with it (spec 5.1).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import ValidationError

from personacore import CONTRACT_VERSION
from personacore.contracts.manifest import (
    CONTRACT_2_0_CHANGE,
    PluginManifest,
    Transport,
)
from personacore.plugins.errors import (
    PluginLoadFailure,
    PluginRejected,
    PluginSecurityError,
    describe_validation_error,
)

MANIFEST_FILENAME = "manifest.toml"
CONFIG_FILENAME = "config.toml"
STDIO_DIRNAME = "plugins"
HTTP_DIRNAME = "plugins-http.d"
SECRETS_DIRNAME = "secrets"

_TRANSPORT_DIRNAME = {
    Transport.STDIO: STDIO_DIRNAME,
    Transport.HTTP: HTTP_DIRNAME,
}


@dataclass(frozen=True)
class PluginRecord:
    """One successfully discovered, validated plugin -- ready to hand to
    whatever launches it (a separate component; see module docstring)."""

    name: str
    directory: Path
    manifest: PluginManifest
    manifest_path: Path
    config_path: Path
    """Where this plugin's own config.toml lives, whether or not it exists
    yet (spec 5.1: config lives in the plugin's own folder, never centrally)."""

    config: dict[str, Any] | None
    """Parsed config.toml contents, unvalidated -- the plugin owns that
    shape, not the core. None if config.toml does not exist yet."""


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything one scan found: successes and failures side by side."""

    plugins: list[PluginRecord]
    failures: list[PluginLoadFailure]

    def by_name(self) -> dict[str, PluginRecord]:
        return {p.name: p for p in self.plugins}


class PluginDiscovery:
    """Scans an appdata root for stdio and HTTP plugin registrations.

    `appdata_root` arrives as a constructor argument -- this module does not
    read a shared/global config object; another component owns that surface.
    """

    def __init__(self, appdata_root: Path | str) -> None:
        self._root = Path(appdata_root)

    def scan(self) -> DiscoveryResult:
        """Scan both plugin directories fresh. Suitable as the admin UI's
        "reload" action (spec 5.1: adding a plugin is copy a folder, hit
        reload) -- just call this again."""
        plugins: list[PluginRecord] = []
        failures: list[PluginLoadFailure] = []

        for transport in (Transport.STDIO, Transport.HTTP):
            root = self._root / _TRANSPORT_DIRNAME[transport]
            found, failed = self._scan_root(root, transport)
            plugins.extend(found)
            failures.extend(failed)

        plugins, dup_failures = _drop_duplicates(plugins)
        failures.extend(dup_failures)
        return DiscoveryResult(plugins=plugins, failures=failures)

    # -- per-directory scan -------------------------------------------------

    def _scan_root(
        self, root: Path, expected_transport: Transport
    ) -> tuple[list[PluginRecord], list[PluginLoadFailure]]:
        plugins: list[PluginRecord] = []
        failures: list[PluginLoadFailure] = []
        if not root.is_dir():
            return plugins, failures  # source not present yet -- nothing to report

        for candidate in sorted(root.iterdir(), key=lambda p: p.name):
            if not candidate.is_dir():
                continue  # stray files at this level are ignored, not errors
            if candidate.name.startswith(("_", ".")):
                # Not a plugin: a leading underscore or dot marks a folder that
                # is here to be copied or ignored, not loaded. Without this the
                # bundled `_template` cannot sit in the plugins directory where
                # an author would look for it — it would fail the name check
                # forever and show as a permanent red row in the admin UI, which
                # teaches people to ignore red rows.
                continue
            outcome = self._load_one(candidate, root, expected_transport)
            if isinstance(outcome, PluginLoadFailure):
                failures.append(outcome)
            else:
                plugins.append(outcome)
        return plugins, failures

    def _load_one(
        self, directory: Path, root: Path, expected_transport: Transport
    ) -> PluginRecord | PluginLoadFailure:
        escape = _directory_escape_message(directory, root)
        if escape is not None:
            return PluginLoadFailure(source=directory, name=directory.name, message=escape)

        manifest_path = directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            return PluginLoadFailure(
                source=directory,
                name=directory.name,
                message=(
                    f"{directory.as_posix()}: missing {MANIFEST_FILENAME} -- every "
                    "plugin folder needs one (spec 5.1)"
                ),
            )

        try:
            raw_text = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            return PluginLoadFailure(
                source=manifest_path,
                name=directory.name,
                message=f"{manifest_path.as_posix()}: could not be read -- {exc}",
            )

        try:
            raw = tomllib.loads(raw_text)
        except tomllib.TOMLDecodeError as exc:
            return PluginLoadFailure(
                source=manifest_path,
                name=directory.name,
                message=f"{manifest_path.as_posix()}: not valid TOML -- {exc}",
            )

        try:
            manifest = PluginManifest.model_validate(raw)
        except ValidationError as exc:
            return PluginLoadFailure(
                source=manifest_path,
                name=directory.name,
                message=describe_validation_error(manifest_path, exc),
            )

        try:
            _check_directory_name(directory, manifest)
            _check_transport_location(directory, manifest, expected_transport)
            _check_contract_compatibility(directory, manifest)
            _check_security(directory, manifest_path, manifest)
        except PluginRejected as exc:
            return PluginLoadFailure(source=manifest_path, name=directory.name, message=str(exc))

        config_path = directory / CONFIG_FILENAME
        config: dict[str, Any] | None = None
        if config_path.is_file():
            try:
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                return PluginLoadFailure(
                    source=config_path,
                    name=directory.name,
                    message=f"{config_path.as_posix()}: not valid TOML -- {exc}",
                )

        return PluginRecord(
            name=manifest.plugin.name,
            directory=directory,
            manifest=manifest,
            manifest_path=manifest_path,
            config_path=config_path,
            config=config,
        )


# -- structural safety: don't even follow a symlinked plugin folder ---------


def _directory_escape_message(directory: Path, root: Path) -> str | None:
    """None if `directory` genuinely lives inside `root`; otherwise a
    plain-English message. Catches a plugin folder that is itself a symlink
    (or is reached through one) pointing outside the plugins directory."""
    try:
        resolved_dir = directory.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError:
        return None  # let the missing-manifest check explain a vanished directory
    try:
        resolved_dir.relative_to(resolved_root)
    except ValueError:
        return (
            f"{directory.as_posix()}: this plugin folder resolves to "
            f"{resolved_dir.as_posix()!r}, outside {resolved_root.as_posix()!r} -- "
            "refusing to follow a symlink out of the plugins directory"
        )
    return None


# -- identity, location, compatibility checks -------------------------------


def _check_directory_name(directory: Path, manifest: PluginManifest) -> None:
    if directory.name != manifest.plugin.name:
        raise PluginSecurityError(
            f"{directory.as_posix()}: folder name {directory.name!r} does not match "
            f"the manifest's declared plugin name {manifest.plugin.name!r} -- a plugin's "
            "folder and its declared name must be identical (spec 7)"
        )


def _check_transport_location(
    directory: Path, manifest: PluginManifest, expected: Transport
) -> None:
    if manifest.plugin.transport != expected:
        wrong_dir = _TRANSPORT_DIRNAME[expected]
        right_dir = _TRANSPORT_DIRNAME[manifest.plugin.transport]
        raise PluginRejected(
            f"{directory.as_posix()}: manifest declares transport "
            f"{manifest.plugin.transport.value!r} but was found under "
            f"{wrong_dir}/ -- move it to {right_dir}/ instead"
        )


def _check_contract_compatibility(directory: Path, manifest: PluginManifest) -> None:
    """Refuse a plugin written for a contract this core does not implement.

    A major mismatch gets the paragraph saying **what changed**, not just that
    something did (ADR-0026). A 1.x plugin meeting a 2.x core is the only such
    jump that exists, and "incompatible, update the plugin" leaves whoever is
    holding the folder with nothing to edit; the sentence names the field.

    **A minor mismatch is a different sentence, and saying so matters.** Since
    contract 2.1 a plugin can legitimately pin a minor it needs, and this core
    can legitimately be older than one. Calling that "a different major version,
    which is incompatible" — as this did while 1.x-meets-2.0 was the only
    mismatch that existed — sends the operator hunting for a breaking change
    that is not there. Nothing is broken: the core is simply behind what the
    plugin asked for, and the message names the version it asked for.
    """
    declared = manifest.plugin.contract
    if _contract_compatible(declared, CONTRACT_VERSION):
        return

    core_major = CONTRACT_VERSION.split(".", 1)[0]
    if declared.split(".", 1)[0] == core_major:
        raise PluginRejected(
            f"{directory.as_posix()}: this plugin needs plugin contract "
            f"{declared!r} and this core implements {CONTRACT_VERSION!r} -- an "
            "earlier minor version, so something the plugin asks for is not here "
            f"yet and it has not been loaded. Update this core to contract "
            f"{declared} or later, or -- if the plugin does not really need "
            "anything newer -- edit its manifest.toml to "
            f'contract = "{core_major}.x", which loads on any {core_major}.y '
            "core (spec 4.5)."
        )

    note = (
        f" {CONTRACT_2_0_CHANGE}"
        if declared.split(".", 1)[0] == "1" and CONTRACT_VERSION.split(".", 1)[0] == "2"
        else ""
    )
    raise PluginRejected(
        f"{directory.as_posix()}: this plugin was written for plugin contract "
        f"{declared!r} and this core implements {CONTRACT_VERSION!r} -- a "
        f"different major version, which is incompatible, so it has not been "
        f"loaded.{note} Edit the plugin's manifest.toml and install it again "
        "(spec 4.5)."
    )


def _contract_compatible(declared: str, core_version: str) -> bool:
    """Semver rule, spec 4.5: a plugin targeting '1.x' works on any 1.y core.
    A plugin pinned to an exact minor works on that minor or any later one in
    the same major (minor bumps are additive and never break an existing
    plugin), never an earlier one that lacks what it needs."""
    d_major, d_minor = declared.split(".", 1)
    c_major, c_minor = core_version.split(".", 1)
    if d_major != c_major:
        return False
    if d_minor == "x":
        return True
    return int(d_minor) <= int(c_minor)


# -- spec 7 security checks on path-like manifest fields --------------------


def _check_security(directory: Path, manifest_path: Path, manifest: PluginManifest) -> None:
    context = manifest_path.as_posix()
    if manifest.plugin.entry:
        _validate_entry(directory, manifest.plugin.entry, context=context)
    for path_value in manifest.permissions.paths:
        _validate_permission_path(path_value, context=context)


# A Windows drive prefix: exactly one letter followed by ':', anchored to a
# string start, whitespace, or a quote character. The single-letter
# requirement is what keeps a URL scheme (`http:`, `data:`) out of this net --
# the character immediately before the ':' in "http:" is 'p', which is itself
# preceded by another letter ('t'), not by start/whitespace/quote, so it never
# matches. "C:", " C:", '"C:', and "'C:" all match; "http:" and "key:value"
# never do.
_DRIVE_LETTER_RE = re.compile(r"(?:^|(?<=[\s'\"]))[A-Za-z]:")


def _is_absolute_path_string(value: str) -> bool:
    if value.startswith(("/", "\\", "~")):
        return True
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()


def _has_traversal_segment(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return ".." in PurePosixPath(normalized).parts


def _strip_matching_quotes(token: str) -> str:
    """Strip one layer of surrounding quotes so a quoted path's leading
    separator isn't hidden behind a literal quote character -- any launcher
    that honours quoting (a shell, shlex, CreateProcess) sees the unquoted
    path underneath, so the check must too."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _has_raw_drive_prefix(value: str) -> bool:
    """True if the untouched raw string contains a Windows drive prefix
    anywhere -- start of string, after whitespace, or after a quote
    character. Spec 7 bypass: neither tokenizer can be trusted to preserve
    this evidence. `str.split()` breaks a quoted absolute path into
    fragments where the leading literal quote defeats a per-token
    drive-letter check (`"C:\\Program Files\\evil\\run.exe" --flag` splits
    into pieces that individually look relative). `shlex.split()` would
    reassemble the quoting correctly but treats backslash as an escape
    character, silently eating a Windows-style '..\\..' traversal attempt
    before this check ever sees it. So this check trusts neither tokenizer:
    it scans the raw string directly, including inside quotes."""
    return bool(_DRIVE_LETTER_RE.search(value))


def _has_raw_traversal_segment(value: str) -> bool:
    """True if any '/'- or '\\'-delimited segment of the raw string is
    exactly '..' -- checked on the untouched string (both slash styles at
    once) so a tokenizer never gets a chance to eat the evidence first."""
    return ".." in re.split(r"[\\/]", value)


def _raw_leading_separator_tokens(value: str) -> list[str]:
    """Every whitespace-separated token in `value` that, once one layer of
    surrounding quotes is stripped, still starts with '/', '\\' or '~' --
    i.e. every token that is an absolute or home-relative path once a
    quote-honouring launcher would see it."""
    return [t for t in value.split() if _strip_matching_quotes(t).startswith(("/", "\\", "~"))]


def _validate_relative_field(field: str, value: str, *, context: str) -> None:
    if _is_absolute_path_string(value):
        raise PluginSecurityError(
            f"{context}: {field} {value!r} is an absolute path -- plugin manifests "
            "may only reference paths relative to the plugin's own folder"
        )
    if _has_traversal_segment(value):
        raise PluginSecurityError(
            f"{context}: {field} {value!r} contains a '..' segment -- a plugin may "
            "not reference anything outside its own folder"
        )


def _validate_raw_path_like(field: str, value: str, *, context: str) -> None:
    """Fail-closed raw-string checks applied to a single path-like manifest
    value (spec 7): a Windows drive prefix, a leading separator once quotes
    are stripped, or a '..' segment, anywhere in the untouched string. This
    runs *in addition to* `_validate_relative_field`'s parsed-path checks --
    it exists precisely because those parsed checks (and any tokenizer built
    on top of them) can be fooled by quoting or backslash-escaping, so the
    decision to reject cannot depend on either being faithful."""
    if _has_raw_drive_prefix(value):
        raise PluginSecurityError(
            f"{context}: {field} {value!r} contains a Windows drive prefix -- this "
            "is an absolute path; plugin manifests may only reference paths "
            "relative to the plugin's own folder (spec 7)"
        )
    if _has_raw_traversal_segment(value):
        raise PluginSecurityError(
            f"{context}: {field} {value!r} contains a '..' segment -- a plugin may "
            "not reference anything outside its own folder"
        )
    offenders = _raw_leading_separator_tokens(value)
    if offenders:
        raise PluginSecurityError(
            f"{context}: {field} {value!r} is an absolute path (token "
            f"{offenders[0]!r}) -- plugin manifests may only reference paths "
            "relative to the plugin's own folder"
        )


def _validate_permission_path(value: str, *, context: str) -> None:
    _validate_raw_path_like("permissions.paths entry", value, context=context)
    _validate_relative_field("permissions.paths entry", value, context=context)
    top = PurePosixPath(value.replace("\\", "/")).parts[0] if value else ""
    if top.lower() == SECRETS_DIRNAME:
        raise PluginSecurityError(
            f"{context}: permissions.paths entry {value!r} points at the appdata "
            "secrets directory -- declare needed secrets in permissions.secrets "
            "instead, plugins never get filesystem access to the secret store"
        )


def _validate_entry(directory: Path, entry: str, *, context: str) -> None:
    # The raw-string checks run first, over the untouched `entry` value,
    # before any tokenizer touches it (spec 7). Both tokenizers available
    # here destroy evidence this check needs: plain `str.split()` loses
    # quoting, so a quoted absolute path's drive letter gets fragmented and
    # never matches; `shlex.split()` treats backslash as an escape character
    # and would silently eat a Windows-style '..\\..' traversal attempt. So
    # the security decision does not depend on either tokenizer being
    # faithful -- it is made against the raw string directly.
    _validate_raw_path_like("entry", entry, context=context)

    # Plain whitespace splitting below is retained as defense-in-depth on
    # top of the raw-string checks above, not as the primary safeguard: it
    # still catches an unquoted absolute or traversal token per-position,
    # which is useful context in the error message even though the raw
    # checks above already cover the same ground and more.
    tokens = entry.split()
    if not tokens:
        raise PluginSecurityError(f"{context}: entry is empty -- nothing to run")

    plugin_root = directory.resolve()
    for token in tokens:
        if token.startswith("-"):
            continue  # a command-line flag, not a path
        _validate_relative_field("entry", token, context=context)
        candidate = directory / token
        # A token can be illegal as a filename rather than merely absent: over
        # 255 bytes a component raises ENAMETOOLONG on Linux, and an embedded
        # NUL raises ValueError. Both surfaced as an unhandled error that took
        # the whole scan down, which is precisely the "one bad plugin never
        # breaks the core" rule (spec section 5.1) failing. A token the OS will
        # not even look at cannot name anything inside the plugin folder, so it
        # is skipped like any other non-path token.
        try:
            if not candidate.exists():
                continue  # e.g. a bare interpreter name like "python"
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        try:
            resolved.relative_to(plugin_root)
        except ValueError:
            raise PluginSecurityError(
                f"{context}: entry token {token!r} resolves to {resolved.as_posix()!r}, "
                f"outside the plugin's own folder {plugin_root.as_posix()!r} -- refusing "
                "(possible symlink escape)"
            ) from None


# -- duplicate names across both directories --------------------------------


def _drop_duplicates(
    plugins: list[PluginRecord],
) -> tuple[list[PluginRecord], list[PluginLoadFailure]]:
    by_name: dict[str, list[PluginRecord]] = {}
    for record in plugins:
        by_name.setdefault(record.name, []).append(record)

    kept: list[PluginRecord] = []
    failures: list[PluginLoadFailure] = []
    for name, records in by_name.items():
        if len(records) == 1:
            kept.append(records[0])
            continue
        dirs = ", ".join(r.directory.as_posix() for r in records)
        for record in records:
            failures.append(
                PluginLoadFailure(
                    source=record.directory,
                    name=name,
                    message=(
                        f"plugin name {name!r} is declared more than once: {dirs} -- "
                        "plugin names must be unique across plugins/ and "
                        "plugins-http.d/; none of them will be loaded until this is "
                        "resolved"
                    ),
                )
            )
    return kept, failures
