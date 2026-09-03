"""Conversations for everything said before conversations existed.

One pass, run from ``AuditStore.__init__``, that groups unattached transcript
rows by owner, surface and silence and writes a heading over each group. The
reasoning for grouping rather than lumping or leaving is in
:meth:`BackfillMixin.backfill_conversations`; the reasoning for it being a
separate module is smaller and older: **this is the part that is allowed to
fail.**

It is deliberately not a migration. A migration that raises takes the store
with it, and a store that will not construct takes ``create_app`` with it — the
lockout this project has produced three times. Migration 0002 is the part that
must succeed; this is the cosmetic regrouping of old rows, and
:meth:`BackfillMixin._backfill_on_open` swallows every exception it throws.

Safe to run repeatedly and bounded by :data:`BACKFILL_BATCH`, because startup
is not the place for an unbounded table scan.
"""

from __future__ import annotations

import sqlite3

from personacore.audit.logging import get_logger
from personacore.audit.models import MessageRole
from personacore.audit.store_base import StoreBase
from personacore.audit.store_rows import _conversations, _group_by_gap

_logger = get_logger(__name__)


BACKFILL_BATCH = 20_000
"""How many pre-conversation transcript rows one backfill pass will regroup.

Startup is not the place for an unbounded table scan, and a household's whole
transcript is not knowable in advance. The bound turns "the core takes an
unknown amount of time to open" into "the core opens, and the oldest few rows
join the list on the next restart", which is the right way round for a
cosmetic regrouping of history.
"""


class BackfillMixin(StoreBase):
    """Regrouping history, best-effort, at startup."""

    # -- what happened to everything written before conversations existed ---

    def backfill_conversations(self, *, batch: int = BACKFILL_BATCH) -> int:
        """Give every unattached transcript row a conversation, and return how
        many rows were claimed.

        **The decision, and why.** There is real transcript data in the running
        install. Three options were on the table: put all of it in one
        conversation, group it by a gap in time, or leave it unattached and
        reachable only through the log view. Grouping by a gap wins, and not
        by a small margin:

        * One conversation holding everything would be titled after the very
          first thing anybody ever said to the core and would then grow
          without end -- which is the *exact* problem this change exists to
          remove, preserved forever as row one of the list.
        * Leaving it unattached is not losing it -- it stays in the log view,
          attributed and searchable -- but it means the conversation list
          starts empty on a system with months of history in it, and "where
          did my conversations go" is a support question that looks identical
          to data loss.
        * Grouping by :data:`~personacore.conversations.models.SESSION_GAP`
          gives back roughly what actually happened. It is a reconstruction,
          and it is *labelled* as one
          (:attr:`~personacore.conversations.models.ConversationOrigin.BACKFILL`),
          so nobody has to wonder whether a surprising split is a bug.

        Grouping is per owner **and** per surface: a voice exchange and an
        admin-UI exchange minutes apart are two things happening, not one
        conversation, and merging them would put words in a thread they were
        never said in.

        **Not a migration, on purpose.** This runs after migrations, outside
        their transaction, and every failure is swallowed by
        :meth:`_backfill_on_open`. A migration that raises takes ``AuditStore``
        with it, and ``AuditStore`` failing to construct takes ``create_app``
        with it -- the lockout this project has produced three times. Deciding
        that a cosmetic regrouping of old rows was worth that risk would be
        indefensible, so the schema change (migration 0002) is the part that
        must succeed and this is the part that is allowed to give up.

        Safe to run repeatedly, and it has to be: it claims only rows with no
        conversation, so a second pass over an already-backfilled store claims
        nothing, and a pass interrupted halfway leaves committed groups alone
        and finishes the rest next time. ``batch`` bounds one pass so a very
        large transcript cannot stall startup; the remainder is picked up on
        the next open.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, ts_utc, owner_kind, owner_id, surface, role, content
                FROM transcript_records
                WHERE conversation_id IS NULL
                ORDER BY owner_kind, owner_id, surface, ts_utc, id
                LIMIT ?
                """,
                (batch,),
            ).fetchall()
            if not rows:
                return 0

            groups = _group_by_gap(rows)
            claimed = 0
            # Same explicit-transaction idiom as _run_migration_step, and for
            # the same reason: a crash partway through must leave the store
            # holding either whole conversations or none, never rows pointing
            # at a heading that was never written.
            previous_isolation_level = self._conn.isolation_level
            self._conn.isolation_level = None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for group in groups:
                        claimed += self._write_backfilled_group(group)
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                else:
                    self._conn.execute("COMMIT")
            finally:
                self._conn.isolation_level = previous_isolation_level

        _logger.info(
            "conversations_backfilled",
            conversations=len(groups),
            transcript_rows=claimed,
        )
        return claimed

    def _write_backfilled_group(self, group: list[sqlite3.Row]) -> int:
        """One reconstructed conversation, and its rows' links. The caller
        holds the lock and an open transaction."""
        first, last = group[0], group[-1]
        opening = next(
            (row["content"] for row in group if row["role"] == MessageRole.USER.value),
            None,
        )
        models = _conversations()
        conversation_id = models.new_conversation_id()
        self._conn.execute(
            """
            INSERT INTO conversations (
                conversation_id, owner_kind, owner_id, surface,
                title, started_at, last_activity_at, origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                first["owner_kind"],
                first["owner_id"],
                first["surface"],
                models.derive_title(opening),
                first["ts_utc"],
                last["ts_utc"],
                models.ConversationOrigin.BACKFILL.value,
            ),
        )
        ids = [row["id"] for row in group]
        placeholders = ",".join("?" for _ in ids)
        # No ``AND conversation_id IS NULL`` here: the SELECT that produced
        # these ids already filtered on it, and this runs under the same lock
        # and the same transaction, so nothing can have claimed them in
        # between. A second, unreachable guard would be code no test can tell
        # the truth about.
        cur = self._conn.execute(
            f"UPDATE transcript_records SET conversation_id = ? "  # noqa: S608
            f"WHERE id IN ({placeholders})",
            (conversation_id, *ids),
        )
        return cur.rowcount if cur.rowcount > 0 else 0

    def _backfill_on_open(self) -> None:
        """Run the backfill at startup, and never let it stop the core.

        Called from ``__init__``, which is on the path to ``create_app``. Every
        exception is logged and swallowed: a store that opens with old rows
        still unattached is a store where the conversation list is short and
        the log view is complete, which is a bad afternoon. A store that
        refuses to open is a household with no assistant and an operator with
        no admin UI to fix it from.
        """
        try:
            self.backfill_conversations()
        except Exception as exc:  # noqa: BLE001 - see docstring
            _logger.error("conversation_backfill_failed", error=repr(exc))
