"""Running one exchange, and the path a browser without scripting takes.

An *exchange* is everything one send produces: the person's message, whoever in
the room answers it, and whoever answers them. This module owns the serial
version of that — the turn runs to completion and the answer arrives at the end
— which is what the plain form post gets and what htmx without ``fetch`` gets.
:mod:`personacore.web.screens.chat_streaming` tells the same exchange as it
happens, and calls into here for the parts that are not about frames.

It also owns the two questions a room has to answer before anybody speaks: who
the floor is offered to (§3.2), and what one persona is actually asked when it
is another character it is answering (§9's fence). Those are here rather than in
:mod:`personacore.web.screens.chat_voices` because they need a runner and a
fence token; the *deciding* — who is in the room, who speaks next, when it goes
quiet — is still that module's, and nothing here second-guesses it.

**It deliberately does not draw anything.** The markup for one reply comes from
``fragments/chat_exchange.html`` through the screen's own fragment renderer,
handed in on :class:`~personacore.web.screens.chat.ChatView`; what a reply
looks like as data is
:mod:`personacore.web.screens.chat_reply`'s. Nothing here reads a
preference and nothing here writes configuration.

Split out of ``chat.py`` unchanged (ADR-0040). The screen still registers these
routes, so nothing outside this package had to learn a new name.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.admin.models import AdminUser
from personacore.agent.untrusted import new_fence_token
from personacore.audit.models import (
    Author,
    AuthorKind,
    MessageRole,
    Owner,
    Surface,
    TranscriptRecord,
)
from personacore.conversations.addressing import (
    FLOOR_MAX_TOKENS,
    FLOOR_NO_THINKING,
    FLOOR_QUESTION,
    FloorAnswer,
    claims_floor,
)
from personacore.conversations.models import ConversationKind
from personacore.web.screens import chat_attachments, chat_image
from personacore.web.screens import chat_voices as voices
from personacore.web.screens.chat_audio import speaker
from personacore.web.screens.chat_reply import _refused, chat_exchange
from personacore.web.screens.chat_thread import (
    ASSISTANT_UNATTRIBUTED,
    CHAT_HISTORY_MESSAGES,
    ChatHistoryMessage,
    ConversationRow,
    _my_name,
    author_label,
    author_parts,
    conversation_rows,
    conversation_start,
    thread_identity,
    thread_records,
    wanted_conversation,
)
from personacore.web.shared import _readable

if TYPE_CHECKING:  # pragma: no cover - the screen builds this and hands it over
    from personacore.conversations.models import Conversation
    from personacore.web.screens.chat import ChatView

log = structlog.get_logger(__name__)


CHAT_UNAVAILABLE = "Chat is not switched on in this core, so a turn cannot be run from this page."
"""Said out loud when no :class:`~personacore.admin.protocols.ChatRunner` was
supplied, rather than hiding the screen: a missing feature and a broken one look
identical from an empty page, and only one of them is worth an operator's
time."""

MAX_MESSAGE_CHARS = 8000
"""Longest message this screen will send.

Not a security control — the agent loop and the LLM host both have their own
limits — but a page that accepts a pasted megabyte and then reports a timeout
is a page that lied about what it would do with it.
"""


KIND_RESPONDERS: Mapping[ConversationKind, Any] = {
    ConversationKind.IMAGE: chat_image.answer,
}
"""**The one place a conversation's kind picks what answers it** —
image-conversations.md contract §4, which asks for exactly that and names the
shape to avoid: "an ``if kind == \"image\"`` branch scattered through the
request path". :attr:`~ConversationKind.TEXT` is deliberately absent rather
than mapped to the ordinary path: a kind with no entry here *is* the ordinary
path, which is what makes "a conversation written before this field existed is
a text conversation" true without a second statement of it.

The contract names ``music`` as a future member. Adding it is adding a
responder module and one line here — not a second copy of a check in every
route that can start a turn, which is what this table replaced.
"""


def _message_refusal(message: str) -> dict[str, Any] | None:
    """What is wrong with these words alone, or ``None``.

    The two checks that are about the *message* and nothing else — empty, and
    longer than this screen sends. Split out of :func:`_unsendable` because a
    kind with no persona (image-conversations.md §3) still has to refuse both
    of them and must **not** be refused by that function's first check, which
    is about the language model: an image conversation does not need one, and
    a core with no chat runner answers one exactly as well as a core with one.
    """
    if not message:
        return _refused("", "There was nothing to send. Type a message first.")
    if len(message) > MAX_MESSAGE_CHARS:
        return _refused(
            message[:MAX_MESSAGE_CHARS],
            f"That message is longer than this screen will send "
            f"({MAX_MESSAGE_CHARS} characters). Send it in parts.",
        )
    return None


# ---------------------------------------------------------------------------
# The parts that never needed a request
# ---------------------------------------------------------------------------
#
# Each of these was a closure inside the screen's ``register`` and captured
# nothing from it, which is why they can sit here as ordinary functions. That
# matters for one of them beyond tidiness: ``scripts/style_bleed.py`` mirrors
# :func:`_asked_of` line for line because it could not import it, and now it
# could. Changing the script is a separate pass and not this one.


def _takes(runner: Any, keyword: str) -> bool:
    """Whether ``runner`` accepts ``keyword``.

    The general form of :func:`_takes_authorship`, which was written first and
    for one keyword. There are two of them now — ``record_user_message`` and
    ``also_present`` — and both exist for the same reason: ``ChatRunner`` is a
    structural protocol satisfied by objects this package did not build, and a
    runner that predates a keyword raises ``TypeError`` on it. This screen's
    error handling then renders that as a failed turn, so the reply vanishes
    and the metrics line under it goes too. That is exactly what happened once,
    and it is why every new keyword is asked about rather than assumed.

    ``**kwargs`` counts as taking it: a wrapper that forwards everything is a
    legitimate runner and refusing it would be this check being clever.
    """
    try:
        # `signature` follows `__call__` for an instance by itself, so this
        # reads a callable object and a plain function the same way — and
        # `None`, a runner this core was never given, raises and is "no".
        parameters = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        # Something not introspectable at all. Treated as "no", which costs a
        # feature and never costs a reply.
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return keyword in parameters


def _takes_authorship(runner: Any) -> bool:
    """Whether this runner can be told not to record the person's message.

    ``ChatRunner`` is a structural protocol, satisfied by objects this
    package did not build — a test double, a future implementation, a core
    assembled with something older. ``record_user_message`` is new, and a
    runner that predates it raises ``TypeError`` on the keyword, which this
    screen's own error handling then renders as a failed turn: the reply
    vanishes and the metrics line under it goes with it. That is exactly
    what happened, and it is why the keyword is now asked about rather than
    assumed — the same treatment ``stream`` and ``ask`` get, and the same
    idiom ``ConversationService.listing`` uses for ``include_hidden``.

    ``**kwargs`` counts as taking it: a wrapper that forwards everything is
    a legitimate runner and refusing it would be this check being clever.
    """
    return _takes(runner, "record_user_message")


def _with_size_refusals(size_refusals: Sequence[str], notice: str) -> str:
    """One notice, size refusals first — contract §9.

    Two sources land in the same sentence under a message: an image read too
    large to *send* (:func:`chat_attachments.send_refusals`, known before the
    turn even runs) and a part too large or the wrong type to *store*
    (:class:`chat_attachments.StoredAttachments.refusals`, known only after).
    Shared by :mod:`chat_exchange` and :mod:`chat_streaming` so a person sees
    one combined sentence whichever path answered them, not a different
    join order on each.
    """
    return " ".join([*size_refusals, notice] if notice else list(size_refusals))


def _asked_of(
    said: Sequence[voices.Said], mine: str
) -> tuple[str, list[ChatHistoryMessage]]:
    """What one persona is asked this turn, and what it has already read.

    The last thing said in the room is the message; everything before it is
    history. That is uniform and it is what makes a room work: the first
    persona answers the person, and the second answers whatever the first
    just said — which is how two characters end up talking to each other
    rather than both answering the same question into the air.

    Another character's words are fenced on the way in, here and in the
    history, by the same function (§9).
    """
    token = new_fence_token()
    latest = said[-1]
    earlier = list(said[:-1])[-CHAT_HISTORY_MESSAGES:]
    return (
        voices.prompt_for(latest, mine=mine, token=token),
        voices.history_for(
            earlier, mine=mine, message_class=ChatHistoryMessage, token=token
        ),
    )


def _attributed_all(
    exchanges: Sequence[dict[str, Any]],
    rows: Sequence[TranscriptRecord],
    *,
    mine: str,
) -> list[dict[str, Any]]:
    """Name the speakers of a whole exchange, from the rows the agent loop
    wrote while it ran.

    Read back rather than taken from a turn's own result, because the
    result does not carry the model: the loop learns which model answered
    from the response that answered (chat-room §3.1) and writes it onto the
    assistant row. Reading it here means the reply that has just landed is
    headed exactly as it will be headed after a reload — a name that changed
    on refresh would look like two different speakers.

    A room answers with several replies from one send, and each one has to
    be headed by the character that actually gave it — the model included,
    which is only knowable from the row the loop wrote. So the replies are
    paired with the newest assistant rows **from the end**, in order: the
    last reply on the screen is the last row in the store.

    Paired by position rather than by name for the reason
    :func:`_tool_calls` pairs by position: the same persona can answer
    twice in one exchange, and a lookup by name would head both with the
    same row. A turn that wrote no row at all leaves its reply without a
    header rather than borrowing somebody else's — less than the whole
    truth, never a wrong one.

    ``reply_author`` and ``reply_model`` are read off the row through
    :func:`~personacore.web.screens.chat_thread.author_parts` rather than
    the combined :func:`~personacore.web.screens.chat_thread.author_label`
    string — the header prints the name, the reply's own collapsed bar prints
    the model, and nothing re-joins them into one string for either to split
    back apart.
    """
    said = next(
        (row for row in reversed(rows) if row.role is MessageRole.USER),
        None,
    )
    # Only rows written since the person last spoke may head a reply that
    # has just landed. A reply belongs to the exchange that message
    # started, so anything older belongs to somebody else's turn — and
    # borrowing a name from one is how one persona's words came to be printed
    # under another persona's name. The pairing below cannot tell a stale row from a
    # fresh one by looking at it; this can, and an unheaded reply is less
    # than the whole truth where a misheaded one is a lie.
    answered = [
        row
        for row in rows
        if row.role is MessageRole.ASSISTANT
        and (said is None or row.timestamp >= said.timestamp)
    ]
    replies = [item for item in exchanges if item["ok"]]
    for item, row in zip(reversed(replies), reversed(answered), strict=False):
        if not item["reply_author"]:
            name, model = author_parts(row.author, fallback=ASSISTANT_UNATTRIBUTED)
            item["reply_author"] = name
            item["reply_model"] = model or ""
    for item in exchanges:
        if item["message"]:
            item["author"] = item["author"] or author_label(
                said.author if said else None, fallback=mine
            )
    return list(exchanges)


def _offer(request: Request, turn: Any, user: AdminUser) -> Any:
    """Ask the voice side whether this reply can be spoken.

    The whole of what this screen does about audio. ``offer`` decides and
    mints a handle; it does not synthesise, so the reply is never made to
    wait on an engine. A core assembled without the voice subsystem has no
    speaker and the reply is simply silent — which is a state, not a fault,
    and says nothing on the screen.
    """
    if not getattr(turn, "ok", False):
        return None
    # The runner already asked, on the reply path, with the loop's own
    # plain text. Taking its answer rather than minting a second handle for
    # the same words: two handles per reply halve how far back the play
    # buttons keep working, for one reply's worth of audio.
    already = getattr(turn, "speech", None)
    if already is not None:
        return already
    made = speaker(request)
    if made is None:
        return None
    try:
        return made.offer(
            str(getattr(turn, "reply", "") or ""),
            persona=str(getattr(turn, "persona", "") or "") or None,
            owner=user.id,
        )
    except Exception:  # noqa: BLE001 - a silent reply beats a dead screen
        return None


@dataclass(frozen=True, slots=True)
class ChatExchange:
    """The exchange machinery, for the streamed path to call into.

    Every field was a closure inside one ``register`` and is now handed over
    explicitly, because the streamed turn lives in another module and a joint
    written down is what stops two of them drifting. The screen builds one of
    these and passes it on; nothing constructs a second.
    """

    view: ChatView
    """The screen's own reads and renderings, carried through rather than
    handed to the stream separately: one view, and no chance of two."""

    turn: Any
    """Run the exchange a message starts, serially, and return what was said.
    The streamed path falls back to this for a runner that cannot report a
    turn as it happens."""

    answered_by_kind: Any
    """What this conversation's *kind* answers with, or ``None`` for the
    ordinary text path — image-conversations.md contract §4's "one place maps
    a kind to a responder", handed over so the streamed path asks the same
    question through the same function rather than repeating the check."""

    open_the_floor: Any
    """Ask each persona whether the message was for it (§3.2)."""

    recorded_unanswered: Any
    """Write down a message nobody answered."""

    unsendable: Any
    """The refusal a message earns before any turn is run, or ``None``."""

    rendered: Any
    """Finished replies as markup, rail and all."""

    fragment_html: Any
    """The same rendering as a string rather than a response, for the frames a
    streamed turn carries it in. It reads nothing back, so it is what a turn
    that already knows what it is showing uses."""


def register(router: APIRouter, view: ChatView) -> ChatExchange:
    """Register the serial send paths, and hand back what the stream needs."""
    ctx = view.ctx
    chat = ctx.chat
    audit = ctx.audit
    require_user = ctx.require_user
    conversations = view.conversations
    _thread_rows = view.thread_rows
    _labels = view.labels
    _members = view.members
    _cap = view.cap
    _visible = view.visible
    _looked_at = view.looked_at
    _exchange_fragment = view.exchange_fragment

    #: Whether this runner can be told who else is in the room (§2). Asked once
    #: at registration, exactly as ``_rooms_possible`` is and for the same
    #: reason: a runner does not change shape between requests, and one that
    #: predates the keyword would raise on it and cost the reply. A runner that
    #: cannot be told still runs the room — the characters simply cannot see
    #: each other, which is the behaviour before this existed.
    _tells_the_room = _takes(chat, "also_present")

    #: Whether this runner can be handed an attached image (attachments
    #: contract §4.2). Same discovery, same reason: a runner from before this
    #: field existed raises ``TypeError`` on the keyword, and a screen reports
    #: a turn, it does not crash one. A runner that cannot be told still runs
    #: the turn — the image is stored and shown and simply does not reach the
    #: model, which is this feature's own starting state.
    _carries_images = _takes(chat, "image_data_urls")

    #: Whether this runner can be told which conversation this turn belongs
    #: to (the memory contract, ``working/contracts/memory.md`` §3.1). Same
    #: discovery, same reason: a runner from before this existed raises on
    #: the keyword, and a turn it cannot be told about still runs — a
    #: ``memory.remember`` call that turn makes simply carries no
    #: conversation id, which is this field's own honest default.
    _carries_conversation = _takes(chat, "conversation_id")

    async def _open_the_floor(
        user: AdminUser,
        exchange: voices.Exchange,
        said: Sequence[voices.Said],
        labels: Mapping[str, str],
    ) -> None:
        """§3.2 — ask each persona whether the message was for it.

        **The expensive path**, and the reason §3.1 exists: one model call per
        persona per turn. It runs only when nobody was named and never for a
        room of one, which :meth:`voices.Exchange.open_floor` decides — this
        function does the asking and nothing else.

        A runner with no ``ask`` cannot put the question — but it still has to
        say so, because :meth:`voices.Exchange.claim` is where "nobody claimed
        the floor" becomes "the primary persona answers anyway" (§3.2, as the
        owner reversed it). Returning without claiming would leave the room
        silent on exactly the runner where nobody could be asked, which is the
        silence the owner rejected.

        Nobody saying yes is still a real answer about *who else* joins in. It
        is no longer an answer about whether anything happens at all.

        **Every persona cut off mid-thought is counted and reported.** For a
        day this feature was dead on the owner's host and looked fine: the
        configured model reasons before it answers, the ceiling was spent on
        the thinking, the answer came back empty, and an empty answer is a no.
        It never once returned a yes, and nothing anywhere said so — the primary persona
        answered every time, exactly as it does when two characters genuinely
        shrug. :data:`FLOOR_NO_THINKING` is the fix; this count is the alarm
        for the day it stops working, because a fix nobody can see failing is
        the same defect again.
        """
        asking = exchange.open_floor()
        if not asking:
            return
        ask = getattr(chat, "ask", None) if chat is not None else None
        if ask is None:
            exchange.claim([])
            return
        context = list(said)[-CHAT_HISTORY_MESSAGES:]
        claimed: list[str] = []
        cut_off = 0
        for name in asking:
            mine = labels.get(name, name)
            try:
                answer = await ask(
                    FLOOR_QUESTION,
                    user=user.id,
                    persona=name,
                    history=voices.history_for(
                        context, mine=mine, message_class=ChatHistoryMessage
                    ),
                    max_tokens=FLOOR_MAX_TOKENS,
                    extra_body=FLOOR_NO_THINKING,
                )
            except Exception as exc:  # noqa: BLE001, S112 - silence is the safe answer
                log.warning("chat_floor_ask_failed", persona=name, error=repr(exc))
                continue
            if not isinstance(answer, FloorAnswer):
                # A runner from before the answer carried its flags. This seam
                # is discovered with ``getattr`` and an older one still
                # satisfies it, so its words are read exactly as they always
                # were — it simply cannot tell a considered no from a question
                # that ran out of budget, and says nothing rather than guessing.
                answer = FloorAnswer(text=str(answer or ""))
            if answer.cut_off_mid_thought:
                cut_off += 1
            if claims_floor(answer.text):
                claimed.append(name)
        if cut_off:
            # Counts and flags, never a word of what was asked or thought. The
            # number that matters is cut_off against asked: all of them means
            # nobody in this room can claim the floor at all, which is the
            # defect, not a quiet room.
            log.warning(
                "chat_floor_ask_cut_off",
                asked=len(asking),
                cut_off=cut_off,
                claimed=len(claimed),
            )
        exchange.claim(claimed)

    async def _finish_attachments(
        user: AdminUser,
        conversation: Any,
        opened: datetime,
        since: datetime,
        typed: str,
        pending: Sequence[chat_attachments.PendingAttachment],
    ) -> tuple[tuple[chat_attachments.AttachmentChip, ...], str]:
        """Store this turn's pending uploads, once the row they belong to
        exists. Thin wrapper over :func:`chat_attachments.finish_pending`,
        which :mod:`chat_streaming` calls the same way — see its own
        docstring for why this cannot run any earlier than the first reply.
        """
        return await chat_attachments.finish_pending(
            ctx,
            _thread_rows,
            user=user,
            conversation=conversation,
            opened=opened,
            since=since,
            typed=typed,
            pending=pending,
        )

    async def _answered_by_kind(
        user: AdminUser, started: datetime | None, message: str
    ) -> list[dict[str, Any]] | None:
        """What this conversation's *kind* answers with, or ``None`` for the
        ordinary text path — image-conversations.md contract §4.

        **Both send paths call this and nothing else.** The contract asks for
        one place that maps a kind to a responder; :data:`KIND_RESPONDERS` is
        that map and this is the one call site of it, shared by the serial
        ``_turn`` below and by ``chat_streaming._turn_frames``. It shipped
        once as a check inside ``_turn`` alone, which meant every browser that
        could stream — all of the owner's — sent an image conversation's message
        to the language model with the default persona and got words back. A
        second copy of the check would have fixed that and left the next
        member (``music``, which the contract names) to be forgotten in
        exactly the same way.

        ``create=False``, and ``started is None`` answered before the store is
        asked at all: a conversation of a kind that is not text always already
        exists by the time a message can reach it — ``chat.py``'s own ``POST
        /admin/chat/image`` makes it before anybody can type into it — so this
        peek never mints one. An ordinary text conversation is still created
        by whichever caller this returned ``None`` to, at its usual place.

        The message checks here are :func:`_message_refusal`, deliberately
        **not** ``_unsendable``: that one refuses a core with no chat runner,
        and a kind with no persona (contract §3) does not need one.
        """
        if started is None:
            return None
        conversation = await conversations.at(Owner.profile(user.id), started, create=False)
        if conversation is None:
            return None
        responder = KIND_RESPONDERS.get(conversation.kind)
        if responder is None:
            return None
        refusal = _message_refusal(message)
        if refusal is not None:
            return [refusal]
        return await responder(
            ctx,
            conversations,
            user=user,
            conversation=conversation,
            since=datetime.now(UTC),
            message=message,
        )

    async def _turn(
        request: Request,
        message: str,
        started: datetime | None,
        uploads: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """Run the exchange this message starts and return what was said.

        A list because a room can answer with more than one voice: one exchange
        per persona that spoke, in the order they spoke. With one persona in
        the room there is exactly one, run exactly as it always was — §7's
        requirement, and the reason the loop below is entered with the same
        message and the same history a single turn has always been given.

        Every failure comes back as a sentence in the reply's place: a refused
        turn, a runner that raised, a language model that is not there. A
        traceback on this screen would be exactly the failure spec §9 exists to
        prevent.

        This is the path a browser with no streaming takes — the plain form
        post, and htmx without ``fetch``. It has no stop button, because there
        is nothing to press it during: the whole exchange runs and the answer
        arrives at the end.

        ``uploads`` is this turn's attachments, read off the same multipart
        form the message came from — never more than one message's worth,
        because the composer clears its pending tiles the moment Send is
        pressed (attachments.md contract §5).
        """
        user = require_user(request)

        # Contract §4 (image-conversations.md). Not a check of its own — the
        # one function both send paths ask, so a kind that is not text is
        # answered identically whether the browser can stream or not.
        by_kind = await _answered_by_kind(user, started, message)
        if by_kind is not None:
            return by_kind

        pending = await chat_attachments.read_pending(uploads)
        refusal = _unsendable(message, pending)
        if refusal is not None:
            return [refusal]

        # Known now, before any turn runs — contract §4.2/§9. Every persona
        # that speaks in this exchange is answering within the scope of the
        # message that carried this image, so every one of their calls below
        # carries it too (see the loop): that is a decision about *this*
        # exchange, not the cross-turn "what does a second persona see of the
        # first's attachments" question contract §10 leaves for later.
        image_urls = chat_attachments.image_data_urls(pending)
        size_refusals = chat_attachments.send_refusals(pending)

        # The conversation this turn is being said into, resolved (or created,
        # on the first message) before the turn so there is somewhere to file
        # what it writes. ``since`` is read after that and before the turn:
        # the agent loop writes the message rows with its own clock, so
        # everything unattached from this instant on belongs here. ``opened``
        # is the same instant, named once — it is what `_written_message_row`
        # reads the thread back with, and it must be the concrete instant
        # rather than the possibly-``None`` `started` (`_thread_rows` treats
        # ``None`` as "nothing said yet" and would find nothing).
        opened = started or datetime.now(UTC)
        conversation = await conversations.at(Owner.profile(user.id), opened)
        since = datetime.now(UTC)

        labels = _labels()
        exchange = voices.Exchange(_members(conversation, labels), cap=_cap())
        said = voices.said_from_rows(await _thread_rows(user, started))
        # What the model is asked (contract §4.1) is not always what the
        # person is shown: a text attachment's own words are appended here,
        # and addressing (`exchange.open`, just below) still reads the
        # person's own typed text, unchanged — see `compose_model_message`'s
        # own docstring for why the two are allowed to differ.
        said.append(voices.Said(text=chat_attachments.compose_model_message(message, pending)))
        exchange.open(message)
        await _open_the_floor(user, exchange, said, labels)

        spoken: list[dict[str, Any]] = []
        attachment_chips: tuple[chat_attachments.AttachmentChip, ...] = ()
        attachment_notice = ""
        first = True
        while (who := exchange.next()) is not None:
            mine = labels.get(who, who)
            prompt, history = _asked_of(said, mine)
            began = time.monotonic()
            # §2, composed fresh for this turn: everybody in the room but the
            # one about to speak. Empty for a room of one, and then the keyword
            # is not passed at all — §7 is a prompt that is byte-identical, not
            # a prompt with an empty line in it.
            room = voices.others(exchange.roster, who) if _tells_the_room else []
            try:
                turn = await chat(  # type: ignore[misc]
                    prompt,
                    user=user.id,
                    history=history,
                    persona=who,
                    # Passed only when it is False, which is the only time it
                    # changes anything: the first turn of an exchange records
                    # the person's message exactly as every turn always has, so
                    # a runner that has never heard of this keyword never sees
                    # it. See `_takes_authorship`.
                    **({} if first else {"record_user_message": False}),
                    **({"also_present": room} if room else {}),
                    # Attachments contract §4.2. Passed to every persona's
                    # call in this exchange — see the comment where
                    # `image_urls` is computed, above.
                    **({"image_data_urls": image_urls} if image_urls and _carries_images else {}),
                    # Memory contract §3.1: every persona's call in this
                    # exchange carries the conversation it is answering
                    # into, the same reasoning as the room and image kwargs
                    # just above. `conversation` is `None` for a stale
                    # marker naming a hidden conversation (`conversations.
                    # at`'s own docstring: "The turn still happens ... they
                    # simply stay unattached") — a real state, not a gap to
                    # guard against as a bug, so this carries no id at all
                    # rather than raising on it.
                    **(
                        {"conversation_id": conversation.conversation_id}
                        if _carries_conversation and conversation is not None
                        else {}
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - see docstring
                # Claimed even though the turn failed: the loop may well have
                # recorded the operator's message before whatever went wrong,
                # and a message stranded outside the conversation it was typed
                # into is worse than a conversation holding a question that got
                # no answer — which is, after all, what happened.
                await conversations.append(conversation, since=since)
                if first and pending:
                    attachment_chips, attachment_notice = await _finish_attachments(
                        user, conversation, opened, since, message, pending
                    )
                    attachment_notice = _with_size_refusals(size_refusals, attachment_notice)
                spoken.append(
                    _refused(
                        message if first else "",
                        f"The turn did not finish: {_readable(exc)}",
                        attachments=attachment_chips,
                        attachment_notice=attachment_notice,
                    )
                )
                return spoken
            if first and pending:
                # Only now does the row this files under exist — see
                # `_finish_attachments`'s own docstring.
                attachment_chips, attachment_notice = await _finish_attachments(
                    user, conversation, opened, since, message, pending
                )
                attachment_notice = _with_size_refusals(size_refusals, attachment_notice)
            spoken.append(
                chat_exchange(
                    # Only the first reply carries the question: the person
                    # typed it once, and drawing it again above every character
                    # that answers would make one message look like three.
                    message if first else "",
                    turn,
                    seconds=time.monotonic() - began,
                    speech=_offer(request, turn, user),
                    attachments=attachment_chips if first else (),
                    attachment_notice=attachment_notice if first else "",
                )
            )
            reply = str(getattr(turn, "reply", "") or "")
            said.append(voices.Said(text=reply, who=mine, persona=True))
            exchange.spoke(who, reply)
            await _open_the_floor(user, exchange, said, labels)
            first = False
        await conversations.append(conversation, since=since)
        return spoken

    @router.post(
        "/chat/fragment", response_class=HTMLResponse, summary="Run one turn of the conversation"
    )
    async def chat_fragment(request: Request) -> HTMLResponse:
        """One real turn, appended to the conversation the page is holding.

        Appended rather than swapped over (``hx-swap`` is ``beforeend``), so the
        exchange the design drew is the unit that repeats, nothing else on the
        page is redrawn, and the reader's scroll position survives the reply.
        The *model's* memory is not that markup — it is
        :func:`_thread_history`, read from the transcript, so what the assistant
        was told matches what was recorded.

        This is the no-JavaScript path (a plain form post, or htmx without
        ``fetch``), so it is the one reply this screen renders that
        ``chat_streaming.py`` never touches. It still owes it the same
        promise: a room with voice off does not autoplay because this route
        forgot to ask. ``speaking_in`` is resolved the same way ``_rendered``
        resolves it — ``create=False``, off the same ``started`` this turn was
        just run against — so a resumed conversation's switch is read here
        exactly as the streamed path reads it, and a brand new one (nothing
        could have muted a conversation that did not exist a moment ago) comes
        back ``None`` and autoplays by the person's own setting, same as ever.
        """
        user = require_user(request)
        form = await request.form()
        message = str(form.get("message") or "").strip()
        wanted = str(form.get("conversation") or "") or None
        started = conversation_start(wanted_conversation(wanted))

        spoken = await _turn(request, message, started, chat_attachments.gathered_uploads(form))
        now = datetime.now(UTC)
        opened = started or now
        records, known = await _visible(user)
        rows = thread_records(records, opened)
        identity = thread_identity(rows, opened.isoformat())
        return _exchange_fragment(
            request,
            _attributed_all(spoken, rows, mine=_my_name(records, user)),
            rail=conversation_rows(records, active=identity, now=now, known=known),
            conversation=opened.isoformat(),
            speaking_in=await _looked_at(user, started),
        )

    async def _recorded_unanswered(user: AdminUser, message: str) -> None:
        """Write down a message nobody answered (§3.2).

        **The one place this screen writes a transcript row**, and it exists
        because of a gap between two contracts: the chat-room contract says the
        agent loop is the only writer of a row, and the many-voices contract
        says a message nobody claims gets no turn at all. Both cannot hold —
        with no turn there is no loop, and the person's message would vanish
        off the screen on the next reload as though they had never typed it.

        So it is recorded here, attributed to the person exactly as the loop
        attributes it, and nothing about the turn that did not happen is
        invented: no assistant row, no persona, no audit record.

        A store too old to be written to leaves the message on the screen and
        gone on reload. That is worse than this and better than an error, and
        it is the same store that cannot hold a roster — so it cannot reach
        this line at all.
        """
        writer = getattr(audit, "record_transcript", None)
        if writer is None:
            return
        try:
            await writer(
                TranscriptRecord(
                    correlation_id=str(uuid4()),
                    timestamp=datetime.now(UTC),
                    surface=Surface.ADMIN_UI,
                    owner=Owner.profile(user.id),
                    role=MessageRole.USER,
                    content=message,
                    author=Author(name=user.id, kind=AuthorKind.HUMAN),
                )
            )
        except Exception as exc:  # noqa: BLE001 - a lost message beats a dead screen
            log.error("chat_unanswered_write_failed", error=repr(exc))

    def _unsendable(
        message: str, pending: Sequence[chat_attachments.PendingAttachment] = ()
    ) -> dict[str, Any] | None:
        """The refusal this message earns before any turn is run, or ``None``.

        The same three checks ``_turn`` makes, in one place, so the streamed
        path and the plain one refuse the same things in the same words.

        ``pending`` makes an empty box sendable: attaching a file with
        nothing typed is an ordinary send on the surface this composer
        copies (contract §6a), not the "nothing to send" this refusal
        otherwise describes.
        """
        if chat is None:
            return _refused(message, CHAT_UNAVAILABLE)
        if not message and pending:
            return None
        return _message_refusal(message)

    async def _rendered(
        request: Request,
        user: AdminUser,
        spoken: Sequence[dict[str, Any]],
        started: datetime | None,
        *,
        autoplay: bool | None = None,
    ) -> str:
        """Finished replies as markup, rail and all.

        Exactly what ``/chat/fragment`` answers with, down to the out-of-band
        swap that reorders the conversation list — the streamed path renders no
        markup of its own, so there is one template for one reply and no second
        idea of what a message looks like.

        Called once per character in a room, which is why it takes a list and
        is usually handed one thing: each reply is rendered as it lands so the
        words appear under the character that said them rather than all at the
        end. An empty list is a real answer — a message nobody claimed (§3.2) —
        and renders as the rail catching up and nothing else.

        ``started`` also names the conversation this render asks whether to
        speak in — ``_looked_at(user, started)``, ``create=False``, exactly the
        read ``chat_stream`` already does for the same reason before it decides
        ``aloud`` (see its own comment). Resolved here rather than threaded
        through every caller, because "whether this reply may autoplay" is a
        property of the render, not of whoever is calling it — a second copy of
        this lookup at each call site is a second place it could be forgotten,
        which is exactly how the rendered reply came to ignore the switch the
        live path already reads.
        """
        now = datetime.now(UTC)
        opened = started or now
        records, known = await _visible(user)
        rows = thread_records(records, opened)
        identity = thread_identity(rows, opened.isoformat())
        return _fragment_html(
            request,
            _attributed_all(spoken, rows, mine=_my_name(records, user)),
            rail=conversation_rows(records, active=identity, now=now, known=known),
            conversation=opened.isoformat(),
            speaking_in=await _looked_at(user, started),
            autoplay=autoplay,
        )

    def _fragment_html(
        request: Request,
        spoken: Sequence[dict[str, Any]],
        *,
        rail: list[ConversationRow] | None = None,
        conversation: str | None = None,
        speaking_in: Conversation | None = None,
        autoplay: bool | None = None,
    ) -> str:
        response = _exchange_fragment(
            request,
            spoken,
            rail=rail,
            conversation=conversation,
            speaking_in=speaking_in,
            autoplay=autoplay,
        )
        return response.body.decode("utf-8")

    @router.post("/chat", response_class=HTMLResponse, summary="Send a message without scripting")
    async def chat_send(request: Request) -> RedirectResponse:
        """The same turn, for a browser running no JavaScript at all.

        The composer declares ``method="post" action="/admin/chat"``, so with
        htmx absent the browser posts here and this answers with a redirect back
        to the conversation — the messages are read out of the transcript, so
        the reply is on the page that lands. A redirect rather than a rendered
        page because a POST that renders is a POST that gets re-sent by the
        refresh the owner is about to press.
        """
        form = await request.form()
        message = str(form.get("message") or "").strip()
        wanted = str(form.get("conversation") or "") or None
        started = conversation_start(wanted_conversation(wanted))
        await _turn(request, message, started, chat_attachments.gathered_uploads(form))
        target = "/admin/chat"
        if started is not None:
            target = f"{target}?c={quote(started.isoformat())}"
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)

    return ChatExchange(
        view=view,
        turn=_turn,
        answered_by_kind=_answered_by_kind,
        open_the_floor=_open_the_floor,
        recorded_unanswered=_recorded_unanswered,
        unsendable=_unsendable,
        rendered=_rendered,
        fragment_html=_fragment_html,
    )
