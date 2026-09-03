"""The non-streaming turn: run it to the end, answer in one JSON body.

The simpler of the two turn paths, and the one that keeps its options open —
nothing has been sent, so a turn that degrades still becomes a 503 rather than a
200 whose body is an apology. The streaming path gives that up deliberately
(:mod:`personacore.api.openai_streaming`), and this module is what it is being
traded against.

Deliberately not here: the folding. What the events mean belongs to
``_WireTurn``, so that this path and the streamed one cannot disagree about a
turn.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Response
from fastapi.responses import JSONResponse

from personacore.agent.loop import TurnRequest
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
    ChatCompletion,
    ChatCompletionChoice,
    ChatMessageOut,
    Usage,
    _dump,
)
from personacore.audit.logging import get_logger
from personacore.audit.models import AuditOutcome

logger = get_logger(__name__)


async def _blocking_response(
    *,
    agent: TurnRunner,
    audit: AuditSink,
    turn: TurnRequest,
    record: Caller,
    model_name: str,
    correlation_id: str,
    prompt_tokens: int,
    detail: dict[str, Any],
) -> Response:
    """Run the turn to completion and answer in one JSON body."""
    wire = _WireTurn(correlation_id=correlation_id)
    events = agent.run_turn(turn)
    try:
        async for event in events:
            wire.feed(event)
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
    completion_tokens = _approx_tokens(wire.text)
    payload = ChatCompletion(
        id=_completion_id(correlation_id),
        created=int(time.time()),
        model=model_name,
        choices=[
            ChatCompletionChoice(
                message=ChatMessageOut(
                    content=wire.content, tool_calls=wire.client_calls or None
                ),
                finish_reason=wire.finish_reason,
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        personacore=wire.extension,
    )
    return JSONResponse(_dump(payload))
