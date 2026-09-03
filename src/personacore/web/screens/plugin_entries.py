"""The per-plugin form's repeating groups and search-and-fill (ADR-0016).

Three GET fragments the settings form swaps in: another blank entry, the
results of a lookup the plugin's own manifest permits, and one of those results
filled into the entry that asked for it.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from personacore.admin.models import (
    PluginLookupRequest,
)
from personacore.admin.plugin_form import (
    ENTRY_KEY_PREFIX,
    form_inputs,
)
from personacore.admin.plugin_schema import (
    PluginSchema,
    SchemaField,
)
from personacore.web.plugin_page import (
    NO_SCHEMA_NOTE,
    STATE_UNSAVED,
    entry_context,
    field_context,
    fill_entry_inputs,
    next_entry_index,
    search_rows,
)
from personacore.web.screens.plugin_common import (
    authorise,
    entry_index,
    group_field,
    op,
    plugin_name_or_404,
    plugin_schema,
)
from personacore.web.shared import (
    UIContext,
    refusal,
)


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the entry, search and fill fragments."""
    templates = ctx.templates
    scans = ctx.scans
    # Borrowed from `plugin_common`, under the names they had while they were
    # closures on the factory - the code below is unchanged from when it was.
    _authorise = authorise
    _entry_index = entry_index
    _group_field = group_field
    _op = op
    _plugin_name_or_404 = plugin_name_or_404
    _plugin_schema = partial(plugin_schema, ctx.layout)
    _refusal = refusal


    # -- repeating groups and search-and-fill (ADR-0016) --------------------

    def _entry_fragment(
        request: Request, name: str, field: SchemaField, entry: dict[str, Any]
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="fragments/group_entry.html",
            context={
                "pname": name,
                "field": field_context(field, values={}),
                "entry": entry,
            },
        )

    def _schema_or_404(name: str, view: Any) -> PluginSchema:
        schema, refusal = _plugin_schema(name)
        if schema is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, refusal or NO_SCHEMA_NOTE)
        _authorise(schema, view)
        return schema

    @router.get(
        "/plugins/{name}/settings/{key}/entry/fragment",
        response_class=HTMLResponse,
        summary="One blank repeating-group entry",
    )
    async def plugin_entry_fragment(
        request: Request, name: str, key: str
    ) -> HTMLResponse:
        """Put one empty entry on the page. **Nothing is written.**

        Appended rather than re-rendering the group, so unsaved edits in the
        entries already on the page survive - the same reason remove and reorder
        are done in the browser.

        The new entry's position comes from the names already on the page, which
        the button sends along: one past the highest, not the count. After an
        entry has been removed in the browser those two differ, and the count
        would put the new entry's inputs on top of an existing one's.
        """
        _plugin_name_or_404(name)
        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == name), None)
        field = _group_field(_schema_or_404(name, view), key)
        on_page = form_inputs(request.query_params.multi_items())
        position = _entry_index(str(next_entry_index(field, on_page)))
        return _entry_fragment(
            request,
            name,
            field,
            entry_context(field, position, key="", values={}, submitted=None),
        )

    async def _run_lookup(
        request: Request, name: str, key: str, query: str
    ) -> tuple[list[Any], str]:
        """ADR-0016's search, run through the JSON API's own endpoint.

        The tool is never named here and never by the request: the *field* is,
        and the plugin's own schema - already stripped of every lookup its
        manifest does not permit - decides the rest. One search per request, and
        a refusal comes back as the sentence to show rather than an error page.
        """
        try:
            body = PluginLookupRequest(field=key, query=query)
        except ValidationError:
            return [], (
                "That search is longer than this page will send. Shorten it and "
                "search again."
            )
        try:
            answer = await _op(request, "lookup")(name=name, body=body, request=request)
        except HTTPException as exc:
            return [], _refusal(exc)
        return list(answer.results), answer.message

    @router.get(
        "/plugins/{name}/settings/{key}/search/fragment",
        response_class=HTMLResponse,
        summary="Search to fill a setting (ADR-0016)",
    )
    async def plugin_search_fragment(
        request: Request, name: str, key: str, q: str = "", entry: str = "0"
    ) -> HTMLResponse:
        """One search, one list of results, and nothing written.

        This is a general mechanism, not a weather feature: which tool runs is
        read from *this* plugin's ``config.schema.json``, the results are
        rendered as data, and the detail line beside each one is built from
        whatever keys that schema asked to fill.
        """
        _plugin_name_or_404(name)
        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == name), None)
        field = _group_field(_schema_or_404(name, view), key)
        position = _entry_index(entry)
        results, message = await _run_lookup(request, name, key, q)
        return templates.TemplateResponse(
            request=request,
            name="fragments/search_results.html",
            context={
                "pname": name,
                "field": {"key": field.key},
                "entry_index": position,
                "query": q,
                "message": message,
                "results": search_rows(results),
            },
        )

    @router.get(
        "/plugins/{name}/settings/{key}/fill/fragment",
        response_class=HTMLResponse,
        summary="Fill one entry from a search result (ADR-0016)",
    )
    async def plugin_fill_fragment(
        request: Request,
        name: str,
        key: str,
        q: str = "",
        entry: str = "0",
        pick: str = "0",
    ) -> HTMLResponse:
        """Re-render one entry with the picked result merged over what was typed.

        The values come from running the same search again rather than from the
        request, so what lands in the boxes is what the plugin's tool said and
        not what a caller claimed it said. It is still ordinary form input when
        it comes back - validated on save exactly as typing it would be.
        """
        _plugin_name_or_404(name)
        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == name), None)
        field = _group_field(_schema_or_404(name, view), key)
        position = _entry_index(entry)
        submitted = form_inputs(request.query_params.multi_items())
        results, _message = await _run_lookup(request, name, key, q)
        chosen = _entry_index(pick)
        if chosen < len(results):
            submitted = fill_entry_inputs(field, position, results[chosen], submitted)
        typed = submitted.get(f"{ENTRY_KEY_PREFIX}{field.key}.{position}")
        return _entry_fragment(
            request,
            name,
            field,
            entry_context(
                field,
                position,
                key=str(typed[0]).strip() if typed else "",
                values={},
                submitted=submitted,
                state=STATE_UNSAVED,
            ),
        )
