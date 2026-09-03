"""Atomic, owner-only JSON files under appdata — shared by the two auth stores.

Both files here hold credential material (a password hash, a session token's
digest), so both want the same two properties, and one implementation is how
they keep having them:

* **Atomic.** A torn account file locks the household out of its own assistant,
  and the recovery for that is the break-glass bypass PC-294 protects.
* **``0o600``.** Least privilege inside a volume only the core should read
  (spec section 7). Best effort: on a Windows development machine ``chmod`` is
  close to a no-op, and failing a write over it would break the developer
  machine while protecting nothing.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from personacore.audit.logging import get_logger

logger = get_logger(__name__)


class AuthStoreError(RuntimeError):
    """The file itself is unusable. Message text reaches an operator, so it
    says what to do rather than only what failed."""


def read_json(path: Path, *, kind: str) -> dict[str, Any] | None:
    """Parse the file, or ``None``.

    ``None`` covers "not there yet" and "unreadable" alike, and every caller
    treats it as *no records* — which fails closed: nobody can sign in, rather
    than everybody can.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.error(f"{kind}_file_invalid", error=str(exc), path=str(path))
        return None
    if not isinstance(raw, dict):
        logger.error(f"{kind}_file_invalid", error="unexpected shape", path=str(path))
        return None
    return raw


def write_json(path: Path, payload: dict[str, Any], *, kind: str) -> None:
    """Replace the file atomically, owner-readable only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _restrict(temporary, kind=kind)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise AuthStoreError(
            f"Could not write {path}: {exc.strerror or exc}. Check the appdata "
            "volume is mounted and writable by the user the container runs as."
        ) from exc


def _restrict(path: Path, *, kind: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.warning(f"{kind}_file_permissions", error=str(exc), path=str(path))


__all__ = ["AuthStoreError", "read_json", "write_json"]
