"""The ``/v1`` router and its front door.

The factory that builds the routes, and the authentication in front of them.
Those two are one module because the order between them is the point:
**nothing runs before the key is verified** — not body parsing, not schema
validation, not the model list — so an unauthenticated caller cannot use error
messages to map the surface. It is why the body is read by hand here rather
than declared as a FastAPI parameter, which would be validated before the
endpoint ever ran.

Every dependency arrives as an argument. This module owns nothing global; the
app object, its lifespan and what else is mounted beside it belong to whoever
assembles the process.

Deliberately not here: the wire shapes, the translation and the two turn paths.
This module picks which one answers, and audits that it did.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from personacore.agent.protocols import AuditSink
from personacore.api.keys import ApiKeyStore
from personacore.api.openai_blocking import _blocking_response
from personacore.api.openai_caller import Caller, KeylessCaller, TurnRunner
from personacore.api.openai_errors import (
    _approx_tokens,
    _error,
    _prompt_text,
    _record,
    _request_audit,
)
from personacore.api.openai_streaming import _stream_response
from personacore.api.openai_translate import (
    _bearer,
    _read_body,
    _resolve_model,
    _to_turn_request,
)
from personacore.api.openai_wire import ModelCard, ModelList, OpenAIApiConfig
from personacore.audit.logging import get_correlation_id, get_logger
from personacore.audit.models import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    Owner,
    Surface,
)
from personacore.contracts.policy import PolicyProfile

logger = get_logger(__name__)


_INVALID_KEY_MESSAGE = (
    "Invalid API key. Ask whoever runs this assistant to issue you one."
)
"""One message for absent, malformed, unknown and disabled keys alike. Anything
that varied between them would be an oracle for probing which keys exist."""


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


def create_openai_router(
    *,
    agent: TurnRunner,
    keys: ApiKeyStore,
    audit: AuditSink,
    config: OpenAIApiConfig | None = None,
    keyless: Callable[[], PolicyProfile | None] | None = None,
) -> APIRouter:
    """Build the ``/v1`` router. Mount it with ``app.include_router(...)``.

    The router carries its own ``/v1`` prefix, so the paths are exactly the
    ones a standard client expects with no assembly-time coordination. Every
    dependency arrives here as an argument: the caller owns the app, the
    lifespan and the wiring, and this module owns nothing global.

    :param agent: the agent loop. Requests pass through it whole — persona,
        tools and memory (section 5.4) — and this router has no other route to
        the LLM.
    :param keys: the per-key policy store (section 5.4). Issuing and revoking
        belong to the admin API; this router only verifies.
    :param audit: the audit store (section 7). Both accepted and rejected
        requests are recorded against ``Surface.API``.
    :param config: wire-level tunables; defaults are sane for a household LAN.
    :param keyless: ADR-0018. Asked, per request, for the profile a caller with
        no valid key should get — or ``None`` for "there is no such caller",
        which is the default and what every existing assembly gets by omitting
        it. Read per request rather than captured, so an operator turning the
        switch off closes the door on the very next request rather than at the
        next restart. **This is the ``/v1`` surface only**; the admin surface
        has its own door (ADR-0032) and knows nothing about this.
    """
    settings = config or OpenAIApiConfig()
    router = APIRouter(prefix="/v1", tags=["openai"])
    # Fixed at assembly time rather than per request: a model's `created` is
    # supposed to be a property of the model, and a value that changes every
    # call makes client-side caching behave oddly.
    catalogue_created = int(time.time())

    async def _authenticate(
        authorization: str | None, *, path: str
    ) -> Caller | JSONResponse:
        """Verify the bearer key, auditing the attempt either way.

        Returns the caller, or the 401 to send back. That 401 is identical for
        every failure mode — see ``_INVALID_KEY_MESSAGE``.

        **A valid key always wins.** It is checked first and returned first, so
        turning keyless on never widens what a key already had: a caller that
        presents a real key gets that key's profile and that key's ceiling, and
        keyless is the floor underneath it rather than a replacement for it
        (ADR-0018 §4).

        **Keyless answers a bad credential, not merely a missing one.** The
        official ``openai`` Python SDK refuses to construct a client without an
        API key at all, so every client built on it — Home Assistant's
        "Skip Authentication" among them — sends a placeholder string instead.
        A server that only admitted the *absence* of ``Authorization`` would be
        unreachable from exactly the clients this exists for. So once the key
        check has failed, for any of its four reasons, the question is simply
        whether this core answers callers with no key; the reason is kept for
        the log, where it is diagnosis rather than policy.
        """
        presented, reason = _bearer(authorization)
        record = keys.verify(presented) if presented else None
        if record is not None:
            return record

        detail_reason = reason or "unknown_or_disabled_key"
        anonymous = keyless() if keyless is not None else None
        if anonymous is not None and anonymous.enabled:
            # Not audited separately: the endpoint that answers this request
            # writes its own ACCESS record, attributed to this profile and
            # carrying `key_id` "keyless", so the trail already says a keyless
            # caller was here. A second record per request would double every
            # row in the log for no fact it does not already hold.
            logger.info("api_keyless_admitted", reason=detail_reason, path=path)
            return KeylessCaller(profile=anonymous)

        logger.warning("api_auth_rejected", reason=detail_reason, path=path)
        await _record(
            audit,
            AuditRecord(
                correlation_id=get_correlation_id() or uuid4().hex,
                timestamp=datetime.now(UTC),
                surface=Surface.API,
                owner=Owner.anonymous(),
                category=AuditCategory.ACCESS,
                action="api.request_rejected",
                outcome=AuditOutcome.REFUSED,
                # No key material, not even a fingerprint: an audit store
                # that holds something derived from a credential is one
                # more copy of that credential to protect.
                detail={"reason": detail_reason, "path": path},
            ),
        )
        return _error(
            401,
            _INVALID_KEY_MESSAGE,
            error_type="invalid_request_error",
            code="invalid_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @router.get("/models")
    async def list_models(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """What this core offers (spec section 5.4).

        Behind the same key as everything else: the model list is one of the
        things "nothing else" in "an unknown key gets 401 and nothing else"
        refers to.
        """
        record = await _authenticate(authorization, path="/v1/models")
        if isinstance(record, JSONResponse):
            return record
        await _record(
            audit,
            _request_audit(
                record,
                action="api.models",
                outcome=AuditOutcome.SUCCESS,
                correlation_id=get_correlation_id() or uuid4().hex,
                detail={},
            ),
        )
        payload = ModelList(
            data=[
                ModelCard(id=name, created=catalogue_created, owned_by=settings.owned_by)
                for name in settings.models
            ]
        )
        return JSONResponse(payload.model_dump(mode="json"))

    @router.get("/models/{model}")
    async def retrieve_model(
        model: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """One entry of ``/v1/models``, by id (spec section 5.4).

        Authenticated first, looked up second — same as every other route
        here, but the reason bites harder in this one shape: a caller who
        knows two status codes (401 for "wrong key", 404 for "no such model")
        could otherwise probe ids with no key at all. Checking the key before
        the id means an anonymous caller gets the identical 401 whether it
        asked for ``personacore`` or a name it made up, so the list this core
        advertises is the only way to learn what is on it.
        """
        path = f"/v1/models/{model}"
        record = await _authenticate(authorization, path=path)
        if isinstance(record, JSONResponse):
            return record
        correlation_id = get_correlation_id() or uuid4().hex
        if model not in settings.models:
            await _record(
                audit,
                _request_audit(
                    record,
                    action="api.models",
                    outcome=AuditOutcome.FAILURE,
                    correlation_id=correlation_id,
                    detail={"reason": "unknown_model"},
                ),
            )
            return _error(
                404,
                f"This assistant does not offer a model called {model!r}. "
                f"Ask it for /v1/models and use one of those.",
                error_type="invalid_request_error",
                # OpenAI does not document a `type`/`code` pair for this
                # endpoint's 404 (see docs/research/openai-wire-conformance.md
                # §5's "Not confirmed" note) — this is our choice, made to
                # match the code `_resolve_model` already uses below for "you
                # asked this core for a model it doesn't have", not a value
                # OpenAI is known to send. `param` is left unset: unlike that
                # 404, this one is about a path segment, not a body field, and
                # every other `param` in this module names a JSON key.
                code="model_not_found",
            )
        await _record(
            audit,
            _request_audit(
                record,
                action="api.models",
                outcome=AuditOutcome.SUCCESS,
                correlation_id=correlation_id,
                detail={"model": model},
            ),
        )
        payload = ModelCard(id=model, created=catalogue_created, owned_by=settings.owned_by)
        return JSONResponse(payload.model_dump(mode="json"))

    @router.post("/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """A turn of conversation, streaming or not.

        The body is parsed by hand rather than declared as a FastAPI parameter
        so that authentication genuinely comes first: a declared body model is
        validated before the endpoint runs, which would let an unauthenticated
        caller read 422s describing the schema.
        """
        path = "/v1/chat/completions"
        record = await _authenticate(authorization, path=path)
        if isinstance(record, JSONResponse):
            return record

        # The id the request middleware bound, so the access record, the
        # transcript and every tool call of this turn are one group in the
        # trace (PC-012). A fresh one only when this router is mounted without
        # that middleware, which is a test assembly, not the application.
        correlation_id = get_correlation_id() or uuid4().hex
        body = await _read_body(request, settings)
        if isinstance(body, JSONResponse):
            await _record(
                audit,
                _request_audit(
                    record,
                    action="api.chat_completion",
                    outcome=AuditOutcome.FAILURE,
                    correlation_id=correlation_id,
                    detail={"reason": "invalid_request"},
                ),
            )
            return body

        model_name = _resolve_model(body.model, settings)
        if isinstance(model_name, JSONResponse):
            await _record(
                audit,
                _request_audit(
                    record,
                    action="api.chat_completion",
                    outcome=AuditOutcome.FAILURE,
                    correlation_id=correlation_id,
                    detail={"reason": "unknown_model"},
                ),
            )
            return model_name

        turn = _to_turn_request(body, record, settings, correlation_id)
        if isinstance(turn, JSONResponse):
            await _record(
                audit,
                _request_audit(
                    record,
                    action="api.chat_completion",
                    outcome=AuditOutcome.FAILURE,
                    correlation_id=correlation_id,
                    detail={"reason": "invalid_messages"},
                ),
            )
            return turn

        # Counts, never content: ADR-0004's transcript store is where what was
        # said lives, and the agent loop writes it.
        base_detail: dict[str, Any] = {
            "model": model_name,
            "stream": body.stream,
            "messages": len(body.messages),
            "raw_passthrough": record.profile.raw_passthrough,
            "key_id": record.key_id,
        }
        prompt_tokens = _approx_tokens(_prompt_text(body.messages))

        if body.stream:
            return await _stream_response(
                agent=agent,
                audit=audit,
                turn=turn,
                record=record,
                model_name=model_name,
                correlation_id=correlation_id,
                prompt_tokens=prompt_tokens,
                include_usage=body.wants_usage_chunk(),
                detail=base_detail,
            )
        return await _blocking_response(
            agent=agent,
            audit=audit,
            turn=turn,
            record=record,
            model_name=model_name,
            correlation_id=correlation_id,
            prompt_tokens=prompt_tokens,
            detail=base_detail,
        )

    return router
