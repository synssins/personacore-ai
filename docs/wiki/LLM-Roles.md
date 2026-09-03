# LLM Roles

The five LLM roles, what each is for, how the fallback works, and why distinct endpoints get distinct circuit breakers. Read this if you are configuring where the assistant's thinking happens.

The core holds a **set of named LLM endpoints, not one endpoint** (ADR-0011). Callers ask for a role, never a URL; nothing in the core hardcodes an address.

Source: `src/personacore/config/settings.py`, `src/personacore/server.py` (`LLMRoster`, `LiveLLM`).

## The five roles

| Role | What it is for | Why it differs |
|---|---|---|
| `interactive` | Conversation with a person. | The big model. Quality matters, latency is felt. |
| `autonomy` | Background subagents. | Runs unattended and often. A small fast model is the right tool. |
| `triage` | Disambiguation and classification. | Short, structured, high volume. Wants the cheapest model that is accurate. |
| `vision` | Scene description. | Needs a vision-capable model, which the others are not. |
| `commands` | Command interpretation. | Latency-critical; a wrong model here is felt immediately. |

This is not a preference, it is how the workload decomposes. Sending a doorbell classification to a 35B model wastes the machine; sending a conversation to a 4B model is felt by whoever is speaking. Vision genuinely cannot be served by a text-only model at all.

**The set is closed in this version.** An open-ended set would put a capability's name into config and invite the core to learn about specific plugins, which spec §13.5 forbids. Adding a role is a contract minor version. A role name the core does not recognise is refused with a message listing the valid ones, rather than failing with an attribute error three frames later.

## The fallback to `interactive`

**Only `interactive` is required.** Every other role falls back to it when unset, so a single-endpoint setup stays a single-endpoint setup with nothing extra to configure.

The fallback is implemented in exactly one method (`LLMRoles.resolve`), and nothing else in the codebase is allowed to reproduce it. Callers never write "or interactive" themselves — that is how a fallback becomes inconsistent between the health dashboard and the client that actually made the request.

Three related questions the config model answers, so the UI and the dashboard agree:

- **Is this role configured?** Does it have a section of its own, rather than borrowing one.
- **What does it fall back to?** `interactive`, or nothing if it is configured.
- **Which roles are configured?** `interactive` always first.

The admin UI and the health dashboard both use these so they can say *"falling back"* rather than implying five mandatory setups.

## Configuration

Per-role sections in `core.toml`. Every field, default and range is on [Core Settings](Core-Settings).

```toml
[llm.interactive]
base_url = "http://llm-host:11434/v1"
model = "llama3.1:8b"
# api_key_secret = "llm_key"
connect_timeout_seconds = 5.0
read_timeout_seconds = 120.0

# Add a section only where a different model is genuinely the right tool.
[llm.vision]
base_url = "http://vision-host:8080/v1"
model = "some-vision-model"
```

`api_key_secret` names a secret in `<appdata>/secrets/core/`; it never holds the key. The Models screen has a key box per role (ADR-0038): typing a key stores it under `llm_<role>_key` and writes the name here. One name per role, so pointing one role at another provider cannot overwrite another role's key.

A pasted endpoint URL is trimmed to its root, so `.../v1`, `.../v1/`, `.../v1/chat/completions`, `.../v1/completions` and `.../v1/models` all resolve to the same thing. Trimming never *adds* a `/v1` — the version prefix is yours to supply.

### Legacy flat `[llm]`

A `core.toml` written before roles existed, whose `[llm]` table holds `base_url`/`model` directly, is still read and means "interactive only". Spec §7's rule that an upgrade never touches appdata means that file has to keep working, so the shape is detected at the boundary rather than migrated on disk — a flat table *is* an interactive-only configuration, and saying so once keeps every reader downstream ignorant of the old shape.

The first save from the admin UI writes the file back in `[llm.interactive]` form.

A document that **mixes** both shapes is refused rather than guessed at, with a message telling you to move the flat settings into `[llm.interactive]`. Detection is by key name and cannot collide: no role name is also an endpoint-settings field name.

## One client per distinct endpoint, one handle per role

Two rules, held together by the roster:

**A role that falls back shares its client.** Opening a second connection pool to the same host, with a second circuit breaker that can disagree with the first about whether that host is up, would be a bug dressed as isolation.

**Distinct endpoints get distinct breakers.** A dead vision host must not open the breaker conversation depends on. One failing endpoint degrades one capability (spec §10), and since the breaker lives inside the client, "distinct breakers" and "distinct clients" are the same sentence.

**Sameness is the whole resolved settings value**, not just the base URL — model name, timeouts and breaker thresholds are all part of what a client *is*. Two roles pointed at one host with different models are two endpoints and get two breakers.

The identity of an endpoint is a 12-character SHA-256 digest of that settings value. It is a digest rather than the value itself so the dashboard can show "these two roles share an endpoint" — and therefore share a breaker — without ever publishing the address. A hand-edited `core.toml` can carry credentials in a base URL (`http://user:secret@host`), and the dashboard is rendered, logged and screenshotted.

## Applying a change without a restart

ADR-0010 puts the LLM host in the admin UI, and a setting that needs a container restart has only moved the friction. So the agent loop and the health dashboard are handed a stable *handle* per role, once; saving new settings rebinds the client inside the handle and they never learn that it changed.

Saving re-resolves every role and swaps only what changed. A role whose endpoint is untouched keeps the exact client object it had, so its pooled connections stay open and its breaker keeps whatever state it had learned. Clients that are no longer used by any role are closed **after** every handle has been rebound, so an in-flight turn finishes against the client it started with rather than losing its connection mid-answer.

## Which caller uses which role

| Caller | Role |
|---|---|
| The agent loop (all conversation, on every surface) | `interactive` |
| Development CLI `personacore chat` | `interactive` |

That is currently the whole list. ADR-0011 deliberately defers **which** role each internal caller uses beyond `interactive` to the moment that caller exists: memory consolidation (P1) is the obvious `autonomy` consumer and the vision plugin (P3) the obvious `vision` one, but guessing now would be designing for features that are not built.

So today, configuring `[llm.vision]` gives you a fifth endpoint that is health-checked and shown on the dashboard, and nothing in the core sends it work yet.

## Health per role

"The LLM is up" is no longer a single fact, so the dashboard reports one row per role: `llm.interactive`, `llm.autonomy`, `llm.triage`, `llm.vision`, `llm.commands`.

Roles sharing an endpoint are **probed once and reported twice** — five requests to the same host every time someone loads the dashboard would be a bug the dashboard caused. Each row carries the model name, the endpoint digest, the breaker state, and `falls_back_to` when the role is borrowing another's configuration.

A source that predates roles (a bare client, as the CLI builds) still reports the single `llm` row it always did.

See [Health and Diagnostics](Health-And-Diagnostics) for what each state means.

## The circuit breaker

Per client, so per distinct endpoint. Thresholds come from that endpoint's `failure_threshold` (default 5 consecutive failures) and `cooldown_seconds` (default 30).

```
closed  --(N consecutive failures)-->  open
open    --(cooldown elapses, next call)-->  half_open  (one probe let through)
half_open --(probe succeeds)-->  closed
half_open --(probe fails)-->     open  (cooldown restarts)
```

While the breaker is open, calls fail immediately with a spoken sentence rather than waiting out a timeout. The health probe is a `GET /models` against the endpoint — the cheapest call that proves the host is answering — and it never raises: a dead or misbehaving host is reported as unhealthy with a plain-English detail.
