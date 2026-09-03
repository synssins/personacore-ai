"""What the admin API is told about a running plugin — ADR-0040.

Moved out of ``personacore.server``. The two sides use different words for the
same plugin states, and this is the one place that translates between them, so
neither the admin package nor the plugin host has to adopt the other's
vocabulary — which is what lets the admin API report "unknown" honestly when no
supervisor is running at all.

It owns the translation and nothing else: it starts no plugin, reads no
manifest, and has no opinion about which plugins exist. It would make the same
sense to a caller with no HTTP surface in the process.
"""

from __future__ import annotations

from typing import Any


class _PluginRuntimeStatus:
    """Structural match for ``admin.protocols.PluginRuntimeStatus``."""

    def __init__(
        self,
        state: str,
        detail: str | None,
        restarts: int,
        waiting_for_secrets: tuple[str, ...] = (),
    ) -> None:
        self.state = state
        self.detail = detail
        self.restarts = restarts
        self.waiting_for_secrets = waiting_for_secrets


class _PluginHealthView:
    """Adapts the host's health snapshots to what the admin API asks for.

    The two shapes differ by a couple of field names. Translating here rather
    than renaming either side keeps the admin module free of any dependency on
    the plugin host, which is what lets the admin API report "unknown" honestly
    when no supervisor is running at all.
    """

    def __init__(self, host: Any) -> None:
        self._host = host

    # The two sides use different words for the same states. The admin API's
    # vocabulary is deliberately free text so it was not boxed in before a
    # supervisor existed; translating here means neither side has to adopt the
    # other's names. Anything unmapped stays as-is rather than being guessed at,
    # because a wrong status is worse than an unfamiliar one — a healthy plugin
    # reported as failing sends someone hunting a fault that is not there.
    _STATE_WORDS = {
        "healthy": "running",
        # NOT "running". A degraded plugin is one the supervisor is retrying
        # with backoff, which includes one that has never successfully started
        # — a config error that stops it dead reads as "degraded" here. Calling
        # that ok put a green row next to a plugin that could not run at all,
        # and the reason was sitting in `detail` the whole time.
        "degraded": "crashed",
        "starting": "starting",
        "failed": "crashed",
        "disabled": "stopped",
    }

    async def reload(self) -> Any:
        """Start, stop and restart plugins to match what is now on disk.

        The admin reload endpoint calls this before rescanning, so "copy a
        folder, hit reload" actually runs the plugin rather than only listing
        it.
        """
        return await self._host.reload()

    def status_for(self, plugin_name: str) -> Any | None:
        for health in self._host.health():
            if health.name == plugin_name:
                raw = str(getattr(health.state, "value", health.state))
                # Carried, not dropped. The supervisor is the authority on what
                # a RUNNING core is short of, and rebuilding a status out of
                # three fields threw that away — so the JSON API called a plugin
                # waiting for a credential "failing" while the page it sits
                # beside called it "waiting" (ADR-0025 §4: waiting is a state,
                # not a fault). Names only; a value never leaves the store.
                waiting = tuple(getattr(health, "waiting_for_secrets", ()) or ())
                return _PluginRuntimeStatus(
                    state=self._STATE_WORDS.get(raw, raw),
                    detail=health.last_error,
                    restarts=health.restart_count,
                    waiting_for_secrets=waiting,
                )
        return None

    def output_for(self, plugin_name: str) -> Any | None:
        """What one plugin last printed to stderr, for the plugin output page.

        Optional on the admin side — discovered with ``getattr`` there, exactly
        as ``reload`` above is — so a core assembled without a plugin host says
        "there is nothing to read this from" rather than showing an empty box
        that looks like a plugin in silence (PC-279).
        """
        return self._host.plugin_output(plugin_name)
