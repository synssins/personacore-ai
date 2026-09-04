"""The review pass -- contract §5.2, `working/PLAN-memory.md` Task 5.

Two pieces, deliberately kept apart:

* :class:`QuietConversationFinder` -- finds conversations that have gone
  quiet and have something unreviewed in them, one entry per persona that
  spoke. **Nothing in this class is memory-specific** beyond the ``marks``
  callback `due()` is handed: it reads the audit store's transcript, groups
  it by conversation, and hands back the new part plus who said it. The
  compaction pass (`working/contracts/compaction.md`, unbuilt) needs exactly
  this same "read the new part of a quiet conversation" mechanism and can
  reuse this class outright -- give it a different `marks` callback (its own
  mark table) and it owes memory nothing.
* :class:`ReviewRunner` -- the memory-specific part. Turns each
  :class:`DueReview` into one call to the triage role, parses what comes
  back, and writes through :class:`~personacore.memory.store.MemoryStore`.

Both run on a background timer (`run_review_ticker`, wired into `server.py`
by the task that owns that file), never on the interactive model, and never
touching a persona whose memory switch is off beyond skipping it.

**Never logs memory text or transcript text.** Every log line below carries
ids, owners, holders, counts -- the same rule `memory/store.py` follows, for
the same reason.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

import structlog

from personacore.agent.errors import PersonaError
from personacore.agent.personas import PersonaStore
from personacore.agent.untrusted import UntrustedKind, wrap_untrusted
from personacore.audit.models import AuthorKind, Owner, Surface, TranscriptRecord
from personacore.boot.llm import LiveLLM
from personacore.config.memory import MemorySettings
from personacore.memory.models import MAX_TEXT_CHARS, WRITTEN_BY_REVIEW
from personacore.memory.review_prompt import REVIEW_SYSTEM_PROMPT
from personacore.memory.store import MemoryStore

log = structlog.get_logger(__name__)

#: How far back the finder looks for activity at all, bounding what would
#: otherwise be an unbounded scan of `transcript_records` (contract §5.2
#: says nothing about a horizon; this is this module's own choice -- see
#: `QuietConversationFinder`'s docstring for what it costs).
DEFAULT_LOOKBACK_HOURS = 48

#: A generous ceiling on how many rows one `due()` call reads back. A
#: household's real message volume in `DEFAULT_LOOKBACK_HOURS` is nowhere
#: near this; it exists so one call cannot grow unbounded.
_MAX_ROWS_PER_SCAN = 5000

#: Cap on the fenced transcript handed to the triage role (contract §5.2
#: names 12 items out but says nothing about the input size; this is one
#: call on a model that is not the conversation slot, so a generous but
#: finite cap protects it the same way `agent/untrusted.py`'s own default
#: protects every other fenced block).
TRANSCRIPT_MAX_CHARS = 24_000

_IMPORTANCE_WORDS: dict[str, float] = {"low": 0.3, "medium": 0.6, "high": 0.9}

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


# --------------------------------------------------------------------------
# QuietConversationFinder -- nothing memory-specific below this line except
# the `marks` callback shape `due()` takes as a parameter, not a dependency.
# --------------------------------------------------------------------------


class TranscriptSource(Protocol):
    """The one method this module reads off the audit store.

    A `Protocol` rather than importing `personacore.audit.store.AuditStore`
    directly: this module has no other reason to know that store exists as a
    class, and typing against the one call it makes is what lets compaction
    hand this the same store object without this file importing anything
    compaction would not also need.
    """

    async def query_transcript(
        self,
        *,
        owner: Owner | None = None,
        surface: Surface | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[TranscriptRecord]: ...


class PersonaNameSource(Protocol):
    """The one method the finder reads off `PersonaStore`: the names that
    currently resolve to a real persona directory. Used only to keep a
    persona deleted after it spoke from turning into a review for a name
    that no longer loads -- see `due()`."""

    def available(self) -> list[str]: ...


@dataclass(frozen=True)
class DueReview:
    """One persona's unreviewed part of one quiet conversation."""

    conversation_id: str
    owner: str
    persona: str
    rows: list[TranscriptRecord]
    mark: str
    """Where this review leaves off, opaque to everything but this module and
    `MemoryStore.review_mark`/`set_review_mark`. Two shapes, self-describing
    by prefix so a reader never has to guess which: ``"id:<n>"`` when the
    newest row in view carries a database id (every row read back from a
    real, persisted store does), else ``"ts:<iso timestamp>"``. Contract
    §5.2 calls the mark "the id of the last message reviewed"; a database id
    is a truer match for that than a timestamp is (two rows can share a
    timestamp; they cannot share an id), so it is preferred whenever it is
    available.
    """


def _owner_string(owner: Owner) -> str:
    """`Owner.id` already carries the sentinel this module's caller wants:
    `Owner.household().id == "household"` and `Owner.anonymous().id ==
    "anonymous"` are the same strings `memory.models.HOUSEHOLD_OWNER` and
    `memory.models.ANONYMOUS_OWNER` name (verified against `audit/models.py`
    `_OWNER_SENTINELS`), and a profile owner's `id` is its profile id
    already. So there is nothing to translate -- this function exists only
    to say, in one place, that the two vocabularies were checked to agree,
    rather than have every call site assume it silently.
    """
    return owner.id


def _mark_for(record: TranscriptRecord) -> str:
    if record.id is not None:
        return f"id:{record.id}"
    return f"ts:{record.timestamp.isoformat()}"


def _is_newer_than_mark(record: TranscriptRecord, mark: str | None) -> bool:
    """Whether `record` is part of what a review at `mark` has not seen yet.

    `mark is None` means this (conversation, persona) has never been
    reviewed -- everything in view is new. An id-shaped mark compares by id
    only when `record` itself carries one; a mark this module never wrote
    (or an id-shaped mark against an id-less record -- never produced by a
    persisted store, but a fake test double could) falls back to treating
    the row as new rather than silently dropping it, because a review that
    runs twice on the same fact is a duplicate the store's own near-duplicate
    check absorbs (contract §5.1), while one that skips a fact loses it for
    good.
    """
    if mark is None:
        return True
    if mark.startswith("id:") and record.id is not None:
        return record.id > int(mark[3:])
    if mark.startswith("ts:"):
        return record.timestamp > datetime.fromisoformat(mark[3:])
    return True


class QuietConversationFinder:
    """Conversations that have gone quiet, with something unreviewed in them.

    Reads `TranscriptSource.query_transcript` directly rather than going
    through `AuditStore.list_conversations` per owner: `query_transcript`
    takes no `owner` at all (`audit/store_records.py:189`), so one bounded,
    ownerless scan since `DEFAULT_LOOKBACK_HOURS` finds every owner's
    activity in one call. `list_conversations` needs an `Owner` and this
    module has no source of "every owner this household has" to iterate
    over -- there is no such registry today (checked: no `ProfileStore`, no
    method on any store that lists distinct owners). Iterating conversations
    per owner was the plan's anticipated fallback for a gap in the audit
    store; there turned out to be a direct route around the gap instead, so
    that fallback was not needed.

    The cost of the direct route is `DEFAULT_LOOKBACK_HOURS`: a conversation
    that fell quiet and was never reviewed before that horizon (only
    reachable if the review pass was off, or this core was down, for longer
    than the horizon) will not surface until something new is said in it.
    Contract §5.2 does not name a horizon; this is this module's own,
    documented choice, not a contract requirement.
    """

    def __init__(
        self,
        audit_store: TranscriptSource,
        personas: PersonaNameSource,
        *,
        quiet_minutes_provider: Callable[[], int],
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    ) -> None:
        self._audit_store = audit_store
        self._personas = personas
        self._quiet_minutes_provider = quiet_minutes_provider
        self._lookback_hours = lookback_hours

    async def due(
        self,
        now: datetime,
        marks: Callable[[str, str], Awaitable[str | None]],
    ) -> list[DueReview]:
        """Every (conversation, persona) pair ready for review at `now`.

        `marks(conversation_id, persona)` is the caller's own "what did I
        last review" lookup -- `MemoryStore.review_mark` in production,
        anything shaped the same in a test or in compaction. This class
        never reads or writes a mark table itself.
        """
        quiet_minutes = self._quiet_minutes_provider()
        since = now - timedelta(hours=self._lookback_hours)
        rows = await self._audit_store.query_transcript(since=since, limit=_MAX_ROWS_PER_SCAN)

        by_conversation: dict[str, list[TranscriptRecord]] = {}
        for row in rows:
            if row.conversation_id is None:
                continue
            by_conversation.setdefault(row.conversation_id, []).append(row)

        available_personas = set(self._personas.available())
        due: list[DueReview] = []

        for conversation_id, convo_rows in by_conversation.items():
            convo_rows.sort(key=lambda r: (r.timestamp, r.id or 0))
            latest = convo_rows[-1]
            if now - latest.timestamp < timedelta(minutes=quiet_minutes):
                continue  # still active -- not quiet yet

            spoken_personas = sorted(
                {
                    row.author.name
                    for row in convo_rows
                    if row.author is not None
                    and row.author.kind is AuthorKind.PERSONA
                    and row.author.name in available_personas
                }
            )
            if not spoken_personas:
                continue

            mark_value = _mark_for(latest)
            owner_string = _owner_string(latest.owner)
            for persona in spoken_personas:
                existing_mark = await marks(conversation_id, persona)
                new_rows = [r for r in convo_rows if _is_newer_than_mark(r, existing_mark)]
                if not new_rows:
                    continue
                due.append(
                    DueReview(
                        conversation_id=conversation_id,
                        owner=owner_string,
                        persona=persona,
                        rows=new_rows,
                        mark=mark_value,
                    )
                )
        return due


# --------------------------------------------------------------------------
# ReviewRunner -- the memory-specific part.
# --------------------------------------------------------------------------


@dataclass
class ReviewStats:
    reviewed: int = 0
    """Due reviews the runner attempted this tick, whatever the outcome."""
    written: int = 0
    touched: int = 0
    dropped: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class _ParsedFact:
    text: str
    importance: float


def _strip_fence(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    return match.group(1) if match else text.strip()


def _parse_facts(text: str | None) -> list[_ParsedFact] | None:
    """Strict parse of the triage role's reply (contract §5.2).

    `None` means the whole reply is unusable and must be dropped and
    counted, never partially trusted: one malformed item is as much a
    reason to distrust the batch as no JSON at all, because a model that
    got the shape wrong on one item has given no reason to believe the
    others are what they claim to be either.
    """
    if not text:
        return None
    try:
        data = json.loads(_strip_fence(text))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    facts: list[_ParsedFact] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        raw_text = item.get("text")
        raw_importance = item.get("importance")
        if not isinstance(raw_text, str):
            return None
        cleaned = raw_text.strip()
        if not cleaned or len(cleaned) > MAX_TEXT_CHARS:
            return None
        if raw_importance not in _IMPORTANCE_WORDS:
            return None
        facts.append(_ParsedFact(text=cleaned, importance=_IMPORTANCE_WORDS[raw_importance]))
    return facts[:12]


def _build_transcript(rows: Sequence[TranscriptRecord]) -> str:
    lines = []
    for row in sorted(rows, key=lambda r: (r.timestamp, r.id or 0)):
        name = row.author.name if row.author is not None else row.role.value
        lines.append(f"{name}: {row.content}")
    return "\n".join(lines)


def triage_is_its_own(triage: Any) -> bool:
    """Whether the triage handle is a connection of its own, ready to use.

    The roster gives every role a handle, and a role nobody configured falls
    back to ``interactive`` (``LiveLLM.falls_back_to`` names the role it
    borrowed). Contract §5.2 says the review pass never runs on the
    interactive model, so a fallen-back triage is treated as absent, exactly
    like one with nothing usable behind it (``.unusable``).
    """
    if getattr(triage, "unusable", None) is not None:
        return False
    return getattr(triage, "falls_back_to", None) is None


class ReviewRunner:
    """Runs the due reviews one tick finds, against the triage role.

    `triage` is `None` (never wired) or a `LiveLLM` whose `.unusable` is set
    (the triage role's key was never supplied -- `boot/llm.py`'s
    `LLMRoster.for_role` always returns a handle, never `None`; a role with
    no usable connection announces itself through `.unusable`, not through
    the handle's absence). Both are the same state from here: `tick()`
    returns an all-zero `ReviewStats` and logs nothing, because the
    boot-time warning belongs to whoever wires this into `server.py`, not to
    every tick after it.
    """

    def __init__(
        self,
        store: MemoryStore,
        finder: QuietConversationFinder,
        triage: LiveLLM | None,
        personas: PersonaStore,
        settings: MemorySettings,
    ) -> None:
        self._store = store
        self._finder = finder
        self._triage = triage
        self._personas = personas
        self._settings = settings

    async def tick(self, now: datetime | None = None) -> ReviewStats:
        # See ``triage_is_its_own`` for why a fallen-back role does not count.
        stats = ReviewStats()
        if self._triage is None or not triage_is_its_own(self._triage):
            return stats

        moment = now or datetime.now(UTC)
        due = await self._finder.due(moment, self._store.review_mark)

        for item in due:
            stats.reviewed += 1
            try:
                persona = self._personas.load(item.persona)
            except PersonaError:
                # Deleted between the finder's scan and this tick, or newly
                # broken. Mark it anyway -- there is nothing to retry that a
                # retry would fix -- and move on without touching the store.
                await self._store.set_review_mark(item.conversation_id, item.persona, item.mark)
                stats.skipped += 1
                continue

            if not persona.memory_enabled:
                # Still set the mark: a persona left off must not be
                # re-examined every tick for the rest of the conversation's
                # life (contract §9: memory off means no review pass).
                await self._store.set_review_mark(item.conversation_id, item.persona, item.mark)
                stats.skipped += 1
                continue

            await self._review_one(item, stats)

        return stats

    async def _review_one(self, item: DueReview, stats: ReviewStats) -> None:
        transcript = _build_transcript(item.rows)
        fenced = wrap_untrusted(
            transcript,
            kind=UntrustedKind.REVIEW_TRANSCRIPT,
            source="a conversation being reviewed for memories",
            token=secrets.token_hex(8),
            max_content_chars=TRANSCRIPT_MAX_CHARS,
        )
        messages = [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": fenced},
        ]

        try:
            response = await self._triage.chat_completion(messages)
        except Exception as exc:  # noqa: BLE001 - a review failure must never take the service down
            # Mark deliberately left unset: the model never answered, so
            # this due review is retried next tick rather than skipped for
            # good. Never the text -- see the module docstring.
            log.warning("memory_review_failed", error=repr(exc))
            return

        content = None
        if response.choices:
            content = response.choices[0].message.content
        facts = _parse_facts(content)

        if facts is None:
            # The model DID answer, just not usably -- unlike a transport
            # failure, retrying next tick would only see the same quiet
            # conversation and likely get the same answer, so the mark is
            # set and this due review is not retried.
            await self._store.set_review_mark(item.conversation_id, item.persona, item.mark)
            stats.dropped += 1
            return

        model_name = self._triage.current.config.model
        for fact in facts:
            _, created = await self._store.add(
                text=fact.text,
                owner=item.owner,
                holder=item.persona,
                written_by=WRITTEN_BY_REVIEW,
                written_persona=item.persona,
                written_model=model_name,
                conversation_id=item.conversation_id,
                correlation_id=f"review:{uuid4()}",
                importance=fact.importance,
            )
            if created:
                stats.written += 1
            else:
                stats.touched += 1

        await self._store.set_review_mark(item.conversation_id, item.persona, item.mark)


# --------------------------------------------------------------------------
# The ticker -- started as a background task by whoever wires server.py.
# --------------------------------------------------------------------------


async def run_review_ticker(
    runner: ReviewRunner,
    *,
    interval_seconds: int = 60,
    status: dict[str, Any],
) -> None:
    """Sleep, tick, record the outcome, forever -- the same shape
    `server.py`'s `_retention_purge_loop` runs the retention purge on. One
    tick failing must never end this loop, the same reasoning as the
    retention purge's own try/except: this is background hygiene, not a
    request, and an exception nobody retrieves is reported as an error
    nobody can act on.

    `status` is mutated in place -- the caller owns the dict (typically
    `app.state.memory_review_status`) so `/health` can read it without this
    function knowing anything about FastAPI.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            stats = await runner.tick()
        except Exception as exc:  # noqa: BLE001 - background hygiene, never the service
            status["last_error"] = repr(exc)
            log.warning("memory_review_tick_failed", error=repr(exc))
            continue
        status["last_success"] = datetime.now(UTC).isoformat()
        status["last_error"] = None
        status["reviewed"] = stats.reviewed
        status["written"] = stats.written
        status["touched"] = stats.touched
        status["dropped"] = stats.dropped
        status["skipped"] = stats.skipped
        log.info(
            "memory_review_completed",
            reviewed=stats.reviewed,
            written=stats.written,
            touched=stats.touched,
            dropped=stats.dropped,
            skipped=stats.skipped,
        )


__all__ = [
    "DEFAULT_LOOKBACK_HOURS",
    "TRANSCRIPT_MAX_CHARS",
    "DueReview",
    "PersonaNameSource",
    "QuietConversationFinder",
    "ReviewRunner",
    "ReviewStats",
    "TranscriptSource",
    "run_review_ticker",
]
