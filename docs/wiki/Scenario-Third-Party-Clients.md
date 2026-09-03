# Scenario: Pointing a Third-Party Client at the Core

You already use LobeChat, Open WebUI, Home Assistant's conversation agent, or a script that talks to an LLM, and you want it to go through PersonaCore instead of straight to your model.

## What changes for the client: one field

The core **is** an OpenAI-compatible server. It implements `/v1/chat/completions` and `/v1/models`, streaming included, so a standard client works unmodified.

In your client's settings, change the base URL from your model host to the core:

```
http://<your-host>:8053/v1
```

(Through your proxy, that will be `https://<your-host>/v1` — see [Scenario: Putting Authelia in Front](Scenario-Putting-Authelia-In-Front). TLS terminates at the proxy; the core's own listener is plain HTTP and should never be published directly.)

Paste the API key you are about to issue into the client's API-key field. That is the whole integration.

## What the client gains for free

Spec §5.4's promise: *"Other systems in the home that would normally point at the LLM directly point at the persona container instead, using documented standards, and get the persona — with its tools and memory — for free."*

Concretely:

- **A persona.** Answers come back in character, and the character is decided by the key's profile, not by the client.
- **A tool gateway.** Whatever plugins you have installed become available to that client, gated by risk. The client never learns what MCP is and holds no credential for anything a plugin talks to.
- **A policy layer.** The key decides which tools, what risk ceiling, which memory scope, whether it may approve a confirmation. A kitchen display and a workshop terminal get genuinely different powers from the same core.
- **An audit trail.** Every request and every tool call is recorded and attributed; every message in and out is stored as a transcript. See [Audit and Trace](Audit-And-Trace).

## Issue a key

From the admin surface, **Access keys** lives under the Security section of the sidebar (`/admin/keys`): it lists what has been issued, has a form to issue a new one, and a confirm-then-revoke control per row.

From a script, the same thing is `POST /admin/api/keys` and `DELETE /admin/api/keys/{key_id}`:

```bash
curl -sS -X POST http://127.0.0.1:8053/admin/api/keys -H "Remote-User: admin" -H "Content-Type: application/json" -d '{"note":"kitchen display","profile":{"id":"kitchen","display_name":"Kitchen display","kind":"user","enabled":true,"allowed_tools":["weather.get_forecast"],"max_tool_risk":"safe"}}'
```

**The key is shown exactly once, at the moment it is issued** — in that response, or on the admin screen's issue form. The core stores a SHA-256 of it and nothing else — it is not in the key listing, not in the audit record, not in any log line, and not recoverable by any endpoint. Losing it costs one re-issue; storing it recoverably would cost the household its front door.

Keys are prefixed `personacore_`. That is not a security feature: it exists so a key pasted into a bug report, a config file or a git diff is recognisable as a PersonaCore credential, by eye and by a secret scanner.

Present it as a bearer token, which is what every OpenAI client does with an API key field.

## Per-key policy

The key carries a whole [policy profile](Policy-Profiles). The fields that matter most when wiring up a client:

| Field | What it does |
|---|---|
| `enabled` | The on/off switch. A disabled key is refused at the door, not let in to be refused later. |
| `persona` | Which character answers. |
| `allowed_tools` | An **allowlist** of qualified tool names (`weather.get_forecast`). Empty means none — installing a plugin never widens what an existing key can do. |
| `max_tool_risk` | The ceiling. `safe` for anything you would call a display. |
| `may_approve_confirm` | Whether this client may approve a confirmation. Default false. |
| `memory_scope` / `memory_write` | Which memory pool, and whether it may write. P1. |
| `safe_mode` | Child-safety clamp — see [Scenario: Multi-User and Anonymous](Scenario-Multi-User-And-Anonymous). |
| `raw_passthrough` | Hands the caller the model directly, skipping the persona, the risk gate and tools. Off unless you deliberately turn it on. |
| `rate_limit` | Carried on every key, and **not enforced yet** — enforcing it belongs in front of every surface at once (the proxy, or one shared limiter), not in this router where it would cover the API and leave voice and the admin UI unprotected. |

**The key's profile decides, never the request body.** A client-supplied `system` message is dropped rather than honoured: "a dumb display widget should not be able to unlock a door" is only true if the widget cannot talk its way into a better profile.

Two more limits worth knowing, both because your client will hit them rather than you:

- **Conversation history is bounded** — the most recent 40 messages of a client-supplied conversation are kept. Everything from a client is untrusted input, and an unbounded history is a cheap way to exhaust the LLM host.
- **A message longer than 32,000 characters is a plain 400**, not a truncation. A silently shortened prompt produces a confidently wrong answer.

## Authentication behaviour

- **Authentication happens before anything else** — before body parsing, before schema validation, before the model list. An unauthenticated caller cannot use error messages to map the surface.
- **All four failure modes produce one byte-identical 401**: absent, malformed, unknown, and disabled. Anything that varied between them would be an oracle for probing which keys exist.
- The message is: *"Invalid API key. Ask whoever runs this assistant to issue you one."*
- **Errors wear OpenAI's envelope**, so an unmodified client renders them and a dead LLM host reads as a sentence rather than a traceback.

Revoking a key takes effect immediately — the store re-reads its file when it changes, so a key issued or revoked through the admin API is live without a restart.

## Choosing a model in the client

**Today, `/v1/models` advertises one entry: `personacore`.** That is a name for this core, not a menu — which *backend* model runs is configuration, and which persona answers is the key's profile. Requests naming a model the core does not advertise are refused with OpenAI's own `model_not_found`, because answering a request for `gpt-4o` with a local model is a lie a client cannot detect.

If you have a client with a hard-coded model name you cannot change, strict model checking can be turned off; the request is then answered by whatever this core actually runs, and the reply says so in its `model` field.

## Decided but not built

Two decisions are recorded and will change this page when they land. Neither works today.

### Persona by model name — [ADR-0017](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0017-persona-selection-and-multi-client.md)

`/v1/models` will advertise **one entry per installed persona** — `glados`, `jarvis`, `butler` — and a request naming one will get that persona.

This is the primary persona-selection mechanism because it uses a control every OpenAI client already has and already shows the user: the model dropdown. Adding a persona becomes a folder in appdata that appears in every client's dropdown, with no new surface on the core's side.

Where both are present, the explicit model name wins for **character** while the key still governs what that character may **do** — character and authority are different questions and are not forced through one control. A port-per-persona arrangement is kept as a fallback for clients that can set neither, and was rejected as the primary because it costs infrastructure per persona, because ports collide, and because it would make adding a character a deployment change.

The current `personacore` id will remain and will mean "whatever the default persona is", so a client pinned to it keeps working.

### Optional API keys — [ADR-0018](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0018-optional-api-keys.md)

Keys will become optional, the way a local Ollama endpoint is. **Today they are required**: spec §5.4 says no anonymous access even on the LAN, and that is what the code does.

When it lands, the shape is already decided:

- A keyless request **is the anonymous profile** — not a bypass, not a special case — so "keyless" cannot mean "unlimited", because the profile model refuses to hold a profile that says so.
- **The default is no tools at all: conversation only.** `allowed_tools` is an allowlist and empty means none, so nothing becomes reachable merely because it is `safe`. Opting a tool back in is deliberate and per tool.
- **Off by default**, enabled in the admin UI, reported by `/health` the way the admin bypass is, and optionally restricted by source address.
- **Keys keep working when keyless is on.** Keyless is a floor, not a replacement.

Until then, issue a key for every client — including ones on your own LAN.

## Related

- [OpenAI-Compatible API](OpenAI-Compatible-API) — the reference page: request shape, streaming, errors.
- [Policy Profiles](Policy-Profiles) — every field.
- [Personas](Personas) — creating and switching characters.
- [Scenario: Multi-User and Anonymous](Scenario-Multi-User-And-Anonymous) — the tier a keyless caller will land in.
