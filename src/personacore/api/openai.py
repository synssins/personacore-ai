"""The exposed OpenAI-compatible API — spec section 5.4 (inbound).

Section 5.4's promise: "Other systems in the home that would normally point at
the LLM directly point at the persona container instead, using documented
standards, and get the persona — with its tools and memory — for free." So this
module is a *translation layer* and nothing else. It speaks OpenAI's wire format
on one side and :class:`personacore.agent.loop.AgentLoop` on the other, and it
never reaches past the loop to the LLM client: doing so would hand a caller the
raw model while skipping the persona, the risk gate and the audit trail, which
is precisely the hole the per-key ``raw_passthrough`` switch exists to open
deliberately and only when an admin says so.

Five rules shape everything here:

1. **Authenticate before anything else happens** (section 5.4: "No anonymous
   access, even on the LAN"). Nothing — not body parsing, not schema
   validation, not the model list — runs before the key is verified, so an
   unauthenticated caller cannot use error messages to map the surface. All
   four failure modes (absent, malformed, unknown, disabled) produce one
   byte-identical 401.
2. **The key's profile decides, never the request body.** Persona, tools,
   memory scope, risk ceiling and permissions come from
   :class:`personacore.contracts.policy.PolicyProfile`, and nothing in the body
   can reach any of them. "A dumb display widget should not be able to unlock a
   door" is a statement about the profile, and the profile is untouchable here.

   A client-supplied ``system`` or ``developer`` message is therefore **kept**,
   as the caller's description of its own world. It used to be dropped, on the
   reasoning that a system prompt is a persona override — which guarded the
   right thing in the wrong place. Home Assistant puts its list of exposed
   entities in that message, so dropping it left the model asking the operator
   for the ``entity_id``s of the exposed devices and inventing names for them.

   What it cannot do is win. The text goes in fenced and labelled as the
   caller's (ADR-0003, the same treatment tool output gets), and the persona's
   prompt is placed **after** it, because later instructions carry more weight
   with a model. No attempt is made to separate the caller's data from its
   instructions — that blob cannot be parsed reliably, so the defence is
   position and framing, never filtering. See ADR-0035 decision 8.
3. **Errors wear OpenAI's envelope** so that an unmodified client renders them
   (section 5.4) and a dead LLM host reads as a sentence, not a traceback
   (section 10). FastAPI's own ``{"detail": ...}`` shape never escapes: this
   module returns responses rather than raising ``HTTPException``, because the
   app object is assembled elsewhere and a router cannot install exception
   handlers on it.
4. **A tool call on the wire is an instruction, so only the caller's own tools
   go there.** The caller sends ``tools``; anything the model calls from that
   list comes back as ``tool_calls`` with ``finish_reason: "tool_calls"``,
   which is what the standard means and what the caller can actually carry
   out. Container tools — this core's plugins — run inside and are reported
   only under the ``personacore`` key, where nothing dispatches. Same name in
   both lists: the caller's wins. Neither: the model is told the tool does not
   exist. See :class:`TurnExtension` and ADR-0035.
5. **Every request is audited** with the profile attributed and
   ``Surface.API`` (section 7). Message content is not put in the audit detail
   — the agent loop writes the transcript records ADR-0004 asks for, and
   duplicating content into the audit log would put the most privacy-sensitive
   data in two stores with one retention policy between them.

Deliberately *not* here: rate limiting. ``PolicyProfile.rate_limit`` is carried
by every key, but enforcing it belongs in front of every surface at once (the
reverse proxy of section 7, or one shared limiter), not in this router where it
would cover the API and leave voice and the admin UI unprotected.

**This module is now the seam list rather than the implementation** (ADR-0040).
Each piece below is a file somebody can open and read whole, and a change to one
of them is a change nobody has to read the other seven to make. Nothing here is
optional: the whole surface is the front door, so a piece that will not import
takes the door with it — the split buys a smaller blast radius to reason about,
not a degradable one.

* :mod:`personacore.api.openai_wire` — OpenAI's shapes and the bytes they
  serialise to, the ``personacore`` extension key included.
* :mod:`personacore.api.openai_caller` — who is calling, and the one method
  this surface needs from the agent loop.
* :mod:`personacore.api.openai_router` — the routes and the key check in front
  of them.
* :mod:`personacore.api.openai_translate` — an OpenAI request in, a
  ``TurnRequest`` out.
* :mod:`personacore.api.openai_turn` — the agent's events folded into OpenAI's
  shapes. Rule 4 above is a statement about this file.
* :mod:`personacore.api.openai_blocking` and
  :mod:`personacore.api.openai_streaming` — the two ways a turn is answered.
* :mod:`personacore.api.openai_errors` — the envelope, the audit record and the
  token estimate.

Everything importable from here before the split is still importable from here,
because callers should not have to know which of those files a name landed in.
"""

from __future__ import annotations

from personacore.api.openai_caller import Caller, KeylessCaller, TurnRunner
from personacore.api.openai_router import create_openai_router
from personacore.api.openai_wire import (
    DEFAULT_MODEL_ID,
    SSE_DONE,
    ApiError,
    ApiErrorDetail,
    ChatCompletion,
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatMessageIn,
    ChatMessageOut,
    ChunkChoice,
    ChunkDelta,
    DeltaFunctionCall,
    DeltaToolCall,
    FinishReason,
    ModelCard,
    ModelList,
    OpenAIApiConfig,
    ResponseFunctionCall,
    ResponseToolCall,
    ToolInvocation,
    ToolOutcome,
    TurnExtension,
    Usage,
)

__all__ = [
    "DEFAULT_MODEL_ID",
    "SSE_DONE",
    "ApiError",
    "ApiErrorDetail",
    "Caller",
    "ChatCompletion",
    "ChatCompletionChoice",
    "ChatCompletionChunk",
    "ChatCompletionRequest",
    "ChatMessageIn",
    "ChatMessageOut",
    "ChunkChoice",
    "ChunkDelta",
    "DeltaFunctionCall",
    "DeltaToolCall",
    "FinishReason",
    "KeylessCaller",
    "ModelCard",
    "ModelList",
    "OpenAIApiConfig",
    "ResponseFunctionCall",
    "ResponseToolCall",
    "ToolInvocation",
    "ToolOutcome",
    "TurnExtension",
    "TurnRunner",
    "Usage",
    "create_openai_router",
]
