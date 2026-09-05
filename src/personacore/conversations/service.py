"""The conversation operations a surface actually calls, and the promise that
none of them can take the surface down.

:class:`~personacore.audit.store.AuditStore` holds the SQL. This holds the two
things a screen needs on top of it and should not each reinvent:

1. **A capability check.** The admin UI is handed an ``AuditGateway``
   (:mod:`personacore.admin.protocols`) — a structural protocol that several
   test doubles and any future store implementation also satisfy. Some of
   those know nothing about conversations. Rather than widen that protocol and
   break every double at once, this asks the object it was given whether it
   can do the job and reports :attr:`ConversationService.available` honestly;
   a screen with an unsuitable store degrades to the single rolling history it
   had before instead of erroring.

2. **A failure floor.** Every method here returns an empty, absent or zero
   answer instead of raising. This is not blanket defensiveness — it is the
   one rule this feature was given: *nothing here may stop ``create_app``
   returning, and no malformed conversation, missing row or unreadable store
   may take the core down.* A conversation list is a convenience on top of a
   transcript that is still whole and still readable in the log view, so
   losing the list is an inconvenience and losing the admin UI is a lockout.
   The exception is logged every time, with ids and counts only and never
   message content.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from personacore.audit.logging import get_logger
from personacore.audit.models import Owner, Surface, TranscriptRecord
from personacore.config.appdata import AppdataLayout
from personacore.conversations.models import (
    MAX_ROSTER,
    MAX_TITLE_LENGTH,
    Conversation,
    ConversationKind,
)
from personacore.workspaces import Workspace, WorkspaceError
from personacore.workspaces import remove as _rmtree_workspace

_logger = get_logger(__name__)

# The ceilings below are workspace.py's own published defaults
# (`config/workspace.py`'s `WorkspaceSettings`). They are read only to count
# a workspace's files before it is removed — `Workspace.list()` never
# enforces either ceiling, so any value here would do; these are simply the
# ones an operator who never touched the setting would actually have.
_COUNT_MAX_FILE_BYTES = 2_000_000
_COUNT_MAX_WORKSPACE_BYTES = 50_000_000


def _unless_hidden(
    conversation: Conversation | None, include_hidden: bool
) -> Conversation | None:
    """The conversation, or ``None`` if it is hidden and hidden is not wanted.

    One helper rather than the same two lines in three places, because the
    consequence of forgetting it once is a conversation the owner asked to have
    off their list still opening for them.
    """
    if conversation is None:
        return None
    if conversation.hidden_at is not None and not include_hidden:
        return None
    return conversation


@runtime_checkable
class ConversationStore(Protocol):
    """The part of the store this service drives.

    ``runtime_checkable`` so :class:`ConversationService` can ask an object it
    did not construct whether it implements this. That check is by method
    *name* only — which is exactly the right strength here: the question being
    asked is "is this a store that knows about conversations at all", not "is
    this store correct", and a positive answer is still followed by a
    try/except on every call.
    """

    async def start_conversation(
        self,
        *,
        owner: Owner,
        surface: Surface,
        title: str | None = None,
        now: datetime | None = None,
        kind: ConversationKind | None = None,
    ) -> Conversation: ...

    async def list_conversations(
        self,
        *,
        owner: Owner,
        surface: Surface | None = None,
        limit: int = 50,
        include_empty: bool = False,
        include_hidden: bool = False,
    ) -> list[Conversation]: ...

    async def get_conversation(
        self, conversation_id: str, *, owner: Owner
    ) -> Conversation | None: ...

    async def conversation_at(
        self, *, owner: Owner, started_at: datetime
    ) -> Conversation | None: ...

    async def conversation_for(
        self,
        *,
        owner: Owner,
        surface: Surface,
        started_at: datetime,
        create: bool = True,
    ) -> Conversation | None: ...

    async def read_conversation(
        self, conversation_id: str, *, owner: Owner, limit: int = 200
    ) -> list[TranscriptRecord]: ...

    async def attach_to_conversation(
        self,
        conversation_id: str,
        *,
        owner: Owner,
        surface: Surface,
        since: datetime,
    ) -> int: ...

    async def delete_conversation(self, conversation_id: str, *, owner: Owner) -> int: ...


@runtime_checkable
class PersonaAwareStore(Protocol):
    """The one newer operation, asked for separately.

    Deliberately **not** folded into :class:`ConversationStore`. That protocol
    is checked with ``isinstance`` against objects this package did not build —
    real stores, test doubles, and whatever a future implementation is — and
    widening it would make every one of them stop being a conversation store at
    all the day this shipped, silently degrading the chat screen to no
    conversations rather than to no persona choice. A second, narrower question
    fails in proportion: a store that cannot record a persona still lists,
    reads and attaches, and the picker says plainly that the choice cannot be
    remembered instead of the rail emptying.
    """

    async def set_conversation_persona(
        self, conversation_id: str, *, owner: Owner, persona: str | None
    ) -> bool: ...


@runtime_checkable
class ThinkingAwareStore(Protocol):
    """The conversation's own thinking override — workspace contract §13, D.

    A fifth protocol, for the same reason :class:`PersonaAwareStore` and the
    others are separate: :class:`ConversationStore` is checked with
    ``isinstance`` against objects this package did not build, so widening it
    would make every existing store and test double stop being a conversation
    store on the day this shipped. A store that cannot answer this still
    lists, reads, attaches and remembers a persona; the chat header's Thinking
    checkbox simply reports that it cannot be remembered here.
    """

    async def set_conversation_thinking(
        self, conversation_id: str, *, owner: Owner, thinking: bool | None
    ) -> bool: ...


@runtime_checkable
class RosterStore(Protocol):
    """Who else is in the room — the many-voices contract, §2.

    A fourth protocol, for the third time and the same reason
    :class:`PersonaAwareStore` and :class:`RoomStore` are separate:
    :class:`ConversationStore` is checked with ``isinstance`` against objects
    this package did not build, so widening it would make every existing store
    and test double stop being a conversation store on the day this shipped.
    The rail would empty rather than the "add a persona" control being absent.

    A store that cannot answer this still lists, reads, attaches and remembers
    one persona — which is the whole of the single-persona conversation §7
    requires to be untouched. A core with an older store simply cannot put a
    second character in a room, and says so.
    """

    async def set_conversation_roster(
        self, conversation_id: str, *, owner: Owner, roster: Sequence[str]
    ) -> bool: ...


@runtime_checkable
class RoomStore(Protocol):
    """Hiding, renaming and filing — the controls that turn a rolling
    transcript into a room somebody keeps.

    A third protocol rather than three more methods on
    :class:`ConversationStore`, for exactly the reason
    :class:`PersonaAwareStore` is separate: ``ConversationStore`` is checked
    with ``isinstance`` against objects this package did not build, so widening
    it would make every existing store and test double stop being a
    conversation store on the day this shipped — the conversation rail would
    empty rather than the rename button being absent. A narrower question fails
    in proportion: a store that cannot rename still lists, reads and attaches.
    """

    async def hide_conversation(
        self, conversation_id: str, *, owner: Owner, hidden_by: str, now: datetime | None = None
    ) -> bool: ...

    async def rename_conversation(
        self, conversation_id: str, *, owner: Owner, title: str
    ) -> bool: ...

    async def set_conversation_group(
        self, conversation_id: str, *, owner: Owner, group: str | None
    ) -> bool: ...

    async def conversation_groups(self, *, owner: Owner) -> list[str]: ...


class ConversationService:
    """Conversations for one surface, over whatever store it was handed."""

    def __init__(
        self, store: object, *, surface: Surface, layout: AppdataLayout | None = None
    ) -> None:
        self._store = store if isinstance(store, ConversationStore) else None
        self._persona_store = store if isinstance(store, PersonaAwareStore) else None
        self._room_store = store if isinstance(store, RoomStore) else None
        self._roster_store = store if isinstance(store, RosterStore) else None
        self._thinking_store = store if isinstance(store, ThinkingAwareStore) else None
        self._surface = surface
        # Workspace contract §2: hiding takes the conversation's workspace
        # with it. `None` — the default, and what every caller before this
        # parameter existed still passes — means no removal happens, the
        # same "a store that cannot do this is not asked to" shape every
        # other optional capability on this class already follows.
        self._layout = layout

    @property
    def available(self) -> bool:
        """Whether the store behind this service knows about conversations.

        A screen asks this once and picks its behaviour, rather than
        discovering the answer through an exception per request.
        """
        return self._store is not None

    @property
    def remembers_persona(self) -> bool:
        """Whether a persona chosen for a thread can be written down.

        Asked before offering the choice, so a store that cannot keep it says
        so rather than appearing to accept a pick that evaporates on the next
        page load.
        """
        return self._persona_store is not None

    @property
    def manages_rooms(self) -> bool:
        """Whether this store can hide, rename and file a conversation.

        Asked before offering those controls, for the same reason
        :attr:`remembers_persona` is asked before offering the picker: a button
        that silently does nothing is worse than a button that is not there.
        """
        return self._room_store is not None

    @property
    def remembers_thinking(self) -> bool:
        """Whether a conversation's own thinking override can be written
        down.

        Asked before offering the chat header's Thinking checkbox, for the
        same reason :attr:`remembers_persona` is asked before the picker."""
        return self._thinking_store is not None

    @property
    def holds_a_roster(self) -> bool:
        """Whether this store can put a second persona in a conversation.

        Asked before the control is offered, for the reason
        :attr:`remembers_persona` is asked before the picker is: a button that
        appears to add somebody to the room and then forgets is worse than one
        that is not there.
        """
        return self._roster_store is not None

    async def set_roster(
        self, conversation: Conversation | None, roster: Sequence[str]
    ) -> bool:
        """Record who else is in this room. ``False`` when nothing was written.

        ``roster`` is everybody besides the persona the picker names — see
        :attr:`~personacore.conversations.models.Conversation.also_present`.
        It is trimmed, de-duplicated and capped at
        :data:`~personacore.conversations.models.MAX_ROSTER` here rather than in
        the store, so the bound applies whichever surface asks and the store
        stays the thing that writes what it was told.

        ``False`` for a store that cannot do this, a conversation that could
        not be resolved, one that is not this owner's, and an unreadable store.
        The caller turns that into a sentence; it never turns into a room with
        somebody in it who is not on the screen.
        """
        if self._roster_store is None or conversation is None:
            return False
        cleaned: list[str] = []
        for name in roster:
            trimmed = str(name).strip()
            if trimmed and trimmed not in cleaned:
                cleaned.append(trimmed)
            if len(cleaned) >= MAX_ROSTER:
                break
        try:
            return await self._roster_store.set_conversation_roster(
                conversation.conversation_id, owner=conversation.owner, roster=cleaned
            )
        except Exception as exc:  # noqa: BLE001 - see module docstring
            _logger.error(
                "conversation_roster_failed",
                conversation_id=conversation.conversation_id,
                error=repr(exc),
            )
            return False

    async def set_persona(self, conversation: Conversation | None, persona: str | None) -> bool:
        """Record who this conversation is being held with. ``False`` when
        nothing was written.

        Writes to the conversation row and to nothing else — in particular not
        to ``core.toml``: the core's default persona is what every other
        surface gets, and choosing in one thread must not move it.

        ``False`` is returned rather than raised for a store that cannot do
        this, a conversation that could not be resolved, a conversation that is
        not this owner's, and an unreadable store. The caller turns that into a
        sentence; it never turns into an unrequested persona.
        """
        if self._persona_store is None or conversation is None:
            return False
        try:
            return await self._persona_store.set_conversation_persona(
                conversation.conversation_id, owner=conversation.owner, persona=persona
            )
        except Exception as exc:  # noqa: BLE001 - see module docstring
            _logger.error(
                "conversation_persona_failed",
                conversation_id=conversation.conversation_id,
                error=repr(exc),
            )
            return False

    async def set_thinking(
        self, owner: Owner, conversation_id: str, value: bool | None
    ) -> bool:
        """Record this conversation's own thinking override (workspace
        contract §13, D). ``False`` when nothing was written.

        Unlike :meth:`set_persona`, this takes ``owner`` and
        ``conversation_id`` directly rather than a loaded
        :class:`~personacore.conversations.models.Conversation` — the chat
        header's Thinking checkbox already knows both without needing to
        load the row first.

        ``value`` is tri-state: ``True``/``False`` pins thinking on or off
        for this thread regardless of the persona's own switch; ``None``
        clears the override and goes back to following the persona.

        ``False`` is returned rather than raised for a store that cannot do
        this, and an unreadable store — the caller turns that into a
        sentence, or simply leaves the checkbox unremembered."""
        if self._thinking_store is None:
            return False
        try:
            return await self._thinking_store.set_conversation_thinking(
                conversation_id, owner=owner, thinking=value
            )
        except Exception as exc:  # noqa: BLE001 - see module docstring
            _logger.error(
                "conversation_thinking_failed",
                conversation_id=conversation_id,
                error=repr(exc),
            )
            return False

    async def start(
        self, owner: Owner, *, kind: ConversationKind | None = None
    ) -> Conversation | None:
        """Begin a new conversation. ``kind`` is fixed for its whole life —
        see :attr:`~personacore.conversations.models.Conversation.kind` —
        and ``None`` means the ordinary text conversation every caller before
        this parameter existed already got.
        """
        if self._store is None:
            return None
        try:
            return await self._store.start_conversation(
                owner=owner, surface=self._surface, kind=kind
            )
        except Exception as exc:  # noqa: BLE001 - see module docstring
            _logger.error("conversation_start_failed", error=repr(exc))
            return None

    async def listing(
        self, owner: Owner, *, limit: int = 50, include_hidden: bool = False
    ) -> list[Conversation]:
        """This owner's conversations, newest activity first.

        Scoped to this service's surface: the chat screen's list is the chat
        screen's conversations. A voice thread appearing in the admin UI's
        sidebar would be a different feature with a different consent story
        (spec section 8), not a free bonus.

        ``include_hidden`` defaults to False, so every caller that predates it
        keeps the list it had. It is the administrator's switch: a hidden
        conversation is kept for review, and the owner's own list is the one
        place it must not appear.
        """
        if self._store is None:
            return []
        # ``include_hidden`` is passed only when it is asked for. A store
        # written before this argument existed -- an older build, or one of the
        # several test doubles that satisfy ``ConversationStore`` structurally
        # -- would raise ``TypeError`` on the keyword, which the guard below
        # would turn into an empty rail. A store with no idea what hidden means
        # has nothing hidden to leave out, so omitting the argument is not a
        # weaker answer, it is the same one.
        extra = {"include_hidden": True} if include_hidden else {}
        # A conversation with nothing said in it yet belongs on the list.
        #
        # 2026-09-02: clicking "new conversation" created only a client-side
        # untitled placeholder in the chat pane; the row did not exist in the
        # conversation list until the first reply landed and gave it a title.
        # The owner asked for the untitled row to appear in the list
        # immediately and have its title fill in after the first exchange.
        #
        # The store's default was off, and its docstring gives two reasons.
        # Only one of them was ever live here:
        #
        #   * A heading over messages that have been purged. Handled already,
        #     and better: `_reap_empty_conversations` deletes it in the *same
        #     transaction* as the deletes that emptied it, so there is no
        #     moment at which such a heading exists to be listed.
        #   * A screen opened and walked away from. This one is real, and it
        #     is what the owner asked to see. The cost -- abandoned empty
        #     threads accumulating -- was accepted. They are not immortal
        #     either: the reap takes them once they pass their surface's
        #     retention window.
        #
        # Same defensive shape as `include_hidden` above and for the same
        # reason: a store written before this keyword -- an older build, or one
        # of the test doubles that satisfy `ConversationStore` structurally --
        # raises `TypeError`, which the guard below turns into an empty rail.
        # Losing the whole list to gain an empty row would be a poor trade.
        try:
            return await self._store.list_conversations(
                owner=owner,
                surface=self._surface,
                limit=limit,
                include_empty=True,
                **extra,
            )
        except TypeError:
            return await self._store.list_conversations(
                owner=owner, surface=self._surface, limit=limit, **extra
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error("conversation_list_failed", error=repr(exc))
            return []

    async def resolve(
        self,
        owner: Owner,
        *,
        conversation_id: str | None = None,
        started_at: datetime | None = None,
        include_hidden: bool = False,
    ) -> Conversation | None:
        """The conversation a request is talking about, or ``None``.

        Two ways in, checked in that order:

        * ``conversation_id`` — what a URL names and what a rebuilt chat form
          will post. Preferred, and the only one that survives the form
          changing.
        * ``started_at`` — the instant marker the *current* chat form already
          carries. Supported so this change needs no edit to that form's
          markup, which is being rebuilt by another change in flight.

        Both resolve through an owner check in the store, so neither is a way
        to reach a conversation that is not yours.

        A **hidden** conversation resolves to ``None`` unless
        ``include_hidden`` is set. Taking it off the list is only half of what
        the owner asked for; a conversation they can still open by holding onto
        its id was not removed from their view at all. The store itself answers
        honestly — it has to, because the administrator's review reads through
        it — so this is the layer that keeps the owner out.
        """
        if self._store is None:
            return None
        try:
            if conversation_id:
                found = await self._store.get_conversation(conversation_id, owner=owner)
                if found is not None:
                    return _unless_hidden(found, include_hidden)
            if started_at is not None:
                found = await self._store.conversation_at(owner=owner, started_at=started_at)
                return _unless_hidden(found, include_hidden)
        except Exception as exc:  # noqa: BLE001
            _logger.error("conversation_resolve_failed", error=repr(exc))
        return None

    async def at(
        self, owner: Owner, started_at: datetime, *, create: bool = True
    ) -> Conversation | None:
        """The conversation an instant names — created if it names none, unless
        ``create`` is off.

        The admin chat screen names a thread by an instant rather than an id,
        so this is the call that turns what the composer and the URL carry into
        the row a turn's messages get attached to. ``create=False`` is for
        callers asking whether something exists (a delete, most obviously)
        rather than intending to speak into it.

        A hidden conversation is ``None`` here too, for the same reason it is
        in :meth:`resolve`: an owner who cleared a thread from their list must
        not be able to keep speaking into it by way of a stale marker in the
        composer. The turn still happens and the messages are still recorded —
        they simply stay unattached and are given a fresh conversation of their
        own by the next backfill, which is the right answer for words said
        after somebody closed the room they were said in.
        """
        if self._store is None:
            return None
        try:
            found = await self._store.conversation_for(
                owner=owner, surface=self._surface, started_at=started_at, create=create
            )
            return _unless_hidden(found, include_hidden=False)
        except Exception as exc:  # noqa: BLE001
            _logger.error("conversation_for_failed", error=repr(exc))
            return None

    async def messages(
        self, conversation: Conversation, *, limit: int = 200
    ) -> list[TranscriptRecord]:
        """One conversation's messages, newest first. Empty on any failure —
        an assistant that has forgotten what was said beats a screen that will
        not open."""
        if self._store is None:
            return []
        try:
            return await self._store.read_conversation(
                conversation.conversation_id, owner=conversation.owner, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "conversation_read_failed",
                conversation_id=conversation.conversation_id,
                error=repr(exc),
            )
            return []

    async def append(self, conversation: Conversation | None, *, since: datetime) -> int:
        """Claim the messages a finished turn just wrote into this
        conversation.

        Called after the turn, not before, because the rows do not exist until
        the agent loop has written them. A failure here means the turn's
        messages stay unattached: they are in the transcript and the log view,
        and the next backfill pass at startup gives them a conversation of
        their own. Nothing is lost, and the reply the operator was waiting for
        still arrives.

        ``None`` — no conversation could be resolved or created — is a no-op
        for the same reason: the messages are recorded either way, and the
        operator is waiting for a reply, not for a filing decision.
        """
        if self._store is None or conversation is None:
            return 0
        try:
            return await self._store.attach_to_conversation(
                conversation.conversation_id,
                owner=conversation.owner,
                surface=conversation.surface,
                since=since,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "conversation_append_failed",
                conversation_id=conversation.conversation_id,
                error=repr(exc),
            )
            return 0

    async def hide(self, owner: Owner, conversation_id: str) -> bool:
        """Hide one of this owner's conversations. Nothing is destroyed.

        Returns ``False`` for "not yours", "not there", "already hidden" and
        "the store refused" — the same outcome from the caller's side, matching
        :meth:`delete`'s convention.

        This is what the owner's control on the chat screen calls. The
        conversation stays in the store, visible to an administrator with the
        moment and the account that hid it, and it ages out on exactly the
        retention schedule a visible one does. Hiding buys privacy from the
        list, not from the record, and it buys no extra lifetime.

        ``hidden_by`` is this owner's id: the only path here today is somebody
        clearing their own list. It is a separate field because an
        administrator hiding somebody else's conversation is the obvious next
        thing to want, and it should not need a migration.
        """
        if self._room_store is None:
            return False
        try:
            hidden = await self._room_store.hide_conversation(
                conversation_id, owner=owner, hidden_by=owner.id
            )
        except Exception as exc:  # noqa: BLE001 - see module docstring
            _logger.error(
                "conversation_hide_failed",
                conversation_id=conversation_id,
                error=repr(exc),
            )
            return False
        if hidden and self._layout is not None:
            self._remove_workspace(conversation_id)
        return hidden

    def _remove_workspace(self, conversation_id: str) -> None:
        """Best-effort: count what is there, remove it, log the count.

        Never raises and never changes what :meth:`hide` reports — a
        workspace that fails to remove is a cleanup problem for the sweep
        (:func:`personacore.workspaces.sweep`) to catch later, not a reason
        to tell the owner their conversation was not hidden.

        **Races a turn already in flight.** A turn that is still writing to
        this conversation's workspace when ``hide`` runs can recreate the
        folder after this method removes it. That is left alone rather than
        locked against: the retention sweep (``server.py``'s scheduled purge,
        every ``RETENTION_PURGE_INTERVAL_SECONDS`` — six hours) finds and
        removes it again on its next pass, the same as any other stray.
        """
        assert self._layout is not None  # noqa: S101 - only called when it is
        try:
            count = len(
                Workspace(
                    self._layout,
                    conversation_id,
                    max_file_bytes=_COUNT_MAX_FILE_BYTES,
                    max_workspace_bytes=_COUNT_MAX_WORKSPACE_BYTES,
                ).list()
            )
        except WorkspaceError:
            count = 0
        if _rmtree_workspace(self._layout, conversation_id):
            _logger.info(
                "conversation_workspace_removed",
                conversation_id=conversation_id,
                files=count,
            )

    async def rename(self, owner: Owner, conversation_id: str, title: str) -> bool:
        """Retitle one of this owner's conversations.

        ``title`` is trimmed and capped at
        :data:`~personacore.conversations.models.MAX_TITLE_LENGTH`. A title
        that is empty after trimming is **refused** and returns ``False``,
        never written as blank: a blank row is one an operator cannot click on
        with any confidence, which is the whole reason
        :data:`~personacore.conversations.models.UNTITLED` exists rather than
        an empty string.

        The cap is a hard cut here, not the word-boundary trim
        :func:`~personacore.conversations.models.derive_title` does. That one
        is summarising a message somebody wrote for another purpose; this is
        sixty-one characters somebody deliberately typed, and quietly dropping
        the last word of a name they chose reads as a bug.
        """
        if self._room_store is None:
            return False
        cleaned = title.strip()[:MAX_TITLE_LENGTH].strip()
        if not cleaned:
            return False
        try:
            return await self._room_store.rename_conversation(
                conversation_id, owner=owner, title=cleaned
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "conversation_rename_failed",
                conversation_id=conversation_id,
                error=repr(exc),
            )
            return False

    async def regroup(self, owner: Owner, conversation_id: str, group: str | None) -> bool:
        """Set or clear this conversation's group. ``None`` clears.

        A group typed as nothing but whitespace clears it too. "Ungrouped" and
        "filed under a name made of spaces" are the same intention, and storing
        the second would put a nameless heading in the sidebar that nothing can
        select.
        """
        if self._room_store is None:
            return False
        cleaned = group.strip()[:MAX_TITLE_LENGTH].strip() if group is not None else None
        try:
            return await self._room_store.set_conversation_group(
                conversation_id, owner=owner, group=cleaned or None
            )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "conversation_regroup_failed",
                conversation_id=conversation_id,
                error=repr(exc),
            )
            return False

    async def groups(self, owner: Owner) -> list[str]:
        """Every group name this owner has used, sorted, for the picker.

        Empty for a store that cannot file conversations and empty on failure,
        because the field is free text: no suggestions is a picker with no
        shortcuts, not a picker that refuses what you type.
        """
        if self._room_store is None:
            return []
        try:
            return await self._room_store.conversation_groups(owner=owner)
        except Exception as exc:  # noqa: BLE001
            _logger.error("conversation_groups_failed", error=repr(exc))
            return []

    async def delete(self, owner: Owner, conversation_id: str) -> int:
        """Delete one of this owner's conversations and its messages.

        Returns the number of messages removed; ``0`` covers "not yours",
        "not there" and "the store refused", which are the same outcome from
        the caller's side: nothing was deleted and nothing broke.

        **The chat screen no longer calls this.** Its control calls
        :meth:`hide`, because a household's conversations are kept for
        administrator review and clearing your own list must not destroy them.
        This stays, unchanged, for an administrator path that really does mean
        destroy.
        """
        if self._store is None:
            return 0
        try:
            return await self._store.delete_conversation(conversation_id, owner=owner)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "conversation_delete_failed",
                conversation_id=conversation_id,
                error=repr(exc),
            )
            return 0


__all__ = [
    "ConversationService",
    "ConversationStore",
    "PersonaAwareStore",
    "RoomStore",
    "RosterStore",
    "ThinkingAwareStore",
]
