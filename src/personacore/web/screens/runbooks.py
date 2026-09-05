"""The Runbooks screen (contract ``working/contracts/runbook.md`` section 6,
plan ``working/team/alpha17/PLAN.md`` row ``screen``).

A runbook is a YAML file a plugin author or the household writes; this alpha
builds no runner (PLAN.md: "nothing executes yet") — only upload, listing,
validation and delete, plus the two switches that will gate a run once one
exists. Everything this screen needs from the actual store lives behind
``app.state.runbooks``, built by the ``validator`` subtask running in
parallel. **This module never imports anything from
``personacore.runbooks.*`` or ``personacore.config.runbooks``** — those
packages are somebody else's work in flight and may not exist in this tree
yet. Instead this screen talks to whatever object it finds at
``app.state.runbooks`` in the exact shape PLAN.md's Joints section promises
(``list()``, ``put()``, ``delete()``, ``plugin_enabled()``,
``set_plugin_enabled()``), duck-typed rather than imported. When the
attribute is absent — a core assembled without the runbooks package wired in
yet, or simply not this build — the page renders a plain sentence instead of
raising, per this task's own brief.

The same reasoning applies to the validator's ``ValidationError``: it is
never imported. An upload that a real store refuses is expected to raise
something carrying a ``messages: list[str]`` attribute (PLAN.md's Joints), so
that attribute is read with ``getattr`` and any exception at all — not just
that one class — is turned into a sentence on the page rather than a 500.

**Requires and Installed.** ``RunbookRecord`` carries ``requires: dict[str,
str]`` (plugin name -> minimum-version specifier, contract section 2's
``requires.plugins``) and ``installed: dict[str, str | None]`` (plugin name ->
installed version, or ``None`` when it is not installed) — spec section 6's
two columns, straight from the store, one line per plugin. Both are read with
``getattr(record, "requires", {})`` / ``getattr(record, "installed", {})``
rather than a bare attribute: a store built before these fields landed still
renders, with those two columns simply empty rather than a crash.

``RunbookRecord.verdict`` (Joints: ``Verdict.ok``, ``Verdict.reasons``) still
carries a compatibility *sentence* for the Verdict column — "Compatible." when
``ok``, else the reasons it is not (or "Not compatible." when a non-``ok``
verdict carries no reasons of its own).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.web.screens.plugin_common import (
    plugin_name_or_404,
    plugin_supports_runbooks,
)
from personacore.web.shared import UIContext, current_config

RUNBOOKS_PATH = "/admin/runbooks"
"""Where the screen lives — the nav link's own address."""

RUNBOOKS_ROUTE = "/runbooks"

RUNBOOK_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
"""Contract section 2: a runbook's own identifier, ``[a-z0-9-]{1,40}``. Bound
before it ever reaches a store call, the same way :func:`plugin_name_or_404`
bounds a plugin name before it reaches one (spec section 7)."""

RUNBOOKS_UNAVAILABLE = "Runbooks are not available in this build."
"""What renders when ``app.state.runbooks`` is absent — a core assembled
without the package wired in, or one running an older build entirely. Never a
500 and never a blank page: the brief for this screen is explicit that this is
the whole of what "not available" looks like."""

RUNBOOKS_OFF_BANNER = "Runbook runs are off. Turn them on in Core settings."
"""Contract section 1.9's exact wording for the off state, shown when
``[runbooks] enabled`` is false. Files still list, still validate and still
delete; only running is gated, and there is no runner yet regardless."""

PLUGIN_OFF_REASON = "runbooks are off for this plugin"
"""Contract section 1.10's exact wording, shown beside a runbook whose own
plugin has not switched runbooks on."""

VERDICT_COMPATIBLE = "Compatible."

INSTALLED_NOT_INSTALLED = "not installed"

SOURCE_BUNDLED = "bundled"

SOURCE_UPLOADED = "uploaded"

UPLOAD_NO_FILE = "Nothing was uploaded: no file was chosen. Pick a .yaml or .zip and try again."

UPLOAD_NO_PLUGIN = "Nothing was uploaded: choose which plugin this runbook is for."

UPLOAD_PLUGIN_NOT_ELIGIBLE = (
    "Nothing was uploaded: {plugin!r} does not declare "
    "“[runbooks] supported = true”, so it cannot take an upload."
)

UPLOAD_UNAVAILABLE_REFUSAL = "Nothing was uploaded: runbooks are not available in this build."

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
"""Contract section 6's upload limit for one runbook file. Enforced here,
before the store ever sees the bytes — this screen reads at most one byte
past this limit, so a large upload is never buffered in full just to be
refused."""

UPLOAD_EXTENSIONS = (".yaml", ".yml", ".zip")
"""The only extensions a runbook upload may carry, checked case-insensitively
against the filename alone, before a single byte is read."""

UPLOAD_WRONG_TYPE = "A runbook is a .yaml file or a .zip holding one."

UPLOAD_TOO_LARGE = "That file is larger than 2 MB, which is the limit for a runbook."

GENERIC_UPLOAD_REFUSAL = "That file was refused."

UPLOAD_OK = "{title} ({version}) uploaded for {plugin}."

DELETE_TITLE = "Delete {title}?"

DELETE_BODY = (
    "This removes {title} ({id}) from {plugin}'s runbooks. The file and any "
    "prompt files that came with it are deleted. This cannot be undone; a "
    "bundled copy is restored the next time {plugin} is installed or "
    "reinstalled, but an uploaded one is gone for good."
)

DELETE_LABEL = "Delete this runbook"

DELETE_NOT_FOUND = "There is no such runbook."

DELETE_UNAVAILABLE = "Runbooks are not available in this build, so nothing can be deleted."


def _format_requires(requires: dict[str, str]) -> list[str]:
    """One "plugin specifier" line per required plugin (contract section 6),
    e.g. ``storybook >=2.1.0``."""
    return [f"{plugin} {specifier}" for plugin, specifier in requires.items()]


def _format_installed(installed: dict[str, str | None]) -> list[str]:
    """One "plugin version" line per required plugin, e.g. ``storybook
    2.3.1``, or ``storybook not installed`` when the version is ``None``."""
    return [
        f"{plugin} {version}" if version else f"{plugin} {INSTALLED_NOT_INSTALLED}"
        for plugin, version in installed.items()
    ]


def _eligible_plugins(ctx: UIContext, listing: Any) -> list[str]:
    """Every installed plugin whose manifest declares
    ``[runbooks] supported = true`` (contract section 6), alphabetically —
    the whole of what the upload selector and the picker (a later alpha) may
    ever offer."""
    names = [view.name for view in listing.plugins]
    return sorted(name for name in names if plugin_supports_runbooks(ctx.layout, name))


def runbook_row(record: Any, *, plugin_enabled: bool) -> dict[str, Any]:
    """One :class:`~personacore.runbooks.store.RunbookRecord` (duck-typed —
    see the module docstring) as the template wants it.

    ``record.valid is False`` takes over the Verdict cell: an invalid file's
    own problems (from the validator, at upload/scan time) are what an
    operator needs to see, and a compatibility verdict about a file that will
    not even parse would be a second, less useful thing to say about the
    same failure. Requires and Installed render regardless — they are the
    file's own declared needs and the scan's own facts, valid or not.
    """
    verdict = record.verdict
    valid = bool(record.valid)
    ok = bool(getattr(verdict, "ok", False)) if valid else False
    reasons = list(getattr(verdict, "reasons", None) or [])
    if not valid:
        verdict_text = ""
    elif ok:
        verdict_text = VERDICT_COMPATIBLE
    else:
        verdict_text = " ".join(reasons) if reasons else "Not compatible."
    requires = getattr(record, "requires", None) or {}
    installed = getattr(record, "installed", None) or {}
    return {
        "plugin": record.plugin,
        "id": record.id,
        "title": record.title or record.id,
        "version": record.version,
        "requires": _format_requires(requires),
        "installed": _format_installed(installed),
        "source": SOURCE_BUNDLED if record.bundled else SOURCE_UPLOADED,
        "valid": valid,
        "problems": list(record.problems or []),
        "verdict_ok": ok,
        "verdict_text": verdict_text,
        "plugin_enabled": plugin_enabled,
        "greyed": not plugin_enabled,
        "grey_reason": PLUGIN_OFF_REASON,
    }


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the Runbooks screen: the list, the upload, and delete's
    plain-page confirmation (the pattern ``keys.py``'s key revocation uses)."""
    templates = ctx.templates
    _shell = ctx.shell
    layout = ctx.layout

    def _store(request: Request) -> Any | None:
        """``request.app.state.runbooks``, or ``None``.

        ``getattr`` rather than a bare attribute read, the same tolerance
        ``memory.py``'s own ``_store`` gives ``memory_store``: a core
        assembled without this wired in yet must render the "not available"
        line rather than raise ``AttributeError``.
        """
        return getattr(request.app.state, "runbooks", None)

    def _core_runbooks_enabled() -> bool:
        """``[runbooks] enabled`` off ``core.toml`` — read the document the
        same way every other screen's switch is (``current_config``), not off
        ``app.state``, so what this screen says is what the file says."""
        current, _unreadable = current_config(layout)
        if current is None:
            return False
        section = current.settings.get("runbooks")
        if isinstance(section, dict):
            return bool(section.get("enabled", False))
        return False

    async def _rows(request: Request, store: Any) -> list[dict[str, Any]]:
        records = store.list()
        return [
            runbook_row(record, plugin_enabled=bool(store.plugin_enabled(record.plugin)))
            for record in records
        ]

    async def _page_context(
        request: Request, *, upload_result: dict[str, str] | None = None
    ) -> dict[str, Any]:
        store = _store(request)
        core_enabled = _core_runbooks_enabled()
        base = {
            **await _shell(request, "runbooks"),
            "available": store is not None,
            "unavailable_message": RUNBOOKS_UNAVAILABLE,
            "core_enabled": core_enabled,
            "banner": None if core_enabled else RUNBOOKS_OFF_BANNER,
            "rows": [],
            "plugin_choices": [],
            "upload_result": upload_result,
        }
        if store is None:
            return base
        listing = await ctx.scans.current()
        base["rows"] = await _rows(request, store)
        base["plugin_choices"] = _eligible_plugins(ctx, listing)
        return base

    @router.get(
        RUNBOOKS_ROUTE, response_class=HTMLResponse, summary="Runbooks: upload, list, delete"
    )
    async def runbooks_page(request: Request) -> HTMLResponse:
        """Contract section 6's screen. Lists and validates whatever switch
        state the core is in — only *running* one is gated (there is no
        runner in this alpha at all)."""
        return templates.TemplateResponse(
            request=request, name="runbooks.html", context=await _page_context(request)
        )

    @router.post(RUNBOOKS_ROUTE, response_class=HTMLResponse, summary="Upload a runbook")
    async def runbooks_upload(request: Request) -> HTMLResponse:
        """Validate on upload (contract section 6) — a refusal is always a
        sentence on this page, never a 500, whatever the store raises.

        Two checks run before the store ever sees the bytes (this task's own
        brief): the filename's extension, checked before anything is read,
        and a 2 MB cap, enforced by never reading past one byte over it."""
        form = await request.form()
        try:
            plugin = str(form.get("plugin") or "").strip()
            upload = form.get("archive")
            filename = str(getattr(upload, "filename", "") or "")
            wrong_type = bool(filename) and not filename.lower().endswith(UPLOAD_EXTENSIONS)
            data = b""
            too_large = False
            if filename and not wrong_type and hasattr(upload, "read"):
                # Read the bytes while the multipart envelope is still open —
                # a spooled upload is not necessarily readable once
                # `form.close()` has run (see `plugin_install.py`'s own
                # `_review_upload`, which reads before closing for the same
                # reason). Capped at one byte past the limit: enough to tell
                # "too large" from "exactly at the limit" without ever
                # buffering a large upload in full.
                data = await upload.read(MAX_UPLOAD_BYTES + 1)
                too_large = len(data) > MAX_UPLOAD_BYTES
        finally:
            await form.close()

        store = _store(request)
        if store is None:
            return templates.TemplateResponse(
                request=request,
                name="runbooks.html",
                context=await _page_context(
                    request,
                    upload_result={"kind": "refused", "message": UPLOAD_UNAVAILABLE_REFUSAL},
                ),
            )
        listing = await ctx.scans.current()
        eligible = _eligible_plugins(ctx, listing)
        if not plugin:
            message = UPLOAD_NO_PLUGIN
        elif plugin not in eligible:
            message = UPLOAD_PLUGIN_NOT_ELIGIBLE.format(plugin=plugin)
        elif not filename:
            message = UPLOAD_NO_FILE
        elif wrong_type:
            message = UPLOAD_WRONG_TYPE
        elif too_large:
            message = UPLOAD_TOO_LARGE
        else:
            try:
                record = store.put(plugin, filename, data)
            except Exception as exc:  # noqa: BLE001 - never a 500 on an upload; see module docstring
                sentences = list(getattr(exc, "messages", None) or [])
                if not sentences:
                    sentences = [str(exc).strip() or GENERIC_UPLOAD_REFUSAL]
                return templates.TemplateResponse(
                    request=request,
                    name="runbooks.html",
                    context=await _page_context(
                        request,
                        upload_result={"kind": "refused", "message": " ".join(sentences)},
                    ),
                )
            message = UPLOAD_OK.format(
                title=getattr(record, "title", None) or getattr(record, "id", plugin),
                version=getattr(record, "version", ""),
                plugin=plugin,
            )
            return templates.TemplateResponse(
                request=request,
                name="runbooks.html",
                context=await _page_context(
                    request, upload_result={"kind": "ok", "message": message}
                ),
            )
        return templates.TemplateResponse(
            request=request,
            name="runbooks.html",
            context=await _page_context(
                request, upload_result={"kind": "refused", "message": message}
            ),
        )

    def _runbook_id_or_404(runbook_id: str) -> str:
        if not RUNBOOK_ID_PATTERN.match(runbook_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, DELETE_NOT_FOUND)
        return runbook_id

    async def _record_or_404(request: Request, plugin: str, runbook_id: str) -> dict[str, Any]:
        plugin_name_or_404(plugin)
        _runbook_id_or_404(runbook_id)
        store = _store(request)
        if store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, DELETE_UNAVAILABLE)
        for record in store.list():
            if record.plugin == plugin and record.id == runbook_id:
                return {"plugin": plugin, "id": runbook_id, "title": record.title or record.id}
        raise HTTPException(status.HTTP_404_NOT_FOUND, DELETE_NOT_FOUND)

    def _delete_confirm_context(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": DELETE_TITLE.format(title=row["title"]),
            "body": DELETE_BODY.format(title=row["title"], id=row["id"], plugin=row["plugin"]),
            "confirm_label": DELETE_LABEL,
        }

    @router.get(
        "/runbooks/{plugin}/{runbook_id}/delete/confirm",
        response_class=HTMLResponse,
        summary="Confirm deleting one runbook (page)",
    )
    async def runbook_delete_confirm_page(
        request: Request, plugin: str, runbook_id: str
    ) -> HTMLResponse:
        """The no-script fallback (ADR-0020), same shape as
        ``keys.py``'s ``key_revoke_confirm_page``."""
        row = await _record_or_404(request, plugin, runbook_id)
        return templates.TemplateResponse(
            request=request,
            name="confirm_page.html",
            context={
                **await _shell(request, "runbooks"),
                **_delete_confirm_context(row),
                "action": f"/admin/runbooks/{plugin}/{runbook_id}/delete",
                "back_href": RUNBOOKS_PATH,
                "back_label": "← Runbooks",
            },
        )

    @router.get(
        "/runbooks/{plugin}/{runbook_id}/delete/confirm/fragment",
        response_class=HTMLResponse,
        summary="Confirm deleting one runbook",
    )
    async def runbook_delete_confirm(
        request: Request, plugin: str, runbook_id: str
    ) -> HTMLResponse:
        row = await _record_or_404(request, plugin, runbook_id)
        return templates.TemplateResponse(
            request=request,
            name="fragments/confirm.html",
            context={
                **_delete_confirm_context(row),
                "action": f"/admin/runbooks/{plugin}/{runbook_id}/delete",
                "target": "body",
            },
        )

    @router.post(
        "/runbooks/{plugin}/{runbook_id}/delete",
        response_class=HTMLResponse,
        response_model=None,
        summary="Delete one runbook",
    )
    async def runbook_delete(
        request: Request, plugin: str, runbook_id: str
    ) -> HTMLResponse | RedirectResponse:
        """Same shape as ``keys.py``'s ``key_revoke``: a plain form post gets a
        real redirect, an htmx caller gets the fragment it asked the dialog
        for — except there is no swap-target list fragment for this screen
        yet, so both paths land on the full page, which is always correct."""
        plugin_name_or_404(plugin)
        _runbook_id_or_404(runbook_id)
        store = _store(request)
        if store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, DELETE_UNAVAILABLE)
        store.delete(plugin, runbook_id)
        if request.headers.get("HX-Request", "").lower() == "true":
            return templates.TemplateResponse(
                request=request, name="runbooks.html", context=await _page_context(request)
            )
        return RedirectResponse(RUNBOOKS_PATH, status_code=status.HTTP_303_SEE_OTHER)


__all__ = [
    "DELETE_BODY",
    "DELETE_LABEL",
    "DELETE_NOT_FOUND",
    "DELETE_TITLE",
    "DELETE_UNAVAILABLE",
    "GENERIC_UPLOAD_REFUSAL",
    "INSTALLED_NOT_INSTALLED",
    "MAX_UPLOAD_BYTES",
    "PLUGIN_OFF_REASON",
    "RUNBOOKS_OFF_BANNER",
    "RUNBOOKS_PATH",
    "RUNBOOKS_ROUTE",
    "RUNBOOKS_UNAVAILABLE",
    "RUNBOOK_ID_PATTERN",
    "SOURCE_BUNDLED",
    "SOURCE_UPLOADED",
    "UPLOAD_EXTENSIONS",
    "UPLOAD_NO_FILE",
    "UPLOAD_NO_PLUGIN",
    "UPLOAD_OK",
    "UPLOAD_PLUGIN_NOT_ELIGIBLE",
    "UPLOAD_TOO_LARGE",
    "UPLOAD_UNAVAILABLE_REFUSAL",
    "UPLOAD_WRONG_TYPE",
    "VERDICT_COMPATIBLE",
    "register",
    "runbook_row",
]
