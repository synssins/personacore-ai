"""Errors, audit, and the token estimate.

What this surface says when it is not answering, plus the two pieces of
book-keeping every route does either way: the access record (section 7) and the
``usage`` estimate, which clients read and several refuse a response without.

The envelope is OpenAI's, not FastAPI's, because a standard client renders
``error.message`` and shows a raw blob for anything else. That is also why these
are functions returning responses rather than exceptions: the app object is
assembled elsewhere, and a router cannot install exception handlers on it.

Deliberately not here: any decision about *whether* something is an error. The
routes decide that; this module only dresses it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse

from personacore.agent.protocols import AuditSink
from personacore.api.openai_caller import Caller
from personacore.api.openai_wire import ApiError, ApiErrorDetail, ChatMessageIn
from personacore.audit.logging import get_logger
from personacore.audit.models import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    Owner,
    Surface,
)

logger = get_logger(__name__)


def _error(
    status: int,
    message: str,
    *,
    error_type: str,
    param: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """An error in OpenAI's envelope, so a standard client renders it."""
    body = ApiError(
        error=ApiErrorDetail(message=message, type=error_type, param=param, code=code)
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=headers)


def _degraded_error(spoken: str) -> JSONResponse:
    """Spec section 10: the assistant says plainly that it cannot do the thing.

    ``spoken`` is the agent loop's own sentence — already written to be said out
    loud and already free of internal detail — so it goes through verbatim.
    """
    return _error(
        503,
        spoken or "I can't reach the part of me that answers questions right now.",
        error_type="server_error",
        code="service_unavailable",
    )


def _unexpected_error() -> JSONResponse:
    """The catch-all. Deliberately says nothing about what broke."""
    return _error(
        500,
        "Something went wrong on my end, so I couldn't answer that.",
        error_type="server_error",
        code="internal_error",
    )


def _request_audit(
    record: Caller,
    *,
    action: str,
    outcome: AuditOutcome,
    correlation_id: str,
    detail: dict[str, Any],
) -> AuditRecord:
    """One audited API request, attributed to the key's profile (sections 7, 8).

    ``AuditCategory.ACCESS`` is the right shelf: this is the record of a door
    being used, not of a tool being run — the loop writes those itself, with
    the same ``correlation_id``, so the trace view can group them.
    """
    return AuditRecord(
        correlation_id=correlation_id,
        timestamp=datetime.now(UTC),
        surface=Surface.API,
        owner=Owner.profile(record.profile.id),
        category=AuditCategory.ACCESS,
        action=action,
        outcome=outcome,
        detail=detail,
    )


async def _record(audit: AuditSink, record: AuditRecord) -> None:
    """Write an audit record, never failing the request over it.

    Mirrors ``AgentLoop._write_audit``: a full disk should degrade the audit
    trail, loudly, rather than take the assistant down with it (section 10).
    """
    try:
        await audit.record_audit(record)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.error("api_audit_write_failed", error=repr(exc), action=record.action)


def _prompt_text(messages: Sequence[ChatMessageIn]) -> str:
    return "".join(message.text() for message in messages)


def _approx_tokens(text: str) -> int:
    """An estimate, and labelled as one.

    OpenAI clients read ``usage``, and several refuse a response without it, so
    the field cannot be omitted — but the core never sees real token counts:
    section 5.3 keeps the LLM behind a streaming interface, and the backend that
    could count is a different machine. Four characters per token is the usual
    English approximation. It is good enough for "roughly how much did that
    cost"; it is not an accounting record, and nothing should bill from it.
    """
    return (len(text) + 3) // 4 if text else 0


def _completion_id(correlation_id: str) -> str:
    """OpenAI-shaped id that doubles as the trace key.

    Sharing the correlation id means a user quoting the id from their client
    lands an operator directly on that turn in section 9's trace view. It is a
    random uuid4, so it identifies the request and nothing about the caller.
    """
    return f"chatcmpl-{correlation_id}"
