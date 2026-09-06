"""What makes a turn a turn, with no HTTP anywhere in it.

**One turn engine, two callers.** ``POST /admin/chat/stream``
(:mod:`personacore.web.screens.chat_streaming`) is one; a runbook's scripted
model step (:mod:`personacore.runbooks.runner`) is the other. Before this
module existed the second had its own thin reader that understood ``text``
events and discarded everything else, registered nothing anybody could attach
to, and wrote neither the metrics record nor the reasoning record a reload
draws its chrome from. A page open on a run's conversation therefore showed a
prompt and then, after a refresh, a bare reply — not because anything branched
on who was speaking, but because the data a person's turn leaves behind was
never written.

So the lifecycle moved here, unchanged, and both callers use it:

* the ring of running turns on ``app.state`` (:data:`RUNNING_ATTRIBUTE`,
  :class:`_RunningTurn`, :func:`running_turn`, :func:`stop_turn`) — one turn
  per ``(owner, marker)``, which is what lets a page attach to a turn it did
  not start;
* the keepalive-and-stop read (:func:`kept_alive`) and the mapping from the
  admin chat runner's events to the frames ``chat.js`` understands
  (:func:`stream_frames`) — so a run's ``delta``/``thinking``/``tool``/
  ``tool_done``/``workspace_files`` frames are the same bytes a person's turn
  produces, because they are produced by the same lines;
* :func:`start_turn`, the door for a caller with no request at all: it
  registers the turn, drives it, records this turn's metrics and reasoning
  under the reply row's own correlation id, and ends with the ``done`` frame
  carrying exactly what a reload of that conversation would draw.

**This module knows nothing about runbooks**, and it must not learn: it takes
an ``on_text`` callback and hands back something with ``stop()`` on it, and the
watchdog, the deadline and the partial file stay in the runner where they
belong. It also knows nothing about a :class:`~fastapi.Request` beyond passing
one to Starlette's own response — everything request-shaped
(:class:`TurnEngine`'s renderer, the templates, the operator's own preferences)
is handed over at registration by :mod:`personacore.web.screens.chat_streaming`
and read back off ``app.state``.

**The lifecycle notes below were written against the HTTP path and are still
about it**, because the failure they describe is the reader closing the tab and
that is still the ordinary end of a streamed reply. A detached turn started by
:func:`start_turn` has no reader at all, which is the easy case of the same
rule: the turn is a task the application owns, nothing about it depends on
anybody watching, and the transcript is written when the turn completes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from personacore.audit.models import (
    AuditOutcome,
    AuditRecord,
    AuthorKind,
    MessageRole,
    Owner,
    ReasoningRecord,
    Surface,
)
from personacore.voice.live import finished_prefix
from personacore.web.screens import chat_workspace
from personacore.web.screens.chat_reply import (
    TURN_METRICS_ACTION,
    TURN_METRICS_CATEGORY,
    TurnMetrics,
    _latency,
    _metrics_detail,
    _refused,
)
from personacore.web.screens.chat_thread import conversation_start, wanted_conversation
from personacore.web.shared import _readable

log = structlog.get_logger(__name__)


_PING = ": still here" + chr(10) * 2
"""One SSE comment frame. The reader ignores it; the socket carries a byte."""

_KEEPALIVE = object()
"""Yielded by :func:`kept_alive` when the turn has gone quiet for a while."""

KEEPALIVE_SECONDS = 10.0
"""How long a streamed turn may say nothing before the socket is reminded it
is alive. Ten seconds is well inside the shortest idle timeout anything between
a browser and this core is likely to enforce, and far too coarse to matter to a
reply that is actually arriving."""


async def kept_alive(
    events: Any,
    *,
    stopping: asyncio.Event | None = None,
    seconds: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[Any]:
    """``events``, with :data:`_KEEPALIVE` yielded whenever it goes quiet.

    ``seconds`` is the quiet spell, passed in rather than read off this
    module's own :data:`KEEPALIVE_SECONDS`, so the caller's copy of that name
    is the one in force. That is not a preference: ``test_chat_streaming.py``
    shortens the wait by patching it on the module it calls this through, and a
    read of this module's global would leave that patch landing on nothing —
    the exact "a monkeypatch on a re-export is a test that has quietly stopped
    testing" failure that file's own comment describes.

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
                timeout=seconds,
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


def frame(event: str, payload: dict[str, Any]) -> str:
    """One server-sent event: a name, a JSON body, a blank line.

    JSON rather than the raw text, for a reason that is not tidiness: a reply
    contains newlines, and a newline inside a ``data:`` line ends the frame.
    Encoding it means the reader never has to guess where a fragment stopped.
    """
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


async def close_stream(events: Any) -> None:
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
        await close_stream(events)


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

    workspace_files: list[str] = field(default_factory=list)
    """Every workspace file this turn's own tool calls left behind — workspace
    contract §7 — gathered off each ``tool_result`` event's own ``detail``
    (``AgentEvent(type=TOOL_RESULT).detail["files"]``, the agent loop's own
    joint), in the order the tool calls finished. Empty for a persona with no
    workspace, or one whose tools produced nothing to keep — most turns,
    which never touch this field at all. Turned into cards for the finished
    exchange by ``chat_workspace.chips_for_names`` and into the one audit
    record a reload reads back by (``_record_workspace_files``)."""


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
        """Somebody pressed stop (§4a). Read by :func:`kept_alive` beside the
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
    workspace_files: Sequence[Any] = (),
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
        workspace_files=workspace_files,
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


async def _drive(turn: _RunningTurn, frames: AsyncIterator[str]) -> Exception | None:
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

    **Returns the exception, rather than only logging it.** A frames generator
    can fail before it ever reaches its own ``handle.settle(...)`` — a bad
    persona, ``engine.markers_html`` itself raising, anything synchronous
    before the first frame — and used to be swallowed right here: the task
    still finished, :meth:`TurnHandle.result` still came back with the
    all-defaults ``ReplyOutcome`` (empty text, ``stopped`` false, ``error``
    ``None``), and a caller reading that answer saw a silent, empty success
    rather than the failure that actually happened. :meth:`TurnHandle._run`
    is what turns this return value into an honest outcome when nobody ever
    called :meth:`TurnHandle.settle`.
    """
    caught: Exception | None = None
    try:
        async for frame in frames:
            turn.publish(frame)
    except asyncio.CancelledError:
        # The application is going down. Let it.
        raise
    except Exception as exc:  # noqa: BLE001 - a dead task must still be buried
        # Never the frame, never the reply — the error only (rule 4).
        log.error("chat_turn_task_failed", error=repr(exc))
        caught = exc
    finally:
        await turn.holding.release()
        turn.finish()
    return caught

# ---------------------------------------------------------------------------
# The frames one model turn produces — the one mapping, for both callers
# ---------------------------------------------------------------------------


async def stream_frames(
    events: Any,
    *,
    out: _Spoken,
    holding: _TurnHolding,
    stopping: asyncio.Event,
    began: float,
    live: Any = None,
    workspace_cards: Callable[[Sequence[str]], str] | None = None,
    on_text: Callable[[str], None] | None = None,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    """One model turn's events, as the frames ``chat.js`` reads.

    Lifted out of ``chat_streaming._stream_one`` **unchanged** — the same
    branches in the same order producing the same JSON — because "the same
    bytes on the wire" is the whole requirement a run's turn had to meet, and
    the only way to keep two copies of this saying the same thing is to have
    one.

    What was a closure over the screen is now four arguments:

    ``live``
        the speech handle, or ``None`` for a turn nothing listens to. A run's
        turn is always ``None``: nobody is in the room to hear it.
    ``workspace_cards``
        given this tool call's own new file names, the markup for their cards,
        or ``""``. ``None`` when there is nothing to link a card to — the
        caller has already decided (``workspace_ref`` in the screen's own
        version), because a card with no download behind it is not drawn
        rather than drawn broken.
    ``on_text``
        every text delta, and only text: never reasoning. This is the runner's
        watchdog seam and nothing else reads it. Called before the frame is
        yielded, so a watchdog that trips on this delta has already called
        ``stop()`` by the time the frame is out.
    ``keepalive``
        how long the turn may say nothing before a comment frame is sent, read
        from the caller's own copy of :data:`KEEPALIVE_SECONDS`.

    ``out`` is filled in as it goes and is complete when this returns: the
    caller reads the reply, the counts and the timings off it.
    """
    buffer = ""
    spoken = 0
    finished = False
    holding.events = events
    try:
        async for event in kept_alive(events, stopping=stopping, seconds=keepalive):
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
                if on_text is not None:
                    on_text(text)
                yield frame("delta", {"text": text})
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
                        # The browser's fetch then waits on
                        # `LiveSpeech.start` until there is something to
                        # say — which for a single sentence is the tail
                        # pushed by `close` below. It is the same audio, one
                        # round trip sooner.
                        yield frame("speech", {"url": live.audio_url})
                    ready, spoken = finished_prefix(buffer, spoken, live.pacing)
                    if ready:
                        # A put on a queue. Nothing here touches an engine,
                        # a thread or a socket.
                        live.add(ready)
            elif kind == "reasoning":
                # Forwarded as its own frame, exactly as before, and now
                # ALSO gathered onto `out.reasoning` — never into `buffer`
                # (that is the reply, and `close_stream`/`live.close`
                # below speak and transcript exactly that), never counted
                # in `out.tokens`, never handed to `on_text`, and never
                # logged: it is conversation content the same way a reply is
                # (`ReasoningRecord`'s own docstring). A model that never
                # reasons sends no `reasoning` events at all, so this branch
                # never fires and the ordinary turn is unchanged — no row, no
                # line, same as before the owner asked for this to survive a
                # reload.
                text = str(getattr(event, "text", "") or "")
                if text:
                    out.reasoning += text
                    yield frame("thinking", {"text": text})
            elif kind == "tool_call":
                name = str(getattr(event, "tool_name", "") or "")
                if name:
                    yield frame("tool", {"name": name})
            elif kind == "tool_result":
                name = str(getattr(event, "tool_name", "") or "")
                took = getattr(event, "duration_ms", None)
                if name:
                    out.tools.append((name, float(took) if took is not None else None))
                    yield frame("tool_done", {"name": name, "took": _latency(took)})
                # Workspace contract §7, the joint: `detail["files"]` on a
                # `TOOL_RESULT` event is the agent loop's own list of bare
                # filenames this one call wrote into the conversation's
                # workspace — empty, or the key simply absent, for a tool
                # call that kept nothing, which is most of them. `getattr`
                # rather than a plain attribute read because `event` is
                # built outside this package (`ChatStreamEvent`'s own
                # docstring: "an unknown kind is ignored"), and the same
                # defensiveness extends to a `detail` that is not a dict.
                detail = getattr(event, "detail", None)
                files = detail.get("files") if isinstance(detail, dict) else None
                new_files = [str(item) for item in files] if isinstance(files, list) else []
                if new_files:
                    out.workspace_files.extend(new_files)
                    # Live, so a card appears under the growing reply
                    # without waiting for the whole turn to finish —
                    # `workspace_cards` is `None` only for a turn that has
                    # no conversation yet, and a card with nothing to link
                    # to is not drawn rather than drawn broken.
                    if workspace_cards is not None:
                        html = workspace_cards(new_files)
                        if html:
                            yield frame("workspace_files", {"html": html})
            elif kind == "notice":
                yield frame("notice", {"text": str(getattr(event, "text", "") or "")})
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
        await close_stream(events)


# ---------------------------------------------------------------------------
# The door for a caller with no request — PLAN.md alpha.21's Joints
# ---------------------------------------------------------------------------

ENGINE_ATTRIBUTE = "chat_turn_engine"
"""Where the assembled engine lives on ``app.state``.

Put there by ``chat_streaming.register`` and relayed up through the routers
that build it (``web.routes`` then ``admin.routes`` then ``boot.surfaces``),
for the same reason the running-turn ring is on the application and not in a
module global: the test suite builds several applications in one process, and a
global would let one core's turn be started — or attached to — through
another's.
"""


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """One scripted turn, as the caller asks for it.

    The fields are the admin chat runner's own ``stream`` keywords and nothing
    else: this is a description of a turn, not of a runbook, and a field only a
    run would ever set does not belong here.
    """

    message: str
    persona: str | None = None
    conversation_id: str | None = None
    thinking: bool | None = None
    temperature: float | None = None
    pins_by_role: Mapping[str, str] | None = None
    author_kind: AuthorKind | None = None
    """Runbook contract §3: whose rows these are. Stamped on every row the
    loop writes for this turn, exactly as the direct call stamped them, so
    ``conversation_history`` keeps dropping a run's rows from what the next
    typed message composes and ``transcript_exchanges`` keeps folding a run's
    own prompt away."""


@dataclass(slots=True)
class ReplyOutcome:
    """What a finished turn produced, for the caller that started it."""

    reply_text: str = ""
    """Everything the model wrote, including a reply that was stopped or
    broken off part-way — the transcript may hold none of it (the loop writes
    the assistant row when its turn completes), so this is the only copy a
    watchdog's ``.partial`` file can be written from."""

    correlation_id: str | None = None
    """The reply row's own id, or ``None`` for a turn that wrote no row."""

    stopped: bool = False
    error: str | None = None


class TurnHandle:
    """A turn that has been registered and is about to run.

    Handed back the instant the turn is in the ring, before the first token,
    so the caller can subscribe to it (the HTTP route) or hold on to it for a
    watchdog (the runner) without racing the first frame.
    """

    __slots__ = ("_done", "_out", "_outcome", "_settled", "running")

    def __init__(self, running: _RunningTurn) -> None:
        self.running = running
        """The live turn. The HTTP route streams from *this* rather than
        looking the turn up in the ring a second time: two lookups is two
        chances to answer with a different turn than the one just started."""

        self._done = asyncio.Event()
        self._outcome = ReplyOutcome()
        self._settled = False
        """Whether :meth:`settle` was ever called. Read by :meth:`_run`'s
        ``finally`` — not by inspecting ``_outcome``, because a genuinely
        settled outcome can itself hold every default value, and that must
        never be mistaken for "nobody settled this"."""
        self._out: _Spoken | None = None
        """The frames' own :class:`_Spoken`, if :meth:`drive` was given one.
        The only place :meth:`_run` can find "whatever text arrived, if any"
        for the fallback outcome below — a caller that never reads
        :meth:`result` (the HTTP route) never passes one, and does not need
        to."""

    def drive(self, frames: AsyncIterator[str], *, out: _Spoken | None = None) -> None:
        """Start the task that runs this turn to its end.

        ``out`` is the same object the frames generator fills in as it runs —
        :func:`start_turn`'s caller has one; the HTTP route's does not, because
        it never awaits :meth:`result` at all.
        """
        self._out = out
        self.running.task = asyncio.create_task(self._run(frames))

    async def _run(self, frames: AsyncIterator[str]) -> None:
        """Drive the frames, then make sure :meth:`result` has an honest answer.

        **A task that finishes without :meth:`settle` having been called is
        not a success.** That used to be exactly what the default
        ``ReplyOutcome()`` looked like — empty text, ``stopped`` false,
        ``error`` ``None`` — indistinguishable from a turn that genuinely said
        nothing. So when :meth:`settle` never ran, this builds the outcome
        explicitly instead of leaving the caller to read defaults as if they
        were an answer: whatever text the frames did manage to gather (``""``
        when nothing ever arrived), the stop flag exactly as it stands, and an
        ``error`` that says the turn ended without a reply — carrying
        ``_drive``'s own exception message when it has one, because "ended
        without a reply" alone is a fact with no cause attached.
        """
        exc: Exception | None = None
        try:
            exc = await _drive(self.running, frames)
        finally:
            if not self._settled:
                reason = "the turn ended without a reply"
                if exc is not None:
                    reason = f"{reason}: {_readable(exc)}"
                self._outcome = ReplyOutcome(
                    reply_text="" if self._out is None else self._out.reply,
                    correlation_id=None,
                    stopped=self.running.stopping.is_set(),
                    error=reason,
                )
            self._done.set()

    def settle(self, outcome: ReplyOutcome) -> None:
        """What this turn produced, recorded for whoever is waiting on it."""
        self._outcome = outcome
        self._settled = True

    async def result(self) -> ReplyOutcome:
        """Wait for the turn to finish and say what it produced.

        Awaitable more than once, and awaiting it is not holding it: a caller
        whose own wait is cancelled — a watchdog's deadline — has let go of
        this and touched nothing the turn is doing.
        """
        await self._done.wait()
        return self._outcome

    def stop(self) -> None:
        """ADR-0043's stop: end the reply being written.

        Nothing is cancelled from underneath the turn — the iteration ends,
        the generator is closed through its own path, and the caller finds
        ``stopped`` set on :meth:`result`.
        """
        self.running.stop()


@dataclass(frozen=True, slots=True)
class TurnEngine:
    """Everything a detached turn needs that only the screen can build.

    Assembled once by ``chat_streaming.register`` and read back off
    ``app.state``. Every field was a closure inside that ``register``; they are
    written down here for the reason
    :class:`~personacore.web.screens.chat.ChatView` gives for the same shape —
    a joint two callers share has to exist as an object, or each of them
    invents one.
    """

    app: Any
    chat: Any
    """The admin chat runner (``ctx.chat``). ``stream`` is asked of it with
    ``getattr``, exactly as the screen asks."""

    audit: Any
    conversations: Any
    thread_rows: Any
    """``async (owner, started) -> rows`` — the thread being spoken in."""

    rendered: Any
    """``async (owner, opened, since) -> str`` — everything said in this
    conversation since ``since``, drawn exactly as a reload would draw it."""

    markers_html: Any
    """``(marker) -> str`` — ``fragments/chat_markers.html`` on its own."""

    workspace_cards: Any
    """``(conversation_id, names) -> str`` — the live cards for one tool
    call's files."""

    carries: frozenset[str]
    """Which keywords this core's streaming runner actually takes, asked once
    at registration for the reason the screen asks: a runner does not change
    shape between requests, and one too old for a keyword raises on it."""

    async def start(
        self,
        *,
        owner: Any,
        conversation: Any,
        message: str,
        persona: str | None = None,
        thinking: bool | None = None,
        temperature: float | None = None,
        pins_by_role: Mapping[str, str] | None = None,
        author_kind: AuthorKind | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> TurnHandle:
        """:func:`start_turn`, as a method, so a caller need not import this
        module at all.

        That is what keeps :mod:`personacore.runbooks` free of an import of
        :mod:`personacore.web`: the runner reads this object off ``app.state``
        and calls it duck-typed, exactly as
        :mod:`personacore.web.screens.chat_run` reads ``app.state.runner``.
        """
        return await start_turn(
            self.app,
            owner=owner,
            conversation=conversation,
            request=TurnRequest(
                message=message,
                persona=persona,
                conversation_id=getattr(conversation, "conversation_id", None),
                thinking=thinking,
                temperature=temperature,
                pins_by_role=pins_by_role,
                author_kind=author_kind,
            ),
            on_text=on_text,
        )


def attach_engine(app: Any, router: Any) -> None:
    """Put the engine a router was built with onto ``app.state``.

    The engine is assembled where its pieces are — inside
    ``chat_streaming.register``, which is handed a router and never an
    application — so it rides up on the router and is bound to the application
    here, at the one point that has both. Bound rather than mutated: the engine
    is frozen, and an application it can start a turn on is a fact about the
    assembly, not a field anybody should be able to change afterwards.

    A router carrying no engine is a core assembled without the chat screen.
    Nothing is set, ``engine_for`` answers ``None``, and a caller that needed
    one says so in plain words rather than raising an ``AttributeError`` four
    frames deeper.
    """
    engine = getattr(router, ENGINE_ATTRIBUTE, None)
    if engine is None:
        return
    setattr(app.state, ENGINE_ATTRIBUTE, replace(engine, app=app))


def engine_for(app: Any) -> TurnEngine | None:
    """The engine this application was assembled with, or ``None``.

    ``None`` on a core built without the chat screen at all — the same
    tolerance every other duck-typed read of ``app.state`` on this surface
    gives.
    """
    return getattr(app.state, ENGINE_ATTRIBUTE, None)


def begin_turn(app: Any, *, owner: str, marker: str | None) -> TurnHandle:
    """Register a turn for this conversation and hand back its handle.

    Registered here rather than by whoever is about to drive it, so there is
    one place a turn enters the ring. ``marker`` is ``None`` for a brand new
    conversation, whose naming instant is minted inside the turn itself — such
    a turn is unregistered until :meth:`_RunningTurn.identify` is called with
    it, and nothing can attach in that window because the marker a browser
    would attach with does not exist yet either.
    """
    turn = _RunningTurn(_turns(app), owner)
    if marker is not None:
        turn.identify(marker)
    return TurnHandle(turn)


async def start_turn(
    app: Any,
    *,
    owner: Any,
    conversation: Any,
    request: TurnRequest,
    on_text: Callable[[str], None] | None = None,
) -> TurnHandle:
    """Run one turn in ``conversation``, with no HTTP request anywhere.

    The turn is in the ring before this returns, so a page opened while it is
    running finds it (``running_turn`` then ``data-turn-running`` then
    ``chat.js``'s ``attachToTurn``) and streams the rest of the reply exactly
    as it attaches to a person's own detached turn. Nothing about the turn
    depends on anybody watching: the task belongs to the application and the
    transcript is written when the turn completes, whoever is or is not
    looking.

    ``owner`` is anything carrying an ``id`` — an
    :class:`~personacore.admin.models.AdminUser` from a screen, an
    :class:`~personacore.audit.models.Owner` from a run. Only the id is read,
    and it is the same string on both, which is why this is not two functions.

    ``on_text`` is the runner's watchdog seam: every text delta, never
    reasoning, called before the frame goes out. The deadline, the ceiling and
    the ``.partial`` file stay with the caller — this function knows nothing
    about any of them and exposes only :meth:`TurnHandle.stop`.
    """
    engine = engine_for(app)
    if engine is None:
        raise RuntimeError("This core has no chat screen, so it can run no turn.")
    marker = conversation.started_at.isoformat()
    # Through `_keyed`, not as typed: the ring is keyed by the one canonical
    # spelling of an instant, and a page attaches with the marker its address
    # carries. A key built any other way is a turn nothing can find.
    key = _keyed(str(owner.id), marker)
    handle = begin_turn(app, owner=str(owner.id), marker=key[1] if key is not None else marker)
    out = _Spoken()
    handle.drive(
        _detached_frames(
            engine,
            handle,
            owner=owner,
            conversation=conversation,
            request=request,
            out=out,
            on_text=on_text,
        ),
        out=out,
    )
    return handle


def _stream_kwargs(engine: TurnEngine, request: TurnRequest) -> dict[str, Any]:
    """The keywords this core's runner will actually take.

    Each one gated on ``carries`` rather than on whether it has a value, for
    the reason the screen gates the same keywords: a runner too old for one
    must never see it, whatever it would have carried, and a runner that does
    take it reads ``None`` as "no opinion" on its own. ``pins_by_role`` is the
    exception and deliberately so — runbook contract §1.5: an empty mapping
    means "pin nothing", which is a different instruction from ``None``, so
    the two are never collapsed.
    """
    extra: dict[str, Any] = {}
    if "conversation_id" in engine.carries and request.conversation_id:
        extra["conversation_id"] = request.conversation_id
    if "thinking" in engine.carries:
        extra["thinking"] = request.thinking
    if "temperature" in engine.carries:
        extra["temperature"] = request.temperature
    if "pins_by_role" in engine.carries and request.pins_by_role is not None:
        extra["pins_by_role"] = dict(request.pins_by_role)
    if "author_kind" in engine.carries and request.author_kind is not None:
        extra["author_kind"] = request.author_kind
    return extra


async def _reply_row(engine: TurnEngine, owner: Any, opened: datetime) -> Any:
    """The row the loop just wrote this turn's reply as.

    The same seam ``chat_streaming._turn_reply_row`` uses and for the same
    reason: the loop's own correlation id lives in a :mod:`contextvars`
    binding set inside a task whose context is a copy, so it never reaches the
    caller — and the row it wrote carries it. ``None`` for a turn that wrote no
    row at all, which is every stopped or broken one.
    """
    try:
        rows = await engine.thread_rows(owner, opened)
    except Exception as exc:  # noqa: BLE001 - a missing replay number beats a dead turn
        log.warning("chat_turn_reply_row_failed", error=repr(exc))
        return None
    for row in reversed(rows):
        if row.role is MessageRole.ASSISTANT:
            return row
    return None


async def _record_detached(
    engine: TurnEngine, owner: Any, row: Any, out: _Spoken, metrics: TurnMetrics | None
) -> None:
    """This turn's numbers and its reasoning, filed under the reply's own id.

    The same two writes ``chat_streaming`` makes after a person's turn, made
    here for a turn nobody typed — which is the whole of what a run's reply was
    missing. The page decides its chrome per row off these records and off
    nothing else (``chat_thread._fill_reply``), so writing them is what makes a
    run's reply carry the thinking box and the metrics line a person's does,
    live and on reload alike.

    Best-effort, each independently: a failure costs the replay's numbers, its
    collapsed thinking line or its cards, never the turn, which has already
    been shown by the time this runs.

    The workspace-files record is one of the three and not an afterthought:
    the ``done`` frame this turn ends with is built from the transcript, the
    same way a reload builds it, so a card that has no record behind it is
    missing from *both* — which is the shape of the bug this whole change
    removes, only one field narrower.
    """
    if metrics is not None:
        try:
            await engine.audit.record_audit(
                AuditRecord(
                    correlation_id=row.correlation_id,
                    timestamp=row.timestamp,
                    surface=Surface.ADMIN_UI,
                    owner=Owner.profile(str(owner.id)),
                    category=TURN_METRICS_CATEGORY,
                    action=TURN_METRICS_ACTION,
                    outcome=AuditOutcome.SUCCESS,
                    detail=_metrics_detail(metrics),
                )
            )
        except Exception as exc:  # noqa: BLE001 - a missing replay number beats a dead turn
            log.warning("chat_turn_metrics_write_failed", error=repr(exc))
    if out.reasoning:
        try:
            await engine.audit.record_reasoning(
                ReasoningRecord(
                    correlation_id=row.correlation_id,
                    timestamp=row.timestamp,
                    surface=Surface.ADMIN_UI,
                    owner=Owner.profile(str(owner.id)),
                    text=out.reasoning,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a lost replay line beats a dead turn
            log.warning("chat_reasoning_write_failed", error=repr(exc))
    if out.workspace_files:
        try:
            await engine.audit.record_audit(
                AuditRecord(
                    correlation_id=row.correlation_id,
                    timestamp=row.timestamp,
                    surface=Surface.ADMIN_UI,
                    owner=Owner.profile(str(owner.id)),
                    category=chat_workspace.WORKSPACE_FILES_CATEGORY,
                    action=chat_workspace.WORKSPACE_FILES_ACTION,
                    outcome=AuditOutcome.SUCCESS,
                    detail={"files": list(out.workspace_files)},
                )
            )
        except Exception as exc:  # noqa: BLE001 - a lost replay linkage beats a dead turn
            log.warning("chat_workspace_files_link_failed", error=repr(exc))


async def _detached_frames(
    engine: TurnEngine,
    handle: TurnHandle,
    *,
    owner: Any,
    conversation: Any,
    request: TurnRequest,
    out: _Spoken,
    on_text: Callable[[str], None] | None,
) -> AsyncIterator[str]:
    """One detached turn as frames, ending with the finished exchange.

    The frames are the same protocol and the same bytes the HTTP path
    produces, because the middle of this is :func:`stream_frames` and nothing
    else. What differs is the two ends, and both differences are absences: no
    form to parse and no room to run at the front, no request-bound rendering
    at the back — the ``done`` frame carries what a **reload** of this
    conversation would draw, built from the transcript that has just been
    written, which is why the chrome a page sees live and the chrome it sees
    after a refresh cannot disagree.
    """
    turn = handle.running
    opened = conversation.started_at
    since = datetime.now(UTC)
    yield frame("markers", {"html": engine.markers_html(opened.isoformat())})

    began = time.monotonic()
    streaming = getattr(engine.chat, "stream", None)
    if streaming is None:
        handle.settle(ReplyOutcome(error="This core cannot run a turn."))
        yield frame("done", {"html": ""})
        return
    events = streaming(
        request.message,
        user=str(owner.id),
        persona=request.persona,
        **_stream_kwargs(engine, request),
    )
    async for piece in stream_frames(
        events,
        out=out,
        holding=turn.holding,
        stopping=turn.stopping,
        began=began,
        workspace_cards=lambda names: engine.workspace_cards(
            conversation.conversation_id, names
        ),
        on_text=on_text,
    ):
        yield piece

    # The loop writes this turn's rows with no conversation on them; the
    # surface that resolved the conversation claims them afterwards — and it
    # has to happen before the reply row is read back, or a screen reading by
    # conversation would not find it. Best-effort, exactly as the runner's own
    # call was: one UPDATE, and a failure leaves the reply unclaimed rather
    # than lost.
    with contextlib.suppress(Exception):
        await engine.conversations.append(conversation, since=since)

    row = None if out.result is None else await _reply_row(engine, owner, opened)
    metrics = (
        None
        if out.result is None
        else TurnMetrics(
            first_token_ms=out.first_token_ms,
            total_ms=out.total_ms,
            tokens=out.tokens,
            first_audio_ms=out.first_audio_ms,
            tools=tuple(out.tools),
            prompt_tokens=getattr(out.result, "prompt_tokens", None),
        )
    )
    if row is not None:
        await _record_detached(engine, owner, row, out, metrics)

    handle.settle(
        ReplyOutcome(
            reply_text=out.reply,
            correlation_id=None if row is None else row.correlation_id,
            stopped=turn.stopping.is_set(),
            error=out.broke or None,
        )
    )
    try:
        html = await engine.rendered(owner, opened, since)
    except Exception as exc:  # noqa: BLE001 - a redraw is never worth the turn
        log.warning("chat_detached_render_failed", error=repr(exc))
        html = ""
    yield frame("done", {"html": html})
