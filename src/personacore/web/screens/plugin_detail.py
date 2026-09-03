"""One plugin's page: health, switches, the generated settings form and the
raw ``config.toml`` editor (spec section 5.1).

**It opens when the plugin does not.** A plugin that failed to start is the
most likely reason anyone comes here, so every part of the page that can still
work does, and the page says which part cannot.

The repeating-group and search-and-fill fragments this page's form uses are in
``plugin_entries``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from personacore.admin.config_io import ConfigRejected
from personacore.admin.models import (
    MAX_PLUGIN_CONFIG_CHARS,
    PluginConfigResponse,
    PluginConfigUpdateRequest,
)
from personacore.admin.plugin_config_io import parse_plugin_toml
from personacore.admin.plugin_form import (
    apply_values,
    changed_keys,
    current_values,
    form_inputs,
    is_unset,
    settings_table,
    validate_submission,
    written_keys,
)
from personacore.web.plugin_page import (
    BROKEN_CONFIG_NOTE,
    NO_SCHEMA_NOTE,
    NO_TABLE_NOTE,
    NOTHING_RENDERABLE_NOTE,
    field_context,
    field_errors,
    health_context,
)
from personacore.web.screens.plugin_common import (
    SECRET_NONE_YET,
    SECRET_OPTIONAL_NOTE,
    SECRET_OPTIONAL_WORD,
    SECRET_STORED,
    SECRETS_NONE_SUBMITTED,
    SECRETS_STORED_NOTE,
    WAITING_WORD,
    authorise,
    can,
    declared_secrets,
    op,
    plugin_config,
    plugin_name_or_404,
    plugin_schema,
    secret_fields_from,
    secret_names,
    secret_requests,
    store_secrets,
    waiting_for,
    waiting_sentence,
)
from personacore.web.shared import (
    NO_PLUGIN_OPERATIONS,
    UIContext,
    api_handler,
    refusal,
)

RAW_TOO_LARGE = (
    "Not saved: those settings are larger than {limit} characters. Nothing was "
    "written."
)

FORM_TOO_LARGE = (
    "Not saved: those settings would make config.toml larger than {limit} "
    "characters. Nothing was written."
)


def saved_message(
    path: str, changed: Sequence[str], values: Mapping[str, Any], api_message: str
) -> str:
    """What the form says after a successful save.

    Names what was written and where, then hands over to the API's own sentence
    — which is the half that says whether the plugin was restarted, and is the
    only part of this screen entitled to make that claim.

    ``changed`` is what reads differently on disk afterwards, not everything the
    form posted: a form posts every field it shows, and listing settings that
    did not move is a quieter version of reporting a save that wrote nothing.
    """
    moved = set(changed)
    written = [key for key in written_keys(values) if key in moved]
    cleared = sorted(key for key, value in values.items() if is_unset(value) and key in moved)
    count = len(written) + len(cleared)
    lead = (
        f"Wrote {count} setting{'' if count == 1 else 's'} to {path}."
        if count
        else f"Wrote to {path}."
    )
    if written:
        lead = f"Wrote {', '.join(written)} to {path}."
    if cleared:
        one = len(cleared) == 1
        lead += (
            f" {', '.join(cleared)} {'was' if one else 'were'} left empty, so "
            f"{'it is' if one else 'they are'} unset — the plugin uses its "
            "default."
        )
    return f"{lead} {api_message}".strip()


def unchanged_message(path: str) -> str:
    """Why nothing was written, when nothing needed to be.

    **The message has to be true.** A page that says "saved" when the file is
    byte-for-byte what it was is worse than one that errors: the operator
    believes it and walks away from an edit that did not happen. Naming the file
    matters too — a plugin's settings exist in two places that look alike, the
    appdata copy this page reads and writes and the copy in the plugin's source
    folder, and a reader checking the wrong one sees a file that never changes.
    """
    return (
        f"Nothing changed — {path} already holds these values, so nothing was "
        "written or restarted. A copy in a plugin's source folder is a "
        "different file and is not what runs."
    )


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the per-plugin page, its two save posts and its switches."""
    templates = ctx.templates
    scans = ctx.scans
    _shell = ctx.shell
    # Borrowed from `plugin_common`, under the names they had while they were
    # closures on the factory - the code below is unchanged from when it was.
    _authorise = authorise
    _can = can
    _op = op
    _plugin_config = plugin_config
    _plugin_name_or_404 = plugin_name_or_404
    _plugin_schema = partial(plugin_schema, ctx.layout)
    _refusal = refusal
    _secret_names = partial(secret_names, ctx.layout)
    layout = ctx.layout


    def _settings_context(
        name: str,
        config: PluginConfigResponse,
        view: Any,
        *,
        submitted: dict[str, list[str]] | None = None,
        errors: Mapping[str, str] | None = None,
        entry_errors: Mapping[tuple[str, str, str], str] | None = None,
    ) -> dict[str, Any]:
        """The generated form's half of the page: fields, notes, or the reason
        there is no form.

        Every branch ends in a page. A missing schema, an unreadable one, a
        ``config.toml`` that does not parse, a table the core cannot identify -
        none of them is allowed to become a refusal to open, because each one is
        a state an operator arrived here to fix.
        """
        context: dict[str, Any] = {
            "fields": [],
            "schema_notes": [],
            "unrenderable": [],
            "form_note": "",
            "schema": None,
            "table": None,
        }
        schema, refusal = _plugin_schema(name)
        if schema is None:
            context["form_note"] = refusal or NO_SCHEMA_NOTE
            return context
        context["schema"] = schema
        context["schema_notes"] = [*schema.notes, *_authorise(schema, view)]
        context["unrenderable"] = [
            {"key": item.key, "label": item.label, "reason": item.reason}
            for item in schema.unrenderable
        ]
        document, _problem = parse_plugin_toml(
            config.content, Path(config.path or "config.toml")
        )
        if document is None:
            context["form_note"] = BROKEN_CONFIG_NOTE
            return context
        table = settings_table(document, schema, name)
        if table is None:
            context["form_note"] = NO_TABLE_NOTE
            return context
        if not schema.renderable:
            context["form_note"] = NOTHING_RENDERABLE_NOTE
            return context
        context["table"] = table
        values = current_values(document, table)
        # This plugin's own namespace, never the core's and never another
        # plugin's: the picker offers what THIS plugin has been given
        # (ADR-0025 section 1). The name comes from the screen's path
        # segment, already through `plugin_name_or_404`, not from the form.
        available = _secret_names(name)
        context["fields"] = [
            field_context(
                field,
                values=values,
                submitted=submitted,
                secret_names=available,
                errors=errors,
                entry_errors=entry_errors,
            )
            for field in schema.fields
        ]
        return context

    async def _detail_context(
        request: Request,
        name: str,
        *,
        tab: str = "form",
        save_result: dict[str, str] | None = None,
        raw_error: str | None = None,
        raw_saved: str | None = None,
        secret_result: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Everything one plugin's page shows, from one scan and one file read."""
        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == name), None)
        failure = next((row for row in listing.failures if row.name == name), None)
        config = await _plugin_config(request, name)
        settings = (
            _settings_context(name, config, view)
            if config is not None
            else {
                "fields": [],
                "schema_notes": [],
                "unrenderable": [],
                "form_note": NO_PLUGIN_OPERATIONS,
            }
        )
        plugin = health_context(name, view, failure)
        plugin["fields"] = settings["fields"]
        plugin["raw_text"] = config.content if config is not None else ""
        plugin["config_path"] = config.path if config is not None else ""
        # The credentials this plugin asked for, and which of them it has
        # (ADR-0025 section 4). One list behind the fields, the state word and
        # the sentence, so the page cannot draw a box for something it says is
        # supplied. Each request carries the author's own description straight
        # out of the manifest (ADR-0026); nothing here reads a value.
        requests = secret_requests(layout, name)
        plugin["secrets"] = requests
        plugin["waiting"] = waiting_for(requests)
        return {
            **await _shell(request, "plugins"),
            "plugin": plugin,
            "active_tab": "raw" if tab == "raw" else "form",
            "save_result": save_result,
            "raw_error": raw_error,
            "raw_saved": raw_saved,
            "form_note": settings["form_note"],
            "schema_notes": settings["schema_notes"],
            "unrenderable": settings["unrenderable"],
            "can_toggle": _can(request, "enable", "disable"),
            "can_uninstall": _can(request, "uninstall"),
            "can_save_raw": _can(request, "write_config"),
            "waiting_word": WAITING_WORD,
            "waiting_note": waiting_sentence(plugin["waiting"]) if plugin["waiting"] else "",
            "stored_word": SECRET_STORED,
            "none_word": SECRET_NONE_YET,
            # An optional request is marked as one and gets its own sentence
            # instead of "None yet." — a plugin is never waiting on it
            # (ADR-0026).
            "optional_word": SECRET_OPTIONAL_WORD,
            "optional_note": SECRET_OPTIONAL_NOTE,
            "secret_result": secret_result,
        }

    @router.get(
        "/plugins/{name}",
        response_class=HTMLResponse,
        summary="One plugin: health, switches, settings",
    )
    async def plugin_detail(request: Request, name: str, tab: str = "form") -> HTMLResponse:
        """Spec section 5.1's per-plugin screen - and it opens when the plugin
        does not.

        A plugin that failed to start is the most likely reason anyone opens
        this page, so health, the reason, the switch, the raw editor and (when
        the file still parses) the generated form all render for a broken plugin
        exactly as they do for a healthy one. The only thing a broken plugin
        loses is the part that genuinely cannot work, and the page says which.
        """
        _plugin_name_or_404(name)
        return templates.TemplateResponse(
            request=request,
            name="plugin_detail.html",
            context=await _detail_context(request, name, tab=tab),
        )

    def _form_fragment(
        request: Request,
        name: str,
        settings: dict[str, Any],
        save_result: dict[str, str] | None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="fragments/settings_form.html",
            context={
                "plugin": {"name": name, "fields": settings["fields"]},
                "save_result": save_result,
                "form_note": settings["form_note"],
                "schema_notes": settings["schema_notes"],
                "unrenderable": settings["unrenderable"],
            },
        )

    @router.post(
        "/plugins/{name}/settings",
        response_class=HTMLResponse,
        summary="Save one plugin's generated settings form",
    )
    async def plugin_settings_save(request: Request, name: str) -> HTMLResponse:
        """Validate every field, patch the ``config.toml``, save it through the
        JSON API - which writes atomically, restarts the plugin and audits.

        **Nothing is opened for writing until every value has passed**, and all
        the failures are collected rather than the first: a refused save leaves
        the file untouched and comes back with the operator's own text still in
        the boxes, each wrong one named beside its box.
        """
        _plugin_name_or_404(name)
        inputs = form_inputs((await request.form()).multi_items())
        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == name), None)
        config = await _plugin_config(request, name)
        if config is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NO_PLUGIN_OPERATIONS)
        settings = _settings_context(name, config, view)
        schema, table = settings["schema"], settings["table"]
        if settings["form_note"] or schema is None or table is None:
            return _form_fragment(request, name, settings, None)

        try:
            values = validate_submission(
                schema, inputs, table=table, secret_names=_secret_names(name)
            )
            content = apply_values(config.content, table, values)
        except ConfigRejected as exc:
            scalar, entry = field_errors(exc.problems, table)
            refused = _settings_context(
                name, config, view, submitted=inputs, errors=scalar, entry_errors=entry
            )
            count = len(exc.problems)
            return _form_fragment(
                request,
                name,
                refused,
                {
                    "kind": "invalid",
                    "message": (
                        f"Not saved - {count} field{'' if count == 1 else 's'} need "
                        "attention above. Nothing was written."
                    ),
                },
            )
        except ValueError as exc:
            refused = _settings_context(name, config, view, submitted=inputs)
            return _form_fragment(
                request, name, refused, {"kind": "invalid", "message": f"Not saved: {exc}"}
            )

        if len(content) > MAX_PLUGIN_CONFIG_CHARS:
            return _form_fragment(
                request,
                name,
                settings,
                {
                    "kind": "invalid",
                    "message": FORM_TOO_LARGE.format(limit=MAX_PLUGIN_CONFIG_CHARS),
                },
            )
        if content == config.content and config.exists:
            return _form_fragment(
                request,
                name,
                settings,
                {"kind": "nothing", "message": unchanged_message(config.path)},
            )

        changed = changed_keys(config.content, content, table, values)
        try:
            outcome = await _op(request, "write_config")(
                name=name,
                body=PluginConfigUpdateRequest(content=content),
                request=request,
            )
        except HTTPException as exc:
            return _form_fragment(
                request,
                name,
                settings,
                {"kind": "invalid", "message": f"Not saved: {_refusal(exc)}"},
            )
        # Re-read: what the boxes show afterwards is what is on disk, so a saved
        # value stops being drawn as an unsaved one the moment it is saved.
        fresh = await _plugin_config(request, name)
        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == name), None)
        return _form_fragment(
            request,
            name,
            _settings_context(name, fresh or config, view),
            {
                "kind": "saved",
                "message": saved_message(
                    outcome.config.path, changed, values, outcome.message
                ),
            },
        )

    @router.post(
        "/plugins/{name}/settings/raw",
        response_class=HTMLResponse,
        summary="Save one plugin's config.toml as text",
    )
    async def plugin_settings_raw(request: Request, name: str) -> HTMLResponse:
        """The raw editor - a fallback *beside* the form, never instead of it.

        Same write path as the form and as the JSON API: text in, parsed before
        anything is opened, written atomically, plugin restarted, audited.
        """
        _plugin_name_or_404(name)
        form = await request.form()
        content = str(form.get("raw") or "")
        raw_error: str | None = None
        raw_saved: str | None = None
        if len(content) > MAX_PLUGIN_CONFIG_CHARS:
            raw_error = RAW_TOO_LARGE.format(limit=MAX_PLUGIN_CONFIG_CHARS)
        else:
            try:
                outcome = await _op(request, "write_config")(
                    name=name,
                    body=PluginConfigUpdateRequest(content=content),
                    request=request,
                )
            except HTTPException as exc:
                raw_error = f"Not saved: {_refusal(exc)}"
            else:
                raw_saved = outcome.message
        return templates.TemplateResponse(
            request=request,
            name="plugin_detail.html",
            context=await _detail_context(
                request, name, tab="raw", raw_error=raw_error, raw_saved=raw_saved
            ),
        )

    # -- the credential a plugin asked for (ADR-0025 section 4) ------------

    @router.post(
        "/plugins/{name}/secrets",
        response_class=HTMLResponse,
        summary="Supply a credential this plugin asked for",
    )
    async def plugin_secrets_save(request: Request, name: str) -> HTMLResponse:
        """Store what was pasted, restart the plugin, re-render the page.

        **The operator is never told there is a secret store.** They did not
        create one, name one, or choose where it goes: the plugin's manifest
        asked for a credential, a box appeared on the page they were already on,
        and they pasted into it. That is the whole of ADR-0025 section 4's "one
        action, on the screen they are already on".

        Every posted field is matched against the names *this plugin's manifest*
        declared before anything is written, so a field added to the page cannot
        write into the namespace under a name the plugin never asked for — and
        the namespace is the plugin's own, so it could not reach another
        plugin's credential even if it could name one.

        **Nothing comes back.** The response re-renders this page from disk, and
        the boxes are empty on it — as they are on every render, including this
        one. What the page says instead is whether a credential is stored, in
        those words (ADR-0025 section 5).

        An empty form is not an error: it is somebody who opened the page,
        thought better of it and pressed the button. Said as a sentence rather
        than a refusal.
        """
        _plugin_name_or_404(name)
        declared = declared_secrets(layout, name)
        form = await request.form()
        try:
            submitted = secret_fields_from(form)
        finally:
            await form.close()
        stored, refusals = store_secrets(layout, name, submitted, declared)
        if refusals:
            # The store's own sentence, which names the secret and never its
            # value. Refusals win the banner: a page reporting "stored" while
            # one of two credentials was rejected is the interface lying about
            # what it holds.
            result = {"kind": "refused", "message": " ".join(refusals)}
        elif stored:
            result = {
                "kind": "ok",
                "message": SECRETS_STORED_NOTE.format(
                    names=", ".join(stored), plugin=name
                ),
            }
            # A plugin reads its environment when it starts, so a credential
            # supplied after it gave up is not in use until it starts again.
            # Off then on, through the JSON API's own handlers — the same two
            # calls a saved setting makes, with the same audit records.
            await _restart(request, name)
        else:
            result = {"kind": "nothing", "message": SECRETS_NONE_SUBMITTED}
        return templates.TemplateResponse(
            request=request,
            name="plugin_detail.html",
            context=await _detail_context(request, name, secret_result=result),
        )

    async def _restart(request: Request, name: str) -> None:
        """Off, then on, through the JSON API's own handlers.

        Nothing here stops or starts a subprocess itself, so the ordering and
        the audit records are the API's. A core assembled without the plugin
        API simply does not restart, and the credential is still stored and in
        use at the plugin's next start — which the page's own row then says.
        """
        for operation in ("disable", "enable"):
            handler = api_handler(request.app, operation)
            if handler is None:  # pragma: no cover - no plugin API at all
                return
            try:
                await handler(name=name, request=request)
            except HTTPException:  # pragma: no cover - reported by the page itself
                return

    # -- switch on, switch off ---------------------------------------------

    async def _toggle_page(request: Request, name: str, operation: str) -> HTMLResponse:
        _plugin_name_or_404(name)
        raw_error: str | None = None
        try:
            await _op(request, operation)(name=name, request=request)
        except HTTPException as exc:
            raw_error = f"Not changed: {_refusal(exc)}"
        return templates.TemplateResponse(
            request=request,
            name="plugin_detail.html",
            context=await _detail_context(request, name, raw_error=raw_error),
        )

    @router.post(
        "/plugins/{name}/enable", response_class=HTMLResponse, summary="Switch a plugin on"
    )
    async def plugin_enable(request: Request, name: str) -> HTMLResponse:
        """ADR-0013's toggle, on - through the JSON API's own handler, so the
        choice is persisted first and applied to the running core second."""
        return await _toggle_page(request, name, "enable")

    @router.post(
        "/plugins/{name}/disable",
        response_class=HTMLResponse,
        summary="Switch a plugin off",
    )
    async def plugin_disable(request: Request, name: str) -> HTMLResponse:
        """Off, keeping the folder and every setting in it. Its tools stop being
        offered to the model and stop being callable."""
        return await _toggle_page(request, name, "disable")
