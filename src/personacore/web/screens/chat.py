"""The Chat screen (spec section 9), and the front door that leads to it.

**This screen deliberately departs from the design canvas.** The design drew
Chat as "one message, one reply - a diagnostic, not a conversation"; the owner
asked for a conversation after seeing it, and then for the rest of what a chat
product is: prior conversations you can pick from, the persona (and so the
voice) chosen where you are talking rather than three screens away, dictation,
and the whole thing feeling immediate. That deviation is CLAUDE.md's "written
up and approved" kind, not drift, and the reasoning is below, beside the code
it explains.

**The shape of the screen.** Messages above, the box you type in pinned below
them, a rail of earlier conversations down the side. That is what every chat
product looks like and the reason is not fashion: the thing you are reading and
the thing you are typing into have to stay next to each other, and the newest
message has to be the one nearest the box.

**Three decisions about feeling fast**, because "snappy" is not a mood:

1. *A sent message appears at once.* The page draws the question itself the
   moment Send is pressed and marks it pending; the server's rendering of the
   same exchange replaces it when the reply lands. Nothing waits on a round
   trip to acknowledge a keystroke that already happened.
2. *A reply never redraws the page.* It is one fragment appended into the
   message list (``hx-swap="beforeend"``), so the sidebar, the composer, the
   persona picker and — the point — the scroll position are untouched. The
   list follows the newest message only when the reader was already at the
   bottom of it; somebody scrolled up reading yesterday is left where they are.
3. *Switching conversations replaces the messages, not the page.* Picking a
   thread swaps the message list and pushes the URL, so the composer keeps its
   focus and its contents and the browser never navigates. Without JavaScript
   the same control is an ordinary link to the same address.
4. *The reply is written as it is generated, and spoken as it is written.*
   ``/chat/stream`` reports one turn as server-sent events, so the words appear
   as the model produces them and each finished sentence goes to the voice
   while the rest is still being written — which is the whole of what the owner
   asked for and what the serial path could not do. The last frame is the same
   rendered exchange ``/chat/fragment`` answers with, so there is one template
   for one reply. A browser without ``fetch`` and ``ReadableStream`` takes the
   old path and gets the whole answer at once, which is what it always got.

**A conversation is a room, not a rolling transcript** (the chat-room contract,
sections 5 and 6). It has a name somebody chose and a group they filed it
under, every message says who said it -- a person, or a persona and the model
that spoke through it -- and it can be saved as a zip whose audio is
synthesised at the moment it is asked for. The controls for all of that are
where the conversation is: the persona picker moved down to the composer bar
beside save and delete, because that is where somebody talking to the assistant
is looking.

**Delete hides.** The owner's control calls ``hide()``; nothing is destroyed,
because a household's conversations are kept for administrator review. What the
owner is told is one line -- see :data:`CONVERSATION_DELETED` -- and every route
on this screen keeps the same promise: a hidden conversation is off the rail,
out of the messages, out of the history the model is given, out of the
download, and its URL answers exactly as an address that never named anything.
A 403-versus-404 difference would tell the owner it is still there and make the
button's wording a lie.

**This screen is assembled from six files, not one** (ADR-0040). What stayed
here is the screen itself: reading one operator's conversation back out of the
transcript, and drawing it. What moved out is what each piece owns --

``chat_thread``
    transcript rows as a conversation: who said what, the rail, the thread.
``chat_reply``
    one finished reply in the shape the message template renders.
``chat_exchange``
    running an exchange, and the path a browser without scripting takes.
``chat_streaming``
    the same exchange told as it happens, and the lifecycle that goes with it.
``chat_room``
    the controls around the messages: name, group, roster, voice, save, hide.

-- beside ``chat_voices`` (who speaks next), ``chat_audio`` (a reply's sound)
and ``chat_save`` (the zip), which were split out earlier. Every name any of
them owns is still importable from here, because this module is what the rest
of the package knows about, and this module still registers every route.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.admin.models import (
    AdminUser,
)
from personacore.audit.models import (
    AuditRecord,
    MessageRole,
    Owner,
    Surface,
    TranscriptRecord,
)
from personacore.config.settings import LLMRole
from personacore.conversations.models import (
    MAX_ROSTER,
    SESSION_GAP,
    UNTITLED,
    Conversation,
    ConversationKind,
    roster_of,
)
from personacore.conversations.service import ConversationService
from personacore.web.screens import chat_attachments, chat_audio, chat_image, chat_workspace
from personacore.web.screens import chat_exchange as exchanges
from personacore.web.screens import chat_room as room
from personacore.web.screens import chat_streaming as streaming
from personacore.web.screens import chat_voices as voices
from personacore.web.screens.chat_exchange import (
    CHAT_UNAVAILABLE,
    MAX_MESSAGE_CHARS,
    _asked_of,  # noqa: F401 - kept importable from this screen; see the note below
    _attributed_all,  # noqa: F401 - kept importable from this screen; see the note below
    _offer,  # noqa: F401 - kept importable from this screen; see the note below
    _takes_authorship,
)
from personacore.web.screens.chat_reply import (
    AUDIO_URL_PREFIX,
    MAX_TOOL_NAME_LENGTH,  # noqa: F401 - kept importable from this screen; see the note below
    MAX_TOOL_NAMES_SHOWN,
    TURN_METRICS_ACTION,  # noqa: F401 - kept importable from this screen; see the note below
    TURN_METRICS_CATEGORY,  # noqa: F401 - kept importable from this screen; see the note below
    TurnMetrics,  # noqa: F401 - kept importable from this screen; see the note below
    _audio_report_url,  # noqa: F401 - kept importable from this screen; see the note below
    _audio_url,  # noqa: F401 - kept importable from this screen; see the note below
    _latency,  # noqa: F401 - kept importable from this screen; see the note below
    _metrics_detail,  # noqa: F401 - kept importable from this screen; see the note below
    _metrics_fields,  # noqa: F401 - kept importable from this screen; see the note below
    _refused,  # noqa: F401 - kept importable from this screen; see the note below
    _tool_calls,  # noqa: F401 - kept importable from this screen; see the note below
    _tool_names,  # noqa: F401 - kept importable from this screen; see the note below
    chat_exchange,
)
from personacore.web.screens.chat_room import (
    CONVERSATION_DELETED,
    CONVERSATION_NOT_DELETED,
    GROUP_CLEARED,  # noqa: F401 - kept importable from this screen; see the note below
    GROUP_SAVED,  # noqa: F401 - kept importable from this screen; see the note below
    NAME_REFUSED,  # noqa: F401 - kept importable from this screen; see the note below
    NAME_SAVED,  # noqa: F401 - kept importable from this screen; see the note below
    NOTHING_TO_NAME,  # noqa: F401 - kept importable from this screen; see the note below
    PERSONA_NOT_REMEMBERED,
    PERSONA_SAVED,
    PERSONA_SCOPE_NOTE,
    PERSONA_UNCHANGED,
    ROOM_NOT_MANAGED,
    ROSTER_ADDED,
    ROSTER_ALREADY,
    ROSTER_FULL,
    ROSTER_NOT_HELD,
    ROSTER_NOTE,
    ROSTER_REMOVED,
    SPEECH_NOT_KEPT,
    SPEECH_OFF_HERE,
    SPEECH_ON_HERE,
    STOP_LABEL,
    STOP_TITLE,
    PersonaChoice,
    _back_to,  # noqa: F401 - kept importable from this screen; see the note below
    _chosen_persona,
    persona_choices,
)
from personacore.web.screens.chat_streaming import (
    _KEEPALIVE,  # noqa: F401 - kept importable from this screen; see the note below
    _PING,  # noqa: F401 - kept importable from this screen; see the note below
    KEEPALIVE_SECONDS,  # noqa: F401 - kept importable from this screen; see the note below
    _close_stream,  # noqa: F401 - kept importable from this screen; see the note below
    _frame,  # noqa: F401 - kept importable from this screen; see the note below
    _kept_alive,  # noqa: F401 - kept importable from this screen; see the note below
    _RunningTurn,  # noqa: F401 - kept importable from this screen; see the note below
    _Spoken,  # noqa: F401 - kept importable from this screen; see the note below
    _TurnHolding,  # noqa: F401 - kept importable from this screen; see the note below
    running_turn,
    stop_turn,  # noqa: F401 - kept importable from this screen; see the note below
)
from personacore.web.screens.chat_thread import (
    ASSISTANT_UNATTRIBUTED,
    CHAT_AUDIT_WINDOW,
    CHAT_HISTORY_MESSAGES,
    CHAT_TRANSCRIPT_WINDOW,
    CONVERSATION_LIST_LIMIT,
    CONVERSATION_SCAN_WINDOW,
    CONVERSATIONS_SHOWN,
    MAX_AUTHOR_CHARS,
    UNGROUPED_HEADING,
    ChatHistoryMessage,
    ConversationRow,
    RailGroup,
    _grouped,  # noqa: F401 - kept importable from this screen; see the note below
    _local_day,  # noqa: F401 - kept importable from this screen; see the note below
    _metrics_for,  # noqa: F401 - kept importable from this screen; see the note below
    _my_name,
    _plain,  # noqa: F401 - kept importable from this screen; see the note below
    _replayed,  # noqa: F401 - kept importable from this screen; see the note below
    _room,
    _spoken,  # noqa: F401 - kept importable from this screen; see the note below
    author_label,
    bulk_selected_rows,
    conversation_history,
    conversation_rows,
    conversation_start,
    participants,
    railed,
    thread_identity,
    thread_records,
    transcript_exchanges,
    wanted_conversation,
)
from personacore.web.screens.profile import (
    AUTOPLAY_DEFAULT,
    AUTOPLAY_PREFERENCE,
    override_from,
)
from personacore.web.shared import (
    RAIL_COLLAPSED_PREFERENCE,
    UIContext,
    current_config,
    group_collapsed_preference,
)

# The imports above carry names this screen no longer implements. That is
# deliberate and it is the rule the split was done under (ADR-0040): everything
# importable from ``chat`` before it is still importable from ``chat`` after,
# so no caller -- another screen, a test, a script -- had to learn where a
# function moved to. The ones marked ``noqa`` are re-exported and nothing more.

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Chat — a conversation, not a single diagnostic turn
# ---------------------------------------------------------------------------
#
# **Where the conversation lives.** Nowhere in this process. Every turn is
# already written to the transcript store by the agent loop (ADR-0004), so the
# messages on the screen are *read back out of that store* rather than
# accumulated in a dictionary keyed by something. Two reasons, and the second is
# the one that decided it: a second store of the same sentences is a second
# thing to expire, purge and get wrong under spec §7's retention rules, and
# server-side session state would silently disagree with the transcript the
# moment the process restarted — leaving the log view and the chat screen
# telling two different stories about the same conversation.
#
# **What identifies a conversation** is therefore an instant: the ISO-8601
# moment it began. Its messages are this operator's admin-surface transcript
# rows from that instant onward, cut at the first silence longer than
# ``SESSION_GAP``. Opening the screen with no conversation named mints "now",
# which is an empty conversation nobody has spoken in yet; the rail lists the
# earlier ones by grouping the same rows the same way.
#
# Nothing about **whose** transcript is read comes from the request. The owner
# is derived from ``require_user`` on the server, so the worst a tampered
# ``conversation`` field can do is widen an operator's window onto their own
# admin transcript — which they can already read in full at ``/admin/logs``.

MIC_DISCLOSURE = (
    "Dictation uses your browser's own speech recogniser, which sends what you "
    "say to Google — nothing else on this page leaves the house."
)
"""The one sentence beside the microphone, and it is not decoration.

The sidebar footer of this application says "nothing leaves the house without
permission". While the microphone is on that is **false**: the Web Speech API
in every browser that ships it streams the audio to the vendor's servers to be
transcribed. The owner chose that recogniser knowingly, so the feature stays — but
the claim has to be true at the same time, which means the control says where
the audio goes, in the place somebody is looking when they press it. It is
repeated, louder, in the badge that appears while the microphone is live.
"""

MIC_LISTENING = "Listening — your audio is going to Google."
"""What the live badge says while the recogniser is running. Shorter than
:data:`MIC_DISCLOSURE` because it is read at a glance, mid-sentence, and
must not be a paragraph."""

BULK_DELETE_NOTHING_SELECTED = "Nothing was selected, so nothing was deleted."
"""The bulk-delete form posted with no row checked and "Select all" left
off — a real outcome, not an error to dress up. With scripting off the
Delete button has no reason to hide itself, so pressing it with nothing
picked is an ordinary thing to do once."""

BULK_DELETE_NAMED_LIMIT = 8
"""How many conversations the confirmation page, and the result that follows
it, will name by title. Past this both say only how many — the same bound
the contract's own §5 rule 1 puts on the confirmation page, reused for the
result rather than invented a second time
(docs/contracts/conversation-list-bulk-actions.md)."""

BULK_DELETE_REASON = "already gone, or not yours"
"""Why one selected conversation was not deleted, worded exactly as
non-committally as :data:`~personacore.web.screens.chat_room.
CONVERSATION_NOT_DELETED`. "Already hidden", "never existed" and "somebody
else's" are one outcome from the selector's own side, and naming which of
the three it was would be the disclosure §6 already refuses for a single
delete."""



@dataclass(frozen=True, slots=True)
class ChatView:
    """The reads and renderings every chat route shares, written down.

    One conversation is read the same way whoever is asking -- the whole
    screen, a thread switch, a turn about to run, a control that has just saved
    something -- and until ADR-0040 that was true because all of them were
    closures in one ``register``. They are in four files now, so the joint
    between them is this object: built once here, handed to each module, and
    never rebuilt.

    Written down rather than passed as loose arguments, for the reason the team
    rules give: a joint that is not written down before the split is one two
    implementers will each invent a shape for. Every field below was a closure
    of the same name, and none of them changed on the way out.
    """

    ctx: UIContext
    """Everything the factory built once -- templates, the audit gateway, the
    persona store, ``require_user``, the preference store, the runner."""

    conversations: ConversationService
    """Conversations over whatever audit store this core was assembled with."""

    streamed_rooms_possible: bool
    """Whether the *streaming* runner can be told not to record the person's
    message. Asked once at registration, because a runner does not change shape
    between requests, and asked separately from the plain one because they are
    separate objects and one may be older than the other."""

    screen: Any
    """Everything ``chat.html`` renders, for one operator and one thread."""

    visible: Any
    """This operator's transcript with the hidden conversations taken out."""

    thread_rows: Any
    """The messages of the thread being spoken in, oldest first."""

    looked_at: Any
    """The conversation a screen is reading, without creating one."""

    labels: Any
    """Every installed persona's display name, by persona name."""

    members: Any
    """Who is in this room, in order."""

    cap: Any
    """How many turns one exchange may run, as this core is configured."""

    speaks: Any
    """Whether a reply in this conversation reads itself aloud."""

    context_length: Any
    """The interactive model's own context window, asked fresh of the backend
    — ``None`` when it cannot be known (spec: the token counter). Async, like
    ``thread_rows``, and read per turn for the same reason ``cap`` is: the LLM
    host can be repointed while somebody is mid-conversation (ADR-0010)."""

    exchange_fragment: Any
    """One send's worth of messages, plus the rail if they changed it."""


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the front door, the Chat page and the fragments it swaps."""
    templates = ctx.templates
    audit = ctx.audit
    chat = ctx.chat
    personas = ctx.personas
    require_user = ctx.require_user
    _shell = ctx.shell

    def _rail_view(
        request: Request, rail: Sequence[ConversationRow] | None
    ) -> dict[str, Any]:
        """The rail, and which parts of it this person has folded away.

        The one place the rail's shape is decided, because the rail is rendered
        from three different responses — the whole screen, a thread switch, and
        the out-of-band swap that rides along with a reply — and a fold that
        only two of them knew about would spring open the moment somebody said
        something.

        Read from the preference store rather than restored by a script, for
        the reason the sidebar's is (``shared.py``): the collapsed rail has to
        be in the markup, not applied to it afterwards.

        ``collapsed_groups`` holds the *names*, with ``""`` standing for the
        ungrouped bucket. A group named ``""`` cannot exist — ``regroup`` clears
        a name made of whitespace rather than storing one — so the sentinel
        cannot be typed. The preference keys themselves do not use it: they
        have a separate key for ungrouped, because a preference name is not
        protected by what a template happens to know.

        Reads are dict lookups on an in-memory table (ADR-0030), so this stays
        an ordinary function even on the streaming path.
        """
        user = require_user(request)
        groups = [] if rail is None else railed(rail)
        collapsed = [
            group.name or ""
            for group in groups
            if ctx.preferences.get_bool(
                user.door, user.id, group_collapsed_preference(group.name)
            )
        ]
        return {
            "conversations": rail,
            "conversation_groups": None if rail is None else groups,
            "rail_collapsed": bool(
                ctx.preferences.get_bool(user.door, user.id, RAIL_COLLAPSED_PREFERENCE)
            ),
            "collapsed_groups": collapsed,
            "conversations_shown": CONVERSATIONS_SHOWN,
        }

    def _browser_dictation_enabled() -> bool:
        """Whether the administrator has switched browser dictation on
        (``[dictation] browser``, PLAN.md's "Next — Speech to text, in the
        container").

        Off by default, and a household fact rather than a per-person choice —
        so this decides whether the microphone control and its disclosure
        exist in the markup **at all**, not whether they are merely hidden.
        Read here, server-side, rather than left to ``chat.js``: a control
        absent from the response cannot be revealed by a script, however that
        script is reached.

        A settings file that cannot be read is treated the same as "off": a
        broken ``core.toml`` must not be the thing that starts sending a
        household's audio to Google.
        """
        current, _unreadable = current_config(ctx.layout)
        if current is None:
            return False
        section = current.settings.get("dictation")
        return bool(section.get("browser")) if isinstance(section, dict) else False

    # Conversations, over whatever audit store this core was assembled with.
    # The service reports whether that store knows about them and swallows
    # every failure below that (see its module docstring), so this screen
    # neither has to ask twice nor has to catch: a store without them leaves
    # every call below answering "nothing", and the gap-rule grouping in
    # :func:`_grouped` carries the screen exactly as it did before.
    # `layout` is passed so `hide()` takes the conversation's workspace with
    # it (workspace contract §2). `ConversationService` is not built in
    # `server.py`/`__main__.py` — it never has been; every screen that needs
    # one builds it locally over `ctx.audit`, this one included — so this is
    # the whole of the wiring, not half of it waiting on a change elsewhere.
    conversations = ConversationService(audit, surface=Surface.ADMIN_UI, layout=ctx.layout)

    # -- reading the conversation -----------------------------------------

    async def _rooms(user: AdminUser) -> tuple[dict[str, Conversation], frozenset[str]]:
        """What the store says about this operator's conversations: the visible
        ones by id, and the ids of the hidden ones.

        One query for both halves. The hidden set is asked for **because the
        screen has to leave them out itself**: the transcript rows of a hidden
        conversation are still in the store — they have to be, an administrator
        reviews through them (§7.2) — so a rail built by grouping rows would
        keep showing a thread the owner asked to have gone, and its URL would
        keep opening it.

        ``include_hidden`` is only asked of a store that says it manages rooms.
        A store that cannot hide has nothing hidden to name, so the question
        would be a keyword an older store does not take, for an empty answer.
        """
        if not conversations.available:
            return {}, frozenset()
        rows = await conversations.listing(
            Owner.profile(user.id),
            limit=CONVERSATION_LIST_LIMIT,
            include_hidden=conversations.manages_rooms,
        )
        known: dict[str, Conversation] = {}
        hidden: set[str] = set()
        for row in rows:
            if getattr(row, "hidden_at", None) is not None:
                hidden.add(row.conversation_id)
            else:
                known[row.conversation_id] = row
        return known, frozenset(hidden)

    async def _visible(
        user: AdminUser,
    ) -> tuple[list[TranscriptRecord], dict[str, Conversation]]:
        """This operator's transcript with the hidden conversations taken out,
        and what the store knows about the ones that are left.

        The one read every rendering path goes through, so there is exactly one
        place a hidden conversation is removed from what the owner is shown. A
        second check somewhere else could disagree with this one, and the
        direction it would disagree in is a conversation the owner deleted
        still being on their screen.
        """
        known, hidden = await _rooms(user)
        records = await _recent(user)
        if not hidden:
            return records, known
        return [row for row in records if row.conversation_id not in hidden], known

    async def _recent(user: AdminUser) -> list[TranscriptRecord]:
        """This operator's admin-surface transcript, oldest first.

        One query serves both the rail and the open thread — they are two
        groupings of the same rows, and asking twice would let them disagree.

        A read that fails is not allowed to take the screen down: an empty rail
        and an empty conversation are what a new operator sees anyway, and
        losing the whole chat screen because the log store hiccuped would be
        worse than losing the list.
        """
        try:
            records = await audit.query_transcript(
                owner=Owner.profile(user.id),
                surface=Surface.ADMIN_UI,
                limit=CONVERSATION_SCAN_WINDOW,
            )
        except Exception:  # noqa: BLE001 - an empty rail beats a dead screen
            return []
        return sorted(records, key=lambda record: record.timestamp)

    async def _thread_rows(
        user: AdminUser, started: datetime | None
    ) -> list[TranscriptRecord]:
        """The messages of the thread being spoken in, oldest first.

        Scoped by three things, and only the last of them comes from the
        request: this operator (``require_user``, never the form), the admin
        surface, and the instant the page says the conversation began.

        For a conversation opened a moment ago that is one narrow query. For a
        conversation being *resumed* it is not: everything said since yesterday
        evening includes this morning's unrelated thread, and handing the model
        that is how an assistant starts answering the wrong question — so a
        resumed thread is cut to its own rows first.

        A hidden conversation's messages are left out here as well as on the
        screen. They would otherwise reach the owner the long way round — read
        back to a model that is about to answer them — which is the same
        disclosure with an extra step in it.

        A read that fails is not allowed to take the screen down: the turn
        still runs, without memory of what came before, which is the same
        degradation an operator sees on their first message.

        **Rows rather than history**, since a room needs to know *who* said
        each message: another persona's reply is untrusted content to the one
        reading it (§9), and a list of role-and-text pairs has thrown away the
        one fact that decides which. :func:`_thread_history` is this, flattened,
        for the single-persona case that never needed to know.
        """
        if started is None:
            return []
        if datetime.now(UTC) - started < SESSION_GAP:
            try:
                records = await audit.query_transcript(
                    owner=Owner.profile(user.id),
                    surface=Surface.ADMIN_UI,
                    since=started,
                    limit=CHAT_TRANSCRIPT_WINDOW,
                )
            except Exception:  # noqa: BLE001 - a forgetful turn beats a dead screen
                return []
            _known, hidden = await _rooms(user)
            if hidden:
                records = [row for row in records if row.conversation_id not in hidden]
            return [
                row
                for row in sorted(records, key=lambda record: record.timestamp)
                if row.role in (MessageRole.USER, MessageRole.ASSISTANT)
            ]
        records, _known = await _visible(user)
        return thread_records(records, started)

    async def _thread_history(
        user: AdminUser, started: datetime | None
    ) -> list[ChatHistoryMessage]:
        """The same thread as plain history, for the single-persona turn.

        ``conversation_history`` reads newest-first, as the store returns rows;
        these are oldest-first, so they are reversed on the way in rather than
        the function being taught a second order.
        """
        rows = await _thread_rows(user, started)
        return conversation_history(list(reversed(rows)))

    async def _turn_audit(rows: Sequence[TranscriptRecord], user: AdminUser) -> list[AuditRecord]:
        """The audit rows this thread's own turns wrote, so a replayed exchange
        can say what the model called and how long each call took — exactly
        what the Logs screen already reads back for the same reason
        (``logs.log_exchanges``).

        Bounded to the thread's own span rather than pulled with the rest of
        the operator's history: a correlation id belongs to exactly one of
        this thread's exchanges, and its audit rows land between the same two
        timestamps that bound it.

        A read that fails costs the tool line only, the same degradation every
        other read on this screen already chooses: an exchange with no tools
        shown here reads exactly like one that made no tool call.
        """
        if not rows:
            return []
        try:
            return await audit.query_audit(
                owner=Owner.profile(user.id),
                surface=Surface.ADMIN_UI,
                since=rows[0].timestamp,
                until=rows[-1].timestamp,
                limit=CHAT_AUDIT_WINDOW,
            )
        except Exception:  # noqa: BLE001 - a tool-less line beats a dead screen
            return []

    async def _turn_reasoning(
        rows: Sequence[TranscriptRecord], user: AdminUser
    ) -> dict[str, str]:
        """This thread's own reasoning, by correlation id — so a replayed
        reply draws the same collapsed line it had while streaming
        (``chat_thread._fill_reply``).

        Not read off ``audit_rows``: reasoning is kept in its own table
        (:class:`~personacore.audit.models.ReasoningRecord`, migration 0007),
        because it is conversation content and not the action metadata
        ``AuditRecord.detail`` is documented to be, and because it is large —
        every read of the tool-calls window this screen already makes would
        otherwise drag kilobytes of thinking behind it whether or not anybody
        had expanded the line. Bounded to the thread's own span for the exact
        reason :func:`_turn_audit` is: a correlation id belongs to exactly one
        exchange, and its reasoning, when there is any, was written between
        the same two timestamps.

        A read that fails costs the replayed thinking line only, the same
        degradation :func:`_turn_audit` already chooses for tool calls.
        """
        if not rows:
            return {}
        try:
            found = await audit.list_reasoning(
                owner=Owner.profile(user.id),
                surface=Surface.ADMIN_UI,
                since=rows[0].timestamp,
                until=rows[-1].timestamp,
                limit=CHAT_AUDIT_WINDOW,
            )
        except Exception:  # noqa: BLE001 - a thinking-less line beats a dead screen
            return {}
        return {record.correlation_id: record.text for record in found}

    async def _attach_replay_audio(request: Request, built: Sequence[dict[str, Any]]) -> None:
        """Give each replayed reply its resynthesis address, or none at all.

        Only a message read back out of the transcript needs this — a reply
        that just landed already carries real audio from
        ``chat_audio.speaker``'s own offer. Checked at render time, every time,
        because "the persona at the time" (``chat_thread``'s own rule) can
        since have lost its voice or been deleted outright, and a player drawn
        for it would either 409 the moment it is pressed or, worse, answer in
        whoever holds that name today — which is the one thing a replayed
        message may never do.

        Resolved once per persona *name* rather than once per message: a long
        conversation held with one persona asks this once, and the answer
        cannot vary by which of that persona's lines is asking.
        """
        resolved: dict[str, tuple[bool, str | None]] = {}
        for entry in built:
            if not entry.get("replayed") or not entry.get("ok"):
                continue
            name = entry.get("reply_persona_name")
            correlation_id = entry.get("correlation_id")
            if not name or not correlation_id:
                continue
            if name not in resolved:
                resolved[name] = await chat_audio.replay_speakable(
                    request, author_name=name, personas=personas
                )
            can_speak, reason = resolved[name]
            entry["audio_url"] = (
                f"{chat_audio.REPLAY_URL_PREFIX}{correlation_id}.wav" if can_speak else None
            )
            entry["voice_note"] = reason

    async def _attach_replay_attachments(user: AdminUser, built: Sequence[dict[str, Any]]) -> None:
        """Turn a replayed message's attachment ids back into chips.

        The ids come off ``chat_thread._fill_reply``, read from the same
        :data:`~personacore.web.screens.chat_attachments.ATTACHMENTS_ACTION`
        record ``chat_attachments.store_pending`` wrote at send time. This is
        the only place that hydration can happen: it needs the audit store,
        which ``chat_thread`` deliberately does not hold (its own module
        docstring — "none of it needs a request, a store or a runner").
        Scoped to this operator's own attachments, exactly as every other
        read on this screen is: a replayed conversation is always this
        operator's own (``_visible``/``_recent``), never somebody else's.
        """
        owner = Owner.profile(user.id)
        for entry in built:
            ids = entry.pop("attachment_ids", None)
            if not ids:
                continue
            entry["attachments"] = await chat_attachments.chips_for_ids(
                ctx, owner=owner, ids=ids
            )

    def _attach_replay_workspace_files(
        found: Conversation | None, built: Sequence[dict[str, Any]]
    ) -> None:
        """Turn a replayed reply's workspace file names back into cards —
        workspace contract §7.

        The names come off ``chat_thread._fill_reply``, read from the same
        :data:`~personacore.web.screens.chat_workspace.WORKSPACE_FILES_ACTION`
        record :mod:`chat_streaming` wrote when the reply landed. Needs the
        conversation's own id, not the correlation id ``chat_thread`` matched
        by — a card's download link is scoped by conversation
        (``chat_workspace.chip_for``), and ``found`` is the one place this
        screen already has it (the same reason it is handed to
        ``_attach_replay_images`` beside this).

        ``found`` is ``None`` for a brand new conversation with nothing
        looked at yet — nothing to hydrate either way, so this is a no-op
        rather than a guess at an id.
        """
        if found is None:
            return
        pinned = None
        for entry in built:
            names = entry.pop("workspace_files", None)
            if not names:
                continue
            # Read once for the whole reload, not once per exchange — a
            # conversation with several tool-fetched files is the ordinary
            # case, and the pin list does not change while this page is
            # being drawn. Contract §13's own joint; see
            # `chat_workspace.pinned_names_for`'s own docstring for why a
            # core that has not landed `Workspace.pinned()` yet still
            # answers, just with nothing pinned.
            if pinned is None:
                pinned = chat_workspace.pinned_names_for(ctx.layout, found.conversation_id)
            entry["workspace_files"] = chat_workspace.chips_for_names(
                found.conversation_id, names, pinned=pinned
            )

    def _attach_replay_images(
        built: Sequence[dict[str, Any]], conversation: Conversation | None
    ) -> None:
        """Turn a replayed image-conversation reply back into the picture it
        was — image-conversations.md contract §7 ("closing it and coming back
        shows the pictures again"). Gated on ``conversation.kind`` rather than
        tried unconditionally: the convention
        :mod:`~personacore.web.screens.chat_image` reads back (an
        assistant row's whole content is its picture's own serve URL) only
        holds for this one kind — trying it on an ordinary reply would draw a
        model's own words as a broken image tile the one time they happened to
        look like a URL.
        """
        if conversation is not None and conversation.kind is ConversationKind.IMAGE:
            chat_image.hydrate_replayed_images(built)

    # -- rendering ---------------------------------------------------------

    def _autoplay_for(request: Request) -> bool:
        """Whether this person's reply speaks itself — ADR-0030's resolution
        order, asked once per rendered turn.

        Keyed on the door as well as the id: the same name arriving through two
        different doors is two different operators, not one (see
        ``AdminUser.door``).
        """
        me = require_user(request)
        return ctx.preferences.resolve_bool(
            me.door,
            me.id,
            AUTOPLAY_PREFERENCE,
            override=override_from(request),
            default=AUTOPLAY_DEFAULT,
        )

    def _exchange_fragment(
        request: Request,
        exchanges: Sequence[dict[str, Any]],
        *,
        rail: list[ConversationRow] | None = None,
        conversation: str | None = None,
        speaking_in: Conversation | None = None,
        autoplay: bool | None = None,
    ) -> HTMLResponse:
        """One send's worth of messages, plus the rail if they changed it.

        Usually one exchange, which is what a conversation with one persona in
        it produces and what this answered with before rooms existed. A room
        with several personas answers with several, in the order they spoke,
        and they are appended to the message list in that order — the whole
        reason this takes a list rather than being called once per reply is the
        **no-JavaScript path**, where the browser gets one response for one form
        post and a second persona's answer would otherwise have nowhere to go.

        The rail rides along as an out-of-band swap rather than as a second
        request: a first message names its conversation and reorders the list,
        and a list that only caught up on the next full page load would be a
        thread you had just started being missing from the threads you have.

        ``speaking_in`` is the conversation these exchanges were said in — the
        actual row, not the ``conversation`` string above, which is only ever
        the instant printed into the markup. It is what decides whether a
        reply that autoplays does so at all: see ``autoplay``, below.
        ``speaking_in=None`` is a real answer, not a missing one — a screen
        opened with nothing said in it yet — and it is treated exactly as
        :func:`_muted` treats it: nothing to be switched off.

        ``autoplay`` overrides what ``speaking_in`` would otherwise decide, and
        has exactly one caller: a streamed turn that was **already read aloud
        while it arrived** passes ``False``, because a reply that speaks itself
        twice is worse than one that never spoke at all. The player is still
        there to press.

        Left as ``None``, the default used to be this person's setting alone —
        which was the bug (voice off for a room silenced whoever answered
        first, since the *live* path reads ``_speaks`` and stops them, and came
        back on for whoever answered next, since the *rendered* reply never
        asked the conversation at all). It is ``_speaks`` now, the same
        function and the same rule the live path already reads: the person's
        setting, and then this conversation's switch, narrower wins.
        """
        return templates.TemplateResponse(
            request=request,
            name="fragments/chat_exchange.html",
            context={
                "exchanges": list(exchanges),
                **_rail_view(request, rail),
                "conversation": conversation,
                # ADR-0030 and §6.2 — resolved per request, because the
                # administrator can change the household rule, or somebody in
                # this room can flip its own switch, while a reply is still
                # rendering.
                "speech_autoplay": _speaks(request, speaking_in) if autoplay is None else autoplay,
            },
        )

    async def _looked_at(
        user: AdminUser, started: datetime | None
    ) -> Conversation | None:
        """The conversation a screen is *reading*, without creating one.

        ``create=False``: looking at a screen is not speaking into it, and
        minting a conversation per page load would fill the rail with threads
        nobody opened. One call because everything the screen wants off the row
        — who answers, who else is in the room, whether this thread's replies
        speak themselves — comes from the same row, and three separate reads
        could disagree.
        """
        if started is None:
            return None
        return await conversations.at(Owner.profile(user.id), started, create=False)

    def _muted(request: Request, conversation: Conversation | None) -> bool:
        """§6.2 — whether this conversation's replies have been silenced.

        A preference on the person, keyed by the conversation, so it is
        resolved out of the same table and by the same rule as the autoplay
        setting it defers to (ADR-0030). It can only ever silence: where the
        two disagree the narrower one wins, and autoplay off for the person
        means silence everywhere whatever a conversation says.

        A conversation that does not exist yet — the screen has been opened and
        nothing has been said — has nothing switched off, which is the same
        answer a new thread has always given.
        """
        key = voices.muted_preference(
            str(getattr(conversation, "conversation_id", "") or "")
        )
        if key is None:
            return False
        me = require_user(request)
        return bool(ctx.preferences.get_bool(me.door, me.id, key))

    def _speaks(request: Request, conversation: Conversation | None) -> bool:
        """Whether a reply in this conversation reads itself aloud.

        The person's own setting (ADR-0030) and then this conversation's
        switch, in that order, because §6.2 says the narrower one wins.
        """
        return _autoplay_for(request) and not _muted(request, conversation)

    # -- the room: who is in it, and who speaks next -----------------------
    #
    # The many-voices contract. Everything that *decides* lives in
    # ``chat_voices``; what is here is the part that needs a request, a store
    # or a runner. The one rule worth repeating at the top of it (§7): a
    # conversation with one persona in it must behave exactly as it did before
    # any of this existed — no extra model call, no addressing check that can
    # decline to answer, no added latency. That is not achieved by care, it is
    # achieved by ``Exchange.solo`` taking a different path.

    #: Whether this core can hold a room at all. A second character's turn must
    #: not write the person's message into the transcript a second time — the
    #: screen would draw one question as several — so a runner that cannot be
    #: told to skip it gets one persona per send. Asked once, at registration,
    #: because a runner does not change shape between requests.
    _rooms_possible = chat is not None and _takes_authorship(chat)
    _streamed_rooms_possible = _rooms_possible and _takes_authorship(
        getattr(chat, "stream", None)
    )

    def _labels() -> dict[str, str]:
        """Every installed persona's display name, by persona name.

        Read through :func:`persona_choices`, which is what the picker uses, so
        the name that summons a character and the name printed over its reply
        are the same string and cannot drift.
        """
        return {
            choice.name: choice.label
            for choice in persona_choices(personas, default=personas.default_persona)
        }

    def _members(
        conversation: Conversation | None, labels: Mapping[str, str]
    ) -> list[voices.RoomMember]:
        """Who is in this room, in order (§2).

        The persona the picker names, then everybody added to it. A store that
        knows nothing about conversations hands back ``None`` and the room is
        the core's default persona on its own — which is the single-persona
        conversation this screen has always had.
        """
        roster = roster_of(conversation, default=personas.default_persona)
        found = voices.members(labels, roster)
        if found and not _rooms_possible:
            # A runner too old to be told whose message this is. The room is
            # the persona that answers first and nobody else — a smaller
            # feature, never a duplicated message, and never a failed turn.
            found = found[:1]
        # A core with no default persona configured at all: the roster is
        # empty and nobody could speak. One unnamed member instead, so the turn
        # still runs and the agent loop resolves the profile's persona exactly
        # as it did before rooms existed. An empty room would be a conversation
        # that silently cannot answer.
        return found or [
            voices.RoomMember(
                name=personas.default_persona, display=personas.default_persona
            )
        ]

    def _cap() -> int:
        """§4.3's hard cap, as this core is configured.

        Read per exchange rather than captured, for the reason every other
        setting on this screen is read per request: an administrator can change
        it while somebody is mid-conversation (ADR-0010).
        """
        current, _unreadable = current_config(ctx.layout)
        return voices.max_persona_turns(None if current is None else current.settings)

    async def _context_length() -> int | None:
        """The interactive model's own context window, or ``None``.

        Structural on ``ctx.llm`` the same way ``model_listing._role_health_source``
        is: a source with no ``role_views`` — a bare client, a test double — is
        still a legitimate one (``LLMHealthSource``'s own docstring), it just
        has nothing this can ask, so it answers ``None`` and the count on
        screen stands alone rather than against a guess.

        Read per request rather than cached, the same reason ``_cap`` beside it
        is: the LLM host can be repointed mid-conversation (ADR-0010), and a
        cached ceiling would show yesterday's model's limit under today's
        replies.
        """
        views = getattr(ctx.llm, "role_views", None)
        source: Any = ctx.llm
        if views is not None:
            source = next(
                (view for view in views() if view.role == LLMRole.INTERACTIVE.value),
                ctx.llm,
            )
        probe = getattr(source, "context_length", None)
        if probe is None:
            return None
        try:
            return await probe()
        except Exception:  # noqa: BLE001 - an unlabelled count beats a dead screen
            return None

    def _thinking_here(conversation: Conversation | None, answering: str) -> bool:
        """Whether the Thinking checkbox in the gear sheet reads checked —
        thinking contract §13 D.

        The conversation's own override wins when it has one; otherwise the
        answering persona's own switch. ``getattr`` both ways: ``thinking``
        on :class:`~personacore.conversations.models.Conversation` and
        ``thinking_enabled`` on a loaded persona are the same core joint
        (``working/contracts/workspace.md`` §13) this screen builds against
        before the other half of the contract has necessarily landed either
        one — absent means "no override" for the first and "on" for the
        second, which is also what each already means once landed.
        """
        override = getattr(conversation, "thinking", None)
        if override is not None:
            return bool(override)
        try:
            loaded = personas.load(answering)
        except Exception:  # noqa: BLE001 - an unreadable persona still gets a checkbox
            return True
        return getattr(loaded, "thinking_enabled", True)

    def _roster_view(
        request: Request, conversation: Conversation | None
    ) -> dict[str, Any]:
        """The room controls in the composer bar: who else is here, and the
        voice switch.

        One place because both are rendered by the whole page *and* by the
        fragment that swaps when another thread is opened — a roster that only
        caught up on a full page load would name the previous conversation's
        characters over this one's messages.

        ``roster`` is everybody after the first: the picker beside it already
        names who answers, and printing that name twice would make the two look
        like different controls for different things.
        """
        labels = _labels()
        here = _members(conversation, labels)
        return {
            "roster": here[1:],
            "roster_note": ROSTER_NOT_HELD if not conversations.holds_a_roster else ROSTER_NOTE,
            "roster_full": len(here) >= MAX_ROSTER,
            "roster_choices": [
                choice
                for choice in persona_choices(personas, default="")
                if choice.name not in {member.name for member in here}
            ],
            "holds_a_roster": conversations.holds_a_roster,
            "speech_here": not _muted(request, conversation),
            "speech_here_note": (
                SPEECH_ON_HERE if not _muted(request, conversation) else SPEECH_OFF_HERE
            ),
            "speech_switchable": conversation is not None,
            "stop_label": STOP_LABEL,
            "stop_title": STOP_TITLE,
            # Thinking contract §13 D. `here[0].name` is who answers first —
            # the same persona the picker names — read straight off the room
            # `_members` just built rather than a second resolution of it.
            "thinking_here": _thinking_here(conversation, here[0].name),
            "thinking_switchable": conversation is not None,
        }

    async def _screen(
        request: Request,
        *,
        wanted: str | None,
        note: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Everything ``chat.html`` renders, for one operator and one thread."""
        user = require_user(request)
        now = datetime.now(UTC)
        started = conversation_start(wanted_conversation(wanted))
        opened = started or now
        records, known = await _visible(user)
        rows = thread_records(records, opened)
        identity = thread_identity(rows, opened.isoformat())
        mine = _my_name(records, user)
        rail = conversation_rows(records, active=identity, now=now, known=known)
        # The picker shows this *conversation's* persona, falling back to the
        # core's default for a thread that has not chosen one. Reopening
        # yesterday's thread with a chosen persona therefore shows that same
        # persona, which is the same answer the next turn will get.
        found = await _looked_at(user, started)
        chosen = _chosen_persona(found)
        built = transcript_exchanges(
            rows,
            human=mine,
            audit_rows=await _turn_audit(rows, user),
            reasoning_by_correlation=await _turn_reasoning(rows, user),
            context_limit=await _context_length(),
        )
        await _attach_replay_audio(request, built)
        await _attach_replay_attachments(user, built)
        _attach_replay_images(built, found)
        _attach_replay_workspace_files(found, built)
        return {
            **await _shell(request, "chat"),
            **_room(rail, rows, mine),
            **_rail_view(request, rail),
            **_roster_view(request, found),
            "conversation": opened.isoformat(),
            # THE SIDEBAR'S COLLAPSE TOGGLE IS A CONTROL THAT NAMES THE OPEN
            # THREAD, and it was the one nobody counted. It posts `here` — the
            # address of the page it was pressed on — and lands the browser
            # back there. But this screen's address only carries ?c= once a
            # rail row has been clicked: opened from the menu, from a bookmark
            # or in a new tab it is bare /admin/chat, which is the "New
            # conversation" address. So folding the menu threw the thread away
            # and opened an empty one — the defect the owner reported on
            # v0.12.0: collapsing the sidebar's chat menu bar changed the open
            # conversation to a new one.
            #
            # The page therefore names itself WITH its thread rather than as
            # the browser happens to have reached it. Encoded here for the same
            # reason `_back_to` encodes: an ISO instant's "+" is a space in a
            # query string.
            # Whether a reply is still being written in this thread right now
            # (detached-turns contract §3/§6). A turn no longer belongs to the
            # connection that started it, so a page opened while one is running
            # — a tablet coming back from sleep, a second device, a plain
            # reload — would otherwise draw a conversation that looks finished
            # and sit there while the answer arrived somewhere nobody was
            # looking. `chat.js` reads this off the composer and attaches.
            # There is no control here and nothing to press: §6 asks for it not
            # to be broken any more, not for a Resume button.
            "turn_running": running_turn(request, user.id, opened.isoformat()),
            "here": f"/admin/chat?c={quote(opened.isoformat())}",
            "active": identity,
            "exchanges": built,
            "personas": persona_choices(personas, default=chosen or personas.default_persona),
            "persona_note": PERSONA_SCOPE_NOTE,
            # Whether this store can name, file and hide a conversation. The
            # controls are absent rather than disabled when it cannot: a button
            # that silently does nothing is worse than a button that is not
            # there (`ConversationService.manages_rooms`).
            "manages_rooms": conversations.manages_rooms,
            "rooms_unavailable": ROOM_NOT_MANAGED,
            "groups": await conversations.groups(Owner.profile(user.id)),
            "me": mine,
            "chat_available": chat is not None,
            "chat_unavailable": CHAT_UNAVAILABLE,
            "history_messages": CHAT_HISTORY_MESSAGES,
            "dictation_enabled": _browser_dictation_enabled(),
            "mic_disclosure": MIC_DISCLOSURE,
            "mic_listening": MIC_LISTENING,
            "max_message_chars": MAX_MESSAGE_CHARS,
            "note": note,
        }

    # -- the way in --------------------------------------------------------

    @router.get("/", summary="The admin UI's front door")
    async def admin_root() -> RedirectResponse:
        """``/admin/`` is the address every document and every operator has, so
        it answers rather than 404s.

        Chat is the first thing in the sidebar and the thing the owner opens
        this interface to do, so this sends the visitor there instead of inventing a
        landing page nobody drew. The application's own ``/`` does the same, and
        those two paths are **the only** ones on this surface that redirect to
        Chat — every other page answers where it is, so reloading it stays on
        it. ``tests/server/test_admin_ui_chat.py`` holds that open.

        ``307`` rather than ``301``: a permanent redirect is cached by the
        browser for as long as it feels like, and a designed home screen
        landing here later would have to fight that cache.
        """
        return RedirectResponse("/admin/chat", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # -----------------------------------------------------------------------
    # The image-conversation joint — composer.md contract §6 / image-
    # conversations.md contract §5. Kept as one small, self-contained block:
    # composer.md fixes this route's shape exactly (`POST /admin/chat/image`
    # -> creates a conversation with `kind=image` -> `303` to
    # `/admin/chat?c=<marker>`) as the seam between the `+` sheet's Images row
    # (built elsewhere, against this same fixed shape) and this screen. Do not
    # change the shape without saying so on both sides of that seam.
    # -----------------------------------------------------------------------

    @router.post("/chat/image", summary="Start a new image conversation")
    async def chat_new_image(request: Request) -> RedirectResponse:
        """Create a conversation whose kind is ``image`` and open it.

        A new one every time, never a jump to an existing one — the owner
        accepted, on 2026-09-02, that the list will fill with single-image
        conversations as a result. ``303`` so the browser follows with a ``GET`` and a
        refresh does not mint a second empty conversation, the same reason
        every other POST-then-open control on this screen answers this way.

        A store too old to hold conversations at all (``conversations.start``
        returning ``None``) degrades to the plain "new conversation" address
        rather than raising — the same tolerance every other read of this
        service already has on this screen. What an *image* conversation
        itself cannot do without a configured image service is said later, in
        the thread, once something is actually typed into it
        (``chat_image.IMAGE_NOT_CONFIGURED``) — this route only ever creates
        the room.
        """
        user = require_user(request)
        conversation = await conversations.start(
            Owner.profile(user.id), kind=ConversationKind.IMAGE
        )
        if conversation is None:
            return RedirectResponse("/admin/chat", status_code=status.HTTP_303_SEE_OTHER)
        marker = quote(conversation.started_at.isoformat())
        return RedirectResponse(f"/admin/chat?c={marker}", status_code=status.HTTP_303_SEE_OTHER)

    async def _bulk_delete_note(request: Request) -> dict[str, str] | None:
        """The sentence :func:`chat_bulk_delete` asked this page load to
        show, rebuilt from the flags its redirect carried — never from
        message text sitting in the address itself, the same discipline
        ``?deleted=`` already keeps below. ``None`` when nothing was carried,
        which is every ordinary visit to this screen.

        ``?bulk_failed=`` carries **markers** — the same timestamp identity
        every other control on this screen already puts in a URL (``?c=``,
        the composer's hidden field) — and never a title. A title is
        ``derive_title`` of the first thing somebody said: conversation
        content, and a query string is the one place that must never go —
        it lands in browser history, in a proxy's access log, in a
        screenshot of the address bar. So the marker is resolved back to a
        title *here*, against this owner's own rail, read fresh the same way
        :func:`chat_bulk_delete` itself reads it. A marker that no longer
        resolves — the conversation aged out, or something else removed it
        in the meantime — is left unnamed rather than guessed at: a
        conversation that cannot be found is one there is nothing truthful
        to call.
        """
        params = request.query_params
        raw_total = params.get("bulk_total")
        if raw_total is None:
            return None
        try:
            total = int(raw_total)
            done = int(params.get("bulk_done") or 0)
        except ValueError:
            return None
        if total <= 0:
            return None
        plural = "" if total == 1 else "s"
        if done >= total:
            return {"kind": "none", "message": f"Deleted {done} conversation{plural}."}
        markers = params.getlist("bulk_failed")
        named = ""
        if markers:
            user = require_user(request)
            titles = {row.id: row.title for row in await _bulk_rail(user)}
            resolved = [titles[marker] for marker in markers if marker in titles]
            if resolved:
                named = f" ({', '.join(resolved)})"
        if done == 0:
            message = (
                f"Nothing was deleted. {total} conversation{plural} could not be "
                f"deleted — {BULK_DELETE_REASON}.{named}"
            )
        else:
            message = (
                f"Deleted {done} of {total} conversation{plural}. The rest could "
                f"not be deleted — {BULK_DELETE_REASON}.{named}"
            )
        return {"kind": "invalid", "message": message}

    @router.get("/chat", response_class=HTMLResponse, summary="Talk to the assistant")
    async def chat_page(
        request: Request, c: str | None = None, deleted: str | None = None
    ) -> HTMLResponse:
        """The conversation screen, showing one thread.

        ``?c=`` names which — the instant that thread began. Without it the
        screen opens a new conversation starting now, which is what the "New
        conversation" control is and what following a link into ``/admin/chat``
        does. With it, the thread is read back out of the transcript, so the
        address is a bookmark: reloading this URL tomorrow shows the same
        conversation rather than a fresh one.

        ``?deleted=`` is where the delete button lands, and it is the *only*
        thing that says so: one line, carried in the address rather than in a
        session so that a refresh cannot replay the hide. ``1`` is
        :data:`CONVERSATION_DELETED` and says nothing about what was kept (§6);
        anything else is :data:`CONVERSATION_NOT_DELETED` and says nothing
        about why, which is the same rule read the other way round.

        ``?bulk_total=`` is the same idea for :func:`chat_bulk_delete` — see
        :func:`_bulk_delete_note`, checked when neither of the above fired.

        Absent is no line at all — an ordinary visit to the screen, which is
        most of them.
        """
        note: dict[str, str] | None = None
        if deleted == "1":
            note = {"kind": "none", "message": CONVERSATION_DELETED}
        elif deleted is not None:
            note = {"kind": "invalid", "message": CONVERSATION_NOT_DELETED}
        else:
            note = await _bulk_delete_note(request)
        return templates.TemplateResponse(
            request=request, name="chat.html", context=await _screen(request, wanted=c, note=note)
        )

    @router.get(
        "/chat/messages",
        response_class=HTMLResponse,
        summary="One conversation's messages",
    )
    async def chat_messages(request: Request, c: str | None = None) -> HTMLResponse:
        """The message list alone, for switching threads without a page load.

        The rail, the header and the composer's hidden field come back with it
        as out-of-band swaps, so picking a thread updates which row is lit, what
        this conversation is called, who is in it and what the next message will
        be attached to — and touches nothing else on the screen. The same
        control is an ordinary link to ``/admin/chat?c=`` when there is no
        JavaScript, and lands on the same conversation.
        """
        user = require_user(request)
        now = datetime.now(UTC)
        started = conversation_start(wanted_conversation(c))
        opened = started or now
        records, known = await _visible(user)
        rows = thread_records(records, opened)
        identity = thread_identity(rows, opened.isoformat())
        mine = _my_name(records, user)
        rail = conversation_rows(records, active=identity, now=now, known=known)
        # The picker comes along too. The persona belongs to the thread, so
        # switching to yesterday's conversation without a page load has
        # to move the control as well — otherwise the screen names one persona
        # while the next turn is answered by another, which is the confusion
        # this whole change exists to remove.
        found = await _looked_at(user, started)
        chosen = _chosen_persona(found)
        built = transcript_exchanges(
            rows,
            human=mine,
            audit_rows=await _turn_audit(rows, user),
            reasoning_by_correlation=await _turn_reasoning(rows, user),
            context_limit=await _context_length(),
        )
        await _attach_replay_audio(request, built)
        await _attach_replay_attachments(user, built)
        _attach_replay_images(built, found)
        _attach_replay_workspace_files(found, built)
        return templates.TemplateResponse(
            request=request,
            name="fragments/chat_messages.html",
            context={
                **_room(rail, rows, mine),
                **_rail_view(request, rail),
                # The room controls follow the thread for the same reason the
                # picker does: who else is in this conversation, and whether it
                # speaks, are properties of the conversation being opened.
                **_roster_view(request, found),
                "swap_roster": True,
                "exchanges": built,
                "conversation": opened.isoformat(),
                "personas": persona_choices(personas, default=chosen or personas.default_persona),
                "manages_rooms": conversations.manages_rooms,
                "rooms_unavailable": ROOM_NOT_MANAGED,
                "groups": await conversations.groups(Owner.profile(user.id)),
                "swap_rail": True,
                "swap_room": True,
            },
        )

    # -- selecting several conversations at once (bulk delete) -------------
    #
    # docs/contracts/conversation-list-bulk-actions.md. Bulk delete is *N*
    # calls to ``ConversationService.hide`` — the same call the single-delete
    # button already makes (``chat_room.chat_hide``) — never a second
    # deletion mechanism, and no new audit machinery (contract §5 rule 2: the
    # existing ``conversation_hidden`` log line already fires once per
    # conversation, which is the whole of what that rule wants).
    #
    # Two routes, in the shape the contract's own rule 6 names: checkboxes on
    # the rail -> a form -> a POST -> a confirmation page -> a second POST
    # that acts. The confirmation page follows ``plugin_uninstall.html`` /
    # ``voice_remove.html`` (rule 5.1) — a full page, a plain form, no
    # ``hx-`` attribute anywhere on the part that actually deletes something.
    # Chat's own single-delete button does *not* have this shape (it is
    # ``hx-confirm``, which does nothing with the script off — issue #14) and
    # is deliberately not touched here.

    async def _bulk_rail(user: AdminUser) -> list[ConversationRow]:
        """This owner's own rail, read fresh.

        The only source of truth for what a posted selection can mean —
        "select all" is resolved against it rather than trusted from the
        client, and a marker naming a row that is not on it selects nothing
        (contract rule 3, :func:`~personacore.web.screens.chat_thread.
        bulk_selected_rows`). Bounded the same way the rail on screen already
        is (``conversation_rows``'s own default, :data:`CONVERSATIONS_SHOWN`):
        a checkbox can only exist for a row that was actually rendered, so
        "all of them" means all of what is on screen.
        """
        records, known = await _visible(user)
        return conversation_rows(records, active=None, now=datetime.now(UTC), known=known)

    @router.post(
        "/chat/bulk-delete/confirm",
        response_class=HTMLResponse,
        summary="Confirm deleting several conversations",
    )
    async def chat_bulk_delete_confirm(request: Request) -> HTMLResponse:
        """The checkbox form's target: a real page, naming how many and, when
        the list is short, which ones (contract rule 5.1).

        A **POST**, unlike the single-id confirmations this follows the shape
        of — an arbitrary selection cannot be named by a link, only carried in
        a form body, which is exactly what the contract's rule 6 asks for:
        checkboxes, a form, a POST, a confirmation page.

        Nothing is deleted here. The rows named are resolved against this
        owner's own rail (:func:`_bulk_rail`) so the page cannot be made to
        claim it is about to delete something that was never this owner's;
        :func:`chat_bulk_delete` re-checks again regardless when it acts
        (contract rule 3) rather than trusting what this page hands back.
        """
        user = require_user(request)
        form = await request.form()
        markers = [str(value) for value in form.getlist("selected")]
        select_all = str(form.get("select_all") or "") == "1"
        rows = bulk_selected_rows(await _bulk_rail(user), markers, select_all=select_all)
        if not rows:
            return templates.TemplateResponse(
                request=request,
                name="chat.html",
                context=await _screen(
                    request,
                    wanted=None,
                    note={"kind": "invalid", "message": BULK_DELETE_NOTHING_SELECTED},
                ),
            )
        count = len(rows)
        plural = "" if count == 1 else "s"
        if count <= BULK_DELETE_NAMED_LIMIT:
            named = ", ".join(f"“{row.title}”" for row in rows)
            body = f"This removes {named} from your conversation list."
        else:
            body = f"This removes {count} conversations from your conversation list."
        return templates.TemplateResponse(
            request=request,
            name="chat_bulk_delete.html",
            context={
                # The shell is what `base.html` reads for the sidebar, the
                # health and security dots and the collapse form's `next`.
                # Without it every one of those renders Undefined -- falsy,
                # so the page still appears, which is exactly what makes the
                # omission easy to miss: an administrator quietly gets a
                # member's chrome. `plugin_uninstall_confirm` spreads it for
                # the same reason.
                **await ctx.shell(request, "chat"),
                "title": f"Delete {count} conversation{plural}?",
                "body": body,
                "confirm_label": f"Delete {count} conversation{plural}",
                "rows": rows,
            },
        )

    @router.post("/chat/bulk-delete", summary="Delete several conversations")
    async def chat_bulk_delete(request: Request) -> RedirectResponse:
        """Do it: one ``hide`` per conversation named on the confirmation
        page, never a batch call (contract §5 rule 2 — there is nothing
        batched to add, audit included; see the note beside that rule).

        Ownership is re-checked here, at the write, against this owner's own
        rail read fresh a second time — not against whatever the confirmation
        page carried, which is untrusted input the same way any posted id is
        (contract rule 3). A marker that no longer resolves to this owner's
        own conversation — already gone, raced by another tab, never theirs
        to begin with — is dropped before anything is attempted against it
        and is not counted at all: it was never a legitimate part of the
        selection, the same non-committal treatment a stray id already gets
        from ``chat_hide``.

        A row that *was* genuinely this owner's a moment ago and still fails
        the write itself — the one real race this can catch — is named in the
        result rather than swallowed (contract rule 4).

        ``303`` so the browser follows with a ``GET`` and a refresh does not
        replay the deletes, the same reason ``chat_hide`` answers this way.
        """
        user = require_user(request)
        owner = Owner.profile(user.id)
        form = await request.form()
        markers = [str(value) for value in form.getlist("selected")]
        rail = await _bulk_rail(user)
        wanted = bulk_selected_rows(rail, markers, select_all=False)
        done = 0
        failed_markers: list[str] = []
        for row in wanted:
            started = conversation_start(wanted_conversation(row.id))
            found = (
                None if started is None else await conversations.at(owner, started, create=False)
            )
            if found is not None and await conversations.hide(owner, found.conversation_id):
                done += 1
            else:
                # The marker, never the title. A title is conversation
                # content — the first thing somebody said, `derive_title`'s
                # own input — and this becomes a redirect's `Location`
                # header: a URL, which lands in browser history, a proxy's
                # access log and a screenshot of the address bar. The marker
                # is a timestamp, the same thing `?c=` already puts in a URL
                # on every other control on this screen, and it is resolved
                # back to a title server-side, against a fresh rail, by
                # `_bulk_delete_note` — never carried in the address itself.
                failed_markers.append(row.id)
        total = len(wanted)
        params = [f"bulk_done={done}", f"bulk_total={total}"]
        if failed_markers and len(failed_markers) <= BULK_DELETE_NAMED_LIMIT:
            params.extend(f"bulk_failed={quote(marker)}" for marker in failed_markers)
        return RedirectResponse(
            "/admin/chat?" + "&".join(params), status_code=status.HTTP_303_SEE_OTHER
        )

    # -- the pieces that are not in this file ------------------------------
    #
    # Registered from here rather than from the router factory, so
    # ``chat.register`` still puts every chat route on the router and nothing
    # outside this package had to learn that the screen is six files. The
    # order is the one the arguments force: the exchange needs the view, and
    # the stream needs the exchange.

    view = ChatView(
        ctx=ctx,
        conversations=conversations,
        streamed_rooms_possible=_streamed_rooms_possible,
        screen=_screen,
        visible=_visible,
        thread_rows=_thread_rows,
        looked_at=_looked_at,
        labels=_labels,
        members=_members,
        cap=_cap,
        speaks=_speaks,
        context_length=_context_length,
        exchange_fragment=_exchange_fragment,
    )
    exchange = exchanges.register(router, view)
    streaming.register(router, exchange)
    room.register(router, view)


__all__ = [
    "ASSISTANT_UNATTRIBUTED",
    "AUDIO_URL_PREFIX",
    "BULK_DELETE_NAMED_LIMIT",
    "BULK_DELETE_NOTHING_SELECTED",
    "BULK_DELETE_REASON",
    "CHAT_HISTORY_MESSAGES",
    "CHAT_TRANSCRIPT_WINDOW",
    "CHAT_UNAVAILABLE",
    "CONVERSATIONS_SHOWN",
    "CONVERSATION_DELETED",
    "CONVERSATION_LIST_LIMIT",
    "CONVERSATION_NOT_DELETED",
    "CONVERSATION_SCAN_WINDOW",
    "MAX_AUTHOR_CHARS",
    "MAX_MESSAGE_CHARS",
    "MAX_TOOL_NAMES_SHOWN",
    "MIC_DISCLOSURE",
    "MIC_LISTENING",
    "PERSONA_NOT_REMEMBERED",
    "PERSONA_SAVED",
    "PERSONA_SCOPE_NOTE",
    "PERSONA_UNCHANGED",
    "ROOM_NOT_MANAGED",
    "ROSTER_ADDED",
    "ROSTER_ALREADY",
    "ROSTER_FULL",
    "ROSTER_NOTE",
    "ROSTER_NOT_HELD",
    "ROSTER_REMOVED",
    "SPEECH_NOT_KEPT",
    "SPEECH_OFF_HERE",
    "SPEECH_ON_HERE",
    "STOP_LABEL",
    "STOP_TITLE",
    "UNGROUPED_HEADING",
    "UNTITLED",
    "ChatHistoryMessage",
    "ConversationRow",
    "PersonaChoice",
    "RailGroup",
    "author_label",
    "bulk_selected_rows",
    "chat_exchange",
    "conversation_history",
    "conversation_rows",
    "conversation_start",
    "wanted_conversation",
    "participants",
    "persona_choices",
    "railed",
    "register",
    "thread_identity",
    "thread_records",
    "transcript_exchanges",
]
