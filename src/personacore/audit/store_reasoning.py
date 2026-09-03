"""Reasoning rows: one write per reply, one owner-scoped read for a thread.

Migration 0007's docstring is the authority on why this is its own table
instead of a column on ``audit_records`` or ``transcript_records`` — its
size, and the fact that it is conversation content and not action metadata.
This module is the read/write half of that decision, in the same shape
:mod:`personacore.audit.store_attachments` already uses for the same kind of
table: a plain insert, an owner-scoped read, and nothing here that knows about
a request, a template, or a store method that is not its own.

**Ageing out mirrors ``audit_records`` exactly, not ``attachments``.** A
reply's reasoning and its transcript row are written moments apart in the
same request and share one timestamp, so the per-surface cutoff
:meth:`~personacore.audit.store_retention.RetentionMixin.purge_older_than`
already applies to both record families applies to this table by the same two
columns, rather than reading age off ``transcript_records`` by ``NOT EXISTS``
the way :mod:`personacore.audit.store_attachments` does for a file that can
legitimately outlive the message that first carried it.

Privacy: only correlation ids, surfaces and a text *length* are ever logged
here — never the reasoning itself, which is conversation content under this
package's rule (see :mod:`personacore.audit.logging`'s own module docstring).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

from personacore.audit.logging import get_logger
from personacore.audit.models import Owner, OwnerKind, ReasoningRecord, Surface
from personacore.audit.store_base import StoreBase
from personacore.audit.store_rows import _iso, _parse_iso, _tolerant

_logger = get_logger(__name__)


def _row_to_reasoning(row: sqlite3.Row) -> ReasoningRecord:
    return ReasoningRecord(
        id=row["id"],
        correlation_id=row["correlation_id"],
        timestamp=_parse_iso(row["ts_utc"]),
        surface=Surface(row["surface"]),
        owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
        text=row["reasoning"],
    )


class ReasoningMixin(StoreBase):
    """The ``reply_reasoning`` table: one writer, one thread-bounded read."""

    async def record_reasoning(self, record: ReasoningRecord) -> ReasoningRecord:
        """Keep one reply's reasoning, filed under its own correlation id.

        Called only when there is something to keep — rule 2 of the feature
        this exists for is that an ordinary reply, the one with no reasoning
        at all, writes nothing here and renders no line. The caller decides
        that; this module writes whatever it is handed.
        """
        return await asyncio.to_thread(self._insert_reasoning, record)

    def _insert_reasoning(self, record: ReasoningRecord) -> ReasoningRecord:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO reply_reasoning (
                    correlation_id, ts_utc, surface, owner_kind, owner_id, reasoning
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.correlation_id,
                    _iso(record.timestamp),
                    record.surface.value,
                    record.owner.kind.value,
                    record.owner.id,
                    record.text,
                ),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        # The text itself is never logged — only its length, which is
        # diagnostic (a truncated write looks nothing like a healthy one) and
        # carries none of what was actually thought.
        _logger.info(
            "reasoning_stored",
            id=row_id,
            correlation_id=record.correlation_id,
            surface=record.surface.value,
            chars=len(record.text),
        )
        return record.model_copy(update={"id": row_id})

    async def list_reasoning(
        self,
        *,
        owner: Owner,
        surface: Surface,
        since: datetime,
        until: datetime,
        limit: int = 400,
    ) -> list[ReasoningRecord]:
        """This owner's reasoning rows in one thread's own span.

        Bounded to ``[since, until]`` rather than pulled with the rest of the
        operator's history — the same reasoning ``chat.py``'s own
        ``_turn_audit`` already gives for a thread's tool calls: a
        correlation id belongs to exactly one exchange, and its reasoning —
        when there is any — was written between the same two timestamps that
        bound the thread.

        A read that fails costs the replayed thinking line only, the same
        degradation every other read this screen makes chooses: a reply with
        no reasoning shown here reads exactly like one the model produced
        none for.
        """
        return await asyncio.to_thread(self._list_reasoning, owner, surface, since, until, limit)

    def _list_reasoning(
        self,
        owner: Owner,
        surface: Surface,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> list[ReasoningRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM reply_reasoning
                WHERE owner_kind = ? AND owner_id = ? AND surface = ?
                  AND ts_utc >= ? AND ts_utc <= ?
                ORDER BY ts_utc DESC
                LIMIT ?
                """,
                (owner.kind.value, owner.id, surface.value, _iso(since), _iso(until), limit),
            ).fetchall()
        return _tolerant(rows, _row_to_reasoning, what="reasoning")
