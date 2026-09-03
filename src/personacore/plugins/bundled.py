"""Installing the plugins that ship with the core — spec section 5.1.

"A dead-simple plugin (weather) ships with the core as living documentation.
'Write a new plugin' = copy the template. This is part of the core deliverable,
not an extra."

Shipping them inside the image is not enough: appdata is where plugins are read
from, and a container's appdata starts empty. Without this the reference plugin
exists only in a repository the operator has no copy of on the machine actually
running the assistant — which is exactly the position an early deployment was
left in.

Same rule as the starter config and the default persona: seed on first run,
**never overwrite** — with one carefully fenced exception (PC-322). A shipped
plugin strictly newer than an *untouched* installed copy is refreshed, because
otherwise a fix to the reference plugin — including a security fix — reaches
nobody who already has it. Everything else about an installed plugin still
belongs to the operator: a deleted one stays deleted, an edited one is left
alone, and their `config.toml` is never written over (spec section 7 — an
upgrade must not touch appdata content).

Every refusal to refresh in here leaves a plugin that keeps working exactly as
it did. That is why the doubtful cases all resolve to "leave it alone".

Where a plugin is seeded is the plugin's own decision, read from its manifest:
`transport = "stdio"` means `appdata/plugins/`, `transport = "http"` means
`appdata/plugins-http.d/`. Discovery reads those two directories separately and
refuses a manifest found in the wrong one, so the transport is not a detail the
seeder may ignore — getting it wrong installs a plugin that can never load.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from personacore.audit import get_logger
from personacore.config.appdata import AppdataLayout

logger = get_logger(__name__)

BUNDLED_PLUGINS_ENV = "PERSONACORE_BUNDLED_PLUGINS"
DEFAULT_BUNDLED_DIR = Path("/opt/personacore/plugins")
"""Where the image keeps its read-only copy. Absent outside a container, which
is why every path here treats "not there" as normal rather than as an error."""

INSTALL_MARKER = ".installed-from-image"
"""Written so a later start can tell "the operator deleted this" from "this was
never installed". Without it, seeding on every start would silently resurrect a
plugin somebody removed on purpose.

**One file, in `plugins/`, covering both plugin directories, keyed by plugin
name.** A name is still an unambiguous key now that there are two destinations,
and it is not a shortcut: discovery refuses a name that appears in both
directories as a duplicate, so a plugin name identifies at most one installed
folder across the whole of appdata. Splitting the record per directory would be
the ambiguous option, not the safe one — an HTTP plugin whose predecessor was
seeded as stdio would find no record of itself under `plugins-http.d/` and seed
a copy the operator had deliberately deleted.
"""

INSTALL_STATE = ".installed-from-image.json"
"""What we put there, file by file, the last time we wrote it. Keyed by plugin
name and kept beside :data:`INSTALL_MARKER`, for the same reason.

This is the whole of how a refresh tells "untouched" from "the operator changed
it": a plugin is unmodified when every shipped file still hashes to what was
recorded when we shipped it. An absent file, or one with no entry for a plugin,
means we genuinely cannot tell the two apart — and not knowing is answered by
leaving the plugin alone, never by overwriting it.
"""

OPERATOR_OWNED = frozenset({"config.toml"})
"""Files inside a plugin that belong to the operator the moment it is installed.

Editing `config.toml` is the expected, documented, encouraged thing to do — it
is how a plugin gets set up at all. So it is excluded from the "is this copy
untouched?" comparison, because an edit there must not stop a fix arriving, and
it is never written over by a refresh, because that edit must survive one. The
shipped `config.toml` is a starting value and only ever a starting value.
"""

_IGNORED_DIR_NAMES = frozenset({"__pycache__"})
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
"""Bytecode caches are made by running a plugin, not by editing one. Counting
them as an operator's change would freeze every plugin the moment it ran."""


# ---------------------------------------------------------------------------
# Semver — enough of it, correctly, with no dependency
# ---------------------------------------------------------------------------

_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

SemverKey = tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]


def parse_semver(version: str) -> SemverKey | None:
    """A sort key for a semver string, or None if it is not one.

    Comparing versions as strings is exactly the bug this exists to avoid:
    ``"1.10.0" < "1.9.0"`` is true of strings and false of releases, so a string
    comparison would refuse the very upgrade it was asked to find.

    The key is ``(major, minor, patch, release?, prerelease identifiers)``.
    ``release?`` is 1 for a plain release and 0 for a prerelease, because semver
    ranks ``1.0.0-rc.1`` *below* ``1.0.0`` — which the identifier tuple alone
    cannot express, an empty tuple sorting first. Build metadata is dropped, as
    semver requires: ``1.0.0+a`` and ``1.0.0+b`` are the same release.
    """
    match = _SEMVER.match(version.strip())
    if match is None:
        return None
    prerelease = match["pre"]
    identifiers: tuple[tuple[int, int, str], ...] = ()
    if prerelease is not None:
        # Numeric identifiers compare numerically and rank below alphanumeric
        # ones; the leading 0/1 is that ranking. The pattern has already limited
        # these to ASCII [0-9A-Za-z-], so `isdigit` cannot see a non-ASCII digit
        # that `int` would then refuse.
        identifiers = tuple(
            (0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in prerelease.split(".")
        )
    return (
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        1 if prerelease is None else 0,
        identifiers,
    )


def is_strictly_newer(shipped: str, installed: str) -> bool | None:
    """Is `shipped` a later release than `installed`?

    None when either side is not semver at all — a question we cannot answer,
    which is not the same as "no" and is logged as its own reason.
    """
    left, right = parse_semver(shipped), parse_semver(installed)
    if left is None or right is None:
        return None
    return left > right


# ---------------------------------------------------------------------------
# What "a plugin's files" means — once, for both the seeder and the CI gate
# ---------------------------------------------------------------------------


def plugin_files(directory: Path) -> list[str]:
    """Every file that counts as part of a plugin, as sorted POSIX-relative
    paths.

    Sorted so the order is the same on every machine, POSIX-separated so a
    Windows checkout hashes to what Linux CI hashes, and bytecode-free so
    running a plugin does not change what it is.
    """
    found: list[str] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if _IGNORED_DIR_NAMES.intersection(relative.parts):
            continue
        if path.suffix in _IGNORED_SUFFIXES:
            continue
        found.append(relative.as_posix())
    return sorted(found)


def file_digest(path: Path) -> str:
    """SHA-256 of one file with line endings normalised.

    Normalising CRLF to LF is what lets the gate mean the same thing on a
    Windows checkout as on Linux CI: git rewrites line endings on the way to
    disk, so without this every hash would differ by platform and the gate would
    cry wolf on every developer machine.
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def plugin_file_digests(directory: Path) -> dict[str, str]:
    """Each of a plugin's files against its own hash."""
    return {name: file_digest(directory / name) for name in plugin_files(directory)}


def plugin_content_hash(directory: Path) -> str:
    """One hash standing for the whole plugin — what PC-323 pins to a version.

    The path goes into the hash as well as the bytes, so moving a file, renaming
    one, or adding an empty one all change the answer.
    """
    digest = hashlib.sha256()
    for name, file_hash in plugin_file_digests(directory).items():
        digest.update(f"{name}\n{file_hash}\n".encode())
    return digest.hexdigest()


def bundled_plugin_dirs(source: Path) -> list[Path]:
    """The plugin folders inside a bundled-plugins directory, sorted.

    A folder is a plugin when it has a manifest; `plugins/README.md` and any
    stray directory are not. The gate and the seeder share this so the gate
    cannot end up pinning something the seeder never installs.
    """
    return sorted(
        candidate
        for candidate in source.iterdir()
        if candidate.is_dir() and (candidate / "manifest.toml").is_file()
    )


def _manifest_plugin_table(directory: Path) -> dict[str, object] | None:
    """A manifest's ``[plugin]`` table, or None if it will not read.

    The cheapest safe read there is: `tomllib` on the file, no schema, no
    pydantic, no import of the contracts package. It is all the seeder needs —
    two scalars — and validating properly here would mean the seeder rejecting
    plugins on grounds discovery is the one that owns.

    Deliberately tolerant: this is also asked of an *installed* copy, which the
    operator may have edited into something that no longer parses. A manifest we
    cannot read is a plugin we leave alone, not a start-up failure.
    """
    try:
        raw = tomllib.loads((directory / "manifest.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        # ValueError covers tomllib.TOMLDecodeError and the ValueError pathlib
        # raises for a malformed path; neither is worth a dead core.
        return None
    table = raw.get("plugin")
    return table if isinstance(table, dict) else None


def declared_version(directory: Path) -> str | None:
    """The `version` a plugin's manifest declares, or None if it cannot be read."""
    table = _manifest_plugin_table(directory)
    if table is None:
        return None
    version = table.get("version")
    return version if isinstance(version, str) else None


def declared_transport(directory: Path) -> str | None:
    """The `transport` a plugin's manifest declares, or None if it cannot be
    read.

    Returned as the raw string rather than an enum, and *not* checked against
    the two legal values here: the caller decides what an unrecognised one
    means, and gets to log the word it actually found.
    """
    table = _manifest_plugin_table(directory)
    if table is None:
        return None
    transport = table.get("transport")
    return transport if isinstance(transport, str) else None


# ---------------------------------------------------------------------------
# The record of what we installed, and what it lets us decide
# ---------------------------------------------------------------------------


def _read_state(path: Path) -> dict[str, dict[str, str]]:
    """Per-plugin `{file: hash}` as it was last written. Unreadable reads as
    empty — which costs a refresh, never an operator's work."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        name: files
        for name, files in loaded.items()
        if isinstance(files, dict) and all(isinstance(value, str) for value in files.values())
    }


def _write_state(path: Path, state: dict[str, dict[str, str]]) -> None:
    try:
        path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        # Losing the record costs future refreshes, not the operator's data, so
        # it is worth a loud line and nothing more drastic.
        logger.error("bundled_plugin_state_unwritable", path=str(path), error=str(exc))


def safe_plugin_file_digests(directory: Path) -> dict[str, str] | None:
    """:func:`plugin_file_digests`, or ``None`` when the directory will not read.

    The seeder runs before anything else in the core is up, so *any* exception
    out of it is the core failing to start — and with it the admin UI, which is
    the only place an operator could fix whatever caused it. A deny-read ACL on
    one file inside one plugin folder used to be enough to do that (the 2026-08 security review).

    So every filesystem answer the seeder needs comes through here, and "the
    disk would not tell me" is a value rather than an exception. `OSError`
    covers permission, a file that vanished between the listing and the read,
    a dangling symlink, a device that is not there, a name too long, and a
    directory that turned out to be something else; `ValueError` covers the
    surprising ones `pathlib` raises for a malformed path.
    """
    try:
        return plugin_file_digests(directory)
    except (OSError, ValueError):
        return None


def _installed_file_digests(installed: Path) -> dict[str, str] | None:
    """The hashes an installed copy is compared by, or ``None`` if unreadable.

    Operator-owned files are dropped, for the reason :func:`_is_unmodified`
    gives.
    """
    try:
        return {
            name: file_digest(installed / name)
            for name in plugin_files(installed)
            if name not in OPERATOR_OWNED
        }
    except (OSError, ValueError):
        return None


def _is_unmodified(installed: Path, recorded: dict[str, str]) -> bool | None:
    """Does this copy still hold exactly the shipped files that were recorded?

    ``None`` is a third answer and a real one: **we could not read the copy, so
    we cannot tell.** It is kept distinct from ``False`` only so the log can say
    which of the two happened; both resolve to leaving the plugin alone, because
    a copy we cannot prove untouched is a copy we do not overwrite.

    Operator-owned files are dropped from both sides: a `config.toml` that has
    been edited, or deleted, says nothing about whether the code was touched.
    Anything else added, removed or altered means the operator has made this
    copy theirs, and it is not ours to replace.
    """
    expected = {name: h for name, h in recorded.items() if name not in OPERATOR_OWNED}
    present = _installed_file_digests(installed)
    if present is None:
        return None
    return present == expected


def _refresh_decision(shipped: Path, installed: Path, recorded: dict[str, str] | None) -> str:
    """``"refresh"``, or the name of the reason we are not going to.

    The reasons are log lines, so they are named rather than described.
    """
    if recorded is None:
        # Installed by a core that kept no record. Those files could be pristine
        # or could be a year of somebody's changes, and nothing on disk tells
        # the two apart — the shipped copy of the version they hold is long
        # gone. `install_bundled_plugins` bootstraps a record when the versions
        # still match and the files are still identical; once the shipped copy
        # has moved on, that door is shut and this plugin is never refreshed.
        # Which is a plugin that keeps working, said out loud in the log.
        return "no_install_record"
    installed_version = declared_version(installed)
    shipped_version = declared_version(shipped)
    if installed_version is None or shipped_version is None:
        return "version_unreadable"
    newer = is_strictly_newer(shipped_version, installed_version)
    if newer is None:
        return "version_not_semver"
    if not newer:
        return "not_newer"
    unmodified = _is_unmodified(installed, recorded)
    if unmodified is None:
        # Something under the plugin's own folder would not read. We cannot
        # prove the copy is untouched, so we do not replace it — the same answer
        # as `operator_modified`, said with the reason it actually had.
        return "files_unreadable"
    if not unmodified:
        return "operator_modified"
    return "refresh"


def _apply_refresh(shipped: Path, installed: Path, recorded: dict[str, str]) -> None:
    """Put the shipped files in place, keep the operator's config, drop what the
    new version no longer ships.

    Writes only inside `installed`, which is one plugin's own directory under
    appdata/plugins. Nothing here reads or writes anywhere else — appdata's
    secrets are not on any path this function can reach.
    """
    shipped_files = set(plugin_files(shipped))
    for name in sorted(shipped_files):
        if name in OPERATOR_OWNED and (installed / name).exists():
            # The operator's setup. A newer shipped default is a suggestion, and
            # it never arrives by deleting their work.
            continue
        target = installed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shipped / name, target)

    # Files the old version shipped and the new one does not. They are ours to
    # remove precisely because we have just proved they are still exactly as we
    # left them; anything the operator added was never in the record and stays.
    for name in sorted(set(recorded) - shipped_files - OPERATOR_OWNED):
        stale = installed / name
        if stale.is_file():
            stale.unlink()

    # Bytecode compiled from code that no longer exists.
    for cache in sorted(installed.rglob("__pycache__")):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)

    # Directories left empty by the removals above.
    for directory in sorted(installed.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def bundled_source(explicit: Path | str | None = None) -> Path | None:
    """Where the bundled plugins are, or None if this is not a container.

    An environment variable holding something that is not a path at all is
    "not a container" too, rather than a start-up crash: the operator who set it
    needs the admin UI up to unset it.
    """
    try:
        candidate = Path(explicit or os.environ.get(BUNDLED_PLUGINS_ENV) or DEFAULT_BUNDLED_DIR)
        return candidate if candidate.is_dir() else None
    except (OSError, ValueError):
        return None


STDIO_TRANSPORT = "stdio"
HTTP_TRANSPORT = "http"


def _transport_roots(layout: AppdataLayout) -> dict[str, Path]:
    """Which appdata directory each transport is seeded into.

    Discovery reads exactly two directories and refuses a manifest found in the
    wrong one, naming the other. Seeding into `plugins/` regardless of transport
    therefore ships an HTTP plugin straight into a permanent red row in the
    admin UI: present on disk, refused on every scan. The routing lives here so
    the seeder and `AppdataLayout` agree on the answer in one place.
    """
    return {STDIO_TRANSPORT: layout.plugins, HTTP_TRANSPORT: layout.plugins_http}


def _destination_root(candidate: Path, layout: AppdataLayout) -> Path | None:
    """Where this shipped plugin belongs, or None — logged — if nowhere.

    Every "no" here costs one plugin its seeding and nothing else, because a
    plugin the seeder cannot place is a plugin discovery would refuse anyway.
    Raising instead would cost the whole core its start (the 2026-08 security
    review), and with it the admin UI that is the only place to fix it.
    """
    roots = _transport_roots(layout)
    transport = declared_transport(candidate)
    if transport is None:
        # No manifest, unreadable manifest, no `[plugin]` table, or no
        # `transport` key. Discovery requires the field, so there is no default
        # to fall back on and guessing "stdio" would seed a plugin into a
        # directory chosen by a coin toss.
        logger.error(
            "bundled_plugin_unroutable", plugin=candidate.name, reason="transport_unreadable"
        )
        return None
    if transport not in roots:
        logger.error(
            "bundled_plugin_unroutable",
            plugin=candidate.name,
            reason="transport_unknown",
            transport=transport,
        )
        return None

    destination_root = roots[transport]
    if not (destination_root / candidate.name).exists():
        elsewhere = [
            root / candidate.name
            for name, root in roots.items()
            if name != transport and (root / candidate.name).exists()
        ]
        if elsewhere:
            # The plugin changed transport between versions and the operator
            # already has the old one installed. Moving their folder from one
            # plugin directory to the other is not the seeder's business: that
            # directory is appdata, the copy may be edited, and a "seed on first
            # run, never overwrite" rule cannot also be a rule that relocates
            # things. Seeding the new one beside the old would be worse still —
            # two folders of the same name is a duplicate discovery rejects, so
            # the operator would lose the working plugin to gain a broken pair.
            # So: leave it exactly as it is, and say why, by name, in the log.
            logger.info(
                "bundled_plugin_transport_changed",
                plugin=candidate.name,
                transport=transport,
                installed_at=[str(path) for path in elsewhere],
                action="left_in_place",
            )
            return None

    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        logger.error(
            "bundled_plugins_directory_unusable", path=str(destination_root), error=str(exc)
        )
        return None
    return destination_root


def install_bundled_plugins(
    layout: AppdataLayout, source: Path | str | None = None
) -> list[str]:
    """Copy the shipped plugins into appdata. Returns the names newly installed.

    Each plugin goes to the directory its own manifest's `transport` names:
    `stdio` to `plugins/`, `http` to `plugins-http.d/`. Discovery refuses a
    manifest found under the wrong one, so a shipped HTTP plugin seeded into
    `plugins/` is a plugin that is installed and can never load.

    Skips any plugin that was installed before and has since been removed —
    deleting a bundled plugin is a decision, and an upgrade that undoes it would
    be the assistant arguing with its operator.

    A plugin already present is refreshed only when the shipped version is
    strictly newer *and* the installed copy is untouched (PC-322). Refreshed
    names are logged rather than returned: they are not new installs, and the
    caller's line for the return value says "installed".

    **This function does not raise for anything it finds on disk** (the 2026-08 security review). It
    is called unwrapped while the core is being built, so an exception out of
    here is a core that never finishes starting — and therefore an admin UI that
    never starts either, which is the one place an operator could have put it
    right. Every filesystem answer below is allowed to be "no": an unreadable
    file, a folder whose permissions were changed, a marker file full of
    rubbish, a manifest that is not TOML, a manifest declaring a transport that
    is neither value. Each one costs that plugin its seeding or its refresh and
    nothing else — none of them reaches the caller — and each
    one is logged with the plugin's name so the operator can see which copy was
    left behind and why.
    """
    origin = bundled_source(source)
    if origin is None:
        return []

    record_root = layout.plugins
    try:
        record_root.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        # No plugins directory means no seeding — of either transport, because
        # this is also where the two records live and seeding without being able
        # to record it is how a plugin the operator deleted comes back. The core
        # still has a config, a persona and an admin UI, all of which are more
        # use to whoever has to fix the volume than a stack trace at start-up.
        logger.error(
            "bundled_plugins_directory_unusable", path=str(record_root), error=str(exc)
        )
        return []

    history = record_root / INSTALL_MARKER
    previously = _read_history(history)
    state_file = record_root / INSTALL_STATE
    state = _read_state(state_file)

    try:
        candidates = bundled_plugin_dirs(origin)
    except (OSError, ValueError) as exc:
        # The image's own copy, so this is close to impossible — and still not
        # worth the core refusing to start over.
        logger.error("bundled_plugins_source_unreadable", source=str(origin), error=str(exc))
        return []

    installed: list[str] = []
    refreshed: list[str] = []
    state_changed = False
    for candidate in candidates:
        try:
            destination_root = _destination_root(candidate, layout)
            if destination_root is None:
                continue  # `_destination_root` has already named it in the log
            outcome = _seed_one(candidate, destination_root, previously, state)
        except (OSError, ValueError) as exc:
            # The backstop behind every guard below, so that the promise in the
            # docstring is a property of this loop rather than a checklist that
            # has to stay complete as the body changes.
            logger.error(
                "bundled_plugin_skipped", plugin=candidate.name, error=str(exc)
            )
            continue
        if outcome in ("installed", "installed_unrecorded"):
            installed.append(candidate.name)
        elif outcome in ("refreshed", "refreshed_unrecorded"):
            refreshed.append(candidate.name)
        state_changed = state_changed or outcome in ("installed", "refreshed", "recorded")

    if installed:
        try:
            history.write_text(
                "\n".join(sorted(previously | set(installed))) + "\n", encoding="utf-8"
            )
        except (OSError, ValueError) as exc:
            # Losing the marker means a plugin the operator later deletes could
            # be seeded again. Worth saying loudly; not worth a dead core.
            logger.error(
                "bundled_plugin_marker_unwritable", path=str(history), error=str(exc)
            )
        logger.info("bundled_plugins_installed", plugins=installed, source=str(origin))
    if refreshed:
        logger.info("bundled_plugins_refreshed", plugins=refreshed, source=str(origin))
    if state_changed:
        _write_state(state_file, state)
    return installed


def _read_history(history: Path) -> set[str]:
    """The names seeded before now. Unreadable reads as empty.

    Costs at worst one re-seed of a plugin the operator deleted, which is
    visible and reversible — where raising costs the whole core its start.
    """
    try:
        return set(history.read_text(encoding="utf-8").split())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        logger.error("bundled_plugin_marker_unreadable", path=str(history), error=str(exc))
        return set()


def _seed_one(
    candidate: Path,
    destination_root: Path,
    previously: set[str],
    state: dict[str, dict[str, str]],
) -> str:
    """Seed or refresh one plugin. Returns what happened, and mutates `state`.

    ``"installed"``, ``"refreshed"``, ``"recorded"`` (a record was bootstrapped
    for an existing copy) or ``"skipped"``. The two ``…_unrecorded`` outcomes are
    the same events with no refresh record written, because the shipped folder
    stopped reading between doing the work and hashing it — the plugin is on
    disk and is reported as such, and the missing record costs it future
    refreshes rather than misreporting what happened.

    Pulled out of the loop so that one plugin's bad day is contained to one
    iteration by construction.
    """
    target = destination_root / candidate.name

    if not target.exists():
        if candidate.name in previously:
            return "skipped"
        try:
            shutil.copytree(candidate, target)
        except (OSError, ValueError) as exc:
            # One plugin that will not copy must not stop the core starting.
            logger.error(
                "bundled_plugin_install_failed", plugin=candidate.name, error=str(exc)
            )
            return "skipped"
        digests = safe_plugin_file_digests(candidate)
        if digests is None:
            # Copied, but we cannot say what we copied. The plugin is installed
            # and works, and is reported as installed; it simply never qualifies
            # for a refresh until a start can read the shipped folder.
            logger.info(
                "bundled_plugin_refresh_skipped",
                plugin=candidate.name,
                reason="shipped_files_unreadable",
            )
            return "installed_unrecorded"
        state[candidate.name] = digests
        return "installed"

    recorded = state.get(candidate.name)
    if recorded is None:
        shipped = safe_plugin_file_digests(candidate)
        if shipped is not None and _is_unmodified(target, shipped) is True:
            # Installed before there was a record, and still identical to what
            # is shipped, so a record can be written honestly. Without this
            # bootstrap every copy installed before PC-322 would be locked out
            # of refreshes for good.
            state[candidate.name] = shipped
            return "recorded"

    decision = _refresh_decision(candidate, target, recorded)
    if decision == "not_newer":
        # The overwhelmingly common case, every single start. Not a log line.
        return "skipped"
    if decision != "refresh":
        logger.info(
            "bundled_plugin_refresh_skipped", plugin=candidate.name, reason=decision
        )
        return "skipped"
    try:
        _apply_refresh(candidate, target, recorded or {})
    except (OSError, ValueError) as exc:
        logger.error(
            "bundled_plugin_refresh_failed", plugin=candidate.name, error=str(exc)
        )
        return "skipped"
    digests = safe_plugin_file_digests(candidate)
    if digests is None:
        # The refresh happened — saying otherwise would be the log denying what
        # is on disk. Only the record is missing.
        logger.info(
            "bundled_plugin_refresh_skipped",
            plugin=candidate.name,
            reason="shipped_files_unreadable",
        )
        return "refreshed_unrecorded"
    state[candidate.name] = digests
    return "refreshed"
