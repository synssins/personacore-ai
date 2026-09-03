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

**One thing survived the move and stopped meaning what it looks like.**
``tests/server/test_chat_streaming.py`` shortens :data:`KEEPALIVE_SECONDS` by
patching it on ``chat``, which is now a re-export: :func:`_kept_alive` reads
this module's copy, so the patch no longer reaches it and that test waits the
whole ten seconds. It still proves what it was written to prove and it is ten
seconds slower for it. The fix is one name in the test, and it was left for
whoever is allowed to touch tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from personacore.admin.models import AdminUser
from personacore.audit.models import (
    AuditOutcome,
    AuditRecord,
    MessageRole,
    Owner,
    ReasoningRecord,
    Surface,
)
from personacore.voice.live import finished_prefix
from personacore.web.screens import chat_attachments
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
    _latency,
    _metrics_detail,
    _refused,
    chat_exchange,
)
from personacore.web.screens.chat_thread import (
    ChatHistoryMessage,
    conversation_start,
    wanted_conversation,
)
from personacore.web.shared import _readable

log = structlog.get_logger(__name__)


_PING = ": still here" + chr(10) * 2
"""One SSE comment frame. The reader ignores it; the socket carries a byte."""

_KEEPALIVE = object()
"""Yielded by :func:`_kept_alive` when the turn has gone quiet for a while."""

KEEPALIVE_SECONDS = 10.0
"""How long a streamed turn may say nothing before the socket is reminded it
is alive. Ten seconds is well inside the shortest idle timeout anything between
a browser and this core is likely to enforce, and far too coarse to matter to a
reply that is actually arriving."""


async def _kept_alive(events: Any, *, stopping: asyncio.Event | None = None) -> AsyncIterator[Any]:
    """``events``, with :data:`_KEEPALIVE` yielded whenever it goes quiet.

    The pending ``__anext__`` is held across a quiet spell rather than being
    cancelled and retried: cancelling it would drop the very event we were
    waiting for. That is why this is `asyncio.wait` on a task we keep, and not
    `asyncio.wait_for`.

    **The cancelled task is awaited before this returns**, and that is not
    tidiness. Driving ``__anext__`` from a separate Task leaves the underlying
    async generator marked as running until the cancellation has actually been
    delivered to it. `_TurnHolding.release` calls ``events.aclose()`` on the way
    out; against a generator still marked running that raises
    ``RuntimeError: aclose(): asynchronous generator is already running``, which
    was being swallowed — so ``release`` set its handle to ``None`` and believed
    it had let go of the agent turn and the LLM socket while both were still
    held. That is the leak `_TurnHolding` exists to prevent, and the first
    version of this function reintroduced it while fixing something else.

    ``stopping`` is §4a's reply-level stop, and this is the one place it can be
    honoured promptly: the turn spends nearly all its life suspended on the
    ``__anext__`` above, so anything that only checks a flag between events
    would not be felt until the model produced its next token — which on a
    twenty-minute reply can be a minute away. Waiting on the flag *beside* the
    read, and returning when it wins, ends the iteration exactly as the model
    running out of tokens does: the caller's ``async for`` finishes, its
    ``finally`` closes the generator, and the turn winds down through the path
    it already had. **Nothing is cancelled from underneath it.**
    """
    iterator = events.__aiter__()
    pending: asyncio.Task[Any] | None = None
    halt: asyncio.Task[Any] | None = None
    try:
        while True:
            if stopping is not None and stopping.is_set():
                return
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            watched: set[asyncio.Future[Any]] = {pending}
            if stopping is not None:
                if halt is None:
                    halt = asyncio.ensure_future(stopping.wait())
                watched.add(halt)
            done, _ = await asyncio.wait(
                watched,
                timeout=KEEPALIVE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if halt is not None and halt in done:
                return
            if pending not in done:
                yield _KEEPALIVE
                continue
            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:
                return
            # Outside the ``try``: a `CancelledError` raised by the *consumer*
            # of this yield is the reader going away, and must not be mistaken
            # for the iterator finishing.
            yield event
    finally:
        if halt is not None:
            halt.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await halt
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


def _frame(event: str, payload: dict[str, Any]) -> str:
    """One server-sent event: a name, a JSON body, a blank line.

    JSON rather than the raw text, for a reason that is not tidiness: a reply
    contains newlines, and a newline inside a ``data:`` line ends the frame.
    Encoding it means the reader never has to guess where a fragment stopped.
    """
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


async def _close_stream(events: Any) -> None:
    """Close a turn's event stream, whatever it thinks of the idea.

    A generator abandoned mid-iteration holds the turn and the connection to
    the model until something happens to collect it, and abandoning one is the
    *ordinary* end of a streamed reply — the reader closes the tab. Nothing
    here may raise: by the time this runs there is nobody left to tell, and a
    failure to hang up politely must not become the failure.
    """
    closer = getattr(events, "aclose", None)
    if closer is None:
        return
    with contextlib.suppress(Exception):
        await closer()


class _TurnHolding:
    """What one streamed turn is holding, and the one way to let it go.

    This exists because of a specific, checked fact about how a streamed
    response ends. Starlette watches for the client going away and **cancels
    the task that is writing the body**; the cancellation lands in its own
    writer, not inside the generator producing the frames, so that generator is
    simply abandoned — still suspended, still holding the agent turn, the
    connection to the model and a queue somebody is filling. Every uvicorn in
    this project announces ASGI spec 2.3, which is the version that takes that
    path, so this is the ordinary case and not a corner of it.

    The response therefore carries a background task that releases the same
    things. Releasing is idempotent, so whichever of the two gets there first
    is the one that does it and the other finds nothing left.
    """

    def __init__(self) -> None:
        self.live: Any = None
        self.events: Any = None
        self.forget: Any = None
        """How to take a finished exchange out of the stop registry.

        Set for a room with more than one persona in it, because that is the
        only kind of exchange the stop button can reach. Called on the way out
        alongside everything else this holds, so a stopped-or-finished exchange
        does not sit in the ring waiting to be evicted — a token that still
        resolves is a token somebody's stale tab can press.
        """

    async def release(self) -> None:
        live, self.live = self.live, None
        events, self.events = self.events, None
        forget, self.forget = self.forget, None
        if live is not None:
            # Synchronous and first: it ends the queue the turn was filling,
            # and it cannot raise, so it happens even if the close below is
            # itself interrupted.
            live.abandon()
        if forget is not None:
            with contextlib.suppress(Exception):
                forget()
        await _close_stream(events)


@dataclass(slots=True)
class _Spoken:
    """What one persona's streamed turn produced, gathered as it happened.

    A mutable object rather than a return value because the thing producing it
    is an async generator of frames: the frames have to reach the browser as
    they are made, and the numbers are only complete when it has finished. One
    small object passed in is the plain way to have both.
    """

    reply: str = ""
    result: Any = None
    tokens: int = 0
    first_token_ms: float | None = None
    total_ms: float | None = None
    tools: list[tuple[str, float | None]] = field(default_factory=list)
    announced: bool = False
    """The browser was told where to listen. Deciding on what was *promised*
    rather than on what arrived makes the answer the same every time — see
    where it is read."""

    first_audio_ms: float | None = None
    broke: str = ""
    """A sentence, when the turn did not finish. Empty otherwise."""

    reasoning: str = ""
    """The model's own reasoning, gathered exactly as ``reply`` is — every
    ``reasoning`` event's text, in order. Empty for the ordinary reply, which
    is most of them: a model that never reasons sends no ``reasoning`` events
    at all. Kept here, not only forwarded to the ``thinking`` frame, because
    the owner asked to be able to read this back later (2026-09-02) — see
    ``_record_reasoning``, which is what makes that true."""


# ---------------------------------------------------------------------------
# A turn that outlives the browser watching it — detached-turns contract §3
# ---------------------------------------------------------------------------

RUNNING_ATTRIBUTE = "chat_running_turns"
"""Where the running turns live on ``app.state``.

On the application rather than in a module global for the same reason the stop
registry beside it is (``chat_voices.STOP_ATTRIBUTE``): the test suite builds
several applications in one process, and a global would let one core's turn be
attached to — or stopped — through another's.
"""

REPLAY_BYTES = 2 * 1024 * 1024
"""How much of a running turn is kept for somebody who attaches late (§3.2).

Two megabytes, and the number is chosen against the worst turn this host
actually produces: fifteen thousand reasoning tokens is normal here, which is
sixty kilobytes of text plus a frame header each. This holds that several times
over and is still a fixed ceiling rather than however long a pasted message
runs.

**Oldest frames out first when it is reached**, which §3.2 permits explicitly
because a late attacher's *final* answer does not come from this buffer: the
``done`` frame carries the server's own rendering of the finished exchange, and
after that the transcript is the authority. The worst a full buffer costs is
the top of a very long reply while it is still being written.

The buffer is short-lived by construction — §7: as soon as the response is in
place the buffer can drop, since the transcript already carries everything
needed to resume the conversation — so this bounds one enormous turn, not
accumulation over a day.
"""

_ENDED = object()
"""Put on every subscriber's queue when the turn is over. Not a frame."""


class _RunningTurn:
    """One turn, running whether or not anybody is watching it.

    **Identity is the conversation's own marker** (§3.1): one conversation can
    only have one turn running at a time, the screen already posts that marker
    on every control, and ``fragments/chat_markers.html`` exists solely to keep
    it correct. The key is ``(owner, marker)``, so rule 3 — a member sees only
    their own turns — is enforced by the lookup itself rather than by a check
    somebody can forget to write.

    **A brand new conversation has no marker to be keyed by**, because the
    instant that names it is minted inside the turn (``opened``, below). Such a
    turn is therefore unregistered until :meth:`identify` is called with the
    same instant the ``markers`` frame carries — which is before the first word
    of the reply. Nothing can attach to it in that window, and nothing needs
    to: the marker a browser would attach with does not exist yet either.

    **The guard against running twice cannot be "same connection"** (§7),
    because deliberately it is not: three devices signed in as the same person
    watch one turn at the same time. It is "is a turn already running for this
    conversation", which is exactly what a lookup on this key answers.
    """

    def __init__(self, ring: dict[tuple[str, str], _RunningTurn], owner: str) -> None:
        self._ring = ring
        self.owner = owner
        self.key: tuple[str, str] | None = None
        self.holding = _TurnHolding()
        self.stopping = asyncio.Event()
        """Somebody pressed stop (§4a). Read by :func:`_kept_alive` beside the
        model's own next event, so it is felt in the middle of a reply and not
        only between them."""

        self.task: asyncio.Task[None] | None = None
        self.finished = False
        self._pinned: list[str] = []
        self._recent: deque[str] = deque()
        self._bytes = 0
        self._watchers: set[asyncio.Queue[Any]] = set()

    # -- identity ----------------------------------------------------------

    def identify(self, marker: str) -> None:
        """Register under the conversation this turn turned out to be in.

        Called the moment ``opened`` is known and with exactly the string the
        ``markers`` frame carries, so the marker a browser attaches with and
        the key it is found by cannot drift.

        A second turn already registered here is **replaced, not stopped**: it
        is a turn this one has already superseded — see the route, which stops
        the previous one before starting a replacement — and stopping it from
        here would end a turn twice for a reason that has nothing to do with
        identity.
        """
        if self.finished:
            return
        self.key = (self.owner, marker)
        self._ring[self.key] = self

    def _forget(self) -> None:
        """Take this turn out of the registry, if it is still the one in it.

        ``is self`` matters: a second send stops this turn and starts another
        under the same key, and this turn's own wind-down happens afterwards.
        Popping the key blindly would deregister the *replacement*.
        """
        key = self.key
        if key is not None and self._ring.get(key) is self:
            del self._ring[key]

    # -- what a late attacher gets ----------------------------------------

    def publish(self, frame: str) -> None:
        """One frame, to everybody attached now and to whoever attaches next.

        Synchronous, and it has to stay that way: :meth:`subscribe` snapshots
        the buffer and adds its queue in one unbroken block, so an ``await``
        anywhere in here would open the window where a frame reaches neither
        the snapshot nor the queue — or, the other way round, both.
        """
        if frame is not _PING:
            # A comment frame keeps a socket warm and says nothing. Replaying
            # one to somebody who has just attached is bytes with no meaning,
            # and it would spend the buffer this turn's actual words need.
            self._remember(frame)
        for queue in self._watchers:
            queue.put_nowait(frame)

    def _remember(self, frame: str) -> None:
        if frame.startswith(("event: markers", "event: exchange")):
            # Never evicted. Between them they are two small frames that say
            # which conversation this is and which exchange the room's stop
            # button reaches — both of which a late attacher needs to be
            # *correct*, not merely complete. Dropping the oldest frames is
            # only safe while the oldest frames are words.
            self._pinned.append(frame)
            return
        self._recent.append(frame)
        self._bytes += len(frame)
        while self._recent and self._bytes > REPLAY_BYTES:
            self._bytes -= len(self._recent.popleft())

    def subscribe(self) -> tuple[asyncio.Queue[Any], list[str]]:
        """A queue of what happens next, and a copy of what already happened.

        The two are taken together, with nothing awaited in between, which is
        the whole of the correctness argument: on one event loop that makes the
        pair atomic against :meth:`publish`, so a frame produced at this exact
        moment lands in the replay or in the queue and never in neither or
        both.

        The queue is unbounded on purpose. It holds at most what one turn
        produces, it is dropped the moment its reader's response ends, and
        bounding it would mean choosing between a wedged socket and a gap in
        the middle of somebody's reply.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        replay = [*self._pinned, *self._recent]
        if self.finished:
            queue.put_nowait(_ENDED)
        else:
            self._watchers.add(queue)
        return queue, replay

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        """One reader has gone. **This is not stopping** (§5 rule 5) — it takes
        a queue out of a set and touches nothing else the turn is holding."""
        self._watchers.discard(queue)

    # -- the two ways it ends ---------------------------------------------

    def stop(self) -> None:
        """§4a's stop: end the reply being written.

        Deregistered here rather than only on the way out, so the conversation
        is free for the next turn immediately — a second send stops this one
        and starts another under the same key in the same breath.
        """
        self.stopping.set()
        self._forget()

    def finish(self) -> None:
        """The turn is over. Tell everyone attached, and drop the buffer.

        §7: as soon as the response is in place the buffer can drop, because
        the conversation can always be resumed from the transcript, which
        already carries the whole of the context. The live
        buffer exists only between a turn starting and its transcript row
        landing; after that the transcript is the only authority and it already
        survives everything.

        Readers mid-replay are unaffected — :meth:`subscribe` handed them their
        own copy of the list, not this one.
        """
        if self.finished:
            return
        self.finished = True
        self._forget()
        self._pinned = []
        self._recent.clear()
        self._bytes = 0
        watchers, self._watchers = self._watchers, set()
        for queue in watchers:
            queue.put_nowait(_ENDED)


STOPPED_REPLY = "You stopped this reply."
"""What the card where the answer would have been says (§4a).

Plain, and it names who did it: the one thing a person needs to know when a
half-written reply disappears is that it was them and not a failure.
"""


def _stopped_or_broken(
    message: str,
    out: _Spoken,
    *,
    stopped: bool,
    attachments: Sequence[Any] = (),
    attachment_notice: str = "",
) -> dict[str, Any]:
    """The card a turn that produced no reply leaves behind.

    **THIS IS THE CHOICE POINT §4a LEAVES OPEN, AND IT IS THE ONLY ONE.**
    The owner has not answered it: whether a stopped reply is kept as far as
    it got, or discarded. Keeping it means a partial answer in the transcript, which is
    then history the next turn reads back. Discarding it means twenty minutes
    of work vanishing on purpose. The room-level stop dodges the question by
    letting the reply *finish* — which a reply-level stop cannot do, because
    finishing is the thing being stopped.

    What ships is **discarded**, and that is what falls out rather than what
    was chosen: the agent loop writes the assistant row when its turn
    completes, so a turn broken off mid-stream writes none. ``out.reply`` holds
    the words that did arrive and nothing here reads it.

    The other answer is one branch in this function — write ``out.reply`` as an
    assistant row and render it as an ordinary reply — and it is **not** written
    here, deliberately. This screen is not the transcript's writer: the
    chat-room contract says the agent loop is, and the one place that rule is
    already bent (``chat_exchange._recorded_unanswered``) carries a paragraph
    explaining why it had to be and writes a *user* row, not an assistant one.
    Bending it a second time, for an answer nobody has given, is a decision
    with a shape — whose name goes over a half-sentence, what the next turn
    reads back — and it belongs to the owner.
    """
    return _refused(
        message,
        STOPPED_REPLY if stopped else out.broke,
        attachments=attachments,
        attachment_notice=attachment_notice,
    )


def _turns(app: Any) -> dict[tuple[str, str], _RunningTurn]:
    """The running turns on this application, made on first use."""
    ring: dict[tuple[str, str], _RunningTurn] | None = getattr(app.state, RUNNING_ATTRIBUTE, None)
    if ring is None:
        ring = {}
        setattr(app.state, RUNNING_ATTRIBUTE, ring)
    return ring


def _keyed(owner: str, marker: str | None) -> tuple[str, str] | None:
    """``(owner, marker)`` in the one canonical spelling, or ``None``.

    Round-tripped through :func:`conversation_start` rather than used as typed,
    so the plus-eaten form a query string produces
    (``chat_thread._PLUS_EATEN``) and the properly encoded one land on the same
    key. A marker that names no instant names no turn.
    """
    started = conversation_start(wanted_conversation(marker))
    if started is None:
        return None
    return (owner, started.isoformat())


def running_turn(request: Request, owner: str, marker: str | None) -> bool:
    """Whether a turn is running in this person's conversation right now.

    Read by the screen so a page opened — or reopened — while a reply is still
    being written can attach to it instead of showing a conversation that looks
    finished (§6: coming back to a conversation should find it exactly where
    it was left).
    """
    key = _keyed(owner, marker)
    if key is None:
        return False
    ring = getattr(request.app.state, RUNNING_ATTRIBUTE, None) or {}
    return key in ring


def stop_turn(request: Request, marker: str | None, owner: str) -> bool:
    """§4a — halt the reply being written in this person's conversation.

    ``True`` when a turn was stopped. An unknown marker, somebody else's
    conversation and a turn that has already finished are one answer, for the
    same reason ``chat_voices.stop`` gives one: which it was belongs in the log
    and not in the response.

    **This is the sibling of the room's stop, not a replacement for it.**
    ``chat_voices.stop`` ends an *exchange* at the current turn's boundary —
    the reply being written finishes and nobody else is asked — and only ever
    exists for a room. This ends the reply itself, which is the thing a solo
    conversation has no button for at all, and is now the only way to end a
    turn somebody has walked away from.
    """
    key = _keyed(owner, marker)
    if key is None:
        return False
    ring = getattr(request.app.state, RUNNING_ATTRIBUTE, None) or {}
    turn = ring.get(key)
    if turn is None:
        return False
    turn.stop()
    # Counts and timings only — never a frame's contents (rule 4).
    log.info("chat_turn_stopped")
    return True


async def _attached(
    turn: _RunningTurn, queue: asyncio.Queue[Any], replay: list[str]
) -> AsyncIterator[str]:
    """One subscriber's view of a turn: what it missed, then what happens next.

    **The ``finally`` unsubscribes and does nothing else, and that is the whole
    fix.** This generator used to *be* the turn: a reader who went away raised
    `CancelledError` at the ``yield``, the ``finally`` cancelled the pending
    read and released the model connection, and a tablet locking its screen
    ended a twenty-minute reply. Detaching is not stopping (§5 rule 5).

    The response also carries a background task that removes the same queue,
    for the reason `_TurnHolding` documents: on the ASGI version every uvicorn
    in this project speaks, a reader who closes the tab leaves this generator
    *abandoned* rather than closed, so its ``finally`` may never run at all.
    Whichever of the two gets there first does it; ``discard`` makes the other
    a no-op.
    """
    try:
        for frame in replay:
            yield frame
        while True:
            frame = await queue.get()
            if frame is _ENDED:
                return
            yield frame
    finally:
        turn.unsubscribe(queue)


async def _dropped(turn: _RunningTurn, queue: asyncio.Queue[Any]) -> None:
    """The response's own way of saying this reader has gone. See `_attached`."""
    turn.unsubscribe(queue)


def _watching(turn: _RunningTurn) -> StreamingResponse:
    """A response that watches ``turn`` and has no power over it."""
    queue, replay = turn.subscribe()
    return StreamingResponse(
        _attached(turn, queue, replay),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            # Without this the reverse proxy in front of this core (spec §7)
            # buffers the whole response and hands it over at the end, which
            # is precisely the behaviour this route exists to stop.
            "X-Accel-Buffering": "no",
        },
        background=BackgroundTask(_dropped, turn, queue),
    )


async def _drive(turn: _RunningTurn, frames: AsyncIterator[str]) -> None:
    """Run one turn to its end, feeding everybody attached to it.

    This is the task the application owns. Nothing in here consults a request,
    a connection or a subscriber: a turn nobody is watching runs exactly as
    fast and finishes exactly as completely as one three tablets are watching,
    which is §3 in one sentence.

    §3.3 — *nothing waits for a reader*. When the frames run out the transcript
    has been written by the path that always wrote it (§5 rule 1), the model is
    released, the buffer is dropped and this turn is out of the registry. A
    turn nobody ever comes back to is a completed conversation, not a held
    resource.
    """
    try:
        async for frame in frames:
            turn.publish(frame)
    except asyncio.CancelledError:
        # The application is going down. Let it.
        raise
    except Exception as exc:  # noqa: BLE001 - a dead task must still be buried
        # Never the frame, never the reply — the error only (rule 4).
        log.error("chat_turn_task_failed", error=repr(exc))
    finally:
        await turn.holding.release()
        turn.finish()


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
    _turn = exchange.turn
    _answered_by_kind = exchange.answered_by_kind
    _open_the_floor = exchange.open_the_floor
    _recorded_unanswered = exchange.recorded_unanswered
    _unsendable = exchange.unsendable
    _rendered = exchange.rendered
    _fragment_html = exchange.fragment_html
    _markers_template = ctx.templates.get_template("fragments/chat_markers.html")

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
        turn = _RunningTurn(ring, user.id)
        if key is not None:
            turn.identify(key[1])
        # Subscribed **before** the task exists, so the first frame cannot be
        # produced before there is somewhere to put it.
        watching = _watching(turn)
        turn.task = asyncio.create_task(
            _drive(turn, _turn_frames(request, user, message, started, turn, uploads))
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

                view = (
                    _refused(
                        message if first else "",
                        "The assistant returned nothing.",
                        attachments=attachment_chips if first else (),
                        attachment_notice=attachment_notice if first else "",
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

        buffer = ""
        spoken = 0
        finished = False
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
        )
        holding.events = events
        try:
            async for event in _kept_alive(events, stopping=stopping):
                if event is _KEEPALIVE:
                    # A comment frame: the reader ignores it, and the socket
                    # has carried a byte. Nothing was written here until the
                    # model's first token, so a turn that thinks for a minute
                    # before speaking looked exactly like a dead connection --
                    # and got closed as one, with the reply lost.
                    yield _PING
                    continue
                kind = getattr(event, "kind", "")
                if kind == "text":
                    text = str(getattr(event, "text", "") or "")
                    if not text:
                        continue
                    out.tokens += 1
                    if out.first_token_ms is None:
                        out.first_token_ms = (time.monotonic() - began) * 1000.0
                    buffer += text
                    yield _frame("delta", {"text": text})
                    if live is not None:
                        if not out.announced:
                            out.announced = True
                            # §6.1: the browser is told where to listen and
                            # queues it behind whatever is still playing. One
                            # voice at a time is decided there, because that is
                            # where the playing happens.
                            #
                            # Announced on the FIRST WORD, not on the first
                            # finished sentence. `complete_sentences` only
                            # marks off text that something was written after,
                            # so a one-sentence reply never reached the line
                            # below — and a one-sentence reply is the ordinary
                            # case in a room. What used to happen then was that
                            # the finished exchange autoplayed instead, through
                            # `data-play-once`, which admin.js fires per swap
                            # with nothing between two of them: two characters
                            # talking over each other, which is exactly what
                            # §6.1 exists to prevent. Announcing here puts every
                            # spoken reply through the one queue.
                            #
                            # The browser's fetch then waits on `LiveSpeech.start`
                            # until there is something to say — which for a
                            # single sentence is the tail pushed by `close`
                            # below. It is the same audio, one round trip
                            # sooner.
                            yield _frame("speech", {"url": live.audio_url})
                        ready, spoken = finished_prefix(buffer, spoken, live.pacing)
                        if ready:
                            # A put on a queue. Nothing here touches an engine,
                            # a thread or a socket.
                            live.add(ready)
                elif kind == "reasoning":
                    # Forwarded as its own frame, exactly as before, and now
                    # ALSO gathered onto `out.reasoning` — never into `buffer`
                    # (that is the reply, and `_close_stream`/`live.close`
                    # below speak and transcript exactly that), never counted
                    # in `out.tokens`, and never logged: it is conversation
                    # content the same way a reply is (`ReasoningRecord`'s own
                    # docstring). A model that never reasons sends no
                    # `reasoning` events at all, so this branch never fires and
                    # the ordinary turn is unchanged — no row, no line, same as
                    # before the owner asked for this to survive a reload.
                    text = str(getattr(event, "text", "") or "")
                    if text:
                        out.reasoning += text
                        yield _frame("thinking", {"text": text})
                elif kind == "tool_call":
                    name = str(getattr(event, "tool_name", "") or "")
                    if name:
                        yield _frame("tool", {"name": name})
                elif kind == "tool_result":
                    name = str(getattr(event, "tool_name", "") or "")
                    took = getattr(event, "duration_ms", None)
                    if name:
                        out.tools.append((name, float(took) if took is not None else None))
                        yield _frame("tool_done", {"name": name, "took": _latency(took)})
                elif kind == "notice":
                    yield _frame("notice", {"text": str(getattr(event, "text", "") or "")})
                elif kind == "done":
                    out.result = getattr(event, "result", None)
                    finished = True
        except Exception as exc:  # noqa: BLE001 - the status went out long ago
            # Mid-stream, so there is no status left to change: the frames say
            # what happened and the stream closes properly. A reader that never
            # sees `done` waits for a reply that is not coming.
            log.error("chat_stream_failed", error=repr(exc))
            out.broke = f"The turn did not finish: {_readable(exc)}"
        finally:
            if live is not None and finished:
                # The tail — whatever was still mid-sentence when the reply
                # ended. Only on a turn that finished: a reply cut off by the
                # reader leaving has no tail worth speaking to nobody.
                live.close(buffer[spoken:])
            # Read now rather than waited for. A reply is never made to wait on
            # speech, and that includes waiting to find out how fast it was.
            out.first_audio_ms = None if live is None else live.first_audio_ms
            out.total_ms = (time.monotonic() - began) * 1000.0
            out.reply = buffer
            # The events are finished with; the live stream is not — the
            # browser may still be fetching it, and the next character's turn
            # will queue behind it. Only the generator is let go here.
            holding.events = None
            await _close_stream(events)
