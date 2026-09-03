"""The trace view — spec section 9's "log of what the agent did".

Split out of :mod:`personacore.admin.routes` (ADR-0040). This module owns the
merge of two record families into one timeline, and the one route that pages
through it. It reads the audit store and writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from personacore.admin.api_shared import AdminApiContext
from personacore.admin.models import (
    TraceEntry,
    TraceFilters,
    TraceKind,
    TracePage,
)
from personacore.admin.protocols import AuditGateway
from personacore.audit import (
    AuditRecord,
    Owner,
    Surface,
    TranscriptRecord,
    get_logger,
)

logger = get_logger(__name__)

MAX_TRACE_LIMIT = 200
MAX_TRACE_WINDOW = 2000
"""Ceiling on rows pulled from the store to satisfy one page. Offset pagination
over a descending-timestamp query means page N costs N pages of rows; this caps
that at something a container can hold, and ``has_more`` stays truthful because
the window is always fetched one row longer than the page needs."""


def _normalise_moment(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive timestamp.

    The store refuses naive timestamps on write for good reasons
    (``personacore.audit.models``), and a query filter is the same ambiguity
    read backwards. A caller who omits the offset means UTC, which is what the
    stored values are, so saying so is better than a 422 about ``tzinfo``.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def build_trace_page(
    audit: AuditGateway,
    *,
    profile: str | None = None,
    surface: Surface | None = None,
    correlation_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    kinds: list[TraceKind] | None = None,
) -> TracePage:
    """The section 9 trace view: "if you can't see what it did, you can't trust
    it or debug it."

    Audit records and transcript messages are merged into one descending
    timeline. The two families are queried separately and merged here rather
    than in SQL because they live in different tables with different columns,
    and a UNION would force both into a lowest-common-denominator row that
    loses exactly the fields (tool arguments, message content) the view exists
    to show.
    """
    wanted = kinds or [TraceKind.AUDIT, TraceKind.TRANSCRIPT]
    owner = Owner.profile(profile) if profile else None
    since = _normalise_moment(since)
    until = _normalise_moment(until)

    # One row beyond the page so `has_more` is measured, not guessed.
    window = min(offset + limit + 1, MAX_TRACE_WINDOW)
    query = {
        "owner": owner,
        "surface": surface,
        "correlation_id": correlation_id,
        "since": since,
        "until": until,
        "limit": window,
    }

    entries: list[TraceEntry] = []
    if TraceKind.AUDIT in wanted:
        for record in await audit.query_audit(**query):
            entries.append(_audit_entry(record))
    if TraceKind.TRANSCRIPT in wanted:
        for record in await audit.query_transcript(**query):
            entries.append(_transcript_entry(record))

    entries.sort(key=lambda e: (e.timestamp, e.record_id), reverse=True)
    page = entries[offset : offset + limit]
    return TracePage(
        entries=page,
        limit=limit,
        offset=offset,
        returned=len(page),
        has_more=len(entries) > offset + limit,
        filters=TraceFilters(
            profile=profile,
            surface=surface,
            correlation_id=correlation_id,
            since=since,
            until=until,
            kinds=wanted,
        ),
    )


def _audit_entry(record: AuditRecord) -> TraceEntry:
    return TraceEntry(
        kind=TraceKind.AUDIT,
        record_id=str(record.record_id),
        correlation_id=record.correlation_id,
        timestamp=record.timestamp,
        surface=record.surface,
        owner_kind=record.owner.kind.value,
        owner_id=record.owner.id,
        category=record.category.value,
        action=record.action,
        risk_level=record.risk_level.value if record.risk_level else None,
        outcome=record.outcome.value,
        detail=record.detail,
    )


def _transcript_entry(record: TranscriptRecord) -> TraceEntry:
    return TraceEntry(
        kind=TraceKind.TRANSCRIPT,
        record_id=str(record.record_id),
        correlation_id=record.correlation_id,
        timestamp=record.timestamp,
        surface=record.surface,
        owner_kind=record.owner.kind.value,
        owner_id=record.owner.id,
        role=record.role.value,
        content=record.content,
    )


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register ``GET /admin/api/trace`` on the guarded router."""
    api = router
    audit = ctx.audit

    # -- trace -------------------------------------------------------------

    @api.get("/trace", response_model=TracePage, summary="Trace view")
    async def trace(
        profile: Annotated[str | None, Query(max_length=256)] = None,
        surface: Annotated[Surface | None, Query()] = None,
        correlation_id: Annotated[str | None, Query(max_length=256)] = None,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        kind: Annotated[list[TraceKind] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_TRACE_LIMIT)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> TracePage:
        """Spec section 9: "live and historical log of what the agent did —
        every tool call with arguments and outcome, every event received, every
        confirmation. If you can't see what it did, you can't trust it or debug
        it."

        ``profile`` filters by owner (spec section 8: every record is
        attributed), ``correlation_id`` follows one turn end to end.
        """
        return await build_trace_page(
            audit,
            profile=profile,
            surface=surface,
            correlation_id=correlation_id,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            kinds=kind,
        )


__all__ = [
    "MAX_TRACE_LIMIT",
    "MAX_TRACE_WINDOW",
    "build_trace_page",
    "register",
]
