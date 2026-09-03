"""Bringing a pre-ADR-0025 secrets directory up to date — ADR-0040.

Moved out of ``personacore.server``. One job: put the core's own secrets where
the namespaced store expects them, and say plainly what moved and what did not.

It runs on every start because a container upgrade is ``docker compose pull &&
up -d`` and nothing runs a migration step in between. It is deliberately not
fatal — see the docstring below — which is why it belongs beside the other
pieces rather than in the assembly that calls it.
"""

from __future__ import annotations

import structlog

from personacore.config import AppdataLayout, SecretStore
from personacore.config.secrets import SecretError

log = structlog.get_logger(__name__)


def _migrate_secrets(layout: AppdataLayout) -> None:
    """Move pre-ADR-0025 flat secrets into the core namespace, and say what moved.

    Called on every start rather than once, because there is no "once" a
    container can be relied on to have had: an upgrade is ``docker compose pull
    && up -d`` and nothing runs a migration step in between. ``migrate()`` is
    deterministic and idempotent, so a second pass over an already-migrated
    store is a directory listing that finds nothing.

    Every secret that existed before namespacing was put in ``secrets/`` by hand
    for the core's own use, so the core namespace is where it goes. **A plugin
    that named one of these stops receiving it, and that is the point**
    (ADR-0025): the name was never the plugin's to claim.

    Two log lines, both names only — a value never reaches a log (ADR-0025
    section 5), and these lines are the reason to say that out loud here.
    ``unmigrated()`` is the second one: a name that exists both flat and in the
    core namespace is left exactly where it is, because only an operator can say
    which of the two is current, and silently replacing a working credential is
    worse than leaving one to be looked at. That case is invisible unless it is
    logged plainly, so it is logged at warning with the names in it.

    A store that cannot be migrated at all does not stop the assistant starting:
    the credentials it holds are read further down, and each of those failures
    already degrades one dependency and names the secret.
    """
    store = SecretStore(layout)
    try:
        moved = store.migrate()
        stranded = store.unmigrated()
    except SecretError as exc:
        log.error("secrets_migration_failed", error=str(exc))
        return
    if moved:
        log.info("secrets_migrated", secrets=moved, owner="core")
    if stranded:
        log.warning(
            "secrets_not_migrated",
            secrets=stranded,
            detail=(
                "These secrets exist both in appdata/secrets/ and in "
                "appdata/secrets/core/. The flat copy was left alone rather than "
                "overwriting the one already in the core namespace. Delete "
                "whichever is out of date."
            ),
        )
