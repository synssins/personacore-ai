"""Request translation — an OpenAI request in, a ``TurnRequest`` out.

Everything inbound that is not authentication: pulling the key out of a header,
reading the body within its ceilings, resolving the model name, and turning a
list of OpenAI messages into the one thing the agent loop understands.

The rule this module exists to keep visible is that **the request body decides
nothing**. It supplies the words, the caller's own tools and the caller's
description of its world; persona, ceilings, memory scope and permissions come
off the profile and are copied through untouched. Read every function here
looking for something that writes to a profile, and there is nothing to find.

Deliberately not here: what happens to the turn afterwards. Nothing in this
module calls the agent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from personacore.agent.loop import (
    MAX_CLIENT_TOOLS,
    ClientTool,
    ClientToolCall,
    ClientToolResult,
    ClientToolRound,
    ConversationMessage,
    TurnRequest,
)
from personacore.api.openai_caller import Caller
from personacore.api.openai_errors import _error
from personacore.api.openai_wire import (
    ChatCompletionRequest,
    ChatMessageIn,
    OpenAIApiConfig,
    _arguments_json,
)
from personacore.audit.logging import get_logger
from personacore.audit.models import Surface

logger = get_logger(__name__)


def _bearer(authorization: str | None) -> tuple[str | None, str | None]:
    """Pull the key out of an ``Authorization`` header.

    Returns ``(key, None)`` or ``(None, reason)``. The reason is for the audit
    log only; the caller sees one 401 whatever it says.
    """
    if not authorization or not authorization.strip():
        return None, "missing_authorization"
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None, "unsupported_scheme"
    value = value.strip()
    if not value:
        return None, "empty_credentials"
    return value, None


async def _read_body(
    request: Request, settings: OpenAIApiConfig
) -> ChatCompletionRequest | JSONResponse:
    """Parse and validate the request body, in OpenAI's error shape.

    The declared ``Content-Length`` is checked before the body is read, so an
    oversized request costs a header parse rather than a megabyte of memory —
    and the read is checked again afterwards, because a chunked request has no
    declared length to check.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > settings.max_body_bytes:
        return _error(
            413,
            "That request is too large for this assistant to read.",
            error_type="invalid_request_error",
            code="request_too_large",
        )
    raw = await request.body()
    if len(raw) > settings.max_body_bytes:
        return _error(
            413,
            "That request is too large for this assistant to read.",
            error_type="invalid_request_error",
            code="request_too_large",
        )
    try:
        return ChatCompletionRequest.model_validate_json(raw)
    except ValueError:
        # The pydantic detail is not echoed back: it quotes the offending
        # input, and this input is untrusted (section 7). It is worth a log
        # line, not a response body.
        logger.info("api_request_unparsable")
        return _error(
            400,
            "That request isn't in a shape this assistant understands. It expects "
            "an OpenAI-style chat completion with a 'messages' list.",
            error_type="invalid_request_error",
            param="messages",
            code="invalid_request",
        )


def _resolve_model(requested: str | None, settings: OpenAIApiConfig) -> str | JSONResponse:
    """Decide which name goes on the reply, and refuse an unknown one."""
    if not requested:
        return settings.models[0]
    if requested in settings.models:
        return requested
    if not settings.strict_model:
        return settings.models[0]
    return _error(
        404,
        f"This assistant does not offer a model called {requested!r}. "
        f"Ask it for /v1/models and use one of those.",
        error_type="invalid_request_error",
        param="model",
        code="model_not_found",
    )


def _to_turn_request(
    body: ChatCompletionRequest,
    record: Caller,
    settings: OpenAIApiConfig,
    correlation_id: str,
) -> TurnRequest | JSONResponse:
    """Turn an OpenAI request into a :class:`TurnRequest`.

    **``system`` and ``developer`` messages are kept, as caller context.** They
    are the caller describing its own world — Home Assistant's exposed-entity
    list arrives in exactly that slot — and they go to
    :attr:`TurnRequest.caller_context`, which the loop fences and places ahead
    of the persona. They are not history, so they never become a ``user`` or
    ``assistant`` turn, and they reach nothing that decides tools, ceilings or
    permissions. See :func:`_caller_context` for the ordering and the cap.

    **``tool`` messages are now kept**, and they are the return leg. They used
    to be dropped on the reasoning that tools run inside the core; that is
    still true of *container* tools, and a client still cannot hand in a result
    for one and have it believed as anything but fenced outside content. But
    the caller's own tools run in the caller, so its results are the only place
    they can come from. They are split off here rather than folded into
    ``history``, because they happened *after* the user spoke and the prompt
    has to say so — see :meth:`AgentLoop._replay_client_tools`.
    """
    messages = body.messages
    last_user = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"),
        None,
    )
    if last_user is None:
        return _error(
            400,
            "There's no user message in that request, so there's nothing to answer.",
            error_type="invalid_request_error",
            param="messages",
            code="invalid_request",
        )

    user_message = messages[last_user].text()
    if not user_message.strip():
        return _error(
            400,
            "The last user message is empty, so there's nothing to answer.",
            error_type="invalid_request_error",
            param="messages",
            code="invalid_request",
        )
    if len(user_message) > settings.max_message_chars:
        return _error(
            400,
            "That message is longer than this assistant will accept in one go. "
            f"Keep it under {settings.max_message_chars} characters.",
            error_type="invalid_request_error",
            param="messages",
            code="context_length_exceeded",
        )

    earlier = [m for m in messages[:last_user] if m.role in ("user", "assistant")]
    history: list[ConversationMessage] = []
    for message in earlier[-settings.max_history_messages :]:
        text = message.text()
        if not text:
            continue
        history.append(
            ConversationMessage(
                role="user" if message.role == "user" else "assistant",
                content=text[: settings.max_message_chars],
            )
        )

    offered = _client_tools(body)
    return TurnRequest(
        user_message=user_message,
        profile=record.profile,
        surface=Surface.API,
        history=history,
        caller_context=_caller_context(messages, settings),
        # persona_override is never taken from the request: on this surface the
        # persona is the key's, full stop (section 5.4).
        correlation_id=correlation_id,
        # ``tool_choice: "none"`` withdraws the offer for this request; it does
        # not erase what already happened, so the rounds are replayed either
        # way. Several clients send "none" on the final call precisely to force
        # a spoken answer out of the results they just handed back.
        client_tools=offered if body.tool_choice != "none" else [],
        client_tool_rounds=_client_tool_rounds(messages[last_user + 1 :], settings),
    )


def _caller_context(
    messages: Sequence[ChatMessageIn], settings: OpenAIApiConfig
) -> str:
    """Every ``system`` and ``developer`` message, joined in the order sent.

    ``developer`` is OpenAI's newer name for ``system`` and both are in the
    spec, so they are treated identically — a client that renamed the role
    should not get different behaviour for the same content.

    **Several are concatenated rather than reduced to one.** The standard
    permits more than one and says nothing about precedence, so there is no
    correct choice to make on the caller's behalf; keeping all of them in
    order preserves whatever the caller meant and loses nothing. Position in
    the list is not consulted either — a client that puts its system message
    last is describing the same world as one that puts it first.

    The array-of-parts content form flattens through
    :meth:`ChatMessageIn.text`, like every other role.

    The join is capped at ``max_message_chars`` and **truncated, not refused**.
    A per-message 400 is right for the question being asked, because a silently
    shortened question gets a confidently wrong answer; this is the caller's
    boilerplate plus an entity list, and a house with a lot of lamps should
    still get an answer. The agent loop caps it again when it fences it, with a
    visible note in the prompt saying so.
    """
    parts = [m.text() for m in messages if m.role in ("system", "developer")]
    joined = "\n\n".join(part for part in parts if part.strip())
    return joined[: settings.max_message_chars]


def _client_tools(body: ChatCompletionRequest) -> list[ClientTool]:
    """The caller's ``tools``, parsed leniently.

    One unusable entry is dropped, never a whole request refused: a client that
    offers thirty tools and one with a name this API cannot carry should get
    twenty-nine, and the caller's schema is the caller's document — unknown
    keys inside it are ignored rather than rejected.

    Duplicates collapse to the first. Nothing here is checked against the
    profile, because nothing here is executed here.
    """
    parsed: list[ClientTool] = []
    seen: set[str] = set()
    for raw in (body.tools or [])[:MAX_CLIENT_TOOLS]:
        try:
            tool = ClientTool.model_validate(raw)
        except ValidationError:
            # Not echoed back: the offending input is the caller's and
            # untrusted (section 7). A log line, not a response body.
            logger.info("api_client_tool_dropped")
            continue
        if tool.function.name in seen:
            continue
        seen.add(tool.function.name)
        parsed.append(tool)
    return parsed


def _client_tool_rounds(
    trailing: Sequence[ChatMessageIn], settings: OpenAIApiConfig
) -> list[ClientToolRound]:
    """The handovers the caller is replaying, from the messages after the user.

    A round is one assistant message carrying ``tool_calls`` followed by the
    ``tool`` messages answering it. Anything else — a stray ``tool`` message
    with no call before it, a result quoting an id that round never mentioned —
    is dropped, so the model is never shown a result with nothing to hang it
    on.

    The names on these calls are labels. Nothing here routes to anything
    executable: a container tool named in a replayed round does not run, is not
    gated, and reaches the model only as fenced outside content
    (:meth:`AgentLoop._replay_client_tools`), which is the same standing a
    sentence the user typed has.
    """
    rounds: list[ClientToolRound] = []
    current: ClientToolRound | None = None
    for message in trailing:
        if message.role == "assistant" and message.tool_calls:
            calls = _replayed_calls(message.tool_calls)
            current = None
            if not calls:
                continue
            current = ClientToolRound(
                text=message.text()[: settings.max_message_chars], calls=calls
            )
            rounds.append(current)
            continue
        if message.role == "tool":
            if current is None or not message.tool_call_id:
                continue
            current.results.append(
                ClientToolResult(
                    id=message.tool_call_id[:128],
                    content=message.text()[: settings.max_message_chars],
                )
            )
            continue
        # A user or assistant message ends the run of tool traffic.
        current = None
    return rounds[-settings.max_history_messages :]


def _replayed_calls(raw: Sequence[Any]) -> list[ClientToolCall]:
    """The ``tool_calls`` of one replayed assistant message.

    Everything in here came off the wire, so each entry is validated on its own
    and a bad one is skipped rather than raised on.
    """
    calls: list[ClientToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            # A client that sent the object rather than the JSON string. Ours
            # went out as a string; take what it meant rather than lose the
            # round over how it spelled it.
            arguments = _arguments_json(arguments)
        try:
            calls.append(
                ClientToolCall(
                    id=str(item.get("id") or "")[:128],
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )
        except ValidationError:
            logger.info("api_replayed_tool_call_dropped")
    return calls
