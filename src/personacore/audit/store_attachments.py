"""Attachment rows: insert, fetch-for-an-owner, delete, and the orphan sweep.

docs/contracts/attachments.md is the authority. §2 names the table's fields
and §3 says the row is what decides who may fetch a file — enforced here in
the query itself, the same shape :mod:`personacore.audit.store_conversations`
already uses: ``owner_kind = ? AND owner_id = ?`` beside the id in one
``WHERE``, so a caller naming somebody else's attachment matches no rows
rather than a row this code then has to remember to reject.

**This module knows nothing about the filesystem.** It reads and writes rows;
the file under ``<appdata>/attachments/<attachment_id>/`` is
:mod:`personacore.attachments`'s concern, because this store has a database
path and no idea where appdata's other directories are. That split is why
:meth:`AttachmentsMixin.orphaned_attachments` only *finds* rows worth
sweeping and never deletes a file — the caller that can reach a file (and
must, per contract §7: "the purge that removes transcript rows removes the
files those rows referenced") is on the other side of that module boundary.

**Ageing out is read off the message, not tracked twice.** The contract fixes
this table's fields (§2) and neither a surface nor an age is among them, so
"has this attachment's message been purged" is answered by asking
``transcript_records`` directly — the same ``correlation_id`` every audit and
transcript row already carries — rather than inventing a second retention
clock that could drift from the first.

Privacy: only ids, correlation ids, media types and byte counts are ever
logged here. ``original_name`` is never logged (contract §6/§7/§11) and
neither is anything about the file's content.
"""

from __future__ import annotations

import asyncio
import sqlite3

from personacore.audit.logging import get_logger
from personacore.audit.models import AttachmentRecord, Owner, OwnerKind
from personacore.audit.store_base import StoreBase
from personacore.audit.store_rows import _column, _iso, _parse_iso, _tolerant

_logger = get_logger(__name__)


def _row_to_attachment(row: sqlite3.Row) -> AttachmentRecord:
    conversation_id = _column(row, "conversation_id")
    return AttachmentRecord(
        attachment_id=row["attachment_id"],
        correlation_id=row["correlation_id"],
        conversation_id=str(conversation_id) if conversation_id is not None else None,
        owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
        media_type=row["media_type"],
        byte_size=int(row["byte_size"]),
        original_name=row["original_name"],
        created_at=_parse_iso(row["created_at"]),
    )


class AttachmentsMixin(StoreBase):
    """The ``attachments`` table: one writer, one owner-scoped read, one
    unconditional delete, and the read-only half of the retention sweep."""

    async def insert_attachment(self, record: AttachmentRecord) -> AttachmentRecord:
        """Record one attachment that has already landed on disk.

        Called only after the bytes are safely written — see
        :func:`personacore.attachments.put` — so a row here is always a
        promise this store can keep. There is nothing to return but the
        record itself: unlike the other tables, the primary key is minted by
        the caller before this is ever reached, not assigned here.
        """
        return await asyncio.to_thread(self._insert_attachment, record)

    def _insert_attachment(self, record: AttachmentRecord) -> AttachmentRecord:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO attachments (
                    attachment_id, correlation_id, conversation_id,
                    owner_kind, owner_id, media_type, byte_size,
                    original_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.attachment_id,
                    record.correlation_id,
                    record.conversation_id,
                    record.owner.kind.value,
                    record.owner.id,
                    record.media_type,
                    record.byte_size,
                    record.original_name,
                    _iso(record.created_at),
                ),
            )
            self._conn.commit()
        # original_name is deliberately absent — contract §6/§7/§11 treat a
        # filename a person chose the same way transcript content is treated.
        _logger.info(
            "attachment_stored",
            attachment_id=record.attachment_id,
            correlation_id=record.correlation_id,
            media_type=record.media_type,
            byte_size=record.byte_size,
        )
        return record

    async def get_attachment(
        self, attachment_id: str, *, owner: Owner
    ) -> AttachmentRecord | None:
        """This owner's attachment, or ``None`` for "not theirs" and "not
        there" alike — contract §3: the owner filter sits in the query's
        ``WHERE`` beside the id, so the two cases are indistinguishable from
        outside rather than something a caller has to remember to check."""
        return await asyncio.to_thread(self._get_attachment, attachment_id, owner)

    def _get_attachment(self, attachment_id: str, owner: Owner) -> AttachmentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id = ? "
                "AND owner_kind = ? AND owner_id = ?",
                (attachment_id, owner.kind.value, owner.id),
            ).fetchone()
        if row is None:
            return None
        found = _tolerant([row], _row_to_attachment, what="attachment")
        return found[0] if found else None

    async def delete_attachment_row(self, attachment_id: str) -> AttachmentRecord | None:
        """Remove one attachment's row outright, and return what was removed.

        **Unconditional — no owner filter.** The owner check belongs to
        whoever decided this attachment should go: an owner-initiated delete
        confirms ownership with :meth:`get_attachment` first, at the point
        that decision is actually being made, and the retention sweep this
        feeds is removing rows nobody can reach any more regardless of whose
        they were. Returns the deleted record so the caller — which is what
        can reach the file on disk — knows which directory to remove;
        ``None`` if there was nothing by that id, which is not an error: two
        callers racing to clean up the same orphan is an ordinary outcome of
        a sweep and a delete overlapping.
        """
        return await asyncio.to_thread(self._delete_attachment_row, attachment_id)

    def _delete_attachment_row(self, attachment_id: str) -> AttachmentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id = ?", (attachment_id,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM attachments WHERE attachment_id = ?", (attachment_id,)
            )
            self._conn.commit()
        found = _tolerant([row], _row_to_attachment, what="attachment")
        record = found[0] if found else None
        _logger.info("attachment_row_deleted", attachment_id=attachment_id)
        return record

    async def orphaned_attachments(self) -> list[AttachmentRecord]:
        """Attachments whose message has already been purged.

        Contract §7: "an attachment ages out with the conversation it belongs
        to." This table carries no surface or timestamp of its own to age by
        — read the migration's docstring for why — so age is read off
        ``transcript_records`` instead: once every row sharing this
        attachment's ``correlation_id`` is gone (aged out by
        :meth:`~personacore.audit.store_retention.RetentionMixin.
        purge_older_than`, or removed outright by an administrator's
        :meth:`~personacore.audit.store_conversations.ConversationsMixin.
        delete_conversation`), the message this attachment belonged to no
        longer exists and neither should it.

        **Hiding a conversation does not orphan its attachments.**
        :meth:`~personacore.audit.store_rooms.RoomsMixin.hide_conversation`
        only sets a timestamp; every transcript row stays, so every
        attachment's ``correlation_id`` still matches a live row and this
        query finds nothing to sweep — exactly the owner's rule (2026-09-02):
        an attachment is deleted when its conversation is deleted, and stays
        hidden until then, aged out, or removed by an admin.

        Read-only, on purpose: deleting the row and the file together is
        :func:`personacore.attachments.purge_orphaned`'s job, because doing
        that needs the appdata layout this store does not have.
        """
        return await asyncio.to_thread(self._orphaned_attachments)

    def _orphaned_attachments(self) -> list[AttachmentRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM attachments
                WHERE NOT EXISTS (
                    SELECT 1 FROM transcript_records t
                    WHERE t.correlation_id = attachments.correlation_id
                )
                """
            ).fetchall()
        return _tolerant(rows, _row_to_attachment, what="attachment")
