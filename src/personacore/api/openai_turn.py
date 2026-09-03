"""Response translation — the agent's events folded into OpenAI's shapes.

One class, and it holds the whole tool-call decision. It absorbs the agent
loop's event stream and produces both halves of the answer: the ordered
emissions the streaming path renders as frames, and the folded state the
blocking path reads off it. One method producing both is what stops the two
paths drifting apart.

The load-bearing rule lives here: **a tool this container ran never reaches a
standard field.** ``tool_calls`` in OpenAI's schema is an instruction to the
caller, so only a call to a tool the caller itself offered goes there; ours are
reported under the ``personacore`` key, where nothing dispatches. That is
ADR-0035, and it is the difference between ``_tool_call`` and
``_client_tool_call`` below.

Deliberately not here: HTTP. Nothing in this module knows a status code, a
header or a frame — it produces values, and the two turn modules send them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from personacore.agent.loop import AgentEvent, AgentEventType
from personacore.api.openai_wire import (
    ChunkDelta,
    DeltaFunctionCall,
    DeltaToolCall,
    FinishReason,
    ResponseFunctionCall,
    ResponseToolCall,
    ToolInvocation,
    ToolOutcome,
    TurnExtension,
    _arguments_json,
    _Emission,
)

_TOOL_CAP_REASON = "tool_iteration_cap"
"""``AgentEvent.detail['reason']`` marking the agent loop's runaway-tool stop.
That turn produced an answer of a kind, so it is a 200, not a 503."""

_MAX_TOOL_ERROR_CHARS = 400
"""Ceiling on a tool's error sentence before it goes on the wire. The loop
writes these to be spoken, but the text can originate in a plugin, and outside
content does not get to set the size of a response frame."""


@dataclass(slots=True)
class _WireTurn:
    """Folds the agent's event stream down to OpenAI's wire format.

    The mapping, event by event:

    * ``TEXT_DELTA`` -> a ``content`` delta. Straight through, unbuffered: this
      is what section 10's latency budget is made of.
    * ``NOTICE`` -> also a ``content`` delta. A notice is a sentence the
      assistant would say out loud ("I can't reach the music system right
      now"), so on a text surface it is simply part of the reply.
    * ``TOOL_CALL`` -> a :class:`ToolInvocation` under the ``personacore`` key,
      on a frame whose delta is empty. It rides there and not in
      ``delta.tool_calls``, because a tool call in a standard field is an
      instruction to run something and the core has already run it — see
      :class:`TurnExtension` and ADR-0035. The frame still goes out the moment
      the call happens, which is what keeps a turn that spends eight seconds
      inside a tool from being eight seconds of dead air.
    * ``TOOL_RESULT`` -> a :class:`ToolOutcome` under the same key.
    * ``CLIENT_TOOL_CALL`` -> a :class:`ResponseToolCall` in the **standard**
      field, and the turn's :attr:`finish_reason` becomes ``"tool_calls"``.
      This is the one event that puts an instruction on the wire, and it can,
      because the tool is one the caller named in its own ``tools``.
    * ``REFUSAL`` -> nothing, still. The loop feeds the refusal back to the
      model, which states it in the persona's own words, so the client already
      gets it as content; putting it on the wire as well would say it twice.
    * ``DONE`` -> the terminal frame, with :attr:`finish_reason`.

    **``finish_reason`` is ``"stop"`` unless the turn handed something back.**
    A turn that ran container tools and answered is finished — there is nothing
    left for the client to do, and a turn that called a container tool and then
    said nothing is still ``"stop"`` with ``content: null``, an empty answer
    rather than a handover. Only a call to the *caller's* own tool is a
    handover, and then ``"tool_calls"`` is the literal truth.

    ``degraded`` marks the turn that produced no assistant text at all and
    ended on a notice — an unreachable LLM host, a missing persona, an open
    circuit breaker. Section 10 wants that to read as a clean failure, so it
    becomes a 503 rather than a 200 whose body happens to be an apology. The
    runaway-tool stop is excluded: the assistant did answer, just not usefully.
    """

    correlation_id: str = ""
    parts: list[str] = field(default_factory=list)
    saw_text: bool = False
    saw_notice: bool = False
    capped: bool = False
    done_text: str = ""
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    tool_results: list[ToolOutcome] = field(default_factory=list)
    unresolved: list[int] = field(default_factory=list)
    client_calls: list[ResponseToolCall] = field(default_factory=list)
    """The handover, if there was one. Kept apart from :attr:`tool_calls` on
    purpose: these two lists go to different halves of the response and must
    never be able to be mixed up by a later edit."""

    def feed(self, event: AgentEvent) -> list[_Emission]:
        """Absorb one event; return what it puts on the wire, if anything."""
        if event.type is AgentEventType.TEXT_DELTA:
            if not event.text:
                return []
            self.saw_text = True
            self.parts.append(event.text)
            return [_Emission(delta=ChunkDelta(content=event.text))]
        if event.type is AgentEventType.NOTICE:
            self.saw_notice = True
            if event.detail.get("reason") == _TOOL_CAP_REASON:
                self.capped = True
            if not event.text:
                return []
            self.parts.append(event.text)
            return [_Emission(delta=ChunkDelta(content=event.text))]
        if event.type is AgentEventType.TOOL_CALL:
            return self._tool_call(event)
        if event.type is AgentEventType.CLIENT_TOOL_CALL:
            return self._client_tool_call(event)
        if event.type is AgentEventType.TOOL_RESULT:
            return self._tool_result(event)
        if event.type is AgentEventType.DONE:
            self.done_text = event.text
        return []

    def _tool_call(self, event: AgentEvent) -> list[_Emission]:
        index = len(self.tool_calls)
        name = event.tool_name or "tool"
        # The loop does not put its own ``ToolCall.id`` on the event, so this
        # is minted here. It only has to be stable between the call and its
        # result and unique within the turn, which the index gives it; it is
        # not the id the audit trail carries.
        call_id = f"call_{self.correlation_id or uuid4().hex}_{index}"
        invocation = ToolInvocation(
            index=index,
            id=call_id,
            name=name,
            arguments=_arguments_json(event.detail.get("arguments")),
        )
        self.tool_calls.append(invocation)
        self.unresolved.append(index)
        # One frame, and its delta is empty: nothing about the assistant's
        # message changes when a tool the core already ran is reported. The
        # frame exists to go out *now*, so a client can see the turn is alive,
        # and to carry the invocation in the namespace nothing dispatches on.
        return [
            _Emission(delta=ChunkDelta(), extension=TurnExtension(tool_calls=[invocation]))
        ]

    def _client_tool_call(self, event: AgentEvent) -> list[_Emission]:
        """The handover, in the standard field, in OpenAI's streaming framing.

        The id is the model's own — the one the loop put on the event — not a
        minted one, because the caller quotes it back on its next request and
        that is how the round is matched up again.

        One delta per call, carrying ``index``, ``id``, ``type`` and the whole
        of ``function``. Identity fields appear exactly once for that index and
        ``arguments`` is a single fragment, which is what
        :class:`DeltaToolCall` requires.
        """
        index = len(self.client_calls)
        name = event.tool_name or "tool"
        call_id = str(event.detail.get("id") or f"call_{self.correlation_id}_{index}")
        arguments = event.detail.get("arguments")
        raw = arguments if isinstance(arguments, str) else _arguments_json(arguments)
        self.client_calls.append(
            ResponseToolCall(
                id=call_id,
                function=ResponseFunctionCall(name=name, arguments=raw),
            )
        )
        return [
            _Emission(
                delta=ChunkDelta(
                    tool_calls=[
                        DeltaToolCall(
                            index=index,
                            id=call_id,
                            type="function",
                            function=DeltaFunctionCall(name=name, arguments=raw),
                        )
                    ]
                )
            )
        ]

    def _tool_result(self, event: AgentEvent) -> list[_Emission]:
        name = event.tool_name or "tool"
        index = self._claim(name)
        error = event.text.strip() or None
        outcome = ToolOutcome(
            index=index,
            id=self.tool_calls[index].id if index is not None else None,
            name=name,
            ok=bool(event.detail.get("ok", False)),
            # The loop's own sentence, already written to be said out loud.
            # Capped because a plugin's error text is outside content and
            # arrives unbounded.
            error=error[:_MAX_TOOL_ERROR_CHARS] if error else None,
        )
        self.tool_results.append(outcome)
        return [_Emission(delta=ChunkDelta(), extension=TurnExtension(tool_results=[outcome]))]

    def _claim(self, name: str) -> int | None:
        """Match a result to the call it answers.

        The loop runs one call at a time and yields its result immediately, so
        the oldest unanswered call of that name is the right one. A result with
        no matching call is not an error — the loop reports a call whose
        arguments would not parse as a result with no preceding ``TOOL_CALL`` —
        it simply has no index to point at.
        """
        for position, index in enumerate(self.unresolved):
            if self.tool_calls[index].name == name:
                del self.unresolved[position]
                return index
        return None

    @property
    def degraded(self) -> bool:
        return self.saw_notice and not self.saw_text and not self.capped

    @property
    def text(self) -> str:
        """The whole reply. Falls back to the ``DONE`` event's text for the
        paths where the loop reports a final answer it never streamed."""
        joined = "".join(self.parts)
        return joined or self.done_text

    @property
    def content(self) -> str | None:
        """``null``, not ``""``, on a turn that produced no words."""
        return self.text or None

    @property
    def finish_reason(self) -> FinishReason:
        """``"tool_calls"`` when the turn handed one back, ``"stop"`` otherwise.

        ``length`` and ``content_filter`` stay in :data:`FinishReason` because
        they are honest things for this surface to say one day — a truncated
        answer, a blocked one — and neither asks the client to do anything.
        Nothing produces them yet.
        """
        return "tool_calls" if self.client_calls else "stop"

    @property
    def extension(self) -> TurnExtension | None:
        """The ``personacore`` key for a whole turn, or ``None`` if no tool ran.

        Both lists go out together once either has anything in it, so a reader
        gets the calls and their outcomes in one place rather than having to
        correlate two optional keys.
        """
        if not self.tool_calls and not self.tool_results:
            return None
        return TurnExtension(tool_calls=self.tool_calls, tool_results=self.tool_results)
