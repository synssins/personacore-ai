"""The shape of one memory, and the vocabulary constants that name owner and
holder (contract §1, §3.1).

A single frozen dataclass, deliberately not a pydantic model: nothing here is
ever built from untrusted input (the store constructs it from its own rows or
its own inserts) and there is no validation to centralise -- see `store.py`
for the one place text length and truncation are decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Holder value for a long-term memory: available to every persona, for
#: every person. The only way in is `MemoryStore.promote`.
GLOBAL_HOLDER = "global"

#: Owner value a promoted memory carries. Long-term memory is about the
#: household, not any one person (contract §1).
HOUSEHOLD_OWNER = "household"

#: Owner value for the anonymous key scope (contract §5.1, §6).
ANONYMOUS_OWNER = "anonymous"

#: Contract §3.1: longer text is truncated at a sentence boundary and the
#: row is marked `truncated`.
MAX_TEXT_CHARS = 2000

#: `written_by` values (contract §3.1, §5).
WRITTEN_BY_TOOL = "tool"
WRITTEN_BY_REVIEW = "review"

#: `review_runs.outcome` values (the review log, schema version 3). One run
#: is recorded per `DueReview` regardless of what happened, so the owner can
#: see what the review pass found and what it threw away, without ever
#: showing memory text anywhere but this admin-only log.
REVIEW_OUTCOME_WRITTEN = "written"
REVIEW_OUTCOME_NOTHING = "nothing"
REVIEW_OUTCOME_PARSE_FAILED = "parse_failed"
REVIEW_OUTCOME_MODEL_FAILED = "model_failed"
REVIEW_OUTCOME_SKIPPED = "skipped"


@dataclass(frozen=True)
class MemoryRecord:
    """One row of `memories`, read back as a value rather than a `sqlite3.Row`.

    Field-for-field the shape fixed as joint J4 in `working/PLAN-memory.md`
    -- every implementer downstream (the provider, the tools, the review
    pass, the screen) builds against this, not against the table.

    `written_owner` (schema version 2) is `owner` as it stood at insert --
    set once by `MemoryStore.add` and never touched by `promote`, which
    changes `owner` itself to `household`. A row from a version-1 database
    reads back `''` here until it is next written.
    """

    memory_id: str
    text: str
    owner: str
    holder: str
    importance: float
    created_at: datetime
    last_used_at: datetime
    use_count: int
    written_by: str
    written_persona: str
    written_model: str
    written_owner: str
    conversation_id: str | None
    correlation_id: str
    edited_by: str | None
    edited_at: datetime | None
    promoted_by: str | None
    promoted_at: datetime | None
    truncated: bool
    last_score: float | None = None
    """The cosine similarity `recall` last matched this row against, or
    `None` when it has never been recalled (schema version 4, contract §6:
    "the score is visible"). Written only by `MemoryStore._recall`'s own
    touch -- the dedupe touch in `add` never sets it, so this is
    specifically "how well the last *recall* matched", not "how well the
    last write matched"."""


@dataclass(frozen=True)
class ReviewRunRecord:
    """One row of `review_runs` -- the review log the Memory screen reads
    (schema version 3). One of these is written per `DueReview` `tick()`
    attempts, whatever happened, so the owner can see what the review pass
    kept and what it rejected without memory text ever reaching structlog.

    `kept` is a JSON-shaped list of `{"text", "importance"}` for facts
    actually written or touched this run. `dropped_items` is a JSON-shaped
    list of `{"raw", "reason"}` for items the parser rejected -- `raw` is
    the whole reply, capped, when nothing about it parsed as a JSON list at
    all; otherwise it is the one rejected item. `model` and `error` are
    `None` when no model call was made at all (`outcome ==
    REVIEW_OUTCOME_SKIPPED`).
    """

    run_id: str
    conversation_id: str
    persona: str
    owner: str
    started_at: datetime
    finished_at: datetime
    model: str | None
    outcome: str
    written: int
    touched: int
    dropped: int
    kept: list[dict[str, Any]]
    dropped_items: list[dict[str, Any]]
    error: str | None
