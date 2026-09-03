"""OpenAI's shapes on the wire, and the bytes they serialise to.

This module owns the contract with the outside world: the request and response
models, the streaming chunk, the error envelope, the ``personacore`` extension
key, and the functions that turn those models into what actually goes down the
socket. It is one file because the failures here are byte-level and silent — an
``object`` literal, a ``finish_reason`` outside the enum, a key that is ``null``
where a client expected it absent — and a reader checking any of that should not
have to follow it across modules.

It owns no behaviour. Nothing here authenticates, runs a turn, or knows the
agent loop exists; it is shapes and serialisers, which is why every other
module of this surface can import it and it can import none of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from personacore.audit.logging import get_logger

logger = get_logger(__name__)


DEFAULT_MODEL_ID = "personacore"
"""What the core calls itself on the wire. See :class:`OpenAIApiConfig`."""

SSE_DONE = "data: [DONE]\n\n"
"""The sentinel every OpenAI streaming client waits for. A stream that ends
without it looks to a client like a dropped connection."""


# ---------------------------------------------------------------------------
# Wire models — OpenAI's shapes, not ours
# ---------------------------------------------------------------------------


class ChatMessageIn(BaseModel):
    """One message as a client sends it.

    ``extra="ignore"`` is a deliberate departure from this codebase's
    ``extra="forbid"`` habit, and it is confined to the wire models: section
    5.4 requires that "any standard client works unmodified", and standard
    clients send ``name``, ``tool_call_id``, ``refusal``, ``annotations`` and
    whatever else the API grew last month. Rejecting an unknown field would
    break the compatibility promise; internal models keep ``forbid``.
    """

    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | list[dict[str, Any]] | None = None

    tool_calls: list[dict[str, Any]] | None = None
    """On an ``assistant`` message: the handover this core sent last time,
    quoted back. Declared rather than ignored because it is the outbound half
    of the return leg — without it the model is shown a tool result with no
    call to hang it on, and several backends' chat templates simply drop it."""

    tool_call_id: str | None = None
    """On a ``tool`` message: which call this answers. It is the only thing
    tying a result to a tool name; a result whose id we did not send is
    dropped rather than trusted (see :func:`_client_tool_rounds`)."""

    def text(self) -> str:
        """Flatten to plain text.

        The multimodal ``content`` array is accepted and its text parts are
        used; non-text parts are dropped rather than refused, because a client
        that always sends the array form should still get an answer. Vision is
        out of scope for the core (it is a section 3.1 plugin's job), so there
        is nothing here that could honour an image part.
        """
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        parts: list[str] = []
        for item in self.content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)


class ChatCompletionRequest(BaseModel):
    """``POST /v1/chat/completions``, as OpenAI documents it.

    Sampling parameters (``temperature``, ``top_p``, ``max_tokens``, …) are
    accepted and ignored rather than rejected: spec section 5.3 puts model
    behaviour in config, so an untrusted client does not get to retune the
    household assistant by sending ``temperature: 2``.
    """

    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessageIn] = Field(min_length=1)
    model: str | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    user: str | None = None
    """Ignored for attribution. Who the caller is comes from the key, not from
    a field the caller fills in themselves (spec section 8)."""

    tools: list[Any] | None = None
    """The tools **the caller** can run, in OpenAI's schema shape.

    Typed as loosely as a list can be, on purpose: one malformed entry must
    cost that entry, not the request, and a declared ``list[dict]`` would make
    a client that sent one junk element get a 400 for the whole call.

    Raw dicts here and parsed leniently in :func:`_client_tools`: one tool with
    a name this core cannot put on a wire is a tool to drop, not a request to
    refuse. These never grant anything — see :class:`ChatMessageOut`."""

    tool_choice: str | dict[str, Any] | None = None
    """Only ``"none"`` is acted on, and it withdraws the caller's own tools for
    this request. ``"auto"``, ``"required"`` and a named function are accepted
    and ignored: what the model reaches for is the model's business, and
    forcing a call it did not want is not something this surface can promise
    across every backend spec section 5.3 allows."""

    parallel_tool_calls: bool | None = None
    """Accepted and ignored. This surface never manufactures a second call —
    it reports what the model produced — so there is nothing here to switch
    off that is not already off."""

    def wants_usage_chunk(self) -> bool:
        options = self.stream_options or {}
        return bool(options.get("include_usage"))

    def offers_tools(self) -> bool:
        return bool(self.tools) and self.tool_choice != "none"


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


FinishReason = Literal["stop", "length", "content_filter", "tool_calls"]
"""OpenAI's closed set. A client's parser validates against exactly these, so a
value invented here (``"error"``, ``"cancelled"``) is a hard failure in strict
clients rather than an unrecognised string they ignore.

``"tool_calls"`` means "I have stopped; you run this and call me back". It was
removed from this type after v0.9.0, which sent it — and ``"stop"`` — alongside
calls to tools that only exist *inside* this container, and Extended OpenAI
Conversation duly tried to run one.

It is back, and it is now true. The only calls that reach
``message.tool_calls`` are calls to tools **the caller itself said it has**, in
the ``tools`` field of its own request. Handing one of those back is not a
guess about how clients branch; it is the thing the value was defined to mean.
A turn that used only container tools still ends ``"stop"`` with nothing in the
standard field, exactly as before."""


class ToolInvocation(BaseModel):
    """A tool call the core made — and had already finished — by the time this
    went on the wire. Not part of OpenAI's schema, on purpose: see
    :class:`TurnExtension`."""

    model_config = ConfigDict(extra="forbid")

    index: int
    id: str
    name: str
    arguments: str
    """A JSON-encoded *string*, matching the shape OpenAI uses for the same
    field, so an operator reading this sees what a tool call normally looks
    like. Nothing parses it to decide what to do; it is a record, not an
    instruction."""


class ToolOutcome(BaseModel):
    """What a tool did, once it had run. Not part of OpenAI's schema — see
    :class:`TurnExtension`."""

    model_config = ConfigDict(extra="forbid")

    index: int | None = None
    id: str | None = None
    name: str
    ok: bool
    error: str | None = None


class TurnExtension(BaseModel):
    """PersonaCore's addition to OpenAI's shapes, under its own key.

    **Everything about the core's own tool activity lives here, and nowhere
    else on the wire.** OpenAI's schema has two slots that look like they would
    fit — ``message.tool_calls`` and ``delta.tool_calls`` — and both are
    forbidden here, because in that schema a tool call is an *instruction to
    the client*: run this and call me back. Every field in this class describes
    work that is already finished. Putting any of it in a standard field asks a
    client to execute a tool that only exists inside this container, which is
    exactly what broke Extended OpenAI Conversation in v0.9.0 (ADR-0035).

    A namespaced top-level key is the one place that cannot happen. No client
    dispatches on an unknown key: SDKs keep it and ignore it, so an operator or
    a curious client can read the whole story and nothing acts on it.

    The tool's *content* is never here — it is untrusted text (ADR-0003) and
    belongs in the trace view, not on this wire.
    """

    model_config = ConfigDict(extra="forbid")

    tool_calls: list[ToolInvocation] = Field(default_factory=list)
    tool_results: list[ToolOutcome] = Field(default_factory=list)


class ResponseFunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: str
    """A JSON-encoded string, and passed through from the model **verbatim**.
    Re-encoding it would change what the caller's own schema is handed, and
    every client on this API calls ``json.loads`` on exactly these bytes."""


class ResponseToolCall(BaseModel):
    """A call this core is asking the **caller** to run.

    Only ever a tool the caller offered in its own ``tools``. A container tool
    never appears here — that was v0.9.0's mistake and it is what
    :class:`TurnExtension` exists for.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["function"] = "function"
    function: ResponseFunctionCall


class ChatMessageOut(BaseModel):
    """The assistant's message.

    ``tool_calls`` here is an **instruction to the caller**, and the only thing
    that ever fills it is a call to a tool the caller told us about in its own
    ``tools``. It is ``None`` — and omitted from the JSON entirely, see
    :func:`_dump` — on every other turn. Tools this container ran are reported
    under the ``personacore`` key instead; see :class:`TurnExtension`.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    """Nullable, and ``null`` — not ``""`` — on a turn that produced no words.
    OpenAI sends ``null`` there and clients branch on it."""

    tool_calls: list[ResponseToolCall] | None = None


class ChatCompletionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = 0
    message: ChatMessageOut
    finish_reason: FinishReason = "stop"
    logprobs: None = None


class ChatCompletion(BaseModel):
    """A non-streaming reply."""

    model_config = ConfigDict(extra="forbid")

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage
    personacore: TurnExtension | None = None


class DeltaFunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    arguments: str | None = None


class DeltaToolCall(BaseModel):
    """One streamed fragment of a handover, in OpenAI's streaming framing.

    The rules, each with a broken client behind it:

    * ``index`` on **every** fragment. Missing it is a ``RuntimeError`` in
      ``openai-python`` and a *silent total loss* in LangChain, which does
      ``except KeyError: pass``.
    * ``id``, ``type`` and ``function.name`` exactly **once** for that index.
      Repeating them concatenates — a name resent five times becomes
      ``get_weatherget_weather…`` — and ``openai-node`` assigns rather than
      concatenating, so it looks fine in JavaScript. That is how this ships
      undetected.
    * ``arguments`` is a JSON-encoded **string**, sent as fragments that
      concatenate.

    This surface sends one fragment per call carrying all of it, which
    satisfies every rule above: identity once, arguments in a single fragment.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    id: str | None = None
    type: Literal["function"] | None = None
    function: DeltaFunctionCall | None = None


class ChunkDelta(BaseModel):
    """A streamed delta.

    ``tool_calls`` is the more dangerous half of a handover, because a delta
    accumulates into a message the client then dispatches on — so it is set on
    exactly the turns where dispatching is the point, and left unset
    everywhere else. ``exclude_unset`` in :func:`_frame` keeps it off the wire
    entirely rather than sending ``null`` or, worse, ``[]``: an empty list makes
    consumers take the tool branch, loop zero times, and drop all the text.

    A tool this container ran never appears here. Its frame carries an empty
    delta and a ``personacore`` extension (:class:`TurnExtension`), which is
    visible and inert.
    """

    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    content: str | None = None
    tool_calls: list[DeltaToolCall] | None = None


class ChunkChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = 0
    delta: ChunkDelta
    finish_reason: FinishReason | None = None
    logprobs: None = None


class ChatCompletionChunk(BaseModel):
    """One SSE frame's payload.

    Serialised with ``exclude_unset=True`` so a delta carries only the keys
    that were actually set — ``{"content": "..."}``, not
    ``{"role": null, "content": "..."}`` — which is what real OpenAI frames
    look like and what strict client parsers expect.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    usage: Usage | None = None
    personacore: TurnExtension | None = None


class ModelCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: Literal["list"] = "list"
    data: list[ModelCard]


class ApiErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class ApiError(BaseModel):
    """OpenAI's error envelope. Standard clients unwrap this and show
    ``error.message``; anything else shows them a raw JSON blob."""

    model_config = ConfigDict(extra="forbid")

    error: ApiErrorDetail


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class OpenAIApiConfig(BaseModel):
    """Tunables for the router. A factory argument, never ambient config."""

    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=lambda: [DEFAULT_MODEL_ID], min_length=1)
    """What ``GET /v1/models`` advertises, and the only values
    ``chat/completions`` will accept in its ``model`` field.

    The core serves one logical model — the assistant — because which *backend*
    model runs is config (section 5.3) and which persona answers is the key's
    profile (section 5.4). Neither is a client's decision, so these ids are
    names for this core, not a menu. An operator can list several (say, one per
    house system) if it helps them read their own audit log.
    """

    owned_by: str = "personacore"
    strict_model: bool = True
    """Refuse a ``model`` the core does not advertise, with OpenAI's own
    ``model_not_found``. On by default because answering a request for
    ``gpt-4o`` with a local model is a lie a client cannot detect. Operators
    with a hard-coded client can turn it off; the request is then answered by
    whatever this core actually runs, and the reply says so in ``model``."""

    max_history_messages: int = Field(default=40, ge=1)
    """How many earlier messages of a client-supplied conversation to keep.
    Everything from a client is untrusted input (section 7), and an unbounded
    history is a cheap way to exhaust the LLM host; the most recent messages
    are the ones that matter."""

    max_message_chars: int = Field(default=32_000, ge=256)
    """Per-message ceiling. Exceeding it is a plain 400, not a truncation — a
    silently shortened prompt produces a confidently wrong answer."""

    max_body_bytes: int = Field(default=1_000_000, ge=1024)
    """Request-body ceiling, checked before the body is read into memory."""


# ---------------------------------------------------------------------------
# Serialisation — what actually goes down the socket
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Emission:
    """One thing to put on the wire, in the order it happened.

    Streaming renders each of these as a frame; the blocking path ignores them
    and reads the folded state off :class:`_WireTurn` instead. One method
    producing both means the two surfaces cannot drift apart.
    """

    delta: ChunkDelta
    extension: TurnExtension | None = None


def _frame(chunk: ChatCompletionChunk) -> str:
    """One SSE frame: ``data: <json>`` and a blank line, per the EventSource
    format every OpenAI client is written against."""
    return f"data: {chunk.model_dump_json(exclude_unset=True)}\n\n"


def _emission_frame(
    completion_id: str, created: int, model: str, emission: _Emission
) -> str:
    """One non-terminal frame. ``exclude_unset`` in :func:`_frame` is what keeps
    an unset ``personacore`` — and every unset delta field — off the wire."""
    chunk = ChatCompletionChunk(
        id=completion_id,
        object="chat.completion.chunk",
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=emission.delta, finish_reason=None)],
    )
    if emission.extension is not None:
        chunk.personacore = emission.extension
    return _frame(chunk)


def _content_frame(completion_id: str, created: int, model: str, text: str) -> str:
    return _emission_frame(
        completion_id, created, model, _Emission(delta=ChunkDelta(content=text))
    )


def _arguments_json(arguments: Any) -> str:
    """A tool call's arguments as OpenAI puts them on the wire: a JSON-encoded
    *string*.

    The loop hands over the parsed dict, so this re-encodes it. Sending the
    object itself is the single most common way a compatible endpoint breaks a
    client, because every one of them calls ``json.loads`` on this field. A
    value that will not encode becomes ``{}`` rather than an exception: the
    turn already ran, and the frame describing it must not be what fails.
    """
    if not arguments:
        return "{}"
    try:
        return json.dumps(arguments, separators=(",", ":"))
    except (TypeError, ValueError):
        logger.warning("api_tool_arguments_unencodable")
        return "{}"


def _dump(payload: ChatCompletion) -> dict[str, Any]:
    """Serialise a completion, dropping the keys that must be absent rather
    than ``null``.

    ``logprobs`` and ``content`` are nulls a client expects to *find*, so a
    blanket ``exclude_none`` is wrong. Two keys are the opposite:

    * ``personacore``, on a turn that ran no tools.
    * ``message.tool_calls``, on every turn that handed nothing back. Absent,
      not ``null`` and above all not ``[]``: a client that branches on the
      field's presence — and Extended OpenAI Conversation branches on its
      truthiness — must see nothing there at all.
    """
    data = payload.model_dump(mode="json")
    if data.get("personacore") is None:
        data.pop("personacore", None)
    for choice in data.get("choices", []):
        message = choice.get("message")
        if isinstance(message, dict) and message.get("tool_calls") is None:
            message.pop("tool_calls", None)
    return data
