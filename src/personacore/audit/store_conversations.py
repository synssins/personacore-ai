"""A conversation: starting one, finding one, reading it, destroying it.

The heading and the messages under it. What a conversation *is* — a heading
rather than a container, and every way the two are allowed to disagree — is set
out in the comment block at the top of the class below, because that is the
reasoning every method here is written against.

Splitting this out of the store is a file boundary, not a trust boundary: the
ownership rule the comment states is enforced by each query in this file, one
by one, and a query added here without ``owner_kind = ? AND owner_id = ?`` is
the bug this module exists to make visible.

Deliberately not here: what you can *set* on a conversation once it exists —
persona, roster, name, group, hidden — which is
:mod:`personacore.audit.store_rooms`; and reconstructing conversations for rows
written before conversations existed, which is
:mod:`personacore.audit.store_backfill`.

Privacy: a title is derived from the opening message, so a title is content and
is never logged. Ids, surfaces and counts are.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from personacore.audit.logging import get_logger
from personacore.audit.models import (
    MessageRole,
    Owner,
    Surface,
    TranscriptRecord,
)
from personacore.audit.store_base import StoreBase
from personacore.audit.store_rows import (
    _conversations,
    _iso,
    _row_to_conversation,
    _row_to_transcript,
    _tolerant,
)

if TYPE_CHECKING:  # pragma: no cover - see store_rows._conversations()
    from personacore.conversations.models import Conversation, ConversationKind

_logger = get_logger(__name__)


class ConversationsMixin(StoreBase):
    """Conversations as headings over transcript rows."""

    # -- conversations ----------------------------------------------------
    #
    # A conversation is a heading, not a container: it holds identity, owner,
    # title and activity, and the messages point back at it. Everything below
    # is written so that the two can disagree without anything breaking -- a
    # heading with no messages left (the retention purge got there first), a
    # message with no heading (written before conversations existed, or by a
    # surface that has no thread), and a conversation id that names nothing at
    # all are all *expected* states with defined, non-fatal answers.
    #
    # Ownership is enforced in every single query, by owner_kind AND owner_id,
    # and never by conversation id alone. A conversation id is a URL-shaped
    # thing an operator can paste, so treating possession of one as permission
    # to read it would make the id a bearer token for somebody else's
    # transcript. The owner comes from the server's own idea of who is signed
    # in (spec section 8), never from the request.

    async def start_conversation(
        self,
        *,
        owner: Owner,
        surface: Surface,
        title: str | None = None,
        now: datetime | None = None,
        kind: ConversationKind | None = None,
    ) -> Conversation:
        """Begin a new, empty conversation and return it.

        ``kind`` is what answers in this thread (``docs/contracts/
        image-conversations.md`` §4) and is fixed here, at creation, for
        good: there is no method on this store that changes it afterwards.
        ``None`` -- every caller that predates this parameter, and every
        ordinary text conversation today -- resolves to
        :attr:`~personacore.conversations.models.ConversationKind.TEXT`.
        """
        return await asyncio.to_thread(
            self._start_conversation, owner, surface, title, now, kind
        )

    def _start_conversation(
        self,
        owner: Owner,
        surface: Surface,
        title: str | None,
        now: datetime | None,
        kind: ConversationKind | None = None,
    ) -> Conversation:
        if now is not None and now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        models = _conversations()
        resolved_kind = kind if kind is not None else models.ConversationKind.TEXT
        conversation = models.Conversation(
            conversation_id=models.new_conversation_id(),
            owner=owner,
            surface=surface,
            # Empty until somebody speaks: derive_title(None) is the
            # placeholder, and _attach_to_conversation replaces it with the
            # real opening line the moment a user message is claimed.
            title=title if title else models.derive_title(None),
            started_at=moment,
            last_activity_at=moment,
            origin=models.ConversationOrigin.LIVE,
            kind=resolved_kind,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, owner_kind, owner_id, surface,
                    title, started_at, last_activity_at, origin, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation.conversation_id,
                    owner.kind.value,
                    owner.id,
                    surface.value,
                    conversation.title,
                    _iso(conversation.started_at),
                    _iso(conversation.last_activity_at),
                    conversation.origin.value,
                    conversation.kind.value,
                ),
            )
            self._conn.commit()
        # No title logged: a title is derived from the opening message, which
        # makes it conversation content under this module's privacy rule.
        _logger.info(
            "conversation_started",
            conversation_id=conversation.conversation_id,
            surface=surface.value,
        )
        return conversation

    async def visible_conversation_ids(self) -> list[str]:
        """Every conversation id that is not hidden, across all owners.

        Read by the workspace sweep (workspace contract §2): a workspace
        folder survives only while its conversation is visible, because
        hiding one already removes its folder and an administrator's delete
        removes the row. Not owner-scoped on purpose — the sweep is
        housekeeping over the whole appdata, not a request from a person.
        """
        return await asyncio.to_thread(self._visible_conversation_ids)

    def _visible_conversation_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id FROM conversations WHERE hidden_at IS NULL"
            ).fetchall()
        return [str(row[0]) for row in rows]

    async def list_conversations(
        self,
        *,
        owner: Owner,
        surface: Surface | None = None,
        limit: int = 50,
        include_empty: bool = False,
        include_hidden: bool = False,
    ) -> list[Conversation]:
        """One person's conversations, most recently spoken in first.

        ``include_hidden`` is off by default so every caller that predates it
        keeps the behaviour it has: an owner's list is the conversations the
        owner has not taken off it. Turning it on is the administrator's view
        (a hidden conversation is kept, not destroyed) and is never reached
        from an owner-facing route.

        ``include_empty`` is off by default, and that default is the answer to
        two different problems with one rule: **a conversation with no
        messages is not shown.**

        * Opening the chat screen mints a conversation before anything has
          been said in it. Somebody who opens the screen, thinks better of it
          and navigates away should not leave a row behind.
        * A conversation whose every message has been purged (ADR-0004) is the
          same shape from here: a heading over nothing. It disappears from the
          list the instant its last message goes, without waiting for the
          sweep in :meth:`purge_older_than` to remove the heading itself.

        The second is the one that matters. Reaping empty headings and hiding
        them are deliberately *both* implemented, because they fail in
        opposite directions: hiding is immediate but leaves rows on disk, and
        reaping removes them but only when a purge next runs. Relying on the
        reap alone would leave a window -- however long the purge interval is
        -- in which the list shows headings that open onto nothing, which is
        exactly the outcome this was asked not to have.
        """
        return await asyncio.to_thread(
            self._list_conversations, owner, surface, limit, include_empty, include_hidden
        )

    def _list_conversations(
        self,
        owner: Owner,
        surface: Surface | None,
        limit: int,
        include_empty: bool,
        include_hidden: bool = False,
    ) -> list[Conversation]:
        clauses = ["c.owner_kind = ?", "c.owner_id = ?"]
        params: list[object] = [owner.kind.value, owner.id]
        if surface is not None:
            clauses.append("c.surface = ?")
            params.append(surface.value)
        if not include_hidden:
            clauses.append("c.hidden_at IS NULL")
        having = "" if include_empty else "HAVING message_count > 0"
        joined = " AND ".join(clauses)
        # Every fragment interpolated below is a fixed literal chosen by this
        # function; every value goes through a `?` placeholder.
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT c.*, COUNT(t.id) AS message_count
                FROM conversations c
                LEFT JOIN transcript_records t
                    ON t.conversation_id = c.conversation_id
                WHERE {joined}
                GROUP BY c.id
                {having}
                ORDER BY c.last_activity_at DESC, c.id DESC
                LIMIT ?
                """,  # noqa: S608
                (*params, limit),
            ).fetchall()
        return _tolerant(rows, _row_to_conversation, what="conversation")

    async def get_conversation(self, conversation_id: str, *, owner: Owner) -> Conversation | None:
        """One conversation, or ``None`` if it does not exist **or is not this
        owner's**.

        The two are deliberately the same answer: distinguishing them would
        turn this into an oracle telling one household member which
        conversation ids another one has.
        """
        return await asyncio.to_thread(self._get_conversation, conversation_id, owner)

    def _get_conversation(self, conversation_id: str, owner: Owner) -> Conversation | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT c.*, COUNT(t.id) AS message_count
                FROM conversations c
                LEFT JOIN transcript_records t
                    ON t.conversation_id = c.conversation_id
                WHERE c.conversation_id = ? AND c.owner_kind = ? AND c.owner_id = ?
                GROUP BY c.id
                """,
                (conversation_id, owner.kind.value, owner.id),
            ).fetchone()
        if row is None:
            return None
        found = _tolerant([row], _row_to_conversation, what="conversation")
        return found[0] if found else None

    async def conversation_at(self, *, owner: Owner, started_at: datetime) -> Conversation | None:
        """The conversation this owner started at exactly this instant.

        The admin chat form posts back the instant the screen was opened, not
        a conversation id, and that screen's markup belongs to another change
        in flight -- so the conversation is resolved from the marker the form
        already carries rather than by editing the form. ``started_at`` is an
        exact match on the stored instant, not a range: it is echoed back from
        a value this store itself produced, so a near-miss means the marker
        did not come from a conversation and the honest answer is ``None``.
        """
        return await asyncio.to_thread(self._conversation_at, owner, started_at)

    def _conversation_at(self, owner: Owner, started_at: datetime) -> Conversation | None:
        if started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT c.*, 0 AS message_count
                FROM conversations c
                WHERE c.owner_kind = ? AND c.owner_id = ? AND c.started_at = ?
                ORDER BY c.id DESC
                LIMIT 1
                """,
                (owner.kind.value, owner.id, _iso(started_at)),
            ).fetchone()
        if row is None:
            return None
        found = _tolerant([row], _row_to_conversation, what="conversation")
        return found[0] if found else None

    async def conversation_for(
        self,
        *,
        owner: Owner,
        surface: Surface,
        started_at: datetime,
        create: bool = True,
    ) -> Conversation | None:
        """The conversation an instant names, creating one if it names none.

        The admin chat screen identifies a thread by an instant rather than by
        an id — the moment the screen was opened, or, once something has been
        said, the moment the thread's first message landed. Both are echoed
        back by the composer and by ``?c=`` in the URL. This is the one place
        that turns either of them into the conversation row messages get
        attached to, so the screen never has to know whether it is starting
        something or resuming it.

        Three steps, in order, and each is exact rather than approximate:

        1. **A conversation that started at this instant** — the screen was
           opened, this is its second or later turn, and the composer is still
           carrying the instant it was opened at.
        2. **The conversation named by the message at this instant** — a thread
           reopened from the rail, whose identity is its first message's
           timestamp. That message already carries a conversation id (its own
           turn attached it, or the backfill did), so the thread is found by
           asking the message rather than by guessing from the clock.
        3. **Neither** — a conversation opened just now that nothing has been
           said in yet. One is created, starting at this instant, so step 1
           finds it next turn. ``create=False`` stops there and answers
           ``None`` instead, which is what a caller that is about to *delete*
           something wants: creating a conversation in order to remove it
           would be an absurd way to answer "there is nothing there".

        Nothing here matches *approximately*. An instant that is close to a
        conversation but not equal to one starts a new conversation, which is
        the honest reading: near-misses come from a stale bookmark or a
        hand-edited field, and quietly filing a message into the nearest
        thread would put words in a conversation they were not said in.

        Returns ``None`` when nothing matched and ``create`` is off.
        """
        return await asyncio.to_thread(
            self._conversation_for, owner, surface, started_at, create
        )

    def _conversation_for(
        self, owner: Owner, surface: Surface, started_at: datetime, create: bool
    ) -> Conversation | None:
        existing = self._conversation_at(owner, started_at)
        if existing is not None:
            return existing

        with self._lock:
            row = self._conn.execute(
                """
                SELECT conversation_id FROM transcript_records
                WHERE owner_kind = ? AND owner_id = ? AND surface = ?
                  AND ts_utc = ? AND conversation_id IS NOT NULL
                ORDER BY id ASC LIMIT 1
                """,
                (owner.kind.value, owner.id, surface.value, _iso(started_at)),
            ).fetchone()
        if row is not None:
            named = self._get_conversation(str(row["conversation_id"]), owner)
            if named is not None:
                return named

        if not create:
            return None
        return self._start_conversation(owner, surface, None, started_at)

    async def read_conversation(
        self, conversation_id: str, *, owner: Owner, limit: int = 200
    ) -> list[TranscriptRecord]:
        """This conversation's messages, newest first -- the same order
        :meth:`query_transcript` returns, so a caller that knows how to read
        one knows how to read the other.

        Returns an empty list for a conversation that is not this owner's,
        rather than raising: reading somebody else's thread and reading a
        thread the purge has emptied are the same non-event.
        """
        return await asyncio.to_thread(self._read_conversation, conversation_id, owner, limit)

    def _read_conversation(
        self, conversation_id: str, owner: Owner, limit: int
    ) -> list[TranscriptRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM transcript_records
                WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?
                ORDER BY ts_utc DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, owner.kind.value, owner.id, limit),
            ).fetchall()
        return _tolerant(rows, _row_to_transcript, what="transcript")

    async def attach_to_conversation(
        self,
        conversation_id: str,
        *,
        owner: Owner,
        surface: Surface,
        since: datetime,
    ) -> int:
        """Claim this owner's unattached messages on this surface from
        ``since`` onward into this conversation, and return how many.

        This is how a surface that does not own its own write path keeps a
        thread. The admin chat screen's messages are written by the agent loop
        (ADR-0004), which knows nothing about conversations and should not have
        to; the screen therefore claims the rows its turn just produced, once
        the turn has finished.

        Three properties make that safe rather than clever:

        * **Only unattached rows** (``conversation_id IS NULL``) are claimed,
          so a second caller cannot take messages out of a conversation that
          already has them, and running this twice for one turn claims nothing
          the second time.
        * **Only this owner's rows**, on this surface, so the worst a wrong
          ``since`` can do is pull an operator's own earlier admin messages
          into their own conversation. It can never reach another person's.
        * The conversation's ``last_activity_at`` and, while it is still
          untitled, its title are recomputed from what the conversation
          actually holds -- so ordering and heading are derived from messages
          that exist rather than from an intention.
        """
        return await asyncio.to_thread(
            self._attach_to_conversation, conversation_id, owner, surface, since
        )

    def _attach_to_conversation(
        self,
        conversation_id: str,
        owner: Owner,
        surface: Surface,
        since: datetime,
    ) -> int:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        with self._lock:
            owned = self._conn.execute(
                "SELECT title FROM conversations "
                "WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?",
                (conversation_id, owner.kind.value, owner.id),
            ).fetchone()
            if owned is None:
                # Not this owner's conversation, or not a conversation at all.
                # Claiming nothing is the whole response; the messages stay
                # unattached and readable in the log view.
                return 0

            cur = self._conn.execute(
                """
                UPDATE transcript_records SET conversation_id = ?
                WHERE conversation_id IS NULL
                  AND owner_kind = ? AND owner_id = ? AND surface = ? AND ts_utc >= ?
                """,
                (conversation_id, owner.kind.value, owner.id, surface.value, _iso(since)),
            )
            claimed = cur.rowcount if cur.rowcount > 0 else 0

            summary = self._conn.execute(
                """
                SELECT MAX(ts_utc) AS newest,
                       (SELECT content FROM transcript_records
                         WHERE conversation_id = ? AND role = ?
                         ORDER BY ts_utc ASC, id ASC LIMIT 1) AS opening
                FROM transcript_records WHERE conversation_id = ?
                """,
                (conversation_id, MessageRole.USER.value, conversation_id),
            ).fetchone()

            newest = summary["newest"] if summary else None
            opening = summary["opening"] if summary else None
            if newest:
                self._conn.execute(
                    "UPDATE conversations SET last_activity_at = ? WHERE conversation_id = ?",
                    (newest, conversation_id),
                )
            derive_title = _conversations().derive_title
            if opening and owned["title"] == derive_title(None):
                self._conn.execute(
                    "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                    (derive_title(str(opening)), conversation_id),
                )
            self._conn.commit()
        # Neither the claimed content nor the derived title is logged: both are
        # conversation content under this module's privacy rule.
        _logger.info(
            "conversation_messages_attached",
            conversation_id=conversation_id,
            surface=surface.value,
            claimed=claimed,
        )
        return claimed

    async def delete_conversation(self, conversation_id: str, *, owner: Owner) -> int:
        """Delete this owner's conversation **and every message in it**.

        Returns the number of messages removed. Deleting only the heading
        would leave the sentences on disk while telling the person who asked
        for them to be gone that they were -- the worst possible outcome for a
        delete control, and the reason both deletes are one transaction.

        A conversation that is not this owner's is not found, and nothing is
        deleted.

        **No longer reached from the chat screen.** The owner's control there
        calls :meth:`hide_conversation` instead, because a household's chats
        are kept for administrator review and clearing your own list must not
        destroy them. This stays, unchanged, as the administrator path and as
        what the word "delete" means when something really is to be destroyed.
        """
        return await asyncio.to_thread(self._delete_conversation, conversation_id, owner)

    def _delete_conversation(self, conversation_id: str, owner: Owner) -> int:
        with self._lock:
            owned = self._conn.execute(
                "SELECT 1 FROM conversations "
                "WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?",
                (conversation_id, owner.kind.value, owner.id),
            ).fetchone()
            if owned is None:
                return 0
            # The audit rows first, while the transcript rows that name them
            # still exist -- a correlation id groups everything one request
            # did, and after the next statement there is nothing left to read
            # those ids off.
            #
            # This used to delete only the transcript. Every TOOL_CALL row for
            # a deleted conversation stayed behind, and once a reply's timings
            # moved onto an audit record (metrics were required to survive a
            # reload) those stayed too -- so an administrator who deleted a
            # conversation kept a record of how long each of its replies took
            # and which tools they called. The rule for the metrics was
            # explicit: gone when the conversation is deleted, whether it
            # aged out or an administrator removed it. Ageing out already did
            # this; removing did not.
            audited = self._conn.execute(
                "DELETE FROM audit_records WHERE owner_kind = ? AND owner_id = ? "
                "AND correlation_id IN ("
                "  SELECT correlation_id FROM transcript_records"
                "  WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?"
                ")",
                (
                    owner.kind.value,
                    owner.id,
                    conversation_id,
                    owner.kind.value,
                    owner.id,
                ),
            )
            audit_deleted = audited.rowcount if audited.rowcount > 0 else 0
            # Same join, same reason, one table over: `reply_reasoning`
            # (migration 0007) shares a reply's correlation id exactly as its
            # `TurnMetrics` audit record does, and the rule for the metrics --
            # gone when the conversation is deleted, whether it aged out or
            # an administrator removed it -- applies to reasoning too. Run
            # before the transcript delete below, while the
            # rows naming these correlation ids still exist to be joined
            # against.
            reasoned = self._conn.execute(
                "DELETE FROM reply_reasoning WHERE owner_kind = ? AND owner_id = ? "
                "AND correlation_id IN ("
                "  SELECT correlation_id FROM transcript_records"
                "  WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?"
                ")",
                (
                    owner.kind.value,
                    owner.id,
                    conversation_id,
                    owner.kind.value,
                    owner.id,
                ),
            )
            reasoning_deleted = reasoned.rowcount if reasoned.rowcount > 0 else 0
            cur = self._conn.execute(
                "DELETE FROM transcript_records WHERE conversation_id = ? "
                "AND owner_kind = ? AND owner_id = ?",
                (conversation_id, owner.kind.value, owner.id),
            )
            deleted = cur.rowcount if cur.rowcount > 0 else 0
            self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )
            self._conn.commit()
        _logger.info(
            "conversation_deleted",
            conversation_id=conversation_id,
            transcript_deleted=deleted,
            audit_deleted=audit_deleted,
            reasoning_deleted=reasoning_deleted,
        )
        return deleted
