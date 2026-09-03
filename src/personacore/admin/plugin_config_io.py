"""Reading and writing one plugin's own ``config.toml`` — spec section 5.1.

Spec section 5.1 is unambiguous about where a plugin's settings live: "**Per-plugin
config lives in the plugin's own folder.** The core never stores plugin settings
centrally; the admin UI edits the plugin's own config file with validation." This
module is the "edits ... with validation" half. Section 9 lists the same thing as
an admin screen — "per-plugin config editing with validation and plain-English
errors" — and ADR-0013 promises a plugin is "enabled and configured there".

Four rules, and the first is the one that gets broken by a well-meaning later
change:

* **The core validates syntax and nothing else.** A plugin's settings mean
  whatever the plugin says they mean; spec section 5.1 gives the shape to the
  plugin, and ``plugins/_template/config.toml`` tells its author to validate it
  themselves because "the core will not: it does not know what your settings
  mean". So this module checks the text parses as TOML and stops there. **Do not
  add a schema, a key whitelist or a type check here** — the core has no
  business knowing a weather plugin has a ``forecast_days``, and the day it does
  is the day a plugin cannot ship a new setting without a core release.
* **Text in, text out.** The file is carried verbatim rather than parsed and
  re-serialised, because its comments are the field help (again, see the
  template) and a round trip through ``tomli_w`` would delete every one of them.
* **The write is atomic**, for the reason
  :func:`personacore.admin.config_io.write_config` gives about ``core.toml``: a
  half-written settings file is worse than an unedited one, and here it also
  turns a working plugin into a load failure at the next scan.
* **A secret value is never rendered and never written** (spec section 7). A
  plugin declares the secrets it needs by name in its manifest under
  ``permissions.secrets`` and the core hands them over at runtime; this file is
  backed up with appdata and readable by anyone who can open the admin UI.

Path handling is deliberately thin: the plugin folder and the file inside it are
both put through :meth:`personacore.config.appdata.AppdataLayout.require_inside`,
and the resolved file must be a direct child of the resolved folder. There is no
new path logic here to get wrong.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from personacore.admin.config_io import (
    SECRET_REFERENCE_SUFFIX,
    ConfigRejected,
    secret_reference_names,
    secret_value_keys,
)
from personacore.admin.models import ConfigProblem, PluginConfigResponse
from personacore.config.appdata import AppdataError, AppdataLayout
from personacore.plugins.discovery import CONFIG_FILENAME
from personacore.plugins.packages import require_plugin_name

CREDENTIAL_HINT = (
    "A plugin declares the secrets it needs by name in its manifest.toml under "
    "permissions.secrets, and the core hands the value over when it starts. "
    "config.toml is backed up and readable by anyone who can open this page, so "
    "it never holds the value itself (spec section 7)."
)
"""Said whenever a plugin config is refused for carrying a credential.

One sentence rather than "not allowed", because the operator who put an API key
in there did it for a reason and needs to know where it goes instead.
"""


class PluginConfigNotFound(ConfigRejected):
    """No plugin of that name is installed, so it has no config to edit.

    A subclass rather than a flag so the endpoint can answer ``404`` for "not
    installed" and ``500`` for "installed but unreadable" without inspecting a
    message.
    """


class PluginConfigInvalid(ConfigRejected):
    """The submitted text is not well-formed TOML, so nothing was written.

    Its own type so the endpoint answers ``422`` — the operator sent something
    that cannot be stored — rather than ``500``, which would read as "the core
    broke" when the core is working exactly as intended.
    """


class PluginConfigUnsafe(ConfigRejected):
    """The file cannot be shown or written: it holds a secret value, or it
    leads outside the plugin's own folder (spec sections 5.1 and 7).

    Answered as ``409``: the request is well-formed and the core is healthy, but
    the state of the file conflicts with a rule that is not negotiable.
    """


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def plugin_config_path(layout: AppdataLayout, name: str) -> Path:
    """Resolve ``<appdata>/plugins[-http]/<name>/config.toml``, or refuse.

    Both transports are searched because a plugin's folder is under
    ``plugins/`` or ``plugins-http/`` depending on how it talks (spec Appendix
    B), and an operator editing settings does not think in transports.

    Three checks, none of them new logic:

    1. The name is put through the manifest's own name rule before it is joined
       onto anything (spec section 7 — everything from outside is untrusted).
    2. The folder and the file are resolved through ``require_inside``, which
       follows symlinks, so a ``config.toml`` symlinked at ``/etc/passwd`` is
       outside appdata and refused.
    3. The resolved file must sit *directly* inside the resolved plugin folder,
       which catches the escape ``require_inside`` cannot see: a link that stays
       within appdata but points at another plugin's settings, or at
       ``core.toml``.
    """
    try:
        require_plugin_name(name)
    except ValueError as exc:
        raise PluginConfigNotFound(
            str(exc),
            [ConfigProblem(key="(plugin)", problem=str(exc))],
        ) from exc

    for root in (layout.plugins, layout.plugins_http):
        directory = root / name
        if directory.is_dir():
            break
    else:
        raise PluginConfigNotFound(
            f"No plugin named {name!r} is installed, so it has no settings to edit.",
            [
                ConfigProblem(
                    key="(plugin)",
                    problem=f"{name!r} was not found in this core's plugin folders.",
                    hint=(
                        "Install the plugin first, then open its settings. The plugin "
                        "list on this page shows what is installed."
                    ),
                )
            ],
        )

    try:
        resolved_directory = layout.require_inside(
            directory, what=f"The plugin folder for {name!r}"
        )
        resolved = layout.require_inside(
            resolved_directory / CONFIG_FILENAME,
            what=f"The settings file for {name!r}",
        )
    except AppdataError as exc:
        raise PluginConfigUnsafe(str(exc), [ConfigProblem(key="(path)", problem=str(exc))]) from exc

    if resolved.parent != resolved_directory:
        raise PluginConfigUnsafe(
            f"The settings file for {name!r} does not sit inside the plugin's own "
            "folder, so it will not be read or written from here.",
            [
                ConfigProblem(
                    key="(path)",
                    problem=(
                        f"{CONFIG_FILENAME} in {name!r} leads somewhere outside that "
                        "plugin's folder."
                    ),
                    hint=(
                        "A plugin's settings live in the plugin's own folder and "
                        "nowhere else (spec section 5.1). Replace the link with a real "
                        "file and try again."
                    ),
                )
            ],
        )
    return resolved


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_plugin_config(layout: AppdataLayout, name: str) -> PluginConfigResponse:
    """One plugin's ``config.toml`` as text, plus whether it currently parses.

    A file that does *not* parse is still returned, with ``valid`` false and the
    syntax error in ``problem``: the plugin whose config is broken is precisely
    the one an operator needs to open the editor on, and refusing to show it
    would send them to a shell — the friction ADR-0010 exists to remove.
    """
    path = plugin_config_path(layout, name)
    if not path.exists():
        return PluginConfigResponse(
            plugin=name,
            path=path.as_posix(),
            exists=False,
            content="",
            valid=True,
            problem=None,
        )

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigRejected(
            f"{path.as_posix()} is not text this core can read: it is not UTF-8. "
            "Nothing was changed.",
            [
                ConfigProblem(
                    key="(file)",
                    problem=f"{path.as_posix()} could not be decoded as UTF-8.",
                    hint="Save the file as UTF-8 text and reload the plugin list.",
                )
            ],
        ) from exc
    except OSError as exc:
        raise ConfigRejected(
            f"{path.as_posix()} could not be read: {exc.strerror or exc}. Check the "
            "appdata volume is mounted and readable.",
            [ConfigProblem(key="(file)", problem=str(exc))],
        ) from exc

    document, problem = parse_plugin_toml(content, path)
    _refuse_secret_values(document, content, path)
    return PluginConfigResponse(
        plugin=name,
        path=path.as_posix(),
        exists=True,
        content=content,
        valid=problem is None,
        problem=problem,
        secret_references=secret_reference_names(document or {}),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def parse_plugin_toml(content: str, path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse, returning ``(document, None)`` or ``(None, plain-English problem)``.

    Never raises, because both callers want the failure as a *value*: reading
    shows it above the editor, writing turns it into a refusal. ``tomllib``'s
    own message already names what it expected and the line and column it
    expected it at, so it is quoted rather than reworded — one wording of a
    syntax error is enough (spec section 9).
    """
    try:
        return tomllib.loads(content), None
    except tomllib.TOMLDecodeError as exc:
        return None, (
            f"{path.name} is not valid TOML: {exc}. "
            "TOML settings look like key = \"value\", with section headings in "
            "square brackets."
        )
    except ValueError as exc:  # pragma: no cover - tomllib raises TOMLDecodeError
        return None, f"{path.name} could not be read as TOML: {exc}."


def top_level_keys(content: str) -> list[str]:
    """The document's top-level key and table names, for an audit record.

    Names only, never values: the audit store is backed up and read by whoever
    is investigating later, and a plugin's settings are not theirs to collect
    (spec section 7). Unparseable text yields an empty list rather than an
    error — this is a description of a change that already succeeded.
    """
    document, _ = parse_plugin_toml(content, Path(CONFIG_FILENAME))
    return sorted(str(key) for key in (document or {}))


def validate_plugin_toml(content: str, path: Path) -> dict[str, Any]:
    """Well-formed TOML, or :class:`ConfigRejected` naming the problem.

    **This is the whole of the core's validation, on purpose.** What the keys
    mean is the plugin's business (spec section 5.1): the core cannot know that
    ``forecast_days`` must be 1-7, the plugin can, and the template tells its
    author to check it at startup so the admin UI shows "greeting must not be
    empty" rather than a stack trace. Adding a schema here would make every new
    plugin setting a core release.
    """
    document, problem = parse_plugin_toml(content, path)
    if problem is not None:
        raise PluginConfigInvalid(
            f"Those settings were not saved: {problem} {path.as_posix()} is unchanged.",
            [
                ConfigProblem(
                    key="(file)",
                    problem=problem,
                    hint=(
                        "Fix the line the message names and save again. Nothing has "
                        "been written, so the plugin is still running on its previous "
                        "settings."
                    ),
                )
            ],
        )
    assert document is not None  # noqa: S101 - narrowing; problem is None means parsed
    return document


def _refuse_secret_values(
    document: dict[str, Any] | None, content: str, path: Path
) -> None:
    """Refuse a config carrying what looks like a live credential (spec 7).

    Applied on the way *out* as well as in, for the reason
    ``config_io._assert_no_secret_values`` gives: a value that should never have
    reached the disk must not be handed onward to a browser, a screenshot or a
    support conversation either.

    When the file does not parse there is no document to walk, so the raw text
    is scanned for the same key names instead — a malformed file is exactly
    where a pasted key is most likely to be sitting.
    """
    offenders = (
        secret_value_keys(document) if document is not None else _secret_keys_in_text(content)
    )
    if not offenders:
        return
    raise PluginConfigUnsafe(
        f"{path.as_posix()} contains a setting that looks like a live secret, so it "
        "is not shown or edited here.",
        [
            ConfigProblem(
                key=key,
                problem=(
                    f"'{key}' looks like a credential rather than a setting. A plugin's "
                    "config.toml never holds one."
                ),
                hint=CREDENTIAL_HINT,
            )
            for key in offenders
        ],
    )


def _secret_keys_in_text(content: str) -> list[str]:
    """Credential-shaped key names in text that would not parse.

    Deliberately crude — it looks for ``name =`` at the start of a line and
    checks the name against the same list the parsed walk uses. It only ever
    causes a refusal, never an acceptance, so a false positive costs an
    operator one rename and a false negative leaves them exactly where the
    unparseable file already had them.
    """
    found: list[str] = []
    for line in content.splitlines():
        head, separator, _ = line.strip().partition("=")
        if not separator:
            continue
        key = head.strip().strip('"').strip("'")
        if key and not key.endswith(SECRET_REFERENCE_SUFFIX) and secret_value_keys({key: ""}):
            found.append(key)
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_plugin_config(layout: AppdataLayout, name: str, content: str) -> PluginConfigResponse:
    """Validate the text as TOML, then replace the plugin's ``config.toml``.

    Atomic, and the ordering is the point: the text is parsed *before* anything
    is opened for writing, so a syntax error leaves the file on disk untouched
    and the plugin running on the settings it already had. The new content then
    goes to a temporary file **in the same directory** — a rename is only atomic
    within one filesystem, and appdata is a mounted volume — is flushed and
    fsynced so the bytes are on the disk rather than in a cache, and is moved
    over the real file with :func:`os.replace`, which is atomic on both POSIX
    and Windows. A crash at any point leaves either the old file or the new one,
    never half of either.

    Returns the config as it reads back from disk, so the caller never has to
    guess whether what it sent is what got stored.
    """
    path = plugin_config_path(layout, name)
    document = validate_plugin_toml(content, path)
    _refuse_secret_values(document, content, path)

    temporary = path.with_name(path.name + ".new")
    try:
        # newline="" keeps the operator's line endings exactly as submitted:
        # Python would otherwise translate "\n" to "\r\n" on Windows, quietly
        # rewriting every line of a file the operator did not edit.
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        _discard(temporary)
        raise ConfigRejected(
            f"Those settings could not be saved to {path.as_posix()}: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and writable; "
            "the plugin is still running on its previous settings.",
            [ConfigProblem(key="(file)", problem=str(exc))],
        ) from exc

    return read_plugin_config(layout, name)


def _discard(path: Path) -> None:
    """Remove a half-written temporary file; never mask the original error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - the real failure is already being raised
        pass


__all__ = [
    "CREDENTIAL_HINT",
    "PluginConfigInvalid",
    "PluginConfigNotFound",
    "PluginConfigUnsafe",
    "parse_plugin_toml",
    "plugin_config_path",
    "read_plugin_config",
    "top_level_keys",
    "validate_plugin_toml",
    "write_plugin_config",
]
