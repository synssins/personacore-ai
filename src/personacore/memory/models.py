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


@dataclass(frozen=True)
class MemoryRecord:
    """One row of `memories`, read back as a value rather than a `sqlite3.Row`.

    Field-for-field the shape fixed as joint J4 in `working/PLAN-memory.md`
    -- every implementer downstream (the provider, the tools, the review
    pass, the screen) builds against this, not against the table.
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
    conversation_id: str | None
    correlation_id: str
    edited_by: str | None
    edited_at: datetime | None
    promoted_by: str | None
    promoted_at: datetime | None
    truncated: bool
