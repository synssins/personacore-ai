"""One turn, told as it happens, and the lifecycle that goes with it.

``/chat/stream`` runs exactly the exchange
:mod:`personacore.web.screens.chat_exchange` runs, and reports it as
server-sent events instead of waiting for the end — so the words appear as the
model produces them and each finished sentence goes to the voice while the rest
is still being written. The last frame is the same rendered exchange the serial
path answers with, so one turn is drawn by one template whichever way it ran.

**The lifecycle is the reason this is one file and not two.** A streamed turn
holds an agent turn, a connection to the model and a queue somebody is filling,
and the ordinary way it ends is the reader closing the tab — which, on the ASGI
version every uvicorn in this project speaks, does not close the generator
producing the frames. So :class:`_TurnHolding` is released from the turn task's
``finally`` *and* from the generator's own, and :func:`_kept_alive` holds a
pending ``__anext__`` across a quiet spell and awaits it on the way out. Each of
those was a leak that took real work to find. They are kept beside the code that
depends on them.

**THE REQUEST WATCHES THE TURN; IT NO LONGER DRIVES IT** (detached-turns
contract §3). It used to: the model connection, the token stream, the audio and
the transcript write all belonged to one HTTP response, so a tablet locking its
screen ended a turn that had been running for twenty minutes and nothing was
written, because the transcript is written when the turn completes. The owner
reported this: a tablet going to sleep ended the chat regardless of how long
the turn had been running.

So a turn is now an :class:`asyncio.Task` the application owns
(:class:`_RunningTurn`), and ``POST /chat/stream`` starts one only if this
conversation has none, then **subscribes**. Three tablets signed in as the same
person attach to the one task and all get the same frames — a fan-out, not a
hand-off (§7). Detaching is not stopping: a response generator that is
cancelled removes its own queue and touches nothing else. Stopping is
:func:`stop_turn`, and it is now the only way to end a turn you have walked
away from — which is why §4a's stop had to be built in the same pass.

**What this does not fix, stated plainly** (§4): a container restart still
kills the turn, because the task is in-process. Draining on ``SIGTERM`` and
persisting a reply as it streams are separate work and are deliberately not
here.

**It deliberately does not decide anything about the room.** Who is in it, who
speaks next and who may read what is
:mod:`personacore.web.screens.chat_voices`'; running a turn and recording
it is :mod:`personacore.web.screens.chat_exchange`'s. **Nothing here waits
for speech** — finished sentences are put on a queue and every engine call
happens on the browser's separate request for the audio.

Split out of ``chat.py`` unchanged (ADR-0040). The screen still registers this
route, and every name below is still importable from that module.

**The lifecycle itself is now next door**, in
:mod:`personacore.web.screens.chat_turns`: the ring, the subscribe/replay/stop
machinery, the keepalive read and the mapping from the runner's events to
frames, moved there so a runbook's scripted step runs the same turn a person's
message does rather than a thinner copy of it (PLAN.md alpha.21). Every name is
re-exported here, unchanged, and everything that needs a
:class:`~fastapi.Request` — the form, the room, the attachments, the
rendering — stayed. Read the paragraphs above beside that module: they describe
the same lifecycle from the side that has a reader to lose.

**One name is deliberately not a plain re-export.**
``tests/server/test_chat_streaming.py`` shortens :data:`KEEPALIVE_SECONDS` by
patching it on the module it calls the keepalive through, and a patch that
lands on a re-export is a test that has quietly stopped testing. So
:func:`_kept_alive` below reads *this* module's copy at call time and passes it
down — see its own docstring for why it is a plain function and not an
``async def``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request

from personacore.admin.models import AdminUser
from personacore.audit.models import (
    AuditOutcome,
    AuditRecord,
    MessageRole,
    Owner,
    ReasoningRecord,
    Surface,
)
from personacore.web.screens import chat_attachments, chat_turns, chat_workspace
from personacore.web.screens import chat_voices as voices
from personacore.web.screens.chat_audio import begin_live
from personacore.web.screens.chat_exchange import (
    ChatExchange,
    _asked_of,
    _offer,
    _takes,
    _with_size_refusals,
)
from personacore.web.screens.chat_reply import (
    TURN_METRICS_ACTION,
    TURN_METRICS_CATEGORY,
    TurnMetrics,
    _metrics_detail,
    _refused,
    chat_exchange,
)
from personacore.web.screens.chat_run import _refusal_message, runner_for
from personacore.web.screens.chat_thread import (
    ChatHistoryMessage,
    conversation_start,
    wanted_conversation,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# The lifecycle, and the mapping from events to frames — both live next door
# ---------------------------------------------------------------------------
#
# Everything that makes a turn a turn — the ring of running turns, the
# subscribe/replay/stop machinery, the keepalive read, and the mapping from the
# runner's events to the frames `chat.js` understands — moved to
# `chat_turns.py` so a runbook's scripted step can use the same code rather
# than a thinner copy of it (PLAN.md alpha.21). Nothing about it changed on the
# way out; this module still owns the route, the form, the room, the
# attachments and the rendering, which is everything that needs a `Request`.
#
# The names are re-exported because they are what the test suite and the rest
# of the screen already call them, and because a rename is a change to
# something these lines are not about.

_PING = chat_turns._PING
_KEEPALIVE = chat_turns._KEEPALIVE
KEEPALIVE_SECONDS = chat_turns.KEEPALIVE_SECONDS
_frame = chat_turns.frame
_close_stream = chat_turns.close_stream
_TurnHolding = chat_turns._TurnHolding
_Spoken = chat_turns._Spoken
RUNNING_ATTRIBUTE = chat_turns.RUNNING_ATTRIBUTE
REPLAY_BYTES = chat_turns.REPLAY_BYTES
_ENDED = chat_turns._ENDED
_RunningTurn = chat_turns._RunningTurn
STOPPED_REPLY = chat_turns.STOPPED_REPLY
_stopped_or_broken = chat_turns._stopped_or_broken
_turns = chat_turns._turns
_keyed = chat_turns._keyed
running_turn = chat_turns.running_turn
stop_turn = chat_turns.stop_turn
_attached = chat_turns._attached
_dropped = chat_turns._dropped
_watching = chat_turns._watching
_drive = chat_turns._drive


def _kept_alive(events: Any, *, stopping: asyncio.Event | None = None) -> AsyncIterator[Any]:
    """``chat_turns.kept_alive``, with **this module's** keepalive interval.

    A plain function returning that generator rather than an ``async def``
    wrapping it, and both halves of that matter. Reading
    :data:`KEEPALIVE_SECONDS` here, at call time, is what keeps
    ``test_chat_streaming.py``'s monkeypatch working: it patches the name on
    this module, and a patch that lands on a re-export is a test that has
    quietly stopped testing (that file's own comment). Handing back the inner
    generator rather than iterating it means the caller closes the real one —
    a wrapper would leave the inner generator's pending read alive, and
    ``_TurnHolding.release``'s ``aclose`` would then raise the
    "already running" error that leak exists to prevent.
    """
    return chat_turns.kept_alive(events, stopping=stopping, seconds=KEEPALIVE_SECONDS)


def register(router: APIRouter, exchange: ChatExchange) -> None:
    """Register the streamed send path, over the machinery the serial one uses."""
    view = exchange.view
    ctx = view.ctx
    chat = ctx.chat
    audit = ctx.audit
    require_user = ctx.require_user
    conversations = view.conversations
    _thread_rows = view.thread_rows
    _labels = view.labels
    _members = view.members
    _cap = view.cap
    _speaks = view.speaks
    _context_length = view.context_length
    _streamed_rooms_possible = view.streamed_rooms_possible
    #: Whether the *streaming* runner can be told who else is in the room (§2).
    #: Asked of the streaming runner separately from the plain one, exactly as
    #: ``streamed_rooms_possible`` is: they are separate objects and one may be
    #: older than the other.
    _tells_the_room = _takes(getattr(chat, "stream", None), "also_present")
    #: Whether the *streaming* runner can be handed an attached image
    #: (attachments contract §4.2) — asked of ``chat.stream`` separately from
    #: the plain runner, for the same reason ``_tells_the_room`` is.
    _carries_images = _takes(getattr(chat, "stream", None), "image_data_urls")
    #: Whether the *streaming* runner can be told which conversation this
    #: turn belongs to (the memory contract, ``working/contracts/memory.md``
    #: §3.1) — asked of ``chat.stream`` separately from the plain runner,
    #: for the same reason ``_tells_the_room`` is.
    _carries_conversation = _takes(getattr(chat, "stream", None), "conversation_id")
    #: Whether the *streaming* runner can be told the conversation's own
    #: thinking override (thinking contract §13 D) — asked of ``chat.stream``
    #: separately from the plain runner, for the same reason
    #: ``_tells_the_room`` is. A runner too old to take ``thinking`` still
    #: runs a persona exactly as its own switch says (``boot.chat``'s own
    #: resolution), so this only ever costs the *override*, never the turn.
    _carries_thinking = _takes(getattr(chat, "stream", None), "thinking")
    _turn = exchange.turn
    _answered_by_kind = exchange.answered_by_kind
    _open_the_floor = exchange.open_the_floor
    _recorded_unanswered = exchange.recorded_unanswered
    _unsendable = exchange.unsendable
    _rendered = exchange.rendered
    _fragment_html = exchange.fragment_html
    _markers_template = ctx.templates.get_template("fragments/chat_markers.html")
    _workspace_cards_template = ctx.templates.get_template("fragments/chat_workspace_cards.html")

    def _workspace_cards_html(chips: Sequence[Any]) -> str:
        """The live ``workspace_files`` frame's own markup — the same
        template the finished exchange includes (``chat_exchange_body.
        html``), rendered on its own the same way ``_markers_html`` renders
        ``chat_markers.html`` on its own: neither needs a ``request`` or a
        ``url_for``, and a card appearing while the reply is still
        streaming must be the exact bytes a reload would draw for it, or the
        two would visibly swap the moment the finished exchange lands."""
        if not chips:
            return ""
        return _workspace_cards_template.render(chips=chips)

    def _markers_html(conversation: str) -> str:
        """``chat_markers.html``, rendered on its own rather than riding along
        with a reply.

        The template needs nothing a request would supply — no ``request``, no
        ``url_for`` — so ``get_template().render()`` is enough; see
        ``chat_save_link.html``, the one control inside it, for the same fact
        checked against the file itself. Kept to this one call so a turn that
        has to correct the markers before anything else has happened does not
        have to build the fragment `_rendered`/`_fragment_html` are for, which
        both want a rendered *reply* and this frame has none yet.
        """
        return _markers_template.render(conversation=conversation)

    async def _turn_reply_row(user: AdminUser, started: datetime | None) -> Any:
        """The row the loop just wrote this persona's reply as.

        Not carried from ``out.result``: :class:`~personacore.admin.protocols.ChatTurnResult`
        does not expose a correlation id, and the loop's own id lives in a
        :mod:`contextvars` binding set inside ``self._run()`` — which runs
        inside the ``asyncio.Task`` :func:`_kept_alive` wraps each
        ``__anext__`` in, and a Task's context is a *copy* taken when the Task
        is created. Whatever ``bind_correlation_id`` sets inside it never
        reaches the context this route resumes in, the same way a
        subprocess's environment change never reaches its parent's. Reading it
        back off the row the loop already wrote is the seam
        ``chat_exchange._attributed_all`` uses to name the model that
        answered, for exactly this reason, and this is that same seam.

        The last assistant row this thread has is the one this persona's turn
        just wrote: `_turn_frames`'s ``while`` loop awaits one persona fully
        — through this very call — before the next one is asked anything, so
        nothing else can have landed a row in between. ``None`` for a turn
        that, somehow, wrote no row at all (the failure `_metrics_for` already
        treats the same as a turn that predates this feature).
        """
        rows = await _thread_rows(user, started)
        for row in reversed(rows):
            if row.role is MessageRole.ASSISTANT:
                return row
        return None

    async def _record_turn_metrics(
        user: AdminUser, started: datetime | None, metrics: TurnMetrics
    ) -> None:
        """Write down what this turn cost, filed under the correlation id its
        own reply already carries, so a reload can say it again
        (``chat_thread._metrics_for``/``_fill_reply``).

        Best-effort, like every other read or write this screen makes around
        the edges of a turn (``_recorded_unanswered``, ``chat._turn_audit``): a
        failure here costs the replay's three numbers, never the turn, which
        has already been shown — and spoken — by the time this runs.

        Filed as an ``AuditRecord`` sharing the reply's own correlation id
        rather than a store of its own: the audit table already ages out by
        surface and retention window (ADR-0004) and already goes with a
        deleted conversation's rows on the same schedule its ``TOOL_CALL``
        rows do, so this needed no new store and no migration to get that for
        free. See ``chat_reply.TURN_METRICS_CATEGORY`` for why ``EVENT`` and
        not a category invented for this.

        Timestamped **from the reply row itself**, not ``datetime.now(UTC)``:
        ``chat._turn_audit`` bounds its query to ``[rows[0].timestamp,
        rows[-1].timestamp]``, and "now" — a beat after the reply was written —
        would land outside that window and never be found again. Reusing the
        row's own timestamp costs nothing (there is no uniqueness constraint on
        it) and keeps this record inside the span it is describing.
        """
        row = await _turn_reply_row(user, started)
        if row is None:
            return
        try:
            await audit.record_audit(
                AuditRecord(
                    correlation_id=row.correlation_id,
                    timestamp=row.timestamp,
                    surface=Surface.ADMIN_UI,
                    owner=Owner.profile(user.id),
                    category=TURN_METRICS_CATEGORY,
                    action=TURN_METRICS_ACTION,
                    outcome=AuditOutcome.SUCCESS,
                    detail=_metrics_detail(metrics),
                )
            )
        except Exception as exc:  # noqa: BLE001 - a missing replay number beats a dead turn
            log.warning("chat_turn_metrics_write_failed", error=repr(exc))

    async def _record_reasoning(
        user: AdminUser, started: datetime | None, reasoning: str
    ) -> None:
        """Keep this turn's own reasoning, filed under the reply's own
        correlation id, so a reload can draw the same collapsed line again
        (``chat_thread._fill_reply``).

        The owner reversed this on 2026-09-02, overruling the decision this
        feature first shipped with — that reasoning was shown live and never
        kept — wanting it retained as additional context that could be fed
        back to the model later. Nothing here changes the
        live path; it only adds where the same text lands afterwards.

        Called only when there is something to keep — an ordinary reply, the
        one with no reasoning at all, must write nothing and render no line
        (rule 2 of the feature this exists for), so the caller checks
        ``reasoning`` before ever reaching this function.

        Best-effort, like ``_record_turn_metrics`` beside it: a failure here
        costs the replayed thinking line, never the turn, which has already
        been shown live by the time this runs.

        Its own table, not this turn's ``AuditRecord`` — see
        :class:`~personacore.audit.models.ReasoningRecord`'s own docstring for
        why: ``detail`` is documented action metadata, reasoning is
        conversation content, and ten to fifteen thousand tokens of it does
        not belong riding along on every read of a thread's tool calls.
        Timestamped from the reply row for the identical reason
        ``_record_turn_metrics`` is — the same row, read back the same way.
        """
        row = await _turn_reply_row(user, started)
        if row is None:
            return
        try:
            await audit.record_reasoning(
                ReasoningRecord(
                    correlation_id=row.correlation_id,
                    timestamp=row.timestamp,
                    surface=Surface.ADMIN_UI,
                    owner=Owner.profile(user.id),
                    text=reasoning,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a lost replay line beats a dead turn
            log.warning("chat_reasoning_write_failed", error=repr(exc))

    async def _record_workspace_files(
        user: AdminUser, started: datetime | None, files: Sequence[str]
    ) -> None:
        """Keep this turn's own workspace files, filed under the reply's own
        correlation id, so a reload can draw the same cards again
        (``chat_thread._fill_reply`` / ``chat.py``'s
        ``_attach_replay_workspace_files``) — workspace contract §7.

        One record for the whole turn, written once it has finished, the
        same shape :func:`personacore.web.screens.chat_attachments.
        store_pending` writes ``chat.attachments`` in and for the same
        reason: several tool calls in one turn each add their own names to
        ``out.workspace_files`` as they finish (see ``_stream_one``), and
        this is the point they are all known and can be filed together
        rather than overwritten call by call.

        Called only when there is something to keep, same rule
        ``_record_reasoning`` follows: a turn whose tools produced nothing
        must write nothing and render no card.

        Best-effort, like ``_record_turn_metrics``/``_record_reasoning``
        beside it: a failure here costs the replayed cards, never the turn,
        which has already shown them live by the time this runs.
        """
        row = await _turn_reply_row(user, started)
        if row is None:
            return
        try:
            await audit.record_audit(
                AuditRecord(
                    correlation_id=row.correlation_id,
                    timestamp=row.timestamp,
                    surface=Surface.ADMIN_UI,
                    owner=Owner.profile(user.id),
                    category=chat_workspace.WORKSPACE_FILES_CATEGORY,
                    action=chat_workspace.WORKSPACE_FILES_ACTION,
                    outcome=AuditOutcome.SUCCESS,
                    detail={"files": list(files)},
                )
            )
        except Exception as exc:  # noqa: BLE001 - a lost replay linkage beats a dead turn
            log.warning("chat_workspace_files_link_failed", error=repr(exc))

    @router.post(
        "/chat/stream",
        summary="Run one turn, reporting it as it happens",
    )
    async def chat_stream(request: Request) -> Any:
        """The same turn as ``/chat/fragment``, told as it unfolds.

        The owner asked for one thing — audio beginning to play at the same
        time the text was still generating — and both halves of the old path
        were serial: the turn ran to completion, the whole reply was rendered at
        once, and only then did the browser ask for the audio. This route is
        the first half; :func:`~personacore.web.screens.chat_audio.begin_live`
        and the stream it opens are the second.

        **Server-sent events, over the POST that starts the turn.** Not
        ``EventSource``, which can only issue a GET and would need the message
        parked somewhere first — a second piece of per-turn server state, and a
        second thing to expire. The frames are the ordinary
        ``event:``/``data:`` shape and ``chat.js`` reads them off the response
        body; nothing was added to the page to do it.

        **The last frame is the finished exchange, rendered by the server.**
        The growing bubble is plain text — the markdown is not markup until the
        reply has ended — and ``done`` carries exactly what ``/chat/fragment``
        would have answered with, so one turn is rendered by one template
        whichever way it was run, markdown and player and footer included.

        **Nothing here waits for speech.** Finished sentences are *put on a
        queue*; every engine call happens on the browser's separate request for
        the audio. An engine that is slow, stopped or broken costs the audio and
        cannot delay or truncate a word of the text.

        **THIS REQUEST WATCHES A TURN; IT DOES NOT RUN ONE** (§3). It starts one
        if this conversation has none, then subscribes — and what it does when
        one is already running depends on whether anything was actually typed:

        *Nothing typed* is somebody attaching. A tablet coming back from sleep,
        or the second and third of the three devices §7 describes, which are
        watching a turn they did not begin. It is not a new field and not a new
        route: an empty message has always meant "there is nothing to send",
        and with a turn already running there is nothing to send *and*
        something to watch. Rule 2 — attaching twice must not run the turn
        twice — is exactly this case.

        *Something typed* is a second send, and this deliberately keeps the
        behaviour it has today: ``chat.js``'s own comment is "A second send
        cancels the first", so the running turn is stopped and the new message
        starts a replacement. **What a send *should* do during a running turn
        is undecided** (§4a): queued and interjected are both real answers and
        the owner has not given one. Preserving today's answer is not choosing
        between them — it is refusing to, in the only way that does not
        silently swallow a message that was typed.
        """
        user = require_user(request)
        form = await request.form()
        message = str(form.get("message") or "").strip()
        wanted = str(form.get("conversation") or "") or None
        started = conversation_start(wanted_conversation(wanted))
        uploads = chat_attachments.gathered_uploads(form)

        ring = _turns(request.app)
        key = _keyed(user.id, wanted)
        running = ring.get(key) if key is not None else None
        if running is not None:
            if not message and not uploads:
                return _watching(running)
            running.stop()

        # A conversation this turn may not have a name for yet — a brand new
        # one is named by an instant minted inside `_turn_frames`, which calls
        # `identify` with it before the first word. See `_RunningTurn`.
        #
        # Registered through `chat_turns.begin_turn`, which is the one place a
        # turn enters the ring whoever started it — a browser here, a runbook
        # step through `chat_turns.start_turn`. This response then streams from
        # **the handle's own turn** rather than looking the key up again: two
        # lookups is two chances to answer with a different turn than the one
        # just started.
        handle = chat_turns.begin_turn(
            request.app, owner=user.id, marker=None if key is None else key[1]
        )
        # Subscribed **before** the task exists, so the first frame cannot be
        # produced before there is somewhere to put it.
        watching = _watching(handle.running)
        handle.drive(
            _turn_frames(request, user, message, started, handle.running, uploads)
        )
        return watching

    async def _turn_frames(
        request: Request,
        user: AdminUser,
        message: str,
        started: datetime | None,
        turn: _RunningTurn,
        uploads: Sequence[Any] = (),
    ) -> AsyncIterator[str]:
        """One exchange as a series of frames, ending with the last reply.

        Usually one turn, which is what a conversation with one persona in it
        is and what this did before rooms existed. A room with several personas
        runs each turn the same way and sends each finished reply as its own
        frame, so the words appear under the character that said them as they
        are said rather than all at once at the end.

        The frame names are the whole protocol between this and ``chat.js``:

        ``markers``
            issued once, the moment the turn knows which conversation it is
            running in — before any ``delta``, and long before ``reply`` or
            ``done``. ``html`` is ``fragments/chat_markers.html``, swapped in
            exactly as the landing exchange's own copy of it is. See its
            module docstring for why every control that names the open thread
            has to carry this and not the instant the page was opened at.
        ``exchange``
            a room with more than one persona is running, and this carries the
            token the stop button posts. Absent for a single persona,
            deliberately — §5's button is lit only while the room is active, and
            a solo turn has no boundary to stop at.
        ``delta`` / ``tool`` / ``tool_done`` / ``notice`` / ``speech``
            unchanged, and now belonging to whichever character is speaking.
        ``thinking``
            a fragment of the model's own reasoning (``reasoning_content`` on
            the wire, never ``content``) — the owner asked to *see it live*,
            not hear it: it is never sent to the voice queue and never joins
            ``reply``, so it can never be spoken and can never be mistaken for
            the answer. It **is** kept — the owner reversed this on
            2026-09-02, overruling the first cut of this feature, wanting the
            reasoning retained as additional context that could be fed back
            to the model later — filed under the reply's own correlation id
            (``_record_reasoning``) once the turn finishes, so the same
            collapsed line a reload draws (``chat_thread._fill_reply``) is not
            a second thing to keep in step with this one. An older client that
            has never heard of this frame ignores it (``chat.js``'s dispatch
            is an if/else chain with no ``else``) and the turn is otherwise
            identical.
        ``turn``
            the next character has begun. The browser starts a fresh bubble.
        ``reply``
            one character has finished; ``html`` is its rendered exchange, and
            **another one follows**.
        ``done``
            the end, with the last rendered exchange in ``html``.

        A client older than ``markers`` ignores a frame name it does not
        recognise (``chat.js``'s frame dispatch falls through silently on an
        ``else``-less chain of ``if``s) and gets exactly what it got before
        this frame existed: a working turn whose controls are wrong until the
        reply lands.

        Everything this exchange is holding is registered on ``turn.holding``
        the moment it exists and released in the ``finally`` below — and again
        by the task driving this generator (`_drive`), because the ordinary way
        a streamed reply ends is the reader closing the tab and that path does
        not always reach this generator at all.

        **``turn`` is the running turn, not the connection watching it.** It is
        here for two things and no others: to be told which conversation this
        turned out to be (`_RunningTurn.identify`, beside ``opened`` below), and
        to be asked whether somebody has pressed stop.
        """
        holding = turn.holding

        # WAVE2.md's text-answer mode, asked before anything else this
        # function does — the same explicit branch `chat_exchange._turn`
        # makes for the serial path, mirrored here because a browser that
        # can stream never reaches that function at all (see the
        # `streaming is None` fallback below, which does). A message
        # answering a gate never reaches the language model either way: it
        # goes to `Runner.answer_text`, and the run's own writes to the
        # transcript are what show the answer and whatever comes next —
        # this yields only the two frames every other early return here
        # yields, `markers` then `done`, with nothing new rendered into the
        # exchange itself.
        runner = runner_for(request)
        if runner is not None and started is not None:
            existing = await conversations.at(Owner.profile(user.id), started, create=False)
            cid = existing.conversation_id if existing is not None else None
            if cid:
                try:
                    awaiting = bool(runner.awaiting_text(cid))
                except Exception:  # noqa: BLE001 - an unreadable gate is not answered here
                    awaiting = False
                if awaiting:
                    yield _frame("markers", {"html": _markers_html(started.isoformat())})
                    try:
                        await runner.answer_text(Owner.profile(user.id), cid, message)
                        answered_spoken: list[dict[str, Any]] = []
                    except Exception as exc:  # noqa: BLE001 - RunRefused's sentence, never a 500
                        answered_spoken = [_refused(message, _refusal_message(exc))]
                    yield _frame(
                        "done", {"html": await _rendered(request, user, answered_spoken, started)}
                    )
                    return

        # image-conversations.md contract §4, asked before anything else this
        # function does. **The same function `_turn` asks** — the contract's
        # "one place maps a kind to a responder", and the reason this is a
        # call and not a copy of a check: it shipped in `_turn` alone, so
        # every browser that could stream (all of them) sent an image
        # conversation's message to the language model instead.
        #
        # A generated picture is not a token stream — one picture arrives at
        # the end — so this emits no `delta` and invents no frame for it. Two
        # frames the client already understands: `markers`, naming the thread
        # every control on the page then points at, and `done`, carrying the
        # finished exchange rendered by the same template `/chat/fragment`
        # answers with. `started` is never `None` here: `_answered_by_kind`
        # returns `None` for a request that named no conversation, and a kind
        # that is not text always already has one.
        if started is not None:
            by_kind = await _answered_by_kind(user, started, message)
            if by_kind is not None:
                yield _frame("markers", {"html": _markers_html(started.isoformat())})
                yield _frame("done", {"html": await _rendered(request, user, by_kind, started)})
                return

        # Neither of the next two returns gets a `markers` frame, and both
        # decisions are the same one: nothing below has resolved a
        # conversation yet, so there is nothing true to name.
        #
        # A refusal never runs a turn at all — `_unsendable` said no before
        # `conversations.at` was ever going to be called, and the marker
        # `_rendered` still puts on the refusal card names an instant that was
        # never given a row. That is unchanged by this frame and is not a
        # symptom of the bug it fixes: nothing was said, so there is no
        # running turn a stale control could be redirected away from.
        pending = await chat_attachments.read_pending(uploads)
        # Known now, before any turn runs — contract §4.2/§9. Passed to every
        # persona's call in this exchange (see `_stream_one`'s `image_urls`
        # argument): every one of them is answering within the scope of the
        # message that carried this image, which is a decision about *this*
        # exchange and not the cross-turn question contract §10 defers.
        image_urls = chat_attachments.image_data_urls(pending)
        size_refusals = chat_attachments.send_refusals(pending)
        streaming = getattr(chat, "stream", None) if chat is not None else None
        if streaming is None:
            # A core with no runner, or one too old to report a turn as it
            # happens. The exchange still runs and the answers still arrive —
            # all at once, which is what this screen did before — so the reader
            # sees a slower reply rather than an error.
            #
            # `_turn` resolves and creates the conversation itself, on its own
            # clock, and does not hand it back — so a `markers` frame emitted
            # from here would either duplicate that resolution (risking a
            # *second* conversation minted at a different instant, for a
            # request that posted no marker at all) or have to wait for `_turn`
            # to finish, which is the last frame anyway. This path already
            # behaves exactly as the whole screen did before streaming existed
            # — one request, one wait, one answer — and the race this brief is
            # about needs a growing reply on screen to be clicked away from,
            # which this path never draws. Closing it for real is a change to
            # what `_turn` returns, which is a contract change and not this
            # file's to make alone.
            spoken = await _turn(request, message, started, uploads)
            yield _frame("done", {"html": await _rendered(request, user, spoken, started)})
            return

        refusal = _unsendable(message, pending)
        if refusal is not None:
            yield _frame("done", {"html": await _rendered(request, user, [refusal], started)})
            return

        # The instant this turn's conversation is named by, computed once and
        # kept — not called again through `started or datetime.now(UTC)` at
        # each of the render sites below. Two calls to `datetime.now(UTC)`
        # a few lines apart are two different instants, and this file's
        # conversation store resolves an instant by exact equality
        # (`_conversation_at`, store_conversations.py): a marker built from
        # the second call would name an instant with no row, which is the
        # same "posting a control makes a fresh conversation" failure this
        # frame exists to end — only moved to the reply landing instead of
        # avoided.
        opened = started or datetime.now(UTC)
        # §3.1, and the same string the `markers` frame is about to carry: this
        # turn now has the name a reconnecting browser will ask for it by. A
        # conversation that already existed was keyed by the route before the
        # task started; a brand new one is keyed here, because until this line
        # the instant naming it had not been minted.
        turn.identify(opened.isoformat())
        conversation = await conversations.at(Owner.profile(user.id), opened)
        # Issued the moment the conversation is known, which is here — before
        # the floor is asked, before a token is minted, before a single word
        # is generated. `chat.js`'s `land()` applies it exactly as it applies
        # `reply`/`done`'s markers, so a control pressed anywhere in the rest
        # of this turn is already pointed at the right thread.
        yield _frame("markers", {"html": _markers_html(opened.isoformat())})
        since = datetime.now(UTC)
        # Asked once for the whole exchange, not per persona: it is a fact
        # about the connection, not about any one reply — see
        # `ChatView.context_length`'s own docstring.
        context_limit = await _context_length()

        labels = _labels()
        here = _members(conversation, labels)
        if not _streamed_rooms_possible:
            # The streaming runner is a separate object from the plain one and
            # may be older than it. Same rule, asked separately.
            here = here[:1]
        exchange = voices.Exchange(here, cap=_cap())
        said = voices.said_from_rows(await _thread_rows(user, started))
        # What the model is asked (contract §4.1) is not always what the
        # person is shown — see `chat_attachments.compose_model_message`'s
        # own docstring. Addressing (`exchange.open`, next) still reads the
        # person's own typed text, unchanged.
        said.append(voices.Said(text=chat_attachments.compose_model_message(message, pending)))
        exchange.open(message)

        if not exchange.solo:
            # §5. Registered before the first turn, because the button has to
            # work during it, and only for a room: a single persona answers
            # once and there is no second turn for a stop to prevent.
            token = voices.begin(request, user.id, exchange)
            holding.forget = lambda: voices.release(request, token)
            yield _frame("exchange", {"token": token})

        # §3.2, and the one call in this function that costs a model per
        # persona. It runs only when §3.1 named nobody and never for a room of
        # one, which ``open_floor`` decides.
        await _open_the_floor(user, exchange, said, labels)

        # Whether replies read themselves aloud here: this person's own setting
        # (ADR-0030), then this conversation's switch (§6.2). Read once for the
        # exchange rather than per turn — a switch flicked mid-exchange should
        # not silence the second character and not the first.
        aloud = _speaks(request, conversation)

        attachment_chips: tuple[chat_attachments.AttachmentChip, ...] = ()
        attachment_notice = ""
        first = True
        who = exchange.next()
        try:
            if who is None:
                # Nobody speaks. Much rarer since §3.2 was reversed — a message
                # nobody claims is now answered by the primary persona — but
                # not gone: the stop button pressed while the floor question is
                # running ends the exchange before anyone is queued, and so
                # does an exchange that fails to start. The message still has
                # to be kept — it was said — so it is recorded here rather than
                # by the turn that never ran. See :func:`_recorded_unanswered`.
                await _recorded_unanswered(user, message)
                await conversations.append(conversation, since=since)
                if pending:
                    # Stored even though nothing is rendered below (`spoken`
                    # stays empty) — the row `_recorded_unanswered` just wrote
                    # is enough to file these under, and a reload draws them
                    # back through the ordinary replay path
                    # (`chat_thread._fill_reply`). Correctness of the data
                    # matters here even where the live render does not change.
                    await chat_attachments.finish_pending(
                        ctx,
                        _thread_rows,
                        user=user,
                        conversation=conversation,
                        opened=opened,
                        since=since,
                        typed=message,
                        pending=pending,
                    )
                # `opened`, not `started`: `_rendered` computes its own
                # `started or datetime.now(UTC)` when handed `None`, which is
                # a second, later instant than the one this turn's
                # conversation was actually created at. Handing it `opened`
                # means there is nothing left to compute — the marker in this
                # landing exchange is the same one the `markers` frame above
                # already named.
                yield _frame("done", {"html": await _rendered(request, user, [], opened)})
                return
            while who is not None:
                mine = labels.get(who, who)
                prompt, history = _asked_of(said, mine)
                # §2, composed fresh for this turn: everybody in the room but
                # the one about to speak. Empty for a room of one, and then the
                # keyword is never passed — §7 asks for a byte-identical
                # prompt, not for an empty line in one.
                room = voices.others(exchange.roster, who) if _tells_the_room else []
                out = _Spoken()
                if not first:
                    # A fresh bubble for the next character, named, so the words
                    # appear under whoever is saying them rather than growing on
                    # the end of the previous character's reply.
                    yield _frame("turn", {"author": mine})
                async for piece in _stream_one(
                    request,
                    user,
                    persona=who,
                    prompt=prompt,
                    history=history,
                    record=first,
                    room=room,
                    image_urls=image_urls if _carries_images else (),
                    # `conversation` is `None` for a stale marker naming a
                    # hidden conversation (`conversations.at`'s own
                    # docstring) — a real state the turn still runs under,
                    # not a bug to guard against by accident. See the same
                    # comment in `chat_exchange.py`'s own version of this.
                    conversation_id=(
                        conversation.conversation_id
                        if _carries_conversation and conversation is not None
                        else None
                    ),
                    # Workspace contract §7's own use of the conversation id —
                    # decoupled from `conversation_id` just above, which is
                    # gated on whether the *agent loop* accepts it
                    # (`_carries_conversation`). Card links need the real id
                    # regardless of that: a runner too old to take
                    # `conversation_id` can still run a persona with a
                    # workspace on, and its cards still need somewhere to
                    # link to.
                    workspace_ref=(
                        conversation.conversation_id if conversation is not None else None
                    ),
                    # Thinking contract §13 D — `found.thinking`, this
                    # conversation's own override, straight off the row.
                    # `getattr`: a core that has not landed the ``thinking``
                    # column yet still runs this turn, just with no override
                    # to carry — the loop then falls back to the persona's
                    # own switch exactly as it does with no conversation at
                    # all.
                    thinking=(
                        getattr(conversation, "thinking", None)
                        if _carries_thinking and conversation is not None
                        else None
                    ),
                    aloud=aloud,
                    streaming=streaming,
                    holding=holding,
                    stopping=turn.stopping,
                    out=out,
                ):
                    yield piece

                if turn.stopping.is_set() or out.broke:
                    # The two ways a turn ends without a reply: somebody
                    # pressed stop (§4a) or it fell over. They take one exit
                    # because everything after this point is identical — the
                    # conversation is claimed, the attachments are filed, and a
                    # card goes where the answer would have been. Only the
                    # sentence on the card differs, and the stop is checked
                    # first because breaking out of the model's stream can set
                    # `broke` on the way past.
                    #
                    # Claimed even though the turn produced nothing, exactly as
                    # `_turn` claims it: the loop may well have recorded the
                    # operator's message before whatever went wrong, and a
                    # message stranded outside the conversation it was typed
                    # into is worse than a conversation holding a question that
                    # got no answer.
                    await conversations.append(conversation, since=since)
                    if first and pending:
                        attachment_chips, attachment_notice = await chat_attachments.finish_pending(
                            ctx,
                            _thread_rows,
                            user=user,
                            conversation=conversation,
                            opened=opened,
                            since=since,
                            typed=message,
                            pending=pending,
                        )
                        attachment_notice = _with_size_refusals(size_refusals, attachment_notice)
                    broken = _stopped_or_broken(
                        message if first else "",
                        out,
                        stopped=turn.stopping.is_set(),
                        attachments=attachment_chips,
                        attachment_notice=attachment_notice,
                        # A tool call can have left files behind before the
                        # turn stopped or broke — workspace contract §7, the
                        # same "what was kept is still shown" rule the
                        # attachment chips just above follow.
                        workspace_files=(
                            chat_workspace.chips_for_names(
                                conversation.conversation_id,
                                out.workspace_files,
                                pinned=chat_workspace.pinned_names_for(
                                    ctx.layout, conversation.conversation_id
                                ),
                            )
                            if conversation is not None and out.workspace_files
                            else ()
                        ),
                    )
                    # No `conversation=` here, deliberately unchanged: this
                    # turn already has one (it is `conversation`, above), and
                    # correcting the page's markers for it is not this frame's
                    # job any more. The `markers` frame already did it, before
                    # the first byte of a reply — a turn that fails mid-way is
                    # exactly the case that frame exists for, since it never
                    # reaches this `done` at all otherwise.
                    yield _frame(
                        "done", {"html": _fragment_html(request, [broken], autoplay=False)}
                    )
                    return

                # **Filed now, not when the exchange ends.** The header over a
                # reply is read back off the row the agent loop just wrote
                # (`_attributed_all`), and a row with no conversation on it is
                # not part of any conversation as far as `_grouped` is
                # concerned — it starts a group of its own, so `thread_records`
                # went on returning the *previous* exchange's rows and the
                # reply was headed with whoever spoke last in it. In a room
                # that is another character's name over somebody's words, which
                # is the one thing this feature must never do; with one persona
                # it was invisible, because the wrong row had the same name on
                # it. Nothing else here waits on this: it is one UPDATE, and a
                # failure leaves the reply unheaded rather than misheaded.
                await conversations.append(conversation, since=since)

                # What this reply cost, once — built here and nowhere else, so
                # the number rendered below and the number written to disk
                # (`_record_turn_metrics`) are the same object and cannot
                # drift apart. `None` for a turn that produced no result: see
                # `_refused` below, which carries no metrics of its own.
                metrics = (
                    None
                    if out.result is None
                    else TurnMetrics(
                        first_token_ms=out.first_token_ms,
                        total_ms=out.total_ms,
                        tokens=out.tokens,
                        first_audio_ms=out.first_audio_ms,
                        tools=tuple(out.tools),
                        # The real count, off the loop's own `DONE` event —
                        # see `_AdminChatResult.prompt_tokens`. `None` on a
                        # runner too old to carry it.
                        prompt_tokens=getattr(out.result, "prompt_tokens", None),
                    )
                )
                if metrics is not None:
                    # After `conversations.append` above, not before: the row
                    # this reads back has to be claimed by this conversation
                    # first, or a screen reading it back by conversation would
                    # not find it. `opened`, not `started` — see the comment
                    # where `opened` is computed; a brand new conversation has
                    # `started is None` and `_thread_rows(user, None)` is
                    # unconditionally empty.
                    await _record_turn_metrics(user, opened, metrics)
                if out.reasoning:
                    # Same ordering reason as the metrics write just above:
                    # after `conversations.append`, so the row this files
                    # under is already claimed by this conversation.
                    await _record_reasoning(user, opened, out.reasoning)
                if out.workspace_files:
                    # Same ordering reason again — workspace contract §7.
                    await _record_workspace_files(user, opened, out.workspace_files)

                if out.reply.strip():
                    # A turn that produced nothing said nothing, so there is
                    # nothing for the next character to read. It still counts
                    # as a turn — `spoke` below — because a persona failing to
                    # answer twice is a room that has to stop somewhere.
                    said.append(voices.Said(text=out.reply, who=mine, persona=True))
                exchange.spoke(who, out.reply)
                await _open_the_floor(user, exchange, said, labels)
                nxt = exchange.next()

                if first and pending:
                    # Only now does the row this files under exist — see
                    # `chat_attachments.finish_pending`'s own docstring.
                    attachment_chips, attachment_notice = await chat_attachments.finish_pending(
                        ctx,
                        _thread_rows,
                        user=user,
                        conversation=conversation,
                        opened=opened,
                        since=since,
                        typed=message,
                        pending=pending,
                    )
                    attachment_notice = _with_size_refusals(size_refusals, attachment_notice)

                # This character's own workspace-file cards (workspace
                # contract §7) — built once and handed to whichever of the
                # two branches below actually draws them, the same
                # `conversation is not None` guard `_stopped_or_broken`
                # above already needs (a stale marker naming a hidden
                # conversation, per the same comment on `conversation_id`
                # below).
                workspace_chips = (
                    chat_workspace.chips_for_names(
                        conversation.conversation_id,
                        out.workspace_files,
                        pinned=chat_workspace.pinned_names_for(
                            ctx.layout, conversation.conversation_id
                        ),
                    )
                    if conversation is not None and out.workspace_files
                    else ()
                )
                view = (
                    _refused(
                        message if first else "",
                        "The assistant returned nothing.",
                        attachments=attachment_chips if first else (),
                        attachment_notice=attachment_notice if first else "",
                        workspace_files=workspace_chips,
                    )
                    if out.result is None
                    else chat_exchange(
                        # Only the first reply carries the question: the person
                        # typed it once, and drawing it again above every
                        # character that answers would make one message look
                        # like three.
                        message if first else "",
                        out.result,
                        seconds=(out.total_ms or 0.0) / 1000.0,
                        speech=_offer(request, out.result, user),
                        metrics=metrics,
                        attachments=attachment_chips if first else (),
                        attachment_notice=attachment_notice if first else "",
                        reasoning=out.reasoning,
                        context_limit=context_limit,
                        workspace_files=workspace_chips,
                    )
                )
                # A reply that has just been read aloud does not read itself
                # aloud again. The player is still there for anyone who wants it
                # repeated. `announced`, not "audio was produced" — see
                # `_Spoken.announced`.
                #
                # `opened`, not `started` — see the comment where `opened` is
                # computed. The landing exchange is still the authority on
                # what this turn's conversation is (its own copy of the
                # markers is not going anywhere), and it has to be *the same*
                # authority as the `markers` frame already given out, not a
                # second, later guess.
                html = await _rendered(
                    request,
                    user,
                    [view],
                    opened,
                    autoplay=False if out.announced else None,
                )
                yield _frame("reply" if nxt is not None else "done", {"html": html})
                first = False
                who = nxt
        finally:
            await holding.release()

        await conversations.append(conversation, since=since)

    async def _stream_one(
        request: Request,
        user: AdminUser,
        *,
        persona: str,
        prompt: str,
        history: Sequence[ChatHistoryMessage],
        record: bool,
        room: Sequence[str],
        image_urls: Sequence[str] = (),
        conversation_id: str | None = None,
        workspace_ref: str | None = None,
        thinking: bool | None = None,
        aloud: bool,
        streaming: Any,
        holding: _TurnHolding,
        stopping: asyncio.Event,
        out: _Spoken,
    ) -> AsyncIterator[str]:
        """One character's turn, as frames, with what it cost left in ``out``.

        Exactly the turn this route has always run — same runner, same events,
        same live speech — lifted out so a room can run it more than once. The
        two things a room adds are ``record`` — the person typed their message
        once, so only the first turn of an exchange writes it to the transcript
        (see ``TurnRequest.record_user_message``) — and ``room``, who else is
        present, so the character about to speak knows it is not alone and can
        name somebody (§2, §3.3). Both are empty for a room of one, which is
        §7.

        ``image_urls`` is this exchange's attached image, if any, as ``data:``
        URIs — attachments contract §4.2. The caller has already decided
        whether ``streaming`` can be told (``_carries_images``); empty means
        either nothing was attached or the runner cannot carry it, and both
        read the same way here: nothing extra is passed to ``streaming``.

        ``conversation_id`` is the memory contract's own field
        (``working/contracts/memory.md`` §3.1) — passed to ``streaming`` only
        when ``_carries_conversation`` says it can be, the same discovery as
        ``image_urls``.

        ``workspace_ref`` is the real conversation id, workspace contract §7's
        own use of it and deliberately **not** the same value as
        ``conversation_id`` above: that one is gated on whether the agent
        loop can be told, this one only builds a download link for a live
        card, and a runner too old to accept ``conversation_id`` can still
        run a persona with a workspace on. Every file name still lands on
        ``out.workspace_files`` regardless — the caller needs the plain
        names either way, for the audit record and for the finished
        exchange's own cards, built once ``conversation`` is known not to be
        ``None``. ``workspace_ref`` being ``None`` only costs the *live*
        card, for the one turn that truly has no conversation yet (see the
        caller's own comment on ``conversation is None``): there is nothing
        to link a card to before that exists.

        ``thinking`` is this conversation's own override (thinking contract
        §13 D) — passed to ``streaming`` only when ``_carries_thinking`` says
        it can be, the same discovery as ``image_urls`` and
        ``conversation_id``. ``None`` either from the caller (no override
        set, or the runner cannot carry one) reads the same way to the loop:
        follow the answering persona's own switch.

        ``stopping`` is §4a's stop, handed straight to `_kept_alive` because
        that is where the turn is actually suspended. When it fires the loop
        below simply ends — no exception, no cancellation — and the caller
        finds ``out.result`` empty and ``stopping`` set. The tail is not
        spoken: `finished` stays False, and a reply cut off has no tail worth
        saying to somebody who just asked for silence.

        **Nothing here waits for speech.** Finished sentences are *put on a
        queue*; every engine call happens on the browser's separate request for
        the audio. An engine that is slow, stopped or broken costs the audio and
        cannot delay or truncate a word of the text.
        """
        began = time.monotonic()
        # Opened before the turn, because whether this reply can be heard has
        # to be answered while there is still nothing to say. `None` is a turn
        # that will not speak itself: no voice subsystem, no voice on this
        # persona, an engine switched off — or speech turned off for this
        # person (ADR-0030) or for this conversation (§6.2).
        live = (
            await begin_live(request, persona=persona, owner=user.id, started=began)
            if aloud
            else None
        )
        holding.live = live

        events = streaming(
            prompt,
            user=user.id,
            history=history,
            persona=persona,
            # Only when it is False — see the same call in `_turn`.
            **({} if record else {"record_user_message": False}),
            # And only when there is somebody else here — see the same call in
            # `_turn`, and `room_block` for why an empty room passes nothing.
            **({"also_present": list(room)} if room else {}),
            # Attachments contract §4.2 — see the same call in `_turn` and
            # `_turn_frames`'s own comment on why every persona's call in this
            # exchange carries it.
            **({"image_data_urls": list(image_urls)} if image_urls else {}),
            # Memory contract §3.1 — see the same call in `_turn`.
            **({"conversation_id": conversation_id} if conversation_id else {}),
            # Thinking contract §13 D. Gated on `_carries_thinking`, not on
            # `thinking is not None`: a runner too old for the keyword at
            # all must never see it, whatever value it would have carried,
            # and a runner that does take it treats `None` as "no override"
            # on its own — the same value it already defaults to.
            **({"thinking": thinking} if _carries_thinking else {}),
        )
        # THE MIDDLE OF THIS TURN IS `chat_turns.stream_frames`, and it is the
        # same lines a run's scripted step goes through (PLAN.md alpha.21).
        # Everything above this call is what only a screen can decide — who is
        # speaking, what the room was told, which keywords this runner takes,
        # whether the reply may speak itself — and everything below it, in the
        # caller, is what only a screen can draw. The frames themselves are one
        # implementation, because "byte-identical on the wire" is not something
        # two copies of a dispatch chain can be trusted to stay.
        async for piece in chat_turns.stream_frames(
            events,
            out=out,
            holding=holding,
            stopping=stopping,
            began=began,
            live=live,
            # `workspace_ref` is `None` only for the one turn that has no
            # conversation yet (see this function's own docstring): a card with
            # nothing to link to is not drawn rather than drawn broken.
            workspace_cards=(
                None
                if workspace_ref is None
                else lambda names: _workspace_cards_html(
                    chat_workspace.chips_for_names(
                        workspace_ref,
                        names,
                        pinned=chat_workspace.pinned_names_for(ctx.layout, workspace_ref),
                    )
                )
            ),
            # This module's own copy of the interval, so the monkeypatch that
            # shortens it still reaches the read — see `_kept_alive` above.
            keepalive=KEEPALIVE_SECONDS,
        ):
            yield piece

    # -- the same turn, for a caller with no request at all -----------------
    #
    # A runbook's model step is a turn in a person's conversation and has to
    # behave like one (PLAN.md alpha.21, the owner's decision): stream as it is
    # written, show the thinking box, leave the metrics line and the cards on
    # the row. It has no request, no form and no room — but everything else it
    # needs is built right here and nowhere else, so it is written down as one
    # object and put where a caller outside this package can read it
    # (`chat_turns.ENGINE_ATTRIBUTE`). Set on the router because that is what
    # this function is handed; `boot.surfaces` lifts it onto `app.state`.
    #
    # The runner reads it duck-typed and calls `start`, exactly as
    # `chat_run.py` reads `app.state.runner` — so `personacore.runbooks` still
    # imports nothing from `personacore.web`.

    async def _detached_html(owner: Any, opened: datetime, since: datetime) -> str:
        """This conversation's last exchange, drawn as a reload would draw it."""
        return await view.detached_exchange_html(owner, opened, since)

    def _detached_cards(conversation_id: str, names: Sequence[str]) -> str:
        return _workspace_cards_html(
            chat_workspace.chips_for_names(
                conversation_id,
                names,
                pinned=chat_workspace.pinned_names_for(ctx.layout, conversation_id),
            )
        )

    setattr(
        router,
        chat_turns.ENGINE_ATTRIBUTE,
        chat_turns.TurnEngine(
            app=None,
            chat=chat,
            audit=audit,
            conversations=conversations,
            thread_rows=_thread_rows,
            rendered=_detached_html,
            markers_html=_markers_html,
            workspace_cards=_detached_cards,
            # Asked once, of the streaming runner, exactly as `_tells_the_room`
            # and its neighbours above are and for the same reason: a runner
            # does not change shape between requests, and one too old for a
            # keyword raises on it rather than ignoring it.
            carries=frozenset(
                keyword
                for keyword in (
                    "conversation_id",
                    "thinking",
                    "temperature",
                    "pins_by_role",
                    "author_kind",
                )
                if _takes(getattr(chat, "stream", None), keyword)
            ),
        ),
    )
