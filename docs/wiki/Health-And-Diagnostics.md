# Health and Diagnostics

Every component the health endpoints report, every state, what each means, and how to read a failing plugin. Read this when something is wrong and you need to know what.

There are **two** health endpoints and they answer different questions.

Source: `src/personacore/server.py`, `src/personacore/admin/routes.py`, `src/personacore/plugins/health.py`.

## `GET /health` — liveness, unauthenticated

Always present, even when every other surface is missing. That is deliberate: the container's health check must answer even during a partial deployment, otherwise a broken mount looks identical to a dead process and the orchestrator restarts a container that is fine.

```json
{
  "status": "ok",
  "version": "…",
  "contract": "…",
  "surfaces": ["admin", "openai"],
  "admin_auth_bypass": null,
  "trusted_proxies": ["127.0.0.1", "::1", "localhost"],
  "admin_auth": {
    "method": "builtin",
    "chosen": "builtin",
    "bypass": false,
    "bypass_user": null,
    "trusted_header": null,
    "trusted_proxies": [],
    "warning": null,
    "setup_required": true
  },
  "retention": {
    "last_success": "…",
    "last_error": null,
    "consecutive_failures": 0
  },
  "bus_password_degraded": null,
  "bus": { "host": "…", "port": 1883, "…": "…" },
  "voice": {
    "engines": [
      {
        "id": "espeak",
        "display": "…",
        "available": true,
        "enabled": true,
        "running": true,
        "state": "running",
        "unavailable_reason": null,
        "error": null
      }
    ],
    "problems": [],
    "skipped_voices": []
  }
}
```

| Field | Meaning |
|---|---|
| `status` | Always `"ok"` if the process is answering. This is liveness, not readiness. |
| `version` | The core's version. |
| `contract` | The plugin/event contract version. |
| `surfaces` | Which routers actually mounted: `admin`, `openai`, or fewer. A surface that failed to import or failed to mount is logged and **skipped** rather than taking the whole container down — so a short list here is the first thing to check when an endpoint 404s. |
| `admin_auth_bypass` | The development bypass user, or `null`. **Anything but `null` in production is a serious finding** — it means anyone who can reach this port is an admin. |
| `trusted_proxies` | The active allowlist. If your proxy's address is not in here, the admin API will 401 every request. |
| `admin_auth` | **Which single way in is open**, resolved once at startup. `method` is what is in force; `chosen` is what `[auth] method` says, so you can see which door you get back when the bypass is removed; `bypass`/`bypass_user`/`warning` describe the break-glass; `trusted_header` and `trusted_proxies` are `null`/empty under `builtin`, because no header is a credential there; `setup_required` says the built-in door has no account yet. Unauthenticated, so it carries no account names and no count of them. See [Security Model](Security-Model). |
| `retention` | The scheduled purge's `last_success` (timestamp or `null`), `last_error` (`repr()` of the exception, or `null`), and `consecutive_failures`. The purge runs at startup and every six hours; a rising `consecutive_failures` with an old `last_success` means the audit database is not being pruned. See [Scenario: Retention and Privacy](Scenario-Retention-And-Privacy). |
| `bus_password_degraded` | The error resolving `[bus].password_secret`, or `null`. Set when the secret could not be read at startup — the bus then runs unauthenticated rather than the core failing to start, since the bus is a degradable dependency. |
| `bus` | **What the running bus is actually configured with**, read off the live object rather than re-read from `core.toml`: `host`, `port`, `client_id`, `username`, `password_set`, plus the connection state and counters. Never the password, and never the name of the secret it came from. |
| `voice` | The core's own voice subsystem (ADR-0029 — the core owns voice; engines are built-in, not plugins). `engines[]` is one row per built-in engine (`id`, `display`, `available`, `enabled`, `running`, `state` — `unavailable`, `off`, `running` or `failed` — `unavailable_reason`, `error`); `problems[]` is engines this build could not load; `skipped_voices[]` names any voice folder skipped at the last walk, with the reason. This is a snapshot taken when voices last changed, not a fresh disk walk on every call, since this endpoint is polled by the container healthcheck every few seconds. |

`bus` is there because an error message on its own cannot be acted on. `MqttError: [Errno -2] Name or service not known` is a DNS failure, which an IP literal cannot produce — so seeing it against an address you believe is an IP means one of three different things: the value has whitespace in it and is not what it looks like, the value is right and this process never received it, or the process is still running the configuration it started with. The address beside the error tells you which. The values are raw and exactly as configured; JSON quotes them, so a trailing space is visible here without any further dressing up.

These are surfaced here precisely so an auth misconfiguration, a silently failing purge, or an unauthenticated bus connection cannot quietly survive into a deployment. See [Security Model](Security-Model).

This endpoint says nothing about whether the LLM is reachable or the plugins are running.

## `GET /admin/api/health` — component health, authenticated

The dashboard. Behind the admin authentication like everything else under `/admin/api`.

**Always returns `200`.** The caller reads `state`. An HTTP error here would make "the dashboard is down" and "the system is down" indistinguishable, and no probe is allowed to raise — a probe that itself misbehaves is reported as `unknown` with the reason attached.

```json
{ "state": "ok", "checked_at": "…", "components": [ { "name": …, "state": …, "detail": …, "facts": {…} } ] }
```

### The three states

Three-valued on purpose. Two values would force "I don't know" to be reported as one of "fine" or "broken", and both of those are lies.

| State | Meaning |
|---|---|
| `ok` | Checked, and healthy. |
| `failing` | Checked, and not working. |
| `unknown` | Could not be determined — no supervisor running, a probe that threw, a plugin deliberately switched off. |

**Overall rollup:** any `failing` component makes the whole thing `failing`; otherwise any `unknown` makes it `unknown`; otherwise `ok`.

## The components

### `llm.<role>` — one row per LLM role

Five rows: `llm.interactive`, `llm.autonomy`, `llm.triage`, `llm.vision`, `llm.commands`. Roles sharing an endpoint are probed once and reported five times, so loading the dashboard does not fire five requests at one host.

| State | Meaning |
|---|---|
| `ok` | The endpoint answered a `GET /models`. |
| `failing` | It did not. `detail` carries the spoken sentence, or *"The language model host is not responding."* |
| `unknown` | The probe itself threw. `detail` says *"The language model host could not be checked: …"* |

`facts` carries:

| Fact | Meaning |
|---|---|
| `model` | The model name configured for this role. |
| `endpoint` | A 12-character digest of this role's whole endpoint configuration. Two roles with the same digest share a client **and a circuit breaker**. |
| `breaker` | `closed` (normal), `open` (failing fast after repeated failures), `half_open` (letting one probe through). |
| `falls_back_to` | Present only when this role has no section of its own — the role it is borrowing. |

**The base URL is deliberately not here.** A hand-edited `core.toml` can carry credentials in one, and the dashboard is rendered, logged and screenshotted. The digest is enough to see which roles share a host.

A core assembled without roles (the development CLI) reports a single `llm` row instead. See [LLM Roles](LLM-Roles).

### `event_bus`

| State | Meaning |
|---|---|
| `ok` | Connected to the broker. |
| `failing` | Not connected. `detail` names the broker it was tried against **and** the last error — *"Not connected to the message broker at `"192.0.2.7":1883`: …"*. |
| `unknown` | The bus could not be checked at all. |

`facts`: `host`, `port`, `client_id`, `username`, `password_set`, then `connected`, `last_error`, `received`, `malformed`, `published`, `reconnects`. A rising `malformed` against a steady `received` means a publisher is writing the wrong shape. See [Event Bus](Event-Bus).

The first five come from the running bus, not from `core.toml`, so a mismatch between the file and the process is visible rather than inferred. On the admin UI's Health screen they are rendered quoted and with any whitespace drawn — a host of `192.0.2.7 ` shows as `"192.0.2.7␣"` and says *"this address contains whitespace, which no host name or IP has"*, because a value that looks correct and is not is the hardest kind to find. `password_set` is a yes/no: never the password, never its length, never the name of the secret behind it.

A disconnected bus is `failing` but the assistant keeps working — it simply has no push channel.

### `audit_store`

Checked rather than assumed, because an assistant that keeps answering while silently recording nothing is worse than one that stops, and this row is the only place that difference becomes visible.

| State | Meaning |
|---|---|
| `ok` | The database is readable and the audit directory is writable. |
| `failing` | The database could not be read (*"Nothing is being recorded until this is fixed."*), the audit directory is missing, or it is not writable (*"Check the appdata volume's ownership and permissions."*). |

`facts`: `schema_version`, and `directory` when healthy.

Writability is tested with an access check on the directory, **not** by writing a probe record — the store is the evidence trail, and salting it with health-check rows to prove it works corrupts the thing being proven.

### `appdata_disk`

| State | Meaning |
|---|---|
| `ok` | More than the warning threshold free. |
| `failing` | Below it: *"Only X free on the appdata volume. Memory, transcripts and the audit log all write here — free some space before it runs out."* |
| `unknown` | Free space could not be measured. |

`facts`: `path`, `total_bytes`, `free_bytes`, `used_bytes`, `warning_bytes`.

The threshold is **1 GiB** by default. The number is chosen because the first symptom of a full volume is the audit log silently failing to write — the one component whose failure hides every other failure.

### `plugin:<name>` — one row per plugin

Both loaded plugins and ones that failed to load. A plugin that failed to load appears as `plugin:<name>` if its name is known, or `plugin:<path>` if it is not, always `failing`, with the load error as the detail and the source path in `facts`.

For a plugin that did load, `facts` carries `transport` and `restarts`.

## How to read a failing plugin

The admin API's three-state vocabulary is a translation of the supervisor's five. It is worth knowing both, because `detail` is written in terms of the second.

| Supervisor state | Meaning | Dashboard shows |
|---|---|---|
| `starting` | Connecting for the first time, or waiting out a restart backoff. | `failing` |
| `healthy` | Connected, handshake done, and its tools match its manifest. | `ok` |
| `degraded` | Loaded but not currently answering — a call failed or the connection dropped, and a restart is on its way. **Includes a plugin that has never successfully started**, e.g. one stopped dead by a config error. | `failing` |
| `failed` | Not running and not coming back on its own: never loaded, manifest disagrees with the server, or crashed once too often. Always carries `last_error`. | `failing` |
| `disabled` | Deliberately not running. Never an error. | `unknown` |

`degraded` maps to `failing` rather than `ok` on purpose. Calling it healthy put a green row next to a plugin that could not run at all, with the reason sitting in `detail` the whole time.

`starting` maps to `failing` too — anything the translation table does not recognise is passed through unchanged rather than guessed at, because a wrong status is worse than an unfamiliar one.

**With no supervisor running at all**, every plugin reports `unknown` with *"No plugin supervisor is running yet, so this plugin's live status is not known."* rather than a guess.

**A plugin you switched off** reports `unknown`, not `failing`, with: *"Switched off in the admin interface. Its folder and its settings are still here; it is not running, and its tools are not offered to the assistant or callable by anything."* Said in full rather than as the word "disabled", because a state with no explanation next to it reads as a fault, and a supervisor that stopped a plugin on request has not failed at anything. Colouring it red would train people to ignore red.

### The order to check things in

1. **Is it in `GET /admin/api/plugins` at all?** If not, discovery never found the folder. Check it is under `<appdata>/plugins/` (or `plugins-http.d/`), and that the folder name is not prefixed `_` or `.` — those are not plugins.
2. **Is it in `failures[]` rather than `plugins[]`?** Then the manifest itself was rejected, and `reason` says why. Spec §5.1 requires a broken plugin to be visible with its error; a failure that is merely absent from the list is the worst possible outcome.
3. **Is `enabled` false?** Then nothing is wrong; it was switched off.
4. **Is `state` failing with a `detail`?** That is the supervisor's `last_error`, written in plain English and safe to show verbatim. A manifest that disagrees with the running server is terminal — restarting cannot fix a manifest.
5. **`restarts`** climbing means it starts and dies. Look at the plugin's own logs.
6. **Has a tool call refused rather than failed?** That is policy, not health. See [Risk Levels](Risk-Levels).

There is also a **state-file failure row**: if `<appdata>/config/plugins-disabled.toml` cannot be read, an extra entry appears in `failures[]` naming that file. It matters because with the file unreadable every plugin reads as enabled, so the row has to be loud.

## Other diagnostics

### The startup log

Structured JSON to stdout and to `<appdata>/audit/personacore.jsonl`. Useful events at boot: `core_config_created`, `default_persona_created`, `bundled_plugins_installed`, `identity_guard_enabled` (or `admin_auth_bypass_enabled` — a warning), `surface_mounted` / `surface_absent` / `surface_mount_failed`, `bus_connected`, `personacore_started`.

Every record is redacted. See [Audit and Trace](Audit-And-Trace).

### `personacore doctor`

A development CLI command. Prints the appdata root, one line per LLM role showing model, base URL and any fallback, the personas found, and whether the `interactive` endpoint answers. On failure it prints the exception and the reminder that `base_url` must include the `/v1` suffix.

It is a diagnostic tool, so it reports rather than raising: it exits `1` when the host is unreachable.

Unlike the dashboard it **does** print base URLs, so treat its output as sensitive if a URL carries credentials.

### The trace view

For "what did it actually do", not "is it up". `GET /admin/api/trace`, filterable by profile, surface, correlation id and time. See [Audit and Trace](Audit-And-Trace).

### The OpenAPI document

`/admin/api/docs` and `/admin/api/openapi.json` on a running instance — generated from the code, so authoritative on exact field types.
