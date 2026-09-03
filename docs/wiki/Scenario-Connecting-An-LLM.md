# Scenario: Connecting an LLM

You want the assistant to actually answer, which means pointing it at an OpenAI-compatible model server you already run.

The core talks to your LLM host **only** through the OpenAI-compatible API — no vendor SDK, no custom protocol. Swapping llama.cpp for Ollama for vLLM is a settings change with zero code changes (spec §5.3), and the LLM host itself is never modified by this project (spec §14).

## What you need

- The base URL of your model server, e.g. `http://<your-host>:11434/v1`.
- A model id that server actually serves.
- An API key, only if your server requires one. Most self-hosted ones do not.

## 1. Open the connection panel

In the admin surface, the connection panel shows **one form per role** rather than a single pair of fields. Fill in `interactive` first — it is the only required one.

If you prefer the file, it is `<appdata>/config/core.toml`, and it is the same document the UI reads and writes. Hand-editing is a legitimate recovery path, not the intended one ([ADR-0010](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0010-runtime-config-lives-in-the-admin-ui.md)). Field-by-field reference: [Core Settings](Core-Settings).

## 2. A pasted endpoint URL is trimmed — deliberately

Every comparable settings screen shows the full `.../v1/chat/completions`, so that is what people paste. The client appends its own paths, so storing what you pasted would produce `.../chat/completions/chat/completions` and a failure with no obvious cause.

So the value is **trimmed to its root before it is stored**. All of these end up as the same thing:

| What you paste | What is stored |
|---|---|
| `http://<your-host>:11434/v1` | `http://<your-host>:11434/v1` |
| `http://<your-host>:11434/v1/` | `http://<your-host>:11434/v1` |
| `http://<your-host>:11434/v1/chat/completions` | `http://<your-host>:11434/v1` |
| `http://<your-host>:11434/v1/completions` | `http://<your-host>:11434/v1` |
| `http://<your-host>:11434/v1/models` | `http://<your-host>:11434/v1` |

Surrounding whitespace is stripped too. That last one matters more than it sounds: a pasted URL was once stored untrusted and untrimmed, so the file and the screen showed a different value from the one in use.

**Note that the `/v1` itself is kept.** The trimming removes known endpoint suffixes, not the API version prefix — the client builds `{base_url}/chat/completions` and `{base_url}/models`, so a base URL with no `/v1` will produce 404s against a server that expects one.

**A container's `localhost` is the container.** The starter config ships `http://localhost:11434/v1` as an obvious placeholder and says so in a comment. Use your host's address, or the service name if the model server is in the same Compose stack.

## 3. If your server needs a key

**Type it into the API key box on the Models screen**, in the panel for the role it belongs to. The key goes into the secret store on the appdata volume and `core.toml` gets only the **name** of that secret, in `api_key_secret` — a config file gets read, copied into backups and pasted into support threads, so it never holds the value itself (ADR-0038).

Three things the box does that are worth knowing before you use it:

- **A stored key is never shown back to you.** The screen says *a key is set*, and that is all it can say. Nothing on the page, in a log line or in an audit record carries the value.
- **Leaving the box empty keeps the key you have.** Changing the model name and pressing Save does not unset your credential. Removing one has its own control beside the box.
- **Each connection has its own key.** Each role stores under `llm_<role>_key` and each persona under `persona_<slug>_key`, so giving Triage a different provider's key cannot overwrite the conversation model's.

**A key replaced under a name that connection already used needs a restart to take effect** — setting one for the first time and removing one both apply immediately, because both change the connection itself.

If you would rather do it by hand, nothing changed: create the file and name it.

```bash
printf '%s' 'PASTE-THE-KEY-HERE' > /srv/personacore/appdata/secrets/core/llm_key
```

Then set `api_key_secret = "llm_key"`. A name nothing answers to is refused on save, with a message telling you to add the secret first. The box keeps a name you chose yourself rather than renaming your secret underneath you.

The write path also actively refuses a `core.toml` containing a credential-shaped key such as `api_key` or `access_token`, telling you to use the `_secret` field instead. See [Security Model](Security-Model).

## 4. Test the connection

Use the **test connection** action, choosing the role. It asks that role's client whether the host answers — literally, it calls `/v1/models` — and reports in words: on success, **reachable**, with how long the host took to answer and, when the host says so, which model it reports serving; on failure, **not reachable**, with the reason in a plain sentence. Nothing is audited, because a probe changes nothing and filling the trace view with probes would bury the records that matter.

The probe never raises. A dead host is an unhealthy status with a readable detail, not a traceback.

Common causes when it fails, in the order they actually happen:

| Symptom | Cause |
|---|---|
| Connection refused, and the URL says `localhost` | You are inside a container. Use the host's address. |
| Connection refused from another machine | Your model server is bound to loopback on its own host. |
| 404 | The base URL is missing its `/v1`, or has a path the server does not serve. |
| 401 | The server wants a key. See step 3. |
| It answers, but the chat turn fails with a model error | The `model` id is not one that server serves. |

## 5. Save, and watch it apply live

Settings apply without a restart. Saving re-resolves every role and swaps only what changed: a role whose endpoint is untouched keeps its existing client, its pooled connections and whatever its circuit breaker had learned. Retired clients are closed only *after* every role has been rebound, so a turn in flight finishes against the client it started with rather than losing its connection mid-answer.

If a setting genuinely could not be applied live, the UI must say so rather than appearing to have worked. A setting that needs a restart to take effect has only moved the friction.

## Using more than one role

Five roles exist, and they exist because the workload actually decomposes that way ([ADR-0011](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0011-llm-roles.md)):

| Role | What it is for | Why it wants a different model |
|---|---|---|
| `interactive` | Conversation with a person | The big model. Quality matters, latency is felt. |
| `autonomy` | Background subagents | Runs unattended and often. Small and fast is the right tool. |
| `triage` | Disambiguation and classification | Short, structured, high volume. The cheapest model that is accurate. |
| `vision` | Scene description | Needs a vision-capable model, which the others are not. |
| `commands` | Command interpretation | Latency-critical; a wrong model here is felt immediately. |

Rules worth knowing before you fill any of them in:

- **`interactive` is required. Every other role is optional and falls back to it.** A single-endpoint setup stays a single-endpoint setup and nothing needs configuring to start. Sending a doorbell classification to a 35B model wastes the machine; sending a conversation to a 4B model is felt by whoever is speaking.
- **Each distinct endpoint gets its own client and its own circuit breaker.** A dead vision host must not open the breaker that conversation depends on — one failing endpoint degrades one capability (spec §10).
- **Roles that resolve to the same settings share one client**, so a fallback does not open a second connection pool to the same host with a second breaker that could disagree with the first about whether it is up.
- **"Same" means the whole resolved settings value**, not just the base URL. Two roles pointed at one host with different model names are two endpoints and get two breakers.
- **The role set is closed.** Adding one is a contract minor version, deliberately: an open-ended set would put a capability's name into config and invite the core to learn about specific plugins, which spec §13.5 forbids.

Which internal caller uses which role beyond `interactive` is decided when that caller exists. Today the agent loop — that is, all conversation — asks for `interactive`. Memory consolidation is the obvious future `autonomy` consumer and the vision plugin the obvious `vision` one; neither is built.

Health is reported **per role**, because "the LLM is up" stopped being a single fact. See [Health and Diagnostics](Health-And-Diagnostics).

Callers ask for a role, never a URL. Nothing in the core hardcodes an address.

## If you already have a flat `[llm]` section

An older `core.toml` with a single flat `[llm]` table still loads and resolves as `interactive`, so a deployment that already had one keeps working. The first save from the UI migrates the file to `[llm.interactive]`.

A document mixing **both** shapes is refused rather than guessed at.

## The failure that is not the connection

Once the connection tests green and turns still come back saying the assistant cannot do something, check the tools line under the chat box before touching the endpoint again. A model that does not support tool calling will decline to use tools that reached it perfectly — that is the model or its chat template, not the pipeline, and no amount of connection fiddling changes it. See [Scenario: Debugging a Plugin](Scenario-Debugging-A-Plugin).

## Related

- [LLM Roles](LLM-Roles) — the reference page.
- [Core Settings](Core-Settings) — every `[llm.<role>]` field and its default.
- [Health and Diagnostics](Health-And-Diagnostics) — per-role health and breaker state.
