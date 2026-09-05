"""Personas — spec section 5.5, Appendix B.

"Persona = files in ``/appdata/personas/<name>/`` (system prompt + metadata).
Hot-swappable at any time from the admin UI or by voice command; file changes
hot-reload. No restarts."

Both halves of that sentence are this module's whole job:

* **Hot-swap** is free once loading is per-turn and cheap: the loop asks for a
  persona by name on every turn, so changing ``PolicyProfile.persona`` or
  passing an override takes effect on the next thing the user says, with no
  session state to invalidate.
* **Hot-reload** is a stat-based cache. Every load stats the persona's files
  and re-reads them if their mtime or size moved. Editing the prompt file with
  a text editor is therefore enough; nothing needs to tell the core it
  happened. Stat-per-turn is a rounding error next to an LLM call, and the
  alternative (a filesystem watcher) is a background task and a class of bug
  for no gain at this scale.

Paths come from :class:`personacore.config.AppdataLayout` — this module never
builds ``root / "personas"`` itself, which is exactly the rule that module
exists to enforce. Persona names are untrusted (they arrive from config, from a
policy profile, or from a voice command), so they are validated against a strict
pattern *and* re-checked for containment before anything is opened.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from personacore.agent.errors import PersonaInvalidError, PersonaNotFoundError
from personacore.audit import get_logger
from personacore.config.appdata import AppdataError, AppdataLayout
from personacore.config.settings import LLMSettings

logger = get_logger(__name__)

PROMPT_FILENAMES: tuple[str, ...] = (
    "system_prompt.md",
    "system_prompt.txt",
    "prompt.md",
    "prompt.txt",
)
"""Accepted names for the prompt file, in preference order. A short fixed list
rather than "any .md in the folder": spec section 9 wants plain-English errors,
and "I couldn't find system_prompt.md" is one."""

METADATA_FILENAME = "persona.toml"
"""Optional. TOML because the rest of the project reads TOML (stdlib
``tomllib``) and the admin UI writes it back with ``tomli-w``."""

DEFAULT_PERSONA_NAME = "default"

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""No slashes, no leading dot, bounded length. Traversal is caught again by
``AppdataLayout.require_inside``; this is the cheap first door."""

# ---------------------------------------------------------------------------
# Speech pauses — a persona's verbal tics, timed rather than rewritten
# ---------------------------------------------------------------------------
#
# A configured persona says "Hmm." constantly. Written down it reads fine.
# Spoken, it arrives from nowhere and sounds tacked on — and the reason is not
# the word or its punctuation. **"Hmm." is a complete sentence**, so the
# splitter (:mod:`personacore.voice.pacing`) gives it a full sentence gap on
# both sides: 450 ms of silence, a murmur, 450 ms more. The pacing is
# delivering a throwaway sound as if it were a pronouncement.
#
# So the text is left exactly as the model wrote it and **only the silence
# around the word changes**. The two repairs tried first are worth naming,
# because both are the obvious move:
#
# * *Prompt the model out of it.* A local model breaks the instruction about
#   one time in ten however it is worded. It cannot be prompted away.
# * *Find-and-replace "Hmm." with "Hmm?" before speaking.* This changes the
#   meaning to fix the delivery. A question mark asserts a question is being
#   asked, and thinking out loud is not a question — the model was writing
#   correct English and the replacement makes it wrong. It also pushes the
#   model toward writing unnatural text to compensate, and every instance
#   still comes out identical, so one mechanical tic replaces another.
#
# What is left is the thing actually at fault: the gap. A persona lists the
# words it says like that, with the pauses they should get, and the words
# themselves are never touched — not in speech, not on the screen, not in the
# transcript.
#
# This lives on the **persona**, not the voice. "Hmm." is a tic of this
# character: point the same voice at another persona and there is nothing to
# time. A voice's pronunciation overrides (``glados = ɡlˈædɒs``) are the other
# shape — how that voice says a word, whoever is speaking. Different concern,
# different home.

SPEECH_TABLE = "speech"
PAUSES_KEY = "pauses"
"""Where the pauses live in ``persona.toml``::

    [speech.pauses]
    Hmm = [120, 180]
    Mmm = 150

A pair is ``[before, after]`` in milliseconds; one number is the same gap on
both sides. Both spellings are accepted because this file is edited by hand as
often as it is written by the admin screen, and ``Mmm = 150`` is what somebody
writes when they mean "short on both sides"."""

MAX_SPEECH_PAUSES = 100
"""How many words one persona can time.

A character has a handful of verbal habits, not a dictionary. Something
arriving from outside gets a bound before it gets a use (spec §7), and this one
also bounds the matcher built from it. Past the cap the first
:data:`MAX_SPEECH_PAUSES` are kept and the rest ignored — a reply is never the
casualty of a long list — while the admin form refuses the save outright, so
the operator is told rather than silently trimmed."""

MAX_PAUSE_MS = 5000
"""The longest gap a persona can name, matching the ceiling a voice's own
pacing is held to (:data:`personacore.voice.pacing.GAP_LIMITS`). **Zero is
legal and means no pause at all**, which is how a word is made to run straight
into what follows it."""

MAX_PAUSE_WORD_CHARS = 64
"""A "word" here is a word or a short phrase — ``uh huh``, ``well now``. Longer
than this is a sentence, and timing sentences is what the voice's own pacing
marks already do."""


class SpeechPause(NamedTuple):
    """One word, and the silence either side of it, in milliseconds.

    A plain tuple on purpose: the pacing module consumes these and knows
    nothing about personas, so what crosses that seam is
    ``(word, before_ms, after_ms)`` rather than a class either side imports.
    """

    word: str
    before_ms: int
    after_ms: int


PAUSE_LINE_REFUSED = (
    "Line {number} of the speech pauses has no “=” in it. Each line is one "
    "word, an equals sign, then the pause before it and the pause after it in "
    "milliseconds: Hmm = 120, 180"
)

PAUSE_WORD_REQUIRED = (
    "Line {number} of the speech pauses has no word in front of the “=”. The "
    "word comes first: Hmm = 120, 180"
)

PAUSE_WORD_TOO_LONG = (
    "“{word}” is longer than {limit} characters. This box times a word or a "
    "short phrase; a whole sentence is what the voice's own pacing already does."
)

PAUSE_NOT_A_NUMBER = (
    "Line {number} of the speech pauses does not say how long to wait. After "
    "the “=” write the milliseconds before and after the word — Hmm = 120, 180 "
    "— or one number for the same pause on both sides."
)

PAUSE_OUT_OF_RANGE = (
    "A pause of {value} milliseconds is outside 0 to {limit}. Zero means no "
    "pause at all; {limit} is five seconds, which is already a very long silence."
)

PAUSE_TOO_MANY = f"That is more than {MAX_SPEECH_PAUSES} speech pauses. Nothing was saved."


def _pause_ms(raw: str) -> int | None:
    """One number from the box, or ``None`` when it is not one.

    A trailing ``ms`` is accepted and dropped: somebody told the box takes
    milliseconds writes ``120ms`` about half the time, and refusing that would
    be a form arguing with an operator who was right.
    """
    text = raw.strip().casefold().removesuffix("ms").strip()
    return int(text) if text.isdigit() else None


def _pause_pair(raw: object) -> tuple[int, int] | None:
    """``[before, after]``, ``150`` or ``"120, 180"`` as one pair of gaps.

    ``None`` for anything else — which is how both callers say "that was not a
    pause" without either of them raising.
    """
    if isinstance(raw, bool):
        # ``true`` is an int in Python and is a duration in nobody's head.
        return None
    values: list[int]
    if isinstance(raw, int):
        values = [raw, raw]
    elif isinstance(raw, str):
        parts = raw.split(",")
        if len(parts) > 2:
            return None
        numbers = [_pause_ms(part) for part in parts]
        if any(number is None for number in numbers):
            return None
        values = [number for number in numbers if number is not None]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
        if not items or len(items) > 2:
            return None
        if not all(isinstance(item, int) and not isinstance(item, bool) for item in items):
            return None
        values = [int(item) for item in items]
    else:
        return None
    if len(values) == 1:
        values = [values[0], values[0]]
    before, after = values[0], values[1]
    if not (0 <= before <= MAX_PAUSE_MS and 0 <= after <= MAX_PAUSE_MS):
        return None
    return before, after


def clean_pauses(pairs: Iterable[tuple[Any, Any]]) -> tuple[SpeechPause, ...]:
    """Pauses in the shape the splitter wants, with everything unusable dropped.

    **Never raises.** This runs over a file an operator edited by hand, and a
    persona whose ``[speech.pauses]`` block is nonsense must still load and
    still answer — it simply answers with today's pacing. A broken entry costs
    that entry and nothing else.

    * Two entries for one word differing only in case are one entry, first
      kept: matching is case-insensitive, so the second could never fire.
    * **Longest word first**, so ``uh huh`` is offered before ``uh`` and wins
      the text they both cover. Ties break alphabetically, so a persona pauses
      identically on every load.
    """
    kept: list[SpeechPause] = []
    seen: set[str] = set()
    for raw_word, raw_gaps in pairs:
        if len(kept) >= MAX_SPEECH_PAUSES:
            break
        if not isinstance(raw_word, str):
            continue
        word = raw_word.strip()
        folded = word.casefold()
        if not word or len(word) > MAX_PAUSE_WORD_CHARS or folded in seen:
            continue
        gaps = _pause_pair(raw_gaps)
        if gaps is None:
            continue
        seen.add(folded)
        kept.append(SpeechPause(word=word, before_ms=gaps[0], after_ms=gaps[1]))
    return tuple(sorted(kept, key=lambda pause: (-len(pause.word), pause.word.casefold())))


def read_pauses(metadata: Mapping[str, Any]) -> tuple[SpeechPause, ...]:
    """The ``[speech.pauses]`` table of one persona's metadata.

    Anything that is not a table of words — a missing section, a number, a
    list — reads as no pauses rather than as a fault. A persona is its prompt;
    this is a decoration on the way to a voice and may not take the character
    down with it.
    """
    speech = metadata.get(SPEECH_TABLE)
    if not isinstance(speech, Mapping):
        return ()
    table = speech.get(PAUSES_KEY)
    if not isinstance(table, Mapping):
        return ()
    return clean_pauses(list(table.items()))


def parse_pause_lines(text: str) -> tuple[tuple[SpeechPause, ...], str | None]:
    """The admin box's lines as pauses, or one sentence saying what is wrong.

    Refuses a line it cannot read rather than dropping it, for the reason the
    pronunciation box does (:func:`personacore.plugins.voice_packages.write_pronunciation`):
    a pause that silently did not save is a fault the operator would go hunting
    for in the engine. Blank lines and ``#`` comments are neither — they are
    how a list gets annotated.

    The refusal is returned rather than raised: the caller re-renders the form
    with what was typed still in the box, and nothing has been written.
    """
    kept: list[tuple[str, list[int]]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            return (), PAUSE_LINE_REFUSED.format(number=number)
        word, _, gaps = line.partition("=")
        word = word.strip()
        if not word:
            return (), PAUSE_WORD_REQUIRED.format(number=number)
        if len(word) > MAX_PAUSE_WORD_CHARS:
            return (), PAUSE_WORD_TOO_LONG.format(
                word=word[:MAX_PAUSE_WORD_CHARS] + "…", limit=MAX_PAUSE_WORD_CHARS
            )
        pair = _pause_pair(gaps)
        if pair is None:
            # "Not a number" and "past the ceiling" are told apart: the
            # operator who typed 9000 knows what a number is and needs the
            # limit, not a lesson in the format.
            over = next(
                (
                    value
                    for value in (_pause_ms(part) for part in gaps.split(",")[:2])
                    if value is not None and value > MAX_PAUSE_MS
                ),
                None,
            )
            if over is not None:
                return (), PAUSE_OUT_OF_RANGE.format(value=over, limit=MAX_PAUSE_MS)
            return (), PAUSE_NOT_A_NUMBER.format(number=number)
        kept.append((word, list(pair)))
    if len(kept) > MAX_SPEECH_PAUSES:
        return (), PAUSE_TOO_MANY
    return clean_pauses(kept), None


def pause_lines(pauses: Iterable[tuple[str, int, int]]) -> str:
    """The pauses back as one line each, for the box that wrote them."""
    return "\n".join(f"{word} = {before}, {after}" for word, before, after in pauses)


def pause_table(pauses: Iterable[tuple[str, int, int]]) -> dict[str, list[int]]:
    """The pauses as the ``[speech.pauses]`` table ``persona.toml`` holds."""
    return {word: [before, after] for word, before, after in pauses}


# ---------------------------------------------------------------------------
# The model this character thinks with — optional, and per persona
# ---------------------------------------------------------------------------
#
# A persona may name its own LLM connection. Absent, it uses whatever the system
# is configured to use; present, it uses exactly what it says.
#
#     [llm]
#     base_url = "http://box:11434/v1"
#     model = "llama3.1:70b"
#
# **Absence is the only thing that means "follow the system".** A persona that
# spells out today's default settings is pinned to those values and stays on
# them when the system default moves — which is the whole reason this is stored
# as an endpoint rather than as a reference to a role. An operator who
# deliberately pinned a character to a model did not ask to be moved off it by
# somebody else's edit on the Models screen.
#
# The value is an ordinary :class:`~personacore.config.settings.LLMSettings`,
# the same shape ``core.toml`` uses for a role. One shape means one base-URL
# normaliser, one set of timeouts, and — the part that matters — one convention
# for the credential: ``api_key_secret`` names a secret, never holds one. There
# is no second way to write a key down.

LLM_TABLE = "llm"

PERSONA_LLM_NOT_A_TABLE = (
    "The persona {name!r} has an “llm” setting that is not a connection. Its own "
    "connection is a section with an address and a model name in it: an [llm] "
    "section holding base_url and model."
)

PERSONA_LLM_INCOMPLETE = (
    "The persona {name!r} has its own [llm] connection but no {missing}. A "
    "persona's own connection needs both an address and a model name — remove "
    "the [llm] section to use the system's model instead."
)

PERSONA_LLM_REFUSED = "The [llm] connection of the persona {name!r} could not be read: {why}"


def read_connection(name: str, metadata: Mapping[str, Any]) -> LLMSettings | None:
    """One persona's own connection, or ``None`` when it uses the system's.

    **A broken ``[llm]`` section is refused, not ignored.** Every other
    decoration in this file degrades quietly — a nonsense pauses table costs
    the pauses and nothing else — but this one cannot, because the quiet
    outcome would be the persona answering from the system default while its
    file says otherwise. Silently speaking as the wrong model is worse than
    saying, in a sentence, that this character cannot be loaded until the
    section is fixed. It stops one persona; the others and the screen are
    untouched, and the edit form opens on a persona that will not load.
    """
    table = metadata.get(LLM_TABLE)
    if table is None:
        return None
    if not isinstance(table, Mapping):
        raise PersonaInvalidError(
            PERSONA_LLM_NOT_A_TABLE.format(name=name),
            detail=f"[{LLM_TABLE}] is {type(table).__name__}, not a table",
        )
    # Checked before validation because LLMSettings has defaults for both:
    # validating a table holding only `api_key_secret` would succeed and quietly
    # point the persona at localhost, which is a connection nobody wrote down.
    missing = [
        key for key in ("base_url", "model") if not str(table.get(key) or "").strip()
    ]
    if missing:
        raise PersonaInvalidError(
            PERSONA_LLM_INCOMPLETE.format(
                name=name,
                missing=" or ".join(
                    {"base_url": "address", "model": "model name"}[key] for key in missing
                ),
            ),
            detail=f"[{LLM_TABLE}] is missing {', '.join(missing)}",
        )
    try:
        return LLMSettings.model_validate(dict(table))
    except ValidationError as exc:
        # The field name and the rule it broke, and **never the value**. A
        # hand-written base URL can carry credentials in it
        # (`http://user:secret@host`), which is the same reason the health
        # dashboard shows a digest of an endpoint rather than its address — so
        # neither the sentence nor the log line is allowed to quote what was in
        # the file. `repr(exc)` would: pydantic puts the input in it.
        why = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise PersonaInvalidError(
            PERSONA_LLM_REFUSED.format(name=name, why=why),
            detail=f"[{LLM_TABLE}] refused: {why}",
        ) from exc


class Persona(BaseModel):
    """One loaded persona.

    ``voice_engine``/``voice_name`` are carried but not acted on here: spec
    section 5.5 keeps persona and voice selection independent, so a persona may
    *suggest* a voice while the voice subsystem (P1) decides what to do about
    it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    system_prompt: str
    prompt_prefix: str = ""
    """Text placed in front of the system message on every turn this persona
    speaks. Empty adds nothing. A default the persona's own prompt can
    override, never a constraint on it: see ``AgentLoop._system_prompt`` for
    where it sits and why. Set by an administrator on a screen, so it is
    configuration and is not fenced."""
    memory_enabled: bool = False
    """Whether this persona reads or writes memory at all (memory contract
    §9). Read from ``persona.toml``'s top-level ``memory`` key.

    **Absent means off** (workspace contract §13, E — flipped from this
    field's original default). A new persona gets no memory until somebody
    turns it on; a persona that already had ``memory = true`` or ``memory =
    false`` written down keeps reading exactly that value, because only the
    *absent* case changed. Anything other than a plain boolean also means
    off and never raises — the same rule ``prompt_prefix`` follows, for the
    same reason: a broken value costs the switch, not the persona."""
    workspace_enabled: bool = False
    """Whether this persona has a per-conversation workspace at all
    (workspace contract §0.1, §5). Read from ``persona.toml``'s top-level
    ``workspace`` key, exactly the way :attr:`memory_enabled` reads
    ``memory`` — absent or not a plain boolean both mean off: the owner's
    decision (contract §0.1) is that a persona gets no workspace until
    somebody turns it on. (``memory_enabled`` shipped with the opposite
    default and was later flipped to match — contract §13, E.)"""
    thinking_enabled: bool = True
    """Whether this persona reasons before it answers (workspace contract
    §13, D). Read from ``persona.toml``'s top-level ``thinking`` key.

    **Absent means on** — the opposite default from :attr:`workspace_enabled`
    and the same one :attr:`memory_enabled` had before contract §13, E: a
    persona that predates this key keeps thinking exactly as it always did.
    Anything other than a plain boolean also means on and never raises, the
    same permissive-read rule every switch on this class follows.

    A conversation may override this per thread
    (``personacore.conversations.models.Conversation.thinking``); the
    persona's own value is what a thread that has never chosen otherwise
    gets, and what every round after a tool result falls back to regardless
    of either setting (the owner's rule: thinking is off once a turn has
    already used a tool)."""
    description: str | None = None
    voice_engine: str | None = None
    voice_name: str | None = None
    speech_pauses: tuple[SpeechPause, ...] = ()
    """Words this persona says with pauses of their own, timed **on the way to
    a voice and nowhere else** — see the section above. The text is never
    changed by them. Already cleaned, capped and ordered longest-word-first by
    :func:`clean_pauses`, so whatever holds one of these can use it without
    checking it again."""
    connection: LLMSettings | None = None
    """This persona's own LLM connection, or ``None`` to use the system's.

    ``None`` and "the same values the system happens to have today" are
    deliberately different states — see the section above. Nothing here decides
    *how* a client is made from it: that is the roster's, so a persona sharing
    an endpoint with a role shares its connection pool and its circuit breaker
    rather than opening a second one that can disagree about whether that host
    is up."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    source_dir: Path


_Fingerprint = tuple[tuple[str, int, int], ...]


def _fingerprint(paths: tuple[Path, ...]) -> _Fingerprint:
    """(path, mtime_ns, size) for each file that exists.

    Size is included as well as mtime because an editor that writes twice
    within one filesystem timestamp tick is not exotic, and a persona that
    silently keeps serving the previous prompt is precisely the "edit this YAML
    and pray" experience spec section 4.4 exists to prevent.
    """
    entries: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


class PersonaStore:
    """Loads personas from ``<appdata>/personas/`` with hot-reload."""

    def __init__(
        self,
        layout: AppdataLayout,
        *,
        default_persona: str = DEFAULT_PERSONA_NAME,
    ) -> None:
        self._layout = layout
        self._default_persona = default_persona
        self._cache: dict[str, tuple[_Fingerprint, Persona]] = {}

    @property
    def default_persona(self) -> str:
        return self._default_persona

    def set_default(self, name: str) -> None:
        """Change which persona answers when a turn names none.

        Spec section 5.5 requires persona changes to take effect mid-session
        with no restart, so this is settable at runtime rather than fixed at
        construction. The name is not checked against the directory here: a
        persona whose files are missing fails loudly at load time with a message
        naming the file, which is more useful than refusing the setting and
        leaving the admin unable to select a persona they are about to install.
        """
        self._default_persona = name

    def available(self) -> list[str]:
        """Persona names on disk, sorted. Used by the admin UI picker (spec
        section 9); returns empty rather than raising if the directory is
        missing, because "no personas installed yet" is a first-run state, not
        an error."""
        try:
            entries = sorted(p.name for p in self._layout.personas.iterdir() if p.is_dir())
        except OSError:
            return []
        return [name for name in entries if _NAME_PATTERN.match(name)]

    def resolve_dir(self, name: str) -> Path:
        """The directory for a persona name, refusing anything that is not a
        plain name inside the personas directory."""
        if not _NAME_PATTERN.match(name):
            raise PersonaNotFoundError(
                f"I don't have a persona called {name!r}.",
                detail=f"persona name {name!r} is not a plain directory name",
            )
        candidate = self._layout.personas / name
        try:
            resolved = self._layout.require_inside(candidate, what=f"The persona {name!r}")
        except AppdataError as exc:
            raise PersonaNotFoundError(
                f"I don't have a persona called {name!r}.", detail=str(exc)
            ) from exc
        # A symlinked persona directory resolves elsewhere; require_inside has
        # already refused anything outside appdata, but the parent must still
        # be the personas directory itself and not some other appdata corner.
        if resolved.parent != self._layout.personas.resolve():
            raise PersonaNotFoundError(
                f"I don't have a persona called {name!r}.",
                detail=f"{resolved} is not directly inside {self._layout.personas}",
            )
        return resolved

    def load(self, name: str | None = None) -> Persona:
        """Load a persona by name, re-reading it if its files changed.

        ``None`` means "the configured default" — the same thing
        ``PolicyProfile.persona is None`` means (spec section 5.4).
        """
        persona_name = name or self._default_persona
        directory = self.resolve_dir(persona_name)
        if not directory.is_dir():
            raise PersonaNotFoundError(
                f"I don't have a persona called {persona_name!r}.",
                detail=f"{directory} does not exist",
            )

        watched = tuple(directory / filename for filename in PROMPT_FILENAMES) + (
            directory / METADATA_FILENAME,
        )
        fingerprint = _fingerprint(watched)
        cached = self._cache.get(persona_name)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]

        persona = self._read(persona_name, directory)
        self._cache[persona_name] = (fingerprint, persona)
        logger.info(
            "persona_loaded",
            persona=persona_name,
            reloaded=cached is not None,
            prompt_chars=len(persona.system_prompt),
        )
        return persona

    def invalidate(self) -> None:
        """Drop the cache. The admin UI's "reload" action (spec section 5.1)
        can call this; ordinary edits do not need it."""
        self._cache.clear()

    # -- reading ----------------------------------------------------------

    def _read(self, name: str, directory: Path) -> Persona:
        prompt_path = next(
            (directory / filename for filename in PROMPT_FILENAMES
             if (directory / filename).is_file()),
            None,
        )
        if prompt_path is None:
            raise PersonaInvalidError(
                f"The persona {name!r} has no prompt file, so I can't be it.",
                detail=(
                    f"expected one of {', '.join(PROMPT_FILENAMES)} in {directory}"
                ),
            )
        try:
            system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PersonaInvalidError(
                f"I couldn't read the persona {name!r}.", detail=repr(exc)
            ) from exc
        if not system_prompt:
            raise PersonaInvalidError(
                f"The persona {name!r} has an empty prompt file, so I can't be it.",
                detail=f"{prompt_path} is empty",
            )

        metadata = self._read_metadata(name, directory / METADATA_FILENAME)
        voice = metadata.get("voice")
        voice_engine = voice.get("engine") if isinstance(voice, dict) else None
        voice_name = voice.get("name") if isinstance(voice, dict) else None
        display_name = metadata.get("display_name")
        description = metadata.get("description")
        raw_prefix = metadata.get("prompt_prefix")
        # A broken value costs the prefix, not the persona.
        prompt_prefix = raw_prefix.strip() if isinstance(raw_prefix, str) else ""
        raw_memory = metadata.get("memory")
        # Absent or broken both mean off (contract §13, E) — a persona that
        # predates this key, or whose value is not a plain bool, gets no
        # memory until somebody turns it on. Only the *absent* case changed
        # from this field's original default; an explicit `memory = true` or
        # `memory = false` already on disk keeps reading exactly that value.
        # Never raises.
        memory_enabled = raw_memory if isinstance(raw_memory, bool) else False
        raw_workspace = metadata.get("workspace")
        # Absent or broken both mean off (contract §0.1) — a persona gets no
        # workspace until somebody turns it on. Never raises.
        workspace_enabled = raw_workspace if isinstance(raw_workspace, bool) else False
        raw_thinking = metadata.get("thinking")
        # Absent or broken both mean on (contract §13, D) — a persona that
        # predates this key keeps thinking exactly as it always did. Never
        # raises.
        thinking_enabled = raw_thinking if isinstance(raw_thinking, bool) else True
        # Never raises, whatever is in the file: a persona whose pauses block
        # is broken loads without them and speaks exactly as it does today.
        pauses = read_pauses(metadata)
        # Unlike the pauses, this one raises when it is wrong: a persona that
        # answered from the system default while its own file named another
        # model would be lying about who was speaking.
        connection = read_connection(name, metadata)

        return Persona(
            name=name,
            display_name=str(display_name) if isinstance(display_name, str) else name,
            system_prompt=system_prompt,
            prompt_prefix=prompt_prefix,
            memory_enabled=memory_enabled,
            workspace_enabled=workspace_enabled,
            thinking_enabled=thinking_enabled,
            description=description if isinstance(description, str) else None,
            voice_engine=voice_engine if isinstance(voice_engine, str) else None,
            voice_name=voice_name if isinstance(voice_name, str) else None,
            speech_pauses=pauses,
            connection=connection,
            metadata=metadata,
            source_dir=directory,
        )

    def _read_metadata(self, name: str, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            # Spec section 9: plain-English errors. A broken metadata file is
            # an admin's typo, and it should say which file and why — but it
            # must not take the persona down, because the prompt (the part
            # that matters) is fine.
            raise PersonaInvalidError(
                f"The settings file for the persona {name!r} has a mistake in it.",
                detail=f"{path}: {exc}",
            ) from exc


__all__ = [
    "DEFAULT_PERSONA_NAME",
    "LLM_TABLE",
    "MAX_PAUSE_MS",
    "MAX_PAUSE_WORD_CHARS",
    "MAX_SPEECH_PAUSES",
    "METADATA_FILENAME",
    "PAUSES_KEY",
    "PAUSE_LINE_REFUSED",
    "PAUSE_NOT_A_NUMBER",
    "PAUSE_OUT_OF_RANGE",
    "PAUSE_TOO_MANY",
    "PAUSE_WORD_REQUIRED",
    "PAUSE_WORD_TOO_LONG",
    "PERSONA_LLM_INCOMPLETE",
    "PERSONA_LLM_NOT_A_TABLE",
    "PERSONA_LLM_REFUSED",
    "PROMPT_FILENAMES",
    "SPEECH_TABLE",
    "Persona",
    "PersonaStore",
    "SpeechPause",
    "clean_pauses",
    "parse_pause_lines",
    "pause_lines",
    "pause_table",
    "read_connection",
    "read_pauses",
]

STARTER_PERSONA = """You are PersonaCore, a household assistant.

Be direct and brief. Answer the question that was asked. Say plainly when you do
not know something rather than guessing.

When a tool can answer the question, use it and report what it actually said.
"""
"""The persona written on first run.

Deliberately says nothing about what the assistant *can* do. An earlier version
told the model there were no tools connected; when tools arrived it kept
refusing, because a persona is instructions and the model was obeying them. A
persona describes manner. Capability is the tool list, and the model can see
that for itself.
"""


def ensure_default_persona(layout: AppdataLayout) -> bool:
    """Write the starter persona if none exists. Returns whether it wrote one.

    A container never runs `init`, so without this the personas directory stays
    empty and the very first turn fails on a missing persona. Never overwrites:
    an existing persona is the operator's (spec section 7).
    """
    directory = layout.personas / DEFAULT_PERSONA_NAME
    prompt = directory / PROMPT_FILENAMES[0]
    if prompt.exists():
        return False
    directory.mkdir(parents=True, exist_ok=True)
    prompt.write_text(STARTER_PERSONA, encoding="utf-8")
    return True
