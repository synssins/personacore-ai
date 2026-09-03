# Policy Profiles

Every field of a policy profile, what enforces it and where, and the anonymous-tier limits the model refuses to hold. Read this before issuing an API key or enabling anonymous access.

A **policy profile** is what one caller is allowed to do. It travels with the caller's credential — an API key carries its profile, so authentication and authorisation are one lookup and nothing can authenticate a caller and then go looking for its permissions somewhere else.

Anonymous access is not separate machinery. It is this same model with the switches down. That was the point of ADR-0003: one policy engine, not two.

Source: `src/personacore/contracts/policy.py`.

## Kinds

| `kind` | Meaning |
|---|---|
| `user` | A household member, identified by login or speaker ID. |
| `api_key` | A machine client of the exposed OpenAI-compatible API. |
| `anonymous` | Unauthenticated access (ADR-0003). Off unless explicitly enabled, and subject to the hard ceilings below. |

## The fields

| Field | Type | Default | Enforced by |
|---|---|---|---|
| `id` | string | required | — identity, used for attribution in every audit and transcript record |
| `display_name` | string | required | — |
| `kind` | `user` \| `api_key` \| `anonymous` | required | The key store refuses to hold an `anonymous` profile; the issue endpoint refuses one with a `400`. |
| `enabled` | bool | `false` | The key store's `verify()` returns nothing for a disabled key (same `401` as unknown). The agent loop refuses a turn whose profile is disabled, before anything else happens. |
| `persona` | string or `null` | `null` | The agent loop, at composition. `null` means the system default. |
| `allowed_tools` | list of `"<plugin>.<tool>"` | `[]` | The agent loop's gate, step 2. Also filters what is offered to the model. |
| `max_tool_risk` | `safe` \| `confirm` \| `restricted` | `safe` | The agent loop's gate, step 3, and passed to the plugin host as the call's ceiling. |
| `may_approve_confirm` | bool | `false` | The agent loop's gate, step 4. |
| `memory_scope` | `none` \| `anonymous` \| `user` \| `household` | `none` | The agent loop, before the memory plugin is asked at all. |
| `memory_write` | bool | `false` | The memory plugin (P1). Not enforced in core today. |
| `may_enumerate_plugins` | bool | `false` | Not enforced in core today — see below. |
| `safe_mode` | bool | `false` | The agent loop: composes the safety block into the system prompt and clamps the risk ceiling to `safe`. |
| `raw_passthrough` | bool | `false` | The agent loop: skips persona, safety block, tools and memory entirely. |
| `scopes` | list of strings | `[]` | The admin JSON API's door (`AdminApiKeyDoor`). See below. |
| `rate_limit` | object | `{requests_per_minute: 60, max_concurrent: 4}` | **Nothing.** See below. |

Unknown fields are rejected outright (`extra="forbid"`).

### `allowed_tools` is an allowlist, and empty means none

Entries are fully qualified: `"weather.get_forecast"`, matching the flat namespace the plugin host publishes. An **allowlist rather than a blocklist**, so a newly installed plugin is unreachable by everyone until someone decides otherwise.

Empty means no tools at all. That is right everywhere except one place: the admin UI's chat box at `/admin/chat` grants the safe tools currently installed, because an admin trying the assistant would otherwise silently get no tools and conclude they were broken.

### `max_tool_risk` is a ceiling, applied *on top of* the allowlist

Both must pass. A tool in `allowed_tools` whose declared risk is above the ceiling is refused. See [Risk Levels](Risk-Levels).

### `may_approve_confirm`

Whether this profile can satisfy a confirmation prompt. Spec §8 defines a user's permissions as two things: which restricted tools they may invoke (the allowlist plus the ceiling) and which actions they may approve — this is the second.

Without it, a `confirm`- or `restricted`-risk tool is refused with *"…needs someone to approve it, and you're not set up to approve things."*, and the refusal is audited as a confirmation event.

### `memory_scope`

| Value | Meaning |
|---|---|
| `none` | The memory plugin is never even asked. |
| `anonymous` | The shared unauthenticated scratchpad. Isolated, never promoted, readable and writable by anyone unauthenticated. |
| `user` | This profile's own memories, plus household. |
| `household` | Shared household memory only. |

The scope check is in the core rather than the plugin, because scope is the caller's property. Memory itself is a P1 plugin and there is no implementation in core: with no provider wired in the assistant simply has no recall, which is honest.

### `safe_mode`

Child-safety controls (ADR-0005). Default **on** for the anonymous profile, off for authenticated adults. What the core implements today:

- A safety instruction block is composed into the system prompt **ahead of** the persona, and the persona cannot override it. See [Personas](Personas).
- The risk ceiling is clamped to `safe`, whatever the profile's own `max_tool_risk` says. Two independent limits, and the tighter one wins.

What ADR-0005 also describes but is **not yet built**: trimming the safe-tool allowlist further, forcing web search into its provider's strictest mode, and screening model output before it is spoken. The classifier is pluggable and is P1.

**This is best-effort and must not be described otherwise.** Content filtering driven by a local model is defeatable. The transcript log (ADR-0004) is the control that actually works, and the two were designed together deliberately.

### `raw_passthrough`

An opt-in per-key raw LLM proxy. No persona, no safety block, no tools, no memory: the conversation sent to the LLM is the client's history plus the user message and nothing else. Still audited — it is still the house's LLM doorway — and the audit detail records `raw_passthrough: true` on every request.

### `rate_limit`

Carried by every profile and **enforced nowhere**. `requests_per_minute` defaults to 60 (minimum 1) and `max_concurrent` to 4 (minimum 1). Enforcement belongs in front of every surface at once — the reverse proxy, or one shared limiter — rather than in one router that would cover the API and leave voice and the admin UI unprotected. Do not rely on this field today.

### `scopes` — named grants on surfaces that are not `/v1`

Exactly one scope exists: **`write:admin`**, which opens the admin JSON API
under `/admin/api`. Shaped `<verb>:<resource>` the way Gitea spells its token
scopes, with write implying read, so a later `read:trace` or `write:keys` is
another entry in the same list rather than a second mechanism.

**Empty by default, and that default is the security property.** The admin JSON
API used to accept anybody who was signed in, which meant a household member
could read `GET /admin/api/trace` — everybody's conversations — and issue
themselves keys to `/v1`. It now takes a key and only a key, and only one
carrying the scope. So a key issued for a display or a script reaches nothing
there, and no key that already existed gained anything.

A list rather than a boolean before there is a second scope to hold: growing a
list is additive, whereas turning a flag into a list later would be a migration
of the key table, of every issued key, and of the screen they are issued from.

A name the core does not know is **refused at construction**, not ignored: an
unknown scope grants nothing, which is safe and is exactly the problem — the
person issuing the key ticked something and was told it worked.

The scope is a checkbox on the Keys screen — *"May use the admin API"* — off
unless it is ticked. See [Admin API](Admin-API) and ADR-0041.

### `may_enumerate_plugins`

Whether the caller can discover what is installed. Required by ADR-0003 to be `false` for anonymous, and the model enforces *that*. There is currently no core code path that reads this field to decide whether to list plugins to a caller — the plugin listing lives on the admin API, which is behind admin authentication rather than a policy profile. It is a schema field waiting for the surface that needs it.

## Anonymous ceilings, refused at construction

The model **refuses to hold** a profile of kind `anonymous` that violates any of these. Not corrected, not warned about — a `ValueError` at construction.

| Rule | Message |
|---|---|
| `max_tool_risk` must be `safe` | *"the anonymous profile may only reach safe tools — '…' is not allowed"* |
| `may_approve_confirm` must be `false` | *"the anonymous profile cannot approve confirmations"* |
| `memory_scope` must not be `user` or `household` | *"the anonymous profile cannot reach household or per-user memory; use the anonymous scope, or none"* |
| `may_enumerate_plugins` must be `false` | *"the anonymous profile cannot enumerate installed plugins"* |
| `scopes` must be empty | *"the anonymous profile cannot carry a scope — scopes open the admin surfaces, and nobody vouched for an anonymous caller"* |
| With `safe_mode` on, `memory_scope` must be `none` | *"with safe_mode on, the anonymous profile must have memory scope none — the anonymous scope is shared and readable by anyone"* |

ADR-0003 is explicit that these must not be left to correct configuration: a misconfigured anonymous profile is exactly the failure this project cannot afford, so the model refuses to hold one.

The last rule is worth reading twice. The anonymous scratchpad is shared across every unauthenticated user — anyone on the LAN can write to it and everyone reads it — so with a child on the tier, safe mode and a non-empty anonymous scope are refused **in combination**. The mechanism matters: the setting is not silently switched off for you. A security control that changes state behind an admin's back is worse than one that makes the admin choose, so the model fails closed and the UI is required to explain the conflict and offer to change both.

## Where a profile comes from

| Caller | Profile source |
|---|---|
| `/v1` API client | The `ApiKeyRecord` attached to the presented bearer key. |
| Admin UI chat (`/admin/chat`) | Built per turn from the proxy-supplied user id: `kind=user`, `enabled=true`, the currently installed `safe` tools, ceiling `safe`, `may_approve_confirm=true`. |
| Development CLI (`personacore chat`) | Built from `--user`: `kind=user`, `enabled=true`, no tools, ceiling `safe`, `may_approve_confirm=true`. |
| Anonymous tier | Not yet wired to a surface. |

A turn with **no** profile at all is representable on purpose — an unknown speaker before speaker ID ships, a key revoked mid-conversation — so that it can be *refused*, rather than being impossible to express and therefore assumed benign. It is refused, audited as an access event, and the caller hears *"I can't answer that right now — this way of reaching me isn't set up to be used."*
