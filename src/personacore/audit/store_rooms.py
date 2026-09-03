"""The controls on a conversation: who is in it, what it is called, where
it is filed, and whether its owner still sees it.

Six writers with one shape. Each is an UPDATE that carries the owner check in
its own WHERE clause, returns whether a row changed, and answers ``False``
identically for "no such conversation" and "not this owner's" — distinguishing
them would make a conversation id a way to find out what somebody else has.

None of them touches ``last_activity_at`` and none of them reorders a list.
Choosing who answers, renaming a thread or filing it is not speaking in it, and
a list that jumped because somebody typed a group name would be reporting
something that did not happen.

**Hiding is not deleting and must never become it.** ``hide_conversation``
writes a timestamp; every word stays on disk for administrator review, and the
retention purge reaps hidden conversations on exactly the same schedule as
visible ones (:mod:`personacore.audit.store_retention`). Destroying is
``delete_conversation``, which lives with the rest of the thread operations.

Nothing here validates a persona name or a group against anything on disk, on
purpose: validation belongs where somebody chose the name, so a refusal can be
a sentence they read rather than a silent fall back to a default.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from personacore.audit.logging import get_logger
from personacore.audit.models import Owner
from personacore.audit.store_base import StoreBase
from personacore.audit.store_rows import _iso

_logger = get_logger(__name__)


class RoomsMixin(StoreBase):
    """What an owner can set on a conversation without speaking in it."""

    async def set_conversation_persona(
        self, conversation_id: str, *, owner: Owner, persona: str | None
    ) -> bool:
        """Record which persona this conversation is being held with.

        Returns whether a row was changed: ``False`` covers "no such
        conversation" and "not this owner's", which are the same non-event from
        the caller's side and are deliberately not distinguished — the owner
        check is here for the same reason it is on every other query in this
        section, and telling a caller that somebody else's conversation exists
        would undo it.

        ``persona`` is stored as given and is **not** validated against the
        personas on disk. Validation belongs where the name is chosen, so it
        can be refused to a person in a sentence; a store that silently dropped
        an unknown name would turn a refusal into a silent fall back to the
        default, which is the one outcome this feature must not have. A name
        that stops being loadable later fails loudly at the next turn, naming
        it.

        ``None`` clears the choice, which is a real request: "go back to
        whatever the core's default is" is not the same as pinning the current
        default's name, because the default may change.

        Nothing else about the conversation moves — not ``last_activity_at``,
        which belongs to messages, and not the title. Choosing who answers is
        not speaking.
        """
        return await asyncio.to_thread(
            self._set_conversation_persona, conversation_id, owner, persona
        )

    def _set_conversation_persona(
        self, conversation_id: str, owner: Owner, persona: str | None
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE conversations SET persona = ?
                WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?
                """,
                (persona, conversation_id, owner.kind.value, owner.id),
            )
            self._conn.commit()
        changed = cursor.rowcount > 0
        # The persona name is a folder name an operator installed, not
        # conversation content, so it is safe to log — and being able to see
        # who a thread was handed to is the point of the record.
        _logger.info(
            "conversation_persona_set",
            conversation_id=conversation_id,
            persona=persona,
            changed=changed,
        )
        return changed

    async def set_conversation_roster(
        self, conversation_id: str, *, owner: Owner, roster: Sequence[str]
    ) -> bool:
        """Record the other personas in this room — the many-voices contract §2.

        ``roster`` is everybody **besides** the one ``persona`` names, in the
        order they were added. An empty sequence stores NULL, which is the
        single-persona conversation this column exists to leave untouched:
        taking the last guest out of a room puts the row back into exactly the
        state it was in before anybody was added, rather than leaving an empty
        list behind that a later read has to interpret.

        Names are stored as given and are **not** checked against the personas
        on disk, for the same reason
        :meth:`set_conversation_persona` does not check: validation belongs
        where somebody picked the name, so a refusal can be a sentence they
        read. A store that quietly dropped an unknown name would turn a refusal
        into a room with fewer people in it than the screen says.

        Returns whether a row was changed. ``False`` covers "no such
        conversation" and "not this owner's", which are the same non-event from
        the caller's side.

        Nothing else about the conversation moves. Adding somebody to a room is
        not speaking in it, so ``last_activity_at`` is untouched and the list
        does not reorder.
        """
        return await asyncio.to_thread(
            self._set_conversation_roster, conversation_id, owner, list(roster)
        )

    def _set_conversation_roster(
        self, conversation_id: str, owner: Owner, roster: list[str]
    ) -> bool:
        stored = json.dumps(roster) if roster else None
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE conversations SET also_present = ?
                WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?
                """,
                (stored, conversation_id, owner.kind.value, owner.id),
            )
            self._conn.commit()
        changed = cursor.rowcount > 0
        # Persona names are folder names an operator installed, not
        # conversation content — the same reasoning
        # ``conversation_persona_set`` is logged under, and who is in a room is
        # exactly what an administrator reviewing it wants to be able to see.
        _logger.info(
            "conversation_roster_set",
            conversation_id=conversation_id,
            roster=roster,
            changed=changed,
        )
        return changed

    # -- the room controls ------------------------------------------------
    #
    # Hide, rename and regroup. All three take the same shape as
    # set_conversation_persona above and for the same reasons: owner checked in
    # the UPDATE itself, ``False`` for "no such conversation" and "not this
    # owner's" without distinguishing them, and nothing else about the
    # conversation moved. Renaming or filing a thread is not speaking in it, so
    # none of them touch ``last_activity_at`` and none of them reorder a list.

    async def hide_conversation(
        self, conversation_id: str, *, owner: Owner, hidden_by: str, now: datetime | None = None
    ) -> bool:
        """Take one conversation off its owner's list, keeping every word of it.

        **This is not a delete and must never become one.** The reason it
        exists is that a household's chats stay available for administrator
        review while the person who held them can clear their own list —
        removing the record is exactly the outcome the feature is there to
        prevent. :meth:`delete_conversation` is still the thing that destroys.

        Already hidden is ``False``: the UPDATE requires ``hidden_at IS NULL``,
        so hiding twice does not move the timestamp. The first time is when it
        happened, and a second click should not rewrite that.

        ``hidden_by`` is recorded separately from the owner because they will
        not always be the same person, and a column that only ever held a copy
        of the owner would be useless the day an administrator path exists.
        """
        return await asyncio.to_thread(
            self._hide_conversation, conversation_id, owner, hidden_by, now
        )

    def _hide_conversation(
        self, conversation_id: str, owner: Owner, hidden_by: str, now: datetime | None
    ) -> bool:
        if now is not None and now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE conversations SET hidden_at = ?, hidden_by = ?
                WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?
                  AND hidden_at IS NULL
                """,
                (_iso(moment), hidden_by, conversation_id, owner.kind.value, owner.id),
            )
            self._conn.commit()
        changed = cursor.rowcount > 0
        _logger.info(
            "conversation_hidden",
            conversation_id=conversation_id,
            changed=changed,
        )
        return changed

    async def rename_conversation(
        self, conversation_id: str, *, owner: Owner, title: str
    ) -> bool:
        """Retitle one conversation.

        ``title`` is written as given. Trimming, capping and refusing a blank
        one belong to the caller — :class:`ConversationService.rename` — so that
        a refusal can be a sentence somebody reads rather than a silently
        stored empty string. The store's job is the owner check and the write.
        """
        return await asyncio.to_thread(self._rename_conversation, conversation_id, owner, title)

    def _rename_conversation(self, conversation_id: str, owner: Owner, title: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE conversations SET title = ?
                WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?
                """,
                (title, conversation_id, owner.kind.value, owner.id),
            )
            self._conn.commit()
        changed = cursor.rowcount > 0
        # The title itself is never logged: it is derived from, or typed in
        # place of, the opening message, which makes it conversation content
        # under this module's privacy rule.
        _logger.info(
            "conversation_renamed",
            conversation_id=conversation_id,
            changed=changed,
        )
        return changed

    async def set_conversation_group(
        self, conversation_id: str, *, owner: Owner, group: str | None
    ) -> bool:
        """File this conversation under a group, or clear it with ``None``.

        The group is stored as given and is not checked against anything: a
        group is a label somebody typed, and there is no table of them to check
        against on purpose (see ``Conversation.group_name``).
        """
        return await asyncio.to_thread(
            self._set_conversation_group, conversation_id, owner, group
        )

    def _set_conversation_group(
        self, conversation_id: str, owner: Owner, group: str | None
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE conversations SET group_name = ?
                WHERE conversation_id = ? AND owner_kind = ? AND owner_id = ?
                """,
                (group, conversation_id, owner.kind.value, owner.id),
            )
            self._conn.commit()
        changed = cursor.rowcount > 0
        # The group name is a filing label an operator typed for themselves,
        # not something anybody said in a conversation, so it is safe to log --
        # and seeing how a household files its threads is the point of having
        # the record at all.
        _logger.info(
            "conversation_grouped",
            conversation_id=conversation_id,
            group=group,
            changed=changed,
        )
        return changed

    async def conversation_groups(self, *, owner: Owner) -> list[str]:
        """Every group name this owner has used, sorted, for the picker.

        Hidden conversations are included. A group is only offered as a
        suggestion, and dropping a label because the one conversation that used
        it was hidden would make the same word un-suggestable for reasons the
        owner cannot see -- while telling them nothing about what is hidden,
        since the suggestion is a word they typed themselves.
        """
        return await asyncio.to_thread(self._conversation_groups, owner)

    def _conversation_groups(self, owner: Owner) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT group_name FROM conversations
                WHERE owner_kind = ? AND owner_id = ? AND group_name IS NOT NULL
                  AND group_name <> ''
                ORDER BY group_name
                """,
                (owner.kind.value, owner.id),
            ).fetchall()
        return [str(row["group_name"]) for row in rows]
