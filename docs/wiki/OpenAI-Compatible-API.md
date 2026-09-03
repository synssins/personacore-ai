# OpenAI-Compatible API

What PersonaCore implements at `/v1`, what a standard OpenAI client gets from it, and where the compatibility honestly stops. Read this if you are pointing a third-party client, a Home Assistant conversation agent or a script at the assistant.

The promise (spec §5.4): other systems in the home that would normally point at the LLM directly point at PersonaCore instead, using documented standards, and get the persona — with its tools and memory — for free.

This is a **translation layer** and nothing else. It speaks OpenAI's wire format on one side and the agent loop on the other. It never reaches past the loop to the LLM client, because doing so would hand a caller the raw model while skipping the persona, the risk gate and the audit trail — which is exactly the hole the per-key `raw_passthrough` switch exists to open deliberately.

Source: `src/personacore/api/openai.py` — which is now the surface's rules and a list of the `openai_*` modules it is assembled from (wire shapes, router and auth, request translation, the event fold, the two turn paths, errors) — and `src/personacore/api/keys.py`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/models` | What this core offers. |
| `POST` | `/v1/chat/completions` | One turn of conversation, streaming or not. |

That is the whole surface. There is no `/v1/completions`, no embeddings, no audio, no images.

## Authentication

`Authorization: Bearer <key>` on every request, including `/v1/models`. No anonymous access, even on the LAN.

**Authentication happens before anything else** — before body parsing, before schema validation, before the model list. A declared FastAPI body model would be validated before the endpoint ran, which would let an unauthenticated caller read `422`s describing the schema, so the body is parsed by hand instead.

All four failure modes — header absent, wrong scheme, unknown key, disabled key — produce one byte-identical `401`:

```json
{"error": {"message": "Invalid API key. Ask whoever runs this assistant to issue you one.",
           "type": "invalid_request_error", "param": null, "code": "invalid_api_key"}}
```

with `WWW-Authenticate: Bearer`. Anything that varied between them would be an oracle for probing which keys exist. Which one it actually was is logged and audited on the server side.

Keys are issued and revoked through the admin API (`POST`/`DELETE /admin/api/keys`, see [Admin API](Admin-API)).

## How keys are stored

In `<appdata>/users/api-keys.json`, written atomically and `0o600`.

- **Only a SHA-256 of the key is stored.** A stolen appdata backup must not hand the thief a working key.
- A password KDF (bcrypt, argon2) is deliberately not used and would not help: a key is 256 bits of `secrets.token_urlsafe` entropy, so there is no dictionary to run against it, and a slow KDF on the hot path of every request would spend the latency budget for nothing. That reasoning holds only because nothing but this store mints keys.
- Issued keys are prefixed `personacore_`. Not a security feature — it exists so a key pasted into a bug report or a git diff is recognisable as a PersonaCore credential, by a secret scanner and by eye.
- Verification is constant-time and **does not stop early**: every record is compared, so neither the comparison nor the number of records examined varies with how close a guess was.
- A key can never carry an `anonymous` profile. The record model refuses it.
- The file is re-read whenever its mtime or size changes, so a key issued through the admin API works on the very next request with no restart.
- A malformed key file fails **closed**: no keys, every request `401`, loudly in the operator's log. One unparsable row does not disable the others, but it is logged rather than silently skipped.

## The key's profile decides, never the request body

Persona, tools, memory scope and risk ceiling all come from the `PolicyProfile` attached to the key (see [Policy Profiles](Policy-Profiles)). Concretely:

- **`system` and `developer` messages are dropped**, not honoured and not rejected. A client-supplied system prompt is a persona override, and persona selection belongs to the key. Dropping beats rejecting because standard clients ship a default system prompt and would otherwise fail on every call — and dropping is the fail-safe direction.
- **`tool` messages are dropped.** Tools run inside the core, gated by the profile. A client cannot hand in a tool result any more than it can hand in a persona.
- **`user` is ignored for attribution.** Who the caller is comes from the key, not from a field the caller fills in.
- **Sampling parameters are accepted and ignored** — `temperature`, `top_p`, `max_tokens` and friends. Model behaviour is configuration, so an untrusted client does not get to retune the household assistant by sending `temperature: 2`. They are accepted rather than rejected so a standard client does not fail.

Unknown fields on the wire models are ignored rather than refused, which is a deliberate departure from the rest of the codebase: standard clients send `name`, `tool_call_id`, `refusal`, `annotations` and whatever the API grew last month, and rejecting them would break the compatibility promise.

## `GET /v1/models`

Returns OpenAI's `{"object": "list", "data": [{"id": ..., "object": "model", "created": ..., "owned_by": "personacore"}]}`.

The default catalogue is a single id, `personacore`. The core serves one *logical* model — the assistant — because which backend model runs is configuration and which persona answers is the key's profile. Neither is a client's decision, so these ids are names for this core, not a menu. An operator can configure several ids if it helps them read their own audit log.

`created` is fixed at assembly time rather than per request, since a model's `created` is supposed to be a property of the model.

> **Planned, not built:** ADR-0017 proposes advertising one entry per installed persona here, so a client's model dropdown becomes the persona picker. The `personacore` id would stay and mean "whatever the default persona is". Today the catalogue is whatever the router was configured with.

## `POST /v1/chat/completions`

### Request

Standard OpenAI chat-completion body. What is actually read: `messages` (required, at least one), `model`, `stream`, `stream_options.include_usage`.

Only `user` and `assistant` messages survive. The **last `user` message** is the turn; everything before it becomes history.

| Limit | Default | Behaviour when exceeded |
|---|---|---|
| Request body | 1,000,000 bytes | `413` (`request_too_large`). Checked against `Content-Length` first, and again after reading, because a chunked request has no declared length. |
| Per message | 32,000 characters | `400` (`context_length_exceeded`) for the user message. A silently shortened prompt produces a confidently wrong answer, so this is a refusal rather than a truncation — but *history* messages are truncated to the same limit rather than refused. |
| History depth | 40 messages | Older messages are dropped. Everything from a client is untrusted, and an unbounded history is a cheap way to exhaust the LLM host. |

By default the core refuses a `model` it does not advertise, with OpenAI's own `model_not_found` (`404`), because answering a request for `gpt-4o` with a local model is a lie the client cannot detect. Operators with a hard-coded client can turn strict matching off; the request is then answered by whatever this core runs, and the reply's `model` field says so.

An unparsable body is a `400` naming what was expected. The pydantic detail is not echoed back — it quotes the offending input, and the input is untrusted.

### Non-streaming response

A normal `chat.completion` object: `id`, `object`, `created`, `model`, `choices[0].message.content`, `finish_reason: "stop"`, `usage`.

The `id` is `chatcmpl-<correlation_id>` and the correlation id is the same one the trace view groups by, so a user quoting the id from their client lands an operator directly on that turn. It is a random uuid4 and says nothing about the caller.

### Streaming response

`text/event-stream`, `data: {...}` frames, terminated by `data: [DONE]`. Headers include `Cache-Control: no-cache` and `X-Accel-Buffering: no` — the latter matters, because the latency budget survives only if the reverse proxy forwards each frame as it arrives rather than buffering the response.

Frame order: one frame with `delta.role = "assistant"`, then content frames, then a frame with an empty delta and `finish_reason: "stop"`, then (if `stream_options.include_usage` was set) a usage-only frame with an empty `choices` array, then `[DONE]`.

Deltas are serialised with unset keys omitted — `{"content": "..."}`, not `{"role": null, "content": "..."}` — which is what real OpenAI frames look like and what strict client parsers expect.

The first events are pulled **before** the response starts, so a turn that fails at the first hurdle still gets a real `503` instead of a `200` whose body turns out to be an apology. Once any assistant text exists the status is committed and the buffered events are replayed ahead of the live ones, so nothing is lost or reordered.

If the stream breaks after the headers have gone out, there is no status left to change: the core appends *" — sorry, I lost my train of thought there and couldn't finish."*, sends a `stop` frame and `[DONE]`, and closes. A client that never sees `[DONE]` hangs until it times out, which is a worse failure than a visible apology.

### What agent events become on the wire

| Agent event | Wire |
|---|---|
| text delta | a `content` delta, straight through, unbuffered |
| notice | also a `content` delta — a notice is a sentence the assistant would say out loud, so on a text surface it is part of the reply |
| tool call, tool result, refusal | **nothing** |
| done | `finish_reason: "stop"`, and `[DONE]` when streaming |

Tool activity is deliberately invisible on the wire. OpenAI's `tool_calls` field means "the client should run this tool"; here the core already ran it, so emitting them would invite a client to run it again. A refusal is fed back to the model, which states it in the persona's own words, so echoing the raw reason would say it twice. All three are in the audit log and the trace view, which is where they belong. See [Audit and Trace](Audit-And-Trace).

### Errors

All errors wear OpenAI's envelope (`{"error": {"message", "type", "param", "code"}}`) so an unmodified client renders them. FastAPI's own `{"detail": ...}` shape never escapes.

| Status | When |
|---|---|
| `400` | Unparsable body, no user message, empty last user message, message too long. |
| `401` | Any authentication failure. |
| `404` | `model_not_found` under strict matching. |
| `413` | Body over the size limit. |
| `500` | Unexpected failure. Deliberately says nothing about what broke. |
| `503` | The turn produced no assistant text at all and ended on a notice — LLM host unreachable, persona missing, circuit breaker open. The body carries the agent's own spoken sentence verbatim. |

The runaway-tool stop is **excluded** from `503`: if the model kept reaching for tools and never answered, the assistant did answer — just not usefully — so that is a `200` whose content explains what happened.

## Per-key policy

Everything the key's profile controls is described in [Policy Profiles](Policy-Profiles). The two that most change what this surface does:

- **`allowed_tools` + `max_tool_risk`** decide which tools are even offered to the model on this turn, and every call is gated again at invocation. See [Risk Levels](Risk-Levels).
- **`persona`** decides which character answers. `null` means the system default.

## Raw passthrough

`raw_passthrough` is a per-key switch. With it on, the turn skips the persona, the safety block, the tools and memory entirely: the conversation sent to the LLM is the client's history plus the user message, nothing more. The caller asked for the model, not the assistant.

It is still audited — it is still the house's LLM doorway — and the audit detail records `raw_passthrough: true` on every request so a passthrough key's traffic is distinguishable in the trace view.

## Honest limits

- **`usage` counts are an estimate, not an accounting record.** The core never sees real token counts: the LLM stays behind a streaming interface and the backend that could count is a different machine. The numbers are `(len(text) + 3) // 4` — the usual four-characters-per-token English approximation — over the concatenated prompt text and the generated text. The field cannot be omitted because several clients refuse a response without it. It is good enough for "roughly how much did that cost". **Nothing should bill from it.**
- **No rate limiting on this router.** Every key carries a `rate_limit`, and nothing here enforces it. Enforcement belongs in front of every surface at once — the reverse proxy, or one shared limiter — not in a router that would cover the API and leave voice and the admin UI unprotected.
- **No tool-calling on the wire.** A client cannot ask the assistant to expose its tools, and cannot supply tool results.
- **No multimodal input.** A `content` array is accepted and its text parts are used; non-text parts are dropped rather than refused, because a client that always sends the array form should still get an answer. Vision is a plugin's job, not the core's.
- **TLS is not terminated here.** The listener is plain HTTP behind the proxy. See [Security Model](Security-Model).

## Pointing a client at it

Set the client's base URL to the proxy's address plus `/v1`, and the API key to one issued from the admin API. In OpenAI SDK terms:

- base URL: `https://assistant.example/v1`
- API key: the `personacore_…` value shown once at issue
- model: `personacore`, or whatever `GET /v1/models` lists

Any client that lets you set those three works unmodified: LobeChat, Open WebUI, Home Assistant's conversation agent, the `openai` Python and Node SDKs, or a `curl` one-liner. If the client insists on sending a system prompt or sampling parameters, that is fine — they are dropped or ignored rather than refused.
