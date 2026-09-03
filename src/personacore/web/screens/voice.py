"""The engines screen: one switch per engine, and what switching one off costs.

ADR-0029 §2 — every supported engine is built into the image and each has its
**own** switch. Turning Piper off does nothing to Kokoro, saving the setting
starts or stops that engine, and there is no restart, no compose edit and no
shell anywhere in it (PC-335, ADR-0010).

Two rules shape the markup more than anything else:

**An engine this build cannot run is offered no switch** (PC-338). It is drawn,
with its reason beside it, and there is nothing to click. A control that
appears to work and does not is the failure this project keeps producing in new
clothes, and a greyed-out switch is still a control somebody will click.

**Switching one off says what goes quiet** (PC-336). The persona whose voice
belongs to that engine keeps working and replies in text; the warning is here,
where the change was made, and on the persona itself. Nothing crashes and no
other persona is affected.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from personacore.audit.models import AuditOutcome
from personacore.plugins.voice_packages import installed_voices
from personacore.web.screens.voice_common import (
    NO_ENGINES,
    NO_VOICE_SUBSYSTEM,
    disable_warning,
    engine_rows,
    personas_using,
    voice_registry,
)
from personacore.web.shared import (
    UIContext,
    current_config,
    settings_problems,
)

ENGINE_FIELD_PREFIX = "engine."
"""The switch's form field, one per engine id."""

ENGINE_PRESENT_PREFIX = "present."
"""Says the switch was on the page.

An unticked checkbox submits nothing at all, which is indistinguishable from
"that engine was not on this form" — and the safe reading of an ambiguous
submission is to change nothing. The same marker ``_controls.html`` puts behind
every plugin toggle, for the same reason.
"""

ENGINES_UNCHANGED = "Nothing changed — those switches were already set that way."

ENGINE_STARTED = "{display} is running."
ENGINE_STOPPED = "{display} is switched off and holds no model."
ENGINE_FAILED = "{display} could not be switched {direction}: {reason}"

ENGINES_LEDE = (
    "Every engine has its own switch. Off means off — not loaded, no memory, no "
    "CPU — and saving acts immediately."
)

PROBLEMS_LEDE = (
    "Speech is running, but some of it was dropped on the way in. Each line "
    "says what, and what to do about it."
)
"""The heading over :meth:`~personacore.voice.registry.VoiceRegistry.problems`.

This screen used to show none of them. An engine that would not construct, a
switch naming an engine this image has not got, a ``[voice]`` key nothing reads
— every one of those reached ``/health`` and stopped there, so the operator
looking at the page where speech is configured saw a build that simply never
had the thing they were looking for. The registry already writes the sentences;
this page had only to print them.
"""

NOTES_LEDE = (
    "One or more switches here are on and the engine is not. Each line says "
    "which and why."
)
"""The heading over what the last apply had to say (:attr:`ApplyReport.notes`).

The case this exists for is the one an operator is least able to work out for
themselves: they turned a switch on, saved, and got no speech. ``notes`` is
where :meth:`~personacore.voice.registry.VoiceRegistry.apply` writes "switched
on in the settings but cannot run here — it stays off", and until now it was
returned by ``apply``, asserted in a test and read by nothing. A switch that is
on beside an engine that is off, with no sentence between them, is exactly the
silence PC-338 and PC-341 are about.
"""

IGNORED_LEDE = (
    "Some of what was sent had no switch on this page, and nothing was "
    "written for it."
)

IGNORED_UNAVAILABLE = (
    "{display} has no switch here because it cannot run on this build: "
    "{reason} Its setting is unchanged."
)

IGNORED_UNKNOWN = "This core has no speech engine called '{engine}'. Nothing was written for it."
"""What a submitted value for a switch that does not exist is answered with.

**Recorded as ignored rather than refused**, and the choice is between two bad
options rather than an obvious one. Refusing the whole post would throw away
the switches that *were* valid — an operator who turned Piper on gets nothing
saved because a stale tab also sent a field for an engine that has since gone
unavailable — and PC-338's whole point is that an engine offering no control is
a normal state, not an error. So the valid half is saved and the rest is
answered by name.

What it must not do is what it did: ``continue``, in silence. A value that
arrives and vanishes with nothing said is the same defect as a switch that says
nothing, seen from the other side.
"""


NOTES_ATTRIBUTE = "voice_notes"
"""Where the last apply's notes sit on the assembled application.

Read off ``app.state`` the same way the registry and the library are
(:func:`~personacore.web.screens.voice_common.voice_registry`), and
defaulted to nothing rather than assumed present: a core assembled without the
voice subsystem has no notes, and a screen that raised looking for them would
have made speech load-bearing on the way to saying it is not.
"""


def _apply_notes(request: Request) -> list[str]:
    """What the last settings apply had to say about the switches, or nothing."""
    notes = getattr(request.app.state, NOTES_ATTRIBUTE, ()) or ()
    return [str(note) for note in notes]


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the engines page and the post that works its switches."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import _record_change, build_persona_listing

    templates = ctx.templates
    _shell = ctx.shell

    async def _personas() -> list[Any]:
        """Every persona, for the warning. Off the event loop — each entry is a
        directory read, the same reason the personas screen threads it."""
        listing = await asyncio.to_thread(
            build_persona_listing, ctx.personas, ctx.personas.default_persona
        )
        return list(listing.personas)

    async def _context(
        request: Request,
        *,
        save_result: dict[str, str] | None = None,
        ignored: list[str] | None = None,
    ) -> dict[str, Any]:
        """The engines page, for the page and for a re-render after a save.

        One function behind both, because a second construction of the list is
        a second answer to "is this engine switchable".
        """
        registry = voice_registry(request)
        voices = await asyncio.to_thread(installed_voices, ctx.layout)
        rows = engine_rows(registry, voices)
        personas = await _personas()
        problems = list(registry.problems()) if registry is not None else []
        # What the last apply had to say. Deduplicated against `problems`
        # because an unknown-engine switch is written into both, and one
        # condition printed twice on one page is how an operator starts
        # counting faults instead of reading them.
        notes = [note for note in _apply_notes(request) if note not in problems]
        for row in rows:
            # The warning is computed for every engine, on by default, so it is
            # already on the row the operator is about to switch off. Written
            # ahead rather than after: a consequence explained only once the
            # change has happened is not a warning.
            row["warning"] = disable_warning(
                row["display"], personas_using(row["id"], voices, personas)
            )
        return {
            **await _shell(request, "voice"),
            "engines": rows,
            "lede": ENGINES_LEDE,
            # Everything the registry had to drop, in its own sentences: a
            # refused engine, an engine that would not construct, a malformed
            # [voice] setting, a switch naming an engine this core has not got.
            # None of them is fatal and none of them is silent.
            "problems": problems,
            "problems_lede": PROBLEMS_LEDE,
            # A switch that is on beside an engine that is not. Written by
            # `VoiceRegistry.apply`, carried on its report, and until now read
            # by nobody: the operator flipped a switch, expected speech, and
            # the one sentence explaining the silence went into a local
            # variable and was dropped.
            "notes": notes,
            "notes_lede": NOTES_LEDE,
            # Values this form was sent for switches it does not offer.
            "ignored": ignored or [],
            "ignored_lede": IGNORED_LEDE,
            "unavailable_note": NO_VOICE_SUBSYSTEM if registry is None else "",
            "no_engines": NO_ENGINES if registry is not None and not rows else "",
            "can_switch": registry is not None and any(row["switchable"] for row in rows),
            "save_result": save_result,
        }

    @router.get("/voice", response_class=HTMLResponse, summary="Speech engines")
    async def voice_engines_page(request: Request) -> HTMLResponse:
        """Every engine, its switch, its voice count and whether it is running."""
        return templates.TemplateResponse(
            request=request, name="voice_engines.html", context=await _context(request)
        )

    @router.post(
        "/voice/engines",
        response_class=HTMLResponse,
        summary="Switch speech engines on and off",
    )
    async def voice_engines_save(request: Request) -> HTMLResponse:
        """Save the switches, which is what starts and stops the engines.

        **Through the settings document, not around it.** The switch lives in
        ``[voice.engines.<id>] enabled``, and ``save_config`` is the JSON API's
        own persist-and-apply helper — the same path ``PUT /admin/api/config``
        takes, which validates, writes atomically, records the audit line and
        then reconciles the running engines against what was saved (PC-335,
        ADR-0010). Calling the registry directly from here would start an
        engine that ``core.toml`` still said was off, so the switch and the
        file would disagree until the next restart put it back.

        A plain form post answered with the whole page: no htmx header, no
        JSON, no fetch. The plugins screen learned what a scripted control
        costs the day a plugin could not be uninstalled, and a switch that
        starts a speech engine is not the place to relearn it.
        """
        user = ctx.require_user(request)
        registry = voice_registry(request)
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
                request,
                {"kind": "refused", "message": unreadable or NO_VOICE_SUBSYSTEM},
            )

        # Only engines that really have a switch on this page. A value for one
        # that does not came from something other than this form, so it is
        # ignored rather than written into the file.
        switchable = {
            status.id for status in registry.status() if status.can_switch
        }
        settings = dict(current.settings)
        voice = dict(settings.get("voice") or {})
        engines = {key: dict(value) for key, value in (voice.get("engines") or {}).items()}
        before = {
            status.id: status.enabled for status in registry.status()
        }
        known = {status.id: status for status in registry.status()}
        changed: list[str] = []
        # A value for a switch this page does not offer is **answered**, not
        # dropped. It used to `continue` in silence, which meant a submission
        # could be accepted, written nowhere, and reported as "nothing changed"
        # — a control that appears to work and does not, arriving from the
        # other direction. See IGNORED_UNKNOWN for why it is ignored rather
        # than refusing the whole save.
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
                            reason=status.unavailable_reason
                            or "no reason was given for it.",
                        )
                    )
                continue
            engines[engine_id] = {**engines.get(engine_id, {}), "enabled": enabled}
            if before.get(engine_id) != enabled:
                changed.append(engine_id)
        voice["engines"] = engines
        settings["voice"] = voice

        if not changed:
            # Still an answer about what was sent: an operator whose whole
            # submission named switches that do not exist must not be told
            # "nothing changed" as though they had changed nothing.
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
            return await _page(
                request, {"kind": "refused", "message": message}, ignored=ignored
            )

        # Read the outcome back off the registry rather than assuming it: an
        # engine that was asked to start and failed is owed a sentence, and
        # "saved" on its own would be a claim about the file rather than about
        # what is running.
        context = await _context(request, ignored=ignored)
        rows = {row["id"]: row for row in context["engines"]}
        # `said` and `faults` rather than `notes` and `problems`: the context
        # already carries two lists by those names and they are different
        # things — one is what this save did, the other is a standing
        # condition of the build.
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
                # PC-336 belongs beside the switch that was just flipped: a
                # persona that has gone quiet is named here rather than
                # discovered on its own page later.
                if row.get("warning"):
                    said.append(row["warning"])
            await _record_change(
                ctx.audit,
                user,
                action="voice.engine.enable" if wanted[engine_id] else "voice.engine.disable",
                outcome=AuditOutcome.SUCCESS,
                detail={"engine": engine_id},
            )

        # What was ignored travels with the answer, so a save that half worked
        # says both halves in one place rather than leaving the operator to
        # notice a switch that never moved.
        context["save_result"] = (
            {"kind": "refused", "message": " ".join(faults + ignored + said)}
            if faults or ignored
            else {"kind": "ok", "message": " ".join(said)}
        )
        return templates.TemplateResponse(
            request=request, name="voice_engines.html", context=context
        )

    async def _page(
        request: Request, result: dict[str, str], *, ignored: list[str] | None = None
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="voice_engines.html",
            context=await _context(request, save_result=result, ignored=ignored),
        )
