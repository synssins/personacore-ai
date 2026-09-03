"""The installed-plugins list (spec section 5.1) and its per-row controls.

The list and the rows are one screen: every control on a row answers with the
re-rendered list, because the list *is* the swap boundary. The install form
above it lives in ``plugin_install``; the page one row links to lives in
``plugin_detail``.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from personacore.admin.models import (
    PluginListing,
)
from personacore.web.plugin_page import (
    by_name,
)
from personacore.web.screens.plugin_common import (
    WAITING_WORD,
    can,
    op,
    plugin_name_or_404,
    secret_requests,
    waiting_for,
)
from personacore.web.shared import (
    NO_PLUGIN_OPERATIONS,
    UIContext,
    api_handler,
    refusal,
)


def plugin_rows(
    listing: PluginListing,
    waiting: Callable[[str], list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The plugin screen's two groups: loaded, and failed to load.

    Spec section 5.1 requires the failures to be visible with their reasons; a
    plugin that is merely absent from the list is the worst outcome for whoever
    just copied a folder in.

    Both groups come back **alphabetically** (PC-281), through the same
    :func:`~personacore.web.plugin_page.by_name` the two new plugin screens
    use, so all three orderings are one function rather than three that agree
    for now. Sorting here rather than in ``build_plugin_listing`` leaves the
    JSON API's own ordering untouched — nothing outside this surface should
    inherit a decision made for a screen.

    ``waiting`` names the credentials a plugin asked for and has not been given
    (ADR-0025 §4). A row with any is drawn **waiting**, not failed: the plugin
    is not broken, it is short of something only a person can supply, and a red
    row would send somebody hunting a fault that is not there. Optional so this
    function stays callable with a listing and nothing else.
    """
    loaded = [
        {
            "name": plugin.name,
            "version": plugin.version,
            "description": plugin.description,
            "enabled": plugin.enabled,
            "waiting": waiting(plugin.name) if waiting is not None else [],
        }
        for plugin in listing.plugins
    ]
    failed = [
        {
            # A failure severe enough to lose the manifest still has the
            # directory it came from, and naming that is more use than "None".
            "name": failure.name or failure.source,
            # The scan has no version for a plugin whose manifest is what
            # failed, and inventing one would be a lie in a diagnostic view.
            "version": "",
            "fail_reason": failure.reason,
            # A plugin that never loaded has no manifest the core trusts, so
            # there is no request to honour and nothing to ask for.
            "waiting": [],
        }
        for failure in listing.failures
    ]
    return by_name(loaded), by_name(failed)

ROW_REFUSALS = {
    "enable": "Not switched on:",
    "disable": "Not switched off:",
    "uninstall": "Not uninstalled:",
}
"""How a refused row operation opens, before the API's own reason.

Three words in front of the handler's sentence, because "No plugin named
'clock' is installed" on its own does not say which of the row's three controls
just declined to do anything. The reason itself is never reworded here — it was
written for an operator already (spec section 9)."""

UNINSTALL_TITLE = "Uninstall {name}?"

UNINSTALL_BODY = (
    "This removes the {name} folder and everything in it — config.toml, any "
    "secrets it was given, its captured log, and the record that it was "
    "switched off. This cannot be undone, and installing {name} again starts "
    "fresh. To just stop it, switch it off instead: that keeps its folder and "
    "settings."
)
"""ADR-0013's uninstall, named in the specific terms spec section 9 asks for.

Names the plugin, names what is destroyed, and names the non-destructive thing
the operator probably meant — a confirmation that says "are you sure?" and
nothing else is a confirmation nobody reads.

**Rewritten because it had stopped being true.** It promised the folder and
``config.toml``, and uninstall had since grown three more things it destroys:
the plugin's secret namespace (ADR-0025 §1 — "everything this plugin owns"
becomes one call), the captured stderr the output page reads, and the
supervisor's record of whether it was switched off. A confirmation that
under-states what it destroys is worse than none, because the operator
consented to something smaller than what happened.

**The wording is part D's**, sent over verbatim: D wired those three into the
uninstall path and is the only place that knows what really goes, so the
sentence describing it comes from there rather than being a guess written
beside it. If that path grows a fifth thing, this sentence changes the same
day — and the test that reads it is the thing that makes forgetting expensive.
"""

UNINSTALL_KEEP = "Keep {name}"
"""The way out of the confirmation, on the button that does nothing.

Named rather than "Cancel", for the same reason the destructive button is named
"Uninstall clock": on a page with two buttons and a paragraph about deletion,
the one word that must not be ambiguous is the safe one.
"""
async def plugins_context(
    ctx: UIContext,
    request: Request,
    *,
    install_result: dict[str, str] | None = None,
    shell: bool = True,
) -> dict[str, Any]:
    """The plugin list's context, for the page and for the swapped fragment.

    One function behind both, because the fragment is the page's own
    ``#plugin-list`` and a second construction of it is a second answer to
    "is this control offered". ``shell=False`` leaves out the sidebar and
    footer the fragment has no use for.

    All three answers are the same rule, and it is a lookup rather than a
    constant: a control is offered exactly when :func:`api_handler` can find
    the JSON API's own handler for it on this application, and marked
    ``later`` when the core was assembled without the plugin API. The list's
    switch needs both halves of the toggle, because a row that could be
    switched off but not back on is worse than one that says "later".
    """
    listing = await ctx.scans.current()

    def _waiting(name: str) -> list[str]:
        return waiting_for(secret_requests(ctx.layout, name))

    loaded, failed = plugin_rows(listing, _waiting)
    return {
        **(await ctx.shell(request, "plugins") if shell else {}),
        "loaded": loaded,
        "failed": failed,
        "install_result": install_result,
        "can_install": can(request, "install"),
        "can_toggle": can(request, "enable", "disable"),
        "can_uninstall": can(request, "uninstall"),
        "waiting_word": WAITING_WORD,
    }


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the plugins list and the switch, switch-off and uninstall
    controls its rows carry."""
    templates = ctx.templates
    _plugins_context = partial(plugins_context, ctx)
    # Borrowed from `plugin_common`, under the names they had while they were
    # closures on the factory - the code below is unchanged from when it was.
    _op = op
    _plugin_name_or_404 = plugin_name_or_404
    _refusal = refusal


    @router.get("/plugins", response_class=HTMLResponse, summary="Installed plugins")
    async def plugins_page(request: Request) -> HTMLResponse:
        """Spec section 5.1's list: everything found, loaded or broken.

        Installing, switching on and off and uninstalling all work from here
        (PC-127, PC-134, PC-136): each row's control posts to a fragment route,
        which runs the JSON API's own handler and hands back the re-rendered
        ``#plugin-list``. Nothing is hardcoded off any more — a control is
        disabled and marked ``later`` only when this core was assembled without
        the plugin API to work it.
        """
        return templates.TemplateResponse(
            request=request,
            name="plugins.html",
            context=await _plugins_context(request),
        )

    # -- the list's row controls: switch on, switch off, uninstall ---------

    async def _row_notice(request: Request, name: str, operation: str) -> dict[str, str]:
        """Run one of the list's row operations, and say what happened in a sentence.

        The operation itself is the JSON API's own handler, reached through
        :func:`api_handler` exactly as the per-plugin screen reaches it — so a
        plugin switched off from a row and one switched off with ``POST
        /admin/api/plugins/{name}/disable`` are the same code, in the same
        order, writing the same audit record. Uninstall in particular stops the
        plugin before its folder is deleted, and that ordering lives in the
        handler; there is no path here that could skip it.

        **Nothing comes back as a status code.** A name that is not installed, a
        removal the filesystem refused, a core with no plugin API at all: each
        is a sentence for the page to render beside the list (spec section 9),
        and the words are the API's own wherever it has them.
        """
        handler = api_handler(request.app, operation)
        if handler is None:  # pragma: no cover - the control is disabled first
            return {"kind": "refused", "message": NO_PLUGIN_OPERATIONS}
        try:
            result = await handler(name=name, request=request)
        except HTTPException as exc:
            return {"kind": "refused", "message": f"{ROW_REFUSALS[operation]} {_refusal(exc)}"}
        return {"kind": "ok", "message": result.message}

    async def _row_fragment(request: Request, name: str, operation: str) -> HTMLResponse:
        """One row's operation, answered with the re-rendered list.

        The response is ``fragments/plugin_installed.html`` — the same body the
        install posts back — because the list is the swap boundary for all three
        controls and the sentence belongs beside the form above it, swapped out
        of band. One fragment behind install, toggle and uninstall means the row
        an operator sees after switching a plugin off is rendered by the code
        that renders the page, not by a second description of a row.
        """
        _plugin_name_or_404(name)
        notice = await _row_notice(request, name, operation)
        return templates.TemplateResponse(
            request=request,
            name="fragments/plugin_installed.html",
            context=await _plugins_context(request, install_result=notice, shell=False),
        )

    @router.post(
        "/plugins/{name}/enable/fragment",
        response_class=HTMLResponse,
        summary="Switch a plugin on from the list",
    )
    async def plugin_enable_fragment(request: Request, name: str) -> HTMLResponse:
        """The list row's switch, on (PC-134).

        Answers 200 whether the plugin was switched on or the change was
        refused, because the response *is* the refreshed list: the row now says
        what the plugin is actually doing, without the page around it being
        reloaded.
        """
        return await _row_fragment(request, name, "enable")

    @router.post(
        "/plugins/{name}/disable/fragment",
        response_class=HTMLResponse,
        summary="Switch a plugin off from the list",
    )
    async def plugin_disable_fragment(request: Request, name: str) -> HTMLResponse:
        """The same switch, off — the folder and every setting in it stay put,
        and the row re-renders as "switched off"."""
        return await _row_fragment(request, name, "disable")

    # -- the confirmation, which does not need a script to work (PC-136) ---
    #
    # It used to be a dialog: the Uninstall button was an `hx-get` of a fragment
    # swapped into `<dialog id="modal">`, which `admin.js` opened on swap. Every
    # part of that answered correctly on the server and the folder really was
    # removed — and a plugin still could not be uninstalled. Install worked, because
    # install posts a form; uninstall did not, because uninstall needed htmx to
    # fire, admin.js to have loaded, its `htmx:afterSwap` listener to run and
    # `showModal()` to succeed. Four things, in the browser, before a
    # destructive action an operator has already decided on.
    #
    # **So the dependency is gone rather than defended.** The control is a link,
    # the confirmation is a page, and confirming is a form post. That is three
    # HTML elements and no JavaScript at all, which is why
    # `test_uninstall_needs_no_javascript_at_all` can prove the whole path with
    # a plain GET and a plain POST carrying no htmx headers — which is exactly
    # what a browser sends when the script has not run.
    #
    # A full-page confirmation is also the more honest shape for this one
    # action. A modal is for something small enough to come back from; this
    # deletes a folder, a credential and a log, and it is worth a page.

    @router.get(
        "/plugins/{name}/uninstall",
        response_class=HTMLResponse,
        summary="Confirm uninstalling one plugin",
    )
    async def plugin_uninstall_confirm(request: Request, name: str) -> HTMLResponse:
        """The confirmation, in specific terms, on a page of its own.

        Names the plugin, names every thing that is destroyed with it
        (:data:`UNINSTALL_BODY`), and names the non-destructive thing the
        operator probably meant. A confirmation that only asks "are you sure?"
        is one that gets clicked through.

        Deliberately the **same path** as the POST that performs it: a GET here
        asks, a POST here does it. One path is one thing to get right, and the
        page's form needs no action attribute at all — it posts to where it
        already is, which is the shape that keeps working when nothing else on
        the page does.
        """
        _plugin_name_or_404(name)
        return templates.TemplateResponse(
            request=request,
            name="plugin_uninstall.html",
            context={
                **await ctx.shell(request, "plugins"),
                "name": name,
                "title": UNINSTALL_TITLE.format(name=name),
                "body": UNINSTALL_BODY.format(name=name),
                "confirm_label": f"Uninstall {name}",
                "keep_label": UNINSTALL_KEEP.format(name=name),
                "can_uninstall": can(request, "uninstall"),
                "unavailable": NO_PLUGIN_OPERATIONS,
            },
        )

    @router.post(
        "/plugins/{name}/uninstall",
        response_class=HTMLResponse,
        summary="Uninstall one plugin, deleting its folder",
    )
    async def plugin_uninstall(request: Request, name: str) -> HTMLResponse:
        """Stop it, delete its folder, then show the list it is no longer in.

        Stopping first is not tidiness: a running stdio plugin is a live
        subprocess holding its own files open, and Windows simply refuses to
        delete them underneath it. The JSON API's handler does that ordering,
        which is the reason this screen calls it rather than repeating it.

        **This is the whole of the no-script path's second half.** It answers a
        plain form post — no htmx header, no JSON, no fetch — with the whole
        plugins page, because a browser that submitted a form is expecting a
        page and has nowhere to put a fragment. It is the same handler and the
        same audit record whether the post came from a script or from a button.
        """
        _plugin_name_or_404(name)
        try:
            removed = await _op(request, "uninstall")(name=name, request=request)
        except HTTPException as exc:
            notice = {
                "kind": "refused",
                "message": f"{ROW_REFUSALS['uninstall']} {_refusal(exc)}",
            }
        else:
            notice = {"kind": "ok", "message": removed.message}
        return templates.TemplateResponse(
            request=request,
            name="plugins.html",
            context=await _plugins_context(request, install_result=notice),
        )
