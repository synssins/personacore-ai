"""Starting, stopping and moving everything that speaks or listens — ADR-0040.

Moved out of ``personacore.server``. Speech is the most detachable thing in the
system (ADR-0029 §6): an engine that will not load, a voice folder that cannot
be read, a Wyoming port already in use — none of them may cost the container.
The cost of that bargain is that the operator has to be able to see what was
lost and why, and this module owns the sentences that say so.

What each of these owns is the *rule*, not the wiring: a settings save may
never fail on account of speech, so every one of them is guarded and every
failure it swallows leaves a sentence behind for the screen to print. The
assembly hands over the registries and calls them; it decides none of that.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import FastAPI

from personacore import __version__
from personacore.config.hearing import HearingSettings
from personacore.config.voice import VoiceSettings
from personacore.config.wyoming import WyomingSettings
from personacore.hearing.registry import HearingRegistry
from personacore.voice.library import VoiceLibrary
from personacore.voice.registry import VoiceRegistry
from personacore.wyoming import WyomingService

log = structlog.get_logger(__name__)


def _wyoming_bind_failure(settings: WyomingSettings, exc: BaseException) -> str:
    """Why the listener is not up, in the words the Core screen prints.

    The address, the reason the operating system gave, and the fix. A switch
    that is on next to a port that never opened is the one state the screen
    cannot work out for itself, and "on — not listening" without a reason sends
    somebody to the logs for a sentence that already exists here.
    """
    reason = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
    return (
        f"{settings.host}:{settings.port} could not be opened — {reason}. "
        f"Change the address or the port and save again, or free that port."
    )


async def apply_hearing_settings(
    hearing_registry: HearingRegistry, new_hearing: HearingSettings
) -> None:
    """Start or stop recognisers to match the switches just saved.

    The mirror of `apply_voice_settings` below, guarded the same way and
    for the same reason: the registry already refuses to raise, and this
    catches anything it somehow does, because a settings save must never
    fail on account of listening.
    """
    try:
        await asyncio.to_thread(hearing_registry.apply, new_hearing)
    except Exception as exc:  # noqa: BLE001 - degradable, never fail the save
        log.warning("hearing_apply_failed", error=repr(exc))


async def apply_voice_settings(
    app: FastAPI,
    voice_registry: VoiceRegistry,
    voices: VoiceLibrary,
    new_voice: VoiceSettings,
) -> None:
    """Start or stop engines to match the switches just saved (PC-335).

    In a worker thread because starting an engine loads a model, which is
    blocking CPU work that would otherwise stall every other request on the
    loop. Guarded twice over: the registry already refuses to raise, and
    this catches anything it somehow does, because a settings save must
    never fail on account of speech.
    """
    try:
        report = await asyncio.to_thread(voice_registry.apply, new_voice)
    except Exception as exc:  # noqa: BLE001 - degradable: never fail the save
        log.warning("voice_apply_failed", error=repr(exc))
        return
    # The report's notes are the half no engine's own status carries:
    # "switched on and cannot run here", and a switch naming nothing. They
    # used to be built, logged nowhere and dropped here, so an operator who
    # flipped a switch and got silence had no line to read anywhere. Set
    # rather than accumulated — a note about a switch they have since
    # corrected must stop being shown the moment they save.
    app.state.voice_notes = tuple(report.notes)
    # Walk the voices directory here, and only here plus the Voices
    # screen, so `/health` can report skipped voices without crawling the
    # disk on every poll. Runs at startup and after every save, which is
    # when what is installed can have changed under a running core.
    try:
        await asyncio.to_thread(voices.refresh)
    except Exception as exc:  # noqa: BLE001 - degradable: never fail the save
        log.warning("voice_listing_failed", error=repr(exc))
    if report.notes:
        log.info("voice_engines_noted", notes=list(report.notes))
    if report.changed:
        log.info(
            "voice_engines_changed",
            started=list(report.started),
            stopped=list(report.stopped),
            failed=sorted(report.failed),
        )


async def apply_wyoming_settings(
    app: FastAPI,
    hearing_registry: HearingRegistry,
    voices: VoiceLibrary,
    new_wyoming: WyomingSettings,
) -> None:
    """Start, stop or move the Wyoming listener to match ``[wyoming]``.

    ADR-0010: the service must start as soon as the setting is turned on and
    saved. It used to bind once at startup, so the switch saved a setting and
    did nothing until the container restarted.

    **Nothing changed is two questions, not one.** The settings have to be
    identical *and* the listener has to be in the state they describe. The
    first question keeps a healthy listener untouched when somebody saves a
    persona; the second means an operator whose bind failed can hit save
    again and have it tried again, rather than being told nothing changed
    by a core that is not listening.

    A change is a **new service**, not a restarted one: host and port are
    read when the socket is opened and a running listener cannot be moved,
    so the old one is stopped and replaced. Stopping first is deliberate --
    the common change is a port move, and binding the new one first would
    leave two listeners up if the old one refused to stop.

    A bind that fails never fails the save. Speech to Home Assistant is as
    detachable as speech itself (ADR-0029 §6), a port already in use is
    something to report rather than a reason to refuse an unrelated
    settings change, and the admin UI is where the wrong setting gets
    fixed. The sentence is kept for the screen; the core stays up.
    """
    # What the running listener was built from, and the one save at a time
    # that gets to move it, both held on the application rather than in a
    # closure. They were locals of `create_app`; they are the listener's
    # state, not the assembly's, and the assembly no longer implements this.
    async with app.state.wyoming_lock:
        # `None` when this core could not build a listener at all (ADR-0040
        # §3). There is then nothing running to compare against and nothing to
        # stop, but a replacement is still built below -- so an operator whose
        # listener failed at boot can hit save and have it tried again, which
        # is the same bargain a failed bind already gets.
        service: WyomingService | None = app.state.wyoming
        if (
            service is not None
            and new_wyoming == app.state.wyoming_applied
            and service.running == new_wyoming.enabled
        ):
            return
        # Cleared here, before anything is attempted, so a reason from an
        # earlier failed bind cannot outlive the save that fixed it.
        app.state.wyoming_error = None
        if service is not None:
            await service.stop()
        app.state.surfaces.discard("wyoming")
        replacement = WyomingService(
            new_wyoming,
            hearing=hearing_registry,
            voices=voices,
            version=__version__,
        )
        # Published before it is started, so a bind that fails leaves the
        # screen reading the settings actually in force rather than the
        # ones that were replaced.
        app.state.wyoming = replacement
        app.state.wyoming_applied = new_wyoming
        try:
            listening = await replacement.start()
        except Exception as exc:  # noqa: BLE001 - degradable: never fail the save
            app.state.wyoming_error = _wyoming_bind_failure(new_wyoming, exc)
            log.error(
                "wyoming_bind_failed",
                host=new_wyoming.host,
                port=new_wyoming.port,
                error=repr(exc),
            )
            return
        app.state.wyoming_error = None
        if listening:
            app.state.surfaces.add("wyoming")
            log.info("surface_mounted", surface="wyoming", port=replacement.bound_port)
        else:
            log.info("wyoming_not_listening", reason="switched off")
