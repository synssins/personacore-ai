"""Plugin discovery errors -- spec section 9.

Discovery never lets one bad plugin take the rest down (spec 5.1: "a bad
plugin never takes the core down", applied here at *load* time rather than at
runtime). Every failure -- malformed TOML, a manifest that fails schema
validation, a security violation -- is converted to a `PluginLoadFailure`
carrying a message written to be read verbatim in the admin UI by someone who
is not holding the source open (spec 9: "plain-English errors"). Nothing here
should ever surface a raw pydantic traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError


@dataclass(frozen=True)
class PluginLoadFailure:
    """One plugin directory or registration that failed to load.

    Discovery collects these alongside successful `PluginRecord`s so the
    admin UI can list a broken plugin next to its error instead of losing the
    whole scan.
    """

    source: Path
    """The manifest file, config file, or directory responsible for the failure."""

    name: str | None
    """Best-effort plugin name -- the directory name, if that much was
    recoverable before the failure. None if not even that is known."""

    message: str
    """Plain-English description naming the file and the offending key.
    Safe to show verbatim in the admin UI (spec 9)."""


class PluginRejected(ValueError):
    """A manifest parsed and validated fine on its own terms, but discovery
    still refuses to load it -- contract-version mismatch, wrong directory
    for its declared transport, and the like. Always caught by discovery and
    turned into a `PluginLoadFailure`; never meant to escape this package."""


class PluginSecurityError(PluginRejected):
    """Specifically: a spec section 7 violation -- path traversal, an
    absolute path, a symlink escape, a folder/name mismatch, or a manifest
    field reaching for the appdata secrets directory."""


def describe_validation_error(manifest_path: Path, exc: ValidationError) -> str:
    """Render a pydantic `ValidationError` as one plain-English line per
    error, each naming the manifest file and the offending key -- spec 9.

    Example: "plugins/weather/manifest.toml: 'plugin.transport' must be
    'stdio' or 'http', got 'stdout'" rather than a pydantic traceback.
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "top level"
        msg = err["msg"]
        # Pydantic v2 prefixes messages raised from a field_validator's
        # ValueError with "Value error, " -- redundant once we're already
        # naming the field, and not something a non-developer needs to see.
        prefix = "Value error, "
        if msg.startswith(prefix):
            msg = msg[len(prefix) :]
        parts.append(f"'{loc}' {msg}")
    joined = "; ".join(parts)
    return f"{manifest_path.as_posix()}: {joined}"
