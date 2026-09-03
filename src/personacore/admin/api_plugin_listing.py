"""What the core knows about the plugins on disk, and the scan behind it.

Split out of :mod:`personacore.admin.routes` (ADR-0040). This module owns one
question — "what is installed, and how is each one getting on" — and answers it
for the JSON API, for the health dashboard and for the designed UI, which is
why it sits below all three rather than inside any of them.

It deliberately does **not** own the routes that change a plugin. Installing,
switching on and uninstalling live in :mod:`personacore.admin.api_plugins`;
what is here only reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime

from personacore.admin.models import (
    HealthState,
    PluginFailureView,
    PluginListing,
    PluginView,
)
from personacore.admin.protocols import (
    PluginHealthSource,
    SecretNameSource,
)
from personacore.audit import get_logger
from personacore.config.appdata import AppdataLayout
from personacore.plugins.discovery import DiscoveryResult, PluginDiscovery
from personacore.plugins.packages import (
    PluginStateError,
    disabled_state_path,
    read_disabled_plugins,
)

logger = get_logger(__name__)


DISABLED_PLUGIN_DETAIL = (
    "Switched off in the admin interface. Its folder and its settings are still "
    "here; it is not running, and its tools are not offered to the assistant or "
    "callable by anything."
)
"""Shown against a plugin the operator disabled (ADR-0013).

Said in full rather than as the word "disabled" because the row also appears on
the health dashboard, and a state with no explanation next to it reads as a
fault.
"""

WAITING_PLUGIN_DETAIL = (
    "Waiting for {a_credential}: {names}. The plugin's author asked for "
    "{it_them} and nobody has supplied {it_them} yet, so it is not running. "
    "Open the plugin's settings page and paste the value into the field asking "
    "for it."
)
"""Shown against a plugin that is short a credential — ADR-0025 section 4.

Deliberately the same word the page uses, "waiting", and deliberately not
"failing": a plugin nobody has given an API key to has not gone wrong. The
sentence names *what* it is waiting for, because "waiting" on its own is the
kind of status that gets ignored, and because that name is the whole of what
the operator has to do next. A name, never a value (ADR-0025 section 5).
"""


def waiting_plugin_detail(names: Sequence[str]) -> str:
    """:data:`WAITING_PLUGIN_DETAIL` written out for one or several names."""
    plural = len(names) != 1
    return WAITING_PLUGIN_DETAIL.format(
        a_credential="credentials" if plural else "a credential",
        it_them="them" if plural else "it",
        names=", ".join(names),
    )


def build_plugin_listing(
    result: DiscoveryResult,
    plugin_health: PluginHealthSource | None,
    *,
    scanned_at: datetime | None = None,
    disabled: Collection[str] = (),
    missing_secrets: Callable[[str, Sequence[str]], Sequence[str]] | None = None,
) -> PluginListing:
    """Render a discovery scan for the API — successes *and* failures.

    Spec section 5.1 requires a broken plugin to be visible with its error. A
    failure that is merely absent from the list is the worst possible outcome:
    the operator sees the folder they copied in and an interface that acts as
    though they never did.

    ``disabled`` names the plugins the operator switched off (ADR-0013). They
    stay in the listing — they are installed, they are just not running — and
    their state is reported as ``unknown`` rather than ``failing``: a supervisor
    that stopped a plugin on request has not failed at anything, and colouring
    it red would train people to ignore red.

    ``missing_secrets`` answers "which of the credentials this manifest asked
    for is the store still short of", and is what makes this listing agree with
    the plugin pages (ADR-0025 section 4). **Two sources exist for that one
    fact and they are not interchangeable.** The supervisor's own
    ``waiting_for_secrets`` is authoritative for a *running* core and is carried
    through here as the fallback; the manifest-plus-store derivation is
    authoritative for anything a *screen* shows, because a plugin that is
    switched off, or that this core was assembled without a supervisor for, has
    no supervisor answer at all and can still be short a credential. Preferring
    the derivation is what stops the JSON API and the page saying different
    things about the same plugin.
    """
    switched_off = set(disabled)
    plugins: list[PluginView] = []
    for record in sorted(result.plugins, key=lambda r: r.name):
        state = HealthState.UNKNOWN
        detail: str | None = None
        restarts = 0
        runtime = plugin_health.status_for(record.name) if plugin_health is not None else None
        supervisor_waiting: list[str] = []
        if runtime is not None:
            state = HealthState.OK if runtime.state == "running" else HealthState.FAILING
            detail = runtime.detail
            restarts = runtime.restarts
            supervisor_waiting = [
                str(name) for name in getattr(runtime, "waiting_for_secrets", ()) or ()
            ]
        # The **required** ones only, and by name (ADR-0026). `secrets` is a
        # list of `SecretRequest` tables since contract 2.0, so `list(...)` here
        # would put whole models where the store expects names — and an
        # optional credential nobody supplied is not something a plugin waits
        # for, because it starts without one.
        declared = record.manifest.permissions.required_secrets
        derived = (
            [str(name) for name in missing_secrets(record.name, declared)]
            if missing_secrets is not None and declared
            else []
        )
        waiting = derived or supervisor_waiting
        if waiting:
            # Not `failing`, and not `ok`. Waiting for something only a person
            # can supply is its own state; ``waiting_for_secrets`` below carries
            # which credential, which the three-valued enum cannot.
            state = HealthState.UNKNOWN
            detail = waiting_plugin_detail(waiting)
        enabled = record.name not in switched_off
        if not enabled:
            state = HealthState.UNKNOWN
            # Off on purpose outranks waiting in the sentence: the operator
            # switched it off, so "it is not running" has an answer already, and
            # the credential is still reported in `waiting_for_secrets`.
            detail = DISABLED_PLUGIN_DETAIL
        plugins.append(
            PluginView(
                name=record.name,
                version=record.manifest.plugin.version,
                transport=record.manifest.plugin.transport.value,
                description=record.manifest.plugin.description,
                directory=record.directory.as_posix(),
                tools={
                    tool_name: tool.risk.value
                    for tool_name, tool in sorted(record.manifest.tools.items())
                },
                state=state,
                detail=detail,
                restarts=restarts,
                waiting_for_secrets=waiting,
                enabled=enabled,
            )
        )

    failures = [
        PluginFailureView(
            name=failure.name,
            source=failure.source.as_posix(),
            reason=failure.message,
        )
        for failure in sorted(result.failures, key=lambda f: (f.name or "", f.message))
    ]
    return PluginListing(
        plugins=plugins,
        failures=failures,
        loaded_count=len(plugins),
        failed_count=len(failures),
        scanned_at=scanned_at or datetime.now(UTC),
    )


def _read_disabled(layout: AppdataLayout) -> tuple[set[str], PluginFailureView | None]:
    """The switched-off list, or an empty set and a failure row explaining why.

    Never raises. The listing and the dashboard are the two screens an operator
    reaches for when something is wrong, so a broken state file has to appear
    *on* them rather than replacing them with a 500.
    """
    try:
        return read_disabled_plugins(layout), None
    except PluginStateError as exc:
        return set(), PluginFailureView(
            name=None,
            source=disabled_state_path(layout).as_posix(),
            reason=str(exc),
        )


def missing_secrets_source(
    secrets: SecretNameSource | None,
) -> Callable[[str, Sequence[str]], Sequence[str]] | None:
    """The "which credentials is this plugin still short of" callable, or None.

    Built here rather than in the router factory because the listing above is
    the only thing that consumes it, and because ``None`` — no store, or a
    store that cannot answer — has to mean "ask nobody" rather than "ask and
    get an empty answer", which are different listings.
    """
    # ADR-0025 section 4, through the one seam this module has to the store.
    # `missing` is optional on `SecretNameSource` in the same way `reload` is on
    # `PluginHealthSource`: a store that can only list names is still a valid
    # store, and an assembly wired without one gets a listing with no waiting
    # rows rather than an error. Names in, names out — nothing here can reach a
    # value, which is what keeps "the API agrees with the page" from costing
    # anything (ADR-0025 section 5).
    _store_missing = getattr(secrets, "missing", None)

    def _missing_secrets(plugin: str, declared: Sequence[str]) -> Sequence[str]:
        if _store_missing is None or not declared:
            return ()
        try:
            return list(_store_missing(list(declared), plugin=plugin))
        except Exception as exc:  # noqa: BLE001 - one unreadable namespace, not a dead listing
            logger.warning(
                "plugin_secret_state_unreadable", plugin=plugin, error=repr(exc)
            )
            return ()

    return _missing_secrets if _store_missing is not None else None


class _PluginScanCache:
    """Holds the current scan so ``reload`` means something.

    Spec section 5.1: the core scans "at startup and on an admin-UI reload
    action". If the listing endpoint rescanned on every request, reload would
    be a button that does nothing observable and the operator would have no way
    to tell whether the core had picked their folder up yet.

    Lives in the router's closure, not at module level — one core, one cache,
    and no shared state between two routers in the same test process.

    **What is cached is the scan, not the listing.** The directory walk and the
    manifest parsing are the expensive part and the part reload exists to
    control; the live facts laid over them — whether the supervisor has the
    plugin up, and whether its credentials are in the store — are cheap and must
    not be stale. Caching the finished listing meant deleting a secret under a
    running plugin left the JSON API reporting ``ok`` while the plugin's own
    page, which reads the store on every render, said "waiting". Two sources for
    one fact, disagreeing because one of them was answering from a snapshot.
    """

    def __init__(
        self,
        discovery: PluginDiscovery,
        plugin_health: PluginHealthSource | None,
        layout: AppdataLayout,
        missing_secrets: Callable[[str, Sequence[str]], Sequence[str]] | None = None,
    ) -> None:
        self._discovery = discovery
        self._plugin_health = plugin_health
        self._layout = layout
        self._missing_secrets = missing_secrets
        self._lock = asyncio.Lock()
        self._scan_result: DiscoveryResult | None = None
        self._disabled: set[str] = set()
        self._problem: PluginFailureView | None = None
        self._scanned_at: datetime | None = None

    async def current(self) -> PluginListing:
        async with self._lock:
            if self._scan_result is None:
                await self._scan()
            return self._render()

    async def reload(self) -> PluginListing:
        async with self._lock:
            # Rescanning the directory is only half of reload. Spec section 5.1
            # promises that copying a folder and pressing reload makes the
            # plugin work — which means the supervisor has to start it, not just
            # the listing learn it exists. Without this a newly installed plugin
            # appears in the UI with no live status and never runs, which looks
            # like the plugin being broken rather than reload being incomplete.
            supervisor_reload = getattr(self._plugin_health, "reload", None)
            if supervisor_reload is not None:
                try:
                    await supervisor_reload()
                except Exception as exc:  # noqa: BLE001 - one bad plugin, section 5.1
                    logger.error("plugin_supervisor_reload_failed", error=repr(exc))
            await self._scan()
            return self._render()

    async def _scan(self) -> None:
        """Walk the plugin directory. The expensive half, and the half reload owns."""
        self._scan_result = await asyncio.to_thread(self._discovery.scan)
        self._disabled, self._problem = await asyncio.to_thread(
            _read_disabled, self._layout
        )
        self._scanned_at = datetime.now(UTC)

    def _render(self) -> PluginListing:
        """The listing as of now, over the scan as of ``reload``."""
        result = self._scan_result
        if result is None:  # pragma: no cover - `current` scans first
            raise RuntimeError("The plugin directory has not been scanned yet.")
        listing = build_plugin_listing(
            result,
            self._plugin_health,
            scanned_at=self._scanned_at,
            disabled=self._disabled,
            missing_secrets=self._missing_secrets,
        )
        if self._problem is not None:
            # A state file the core cannot read is itself a broken thing on
            # disk, so it is listed like one rather than taking the whole
            # listing down (spec section 5.1). Every plugin reads as enabled
            # until it is fixed, which is why the row has to be loud.
            listing.failures.append(self._problem)
            listing.failed_count = len(listing.failures)
        return listing


__all__ = [
    "DISABLED_PLUGIN_DETAIL",
    "WAITING_PLUGIN_DETAIL",
    "build_plugin_listing",
    "missing_secrets_source",
    "waiting_plugin_detail",
]
