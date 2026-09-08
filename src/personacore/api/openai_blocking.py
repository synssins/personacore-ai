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

import contextlib
import time
from datetime import datetime
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
from personacore.conversations.models import Conversation
from personacore.conversations.service import ConversationService

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
    conversations: ConversationService | None = None,
    conversation: Conversation | None = None,
    since: datetime | None = None,
) -> Response:
    """Run the turn to completion and answer in one JSON body.

    ``conversations``/``conversation``/``since`` are the live-conversation
    wiring (``openai_router.py``'s ``chat_completions``): once the turn has
    written its rows, the resolved conversation claims the ones written
    ``since`` the turn began — see :meth:`ConversationService.append`. Best
    effort and silent either way, the same as the admin chat screen's own
    call: a claim that fails leaves the reply exactly as usable as it always
    was, unattached until the next backfill.
    """
    wire = _WireTurn(correlation_id=correlation_id)
    events = agent.run_turn(turn)
    try:
        try:
            async for event in events:
                wire.feed(event)
        finally:
            # However the turn ends — finished, degraded, or raised — any rows
            # it managed to write before that already carry this conversation
            # id (the loop stamps it straight onto each row); this call is
            # only what recomputes `last_activity_at` and the title, so it
            # runs on every exit rather than only the happy path.
            if conversations is not None and since is not None:
                with contextlib.suppress(Exception):
                    await conversations.append(conversation, since=since)
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
