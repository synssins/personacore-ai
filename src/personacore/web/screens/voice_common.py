"""What the voice screens share: the engines, the voices, and the words.

This surface performs nothing about speech itself. ADR-0029's addendum splits
the work three ways, and the core voice module owns which engines exist, which
are running and whether a voice can be spoken; the screens reach it through
:func:`voice_registry` and :func:`voice_library`, which read it off the running
application exactly as :func:`~personacore.web.shared.api_handler` reaches
the JSON API's handlers. One lookup, and a core assembled without the voice
subsystem renders honestly rather than failing to start.

**The registry is asked; nothing here re-derives it.** Whether an engine offers
a switch is :attr:`~personacore.voice.registry.EngineStatus.can_switch`, and
why a persona is not speaking is
:attr:`~personacore.voice.registry.VoiceResolution.reason` — both written once,
in the module that knows, so PC-338's rule and PC-336's sentence have one
implementation between the engines screen, the persona screen and the speak
path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import Request

from personacore.plugins.voice_packages import VoiceMetadata

REGISTRY_ATTRIBUTE = "voice_registry"
"""Where the engine registry lives on the assembled application."""

LIBRARY_ATTRIBUTE = "voices"
"""Where the voice library lives, beside it.

Both are named once here rather than spelled out in three screens, so the day
either moves is a one-line day."""


def voice_registry(request: Request) -> Any | None:
    """The running application's engine registry, or ``None``.

    ``None`` means this core was assembled without the voice subsystem. Every
    control that needs one is then drawn with the reason beside it — the same
    treatment the plugin screens give a core with no plugin API, because hiding
    them would hide the shape of the screen and offering them would offer
    something that cannot work.
    """
    return getattr(request.app.state, REGISTRY_ATTRIBUTE, None)


def voice_library(request: Request) -> Any | None:
    """The running application's voice library, or ``None``."""
    return getattr(request.app.state, LIBRARY_ATTRIBUTE, None)


NO_VOICE_SUBSYSTEM = (
    "This core was assembled without its voice subsystem, so nothing can "
    "speak. Voices already installed are still listed."
)

NO_ENGINES = (
    "No speech engine is built into this core. Voices can still be installed "
    "and will speak once an engine arrives."
)

TEST_SENTENCE = (
    "I saw the kettle boil about an hour ago, so I put the day's first cup "
    "down beside you."
)
SPEAK_TOO_LONG = "That is more than {limit} characters. Nothing was synthesised."
"""Refused before the work starts, not after."""

SPEAK_MAX_CHARACTERS = 400
"""How much text the test-speak box accepts.

Generous for a sentence and far short of anything worth synthesising as a
document. The engine has its own ceiling; this one exists so the box refuses
before the work starts rather than after."""

SPEAK_HINT = (
    "Type what you want to hear. Testing one word? Put it in a sentence — "
    "alone, this voice may mispronounce it even when the fix is correct."
)
"""Said under the box, because the trap is invisible and costs an afternoon."""

CARRIED_INTO_A_SENTENCE = (
    "One word alone is not a fair test, so it was spoken as “Say {word} "
    "again.” — listen to the middle word."
)
"""Shown when a bare word is carried into a carrier sentence.

Not a refusal. The operator asked to hear one word, which is the natural thing
to do after editing its pronunciation, and refusing would send them away to
compose a sentence themselves. Saying one word alone would be worse: it comes
out wrong even when the fix is right (PC-203), so the answer would be a lie in
whichever direction they read it. Speaking it in context and saying so is the
only honest option that still answers the question they asked."""

"""What the test-speak control says, and it is a sentence on purpose (PC-203).

Measured, not preferred. This voice says "about" as "bout", "put" as "putt",
"saw" as "sow" and "day" as "deh" when each is given alone, and says all four
correctly inside a sentence — so a one-word test reports faults that are not
there and hides the ones that are, which turns the one tool for checking a
pronunciation fix into a thing that lies in both directions. Those four words
are in this line deliberately; changing it means keeping them.
"""

TEST_SPEAK_UNAVAILABLE = "Nothing can speak this voice right now."

TEST_SPEAK_ENGINE_OFF = "{display} is switched off, so nothing can speak this voice yet."
TEST_SPEAK_ENGINE_UNAVAILABLE = "{display} cannot run here: {reason}"
TEST_SPEAK_ENGINE_MISSING = "This core does not have the {engine} engine."


def speak_blocker(engine: str, row: dict[str, Any], resolution: Any | None) -> str:
    """Why this voice cannot be spoken, in the voice screen's own terms.

    The library's own sentence is written for the *persona* screen — it ends
    "this persona replies in text", which is the right thing to say beside a
    persona and the wrong thing to say beside a voice nobody has assigned yet.
    So the engine-level cases are worded here, and anything the library knows
    that this screen does not (a running engine that has not got this voice)
    falls through to its sentence, which is already about the voice.
    """
    if resolution is not None and resolution.can_speak:
        return ""
    if not row:
        return TEST_SPEAK_ENGINE_MISSING.format(engine=engine)
    if not row.get("available"):
        return TEST_SPEAK_ENGINE_UNAVAILABLE.format(
            display=row["display"], reason=row.get("reason") or "no reason was given."
        )
    if not row.get("running"):
        return TEST_SPEAK_ENGINE_OFF.format(display=row["display"])
    reason = getattr(resolution, "reason", "") if resolution is not None else ""
    return reason or TEST_SPEAK_UNAVAILABLE


def engine_rows(registry: Any | None, voices: Sequence[VoiceMetadata]) -> list[dict[str, Any]]:
    """Every engine, with its switch, its voice count and what it is doing.

    Read straight off :meth:`~personacore.voice.registry.VoiceRegistry.status`.
    ``switchable`` is the registry's own ``can_switch`` rather than a second
    reading of "available", so PC-338 — an unavailable engine offers no control
    at all — is decided in one place and this screen obeys it rather than
    re-deciding it.

    The voice count comes from disk rather than from the engine, because a
    voice installed for an engine that is switched off is still installed, and
    a count that only appeared once an engine was running would read as "your
    upload did not work".
    """
    if registry is None:
        return []
    counts: dict[str, int] = {}
    for voice in voices:
        counts[voice.engine] = counts.get(voice.engine, 0) + 1
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
            "voices": counts.get(status.id, 0),
        }
        for status in registry.status()
    ]
    return sorted(rows, key=lambda row: row["display"].lower())


def voice_rows(
    voices: Sequence[VoiceMetadata], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every installed voice, in one list, labelled with its engine.

    ADR-0029 §5: one list, every voice, every engine, and the engine is a label
    rather than a step. Built from what is on disk so a voice belonging to an
    engine this build does not have is still shown — it can still be exported
    and removed, and a voice that vanished from the screen because an image was
    rolled back is the worst possible answer to "where did my voice go".
    """
    engines = {row["id"]: row for row in rows}
    return [
        {
            "engine": voice.engine,
            "id": voice.id,
            "display": voice.display,
            "description": voice.description or "",
            "language": voice.language or "",
            "label": voice_label(voice),
            "engine_known": voice.engine in engines,
            "engine_enabled": bool(engines.get(voice.engine, {}).get("enabled")),
            "engine_running": bool(engines.get(voice.engine, {}).get("running")),
            "unsupported": voice.unsupported or "",
        }
        for voice in voices
    ]


def voice_label(voice: VoiceMetadata) -> str:
    """``GLaDOS (vits-onnx)`` — ADR-0029 §5's one spelling.

    The display name falls back to the id, so a voice nobody has named yet is
    still a row a person can click rather than a bracket with nothing in front
    of it. The same fallback :class:`~personacore.voice.engine.Voice` makes,
    for the same reason.
    """
    return f"{voice.display} ({voice.engine})"


def personas_using(
    engine_id: str, voices: Sequence[VoiceMetadata], personas: Sequence[Any]
) -> list[str]:
    """The personas whose voice belongs to one engine, by display name.

    Used to write the warning **before** a switch is turned off (PC-336).
    Matching is on the engine alone: a persona naming a voice this engine no
    longer has is in the same position as one naming a voice it does, and both
    go quiet if it is switched off.
    """
    ours = {voice.id for voice in voices if voice.engine == engine_id}
    named: list[str] = []
    for persona in personas:
        engine = getattr(persona, "voice_engine", None)
        name = getattr(persona, "voice_name", None)
        if engine == engine_id or (
            name is not None and name in ours and engine in (None, engine_id)
        ):
            named.append(getattr(persona, "display_name", None) or getattr(persona, "name", "?"))
    return sorted(set(named))


DISABLE_WARNING = (
    "{personas} {verb} the {engine} engine. Switching it off leaves {pronoun} "
    "replying in text instead of speaking. Switch it back on and {pronoun} "
    "speaks again."
)
"""What is said where the switch was flipped (PC-336).

States the consequence and stops. A persona whose engine is off is not broken
and must not be described as though it were: it answers, in text, and the
operator gets it back by switching the engine on.
"""


def disable_warning(engine_display: str, personas: Sequence[str]) -> str:
    """The warning, or ``""`` when no persona uses that engine."""
    if not personas:
        return ""
    plural = len(personas) > 1
    return DISABLE_WARNING.format(
        personas=", ".join(personas),
        verb="use" if plural else "uses",
        engine=engine_display,
        pronoun="them" if plural else "it",
    )


# ---------------------------------------------------------------------------
# The persona's picker — one list, every engine (PC-202)
# ---------------------------------------------------------------------------

VOICE_VALUE_SEPARATOR = "/"
"""What joins an engine id to a voice id in one ``<option value>``.

The same spelling :attr:`~personacore.voice.library.LibraryEntry.key` uses, so
the value this form submits and the key the library hands out are one string
rather than two conventions that agree until somebody edits one. Neither id may
contain a slash — both charsets are checked before either reaches a path — so
the split is unambiguous.
"""

VOICE_NONE = ""
VOICE_NONE_LABEL = "(no voice — replies in text)"

PERSONA_VOICE_NOTE = "One list, every voice from every engine."
"""ADR-0029 §5 on the screen, in a line. The engine is how a voice is spoken,
not a decision the operator navigates (PC-202)."""

PERSONA_VOICE_NONE_INSTALLED = (
    "No voices are installed, so there is nothing to choose. Upload one on the "
    "Voices screen and it appears here."
)


def voice_value(engine: str | None, voice: str | None) -> str:
    """One persona's stored voice as the picker's option value."""
    if not engine or not voice:
        return VOICE_NONE
    return f"{engine}{VOICE_VALUE_SEPARATOR}{voice}"


def split_voice_value(raw: str) -> tuple[str | None, str | None]:
    """A submitted option value back into an engine and a voice, or two ``None``.

    Nothing is validated here. The caller checks both halves against the voice
    module's own id rules **before** either reaches a path, which is the
    ordering that matters and the one the plugin installer's traversal bug was
    about.
    """
    engine, separator, voice = (raw or "").partition(VOICE_VALUE_SEPARATOR)
    if not separator or not engine.strip() or not voice.strip():
        return None, None
    return engine.strip(), voice.strip()


def persona_voice_options(voices: Sequence[VoiceMetadata]) -> list[dict[str, str]]:
    """Every installed voice as ``GLaDOS (vits-onnx)``, with "no voice" first.

    Every voice from every engine, whether or not its engine is switched on. A
    voice hidden because its engine is off would be a voice an operator could
    not select ahead of remembering to switch that engine on, and the engine is
    a label rather than a gate.
    """
    return [{"value": VOICE_NONE, "label": VOICE_NONE_LABEL}] + [
        {"value": voice_value(voice.engine, voice.id), "label": voice_label(voice)}
        for voice in voices
    ]


def persona_voice_warning(
    engine: str | None, voice: str | None, library: Any | None
) -> str:
    """Why this persona is not speaking, or ``""`` when it is (PC-336).

    **The library writes the sentence, not this screen.**
    :meth:`~personacore.voice.library.VoiceLibrary.resolve` is what the speak
    path itself asks, so what the operator reads here is what actually decided
    that the reply came out as text — rather than a second explanation beside
    it, ready to disagree.

    Silent when the persona has no voice at all (that is a choice, not a
    fault) and when there is no library to ask, because nothing should be
    claimed about an engine this core cannot see.
    """
    if not engine or not voice or library is None:
        return ""
    resolution = library.resolve(engine, voice)
    if resolution.can_speak:
        return ""
    return resolution.reason or ""


__all__ = [
    "DISABLE_WARNING",
    "LIBRARY_ATTRIBUTE",
    "NO_ENGINES",
    "NO_VOICE_SUBSYSTEM",
    "PERSONA_VOICE_NONE_INSTALLED",
    "PERSONA_VOICE_NOTE",
    "REGISTRY_ATTRIBUTE",
    "CARRIED_INTO_A_SENTENCE",
    "SPEAK_HINT",
    "SPEAK_TOO_LONG",
    "SPEAK_MAX_CHARACTERS",
    "TEST_SENTENCE",
    "TEST_SPEAK_ENGINE_MISSING",
    "TEST_SPEAK_ENGINE_OFF",
    "TEST_SPEAK_ENGINE_UNAVAILABLE",
    "TEST_SPEAK_UNAVAILABLE",
    "VOICE_NONE",
    "VOICE_NONE_LABEL",
    "VOICE_VALUE_SEPARATOR",
    "disable_warning",
    "engine_rows",
    "persona_voice_options",
    "persona_voice_warning",
    "personas_using",
    "speak_blocker",
    "split_voice_value",
    "voice_label",
    "voice_library",
    "voice_registry",
    "voice_rows",
    "voice_value",
]
