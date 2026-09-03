"""One real turn, for the admin surface's chat box — ADR-0040.

Moved out of ``personacore.server``. This module owns what a turn taken from a
screen *is*: which tools it may call, the policy profile it runs under, how the
stream is folded into a finished result, and what a finished reply can be heard
as. The assembly builds one of these and hands it over; it does not decide any
of that.

The result and event types here are plain objects that structurally match
``personacore.admin.protocols``. That seam stays structural on purpose (ADR-0007
draws the same line the other way): neither side imports the other's types, so
the admin package still has no dependency on the agent loop.

Nothing in here is HTTP. It is handed a loop, a persona store, a plugin host and
optionally a speaker, and it would run identically under a surface that had
never heard of FastAPI.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import structlog

from personacore.agent.loop import (
    AgentEventType,
    AgentLoop,
    ConversationMessage,
    TurnRequest,
)
from personacore.agent.personas import PersonaStore
from personacore.audit.models import Surface
from personacore.contracts.policy import PolicyProfile, ProfileKind, RiskLevel
from personacore.conversations.addressing import FloorAnswer
from personacore.voice.reply import ReplySpeaker, ReplySpeech

log = structlog.get_logger(__name__)


class _AdminChatResult:
    """Structural match for ``personacore.admin.protocols.ChatTurnResult``.

    A plain object rather than an import from the admin package: the seam is
    structural on purpose, so the assembly can satisfy it without either side
    depending on the other's types.
    """

    def __init__(
        self,
        *,
        ok: bool,
        reply: str,
        persona: str,
        error: str | None = None,
        tools_offered: list[str] | None = None,
        tools_called: list[str] | None = None,
        speech: ReplySpeech | None = None,
        prompt_tokens: int | None = None,
    ) -> None:
        self.ok = ok
        self.reply = reply
        self.persona = persona
        self.error = error
        # Reported so "it says it has no tools" stops being ambiguous. Without
        # these, a plugin that is not installed and a model that declines to
        # call one look identical from the page — a debugging trap that cost
        # real time before it was fixed.
        self.tools_offered = tools_offered or []
        self.tools_called = tools_called or []
        #: How this reply can be heard — :class:`personacore.voice.reply.ReplySpeech`,
        #: carrying ``audio_url`` when the persona's voice can speak and the
        #: PC-336 sentence when it cannot. ``None`` from a runner assembled
        #: without voice at all. A screen reads it with ``getattr`` and a
        #: default, the way it reads ``tools_offered``: a reply with no audio is
        #: an ordinary reply, never a failure.
        self.speech = speech
        #: What this turn's own request to the model actually cost, in the
        #: backend's own tokenizer — read off the loop's final ``DONE`` event
        #: (:func:`personacore.agent.loop._usage_detail`), never estimated
        #: here. ``None`` on a backend that never reported one, which is every
        #: backend until it is asked (``LLMClient.stream_chat_completion``'s
        #: ``stream_options``) — absent, not a guess.
        self.prompt_tokens = prompt_tokens


async def _offer_speech(
    speaker: ReplySpeaker | None,
    reply: str,
    *,
    persona: str,
    owner: str,
) -> ReplySpeech | None:
    """What this reply can be heard as, or ``None`` — and never an exception.

    Three guards, because the reply is the product and the audio is an
    addition to it (ADR-0029 §6: losing speech costs speech and nothing else):
    a core with no speaker returns ``None``; the lookup runs in a worker thread
    because it reads the voices directory; and anything that escapes
    :meth:`~personacore.voice.reply.ReplySpeaker.offer` — which is written not
    to raise, but "written not to" is a claim about today's code — is logged
    and dropped rather than allowed to turn a good answer into an error.
    """
    if speaker is None:
        return None
    try:
        return await asyncio.to_thread(speaker.offer, reply, persona=persona, owner=owner)
    except Exception as exc:  # noqa: BLE001 - speech may never cost the reply
        log.warning("reply_speech_failed", error=repr(exc))
        return None


class _AdminChatEvent:
    """Structural match for ``personacore.admin.protocols.ChatStreamEvent``.

    A plain object for the same reason :class:`_AdminChatResult` is one: the
    seam is structural, so neither side has to import the other's types.
    """

    __slots__ = ("kind", "text", "tool_name", "duration_ms", "result")

    def __init__(
        self,
        kind: str,
        *,
        text: str = "",
        tool_name: str | None = None,
        duration_ms: float | None = None,
        result: Any | None = None,
    ) -> None:
        self.kind = kind
        self.text = text
        self.tool_name = tool_name
        self.duration_ms = duration_ms
        self.result = result


class _AdminChat:
    """One real turn, for the admin surface's chat box — both ways round.

    **The two ways are one turn.** :meth:`__call__` runs the streaming form and
    keeps only the end of it, so the non-streaming path cannot drift from the
    streaming one: same persona resolution, same tool grant, same policy
    profile, same audit trail, same transcript. What differs is when the caller
    is told, and nothing else. Before this, "collect the stream and render at
    the end" was the *only* form, and a forty-sentence reply appeared as one
    wall after twenty-five seconds of nothing.

    **This is where the assistant gets a voice** (PC-256). ``speech`` is asked
    what the finished reply can be heard as, *after* the reply exists and with
    the loop's own plain text — the string PC-264 keeps beside the rendered
    form so that ``**Today**`` is never read out as "asterisk asterisk Today".
    Nothing is synthesised here: the speaker mints a handle and the audio is
    fetched on a request of its own, so a slow or broken engine costs the audio
    and can never delay or fail the answer. ``None`` means a core with no voice
    subsystem, and every reply is text.

    ``history`` is the conversation the admin UI is continuing, oldest first
    (:class:`personacore.admin.protocols.ChatHistoryTurn`). It is converted to
    the loop's own :class:`ConversationMessage` here — which accepts ``user``
    and ``assistant`` and nothing else, so a role this runner is handed that is
    neither is refused by the model rather than smuggled into the prompt. The
    admin UI reconstructs the list from the transcript store; nothing about the
    conversation is held in this process.

    ``persona`` is who answers this one turn, and ``None`` is the configured
    default — the meaning ``[core] default_persona`` has always had, for this
    surface and for every other. It is handed straight to the loop as
    ``TurnRequest.persona_override``, which is the *same* line of resolution
    the default takes (``AgentLoop._compose``: ``personas.load(override or
    profile.persona)``), so ADR-0005's safety block and the persona's own
    guardrails are composed around a chosen persona exactly as around the
    default one. Nothing here loads a prompt, and nothing here writes config: a
    per-turn choice moves no setting and changes nobody else's assistant.

    A persona that cannot be loaded — never installed, deleted since it was
    chosen, or a name that is not a plain persona name at all — raises inside
    the loop's own resolution and comes back as its ``NOTICE``, which this
    reports as a failed turn carrying that sentence. It is never swapped for
    the default: answering as somebody the operator did not ask for is worse
    than saying no.

    ``also_present`` is the display names of the other personas in the room
    (the many-voices contract §2), passed straight to
    ``TurnRequest.also_present`` so the persona is told who else is here and
    how to reach them. **Empty by default and empty for a room of one**, which
    is the case §7 protects: the prompt is then composed exactly as it always
    was, byte for byte. Nothing is computed from it here — who is in a room is
    the screen's knowledge, and this runner has never known about
    conversations.

    ``image_data_urls`` is an attached image, as ``data:`` URIs (attachments
    contract §4.2), passed straight to ``TurnRequest.image_data_urls``.
    **Empty by default**, which is every turn before this field existed —
    this runner does not read the attachment store, does not check the
    vision probe (contract §4.3 forbids a gate on this path) and does not
    decide what counts as an image; the chat screen has already read the
    bytes and built the URIs by the time this is called.
    """

    def __init__(
        self,
        agent: AgentLoop,
        personas: PersonaStore,
        host: Any,
        speech: ReplySpeaker | None = None,
    ) -> None:
        self._agent = agent
        self._personas = personas
        self._host = host
        self._speech = speech

    async def _tools_for(self, user: str) -> list[str]:
        """The tools this turn may call.

        The allowlist is per profile and empty means none (spec section 5.4),
        which is right everywhere else and wrong here: an admin trying the
        assistant would silently get no tools and conclude they are broken.
        Grant exactly the safe tools currently installed — nothing latent, and
        nothing above the ceiling.
        """
        try:
            return [
                spec.name for spec in await self._host.list_tools() if spec.risk is RiskLevel.SAFE
            ]
        except Exception as exc:  # noqa: BLE001 - no tools is a worse answer than no chat
            log.warning("admin_chat_tool_listing_failed", error=repr(exc))
            return []

    def _profile(self, user: str, available: Sequence[str]) -> PolicyProfile:
        """The policy this surface's turns run under.

        One place, because the open-floor ask (:meth:`ask`) has to run under
        exactly the profile the turn would — a persona asked whether it wants
        to speak while wearing a different set of rules than it would answer
        under is being asked about a turn that will not happen.
        """
        return PolicyProfile(
            id=user,
            display_name=user,
            kind=ProfileKind.USER,
            enabled=True,
            allowed_tools=list(available),
            max_tool_risk=RiskLevel.SAFE,
            may_approve_confirm=True,
        )

    def _request(
        self,
        message: str,
        *,
        user: str,
        history: Sequence[Any],
        persona: str | None,
        available: Sequence[str],
        record_user_message: bool = True,
        also_present: Sequence[str] = (),
        image_data_urls: Sequence[str] = (),
    ) -> TurnRequest:
        return TurnRequest(
            user_message=message,
            profile=self._profile(user, available),
            surface=Surface.ADMIN_UI,
            history=[
                ConversationMessage(role=turn.role, content=turn.content)  # type: ignore[arg-type]
                for turn in history
            ],
            persona_override=persona,
            record_user_message=record_user_message,
            also_present=list(also_present),
            # Attachments contract §4.2. Empty by default, which composes the
            # loop's prompt exactly as it always has — see
            # ``TurnRequest.image_data_urls``.
            image_data_urls=list(image_data_urls),
        )

    async def ask(
        self,
        question: str,
        *,
        user: str,
        persona: str,
        history: Sequence[Any] = (),
        max_tokens: int = 8,
        extra_body: Mapping[str, Any] | None = None,
    ) -> FloorAnswer:
        """Put one short question to a persona — the many-voices contract §3.2.

        Discovered by the chat screen with ``getattr``, exactly as ``stream``
        is, so a runner that predates rooms simply cannot be asked and the
        screen falls back to never opening the floor. The whole of the work is
        in :meth:`~personacore.agent.loop.AgentLoop.ask_persona`; this supplies
        the same profile a turn would run under and converts the history the
        admin surface keeps.

        **No tools are granted.** The question is "do you want to answer this",
        and a persona reaching for the weather to decide would be paying for a
        turn to find out whether to take one.

        ``extra_body`` is passed straight through — the caller owns what goes
        in it, because the caller is the screen that owns §3.2 and knows the
        question it is asking. An empty answer is every kind of failure, and
        the caller reads it as no. It never raises: a screen reports, it does
        not crash.
        """
        try:
            return await self._agent.ask_persona(
                question,
                persona_name=persona,
                profile=self._profile(user, ()),
                surface=Surface.ADMIN_UI,
                context=[
                    ConversationMessage(role=turn.role, content=turn.content)  # type: ignore[arg-type]
                    for turn in history
                ],
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
        except Exception as exc:  # noqa: BLE001 - silence is the safe answer
            log.warning("admin_chat_ask_failed", persona=persona, error=repr(exc))
            return FloorAnswer()

    async def stream(
        self,
        message: str,
        *,
        user: str,
        history: Sequence[Any] = (),
        persona: str | None = None,
        record_user_message: bool = True,
        also_present: Sequence[str] = (),
        image_data_urls: Sequence[str] = (),
    ) -> AsyncIterator[_AdminChatEvent]:
        """The turn as it happens, ending with the finished result.

        Every failure the loop can survive arrives as its own ``NOTICE`` and is
        reported in the final result rather than raised; anything that escapes
        the loop entirely is caught here and becomes a failed turn carrying one
        sentence, because a screen reports and does not crash.

        **The loop's generator is closed by the ``finally`` below**, so a caller
        that stops iterating — a browser that closed the tab mid-reply, which is
        the ordinary case — releases the turn rather than leaving it holding a
        model connection open until the process notices.
        """
        # Reported as the persona *asked for*, so a refusal names who was
        # wanted rather than who happens to be configured. Resolution itself
        # stays inside the loop.
        answering = persona or self._personas.default_persona
        available = await self._tools_for(user)
        request = self._request(
            message,
            user=user,
            history=history,
            persona=persona,
            available=available,
            record_user_message=record_user_message,
            also_present=also_present,
            image_data_urls=image_data_urls,
        )

        parts: list[str] = []
        notices: list[str] = []
        called: list[str] = []
        # This turn's own token cost, read off the loop's final `DONE` event
        # (`personacore.agent.loop._usage_detail`) rather than estimated here
        # — see `_AdminChatResult.prompt_tokens`. `None` until a `DONE` event
        # actually carries one.
        prompt_tokens: int | None = None
        events = self._agent.run_turn(request)
        try:
            async for event in events:
                if event.type is AgentEventType.TEXT_DELTA:
                    parts.append(event.text)
                    yield _AdminChatEvent("text", text=event.text)
                elif event.type is AgentEventType.DONE:
                    value = event.detail.get("prompt_tokens")
                    if isinstance(value, int):
                        prompt_tokens = value
                elif event.type is AgentEventType.REASONING_DELTA:
                    # Forwarded and nowhere accumulated: `reply` below is
                    # still built from `parts` alone, so a turn that produced
                    # only thinking and no answer is unaffected by this
                    # branch and is still reported as "the assistant returned
                    # nothing" further down — exactly as it was before this
                    # existed. Never in `notices` either: the thinking is not
                    # a plain-English statement about the assistant's own
                    # condition, and no test speaks it.
                    yield _AdminChatEvent("reasoning", text=event.text)
                elif event.type is AgentEventType.TOOL_CALL and event.tool_name:
                    called.append(event.tool_name)
                    yield _AdminChatEvent("tool_call", tool_name=event.tool_name)
                elif event.type is AgentEventType.TOOL_RESULT and event.tool_name:
                    duration = event.detail.get("duration_ms")
                    yield _AdminChatEvent(
                        "tool_result",
                        tool_name=event.tool_name,
                        duration_ms=float(duration) if isinstance(duration, int | float) else None,
                    )
                elif event.type is AgentEventType.NOTICE:
                    notices.append(event.text)
                    yield _AdminChatEvent("notice", text=event.text)
        except Exception as exc:  # noqa: BLE001 - a screen reports, it does not crash
            log.error("admin_chat_failed", error=repr(exc))
            yield _AdminChatEvent(
                "done",
                result=_AdminChatResult(
                    ok=False,
                    reply="",
                    persona=answering,
                    error="The turn could not be completed.",
                    tools_offered=available,
                    tools_called=called,
                ),
            )
            return
        finally:
            await _aclose(events)

        reply = "".join(parts).strip()
        if reply:
            yield _AdminChatEvent(
                "done",
                result=_AdminChatResult(
                    ok=True,
                    reply=reply,
                    persona=answering,
                    tools_offered=available,
                    tools_called=called,
                    speech=await _offer_speech(
                        self._speech, reply, persona=answering, owner=user
                    ),
                    prompt_tokens=prompt_tokens,
                ),
            )
            return
        yield _AdminChatEvent(
            "done",
            result=_AdminChatResult(
                ok=False,
                reply="",
                persona=answering,
                error=notices[0] if notices else "The assistant returned nothing.",
                tools_offered=available,
                tools_called=called,
                prompt_tokens=prompt_tokens,
            ),
        )

    async def __call__(
        self,
        message: str,
        *,
        user: str,
        history: Sequence[Any] = (),
        persona: str | None = None,
        record_user_message: bool = True,
        also_present: Sequence[str] = (),
        image_data_urls: Sequence[str] = (),
    ) -> _AdminChatResult:
        """The same turn, collected. Non-streaming callers are unchanged.

        Collecting the stream before answering is fine where the product's job
        — speaking within two seconds — does not apply: the OpenAI-compatible
        API's non-streaming mode, a turn taken through ``app.state.chat_runner``,
        and a browser running no JavaScript at all.
        """
        result: _AdminChatResult | None = None
        async for event in self.stream(
            message,
            user=user,
            history=history,
            persona=persona,
            record_user_message=record_user_message,
            also_present=also_present,
            image_data_urls=image_data_urls,
        ):
            if event.kind == "done" and event.result is not None:
                result = event.result
        if result is not None:
            return result
        # Unreachable while `stream` ends with a `done`, and still written:
        # a caller that gets `None` from a runner has no sentence to show.
        return _AdminChatResult(
            ok=False,
            reply="",
            persona=persona or self._personas.default_persona,
            error="The turn could not be completed.",
        )


async def _aclose(events: Any) -> None:
    """Close an async generator, whatever it thinks of the idea.

    The turn holds a connection to the LLM host and a place in the loop's own
    state; a generator abandoned mid-iteration keeps both until the garbage
    collector happens to look. A browser closing its tab mid-reply is the
    ordinary case here, not the exception, so this runs in a ``finally`` and
    never raises: a stream that will not close politely has already stopped
    being useful.
    """
    closer = getattr(events, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as exc:  # noqa: BLE001 - closing may not become the failure
        log.info("admin_chat_close_failed", error=repr(exc))


def _make_chat_runner(
    agent: AgentLoop,
    personas: PersonaStore,
    host: Any,
    speech: ReplySpeaker | None = None,
) -> Any:
    """The admin surface's chat runner — see :class:`_AdminChat`.

    Still a factory rather than the class itself so that every assembly builds
    it the same way, and so that what the admin surface is handed stays "a
    callable that runs a turn" (:class:`personacore.admin.protocols.ChatRunner`)
    rather than a named type it would then be tempted to import.
    """
    return _AdminChat(agent, personas, host, speech)
