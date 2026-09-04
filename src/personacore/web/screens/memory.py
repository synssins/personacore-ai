"""The Memory screen (contract `working/contracts/memory.md` §8, plan
`working/PLAN-memory.md` Task 6, joint J4's `MemoryStore`).

Administrator only, admin-only by construction: this module never checks who
the caller is. It is registered on the same router every other admin screen
is, and that router refuses a household member on everything outside a
written allowlist (`web/routes.py`'s `MEMBER_PATHS` / `MEMBER_PREFIXES`,
ADR-0032) — this screen is deliberately absent from both, so the default-deny
already answers "who may see this" before any handler here runs.

**Grouping.** Long term (holder `global`, owner `household`) is its own
group, always first and never affected by the person or persona filter — it
belongs to every persona and every person at once, so narrowing to one of
either would misstate what it is. Everything else groups by owner (a
person's account id, or the anonymous owner), in the order the account store
lists people plus "Anonymous callers" last, then by holder (persona name)
within, in `PersonaStore.available()`'s order. An owner or holder with rows
but no matching account/persona (a deleted account, a removed persona) still
gets a group — sorted alphabetically after the known ones — because a memory
that exists must still be reachable to delete.

**Counts are of what is actually shown**, not `MemoryStore.counts()`'s
whole-store totals: the summary line and every group heading count the rows
in the current filtered/searched result, because a header claiming a count
the search has just narrowed past would be the more confusing number to
show, not the more correct one.

**The store may not exist.** `request.app.state.memory_store` is `None`
when the bundled embedding model was not found at boot (Task 4's wiring) —
this screen degrades to a plain `.empty` block rather than raising, the same
tolerance `review.py` gives an audit store that cannot be asked.

**Text is never audited or logged.** Every write here (`memory.promote`,
`memory.delete`, `memory.edit`) records the memory id only, through the same
`_record_change` helper every other admin screen's changes go through
(`personacore.admin.routes._record_change`) — see contract §10.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.audit.models import AuditOutcome
from personacore.memory.models import (
    ANONYMOUS_OWNER,
    GLOBAL_HOLDER,
    HOUSEHOLD_OWNER,
    MAX_TEXT_CHARS,
    MemoryRecord,
)
from personacore.web.shared import UIContext

MEMORY_PATH = "/admin/memory"
"""Where the screen lives, for the sidebar link and every redirect back to it."""

MEMORY_ROUTE = "/memory"
"""The route as declared, relative to the router's own ``/admin`` prefix."""

MEMORIES_SHOWN = 1000
"""A ceiling on each store query, the same shape as `review.py`'s
`CONVERSATIONS_SHOWN`: a bound on the read rather than a page size, because
this list has no unbounded growth path a person cannot already prune from
this very screen."""

ANONYMOUS_LABEL = "Anonymous callers"

MEMORY_UNAVAILABLE = (
    "Memory is not available on this core — the bundled embedding model was "
    "not found at boot, so nothing has been kept and there is nothing to "
    "prune here."
)

NOTHING_TO_SHOW = "Nothing matches these filters."

HELP_LINE = (
    "What each character has kept about each person, and the household's "
    "long-term memory. A short-term memory is removed after 60 days without "
    "use unless you promote it. Delete is immediate."
)

TEXT_REQUIRED = "A memory cannot be saved empty."

TEXT_TOO_LONG = (
    f"That memory is longer than {MAX_TEXT_CHARS} characters. Shorten it and "
    "save again."
)

MEMORY_NOT_FOUND = "There is no memory with that id."

PER_PERSON_NOT_YET = (
    "Nobody can see their own memory here yet — that view is not switched on "
    "for this core."
)
"""What `GET /admin/profile/memory` answers, to everybody, in v1 (contract
§8: "not linked from any screen and its route returns 404 to everyone until
a settings flag ... exists and is on. The flag does not exist in v1.")."""


# ---------------------------------------------------------------------------
# Pure helpers — read the rows, never touch HTTP
# ---------------------------------------------------------------------------


def _when(moment: datetime | None) -> str:
    """One timestamp, in the screen's own format. `""` for missing."""
    return "" if moment is None else moment.strftime("%Y-%m-%d %H:%M")


def filters_from_query(person: Sequence[str], persona: str, q: str) -> dict[str, Any]:
    """The three filter values, normalised once, for both the query string
    builder and the store queries below."""
    return {
        "person": [p for p in person if p],
        "persona": persona.strip(),
        "q": q.strip(),
    }


def filters_query_string(filters: Mapping[str, Any]) -> str:
    """The filters as a query string, for a redirect or a link back with
    them intact — the design's own requirement that the selection "is in the
    query string, so it survives reload and can be linked" (contract §8).
    """
    pairs: list[tuple[str, str]] = [("person", p) for p in filters["person"]]
    if filters["persona"]:
        pairs.append(("persona", filters["persona"]))
    if filters["q"]:
        pairs.append(("q", filters["q"]))
    return urlencode(pairs)


def review_url(record: MemoryRecord) -> str | None:
    """Where this row's conversation can be opened, or `None`.

    Only ever the two-argument review page (`user`, `conversation`), and only
    when the row still names a real person: a promoted row's `owner` has
    already become `household` (contract §7's promote is the only way in),
    which review has no account for, and the anonymous owner has no account
    at all. Contract §8: "the link is `/admin/review?...` if review can open
    a conversation by id, else omit the link and say so" — the "say so" half
    is the template's, not this function's.
    """
    if record.conversation_id is None:
        return None
    if record.owner in (HOUSEHOLD_OWNER, ANONYMOUS_OWNER):
        return None
    return "/admin/review?" + urlencode(
        {"user": record.owner, "conversation": record.conversation_id}
    )


def memory_row(record: MemoryRecord) -> dict[str, Any]:
    """One memory, in the shape the template renders (contract §8's table).

    ``last_used`` is `None` rather than a timestamp identical to `created`
    when `use_count` is 0: the store sets `last_used_at = created_at` at
    insert (there is nothing else to set it to yet), and showing that back as
    "last used" would claim an activity that never happened. The mockup's own
    row for an unused memory says "never used", not a repeated timestamp.
    """
    edited = None
    if record.edited_by and record.edited_at is not None:
        edited = f"{record.edited_by} · {_when(record.edited_at)}"
    promoted = None
    if record.promoted_by and record.promoted_at is not None:
        promoted = f"{_when(record.promoted_at)} by {record.promoted_by}"
    return {
        "id": record.memory_id,
        "text": record.text,
        "created": _when(record.created_at),
        "used": record.use_count,
        "last_used": None if record.use_count == 0 else _when(record.last_used_at),
        "source": record.written_by,
        "model": record.written_model,
        "edited": edited,
        "truncated": record.truncated,
        "promoted": promoted,
        "review_url": review_url(record),
    }


def _person_label(owner: str, accounts: Mapping[str, str]) -> str:
    """What a group is titled. Contract §8: "the person label comes from the
    accounts store's display name, falling back to the id" — the account
    store's own listing (`UserView`) has no field beyond the account's own
    name, so that name *is* the display name here; the fallback is for an
    owner id this core has no account for at all (a deleted account, or a
    profile id an access key wrote under), where the raw id is what is left
    to show.
    """
    if owner == ANONYMOUS_OWNER:
        return ANONYMOUS_LABEL
    return accounts.get(owner, owner)


def build_groups(
    records: Sequence[MemoryRecord],
    *,
    owner_order: Sequence[str],
    holder_order: Sequence[str],
    accounts: Mapping[str, str],
    minors: Mapping[str, bool],
) -> list[dict[str, Any]]:
    """Person groups, each holding its persona groups, from a flat list of
    non-long-term rows. Ordering is `owner_order` (accounts, then
    "anonymous") then `holder_order` (`PersonaStore.available()`), with
    anything neither names sorted alphabetically after — see the module
    docstring.
    """
    by_owner: dict[str, list[MemoryRecord]] = {}
    for record in records:
        by_owner.setdefault(record.owner, []).append(record)

    ordered_owners = [owner for owner in owner_order if owner in by_owner]
    ordered_owners += sorted(owner for owner in by_owner if owner not in owner_order)

    groups: list[dict[str, Any]] = []
    for owner in ordered_owners:
        owner_rows = by_owner[owner]
        by_holder: dict[str, list[MemoryRecord]] = {}
        for record in owner_rows:
            by_holder.setdefault(record.holder, []).append(record)
        ordered_holders = [holder for holder in holder_order if holder in by_holder]
        ordered_holders += sorted(
            holder for holder in by_holder if holder not in holder_order
        )
        personas = [
            {
                "name": holder,
                "count": len(by_holder[holder]),
                "rows": [memory_row(record) for record in by_holder[holder]],
            }
            for holder in ordered_holders
        ]
        groups.append(
            {
                "owner": owner,
                "label": _person_label(owner, accounts),
                "is_minor": bool(minors.get(owner, False)),
                "count": len(owner_rows),
                "personas": personas,
            }
        )
    return groups


def _accounts(ctx: UIContext) -> tuple[dict[str, str], dict[str, bool], list[str]]:
    """Every known account, as (label-by-id, minor-by-id, ids-in-order).

    Empty everywhere when this core has no account store at all
    (`ctx.auth_context is None`, the same three-state read `review.py`'s
    `_people` makes) — an owner id then simply falls back to its raw id, and
    the people filter offers "Anonymous callers" alone.
    """
    if ctx.auth_context is None:
        return {}, {}, []
    people = ctx.auth_context.user_views()
    labels = {person.username: person.username for person in people}
    minors = {person.username: person.is_minor for person in people}
    order = [person.username for person in people]
    return labels, minors, order


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the Memory screen: the page, the filtered list fragment, the
    one-tap promote and delete, the edit form, and the (always-404) stub for
    a member's own view.
    """
    # Imported inside `register`, like every other screen's admin-change
    # write: `web/routes.py` builds this router, so a top-level import back
    # into it would be a cycle.
    from personacore.admin.routes import _record_change

    templates = ctx.templates
    require_user = ctx.require_user
    shell = ctx.shell

    def _store(request: Request) -> Any | None:
        """`request.app.state.memory_store`, or `None`.

        `getattr` rather than a bare attribute read: Task 4 wires this
        attribute onto `app.state` in the same wave this screen was built in,
        and a core assembled without it must render the unavailable block
        rather than raise `AttributeError` — the same reason `review.py`
        reaches every collaborator through `getattr`.
        """
        return getattr(request.app.state, "memory_store", None)

    def _wants_fragment(request: Request) -> bool:
        """Whether this request came from htmx (a filter change, or a
        promote/delete tap with scripts on) rather than a plain form post.

        htmx sets this header on every request it issues; a browser
        submitting a plain form never does. It is the one place this screen
        decides which of the two answers a POST gets — see the module
        docstring's note on click-first.
        """
        return request.headers.get("hx-request", "").lower() == "true"

    async def _list_context(
        request: Request, *, person: Sequence[str], persona: str, q: str
    ) -> dict[str, Any]:
        filters = filters_from_query(person, persona, q)
        qs = filters_query_string(filters)
        store = _store(request)
        accounts, minors, people_order = _accounts(ctx)
        people_choices = [
            {
                "id": account_id,
                "label": accounts.get(account_id, account_id),
                "checked": account_id in filters["person"],
            }
            for account_id in people_order
        ] + [
            {
                "id": ANONYMOUS_OWNER,
                "label": ANONYMOUS_LABEL,
                "checked": ANONYMOUS_OWNER in filters["person"],
            }
        ]
        persona_choices = ctx.personas.available()

        base = {
            "filters": filters,
            "filters_qs": qs,
            "people_choices": people_choices,
            "people_selected_count": len(filters["person"]),
            "persona_choices": persona_choices,
        }

        if store is None:
            return {
                **base,
                "unavailable": True,
                "unavailable_message": MEMORY_UNAVAILABLE,
                "long_term": [],
                "groups": [],
                "total_people": 0,
                "total_personas": 0,
                "total_shown": 0,
                "nothing_to_show": NOTHING_TO_SHOW,
            }

        search = filters["q"] or None
        long_term_records = await store.list(
            owners=[HOUSEHOLD_OWNER],
            holders=[GLOBAL_HOLDER],
            search=search,
            limit=MEMORIES_SHOWN,
        )
        owners_filter = filters["person"] or None
        holders_filter = [filters["persona"]] if filters["persona"] else None
        other_records = await store.list(
            owners=owners_filter,
            holders=holders_filter,
            search=search,
            limit=MEMORIES_SHOWN,
        )
        # `holder != GLOBAL_HOLDER` drops the household's own rows out of
        # this half: with no person filter, `owners_filter` is `None` and
        # the query above would otherwise return the long-term rows a
        # second time, once here and once in `long_term_records`.
        person_records = [r for r in other_records if r.holder != GLOBAL_HOLDER]

        long_term_rows = [memory_row(record) for record in long_term_records]
        groups = build_groups(
            person_records,
            owner_order=[*people_order, ANONYMOUS_OWNER],
            holder_order=persona_choices,
            accounts=accounts,
            minors=minors,
        )
        total_personas = sum(len(group["personas"]) for group in groups)
        total_shown = len(long_term_rows) + sum(group["count"] for group in groups)

        return {
            **base,
            "unavailable": False,
            "unavailable_message": None,
            "long_term": long_term_rows,
            "groups": groups,
            "total_people": len(groups),
            "total_personas": total_personas,
            "total_shown": total_shown,
            "nothing_to_show": NOTHING_TO_SHOW,
        }

    def _list_fragment(request: Request, context: Mapping[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="fragments/memory_list.html", context=dict(context)
        )

    @router.get(MEMORY_ROUTE, response_class=HTMLResponse, summary="The Memory screen")
    async def memory_page(request: Request) -> HTMLResponse:
        person, persona, q = _current_filters(request)
        context = await _list_context(request, person=person, persona=persona, q=q)
        return templates.TemplateResponse(
            request=request,
            name="memory.html",
            context={
                **await shell(request, "memory"),
                **context,
                "help_line": HELP_LINE,
            },
        )

    @router.get(
        "/memory/list", response_class=HTMLResponse, summary="The filtered memory list"
    )
    async def memory_list(request: Request) -> HTMLResponse:
        person, persona, q = _current_filters(request)
        context = await _list_context(request, person=person, persona=persona, q=q)
        return _list_fragment(request, context)

    async def _record_or_404(store: Any, memory_id: str) -> MemoryRecord:
        record = await store.get(memory_id) if store is not None else None
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, MEMORY_NOT_FOUND)
        return record

    def _current_filters(request: Request) -> tuple[list[str], str, str]:
        return (
            request.query_params.getlist("person"),
            request.query_params.get("persona") or "",
            request.query_params.get("q") or "",
        )

    async def _after_change(request: Request) -> Response:
        """What a promote or delete answers with: the refreshed list for
        htmx, a plain redirect back to the page for everyone else (contract
        §8: no confirmation page, and the screen still has to work with
        scripts off, ADR-0020)."""
        person, persona, q = _current_filters(request)
        if _wants_fragment(request):
            context = await _list_context(request, person=person, persona=persona, q=q)
            return _list_fragment(request, context)
        qs = filters_query_string(filters_from_query(person, persona, q))
        target = f"{MEMORY_PATH}?{qs}" if qs else MEMORY_PATH
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)

    @router.post(
        "/memory/{memory_id}/promote",
        response_class=HTMLResponse,
        summary="Promote one memory to long term",
    )
    async def memory_promote(request: Request, memory_id: str) -> Response:
        user = require_user(request)
        store = _store(request)
        await _record_or_404(store, memory_id)
        ok = await store.promote(memory_id, by=user.id)
        if ok:
            await _record_change(
                ctx.audit,
                user,
                action="memory.promote",
                outcome=AuditOutcome.SUCCESS,
                detail={"memory_id": memory_id},
            )
        return await _after_change(request)

    @router.post(
        "/memory/{memory_id}/delete",
        response_class=HTMLResponse,
        summary="Delete one memory",
    )
    async def memory_delete(request: Request, memory_id: str) -> Response:
        user = require_user(request)
        store = _store(request)
        await _record_or_404(store, memory_id)
        ok = await store.delete(memory_id)
        if ok:
            await _record_change(
                ctx.audit,
                user,
                action="memory.delete",
                outcome=AuditOutcome.SUCCESS,
                detail={"memory_id": memory_id},
            )
        return await _after_change(request)

    async def _edit_context(
        request: Request, record: MemoryRecord, *, error: str | None = None, text: str | None = None
    ) -> dict[str, Any]:
        person, persona, q = _current_filters(request)
        return {
            **await shell(request, "memory"),
            "memory": memory_row(record),
            "text_value": record.text if text is None else text,
            "error": error,
            "filters_qs": filters_query_string(filters_from_query(person, persona, q)),
            "max_chars": MAX_TEXT_CHARS,
        }

    @router.get(
        "/memory/{memory_id}/edit",
        response_class=HTMLResponse,
        summary="Edit one memory's text",
    )
    async def memory_edit_form(request: Request, memory_id: str) -> HTMLResponse:
        store = _store(request)
        record = await _record_or_404(store, memory_id)
        return templates.TemplateResponse(
            request=request, name="memory_edit.html", context=await _edit_context(request, record)
        )

    @router.post(
        "/memory/{memory_id}/edit",
        response_class=HTMLResponse,
        summary="Save an edited memory",
    )
    async def memory_edit_save(request: Request, memory_id: str) -> Response:
        user = require_user(request)
        store = _store(request)
        record = await _record_or_404(store, memory_id)
        form = await request.form()
        try:
            text = str(form.get("text") or "").strip()
        finally:
            await form.close()

        if not text:
            return templates.TemplateResponse(
                request=request,
                name="memory_edit.html",
                context=await _edit_context(request, record, error=TEXT_REQUIRED, text=text),
            )
        if len(text) > MAX_TEXT_CHARS:
            return templates.TemplateResponse(
                request=request,
                name="memory_edit.html",
                context=await _edit_context(request, record, error=TEXT_TOO_LONG, text=text),
            )

        await store.edit(memory_id, text=text, by=user.id)
        await _record_change(
            ctx.audit,
            user,
            action="memory.edit",
            outcome=AuditOutcome.SUCCESS,
            detail={"memory_id": memory_id},
        )
        person, persona, q = _current_filters(request)
        qs = filters_query_string(filters_from_query(person, persona, q))
        target = f"{MEMORY_PATH}?{qs}" if qs else MEMORY_PATH
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)

    # -- the per-person view's framework — not built, still refused (§8) ----

    @router.get(
        "/profile/memory",
        response_class=HTMLResponse,
        summary="A member's own memory (not switched on in v1)",
    )
    async def profile_memory(request: Request) -> HTMLResponse:
        """Contract §8: this route exists and returns 404 to everyone until
        `[memory] members_see_own` exists and is on, which it does not in
        v1. Registered under `/admin/profile/`, which `MEMBER_PREFIXES`
        already lets a member reach — reaching it and being refused by the
        handler is the whole of what "framework, no access" means here.
        """
        raise HTTPException(status.HTTP_404_NOT_FOUND, PER_PERSON_NOT_YET)


__all__ = [
    "ANONYMOUS_LABEL",
    "HELP_LINE",
    "MEMORIES_SHOWN",
    "MEMORY_NOT_FOUND",
    "MEMORY_PATH",
    "MEMORY_ROUTE",
    "MEMORY_UNAVAILABLE",
    "NOTHING_TO_SHOW",
    "PER_PERSON_NOT_YET",
    "TEXT_REQUIRED",
    "TEXT_TOO_LONG",
    "build_groups",
    "filters_from_query",
    "filters_query_string",
    "memory_row",
    "register",
    "review_url",
]
