"""The recognisers screen: one switch per recogniser, and what it can do.

The listening mirror of :mod:`personacore.web.screens.voice`, and the two
rules that shape that screen shape this one, word for word.

**A recogniser this build cannot run is offered no switch.** It is drawn, with
its reason beside it, and there is nothing to click. A control that appears to
work and does not is the failure this project keeps producing in new clothes,
and a greyed-out switch is still a control somebody will click.

**Available and enabled are different words and the screen must not blur
them.** A recogniser can be switched on and still not run — the model files are
not in the image, and the switch stays on because rolling back to an image that
has them should not lose the setting. :class:`~personacore.hearing.registry.EngineStatus`
carries both, so the two are read rather than derived, and the row says which
of them is the reason nothing is listening.

Saving goes through ``save_config``, the same persist-and-apply path the voice
switches take: it writes ``[hearing.engines.<id>] enabled``, and the server's
own apply starts or stops that recogniser there and then (ADR-0010). Calling
the registry from here would start something ``core.toml`` still said was off.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from personacore.admin.authn import require_admin
from personacore.audit.models import AuditOutcome
from personacore.web.shared import (
    UIContext,
    current_config,
    settings_problems,
)

REGISTRY_ATTRIBUTE = "hearing_registry"
"""Where the recogniser registry lives on the assembled application."""

ENGINE_FIELD_PREFIX = "engine."
"""The switch's form field, one per recogniser id."""

ENGINE_PRESENT_PREFIX = "present."
"""Says the switch was on the page.

An unticked checkbox submits nothing at all, which is indistinguishable from
"that recogniser was not on this form" — and the safe reading of an ambiguous
submission is to change nothing. The same marker the engines screen and every
plugin toggle use, for the same reason.
"""

NO_HEARING_SUBSYSTEM = (
    "This core was assembled without its hearing subsystem, so nothing can "
    "turn audio into words."
)

NO_ENGINES = (
    "No recogniser is built into this core. Nothing can turn audio into words "
    "until one arrives in the image."
)

ENGINES_LEDE = (
    "Every recogniser has its own switch. Off means off — not loaded, no "
    "memory, no CPU — and saving acts immediately."
)

PROBLEMS_LEDE = (
    "Listening is running, but some of it was dropped on the way in. Each line "
    "says what, and what to do about it."
)

ENABLED_BUT_UNAVAILABLE = (
    "{display} is switched on but cannot run on this build, so nothing is "
    "listening through it. The switch is left as you set it: an image that "
    "carries it will start it."
)
"""Said on the row, because on and running are different words.

The registry writes this same sentence into its apply report and the server
drops it, so this screen writes its own from the two fields the status already
carries — which is the state an operator is least able to work out for
themselves: they flipped a switch, saved, and got silence.
"""

ENGINES_UNCHANGED = "Nothing changed — those switches were already set that way."

ENGINE_STARTED = "{display} is listening."
ENGINE_STOPPED = "{display} is switched off and holds no model."
ENGINE_FAILED = "{display} could not be switched {direction}: {reason}"

IGNORED_LEDE = (
    "Some of what was sent had no switch on this page, and nothing was "
    "written for it."
)

IGNORED_UNAVAILABLE = (
    "{display} has no switch here because it cannot run on this build: "
    "{reason} Its setting is unchanged."
)

IGNORED_UNKNOWN = "This core has no recogniser called '{engine}'. Nothing was written for it."


def hearing_registry(request: Request) -> Any | None:
    """The running application's recogniser registry, or ``None``.

    ``None`` means this core was assembled without the hearing subsystem. The
    screen then renders with the reason on it rather than failing, which is the
    same bargain the voice screens make.
    """
    return getattr(request.app.state, REGISTRY_ATTRIBUTE, None)


def engine_rows(registry: Any | None) -> list[dict[str, Any]]:
    """Every recogniser, with its switch and what it is doing.

    Read straight off :meth:`~personacore.hearing.registry.HearingRegistry.status`.
    ``switchable`` is the registry's own ``can_switch`` rather than a second
    reading of "available", so the rule that an unavailable recogniser offers
    no control at all is decided in one place.
    """
    if registry is None:
        return []
    rows = [
        {
            "id": status.id,
            "display": status.display,
            "available": status.available,
            "reason": status.unavailable_reason or "",
            "switchable": status.can_switch,
            "enabled": status.enabled,
            "running": status.running,
            "state": str(status.state),
            "error": status.error or "",
            "note": (
                ENABLED_BUT_UNAVAILABLE.format(display=status.display)
                if status.enabled and not status.available
                else ""
            ),
        }
        for status in registry.status()
    ]
    return sorted(rows, key=lambda row: row["display"].lower())


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the recognisers page and the post that works its switches."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import _record_change

    templates = ctx.templates
    _shell = ctx.shell

    async def _context(
        request: Request,
        *,
        save_result: dict[str, str] | None = None,
        ignored: list[str] | None = None,
    ) -> dict[str, Any]:
        """The recognisers page, for the page and for a re-render after a save.

        One function behind both, because a second construction of the list is
        a second answer to "can this recogniser be switched".
        """
        registry = hearing_registry(request)
        rows = engine_rows(registry)
        return {
            **await _shell(request, "hearing"),
            "engines": rows,
            "lede": ENGINES_LEDE,
            # Everything the registry had to drop, in its own sentences: a
            # recogniser that would not construct, a malformed [hearing] key,
            # a switch naming a recogniser this image has not got.
            "problems": list(registry.problems()) if registry is not None else [],
            "problems_lede": PROBLEMS_LEDE,
            "ignored": ignored or [],
            "ignored_lede": IGNORED_LEDE,
            "unavailable_note": NO_HEARING_SUBSYSTEM if registry is None else "",
            "no_engines": NO_ENGINES if registry is not None and not rows else "",
            "can_switch": registry is not None and any(row["switchable"] for row in rows),
            "save_result": save_result,
        }

    async def _page(
        request: Request,
        result: dict[str, str] | None = None,
        *,
        ignored: list[str] | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="hearing_engines.html",
            context=await _context(request, save_result=result, ignored=ignored),
        )

    @router.get("/hearing", response_class=HTMLResponse, summary="Recognisers")
    async def hearing_engines_page(request: Request) -> HTMLResponse:
        """Every recogniser, its switch, and whether it is listening."""
        require_admin(ctx.require_user(request))
        return await _page(request)

    @router.post(
        "/hearing/engines",
        response_class=HTMLResponse,
        summary="Switch recognisers on and off",
    )
    async def hearing_engines_save(request: Request) -> HTMLResponse:
        """Save the switches, which is what starts and stops the recognisers.

        Through the settings document, not around it: the switch lives in
        ``[hearing.engines.<id>] enabled`` and ``save_config`` is the JSON
        API's own persist-and-apply helper, so the file and what is running
        cannot disagree.

        A plain form post answered with the whole page: no htmx header, no
        JSON, no fetch — the same shape the speech engines screen settled on.
        """
        user = require_admin(ctx.require_user(request))
        registry = hearing_registry(request)
        form = await request.form()
        try:
            wanted = {
                key[len(ENGINE_PRESENT_PREFIX) :]: bool(
                    form.get(f"{ENGINE_FIELD_PREFIX}{key[len(ENGINE_PRESENT_PREFIX):]}")
                )
                for key in form
                if isinstance(key, str) and key.startswith(ENGINE_PRESENT_PREFIX)
            }
        finally:
            await form.close()

        current, unreadable = current_config(ctx.layout)
        if registry is None or current is None:
            return await _page(
                request, {"kind": "refused", "message": unreadable or NO_HEARING_SUBSYSTEM}
            )

        known = {status.id: status for status in registry.status()}
        switchable = {status.id for status in known.values() if status.can_switch}
        before = {status.id: status.enabled for status in known.values()}

        settings = dict(current.settings)
        hearing = dict(settings.get("hearing") or {})
        engines = {key: dict(value) for key, value in (hearing.get("engines") or {}).items()}

        changed: list[str] = []
        # A value for a switch this page does not offer is answered, not
        # dropped. Refusing the whole post would throw away the switches that
        # were valid, and a recogniser offering no control is a normal state
        # rather than an error.
        ignored: list[str] = []
        for engine_id, enabled in sorted(wanted.items()):
            if engine_id not in switchable:
                status = known.get(engine_id)
                if status is None:
                    ignored.append(IGNORED_UNKNOWN.format(engine=engine_id))
                else:
                    ignored.append(
                        IGNORED_UNAVAILABLE.format(
                            display=status.display,
                            reason=status.unavailable_reason or "no reason was given for it.",
                        )
                    )
                continue
            engines[engine_id] = {**engines.get(engine_id, {}), "enabled": enabled}
            if before.get(engine_id) != enabled:
                changed.append(engine_id)
        hearing["engines"] = engines
        settings["hearing"] = hearing

        if not changed:
            return await _page(
                request,
                {
                    "kind": "refused" if ignored else "none",
                    "message": " ".join(ignored) if ignored else ENGINES_UNCHANGED,
                },
                ignored=ignored,
            )

        try:
            await ctx.save_config(settings, user, action="config.update")
        except HTTPException as exc:
            _errors, message = settings_problems(exc)
            return await _page(request, {"kind": "refused", "message": message}, ignored=ignored)

        # Read the outcome back off the registry rather than assuming it: a
        # recogniser that was asked to start and failed is owed a sentence, and
        # "saved" alone would be a claim about the file rather than about what
        # is running.
        context = await _context(request, ignored=ignored)
        rows = {row["id"]: row for row in context["engines"]}
        said: list[str] = []
        faults: list[str] = []
        for engine_id in changed:
            row = rows.get(engine_id, {})
            display = row.get("display", engine_id)
            if row.get("error"):
                faults.append(
                    ENGINE_FAILED.format(
                        display=display,
                        direction="on" if wanted[engine_id] else "off",
                        reason=row["error"],
                    )
                )
            elif row.get("running"):
                said.append(ENGINE_STARTED.format(display=display))
            else:
                said.append(ENGINE_STOPPED.format(display=display))
            await _record_change(
                ctx.audit,
                user,
                action=(
                    "hearing.engine.enable" if wanted[engine_id] else "hearing.engine.disable"
                ),
                outcome=AuditOutcome.SUCCESS,
                detail={"engine": engine_id},
            )

        context["save_result"] = (
            {"kind": "refused", "message": " ".join(faults + ignored + said)}
            if faults or ignored
            else {"kind": "ok", "message": " ".join(said)}
        )
        return templates.TemplateResponse(
            request=request, name="hearing_engines.html", context=context
        )
