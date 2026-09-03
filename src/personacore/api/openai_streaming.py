"""The streaming turn: server-sent events, and the bargain they cost.

The first events are pulled before the response starts, so a turn that fails at
the first hurdle is still a real 503. Once anything proves the turn is alive the
status is committed — see ``_COMMITS_THE_STREAM`` — and from that point a
failure can only arrive as a spoken apology inside a 200. That trade is
deliberate: the alternative was a client sitting without headers for the whole
of a tool round, unable to tell a slow tool from a hung process.

The framing itself belongs to :mod:`personacore.api.openai_wire`; this module
decides *which* frames go out and in what order, including the terminal one and
the ``data: [DONE]`` sentinel a client waits for before it stops reading.

Deliberately not here: what an event means. That is ``_WireTurn``, shared with
the blocking path.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Response
from fastapi.responses import StreamingResponse

from personacore.agent.loop import AgentEvent, AgentEventType, TurnRequest
from personacore.agent.protocols import AuditSink
from personacore.api.openai_caller import Caller, TurnRunner
from personacore.api.openai_errors import (
    _approx_tokens,
    _completion_id,
    _degraded_error,
    _record,
    _request_audit,
    _unexpected_error,
)
from personacore.api.openai_turn import _WireTurn
from personacore.api.openai_wire import (
    SSE_DONE,
    ChatCompletionChunk,
    ChunkChoice,
    ChunkDelta,
    Usage,
    _content_frame,
    _Emission,
    _emission_frame,
    _frame,
)
from personacore.audit.logging import get_logger
from personacore.audit.models import AuditOutcome

logger = get_logger(__name__)


_COMMITS_THE_STREAM = frozenset(
    {
        AgentEventType.TEXT_DELTA,
        AgentEventType.TOOL_CALL,
        AgentEventType.CLIENT_TOOL_CALL,
        AgentEventType.TOOL_RESULT,
        AgentEventType.REFUSAL,
    }
)
"""Events that prove the turn is genuinely running, so the 200 can be sent.

Before this, ``_stream_response`` waited for the first ``TEXT_DELTA`` — which
on a tool-running turn arrives only *after* the whole tool round is finished,
so the client sat without headers for the duration and could not tell a slow
tool from a hung process. A tool call is proof of life too. The cost is real
and accepted: once the 200 is out, a failure later in the turn can no longer
become a 503, and comes through as the loop's spoken apology instead — which
is the same bargain every mid-stream failure already makes."""


async def _stream_response(
    *,
    agent: TurnRunner,
    audit: AuditSink,
    turn: TurnRequest,
    record: Caller,
    model_name: str,
    correlation_id: str,
    prompt_tokens: int,
    include_usage: bool,
    detail: dict[str, Any],
) -> Response:
    """Stream the turn as server-sent events.

    The first events are pulled *before* the response starts, so a turn that
    fails at the first hurdle — LLM host down, persona missing — still gets a
    real 503 instead of a 200 whose body turns out to be an apology. Once any
    assistant text exists the status is committed and the buffered events are
    replayed ahead of the live ones, so nothing is lost or reordered.
    """
    events = agent.run_turn(turn)
    wire = _WireTurn(correlation_id=correlation_id)
    buffered: list[_Emission] = []
    finished = False
    try:
        async for event in events:
            buffered.extend(wire.feed(event))
            if event.type in _COMMITS_THE_STREAM:
                break
            if event.type is AgentEventType.DONE:
                finished = True
                break
    except Exception as exc:  # noqa: BLE001 - a client never sees a traceback
        logger.error("api_turn_failed", error=repr(exc), correlation_id=correlation_id)
        await _record(
            audit,
            _request_audit(
                record,
                action="api.chat_completion",
                outcome=AuditOutcome.FAILURE,
                correlation_id=correlation_id,
                detail={**detail, "reason": "turn_error"},
            ),
        )
        await _aclose(events)
        return _unexpected_error()

    if wire.degraded:
        await _record(
            audit,
            _request_audit(
                record,
                action="api.chat_completion",
                outcome=AuditOutcome.FAILURE,
                correlation_id=correlation_id,
                detail={**detail, "reason": "degraded"},
            ),
        )
        await _aclose(events)
        return _degraded_error(wire.text)

    await _record(
        audit,
        _request_audit(
            record,
            action="api.chat_completion",
            outcome=AuditOutcome.SUCCESS,
            correlation_id=correlation_id,
            detail=detail,
        ),
    )

    completion_id = _completion_id(correlation_id)
    created = int(time.time())

    async def frames() -> AsyncIterator[str]:
        try:
            yield _frame(
                ChatCompletionChunk(
                    id=completion_id,
                    object="chat.completion.chunk",
                    created=created,
                    model=model_name,
                    choices=[
                        ChunkChoice(
                            index=0, delta=ChunkDelta(role="assistant"), finish_reason=None
                        )
                    ],
                )
            )
            for emission in buffered:
                yield _emission_frame(completion_id, created, model_name, emission)
            if not finished:
                async for event in events:
                    for emission in wire.feed(event):
                        yield _emission_frame(completion_id, created, model_name, emission)
            yield _frame(
                ChatCompletionChunk(
                    id=completion_id,
                    object="chat.completion.chunk",
                    created=created,
                    model=model_name,
                    choices=[
                        ChunkChoice(
                            index=0, delta=ChunkDelta(), finish_reason=wire.finish_reason
                        )
                    ],
                )
            )
            if include_usage:
                completion_tokens = _approx_tokens(wire.text)
                yield _frame(
                    ChatCompletionChunk(
                        id=completion_id,
                        object="chat.completion.chunk",
                        created=created,
                        model=model_name,
                        choices=[],
                        usage=Usage(
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=prompt_tokens + completion_tokens,
                        ),
                    )
                )
            yield SSE_DONE
        except Exception as exc:  # noqa: BLE001 - the status is already sent
            # Mid-stream failure: the headers went out as 200 long ago, so
            # there is no status left to change. Say so in the stream and close
            # it properly — a client that never sees [DONE] hangs until it
            # times out, which is a worse failure than a visible apology.
            logger.error(
                "api_stream_failed", error=repr(exc), correlation_id=correlation_id
            )
            yield _content_frame(
                completion_id,
                created,
                model_name,
                " — sorry, I lost my train of thought there and couldn't finish.",
            )
            yield _frame(
                ChatCompletionChunk(
                    id=completion_id,
                    object="chat.completion.chunk",
                    created=created,
                    model=model_name,
                    choices=[ChunkChoice(index=0, delta=ChunkDelta(), finish_reason="stop")],
                )
            )
            yield SSE_DONE
        finally:
            await _aclose(events)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Section 10's latency budget survives only if the reverse proxy in
            # front of this (section 7) forwards each frame as it arrives.
            "X-Accel-Buffering": "no",
        },
    )


async def _aclose(events: AsyncIterator[AgentEvent]) -> None:
    """Shut the agent's generator down when we stop reading it early — a
    disconnected client, a pre-flight failure — so its ``finally`` runs and the
    correlation id is unbound."""
    closer = getattr(events, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as exc:  # noqa: BLE001 - cleanup never fails a request
        logger.warning("api_turn_close_failed", error=repr(exc))
