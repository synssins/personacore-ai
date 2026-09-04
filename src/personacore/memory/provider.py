"""``CoreMemoryProvider`` — the read side of the memory seam, joint J5/organ C
of ``working/PLAN-memory.md``, built to ``working/contracts/memory.md`` §2, §6.

``agent/protocols.py``'s ``MemoryProvider`` is a plain ``Protocol``: `recall`,
one method. This is the only implementation in core — spec §6 called memory a
plugin, ADR-0045 overrides that (contract §2: the core has to be the writer of
owner and holder, and recall lands in the system message, which only the loop
composes). Everything this module does is turn a `MemoryRecallRequest` — the
caller's scope and profile, nothing more — into the one `owner`/`holder`/
`include_global` combination contract §6 allows, and hand `MemoryStore.recall`
that and nothing else.

**No text ever passes through here uninspected and nothing here logs it** —
this module does not import a logger at all, matching `store.py`'s own rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from personacore.agent.protocols import MemoryItem, MemoryRecallRequest
from personacore.contracts import MemoryScope
from personacore.memory.models import ANONYMOUS_OWNER


class _StoreLike(Protocol):
    """The one method `CoreMemoryProvider` calls on `MemoryStore` — a
    `Protocol` rather than the concrete import so a test can hand this a
    fake store with no database behind it at all, the same reason
    `store.py` names `EmbedderLike`/`SettingsLike` instead of importing."""

    async def recall(
        self,
        *,
        owner: str,
        holder: str,
        query: str,
        limit: int,
        include_global: bool = True,
    ) -> Sequence[tuple[object, float]]: ...


class CoreMemoryProvider:
    """`MemoryProvider` (`agent/protocols.py`) over one `MemoryStore`.

    `settings` is accepted for the same shape every other memory component
    takes it in — the store already owns the settings it needs for ranking
    and dedupe — and is kept here unused rather than dropped from the
    constructor, so this class's shape does not have to change if a later
    version reads a setting of its own (contract §9's settings screen is the
    kind of thing that could add one).
    """

    def __init__(self, store: _StoreLike, settings: object) -> None:
        self._store = store
        self._settings = settings

    async def recall(self, request: MemoryRecallRequest) -> Sequence[MemoryItem]:
        """Contract §6: the filter is entirely in who gets asked for what.

        `request.persona is None` — a raw-passthrough turn, contract §6 —
        recalls nothing: there is no holder to ask the store for. Otherwise
        the scope decides owner and whether the household's long-term store
        is included, exactly as `agent/loop.py._memory_block` already
        enforces the scope-is-``none`` half of this on the way in:

        * ``ANONYMOUS`` — the anonymous owner only, no global (contract §6:
          "Anonymous scope reads the anonymous owner only, as today").
        * ``USER`` / ``HOUSEHOLD`` — this profile's own memories plus the
          household's long-term ones.
        """
        if request.persona is None:
            return []
        if request.scope is MemoryScope.ANONYMOUS:
            owner = ANONYMOUS_OWNER
            include_global = False
        else:
            owner = request.profile_id
            include_global = True
        rows = await self._store.recall(
            owner=owner,
            holder=request.persona,
            query=request.query,
            limit=request.limit,
            include_global=include_global,
        )
        return [
            MemoryItem(
                text=record.text,
                source="memory",
                score=score,
                holder=record.holder,
                memory_id=record.memory_id,
            )
            for record, score in rows
        ]


__all__ = ["CoreMemoryProvider"]
