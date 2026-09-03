"""One plugin's own ``config.toml`` — reading it, writing it, filling it in.

Spec section 5.1 ("the admin UI edits the plugin's own config file") and
ADR-0016's search-and-fill. Split out of :mod:`personacore.admin.routes`
(ADR-0040).

**The core checks the text is well-formed TOML and nothing else.** What the
keys mean belongs to the plugin, and a core that knew one plugin's settings
could not let that plugin ship a new one without a core release.

The lookup's order of checks is the security decision in this module: the
plugin's schema says which field may be filled and by which tool, the plugin's
manifest says whether that tool is ``safe``, and only then is anything called.
A request never names a tool.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Request, status
from fastapi import Path as PathParam

from personacore.admin.api_shared import AdminApiContext, _fail, _record_change
from personacore.admin.config_io import ConfigRejected
from personacore.admin.models import (
    MAX_LOOKUP_QUERY_CHARS,
    AdminUser,
    ConfigProblem,
    PluginConfigResponse,
    PluginConfigUpdateRequest,
    PluginConfigUpdateResponse,
    PluginLookupRequest,
    PluginLookupResponse,
)
from personacore.admin.plugin_config_io import (
    PluginConfigInvalid,
    PluginConfigNotFound,
    PluginConfigUnsafe,
    read_plugin_config,
    top_level_keys,
    write_plugin_config,
)
from personacore.admin.plugin_lookup import (
    LookupRefused,
    authorise_lookups,
    find_lookup,
    results_from,
)
from personacore.admin.plugin_schema import SCHEMA_FILENAME, SchemaRefused, read_plugin_schema
from personacore.audit import AuditOutcome, Owner, Surface, get_logger
from personacore.contracts.manifest import RiskLevel
from personacore.plugins.host import TOOL_SEPARATOR
from personacore.plugins.packages import PLUGIN_NAME_PATTERN

logger = get_logger(__name__)

LOOKUP_ACTION = "plugins.config.lookup"
"""Audit action for ADR-0016's search-and-fill.

Named beside the config write it belongs with, and recorded whatever the outcome:
"an admin searching for a town is a thing that happened, and the log should say
so" (ADR-0016). The plugin host writes its own record for the tool call itself,
so a search leaves two entries — the admin action, and the call it caused."""


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register the per-plugin config routes on the guarded router."""
    api = router
    layout = ctx.layout
    audit = ctx.audit
    scans = ctx.scans
    require_user = ctx.require_user
    live_toggle = ctx.live_toggle
    call_plugin_tool = ctx.call_plugin_tool

    async def _plugin_config(name: str) -> PluginConfigResponse:
        """Read one plugin's ``config.toml``. Shared with the UI."""
        try:
            return await asyncio.to_thread(read_plugin_config, layout, name)
        except PluginConfigNotFound as exc:
            raise _fail(status.HTTP_404_NOT_FOUND, exc.message, exc.problems) from exc
        except PluginConfigUnsafe as exc:
            raise _fail(status.HTTP_409_CONFLICT, exc.message, exc.problems) from exc
        except ConfigRejected as exc:
            raise _fail(
                status.HTTP_500_INTERNAL_SERVER_ERROR, exc.message, exc.problems
            ) from exc

    async def _reload_after_config(name: str) -> bool:
        """Restart one plugin so a saved setting is actually in use (ADR-0010).

        A plugin reads its ``config.toml`` when it starts, so a saved setting
        that needs a restart is exactly the friction ADR-0010 exists to remove.
        Off-then-on rather than a whole-host reload: the operator changed one
        plugin, and taking every other one down with it is a bigger event than
        they asked for.

        A plugin that is *switched off* is deliberately left off — restarting it
        would be an enable nobody asked for. Returns whether the running core
        picked the change up, so the caller can say "in use now" or "at the next
        start" and never guess.
        """
        if live_toggle is None:
            return False
        listing = await scans.current()
        enabled = next(
            (view.enabled for view in listing.plugins if view.name == name), False
        )
        if not enabled:
            return False
        try:
            await live_toggle(name, False)
            await live_toggle(name, True)
        except Exception as exc:  # noqa: BLE001 - one plugin, spec section 5.1
            logger.error("plugin_config_reload_failed", plugin=name, error=repr(exc))
            return False
        return True

    async def _save_plugin_config(
        name: str, content: str, user: AdminUser
    ) -> PluginConfigUpdateResponse:
        """Validate, write atomically, restart the plugin, audit. Shared with
        the UI so a saved setting means the same thing either way."""
        action = "plugins.config.update"
        try:
            saved = await asyncio.to_thread(write_plugin_config, layout, name, content)
        except (PluginConfigNotFound, PluginConfigInvalid, PluginConfigUnsafe) as exc:
            codes = {
                PluginConfigNotFound: status.HTTP_404_NOT_FOUND,
                PluginConfigInvalid: status.HTTP_422_UNPROCESSABLE_CONTENT,
                PluginConfigUnsafe: status.HTTP_409_CONFLICT,
            }
            await _record_change(
                audit,
                user,
                action=action,
                outcome=AuditOutcome.REFUSED,
                # The reason, never the rejected text: it is the operator's
                # document and this record goes into backups (spec section 7).
                detail={"plugin": name, "bytes": len(content), "reason": exc.message},
            )
            raise _fail(codes[type(exc)], exc.message, exc.problems) from exc
        except ConfigRejected as exc:
            await _record_change(
                audit,
                user,
                action=action,
                outcome=AuditOutcome.FAILURE,
                detail={"plugin": name, "bytes": len(content)},
            )
            raise _fail(
                status.HTTP_500_INTERNAL_SERVER_ERROR, exc.message, exc.problems
            ) from exc

        reloaded = await _reload_after_config(name)
        await scans.reload()
        await _record_change(
            audit,
            user,
            action=action,
            outcome=AuditOutcome.SUCCESS,
            detail={
                "plugin": name,
                "path": saved.path,
                "bytes": len(content),
                # Top-level key names only. Enough for an investigator to see
                # what area changed, without copying the settings themselves
                # into the audit store (spec section 7).
                "keys": top_level_keys(saved.content),
            },
        )
        message = f"Settings saved to {saved.path}."
        message += (
            f" {name} was restarted, so they are in use now."
            if reloaded
            else " They take effect when the plugin next starts."
        )
        return PluginConfigUpdateResponse(
            saved=True, config=saved, reloaded=reloaded, message=message
        )

    async def _plugin_lookup(
        name: str, field_key: str, query: str, user: AdminUser
    ) -> PluginLookupResponse:
        """Run one field's declared lookup — ADR-0016. Shared with the UI.

        The order of the checks is the decision, so it is worth reading as one:

        1. The plugin's **schema** says which field may be filled and by which
           tool. A request never names a tool.
        2. The plugin's **manifest** says whether that tool is ``safe``.
           :func:`~personacore.admin.plugin_lookup.authorise_lookups` deletes
           every lookup it is not, so step 3 cannot find one.
        3. Only then is anything called, through the ordinary tool path — which
           applies the plugin's declared permissions and writes its own audit
           record — and the admin action is recorded here as well, because "an
           admin searching for a town is a thing that happened, and the log
           should say so".
        """
        wanted = query.strip()
        if len(wanted) > MAX_LOOKUP_QUERY_CHARS:
            # The API model caps this too; checked again here because the
            # admin UI calls this helper directly and a bound that only one of
            # two callers applies is not a bound (spec section 7).
            raise _fail(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"That search is longer than {MAX_LOOKUP_QUERY_CHARS} characters, "
                "which is longer than any name worth looking up. Nothing was called.",
                [ConfigProblem(key=field_key, problem="the search text is too long")],
            )
        if not wanted:
            raise _fail(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "There is nothing to search for yet. Type what you are looking for, "
                "then search.",
                [ConfigProblem(key=field_key, problem="the search box was empty")],
            )
        try:
            schema = await asyncio.to_thread(read_plugin_schema, layout, name)
        except SchemaRefused as exc:
            raise _fail(
                status.HTTP_422_UNPROCESSABLE_CONTENT, exc.message, exc.problems
            ) from exc
        except PluginConfigUnsafe as exc:
            raise _fail(status.HTTP_409_CONFLICT, exc.message, exc.problems) from exc
        except ConfigRejected as exc:
            raise _fail(
                status.HTTP_500_INTERNAL_SERVER_ERROR, exc.message, exc.problems
            ) from exc
        if schema is None:
            raise _fail(
                status.HTTP_404_NOT_FOUND,
                f"{name} ships no {SCHEMA_FILENAME}, so no setting on it offers a "
                "search.",
            )

        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == name), None)
        if view is None:
            raise _fail(
                status.HTTP_409_CONFLICT,
                f"{name} is not loaded, so the core cannot tell which of its tools are "
                "safe to call and will not call any of them. Fix the plugin, reload, "
                "then search.",
            )
        authorise_lookups(schema, view.tools)
        try:
            field, lookup = find_lookup(schema, field_key)
        except LookupRefused as exc:
            raise _fail(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                exc.message,
                [ConfigProblem(key=field_key, problem=exc.message)],
            ) from exc
        if call_plugin_tool is None:
            raise _fail(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "This core has nothing that can call a plugin's tools, so the search "
                f"on {field.label} cannot run. Type the values in by hand — the fields "
                "are still editable.",
            )

        try:
            outcome = await call_plugin_tool(
                f"{name}{TOOL_SEPARATOR}{lookup.tool}",
                {lookup.query_argument: wanted},
                risk_ceiling=RiskLevel.SAFE,
                # The operator, not the household: the tool path writes this
                # call's audit record, and it is the same operator the
                # admin-change record below is attributed to. Without this the
                # two halves of one lookup disagree about who did it and about
                # which surface's retention window applies (ADR-0004).
                owner=Owner.profile(user.id),
                surface=Surface.ADMIN_UI,
            )
        except Exception as exc:  # noqa: BLE001 - one search, not the page
            logger.error(
                "plugin_lookup_failed", plugin=name, field=field_key, error=repr(exc)
            )
            await _record_change(
                audit,
                user,
                action=LOOKUP_ACTION,
                outcome=AuditOutcome.FAILURE,
                detail={"plugin": name, "field": field_key, "tool": lookup.tool},
            )
            raise _fail(
                status.HTTP_502_BAD_GATEWAY,
                f"{lookup.tool} could not be called, so there are no results. The "
                "plugin may not be running.",
            ) from exc

        answer = PluginLookupResponse(
            plugin=name, field=field_key, tool=lookup.tool, query=wanted, message=""
        )
        if not outcome.ok:
            answer.message = outcome.error or (
                f"{lookup.tool} could not answer, so there are no results."
            )
        else:
            try:
                answer.results = results_from(lookup, outcome.content)
            except LookupRefused as exc:
                await _record_change(
                    audit,
                    user,
                    action=LOOKUP_ACTION,
                    outcome=AuditOutcome.FAILURE,
                    # The query, not the answer: the reply is untrusted text and
                    # the audit store is not where it belongs (spec section 7).
                    detail={
                        "plugin": name,
                        "field": field_key,
                        "tool": lookup.tool,
                        "query": wanted,
                        "reason": exc.message,
                    },
                )
                raise _fail(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
            answer.message = (
                f"{len(answer.results)} match(es) for {wanted!r}. Pick one to fill "
                f"{field.label}, or edit the values by hand."
                if answer.results
                else f"Nothing found for {wanted!r}. Try a different spelling, or a "
                "town with its state or country — or type the values in by hand."
            )
        await _record_change(
            audit,
            user,
            action=LOOKUP_ACTION,
            outcome=AuditOutcome.SUCCESS if outcome.ok else AuditOutcome.FAILURE,
            # What was searched for and how many came back. The results
            # themselves are untrusted text and stay out of the store.
            detail={
                "plugin": name,
                "field": field_key,
                "tool": lookup.tool,
                "query": wanted,
                "results": len(answer.results),
            },
        )
        return answer

    @api.post(
        "/plugins/{name}/config/lookup",
        response_model=PluginLookupResponse,
        summary="Fill a setting by asking the plugin (ADR-0016)",
    )
    async def plugin_config_lookup(
        name: Annotated[str, PathParam(pattern=PLUGIN_NAME_PATTERN)],
        body: PluginLookupRequest,
        request: Request,
    ) -> PluginLookupResponse:
        """ADR-0016: "A config field may declare that a tool can populate it."

        The body names the **setting**, never the tool. Which tool runs is read
        from the plugin's own ``config.schema.json`` and permitted only if the
        plugin's manifest declares it ``safe`` — "being installed does not make a
        tool reachable from a settings page".

        No match is a ``200`` with an empty list and a sentence saying so:
        "nothing found" is an answer, and an error status for it would make a
        working lookup indistinguishable from a broken one.
        """
        return await _plugin_lookup(name, body.field, body.query, require_user(request))

    @api.get(
        "/plugins/{name}/config",
        response_model=PluginConfigResponse,
        summary="Read one plugin's own config.toml",
    )
    async def get_plugin_config(
        name: Annotated[str, PathParam(pattern=PLUGIN_NAME_PATTERN)],
    ) -> PluginConfigResponse:
        """Spec section 5.1: "per-plugin config lives in the plugin's own
        folder ... the admin UI edits the plugin's own config file".

        The file comes back as text, comments included, because a plugin's
        comments are its field help (``plugins/_template/config.toml``) and
        re-serialising a parsed document would delete them.

        A file that does not currently parse is still returned, with ``valid``
        false and the syntax error in ``problem`` — the broken one is the one
        an operator needs to open.
        """
        return await _plugin_config(name)

    @api.put(
        "/plugins/{name}/config",
        response_model=PluginConfigUpdateResponse,
        summary="Write one plugin's own config.toml",
    )
    async def put_plugin_config(
        name: Annotated[str, PathParam(pattern=PLUGIN_NAME_PATTERN)],
        body: PluginConfigUpdateRequest,
        request: Request,
    ) -> PluginConfigUpdateResponse:
        """Replace a plugin's ``config.toml`` — validated, atomic, audited.

        **The core checks the text is well-formed TOML and nothing else.** What
        the keys mean belongs to the plugin (spec section 5.1): the core does
        not know that ``forecast_days`` must be 1-7, the plugin does, and the
        plugin template tells its author to check exactly that at startup. Do
        not add a schema here — a core that knew one plugin's settings could not
        let that plugin ship a new one without a core release.

        A syntax error is a ``422`` naming the problem in plain English, and
        **the file on disk is untouched**: the text is parsed before anything is
        opened for writing.
        """
        return await _save_plugin_config(name, body.content, require_user(request))


__all__ = [
    "LOOKUP_ACTION",
    "register",
]
