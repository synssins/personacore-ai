"""The ``workstation`` plugin's own settings screen — reshape plan R4.

Decision 0.1 (``working/team/enrolment-v2/PLAN.md``): every enrolled machine is
a row inside the ``workstation`` plugin's own settings page. Machines never
appear on the generic Plugins list, and the plugin itself is not one until the
first machine joins (``EnrolmentService._ensure_plugin``). So this screen is
registered on a **literal** path — ``/plugins/workstation`` — ahead of the
generic ``/plugins/{name}`` in ``web/routes.py``, the same trick
``plugin_install`` already uses against the same competing pattern: a literal
route registered first wins the match, and every other plugin's name still
falls through to the generic page untouched.

The approved design is ``working/team/enrolment-v2/r4-screen/canvas/`` — four
artboards. **Open them before changing this file.** Visual drift from them is
a defect (CLAUDE.md).

Two things the canvas draws that have no backend behind them yet, both
answered here rather than left to drift into a dead control:

* **"Ask before anything risky."** This core has no confirmation channel at
  all (``enrolment/workstation.py``'s own module docstring: "this core has no
  way to ask a person to confirm anything yet" — issue #6), and there is no
  path from a plugin-wide setting to the Agent's own screen either. The switch
  is drawn exactly as designed and rendered **disabled, marked "later"** — the
  same treatment every other control on this surface gets when its backend
  does not exist (``NO_PLUGIN_OPERATIONS``, the Runbooks switch while the core
  switch is off). Shipping it live would be shipping a switch that does
  nothing while claiming otherwise, which is the specific failure this
  screen's brief warns against.
* **"Logs" and "permissions."** Both are drawn on every row. The log *read*
  path is R6, blocked on the Agent shipping a tool to read it
  (``PLAN.md`` §2.4) — not started. "Permissions" is not named as a
  deliverable anywhere in R1 through R6; nothing describes what it would even
  read or write. Both render, in the design's own position and wording, as
  disabled controls marked "later" — not removed (that would be silent drift
  from the canvas) and not linked to a page that does not exist.

Two facts the canvas needs that the registry alone cannot answer, both read
from ``request.app.state.plugin_host`` the same duck-typed way
``plugin_detail.py``'s own ``_runbooks_store`` reads ``app.state.runbooks`` —
optional, so a core assembled without a plugin host still renders this page
with everything reported honestly as unknown rather than guessed:

* **Online, per machine.** The registry's own ``connection_state`` seam
  (``MachineRegistry(connection_state=...)``) is wired here from the live
  host's ``PluginHealth.endpoints`` — one row per machine, ADR-0048 — rather
  than polled: a machine is online when *any* of its addresses answers, and
  unknown (never offline) when this core has no host to ask at all.
* **Tool count, per machine.** The union of ``EndpointHealth.tools`` across a
  machine's own addresses. Whether that count survives a disconnect the way
  the canvas's offline row implies (``KIDS-PC … 9 tools``) depends on
  ``PluginSupervisor`` internals this module does not reach into and has not
  verified line by line; it is what the health snapshot reports, honestly, and
  nothing here invents a number the host does not have.

**Removing a machine goes through the public hook, not the registry
directly.** :meth:`personacore.enrolment.workstation.EnrolmentService.remove_machine`
is the mirror of :meth:`~personacore.enrolment.workstation.EnrolmentService.enrol`:
one operation that takes the row and the token out **and** rewrites
``manifest.toml``, which a bare
:meth:`~personacore.enrolment.registry.MachineRegistry.remove` never touched.
Built here the same way :func:`personacore.admin.api_enrol.build_service`
builds one for the enrolment route, with one substitution:
``set_enabled`` is read off ``request.app.state.plugin_host`` (the same
duck-typed, optional read this module uses for the endpoint index below),
because :class:`~personacore.web.shared.UIContext` carries no ``live_toggle``
of its own and the host's own ``set_enabled`` is exactly what the JSON API's
``live_toggle`` already resolves to.

**The last machine — the owner's own ruling, 2026-09-09: parked, not
uninstalled.** ``remove_machine`` switches the plugin off and leaves its
folder, its settings and its row on the Plugins screen in place; a machine
joining switches it back on by itself. ``MachineRemoved.switched_off`` is the
cue, and this screen's notice says so in those words rather than leaving the
owner looking at a "switched off" row wondering whether he broke something —
the one thing CLAUDE.md's team model asks of a control like this: the
interface states what happened, including a happened-to-it the owner did not
click.

**The two refusal cards render now.** ``PairingState`` gained ``SETTLING``
and ``REFUSED``, and ``GET /admin/api/pairings/current`` carries a
``refusal: {reason, machine}`` under ``REFUSED`` — set by
:meth:`~personacore.enrolment.pairing.PairingStore.mark_refused`, called from
inside :meth:`EnrolmentService.enrol`'s own refusal path, so a code redeemed
and then turned away (a name collision, an unreachable push) is finally
distinct from the harmless instant between redemption and a name landing
(``SETTLING``) and from a real join (``CLAIMED``). ``reason`` is a short code
— never the sentence the Agent got, which can name an already-enrolled
machine and its address and is deliberately not this screen's to carry. The
two cards this screen writes prose for are ``name_taken`` and
``machine_unreachable``, with ``refused`` as the generic fallback every other
reason (and any this screen has not been taught) collapses to.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.audit import AuditOutcome
from personacore.config.secrets import SecretStore
from personacore.enrolment.pairing import DEFAULT_TTL_SECONDS, PairingState
from personacore.enrolment.registry import (
    PLUGIN_NAME,
    Machine,
    MachineRegistry,
    MachineRejected,
    MachineView,
    enrolment_audit_detail,
)
from personacore.enrolment.workstation import EnrolmentRefused, EnrolmentService
from personacore.plugins.health import EndpointHealth
from personacore.web.plugin_page import health_context
from personacore.web.shared import (
    NO_PAIRING_OPERATIONS,
    UIContext,
    api_handler,
    refusal,
)

POLL_SECONDS = 2
"""How often the pairing dialog polls while a code is waiting. A code lives
five minutes (``pairing.DEFAULT_TTL_SECONDS``); every two seconds is smooth
enough for the draining bar to read as continuous without hammering the core
the way ``run_status.html``'s own 4-second poll for a much longer-lived job
would understate here."""

LOGS_LATER_NOTE = "Reading a machine's own log needs a tool the Agent does not ship yet."

PERMISSIONS_LATER_NOTE = "Per-machine permissions are not built yet."

MACHINE_REMOVE_TITLE = "Remove {name}?"

MACHINE_REMOVE_LABEL = "Remove {name}"

NAME_TAKEN_TITLE = "That name is taken."

MACHINE_UNREACHABLE_TITLE = "Could not reach {name}."

GENERIC_REFUSAL_TITLE = "That workstation could not join."

LAST_MACHINE_REMOVE_SUFFIX = (
    " {name} is the last one, so removing it switches the workstation plugin "
    "off — not uninstalled, and it switches back on by itself the moment a "
    "workstation joins."
)

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


# ---------------------------------------------------------------------------
# Small, pure helpers — kept free of Request so they can be unit tested
# ---------------------------------------------------------------------------


def display_host(url: str) -> str:
    """A stored address's bare host, for the row — never the scheme, port or
    path. ``https://[2001:db8::14]:9443/mcp`` becomes ``[2001:db8::14]``;
    ``https://203.0.113.14:9443/mcp`` becomes ``203.0.113.14`` — matching the
    canvas's own display, which never shows the port or the ``/mcp`` path.
    """
    host = urlsplit(url).hostname or url
    return f"[{host}]" if ":" in host else host


def absolute_moment(moment: datetime) -> str:
    """``9 Sep, 13:04`` — the canvas's own date format. No locale-dependent
    ``strftime`` directive: ``%-d``/``%#d`` (no leading zero) are POSIX- and
    Windows-only respectively, and this core's containers are Linux."""
    return f"{moment.day} {_MONTHS[moment.month - 1]}, {moment:%H:%M}"


def relative_moment(moment: datetime, now: datetime) -> str:
    """"2 minutes ago" / "4 hours ago" / "yesterday, 21:40" / an absolute date.

    Everything in this core is stored and rendered in UTC — there is no
    per-user timezone anywhere else in this admin surface either, so "today"
    and "yesterday" are UTC's own calendar days.
    """
    seconds = max((now - moment).total_seconds(), 0.0)
    if seconds < 90:
        return "moments ago"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    # "Yesterday" is a calendar day, not a rolling 24 hours — checked before
    # the hour count so a machine last seen at 21:40 and looked at again at
    # 09:00 the next morning reads as "yesterday, 21:40" rather than "11
    # hours ago", which is what the canvas itself draws (KIDS-PC's row).
    if moment.date() != now.date():
        days = (now.date() - moment.date()).days
        if days == 1:
            return f"yesterday, {moment:%H:%M}"
        return absolute_moment(moment)
    hours = int(minutes // 60)
    return f"{hours} hour{'' if hours == 1 else 's'} ago"


def endpoint_index(request: Request) -> Mapping[str, EndpointHealth]:
    """``{address -> its machine's EndpointHealth}`` for the ``workstation``
    plugin, or ``{}`` when this core has no plugin host, the host has no
    opinion, or the plugin has not loaded (never installed, or installed but
    not yet started).

    One row per machine, and a machine with three addresses appears under all
    three keys — ``EndpointHealth.every_address`` is the machine's own list, so
    a row is found by whichever address is looked up.

    Read off ``request.app.state.plugin_host`` with ``getattr`` — the same
    duck-typed, optional read ``plugin_detail.py``'s ``_runbooks_store`` uses
    for ``app.state.runbooks`` — rather than through
    :class:`~personacore.admin.protocols.PluginHealthSource`, whose adapter
    (``boot/plugins.py``) does not carry ``PluginHealth.endpoints`` at all. An
    empty mapping here means every machine's online word and tool count come
    back as "unknown" and ``0`` respectively — never guessed as offline, never
    invented as a number nobody reported.
    """
    host = getattr(request.app.state, "plugin_host", None)
    if host is None:
        return {}
    try:
        rows = host.health()
    except Exception:  # noqa: BLE001 - a settings page must not 500 on this
        return {}
    for row in rows:
        if row.name == PLUGIN_NAME:
            return {
                address: endpoint
                for endpoint in row.endpoints
                for address in endpoint.every_address
            }
    return {}


def make_connection_state(index: Mapping[str, EndpointHealth]):
    """The registry's ``connection_state`` seam, closed over one request's
    endpoint index.

    ``None`` when none of a machine's own addresses appear in the index at
    all — no host, or the plugin has never started — never guessed as
    offline. Otherwise online when *any* of its addresses currently answers.

    A machine's several addresses (an IPv4 and an IPv6 on one interface) are
    one connection tried in declared order (``EndpointSetSupervisor``'s own
    doc), so they resolve to one row and ``any`` is reading one answer rather
    than combining several. It stays written as ``any`` because the registry's
    address list and the manifest's are written at different moments, and a
    machine part-way through a rewrite must not read as offline.
    """

    def _connection_state(machine: Machine) -> bool | None:
        known = [index[address.url] for address in machine.addresses if address.url in index]
        if not known:
            return None
        return any(endpoint.is_callable for endpoint in known)

    return _connection_state


def tool_count(machine: Machine, index: Mapping[str, EndpointHealth]) -> int:
    """The union of tool names across one machine's own addresses. ``0`` when
    none of them are known to the host — not a guess, the honest count of what
    is known right now."""
    names: set[str] = set()
    for address in machine.addresses:
        endpoint = index.get(address.url)
        if endpoint is not None:
            names.update(endpoint.tools)
    return len(names)


def machine_row(
    view: MachineView, index: Mapping[str, EndpointHealth], *, now: datetime
) -> dict[str, Any]:
    """One row of the machine list, or the Joined dialog's own machine card —
    both draw the same facts, so both are built from this one function."""
    machine = view.machine
    online = view.online
    if online is True:
        tone, word = "ok", "online"
    elif online is False:
        tone, word = "off", "offline"
    else:
        tone, word = "warn", "unknown"

    addresses = [display_host(address.url) for address in machine.addresses]
    count = len(addresses)
    listening = f"{count} address{'es' if count != 1 else ''} listening"

    joined = absolute_moment(machine.enrolled_at)
    if online:
        activity = (
            f"last acted {relative_moment(machine.last_acted_at, now)}"
            if machine.last_acted_at is not None
            else "no activity yet"
        )
    elif machine.last_acted_at is not None:
        # Decision, flagged in the report: the registry has no record of when
        # a connection last dropped, only of when the machine last did
        # something (``record_last_acted``). Rendered as "last seen" against
        # that same timestamp rather than inventing a disconnect time this
        # core has never recorded — which means it can read a little stale
        # right after a machine goes quiet without having acted first, and
        # never means anything false: it is always the true last time it did
        # something, just not necessarily the true last time it answered.
        activity = f"last seen {relative_moment(machine.last_acted_at, now)}"
    else:
        activity = None

    return {
        "id": machine.id,
        "name": machine.name,
        "online": online,
        "tone": tone,
        "word": word,
        "tool_count": tool_count(machine, index),
        "addresses": addresses,
        "listening": listening,
        "joined": joined,
        "activity": activity,
    }


def find_view(views: Sequence[MachineView], name: str) -> MachineView | None:
    """One view by exact, case-insensitive name — the same comparison
    :meth:`MachineRegistry.find` uses, applied to a listing already read."""
    wanted = " ".join(name.split()).casefold()
    return next(
        (view for view in views if " ".join(view.machine.name.split()).casefold() == wanted),
        None,
    )


def refusal_card(reason: str, machine: str | None) -> dict[str, Any]:
    """One refusal card's title and body, built from the short code alone.

    **Never the sentence the Agent got.** That sentence is free text written
    for the person standing at the workstation, it already crossed the wire
    to the Agent, and it can name an already-enrolled machine and its
    address — precisely the two facts ``PairingRefusal`` deliberately does
    not carry to this side (module docstring). So this writes prose from
    ``reason`` and ``machine`` — the joining machine's own name, which for a
    collision is the same string the row already in the list is using —
    never from anything this screen would have to go and read the sentence
    to know, such as an address.

    ``name_taken`` and ``machine_unreachable`` are the two the canvas draws a
    card for; every other code, including the generic ``refused``, gets the
    fallback the canvas has no card for a specific reason. That default is
    load-bearing: a refusal this screen cannot name still has to close the
    dialog with something true.
    """
    if reason == "name_taken" and machine:
        return {
            "reason": reason,
            "title": NAME_TAKEN_TITLE,
            "body": (
                f"A machine called {machine!r} is already on this core, so this "
                "one could not join under that name. Remove the one that is "
                "there, or rename this machine and try again."
            ),
            "machine": machine,
        }
    if reason == "machine_unreachable":
        return {
            "reason": reason,
            "title": (
                MACHINE_UNREACHABLE_TITLE.format(name=machine)
                if machine
                else "Could not reach it."
            ),
            "body": (
                f"The core could not hand {machine or 'the workstation'} its "
                "credential — the connection failed. Nothing was installed. "
                "Check it is running and reachable, then get a fresh code and "
                "press Join again."
            ),
            "machine": machine,
        }
    return {
        "reason": "refused",
        "title": GENERIC_REFUSAL_TITLE,
        "body": (
            "It was turned away, and this screen has no card written for that "
            "particular reason yet. Get a fresh code and try again."
        ),
        "machine": machine,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the workstation screen. Must run **before**
    ``plugin_detail_screen.register`` on the same router — see the module
    docstring for why the literal path has to win the match.
    """
    templates = ctx.templates
    layout = ctx.layout
    scans = ctx.scans
    require_user = ctx.require_user

    def _registry(request: Request) -> tuple[MachineRegistry, Mapping[str, EndpointHealth]]:
        index = endpoint_index(request)
        registry = MachineRegistry(
            layout=layout,
            secrets=SecretStore(layout),
            connection_state=make_connection_state(index),
        )
        return registry, index

    def _enrolment_service(request: Request) -> EnrolmentService:
        """The public counterpart of ``build_service`` (``admin/api_enrol.py``),
        for the one call this screen makes into it: :meth:`EnrolmentService.
        remove_machine`.

        ``set_enabled`` is read off ``request.app.state.plugin_host`` — the
        same object :func:`endpoint_index` already reads, duck-typed, for the
        reason that function gives — because that host's own ``set_enabled``
        is exactly what ``admin/routes.py`` resolves ``live_toggle`` to
        (``getattr(plugin_toggle, "set_enabled", None)``), and
        :class:`UIContext` is not handed that object directly. ``redeem`` is
        never called from here — ``remove_machine`` does not touch a pairing
        code — so it is a value that satisfies the dataclass and nothing
        more.
        """
        host = getattr(request.app.state, "plugin_host", None)
        set_enabled = getattr(host, "set_enabled", None) if host is not None else None
        return EnrolmentService(
            layout=layout,
            secrets=SecretStore(layout),
            redeem=lambda _code: False,
            reload=scans.reload,
            set_enabled=set_enabled,
            package_limits=ctx.package_limits,
        )

    def _views(registry: MachineRegistry) -> tuple[tuple[MachineView, ...], str | None]:
        """Every machine, or ``((), the reason it could not be read)``.

        "Not installed" — nobody has ever joined — is not an error: it is the
        ordinary state before the first Join, and the page renders an empty
        list rather than a refusal for it. Any other refusal (an unreadable or
        future-format ``machines.toml``) is real and is shown, because
        silently reporting zero machines when there is a problem reading the
        file would hide exactly the thing this page exists to surface.
        """
        try:
            return registry.views(), None
        except MachineRejected as exc:
            if exc.reason == "not_installed":
                return (), None
            return (), exc.message

    async def _plugin_header(request: Request) -> dict[str, Any]:
        listing = await scans.current()
        view = next((row for row in listing.plugins if row.name == PLUGIN_NAME), None)
        failure = next((row for row in listing.failures if row.name == PLUGIN_NAME), None)
        if view is None and failure is None:
            # Neither loaded nor failed to load: this core has never installed
            # the plugin, which is the ordinary state before the first Join —
            # not the "last scan did not find it" failure `health_context`
            # would otherwise report.
            return {"name": PLUGIN_NAME, "version": "", "state": "new", "fail_reason": None}
        return health_context(PLUGIN_NAME, view, failure)

    async def _page_context(
        request: Request, *, notice: dict[str, str] | None = None
    ) -> dict[str, Any]:
        registry, index = _registry(request)
        views, list_error = _views(registry)
        now = datetime.now(UTC)
        rows = [machine_row(view, index, now=now) for view in views]
        return {
            **await ctx.shell(request, "plugins"),
            "plugin": await _plugin_header(request),
            "machines": rows,
            "machine_count": len(rows),
            "list_error": list_error,
            "notice": notice,
            "can_add": api_handler(request.app, "issue_pairing") is not None,
        }

    @router.get(
        "/plugins/workstation",
        response_class=HTMLResponse,
        summary="The workstation plugin: every enrolled machine",
    )
    async def workstation_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="workstation.html", context=await _page_context(request)
        )

    # -- pairing: add a workstation -----------------------------------------

    async def _pairing_status_context(request: Request) -> dict[str, Any]:
        """What the polled inner element says — never the code (see the
        module docstring for why it cannot be, and where the code actually
        lives on the page instead)."""
        handler = api_handler(request.app, "current_pairing")
        if handler is None:
            return {"status": "unavailable", "note": NO_PAIRING_OPERATIONS}
        snapshot = await handler(request=request)
        waiting = snapshot.status == PairingState.WAITING
        base: dict[str, Any] = {"status": str(snapshot.status)}
        if waiting:
            total = max(DEFAULT_TTL_SECONDS, 1)
            remaining = max(snapshot.expires_in_s, 0)
            base["percent"] = round(min(remaining / total, 1.0) * 100)
            minutes, seconds = divmod(remaining, 60)
            base["countdown"] = f"{minutes}:{seconds:02d}"
            base["poll"] = True
            return base
        if snapshot.status == PairingState.CLAIMED:
            registry, index = _registry(request)
            views, _error = _views(registry)
            now = datetime.now(UTC)
            view = find_view(views, snapshot.claimed_by) if snapshot.claimed_by else None
            base["joined"] = machine_row(view, index, now=now) if view else None
            base["claimed_by"] = snapshot.claimed_by
            # Out-of-band: the list behind the dialog refreshes the moment the
            # dialog itself learns a machine joined, so closing the dialog is
            # never what makes the new row appear (acceptance: "the machine
            # appears in the list by itself without the owner refreshing").
            base["machines_oob"] = [machine_row(v, index, now=now) for v in views]
            return base
        if snapshot.status == PairingState.SETTLING:
            # Redeemed; the enroller has not said how it went yet. `current()`'s
            # own docstring: ordinarily a split second, and for as long as the
            # code's TTL if it never says at all. Never guessed either way.
            base["poll"] = True
            return base
        if snapshot.status == PairingState.REFUSED and snapshot.refusal is not None:
            card = refusal_card(snapshot.refusal.reason, snapshot.refusal.machine)
            if card["reason"] == "name_taken" and snapshot.refusal.machine:
                # "Show the old one": the collision is with a machine of the
                # same name already on this core — the refused machine's own
                # name, which is why it collided — so the existing row is
                # found the same way anything else here is, never guessed.
                registry, _index = _registry(request)
                views, _error = _views(registry)
                existing = find_view(views, snapshot.refusal.machine)
                card["existing_machine_id"] = existing.machine.id if existing else None
            base["refusal"] = card
            return base
        # EXPIRED or NONE: nothing left to say beyond the state word itself.
        return base

    @router.post(
        "/plugins/workstation/pairing",
        response_class=HTMLResponse,
        summary="Issue a pairing code (shown once, here)",
    )
    async def workstation_pairing_issue(request: Request) -> HTMLResponse:
        """Mint a code through the JSON API's own ``issue_pairing`` — the same
        handler ``POST /admin/api/pairings`` calls, so the audit record and
        the "never anywhere else" rule for the code both come from there.
        """
        handler = api_handler(request.app, "issue_pairing")
        code: str | None = None
        note: str | None = None
        if handler is None:
            note = NO_PAIRING_OPERATIONS
        else:
            try:
                issued = await handler(request=request)
            except HTTPException as exc:
                note = refusal(exc)
            else:
                code = issued.code
        status_context = await _pairing_status_context(request)
        context = {**status_context, "code": code, "note": note or status_context.get("note")}
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request, name="fragments/workstation_pairing_dialog.html", context=context
            )
        return templates.TemplateResponse(
            request=request,
            name="workstation_pairing_page.html",
            context={**await ctx.shell(request, "plugins"), **context},
        )

    @router.get(
        "/plugins/workstation/pairing",
        response_class=HTMLResponse,
        summary="The pairing dialog, as a page (no-script fallback)",
    )
    async def workstation_pairing_page(request: Request) -> HTMLResponse:
        """Read-only — see :func:`workstation_pairing_issue` for the one route
        that actually mints a code. A plain refresh of this page can only ever
        show the status, never the code: codes are shown once, in the
        response that made one."""
        status_context = await _pairing_status_context(request)
        context = {"code": None, "note": None, **status_context}
        return templates.TemplateResponse(
            request=request,
            name="workstation_pairing_page.html",
            context={**await ctx.shell(request, "plugins"), **context},
        )

    @router.get(
        "/plugins/workstation/pairing/fragment",
        response_class=HTMLResponse,
        summary="The pairing dialog's own poll",
    )
    async def workstation_pairing_fragment(request: Request) -> HTMLResponse:
        """The inner element the dialog polls itself with — never the code,
        and never the rest of the dialog around it (see the module docstring:
        the code lives outside what this route re-renders)."""
        context = await _pairing_status_context(request)
        return templates.TemplateResponse(
            request=request, name="fragments/workstation_pairing_status.html", context=context
        )

    @router.post(
        "/plugins/workstation/pairing/cancel",
        response_class=HTMLResponse,
        response_model=None,
        summary="Cancel the live pairing code",
    )
    async def workstation_pairing_cancel(request: Request) -> HTMLResponse | RedirectResponse:
        handler = api_handler(request.app, "cancel_pairing")
        if handler is not None:
            await handler(request=request)
        if request.headers.get("HX-Request"):
            # `data-modal-close-after` (fragments/workstation_pairing_status.html)
            # closes the dialog on the browser side the moment this request
            # lands, whatever the body says — the same rule
            # fragments/confirm.html's own cancel already relies on.
            return HTMLResponse("")
        # No script ran this POST (ADR-0020's click-first bar): there is no
        # dialog to close, only the page underneath it to go back to.
        return RedirectResponse("/admin/plugins/workstation", status_code=status.HTTP_303_SEE_OTHER)

    # -- remove a machine -----------------------------------------------------

    def _machine_or_404(registry: MachineRegistry, machine_id: str) -> Machine:
        machine = registry.get(machine_id)
        if machine is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no workstation with that id.")
        return machine

    def _remove_confirm_context(machine: Machine, *, last: bool) -> dict[str, Any]:
        body = (
            f"This takes {machine.name} off the list and deletes its credential. "
            "A machine removed this way needs a fresh code to join again."
        )
        if last:
            body += LAST_MACHINE_REMOVE_SUFFIX.format(name=machine.name)
        return {
            "title": MACHINE_REMOVE_TITLE.format(name=machine.name),
            "body": body,
            "confirm_label": MACHINE_REMOVE_LABEL.format(name=machine.name),
        }

    @router.get(
        "/plugins/workstation/machines/{machine_id}/remove",
        response_class=HTMLResponse,
        summary="Confirm removing one workstation (page)",
    )
    async def workstation_machine_remove_confirm_page(
        request: Request, machine_id: str
    ) -> HTMLResponse:
        registry, _index = _registry(request)
        machine = _machine_or_404(registry, machine_id)
        last = len(registry.list()) <= 1
        return templates.TemplateResponse(
            request=request,
            name="confirm_page.html",
            context={
                **await ctx.shell(request, "plugins"),
                **_remove_confirm_context(machine, last=last),
                "action": f"/admin/plugins/workstation/machines/{machine_id}/remove",
                "back_href": "/admin/plugins/workstation",
                "back_label": "← workstation",
            },
        )

    @router.get(
        "/plugins/workstation/machines/{machine_id}/remove/fragment",
        response_class=HTMLResponse,
        summary="Confirm removing one workstation",
    )
    async def workstation_machine_remove_confirm(request: Request, machine_id: str) -> HTMLResponse:
        registry, _index = _registry(request)
        machine = _machine_or_404(registry, machine_id)
        last = len(registry.list()) <= 1
        return templates.TemplateResponse(
            request=request,
            name="fragments/confirm.html",
            context={
                **_remove_confirm_context(machine, last=last),
                "action": f"/admin/plugins/workstation/machines/{machine_id}/remove",
                "target": "body",
            },
        )

    @router.post(
        "/plugins/workstation/machines/{machine_id}/remove",
        response_class=HTMLResponse,
        summary="Remove one workstation",
    )
    async def workstation_machine_remove(request: Request, machine_id: str) -> HTMLResponse:
        # Imported inside the handler: `routes.py` builds this router, so a
        # module-level import back into `admin.routes` would be a cycle — the
        # same reason `persona_delete.py` imports `_record_change` here too.
        from personacore.admin.routes import _record_change

        user = require_user(request)
        # Looked up before the write, for the message alone: the id is what
        # `remove_machine` takes, and once it has run the name is gone from
        # the registry it would otherwise be read back from.
        registry, _index = _registry(request)
        machine = _machine_or_404(registry, machine_id)

        service = _enrolment_service(request)
        try:
            result = await service.remove_machine(machine_id)
        except EnrolmentRefused as exc:
            notice = {"kind": "refused", "message": f"Not removed: {exc.message}"}
            await _record_change(
                ctx.audit,
                user,
                action="plugins.workstation.remove",
                outcome=AuditOutcome.FAILURE,
                detail=enrolment_audit_detail(plugin=PLUGIN_NAME, reason=exc.reason),
            )
            return templates.TemplateResponse(
                request=request,
                name="workstation.html",
                context=await _page_context(request, notice=notice),
            )

        if not result.removed:
            # A double click, not a fault (MachineRemoved.removed's own
            # docstring) — the row was already gone by the time this posted.
            message = f"{machine.name} was already removed."
        elif result.switched_off:
            # The owner's own ruling: parked, not uninstalled. Said plainly,
            # so a "switched off" row does not read as something he broke.
            message = (
                f"Removed {machine.name}. No workstations are left, so the "
                "workstation plugin switched itself off — its folder and "
                "settings are still there, and it switches back on by "
                "itself the moment a workstation joins."
            )
        else:
            message = f"Removed {machine.name}."

        await _record_change(
            ctx.audit,
            user,
            action="plugins.workstation.remove",
            outcome=AuditOutcome.SUCCESS,
            detail=enrolment_audit_detail(
                plugin=PLUGIN_NAME,
                machines=result.machines_left,
                state="disabled" if result.switched_off else "unknown",
            ),
        )

        notice = {"kind": "ok", "message": message}
        return templates.TemplateResponse(
            request=request,
            name="workstation.html",
            context=await _page_context(request, notice=notice),
        )


__all__ = [
    "LOGS_LATER_NOTE",
    "PERMISSIONS_LATER_NOTE",
    "absolute_moment",
    "display_host",
    "endpoint_index",
    "find_view",
    "machine_row",
    "make_connection_state",
    "register",
    "relative_moment",
    "tool_count",
]
