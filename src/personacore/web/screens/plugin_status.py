"""The two plugin-wide screens: what every plugin is doing (PC-280) and
what every plugin printed (PC-279).

Both read the supervisor view the JSON API is given rather than a second one,
and both say so out loud when this core was assembled without one - an empty
box would read as "nothing happened", which is a different fact.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from personacore.admin.protocols import (
    PluginOutputView,
    PluginRuntimeStatus,
)
from personacore.plugins.mcp_client import PLUGIN_OUTPUT_CHARS
from personacore.web.plugin_page import (
    NO_OUTPUT_SOURCE_NOTE,
    NO_RUNTIME_NOTE,
    OUTPUT_UNTRUSTED_NOTE,
    TOOLS_NONE_NOTE,
    TOOLS_NOTE,
    UPTIME_LATER,
    plugin_health_rows,
    plugin_output_rows,
)
from personacore.web.screens.plugin_common import plugin_name_or_404
from personacore.web.shared import UIContext


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the plugin health page and the plugin output pages."""
    templates = ctx.templates
    scans = ctx.scans
    plugin_health = ctx.plugin_health
    _shell = ctx.shell
    # Borrowed from `plugin_common`, under the names they had while they were
    # closures on the factory - the code below is unchanged from when it was.
    _plugin_name_or_404 = plugin_name_or_404


    # -- plugin health and plugin output -----------------------------------
    #
    # Two screens about plugins and nothing else. The system dashboard already
    # carries a row per plugin, but it carries it between the disk and the
    # broker, and "which of my plugins is unhappy" is a different question from
    # "is this machine well" — the second is answered by a glance, the first by
    # reading. So each plugin gets its state, its transport, the tools it
    # declares, its restart count and its last error together (PC-280), and a
    # second screen carries what it has actually printed (PC-279).
    #
    # **Both are read-only.** Nothing here starts, stops, saves or deletes
    # anything; the controls that do live on the plugin list and the per-plugin
    # screen, and duplicating them would be a second place for an uninstall to
    # be clicked by accident.

    def _status_for(name: str) -> PluginRuntimeStatus | None:
        """The supervisor's view of one plugin, or ``None``.

        Wrapped rather than called directly so a health source that raises
        cannot take a whole page down: this screen's job is to *report* on
        things that are broken, and being the thing that breaks would be a poor
        showing. A failure reads as "not reported", which is what it is.
        """
        if plugin_health is None:
            return None
        try:
            return plugin_health.status_for(name)
        except Exception:  # noqa: BLE001 - see docstring
            return None

    #: The supervisor's stderr capture, if this core has one. Looked up once at
    #: build time rather than per request: whether the capability exists is a
    #: property of how the core was assembled, not of who is asking.
    output_for_plugin = getattr(plugin_health, "output_for", None)

    def _output_for(name: str) -> PluginOutputView | None:
        if output_for_plugin is None:  # pragma: no cover - guarded by the caller
            return None
        try:
            return output_for_plugin(name)
        except Exception:  # noqa: BLE001 - a page about failures must not fail
            return None

    @router.get(
        "/plugins/health",
        response_class=HTMLResponse,
        summary="Every plugin's state, transport, tools, restarts and last error",
    )
    async def plugins_health_page(request: Request) -> HTMLResponse:
        """Spec section 9's plugin health, on a page of its own (PC-280)."""
        listing = await scans.current()
        return templates.TemplateResponse(
            request=request,
            name="plugin_health.html",
            context={
                **await _shell(request, "plugin-health"),
                "plugins": plugin_health_rows(
                    listing, _status_for if plugin_health is not None else None
                ),
                "runtime_note": None if plugin_health is not None else NO_RUNTIME_NOTE,
                "tools_note": TOOLS_NOTE,
                "tools_none": TOOLS_NONE_NOTE,
                "uptime_later": UPTIME_LATER,
            },
        )

    async def _output_context(request: Request, only: str | None = None) -> dict[str, Any]:
        """The output screen's context, for every plugin or for exactly one.

        One function and one template behind both, because the two pages differ
        only in how many rows they carry: a sweep ("who is complaining?") and a
        destination ("show me this one"). Two templates would be two places for
        the same four ways of being empty to be worded differently.
        """
        listing = await scans.current()
        rows = plugin_output_rows(
            listing,
            _output_for if output_for_plugin is not None else None,
            chars=PLUGIN_OUTPUT_CHARS,
        )
        if only is not None:
            rows = [row for row in rows if row["name"] == only]
        return {
            **await _shell(request, "plugin-output"),
            "plugins": rows,
            "only": only,
            # A named plugin the scan has never heard of. Not a 404: the folder
            # may have been removed a moment ago, or the name may be a typo, and
            # either way an operator who followed a link deserves a sentence
            # rather than an error page.
            "unknown": only is not None and not rows,
            "source_note": None if output_for_plugin is not None else NO_OUTPUT_SOURCE_NOTE,
            "untrusted_note": OUTPUT_UNTRUSTED_NOTE,
        }

    @router.get(
        "/plugins/logs",
        response_class=HTMLResponse,
        summary="What every plugin has printed",
    )
    async def plugins_output_page(request: Request) -> HTMLResponse:
        """Every plugin's output on one page — the sweep (PC-279)."""
        return templates.TemplateResponse(
            request=request,
            name="plugin_logs.html",
            context=await _output_context(request),
        )

    @router.get(
        "/plugins/{name}/logs",
        response_class=HTMLResponse,
        summary="What one plugin has printed",
    )
    async def plugin_output_page(request: Request, name: str) -> HTMLResponse:
        """One plugin's output — the destination every failing row links to.

        This is what makes the errand one step: a plugin that will not start is
        a link away from the sentence explaining it, from the plugin list, from
        the plugin health page and from its own settings screen.
        """
        return templates.TemplateResponse(
            request=request,
            name="plugin_logs.html",
            context=await _output_context(request, only=_plugin_name_or_404(name)),
        )
