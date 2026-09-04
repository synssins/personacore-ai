"""Memory's settings — ``[memory]`` in ``core.toml``.

``working/contracts/memory.md`` section 9 names five knobs, shown on the Core
settings screen with defaults filled in rather than left blank: the quiet
interval before a conversation is reviewed, how many rows come back on a
recall, the recency half-life, the near-duplicate threshold the store uses
instead of inserting a second copy of the same fact, and how long an
unpromoted memory survives with no activity.

Per-persona on/off is a different switch and lives on the persona itself
(``persona.toml``'s ``memory`` key, read in
:mod:`personacore.agent.personas`) — this file is the household-wide shape
only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MemorySettings(BaseModel):
    """``[memory]`` — the shape every implementer builds to (contract §9, J1)."""

    model_config = ConfigDict(extra="forbid")

    quiet_minutes: int = Field(default=10, ge=1, le=1440)
    """How long a conversation has to sit with nothing new before the review
    pass reads it. Bounded at a day: a value more useful as "never" belongs
    to turning the review pass off, which is the persona switch, not a very
    large number here."""

    recall_limit: int = Field(default=8, ge=1, le=50)
    """How many memories come back on a single recall — the persona's own
    plus the household's long-term ones, combined."""

    half_life_days: float = Field(default=30.0, gt=0)
    """Recency half-life, in days, for the recall ranking in contract §6: a
    memory not used in this many days scores half of one used today."""

    duplicate_threshold: float = Field(default=0.92, gt=0, le=1)
    """Cosine similarity above which a new memory is treated as the same
    fact already held and touches that row instead of inserting a second
    one (contract §5.1)."""

    short_term_days: int = Field(default=60, ge=1)
    """A short-term memory with no activity for this many days is purged
    (contract §7). A promoted, long-term memory never expires and this bound
    does not apply to it."""

    recall_floor: float = Field(default=0.3, ge=0, le=1)
    """Minimum cosine similarity a candidate must clear to be recalled at
    all -- owner, 2026-09-04. Below this a match is noise, not a memory
    worth surfacing, and is dropped before ranking rather than merely
    scored low. `0` disables the floor: every candidate the vector search
    returns is ranked exactly as it always was."""


__all__ = ["MemorySettings"]
