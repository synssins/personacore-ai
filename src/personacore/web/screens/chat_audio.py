"""The audio for one chat reply — PC-256's other half, now with two ways in
(plus a fourth path for a message read back out of the transcript, below).

A file of its own rather than part of the Chat screen, for two reasons that
point the same way. It answers with a media file instead of a fragment, so it
shares nothing with the screen but a URL; and the screen itself is a rendered
design that changes with the design, while this is the socket underneath it.
Whatever the conversation ends up looking like, a reply's audio is fetched
from here.

**The reply is never made to wait for speech.** Nothing is synthesised on the
turn — :meth:`personacore.voice.reply.ReplySpeaker.offer` only decides whether
this voice can speak and keeps the words against a handle. The engine is asked
here, on a request the browser makes after the answer is already on the screen,
which is why an engine that is slow, stopped or broken costs the audio and
cannot reach the reply.

**A handle is one operator's.** It is a random token minted per reply and
checked against the operator this request authenticated as, so the URL under
one person's conversation is not a way into another's. An unknown handle and
somebody else's handle get the same sentence and the same status: which it was
belongs in the log, not in the answer.

Two paths, and the screen is told which one it got
--------------------------------------------------

The **direct** path calls the engine here and answers with a finished WAV. It
is the original, it stays, and it has to: the Wyoming surface is off by default
because that protocol carries no credential of any kind, and a core with it off
must still be able to speak.

The **Wyoming** path sends the same words over a loopback socket to this core's
own Wyoming server (:mod:`personacore.wyoming.client`) and hands the audio to
the browser sentence by sentence, as the server produces it. That is the point
of it: it is the only thing in this project that drives the streaming synthesis
the way Home Assistant will, so it is the only thing that can find out whether
that code works.

**Which path answered is printed on the screen, always.** A validation tool
that quietly fell back to the direct engine would let somebody hear audio,
conclude streaming works, and be wrong — the worst outcome available here.
So the run is recorded and the screen fetches it: the path, the time to first
audio, how many audio groups arrived, and whether ``synthesize-stopped`` came.
One group is a whole reply synthesised in one go; several is streaming.

The **live** path is the third, and it is not a way of speaking a reply — it is
a way of speaking one that has not finished being written. The turn pushes each
sentence onto a :class:`~personacore.voice.live.LiveSpeech` the moment it has
certainly ended, and this module speaks them here, on the browser's own request,
while the rest of the reply is still arriving. It is fetched once and cannot be
replayed, which is why it has an address of its own; the finished reply keeps
its ordinary ``.wav`` for playing again afterwards.

Counts, timings and formats. **Never the text and never the audio** — the same
rule the Wyoming server keeps, for the same reason: what a household is about
to be told does not belong in a log or in a dictionary that outlives the reply.

The **replay** path is the fourth, and it answers a different question: not
"speak this reply that just arrived" but "speak this message again, whenever
that was". There is no handle to hold — the process that minted one is long
gone by the time somebody reloads the page — so it is addressed by the turn's
own ``correlation_id``, resolved from the transcript store on every request
and spoken in the voice of the persona the row itself says answered. Nothing
it produces is kept: the owner asked for resynthesis specifically so the
reload the owner asked for did not mean a second place audio gets stored.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from markupsafe import escape

from personacore.admin.models import AdminUser
from personacore.admin.protocols import AuditGateway
from personacore.agent.personas import PersonaStore
from personacore.audit.models import AuthorKind, MessageRole, Owner, Surface, TranscriptRecord
from personacore.voice.engine import EngineError, VoiceState, wav_bytes
from personacore.voice.live import LiveSpeech
from personacore.voice.reply import MAX_SPOKEN_CHARS, SPEAKER_ATTRIBUTE, speakable_text
from personacore.web.shared import UIContext
from personacore.wyoming.client import (
    SpeechRun,
    WyomingSpeech,
    WyomingSpeechError,
    streaming_wav_header,
)

log = structlog.get_logger(__name__)

NO_SPEECH = "This core was assembled without its voice subsystem, so nothing can be spoken."

UNUSABLE_HANDLE = (
    "That is not a reply this core can speak. Replies keep their audio for a "
    "short while and this one cannot be looked up at all."
)

WYOMING_OFF = "Wyoming is switched off, so this was spoken by the engine directly."
"""Said on the screen, not swallowed.

Falling back is right — the port is off by default because the protocol has no
authentication, and an operator who has not turned it on should still hear
their replies. Falling back *silently* is the thing that cannot happen: it
would let somebody test the streaming path without ever using it.
"""

WYOMING_UNREACHABLE = "{reason} Spoken by the engine directly instead."
"""The other fallback: the port is open and nothing answered on it.

Also announced, and it names the port, because a Wyoming server that is
switched on and unreachable is a fault an operator can go and look at.
"""

HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
"""What :func:`secrets.token_urlsafe` produces, and nothing else.

The handle is a dictionary key and never touches a path, so this is not a
traversal guard — it is a cheap door that keeps anything shaped unlike a
handle out of the lookup and out of the log line it would otherwise fill.
"""

RUNS_KEPT = 32
"""How many speech runs are remembered, per core, oldest out first.

The same order of magnitude as the replies themselves
(:data:`~personacore.voice.reply.MAX_PENDING_REPLIES`), because a run is only
interesting while the reply it belongs to is still on the screen with a play
button under it.
"""

LIVE_URL_PREFIX = "/admin/chat/live/"
"""Where a reply that is still being written is heard from.

A second address rather than a second meaning for the first: the finished
reply's ``.wav`` can be fetched again, and this one cannot — it is one pass
through a synthesis that is happening as the words arrive. Two things that
behave that differently must not share a URL shape.
"""

REPLAY_URL_PREFIX = "/admin/chat/message/"
"""Where a message read back out of the transcript is heard from.

A third address, and a third meaning again. The finished reply's ``.wav``
(above) is a handle minted per process and dead the moment this core
restarts; this one names **the message itself** — its turn's own
``correlation_id``, which the store already carries and which survives a
restart because it is a column, not a dictionary key in memory. Fetching it
resynthesises from the plain text still in the transcript, in the voice of
the persona that answered *then*, and keeps nothing: the owner chose
resynthesis over storing audio specifically so nothing new sits on disk for
it.
"""

REPLAY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
"""What a correlation id looks like on every path that mints one --
``str(uuid4())`` or ``uuid4().hex``, depending which surface ran the turn --
and nothing else. The same purpose :data:`HANDLE_PATTERN` serves: a cheap door
in front of the lookup, not a traversal guard, since the id only ever reaches
a bound SQL parameter.
"""

REPLAY_GONE = (
    "That reply can no longer be spoken. Either it belongs to a different "
    "conversation, or the persona and voice that answered it are no longer "
    "installed. The text is still on the screen."
)
"""One sentence for every way a replay can fail to resolve — a malformed or
foreign correlation id, a human message, a persona since deleted, a voice
since uninstalled. Deliberately one sentence for all of them: which it was
belongs in the log, the same rule :data:`REPLY_GONE` keeps for a live handle,
and for the same reason — telling "not yours" apart from "gone" from outside
is a way to go looking for somebody else's conversations by correlation id.
"""

LIVE_KEPT = 8
"""How many live streams are remembered, per core, oldest out first.

Far fewer than the finished replies, because one of these is only interesting
for the seconds between a turn starting and the browser fetching it. A handle
that has not been collected by then belongs to a tab that went away.
"""

LIVE_ATTRIBUTE = "reply_live_speech"
"""Where the ring of live streams lives on ``app.state`` — same reasoning as
:data:`RUNS_ATTRIBUTE`, which is directly below."""

LIVE_GONE = (
    "That reply is no longer being spoken. A reply is read aloud once, as it "
    "arrives; the text is still on the screen."
)
"""One sentence for a handle that is unknown, somebody else's, or already
being played on another tab — the same rule
:data:`~personacore.voice.reply.REPLY_GONE` keeps, for the same reason: which
it was belongs in the log, not in the answer."""

RUNS_ATTRIBUTE = "reply_speech_runs"
"""Where the ring of runs lives on ``app.state``.

On the application rather than in a module global for the reason every other
piece of state here is: the test suite builds several applications in one
process, and a global would let one core's numbers be read through another's.
"""


def speaker(request: Request) -> Any | None:
    """The running application's reply speaker, or ``None``.

    ``None`` means this core has no voice subsystem — the same treatment, and
    the same reasoning, as :func:`~personacore.web.screens.voice_common.voice_library`:
    say so plainly rather than offer something that cannot work.
    """
    return getattr(request.app.state, SPEAKER_ATTRIBUTE, None)


def wyoming_port(request: Request) -> int | None:
    """The port this core's own Wyoming server is listening on, or ``None``.

    Read off the running service, never off the request and never off the
    settings document: a port that was configured and did not bind is not a
    port to connect to. ``None`` means "not listening", which is the ordinary
    state — the switch is off by default.

    There is no host here on purpose. See
    :data:`personacore.wyoming.client.LOOPBACK_HOST`: the address this core
    dials is a constant, and nothing on the way from a browser to that socket
    is allowed to name it.
    """
    service = getattr(request.app.state, "wyoming", None)
    if service is None or not getattr(service, "running", False):
        return None
    port = getattr(service, "bound_port", None)
    return int(port) if port else None


# ---------------------------------------------------------------------------
# A replayed message's audio — resynthesised, never stored
# ---------------------------------------------------------------------------


def _persona_id_for(personas: PersonaStore, display_name: str) -> str | None:
    """The persona this display name was minted from, or ``None``.

    A transcript row's ``author.name`` is the persona's *display* name
    (``agent.loop``: ``persona.display_name or persona.name``), and a voice is
    resolved by the loadable id — not always the same string. Reading every
    installed persona back and matching on ``display_name`` finds it either
    way: a persona whose metadata never set one has both equal already, so
    this still finds it.

    ``None`` when nothing installed today answers to that name — the persona
    was renamed, or removed outright — which is not a fault: it is exactly the
    case a replayed reply must stay silent for rather than guess whose voice
    to use.
    """
    for name in personas.available():
        try:
            candidate = personas.load(name)
        except Exception as exc:  # noqa: BLE001 - a persona that will not load speaks for nobody
            log.info("replay_speech_persona_unavailable", persona=name, error=repr(exc))
            continue
        if candidate.display_name == display_name:
            return candidate.name
    return None


async def _resolve_replay(
    request: Request, *, author_name: str, personas: PersonaStore
) -> tuple[Any, tuple[tuple[str, int, int], ...]]:
    """The voice this persona would answer in today, and its speech pauses —
    or ``(None, ())`` for every reason it cannot: no voice subsystem, no
    persona left by that name, a resolve that raised.

    The one place this question is asked, so the render-time check
    (:func:`replay_speakable`) and the request that actually speaks
    (``chat_message_audio``) cannot disagree about the answer between one and
    the other.
    """
    made = speaker(request)
    resolve = getattr(made, "resolve", None)
    if resolve is None:
        return None, ()
    persona_id = await asyncio.to_thread(_persona_id_for, personas, author_name)
    if persona_id is None:
        return None, ()
    try:
        return await asyncio.to_thread(resolve, persona_id)
    except Exception as exc:  # noqa: BLE001 - a silent replay beats a dead screen
        log.warning("replay_speech_resolve_failed", error=repr(exc))
        return None, ()


async def replay_speakable(
    request: Request, *, author_name: str, personas: PersonaStore
) -> tuple[bool, str | None]:
    """Whether the persona named ``author_name`` could answer, in its own
    voice, right now — and the PC-336 sentence when a voice was chosen and
    cannot be heard.

    The render-time half of resynthesising a replayed reply's audio: the
    screen asks this once per persona while building the page, so it can leave
    the player off the message entirely rather than draw one that would 409
    the moment it is pressed, or — the failure this exists to rule out —
    speak in whoever answers to that name today.
    """
    resolution, _pauses = await _resolve_replay(request, author_name=author_name, personas=personas)
    if resolution is None:
        return False, None
    if not resolution.can_speak:
        # A persona that chose no voice says nothing here, same as the live
        # path (`ReplySpeaker._offer`) — it is a choice, not a fault.
        reason = None if resolution.state is VoiceState.NO_VOICE_CHOSEN else resolution.reason
        return False, reason
    return True, None


async def _owned_reply(
    audit: AuditGateway, user: AdminUser, correlation_id: str
) -> TranscriptRecord | None:
    """This operator's own reply for that correlation id, or ``None``.

    The owner filter sits in the query itself — ``owner_kind = ? AND
    owner_id = ?`` in the same ``WHERE`` clause as the correlation id — so a
    correlation id naming somebody else's turn returns no rows at all, rather
    than rows this code then has to remember to check. The same protection
    :meth:`~personacore.voice.reply.ReplySpeaker._speakable` gives a live
    handle, enforced at the SQL boundary instead of in Python.
    """
    try:
        rows = await audit.query_transcript(
            owner=Owner.profile(user.id),
            surface=Surface.ADMIN_UI,
            correlation_id=correlation_id,
            limit=8,
        )
    except Exception:  # noqa: BLE001 - a refusal beats a dead route
        return None
    return next((row for row in rows if row.role is MessageRole.ASSISTANT), None)


def remember(request: Request, handle: str, owner: str, run: SpeechRun) -> None:
    """Keep this run where the screen can ask for it.

    The object is kept, not a copy: a streamed run is still filling in while
    the browser is playing it, so a snapshot taken now would report one group
    and no terminator for every reply.
    """
    state = request.app.state
    runs: OrderedDict[str, tuple[str, SpeechRun]] | None = getattr(state, RUNS_ATTRIBUTE, None)
    if runs is None:
        runs = OrderedDict()
        setattr(state, RUNS_ATTRIBUTE, runs)
    runs[handle] = (owner, run)
    runs.move_to_end(handle)
    while len(runs) > RUNS_KEPT:
        runs.popitem(last=False)


def recall(request: Request, handle: str, owner: str) -> SpeechRun | None:
    """This operator's last run for that handle, or ``None``.

    The owner check is the same one the audio itself gets. A run carries no
    words, but it carries the fact that somebody was told something and how
    long it took to say, and that is still theirs.
    """
    runs = getattr(request.app.state, RUNS_ATTRIBUTE, None) or {}
    kept = runs.get(handle)
    if kept is None or kept[0] != owner:
        return None
    return kept[1]


async def begin_live(
    request: Request, *, persona: str | None, owner: str, started: float
) -> LiveSpeech | None:
    """Open a stream this turn can speak into, or ``None`` for a silent turn.

    Called **before** the turn runs, which is the whole difference between this
    and :meth:`~personacore.voice.reply.ReplySpeaker.offer`: whether there will
    be audio has to be decided while there is still nothing to say, so the
    browser can be given somewhere to listen at the same moment the first word
    appears.

    ``None`` for every reason a reply is silent — no voice subsystem, a persona
    that names no voice, an engine that is switched off, a speaker too old to
    have :meth:`~personacore.voice.reply.ReplySpeaker.resolve`. None of those is
    a fault and none of them says anything on the screen: the reply is text,
    which is what a reply is. **It never raises**, for the reason every other
    call on this path does not: speech is an addition to the answer and may not
    become a condition of it.
    """
    made = speaker(request)
    resolve = getattr(made, "resolve", None)
    if resolve is None:
        return None
    try:
        # In a worker thread because it asks the voice library, which reads the
        # disk — the same treatment `offer` gets on the reply path.
        resolution, pauses = await asyncio.to_thread(resolve, persona)
    except Exception as exc:  # noqa: BLE001 - a silent reply beats a dead screen
        log.warning("live_speech_resolve_failed", error=repr(exc))
        return None
    if resolution is None or not getattr(resolution, "can_speak", False):
        return None

    voice = getattr(resolution, "voice", None)
    handle = secrets.token_urlsafe(16)
    live = LiveSpeech(
        resolution,
        pacing=getattr(resolution, "pacing", None),
        pauses=pauses,
        voice_label=getattr(voice, "label", "") or "",
        audio_url=f"{LIVE_URL_PREFIX}{handle}.wav",
        max_chars=MAX_SPOKEN_CHARS,
        started=started,
    )
    _keep_live(request, handle, owner, live)
    return live


def _keep_live(request: Request, handle: str, owner: str, live: LiveSpeech) -> None:
    """Put one live stream where the audio request can find it."""
    state = request.app.state
    ring: OrderedDict[str, tuple[str, LiveSpeech]] | None = getattr(state, LIVE_ATTRIBUTE, None)
    if ring is None:
        ring = OrderedDict()
        setattr(state, LIVE_ATTRIBUTE, ring)
    ring[handle] = (owner, live)
    ring.move_to_end(handle)
    while len(ring) > LIVE_KEPT:
        # Evicted rather than closed: the turn that owns it closes it in its own
        # `finally`, and a stream ended from here would cut audio that is
        # playing because somebody else started a conversation.
        ring.popitem(last=False)


def recall_live(request: Request, handle: str, owner: str) -> LiveSpeech | None:
    """This operator's live stream for that handle, or ``None``.

    The same owner check the finished reply's audio gets, and it matters more
    here rather than less: this is a conversation being read aloud while it is
    still being written.
    """
    ring = getattr(request.app.state, LIVE_ATTRIBUTE, None) or {}
    kept = ring.get(handle)
    if kept is None or kept[0] != owner:
        return None
    return kept[1]


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the audio for a reply, and the report of how it was produced."""
    require_user = ctx.require_user
    templates = ctx.templates
    audit = ctx.audit
    personas = ctx.personas

    def _refused(message: str) -> HTMLResponse:
        """A refusal as a sentence, at a status that is not success.

        Escaped, because a refusal can quote a voice name, and a voice name
        comes out of a persona file an operator wrote. One status for every
        refusal on purpose: an expired handle and another operator's handle
        must not be distinguishable from outside.
        """
        return HTMLResponse(
            f"<p>{escape(message)}</p>", status_code=status.HTTP_409_CONFLICT
        )

    @router.get(
        "/chat/reply/{handle}.wav",
        summary="The audio for one chat reply",
    )
    async def chat_reply_audio(request: Request, handle: str) -> Any:
        """Speak this reply now and hand it back — over Wyoming where possible.

        The Wyoming attempt happens **before** a single byte of the response is
        written, and that is what makes an honest fallback possible: everything
        that can go wrong before the first audio group — the port being shut,
        the server refusing the voice, nothing answering — goes wrong while
        this is still free to choose the other path and say that it did. After
        the first byte there is no choosing left, so a failure from there on
        ends the audio and is reported as what it is.

        The direct path is unchanged, including running the synthesis in a
        worker thread: it is CPU work in a C library and must not stop this
        process serving everything else.
        """
        user = require_user(request)
        made = speaker(request)
        if made is None:
            return _refused(NO_SPEECH)
        if not HANDLE_PATTERN.match(handle):
            return _refused(UNUSABLE_HANDLE)

        port = wyoming_port(request)
        fallback: SpeechRun | None = None
        if port is not None:
            try:
                answer, fallback = await _over_wyoming(made, handle, user.id, port)
            except EngineError as exc:
                return _refused(str(exc))
            if answer is not None:
                remember(request, handle, user.id, answer[1])
                return answer[0]

        if fallback is None:
            fallback = SpeechRun(path="direct", port=port)
            fallback.note = WYOMING_OFF if port is None else None
        remember(request, handle, user.id, fallback)
        return await _directly(made, handle, user.id, fallback)

    async def _over_wyoming(
        made: Any, handle: str, owner: str, port: int
    ) -> tuple[tuple[Response, SpeechRun] | None, SpeechRun | None]:
        """The streamed answer, or the run explaining why there is not one.

        Exactly one half of the pair is filled in. An **exception** is a third
        outcome and means something else again: this reply cannot be spoken at
        all — an expired handle, somebody else's handle, a voice that has
        stopped being installed — and that is the operator's sentence, not a
        reason to try another path.
        """
        asked = getattr(made, "request", None)
        if asked is None:  # pragma: no cover - a speaker older than this route
            return None, None

        def _refused_run(reason: str) -> tuple[None, SpeechRun]:
            beaten = SpeechRun(path="direct", port=port)
            beaten.note = WYOMING_UNREACHABLE.format(reason=reason)
            return None, beaten

        # Raises `EngineError` for exactly the cases `audio` raises it for, in
        # exactly the same words: whether a handle may be spoken is one
        # decision and both paths take it from the same place. In a worker
        # thread because it asks the voice library, which reads the disk.
        wanted = await asyncio.to_thread(asked, handle, owner=owner)

        speech = WyomingSpeech(port=port, voice_name=wanted.voice_name)
        try:
            run = await speech.start(wanted.text)
        except WyomingSpeechError as exc:
            await speech.close()
            return _refused_run(str(exc))
        if run.failure is not None or not run.audio_bytes or run.rate is None:
            # The server answered and said no — an unknown voice, a stopped
            # engine — or said nothing at all. Either way no audio has been
            # written yet, so the direct engine still gets its turn and the
            # note says what Wyoming did.
            await speech.close()
            return _refused_run(run.failure or "The Wyoming server produced no audio.")

        header = streaming_wav_header(
            rate=run.rate, width=run.width or 2, channels=run.channels or 1
        )
        streamed = StreamingResponse(
            _streamed(speech, header),
            media_type="audio/wav",
            headers=_audio_headers(streamed=True),
        )
        return (streamed, run), None

    async def _directly(made: Any, handle: str, owner: str, run: SpeechRun) -> Response:
        """The original path: one engine call, one finished WAV."""
        try:
            spoken = await asyncio.to_thread(made.audio, handle, owner=owner)
        except EngineError as exc:
            # The engine's own sentence, written for an operator: "switched
            # off", "not installed", "did not start". Everything the speaker
            # could not turn into one of those has already been logged and
            # comes through as a sentence of its own.
            run.failure = str(exc)
            return _refused(str(exc))
        except Exception as exc:  # noqa: BLE001 - a screen reports, it does not crash
            run.failure = f"That reply could not be spoken: {exc.__class__.__name__}."
            return _refused(run.failure)
        data = wav_bytes(spoken)
        # One group, because that is what this path is: one call to the engine
        # for the whole reply, finished before anything is sent. The count is
        # the point of comparison — several groups is the streamed path.
        run.groups = 1
        # Samples, not the file: 44 bytes of that is the header
        # `voice.engine.wav_bytes` just wrote, and comparing a WAV's length
        # with a stream's sample count would flatter one of them by 44 bytes.
        run.audio_bytes = max(0, len(data) - 44)
        run.rate = int(getattr(spoken, "sample_rate", 0) or 0) or None
        run.channels = int(getattr(spoken, "channels", 1) or 1)
        run.width = 2
        log.info("reply_audio_served", **run.as_log())
        return Response(
            content=data,
            media_type="audio/wav",
            headers={**_audio_headers(streamed=False), "Content-Length": str(len(data))},
        )

    @router.get(
        "/chat/message/{correlation_id}.wav",
        summary="A message read back out of the transcript, resynthesised",
    )
    async def chat_message_audio(request: Request, correlation_id: str) -> Any:
        """Speak a replayed message now, from the plain text still in the
        transcript — never from a stored WAV, because there is none.

        Every check here mirrors the live path's own (``chat_reply_audio``)
        deliberately: the same character set on the identifier
        (:data:`REPLAY_ID_PATTERN`), the same ownership check at the query
        boundary (:func:`_owned_reply`), the same voice resolution
        (:func:`_resolve_replay`) the render-time check just made. What is
        different is the source of the words — a row already on disk instead
        of a handle held in memory — and that nothing produced here is kept
        anywhere once the response finishes.
        """
        user = require_user(request)
        made = speaker(request)
        if made is None:
            return _refused(NO_SPEECH)
        if not REPLAY_ID_PATTERN.match(correlation_id):
            return _refused(REPLAY_GONE)

        record = await _owned_reply(audit, user, correlation_id)
        if record is None or record.author is None or record.author.kind is not AuthorKind.PERSONA:
            return _refused(REPLAY_GONE)

        # PC-264: the plain text the model produced, not `reply_html` — the
        # same rule the copy button and the live speech path both keep.
        # Capped exactly as the live reply is (`MAX_SPOKEN_CHARS`): one policy
        # for how much of a long answer is ever handed to an engine, not a
        # second one invented for this path.
        text = speakable_text(record.content, limit=MAX_SPOKEN_CHARS)
        if not text:
            return _refused(REPLAY_GONE)

        resolution, pauses = await _resolve_replay(
            request, author_name=record.author.name, personas=personas
        )
        if resolution is None or not resolution.can_speak:
            return _refused((resolution.reason if resolution else None) or REPLAY_GONE)

        try:
            spoken = await asyncio.to_thread(resolution.speak, text, pauses=pauses)
        except EngineError as exc:
            return _refused(str(exc))
        except Exception as exc:  # noqa: BLE001 - a screen reports, it does not crash
            return _refused(f"That reply could not be spoken: {exc.__class__.__name__}.")

        data = wav_bytes(spoken)
        return Response(
            content=data,
            media_type="audio/wav",
            headers={**_audio_headers(streamed=False), "Content-Length": str(len(data))},
        )

    @router.get(
        "/chat/live/{handle}.wav",
        summary="A reply, spoken as it is being written",
    )
    async def chat_live_audio(request: Request, handle: str) -> Any:
        """The streamed half of PC-252: audio that starts before the text ends.

        The turn is putting finished sentences on this stream while this
        request is speaking the ones already there, so the two run on separate
        tasks and neither waits for the other. That is what makes the rule at
        the top of this module survive streaming: the turn's side of a
        :class:`~personacore.voice.live.LiveSpeech` is a queue push, and every
        engine call is here, on a request the reply does not depend on.

        ``start`` runs before a byte is written, for the same reason the
        Wyoming path's does: a header needs a rate and a width, and everything
        that can go wrong before the first sample can still be answered with a
        status. After that there is no status left to change and a failure ends
        the audio, which the run says and the log records.
        """
        user = require_user(request)
        if not HANDLE_PATTERN.match(handle):
            return _refused(LIVE_GONE)
        live = recall_live(request, handle, user.id)
        if live is None:
            return _refused(LIVE_GONE)
        if not await live.start():
            # Nothing speakable came out of the turn — an empty reply, a reply
            # that was only markup, or a turn that failed before it said
            # anything. The text on the screen already says which.
            return _refused(live.failure or LIVE_GONE)

        header = streaming_wav_header(
            rate=live.rate or 22050, width=live.width or 2, channels=live.channels or 1
        )
        return StreamingResponse(
            _live_stream(live, header),
            media_type="audio/wav",
            headers=_audio_headers(streamed=True),
        )

    @router.get(
        "/chat/reply/{handle}.report",
        response_class=HTMLResponse,
        summary="How one reply's audio was produced",
    )
    async def chat_reply_report(request: Request, handle: str) -> HTMLResponse:
        """The line under the player: which path spoke, and the numbers.

        Fetched by the page once the audio has finished or failed, because the
        interesting numbers — how many audio groups arrived, whether the
        exchange was ended properly — are not known until then.
        """
        user = require_user(request)
        if not HANDLE_PATTERN.match(handle):
            return HTMLResponse("", status_code=status.HTTP_204_NO_CONTENT)
        run = recall(request, handle, user.id)
        if run is None:
            return HTMLResponse("", status_code=status.HTTP_204_NO_CONTENT)
        return templates.TemplateResponse(
            request=request,
            name="fragments/reply_speech_report.html",
            context={"run": run},
        )


async def _streamed(speech: WyomingSpeech, header: bytes) -> AsyncIterator[bytes]:
    """The WAV header, then every sample as the server produces it.

    The header goes out first and alone, so a browser can start decoding
    before the second sentence has been synthesised. Its length fields are the
    "unknown" ones (:func:`~personacore.wyoming.client.streaming_wav_header`);
    the real length is not knowable here and never will be.

    The connection is closed in ``finally``, which covers the case that is easy
    to miss: a browser that navigates away mid-reply cancels this generator,
    and the socket underneath it has to be let go rather than left holding the
    engine's turn.
    """
    try:
        yield header
        async for piece in speech.audio():
            yield piece
    finally:
        await speech.close()
        log.info("reply_audio_served", **speech.run.as_log())


async def _live_stream(live: LiveSpeech, header: bytes) -> AsyncIterator[bytes]:
    """The WAV header, then every piece as the voice finishes saying it.

    The ``finally`` covers the case that is easy to miss and is not rare: the
    listener closes the tab, this generator is cancelled, and the turn on the
    other side is still pushing sentences onto a queue nobody is reading. Ending
    the stream from here is what stops that queue growing for the rest of the
    reply — and it must not raise on the way out, because there is nobody left
    to tell.
    """
    try:
        yield header
        async for piece in live.audio():
            yield piece
    finally:
        live.abandon()
        log.info("reply_live_audio_served", **live.as_log())


def _audio_headers(*, streamed: bool) -> dict[str, str]:
    """How a reply's audio is framed for a browser, and kept out of every cache.

    ``no-store`` because this is conversation content: a reply belongs to the
    operator who asked for it, and a proxy or a disk cache holding it is a copy
    of what was said sitting somewhere nobody decided it should be.

    ``Accept-Ranges: none`` on the streamed path because there is no length and
    no seeking back: a player that asked for a byte range would be asking for
    part of a file that is still being spoken.
    """
    headers = {
        "Content-Disposition": 'inline; filename="reply.wav"',
        "Cache-Control": "no-store",
    }
    if streamed:
        headers["Accept-Ranges"] = "none"
    return headers


__all__ = [
    "HANDLE_PATTERN",
    "LIVE_ATTRIBUTE",
    "LIVE_GONE",
    "LIVE_KEPT",
    "LIVE_URL_PREFIX",
    "NO_SPEECH",
    "REPLAY_GONE",
    "REPLAY_ID_PATTERN",
    "REPLAY_URL_PREFIX",
    "RUNS_ATTRIBUTE",
    "UNUSABLE_HANDLE",
    "WYOMING_OFF",
    "WYOMING_UNREACHABLE",
    "begin_live",
    "recall",
    "recall_live",
    "register",
    "remember",
    "replay_speakable",
    "speaker",
    "wyoming_port",
]
