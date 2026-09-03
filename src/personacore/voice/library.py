"""Every installed voice, across every engine, as one list — ADR-0029 §5.

Picking an engine is not a step (PC-202). An operator sees ``GLaDOS
(vits-onnx)`` in one list and chooses it; the engine is how a voice is spoken,
not a decision they navigate.

Two things this module is careful about.

**A disabled engine's voices are still known.** They are listed, they are
marked not speakable, and they carry the sentence saying why. That distinction
is the whole of PC-336: a persona whose engine is switched off is not broken
and not silently muted — it replies in text, and somebody can be told exactly
which switch to flip. A library that simply omitted a disabled engine's voices
could only say "that voice does not exist", which is both wrong and unhelpful.

**Nothing an engine does escapes.** A voice folder that cannot be read is
skipped and named (never fatal); an engine whose ``voices()`` raises — which
the contract forbids, but a contract is not a guarantee — costs that engine's
listing and nothing else. One unreadable voice must not cost an operator the
other nine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from personacore.config.appdata import AppdataLayout
from personacore.voice.engine import (
    VOICE_ID_PATTERN,
    Audio,
    Engine,
    EngineError,
    SkippedVoice,
    Voice,
    VoiceState,
)
from personacore.voice.pacing import Pacing, PausedWord, read_pacing, speak_paced
from personacore.voice.registry import EngineState, EngineStatus, VoiceRegistry

log = structlog.get_logger(__name__)


def engine_voice_root(voices_root: Path, engine_id: str) -> Path:
    """``appdata/voices/<engine-id>`` — the one place this path is composed.

    The engine is never handed the voices root and never builds this itself,
    which is how "an engine never reads outside its own directory" is enforced
    by construction rather than by asking nicely.
    """
    return voices_root / engine_id


@dataclass(frozen=True)
class LibraryEntry:
    """One voice as a surface shows it."""

    voice: Voice
    engine_id: str
    engine_display: str
    speakable: bool
    reason: str | None = None
    """Why it cannot be spoken right now, in a sentence. ``None`` when it can."""

    @property
    def label(self) -> str:
        """``GLaDOS (vits-onnx)``."""
        return self.voice.label

    @property
    def key(self) -> str:
        """``vits-onnx/glados`` — what a form field submits.

        Engine and voice travel together because a voice name alone is not
        unique: two engines may both ship a voice called ``glados``, and
        resolving that by guessing would pick a different one depending on
        which engines happen to be switched on.
        """
        return f"{self.engine_id}/{self.voice.id}"


@dataclass(frozen=True)
class VoiceListing:
    """The whole picker, in one object."""

    entries: tuple[LibraryEntry, ...] = ()
    skipped: tuple[SkippedVoice, ...] = ()
    """Voice folders that could not be read, each with its reason. Shown, not
    swallowed — an operator who uploaded a voice and cannot find it in the list
    is owed the reason."""
    problems: tuple[str, ...] = ()
    """Engine-level trouble: an engine that could not list at all, a refused
    registration, a malformed ``[voice]`` setting."""

    def speakable(self) -> tuple[LibraryEntry, ...]:
        return tuple(entry for entry in self.entries if entry.speakable)


@dataclass(frozen=True)
class VoiceResolution:
    """The answer to "can this persona's voice speak right now, and if not,
    what do I tell the operator" — PC-336, in one object.

    The persona path branches on :attr:`can_speak` and prints :attr:`reason`.
    It never inspects the registry itself, and it never treats any of this as
    an error: a disabled engine is an expected condition with a sentence
    attached, not an exception to propagate.
    """

    state: VoiceState
    can_speak: bool
    reason: str | None = None
    """Plain English, always set unless the voice can speak. Written for the
    operator, so it names the engine and what to do about it."""
    voice: Voice | None = None
    engine: Engine | None = None
    """The engine to call :meth:`~personacore.voice.engine.Engine.speak` on.
    Set **only** when :attr:`can_speak` — holding a disabled engine's object
    is not permission to start it."""
    engine_id: str = ""
    engine_display: str = ""
    pacing: Pacing = field(default_factory=Pacing)
    """This voice's own gaps (PC-342), read from its folder by :meth:`
    VoiceLibrary.resolve`.

    Carried on the resolution rather than fetched inside :meth:`speak` so that
    one filesystem read answers a whole reply, and so that a caller can see
    what a voice will be paced at without synthesising anything."""

    @property
    def replies_in_text(self) -> bool:
        """ADR-0029 §6 read the other way round: losing an engine costs speech
        and nothing else, so anything that is not ready is simply text."""
        return not self.can_speak

    def speak(self, text: str, *, pauses: Sequence[PausedWord] = (), **knobs: Any) -> Audio:
        """Audio for ``text`` in this voice, paced by its punctuation (PC-342).

        Only ever called after :attr:`can_speak`; calling it otherwise raises
        :class:`~personacore.voice.engine.EngineError` carrying the same
        sentence :attr:`reason` holds, so a caller that forgot to check gets
        the operator-facing explanation rather than an ``AttributeError`` on
        ``None``.

        **This is where core splits and paces**, because it is the only place
        in this build that calls an engine's ``speak`` at all — so the rule
        that an engine is handed one piece of text and never sees punctuation
        as timing (ADR-0029) is true by construction rather than by everyone
        remembering. :func:`~personacore.voice.pacing.speak_paced` decides how
        many calls that is; a single sentence, and any text at all on a voice
        whose gaps are zero, is still exactly one call with the text untouched.

        ``pauses`` is the **speaking persona's** list of words that take
        pauses of their own — a tic of that character rather than of this
        voice, which is why it arrives per call instead of sitting in the
        voice's own pacing. It is timing only: no word of ``text`` changes.

        The engine gets its **own** voice object back
        (:attr:`~personacore.voice.engine.Voice.speakable_object`), not core's
        adopted copy.
        """
        if not self.can_speak or self.engine is None or self.voice is None:
            raise EngineError(self.reason or "This voice cannot be spoken right now.")
        engine = self.engine
        speakable = self.voice.speakable_object
        return speak_paced(
            lambda piece: engine.speak(speakable, piece, **knobs),
            text,
            self.pacing,
            pauses=pauses,
        )


class VoiceLibrary:
    """Voices, across engines, and whether each one can be spoken.

    Reads the filesystem on every call rather than caching. A voice is
    installed by uploading a zip (PC-337) and the operator expects to see it
    immediately; a cache would put a stale list between them and the thing they
    just uploaded, and the directory is a handful of folders.

    :meth:`snapshot` is the one exception and is not a cache in front of that:
    it never reads, it only remembers what the last read found, for the single
    caller that must answer without touching the disk at all.
    """

    def __init__(self, registry: VoiceRegistry, layout: AppdataLayout) -> None:
        self._registry = registry
        self._layout = layout
        # The last listing computed, kept so something that must not touch the
        # disk can still say what the disk said. See `snapshot`.
        self._last: VoiceListing | None = None

    @property
    def root(self) -> Path:
        return self._layout.voices

    # -- listing ----------------------------------------------------------

    def listing(self) -> VoiceListing:
        """Every voice from every engine, disabled engines included."""
        entries: list[LibraryEntry] = []
        skipped: list[SkippedVoice] = []
        problems: list[str] = list(self._registry.problems())

        for status in self._registry.status():
            engine = self._registry.get(status.id)
            if engine is None:  # pragma: no cover - status and get share a dict
                continue
            voices, engine_skipped, problem = self._read(engine, status)
            if problem:
                problems.append(problem)
            skipped.extend(engine_skipped)
            reason = None if status.speakable else self._engine_reason(status)
            for voice in voices:
                entries.append(
                    LibraryEntry(
                        voice=voice,
                        engine_id=status.id,
                        engine_display=status.display,
                        speakable=status.speakable,
                        reason=reason,
                    )
                )

        entries.sort(key=lambda entry: (entry.voice.display.lower(), entry.engine_id))
        made = VoiceListing(
            entries=tuple(entries),
            skipped=tuple(skipped),
            problems=tuple(problems),
        )
        # Recorded on the way past rather than by a second walk: every surface
        # that lists voices pays for the read anyway, so the snapshot `/health`
        # reads is kept current for free by the screens an operator uses.
        self._last = made
        return made

    def snapshot(self) -> VoiceListing:
        """The last listing computed, **without reading the disk**.

        ``/health`` is polled by the container healthcheck every few seconds
        and must not walk ``appdata/voices`` to answer, but the skipped voices
        it is owed are only knowable by walking. So the walk is done where
        voices actually change — at startup and on every settings apply
        (:func:`personacore.server.create_app`), and on every visit to the
        Voices screen, which install and remove both re-render — and this hands
        back what it found. Empty until the first walk, which is honest: no
        voice has been read yet, so none has been skipped yet.

        A cache with a clock was the alternative and is worse in both
        directions: it still walks on a timer nobody asked for, and it puts a
        stale list between an operator and the voice they just uploaded, which
        is the one thing :class:`VoiceLibrary` was written not to do.
        """
        return self._last or VoiceListing()

    def refresh(self) -> VoiceListing:
        """Walk now and update :meth:`snapshot`. Never raises.

        Called from the settings-apply path, which runs inside the running
        application and must not be able to fail it. :meth:`listing` already
        contains every engine's misbehaviour, but "already contains" is a
        claim about today's code and this is a promise about the core staying
        up.
        """
        try:
            return self.listing()
        except Exception as exc:  # noqa: BLE001 - speech may never cost the core
            log.warning("voice_listing_refresh_failed", error=repr(exc))
            return self.snapshot()

    def voices_for(self, engine_id: str) -> tuple[Voice, ...]:
        """One engine's voices, whether or not it is switched on."""
        status = self._registry.status_for(engine_id)
        engine = self._registry.get(engine_id)
        if status is None or engine is None:
            return ()
        voices, _, _ = self._read(engine, status)
        return tuple(voices)

    def _read(
        self, engine: Engine, status: EngineStatus
    ) -> tuple[list[Voice], list[SkippedVoice], str | None]:
        """Ask one engine what it has, and let nothing out.

        Called for disabled engines too. Listing is filesystem work by
        contract, so it does not start anything — and if some engine
        implementation ever breaks that, the failure lands here as a named
        problem rather than as a crash.
        """
        root = engine_voice_root(self.root, status.id)
        try:
            found, engine_skipped = engine.voices(root)
        except Exception as exc:  # noqa: BLE001 - one engine's listing, and only its own
            log.warning("voice_listing_failed", engine=status.id, error=repr(exc))
            return (
                [],
                [],
                f"{status.display} could not list its voices: {exc}. Its voices are "
                "not shown; every other engine is unaffected.",
            )

        voices: list[Voice] = []
        skipped: list[SkippedVoice] = [
            SkippedVoice.adopt(item, status.id) for item in (engine_skipped or ())
        ]
        for source in found or ():
            # Adopted rather than passed through: the engine's own voice object
            # carries model paths and phoneme maps that no template should see,
            # and the engines were written against their own field names (see
            # `Voice.adopt`). The original travels on `native` for the trip
            # back to `speak`.
            voice = Voice.adopt(source, status.id)
            if voice is None:
                skipped.append(
                    SkippedVoice(
                        id="?",
                        reason=(
                            f"{status.display} returned something with no voice name "
                            "on it, so it was skipped."
                        ),
                        engine_id=status.id,
                    )
                )
                continue
            problem = self._refuse(voice, root)
            if problem is not None:
                skipped.append(
                    SkippedVoice(
                        id=voice.id,
                        reason=problem,
                        path=voice.path,
                        engine_id=status.id,
                    )
                )
                continue
            voices.append(voice)
        return voices, skipped, None

    def _refuse(self, voice: Voice, root: Path) -> str | None:
        """``None`` if the voice may be listed, else why it may not.

        Core's own door, closed regardless of what an engine believes. A voice
        whose path is outside its engine's directory is refused here even if
        the engine was happy to return it — appdata containment (spec §7) is
        not an engine's decision to make.
        """
        voice_id = getattr(voice, "id", None)
        if not isinstance(voice_id, str) or not VOICE_ID_PATTERN.match(voice_id):
            # The installer's own clause, word for word
            # (`voice_packages.VOICE_ID_UNUSABLE`): one rule, stated the same
            # way wherever it refuses. It says lowercase because the rule
            # enforces lowercase — `GLaDOS` and `glados` are one folder on
            # Windows and two on Linux, so a persona's stored reference would
            # resolve differently per host.
            return (
                f"'{voice_id}' is not a usable voice name, so it was skipped. An "
                "id is 1–64 characters of lowercase letters, digits, dots, dashes "
                "and underscores, starting with a letter or a digit. Rename the "
                "folder and it appears."
            )
        path = getattr(voice, "path", None)
        if path is None:
            return None
        try:
            resolved = Path(path).resolve()
        except OSError as exc:
            return f"'{voice_id}' could not be read and was skipped: {exc}."
        if resolved != root.resolve() and root.resolve() not in resolved.parents:
            return (
                f"'{voice_id}' was skipped because it points outside "
                f"{root}. A voice lives in its own engine's folder."
            )
        return None

    # -- the persona question ---------------------------------------------

    def resolve(self, engine_id: str | None, voice_id: str | None) -> VoiceResolution:
        """Can this voice speak right now, and if not, what do I say?

        Every branch returns a resolution. Nothing raises, because none of
        these is an error: a persona with no voice, a persona whose engine is
        off and a persona whose engine is missing all reply in text, and the
        only difference between them is the sentence.
        """
        if not engine_id or not voice_id:
            return VoiceResolution(
                state=VoiceState.NO_VOICE_CHOSEN,
                can_speak=False,
                reason="No voice is set for this persona, so it replies in text.",
                engine_id=engine_id or "",
            )

        status = self._registry.status_for(engine_id)
        engine = self._registry.get(engine_id)
        if status is None or engine is None:
            return VoiceResolution(
                state=VoiceState.ENGINE_UNKNOWN,
                can_speak=False,
                reason=(
                    f"This persona asks for the speech engine '{engine_id}', which "
                    "this build does not have. It replies in text. Check the "
                    "engine name on the Voice screen."
                ),
                engine_id=engine_id,
            )

        voice = self._find(engine, status, voice_id)

        if not status.speakable:
            return VoiceResolution(
                state=self._state_for(status),
                can_speak=False,
                reason=self._engine_reason(status, voice_id=voice_id),
                voice=voice,
                engine_id=status.id,
                engine_display=status.display,
            )

        if voice is None:
            return VoiceResolution(
                state=VoiceState.VOICE_MISSING,
                can_speak=False,
                reason=(
                    f"{status.display} is running but has no voice called "
                    f"'{voice_id}' installed, so this persona replies in text. "
                    "Upload the voice on the Voice screen."
                ),
                engine_id=status.id,
                engine_display=status.display,
            )

        return VoiceResolution(
            state=VoiceState.READY,
            can_speak=True,
            voice=voice,
            engine=engine,
            engine_id=status.id,
            engine_display=status.display,
            pacing=self.pacing_for(voice),
        )

    def pacing_for(self, voice: Voice | None) -> Pacing:
        """One voice's gaps, from its own folder. Never raises.

        Read by **core**, from the voice directory core already knows, rather
        than asked of the engine: pacing is not the engine's to know (ADR-0029)
        and a setting an engine had to surface would be a setting each engine
        surfaced differently. A voice whose path is unknown — an engine that
        returned one without a folder — paces at the default rather than not at
        all.
        """
        try:
            paced, _notes = read_pacing(getattr(voice, "path", None))
        except Exception as exc:  # noqa: BLE001 - pacing may never cost the voice
            log.warning("voice_pacing_unreadable", error=repr(exc))
            return Pacing()
        return paced

    def _find(self, engine: Engine, status: EngineStatus, voice_id: str) -> Voice | None:
        voices, _, _ = self._read(engine, status)
        for voice in voices:
            if voice.id == voice_id:
                return voice
        return None

    @staticmethod
    def _state_for(status: EngineStatus) -> VoiceState:
        if status.state is EngineState.UNAVAILABLE:
            return VoiceState.ENGINE_UNAVAILABLE
        if status.state is EngineState.FAILED:
            return VoiceState.ENGINE_FAILED
        return VoiceState.ENGINE_DISABLED

    @staticmethod
    def _engine_reason(status: EngineStatus, voice_id: str | None = None) -> str:
        """The sentence PC-336 owes the operator.

        Three different situations that look identical from the persona's side
        and are not from the operator's: a switch they can flip, a switch that
        does not exist on this hardware, and a switch that is on and broken.
        Each gets the action that actually applies to it.
        """
        subject = f"The voice '{voice_id}'" if voice_id else "This voice"
        if status.state is EngineState.UNAVAILABLE:
            because = status.unavailable_reason or "it cannot run on this hardware."
            return (
                f"{subject} is spoken by {status.display}, which cannot run here: "
                f"{because} This persona replies in text."
            )
        if status.state is EngineState.FAILED:
            return (
                f"{subject} is spoken by {status.display}, which is switched on but "
                f"did not start. {status.error or ''} This persona replies in text "
                "until it does."
            ).replace("  ", " ")
        return (
            f"{subject} is spoken by {status.display}, which is switched off, so "
            "this persona replies in text. Switch it on under Voice to hear it "
            "again — nothing else about the persona changes."
        )


@dataclass(frozen=True)
class VoiceHealth:
    """What ``/health`` and the engines screen say about speech.

    Assembled here rather than in the server so the two cannot drift, and
    deliberately carrying no path: a health endpoint is read by things that
    have no business knowing the appdata layout.
    """

    engines: tuple[EngineStatus, ...] = ()
    problems: tuple[str, ...] = ()
    skipped: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, object]:
        return {
            "engines": [
                {
                    "id": status.id,
                    "display": status.display,
                    "available": status.available,
                    "enabled": status.enabled,
                    "running": status.running,
                    "state": status.state.value,
                    "unavailable_reason": status.unavailable_reason,
                    "error": status.error,
                }
                for status in self.engines
            ],
            "problems": list(self.problems),
            "skipped_voices": list(self.skipped),
        }


def voice_health(
    registry: VoiceRegistry,
    library: VoiceLibrary | None = None,
    *,
    listing: VoiceListing | None = None,
) -> VoiceHealth:
    """One reading of what speech is doing, for /health and the UI alike.

    Three ways to ask, and the difference between them is who pays for the
    disk read:

    ``registry`` alone — engines and problems, no voices, no filesystem.

    ``library`` — walks now. For a caller that wants the truth this instant
    and is a person waiting for a page.

    ``listing`` — a walk somebody else already did, handed in. This is what
    ``/health`` uses, with :meth:`VoiceLibrary.snapshot`: the healthcheck polls
    every few seconds and has no business walking ``appdata/voices`` that
    often, but a skipped voice's reason is still owed to whoever reads
    ``skipped_voices`` there. Passing the snapshot is how it gets both. When
    both are given, ``listing`` wins — it is the explicit one.
    """
    if listing is None and library is not None:
        listing = library.listing()
    # Union rather than "the listing's, if there is one". A snapshot taken
    # before the first walk is an empty listing, not a missing one, and reading
    # its `problems` in place of the registry's would drop every engine this
    # build could not load — the exact line `/health` is read for.
    problems = list(registry.problems())
    if listing is not None:
        problems.extend(item for item in listing.problems if item not in problems)
    return VoiceHealth(
        engines=registry.status(),
        problems=tuple(problems),
        skipped=(
            tuple(f"{item.engine_id}/{item.id}: {item.reason}" for item in listing.skipped)
            if listing is not None
            else ()
        ),
    )
