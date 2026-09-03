"""Ageing records out — ADR-0004.

One pass over both record families and the conversation headings above them,
per-surface windows, in a single transaction under the store's lock. Three
properties are the whole of this module and each of them was a defect first:

* **A row whose surface this build does not recognise is still purged**, on the
  default window. Without the catch-all it matches none of the per-surface
  DELETEs and is kept forever — a silent retention failure on the most
  privacy-sensitive table in the system.
* **Hidden conversations are swept on the same schedule as visible ones.**
  There is no ``hidden_at IS NULL`` anywhere below and there must never be one:
  hiding is a review window, not an archive, and a clause here would make
  hidden conversations immortal.
* **A purge that finds the store closed reports nothing deleted rather than
  raising.** It runs unattended on a timer, and an exception here reads as
  "retention is broken" on the Health screen.

Deliberately not here: when a purge runs. That is the server's timer; this only
knows how to do one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from personacore.audit.logging import get_logger
from personacore.audit.models import PurgeResult, Surface
from personacore.audit.store_base import StoreBase
from personacore.audit.store_rows import _iso

_logger = get_logger(__name__)


class RetentionMixin(StoreBase):
    """The age-out pass, and the empty headings it leaves behind."""

    # -- retention --------------------------------------------------------

    async def purge_older_than(self, *, now: datetime | None = None) -> PurgeResult:
        """Age-out per ADR-0004: each surface has its own retention window
        (personacore.audit.models.RetentionConfig), defaulting to 30 days.
        Applied to both record families, since both are attributed by surface
        and both are, per ADR-0004, "the highest-value data in appdata from a
        privacy standpoint" once they hold content.

        A row whose `surface` is not a current `Surface` member is still
        purged, on the default retention window, rather than kept forever --
        see `_purge_older_than`. `now`, like every stored timestamp, must be
        timezone-aware; a naive value raises `ValueError` instead of being
        silently reinterpreted as local time.
        """
        return await asyncio.to_thread(self._purge_older_than, now)

    def _purge_older_than(self, now: datetime | None) -> PurgeResult:
        # `now` is the retention cutoff's reference point -- exactly as
        # retention-critical as every stored timestamp, which is validated
        # tz-aware (personacore.audit.models._require_tz). A naive `now`
        # would otherwise be silently reinterpreted as local wall-clock time
        # by `.astimezone(UTC)` below, shifting every cutoff by the host's
        # UTC offset. Reject it the same way record timestamps are rejected.
        if now is not None and now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        reference = (now or datetime.now(UTC)).astimezone(UTC)
        audit_deleted = 0
        transcript_deleted = 0
        reasoning_deleted = 0
        known_surfaces = [surface.value for surface in Surface]

        with self._lock:
            if self._closed:
                # The store was shut while this pass sat behind the lock. That
                # is the ordinary end of a purge on a timer, not a failure, so
                # it reports nothing deleted rather than raising -- an
                # exception here reads as "retention is broken" on /health.
                return PurgeResult(
                    audit_deleted=0,
                    transcript_deleted=0,
                    conversations_deleted=0,
                    reasoning_deleted=0,
                )
            for surface in Surface:
                cutoff = reference - timedelta(days=self._config.retention.days_for(surface))
                cutoff_iso = _iso(cutoff)

                cur = self._conn.execute(
                    "DELETE FROM audit_records WHERE surface = ? AND ts_utc < ?",
                    (surface.value, cutoff_iso),
                )
                audit_deleted += cur.rowcount if cur.rowcount > 0 else 0

                cur = self._conn.execute(
                    "DELETE FROM transcript_records WHERE surface = ? AND ts_utc < ?",
                    (surface.value, cutoff_iso),
                )
                transcript_deleted += cur.rowcount if cur.rowcount > 0 else 0

                # `reply_reasoning` copies `surface`/`ts_utc` from the reply's
                # own transcript row rather than reading age off it the way
                # `attachments` does (migration 0007's own docstring) -- so it
                # ages out on this same per-surface cutoff, the one both
                # deletes above already use.
                cur = self._conn.execute(
                    "DELETE FROM reply_reasoning WHERE surface = ? AND ts_utc < ?",
                    (surface.value, cutoff_iso),
                )
                reasoning_deleted += cur.rowcount if cur.rowcount > 0 else 0

            # Catch-all for rows whose `surface` column is not a current
            # `Surface` member -- schema drift, an older/newer code version,
            # or manual data repair. Without this, such a row matches none of
            # the per-surface DELETEs above and is retained forever regardless
            # of age: a silent retention failure on the most privacy-sensitive
            # table in the system (ADR-0004). An unrecognised surface is not
            # exempt from purge; it gets the default retention window.
            default_cutoff_iso = _iso(
                reference - timedelta(days=self._config.retention.default_days)
            )
            placeholders = ",".join("?" for _ in known_surfaces)

            cur = self._conn.execute(
                f"DELETE FROM audit_records WHERE surface NOT IN ({placeholders}) "  # noqa: S608
                "AND ts_utc < ?",
                (*known_surfaces, default_cutoff_iso),
            )
            audit_deleted += cur.rowcount if cur.rowcount > 0 else 0

            cur = self._conn.execute(
                f"DELETE FROM transcript_records WHERE surface NOT IN ({placeholders}) "  # noqa: S608
                "AND ts_utc < ?",
                (*known_surfaces, default_cutoff_iso),
            )
            transcript_deleted += cur.rowcount if cur.rowcount > 0 else 0

            cur = self._conn.execute(
                f"DELETE FROM reply_reasoning WHERE surface NOT IN ({placeholders}) "  # noqa: S608
                "AND ts_utc < ?",
                (*known_surfaces, default_cutoff_iso),
            )
            reasoning_deleted += cur.rowcount if cur.rowcount > 0 else 0

            conversations_deleted = self._reap_empty_conversations(reference)

            self._conn.commit()

        _logger.info(
            "audit_store_purged",
            audit_deleted=audit_deleted,
            transcript_deleted=transcript_deleted,
            conversations_deleted=conversations_deleted,
            reasoning_deleted=reasoning_deleted,
        )
        return PurgeResult(
            audit_deleted=audit_deleted,
            transcript_deleted=transcript_deleted,
            reasoning_deleted=reasoning_deleted,
            conversations_deleted=conversations_deleted,
        )

    def _reap_empty_conversations(self, reference: datetime) -> int:
        """Remove conversation headings that no longer head anything.

        Run in the same pass, and the same transaction, as the deletes that
        emptied them: a conversation is a heading over transcript rows and
        holds no message of its own, so when the last row it pointed at ages
        out there is nothing left for it to be a heading of. Left in place it
        becomes a row in somebody's conversation list that opens onto an empty
        screen -- the seam that looks fine right up until the first person's
        history reaches the end of its retention window.

        **Hidden conversations are swept on exactly the same schedule.** There
        is no ``hidden_at IS NULL`` in either DELETE below, and there must
        never be one: hiding takes a conversation off its owner's list and
        changes nothing about how long it is kept. A clause here would make
        hidden conversations immortal, which is the precise opposite of what
        hiding is for -- it is a review window, not an archive. The transcript
        rows go the same way for the same reason: the purge above matches on
        surface and age and has never known what a conversation is.

        **Age is still respected.** A heading is only reaped once it is itself
        past the retention window for its surface -- so a conversation opened
        thirty seconds ago and not yet spoken in, which is legitimately empty,
        survives. Reaping every empty conversation unconditionally would
        delete the conversation an operator is *sitting in* between opening
        the screen and sending the first message.

        The caller holds the lock and commits.
        """
        deleted = 0
        for surface in Surface:
            cutoff = reference - timedelta(days=self._config.retention.days_for(surface))
            cur = self._conn.execute(
                """
                DELETE FROM conversations
                WHERE surface = ? AND last_activity_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM transcript_records t
                      WHERE t.conversation_id = conversations.conversation_id
                  )
                """,
                (surface.value, _iso(cutoff)),
            )
            deleted += cur.rowcount if cur.rowcount > 0 else 0

        # Same catch-all as the record families above: a heading whose surface
        # this build does not recognise is not exempt from the sweep, it just
        # gets the default window.
        known_surfaces = [surface.value for surface in Surface]
        placeholders = ",".join("?" for _ in known_surfaces)
        default_cutoff_iso = _iso(
            reference - timedelta(days=self._config.retention.default_days)
        )
        cur = self._conn.execute(
            f"""
            DELETE FROM conversations
            WHERE surface NOT IN ({placeholders}) AND last_activity_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM transcript_records t
                  WHERE t.conversation_id = conversations.conversation_id
              )
            """,  # noqa: S608
            (*known_surfaces, default_cutoff_iso),
        )
        deleted += cur.rowcount if cur.rowcount > 0 else 0
        return deleted
