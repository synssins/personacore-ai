"""The transcript and trace view (spec section 9).

Read-only - a record, not a control surface. "If you can't see what it did, you
can't trust it or debug it."
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from personacore.admin.models import (
    TraceEntry,
    TraceKind,
    TracePage,
)
from personacore.audit.models import (
    AuditCategory,
    Surface,
)
from personacore.web.shared import (
    PERSONA_UNRECORDED,
    UIContext,
    _human_gap,
)

#: The design's surface filter, in the design's own words, mapped onto the
#: audit store's :class:`Surface`. ``all`` is the absence of a filter rather
#: than a surface, so it is ``None``.
SURFACE_FILTERS: dict[str, Surface | None] = {
    "all": None,
    "voice": Surface.VOICE,
    "api": Surface.API,
    "admin": Surface.ADMIN_UI,
    "anonymous": Surface.ANONYMOUS,
}

_SURFACE_WORD: dict[Surface, str] = {
    Surface.VOICE: "voice",
    Surface.API: "api",
    Surface.ADMIN_UI: "admin",
    Surface.ANONYMOUS: "anonymous",
    Surface.SYSTEM: "system",
}

#: What an empty log list says, per filter. Spec section 9 wants plain English,
#: and "no results" is the least useful thing a filtered view can say: each of
#: these names the reason there is nothing rather than the fact.
_EMPTY_TEXT: dict[str, str] = {
    "all": "Nothing recorded yet. Conversations appear here as they happen.",
    "voice": (
        # Not "no voice is installed". Voice is core (ADR-0029) and several
        # were already installed, so that sentence sent the owner looking at
        # the Voices screen for a fault that was never there. The real reason is that
        # nothing writes `Surface.VOICE`: a turn that arrives over Wyoming is
        # never recorded, so this view stays empty however much is spoken.
        "Nothing recorded on the voice surface. Spoken turns are not written "
        "to the log yet."
    ),
    "api": (
        "Nothing recorded from the API. Issue an access key and point a client "
        "at it to see exchanges here."
    ),
    "admin": "Nothing said from the admin interface yet.",
    "anonymous": "Nothing recorded from anonymous callers.",
}

LOG_RECORD_WINDOW = 200
"""Records pulled from the store to build one page of the log view.

The store paginates *records*; this screen shows *exchanges*, and one exchange
is several records. A fixed window read then grouped is honest about that —
asking for "50 exchanges" from a record-oriented query would mean guessing how
many records that is.
"""

LOG_EXCHANGES = 40
"""Exchanges rendered per page, once the window above has been grouped."""


def _tools_called(rows: Sequence[TraceEntry]) -> list[str]:
    """Tool names called during one exchange, in order, without repeats."""
    names: list[str] = []
    for row in rows:
        if row.kind is not TraceKind.AUDIT or row.category != AuditCategory.TOOL_CALL.value:
            continue
        if row.action and row.action not in names:
            names.append(row.action)
    return names


def log_exchanges(page: TracePage, *, limit: int = LOG_EXCHANGES) -> list[dict[str, Any]]:
    """Group a trace page into the conversation exchanges the design renders.

    The store returns a flat, time-ordered stream of audit records and
    transcript messages; the design's log entry is one exchange — what was
    asked, what was answered, and what the system did in between. The
    correlation id is what joins them, and it exists for exactly this (see
    :class:`personacore.audit.models.AuditRecord`).

    **Groups with no transcript message are left out.** An admin change is an
    audit record with no question and no reply, and this design has no shape
    for one: rendering it would put the words "asked" and "replied" around
    fields that are empty. Those records are still in the JSON trace API — see
    the slice report; the design has no admin-change view yet.
    """
    grouped: dict[str, list[TraceEntry]] = {}
    for entry in page.entries:
        grouped.setdefault(entry.correlation_id, []).append(entry)

    exchanges: list[dict[str, Any]] = []
    for correlation_id, unsorted in grouped.items():
        rows = sorted(unsorted, key=lambda e: e.timestamp)
        said = [row for row in rows if row.kind is TraceKind.TRANSCRIPT]
        if not said:
            continue
        asked = next((row for row in said if row.role == "user"), said[0])
        replied = next((row for row in reversed(said) if row.role == "assistant"), None)
        exchanges.append(
            {
                "id": correlation_id,
                "at": rows[-1].timestamp,
                # UTC, as stored. The design's summary row has no place for a
                # timezone label — noted in the slice report.
                "time": rows[-1].timestamp.astimezone(UTC).strftime("%d %b %H:%M"),
                "surface": _SURFACE_WORD.get(asked.surface, asked.surface.value),
                "client": asked.owner_id,
                "persona": PERSONA_UNRECORDED,
                "message": asked.content or "",
                "reply": (replied.content if replied else "") or "No reply was recorded.",
                # Neither the number of tools offered nor which model answered is
                # written to any store, so the design's facts for them are marked
                # "later" rather than filled with a guess.
                "tools_offered": None,
                "tools_called": _tools_called(rows),
                "model_label": None,
                "duration": _human_gap(rows[-1].timestamp - rows[0].timestamp),
            }
        )
    exchanges.sort(key=lambda e: (e["at"], e["id"]), reverse=True)
    return exchanges[:limit]


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the log view and the surface filter's swap target."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import build_trace_page

    templates = ctx.templates
    audit = ctx.audit
    _shell = ctx.shell


    # -- logs --------------------------------------------------------------

    async def _log_context(surface_filter: str) -> dict[str, Any]:
        chosen = surface_filter if surface_filter in SURFACE_FILTERS else "all"
        page = await build_trace_page(
            audit,
            surface=SURFACE_FILTERS[chosen],
            limit=LOG_RECORD_WINDOW,
            kinds=[TraceKind.AUDIT, TraceKind.TRANSCRIPT],
        )
        return {
            "surface_filter": chosen,
            "entries": log_exchanges(page),
            "empty_text": _EMPTY_TEXT[chosen],
        }

    @router.get("/logs", response_class=HTMLResponse, summary="Transcript and trace view")
    async def logs_page(request: Request, surface: str = "all") -> HTMLResponse:
        """Spec section 9: "if you can't see what it did, you can't trust it or
        debug it." Read-only — a record, not a control surface."""
        return templates.TemplateResponse(
            request=request,
            name="logs.html",
            context={**await _shell(request, "logs"), **await _log_context(surface)},
        )

    @router.get(
        "/logs/fragment", response_class=HTMLResponse, summary="Filtered log entries"
    )
    async def logs_fragment(request: Request, surface: str = "all") -> HTMLResponse:
        """The surface filter's swap target — entries only, no shell."""
        return templates.TemplateResponse(
            request=request,
            name="fragments/log_entries.html",
            context=await _log_context(surface),
        )
