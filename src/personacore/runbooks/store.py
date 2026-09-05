"""Runbooks on disk — ``working/contracts/runbook.md`` §3 ("Where") and §6.

Layout, one folder per plugin under ``<appdata>/runbooks/``::

    runbooks/<plugin>/<id>.yaml            the runbook itself
    runbooks/<plugin>/<id>.verdict.json    {"valid": bool, "problems": [...]},
                                            written at put/install time
    runbooks/<plugin>/<id>.bundled         present iff a plugin install put
                                            this runbook here, not an upload
    runbooks/<plugin>/prompts/*.md         every prompt file for every
                                            runbook of this plugin, shared —
                                            a runbook's own `prompt:` field
                                            names one of these relative to
                                            itself, e.g. "prompts/p1.md"
    runbooks/<plugin>/.enabled             present iff runbooks are switched
                                            on for this plugin (default off)

Two identities are deliberately never trusted for a path: the *uploaded
filename* and the *bundling plugin's own claim about itself*. A runbook's
storage name always comes from its own ``runbook:`` field, read after the
file is proven to at least be YAML holding a mapping — the same reasoning
``plugins/packages.py`` gives for never joining an uploaded filename onto a
path. ``plugin`` is checked against the manifest's own name pattern before it
is ever joined onto ``<appdata>/runbooks/``.

:meth:`RunbookStore.list` recomputes validity and compatibility **fresh, on
every call**, rather than trusting the persisted ``.verdict.json``. That file
exists so a problem survives being written down (contract §6 wants the
validation result *shown*, and something on disk is easier to show than
something that only ever existed inside one request) — but plugin facts move
under a runbook's feet (a plugin can be upgraded, switched off, uninstalled)
and a stored file's own validity cannot change, so the only source of truth
that stays honest across both is reading the file and asking the plugin host
again.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from personacore.audit import get_logger
from personacore.config.appdata import AppdataLayout
from personacore.contracts.manifest import _NAME_RE as _PLUGIN_NAME_RE
from personacore.plugins.bundled import is_strictly_newer
from personacore.runbooks.compat import PluginFacts, Verdict
from personacore.runbooks.compat import check as check_compat
from personacore.runbooks.schema import RUNBOOK_ID_RE
from personacore.runbooks.validate import ValidationError, validate_runbook

logger = get_logger(__name__)

MAX_RUNBOOK_BYTES = 2 * 1024 * 1024
"""Contract text: "refuse over 2 MB". Applied to the raw upload, and again to
the sum of what a zip's own headers and its actual bytes turn out to hold —
a zip small on disk can still be a bomb once unpacked."""

MAX_ZIP_ENTRIES = 200
"""A runbook is one file plus a handful of prompts, not an archive. Generous
enough for the seven-pass example in the contract to have three times over."""

PROMPTS_DIRNAME = "prompts"
ENABLED_FILENAME = ".enabled"
VERDICT_SUFFIX = ".verdict.json"
BUNDLED_SUFFIX = ".bundled"
YAML_SUFFIX = ".yaml"


class RunbookStoreError(ValueError):
    """A ``put``/``delete``/name was refused. Text is written for an operator,
    the same house rule every refusal in this codebase already follows."""


@dataclass(frozen=True)
class RunbookRecord:
    """One runbook, as the Runbooks screen would show it."""

    plugin: str
    id: str
    version: str
    title: str
    description: str
    bundled: bool
    path: Path
    valid: bool
    problems: list[str]
    verdict: Verdict
    requires: dict[str, str]
    """``requires.plugins`` from the file itself: plugin name -> version
    specifier (e.g. ``{"vesmark": ">=1.4.3"}``). Empty for a file that did
    not even validate — there is no trustworthy ``requires:`` to read off an
    invalid file the same way :func:`_peek_metadata` reads a best-effort
    version/title/description, since a malformed document may not parse as a
    mapping at all."""

    installed: dict[str, str | None]
    """The same plugin names as ``requires``, each mapped to what
    :meth:`PluginFacts.installed` reports right now — ``None`` when the
    plugin named in ``requires`` is not installed at all. Lets the Runbooks
    screen show Requires/Installed as versions side by side, the same facts
    :mod:`personacore.runbooks.compat` already checks, without changing
    :class:`~personacore.runbooks.compat.Verdict`."""


class RunbookStore:
    """Every runbook in appdata, plus the per-plugin runbooks switch."""

    def __init__(
        self,
        layout: AppdataLayout,
        facts: PluginFacts,
        settings_enabled: Callable[[], bool],
    ) -> None:
        self._layout = layout
        self._facts = facts
        self._settings_enabled = settings_enabled

    def core_enabled(self) -> bool:
        """The household-wide switch (``settings.runbooks.enabled``), as it
        is *right now* — read through the callable rather than captured once,
        so a save on the Core settings screen is seen on the next call with
        no restart."""
        return self._settings_enabled()

    # -- paths --------------------------------------------------------------

    def _plugin_dir(self, plugin: str) -> Path:
        _require_plugin_name(plugin)
        return self._layout.runbooks / plugin

    def _yaml_path(self, plugin: str, runbook_id: str) -> Path:
        return self._plugin_dir(plugin) / f"{runbook_id}{YAML_SUFFIX}"

    def _verdict_path(self, plugin: str, runbook_id: str) -> Path:
        return self._plugin_dir(plugin) / f"{runbook_id}{VERDICT_SUFFIX}"

    def _bundled_marker(self, plugin: str, runbook_id: str) -> Path:
        return self._plugin_dir(plugin) / f"{runbook_id}{BUNDLED_SUFFIX}"

    def _enabled_path(self, plugin: str) -> Path:
        return self._plugin_dir(plugin) / ENABLED_FILENAME

    def _prompts_dir(self, plugin: str) -> Path:
        return self._plugin_dir(plugin) / PROMPTS_DIRNAME

    # -- the per-plugin switch (contract §1.10) ------------------------------

    def plugin_enabled(self, plugin: str) -> bool:
        """Default off: an absent ``.enabled`` file means off, not unknown."""
        return self._enabled_path(plugin).is_file()

    def set_plugin_enabled(self, plugin: str, value: bool) -> None:
        path = self._enabled_path(plugin)
        if value:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    # -- listing --------------------------------------------------------------

    def list(self) -> list[RunbookRecord]:
        """Every plugin dir, sorted by plugin then id."""
        root = self._layout.runbooks
        if not root.is_dir():
            return []
        records: list[RunbookRecord] = []
        for plugin_dir in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
            plugin = plugin_dir.name
            for yaml_path in sorted(plugin_dir.glob(f"*{YAML_SUFFIX}")):
                runbook_id = yaml_path.stem
                records.append(self._record_for(plugin, runbook_id, yaml_path))
        return records

    def _record_for(self, plugin: str, runbook_id: str, yaml_path: Path) -> RunbookRecord:
        bundled = self._bundled_marker(plugin, runbook_id).is_file()
        try:
            text = yaml_path.read_text(encoding="utf-8")
        except OSError as exc:
            return RunbookRecord(
                plugin=plugin,
                id=runbook_id,
                version="",
                title="",
                description="",
                bundled=bundled,
                path=yaml_path,
                valid=False,
                problems=[f"the runbook file could not be read: {exc.strerror or exc}"],
                verdict=Verdict(ok=False, reasons=[]),
                requires={},
                installed={},
            )

        prompts = self._load_prompts(plugin)
        try:
            runbook = validate_runbook(text, prompts)
        except ValidationError as exc:
            version, title, description = _peek_metadata(text)
            return RunbookRecord(
                plugin=plugin,
                id=runbook_id,
                version=version,
                title=title,
                description=description,
                bundled=bundled,
                path=yaml_path,
                valid=False,
                problems=list(exc.messages),
                verdict=Verdict(ok=False, reasons=[]),
                requires={},
                installed={},
            )

        verdict = check_compat(runbook, self._facts, plugin_runbooks_on=self.plugin_enabled)
        requires = dict(runbook.requires.plugins)
        installed = {name: self._facts.installed(name) for name in requires}
        return RunbookRecord(
            plugin=plugin,
            id=runbook_id,
            version=runbook.version,
            title=runbook.title,
            description=runbook.description,
            bundled=bundled,
            path=yaml_path,
            valid=True,
            problems=[],
            verdict=verdict,
            requires=requires,
            installed=installed,
        )

    def _load_prompts(self, plugin: str) -> dict[str, str]:
        prompts_dir = self._prompts_dir(plugin)
        if not prompts_dir.is_dir():
            return {}
        plugin_dir = self._plugin_dir(plugin)
        found: dict[str, str] = {}
        for path in sorted(prompts_dir.rglob("*.md")):
            if not path.is_file():
                continue
            relative = path.relative_to(plugin_dir).as_posix()
            try:
                found[relative] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return found

    # -- put ------------------------------------------------------------------

    def put(self, plugin: str, filename: str, data: bytes) -> RunbookRecord:
        """Store one runbook, uploaded as a ``.yaml`` or a ``.zip``.

        The storage name is always the parsed ``runbook:`` id, never the
        uploaded ``filename`` — the same reasoning ``plugins/packages.py``
        gives for never joining an uploaded name onto a path. Two uploads of
        one id keep whichever ``version:`` is newer (contract §2); the older
        one is refused silently by being a no-op, not by raising, since
        nothing is actually wrong with it.
        """
        _require_plugin_name(plugin)
        if len(data) > MAX_RUNBOOK_BYTES:
            raise RunbookStoreError(
                f"That file is {_human_bytes(len(data))}, over the "
                f"{_human_bytes(MAX_RUNBOOK_BYTES)} limit for a runbook. Nothing "
                "was stored."
            )

        lower = filename.lower()
        if lower.endswith((".yaml", ".yml")):
            text = _decode_text(data, what="The runbook file")
            new_prompts: dict[str, str] = {}
        elif lower.endswith(".zip"):
            text, new_prompts = _extract_zip_upload(data)
        else:
            raise RunbookStoreError(
                f"{filename!r} is not a .yaml or a .zip. Upload the runbook file "
                "itself, or a .zip holding the runbook's <id>.yaml plus its "
                "prompts/ folder."
            )

        runbook_id = _peek_id(text)
        if runbook_id is None:
            raise RunbookStoreError(
                "The file has no valid 'runbook:' id at its top level (lowercase "
                "letters, digits and hyphens, 1-40 characters), so there is no "
                "name to store it under. Nothing was stored."
            )

        plugin_dir = self._plugin_dir(plugin)
        yaml_path = plugin_dir / f"{runbook_id}{YAML_SUFFIX}"

        if yaml_path.is_file():
            existing_version = _peek_metadata(_safe_read(yaml_path))[0]
            incoming_version = _peek_metadata(text)[0]
            if is_strictly_newer(incoming_version, existing_version) is False:
                # Not newer (older, equal, or unknown either way): the file
                # already stored is kept exactly as it is.
                return self._record_for(plugin, runbook_id, yaml_path)

        try:
            plugin_dir.mkdir(parents=True, exist_ok=True)
            for relative, content in new_prompts.items():
                target = plugin_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            yaml_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise RunbookStoreError(
                f"The runbook could not be stored: {exc.strerror or exc}."
            ) from exc

        # An upload replaces whatever was there, including a bundled marker —
        # the content on disk is now the operator's, not the plugin's own.
        self._bundled_marker(plugin, runbook_id).unlink(missing_ok=True)

        prompts = self._load_prompts(plugin)
        try:
            validate_runbook(text, prompts)
            problems: list[str] = []
        except ValidationError as exc:
            problems = list(exc.messages)
        self._write_verdict(plugin, runbook_id, valid=not problems, problems=problems)

        return self._record_for(plugin, runbook_id, yaml_path)

    # -- delete -----------------------------------------------------------------

    def delete(self, plugin: str, runbook_id: str) -> bool:
        """Remove one runbook. Returns whether it was there to remove.

        Shared prompt files are left alone: they belong to the plugin, not to
        one runbook, and another of the plugin's runbooks may still use them.
        """
        _require_plugin_name(plugin)
        yaml_path = self._yaml_path(plugin, runbook_id)
        existed = yaml_path.is_file()
        yaml_path.unlink(missing_ok=True)
        self._verdict_path(plugin, runbook_id).unlink(missing_ok=True)
        self._bundled_marker(plugin, runbook_id).unlink(missing_ok=True)
        return existed

    # -- bundled installs (contract §6, "validated on plugin install") ----------

    def install_bundled(self, plugin: str, plugin_dir: Path) -> list[RunbookRecord]:
        """Copy ``plugin_dir/runbooks/*.yaml`` (+ ``runbooks/prompts/``) in.

        Never raises. A plugin install must not be blocked, still less
        rolled back, by a runbook author's mistake — contract §6: "a
        failure is a warning, not a refusal." Every file that cannot even be
        read is skipped and logged; every file that reads but does not
        validate is still installed, with its problems recorded and
        returned on its record.
        """
        _require_plugin_name(plugin)
        source_root = Path(plugin_dir) / "runbooks"
        if not source_root.is_dir():
            return []

        dest_dir = self._plugin_dir(plugin)
        source_prompts = source_root / PROMPTS_DIRNAME
        if source_prompts.is_dir():
            for source_file in sorted(source_prompts.rglob("*.md")):
                if not source_file.is_file():
                    continue
                try:
                    content = source_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning(
                        "runbook_bundled_prompt_unreadable",
                        plugin=plugin,
                        file=str(source_file),
                        error=str(exc),
                    )
                    continue
                # `source_root` is the plugin's own `runbooks/` folder, so a
                # file under `runbooks/prompts/x.md` is already relative to it
                # as `prompts/x.md` — exactly the shared layout this store
                # keeps at `<appdata>/runbooks/<plugin>/prompts/x.md`.
                relative = source_file.relative_to(source_root).as_posix()
                target = dest_dir / relative
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                except OSError as exc:
                    logger.warning(
                        "runbook_bundled_prompt_install_failed",
                        plugin=plugin,
                        file=str(source_file),
                        error=str(exc),
                    )

        records: list[RunbookRecord] = []
        for index, source_yaml in enumerate(sorted(source_root.glob(f"*{YAML_SUFFIX}"))):
            try:
                text = source_yaml.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning(
                    "runbook_bundled_unreadable",
                    plugin=plugin,
                    file=str(source_yaml),
                    error=str(exc),
                )
                continue

            runbook_id = _peek_id(text) or _fallback_id(source_yaml, index)
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = dest_dir / f"{runbook_id}{YAML_SUFFIX}"
                target.write_text(text, encoding="utf-8")
                (dest_dir / f"{runbook_id}{BUNDLED_SUFFIX}").write_text("", encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "runbook_bundled_install_failed",
                    plugin=plugin,
                    runbook=runbook_id,
                    error=str(exc),
                )
                continue

            prompts = self._load_prompts(plugin)
            try:
                validate_runbook(text, prompts)
                problems: list[str] = []
            except ValidationError as exc:
                problems = list(exc.messages)
            self._write_verdict(plugin, runbook_id, valid=not problems, problems=problems)
            records.append(self._record_for(plugin, runbook_id, target))

        return records

    def _write_verdict(
        self, plugin: str, runbook_id: str, *, valid: bool, problems: list[str]
    ) -> None:
        path = self._verdict_path(plugin, runbook_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"valid": valid, "problems": problems}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - the yaml write just succeeded
            logger.warning(
                "runbook_verdict_unwritable", plugin=plugin, runbook=runbook_id, error=str(exc)
            )


# ---------------------------------------------------------------------------
# Small helpers with no state of their own
# ---------------------------------------------------------------------------


def _require_plugin_name(name: str) -> None:
    if not isinstance(name, str) or not _PLUGIN_NAME_RE.fullmatch(name):
        raise RunbookStoreError(
            f"{name!r} is not a plugin name. Plugin names are lowercase letters, "
            "digits and hyphens, starting with a letter."
        )


def _decode_text(data: bytes, *, what: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunbookStoreError(f"{what} is not valid UTF-8 text.") from exc


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _peek_id(text: str) -> str | None:
    """The parsed ``runbook:`` id, or ``None`` if the file will not even say
    what it is. Deliberately cheaper than full validation — this is used to
    decide *where a file goes*, before anything about its content is
    trusted."""
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    candidate = raw.get("runbook")
    if isinstance(candidate, str) and RUNBOOK_ID_RE.fullmatch(candidate):
        return candidate
    return None


def _peek_metadata(text: str) -> tuple[str, str, str]:
    """Best-effort ``(version, title, description)`` for a runbook that has
    not passed validation, so the Runbooks screen still has something to show
    beside its problems rather than three blank cells."""
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError:
        return ("", "", "")
    if not isinstance(raw, dict):
        return ("", "", "")
    version = raw.get("version")
    title = raw.get("title")
    description = raw.get("description")
    return (
        version if isinstance(version, str) else "",
        title if isinstance(title, str) else "",
        description if isinstance(description, str) else "",
    )


def _fallback_id(path: Path, index: int) -> str:
    """A storage id for a bundled runbook whose own ``runbook:`` field could
    not be trusted — used only so :meth:`RunbookStore.install_bundled` never
    has to skip a file solely for lacking one; the invalid id itself is one
    more thing :func:`~personacore.runbooks.validate.validate_runbook` will
    go on to report."""
    candidate = re.sub(r"[^a-z0-9-]", "-", path.stem.lower()).strip("-")[:40]
    if candidate and RUNBOOK_ID_RE.fullmatch(candidate):
        return candidate
    return f"bundled-runbook-{index}"


def _check_zip_member_name(name: str) -> None:
    if not name or "\x00" in name:
        raise RunbookStoreError(
            "The zip contains an entry with no usable name. Nothing was stored."
        )
    if "\\" in name:
        raise RunbookStoreError(f"The zip entry {name!r} contains a backslash. Nothing was stored.")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise RunbookStoreError(f"The zip entry {name!r} is an absolute path. Nothing was stored.")
    if any(part in ("..", ".") for part in PurePosixPath(name).parts):
        raise RunbookStoreError(
            f"The zip entry {name!r} contains a '..' segment. Nothing was stored."
        )


def _extract_zip_upload(data: bytes) -> tuple[str, dict[str, str]]:
    """The runbook's text and its prompt files, from a ``.zip`` holding
    ``<id>.yaml`` at the top level plus ``prompts/*.md`` — the only two
    shapes accepted. Every member's path is checked before anything is read,
    and the running total of actual bytes is capped independently of
    whatever the archive's own headers claim."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise RunbookStoreError(f"That file is not a readable zip archive ({exc}).") from exc

    with archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) > MAX_ZIP_ENTRIES:
            raise RunbookStoreError(
                f"The zip holds {len(members)} entries, over the {MAX_ZIP_ENTRIES} "
                "allowed for a runbook."
            )

        yaml_members: list[zipfile.ZipInfo] = []
        prompt_members: list[zipfile.ZipInfo] = []
        declared_total = 0
        for member in members:
            name = member.filename.replace("\\", "/")
            _check_zip_member_name(member.filename)
            declared_total += member.file_size
            if declared_total > MAX_RUNBOOK_BYTES:
                raise RunbookStoreError(
                    f"The zip unpacks to more than {_human_bytes(MAX_RUNBOOK_BYTES)}. "
                    "Nothing was stored."
                )
            parts = PurePosixPath(name).parts
            if len(parts) == 1 and name.lower().endswith(YAML_SUFFIX):
                yaml_members.append(member)
            elif len(parts) >= 2 and parts[0] == PROMPTS_DIRNAME and name.lower().endswith(".md"):
                prompt_members.append(member)
            else:
                raise RunbookStoreError(
                    f"The zip entry {member.filename!r} is neither the runbook's "
                    "own <id>.yaml at the top level nor a file under "
                    "prompts/*.md. Nothing was stored."
                )

        if len(yaml_members) != 1:
            raise RunbookStoreError(
                "The zip must hold exactly one <id>.yaml at its top level; found "
                f"{len(yaml_members)}. Nothing was stored."
            )

        actual_total = 0
        text_bytes = archive.read(yaml_members[0])
        actual_total += len(text_bytes)
        if actual_total > MAX_RUNBOOK_BYTES:
            raise RunbookStoreError(
                f"The zip unpacks to more than {_human_bytes(MAX_RUNBOOK_BYTES)}. "
                "Nothing was stored."
            )
        text = _decode_text(text_bytes, what="The runbook file inside the zip")

        prompts: dict[str, str] = {}
        for member in prompt_members:
            content_bytes = archive.read(member)
            actual_total += len(content_bytes)
            if actual_total > MAX_RUNBOOK_BYTES:
                raise RunbookStoreError(
                    f"The zip unpacks to more than {_human_bytes(MAX_RUNBOOK_BYTES)}. "
                    "Nothing was stored."
                )
            relative = PurePosixPath(member.filename.replace("\\", "/")).as_posix()
            prompts[relative] = _decode_text(
                content_bytes, what=f"The prompt file {relative!r} inside the zip"
            )

        return text, prompts


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - the loop always returns


__all__ = [
    "MAX_RUNBOOK_BYTES",
    "RunbookRecord",
    "RunbookStore",
    "RunbookStoreError",
]
