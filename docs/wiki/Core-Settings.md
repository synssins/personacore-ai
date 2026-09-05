# Core Settings

Every setting in `core.toml`, its default, its allowed range, and what happens when you get it wrong. Read this if you are setting PersonaCore up, or changing where it points.

`core.toml` is the core's own settings file. It lives at `<appdata>/config/core.toml` — see [Appdata Layout](Appdata-Layout) for where `<appdata>` is. The admin API reads and writes the same file (`GET`/`PUT /admin/api/config`, see [Admin API](Admin-API)), so hand-editing and the UI are two ways at one document rather than two competing sources of truth.

**Secrets are never written here.** Fields that need a credential name a secret instead; the value lives in a file under `<appdata>/secrets/`. The write path actively refuses a document containing a credential-shaped key (`api_key`, `access_token`, `passphrase`, and similar) with a message telling you to use the `_secret` field instead. See [Security Model](Security-Model).

Source: `src/personacore/config/settings.py`, `src/personacore/admin/config_io.py`.

## First run, and what counts as an error

| Situation | What happens |
|---|---|
| No `core.toml` at all | **Legitimate.** The core starts on built-in defaults, and writes a commented starter file at `<appdata>/config/core.toml` so there is something discoverable to edit. It never overwrites an existing file. |
| `core.toml` exists but is not valid TOML | **Error.** Startup fails with `ConfigError` naming the file and the parse problem. |
| `core.toml` parses but a value is wrong or a key is unknown | **Error.** `ConfigError` listing one line per offending key. |

Falling back to defaults for a *broken* file was considered and rejected: it would hide a typo and quietly start the assistant pointed somewhere unintended.

Unknown keys are rejected everywhere (`extra="forbid"` on every settings model). A setting the core does not recognise is nearly always a typo for one it does, and the admin API says exactly that: *"PersonaCore has no setting called '…'. Check the spelling, or remove it."*

## Top level

| Key | Type | Default | Notes |
|---|---|---|---|
| `default_persona` | string | `"default"` | Which persona answers when a turn names none. Not checked against disk when set — a persona whose files are missing fails at load time with a message naming the file. See [Personas](Personas). |

## `[llm.<role>]` — the LLM roles block

Five roles: `interactive`, `autonomy`, `triage`, `vision`, `commands`. Only `interactive` is required; every other role falls back to it. Full explanation in [LLM Roles](LLM-Roles).

Each role section carries the same fields:

| Key | Type | Default | Range | Notes |
|---|---|---|---|---|
| `base_url` | string | `"http://localhost:8080/v1"` | — | Root of an OpenAI-compatible host. Trimmed on read — see below. |
| `model` | string | `"local-model"` | — | Sent as `model` in every request body. |
| `api_key_secret` | string or absent | absent | — | The **name** of a secret in `<appdata>/secrets/`, never the key itself. |
| `connect_timeout_seconds` | float | `5.0` | > 0 | Time to establish a connection. |
| `read_timeout_seconds` | float | `120.0` | > 0 | Time to read a response. Deliberately generous and separate from connect: a long generation is not a dead host, and conflating the two kills streaming mid-answer. |
| `failure_threshold` | int | `5` | ≥ 1 | Consecutive failures before this endpoint's circuit breaker opens. |
| `cooldown_seconds` | float | `30.0` | > 0 | How long the breaker stays open before it lets one probe through. |

A value outside its range is a validation error naming the key, not a clamp.

### The pasted endpoint URL is trimmed to its root

Whatever you paste into `base_url` is normalised before it is stored. The stored value is what the settings screen shows and what the client actually uses, so the two can never disagree.

Trimming: leading/trailing whitespace is stripped, trailing `/` is removed, then **one** of the suffixes `/chat/completions`, `/completions`, `/models` is removed if present, and any remaining trailing `/` goes.

All of these end up as `http://llm-host:8080/v1`:

- `http://llm-host:8080/v1`
- `http://llm-host:8080/v1/`
- `http://llm-host:8080/v1/chat/completions`
- `http://llm-host:8080/v1/completions`
- `http://llm-host:8080/v1/models`

The client then builds `{base_url}/chat/completions` and `{base_url}/models` itself.

**Trimming does not add anything.** A `base_url` of `http://llm-host:11434` with no `/v1` stays exactly that, and requests go to `http://llm-host:11434/chat/completions`, which most backends will not answer. The version prefix is yours to supply. (ADR-0011.)

**"localhost" inside a container is the container.** The starter file says so, and it is the single most common first-run failure: use the host's address, or the service name if the LLM runs in the same Compose stack.

### Legacy flat `[llm]`

A pre-ADR-0011 `core.toml` whose `[llm]` table holds `base_url`/`model` directly is still read, and means "interactive only". A document that mixes both shapes — flat keys *and* per-role sections under `[llm]` — is refused with a message telling you to move the flat settings into `[llm.interactive]`. Detection is by key name and cannot collide, because no role name is also a field name.

## `[bus]`

The MQTT event bus (spec §5.2). See [Event Bus](Event-Bus).

| Key | Type | Default | Range | Notes |
|---|---|---|---|---|
| `host` | string | `"mosquitto"` | — | Broker hostname. |
| `port` | int | `1883` | 1–65535 | |
| `username` | string or absent | absent | — | |
| `password` | string or absent | absent | — | The broker password itself. Write-only: never returned by the admin API. |
| `password_secret` | string or absent | absent | — | Name of a secret, not a password. Still supported; `password` wins when both are set. |
| `client_id` | string | `"personacore"` | — | MQTT client identifier. |

### The broker password

`password` is the exception to this file's "secrets are referenced by name" rule, and a deliberate one: an operator typing an MQTT password into the Core settings screen should not first have to hand-create a file in `<appdata>/secrets/` and then name it. It is typed into a password box on that screen and stored here.

Because the value lives in the config document, it never comes back out of one:

- `GET /admin/api/config` returns `"***"` in its place, and so does the raw-JSON tab of the Core settings screen.
- Saving a document that still carries `"***"` **leaves the stored password untouched**. That is what makes a read-then-save round trip safe, and what makes an empty password box mean "keep it" rather than "delete it".
- Removing the password takes the explicit *remove the password* control beside the box. The bus then connects unauthenticated.
- The form field is write-only. The value appears in no `value=` attribute, no title, no data attribute, and nowhere in the page source.

`password_secret` is resolved at assembly time from `<appdata>/secrets/` and handed to the bus as a `SecretStr`, unwrapped only at the moment of connection — the same pattern the LLM client uses for `api_key_secret`. If the named secret cannot be read, the core does **not** stop: the bus is a degradable dependency (spec §10), so it starts unauthenticated and the failure is logged and reported on `/health` as `bus_password_degraded`. The Core settings screen reads that state as "no password is set", because that is what is true of the connection. `username` is passed either way.

With no broker reachable — or with authentication rejected because the password could not be resolved — the assistant simply has no push channel; everything else keeps working and the client reconnects in the background.

## `[server]`

What the core's own listener binds to.

| Key | Type | Default | Range |
|---|---|---|---|
| `host` | string | `"0.0.0.0"` | — |
| `port` | int | `8053` | 1–65535 |

Two caveats, both worth knowing before you rely on this section:

1. **TLS terminates at the reverse proxy** (spec §7). This listener is plain HTTP and is not safe to publish to a network you do not control.
2. **`[server]` is the last-resort source, not the first.** `serve()` resolves the bind address and port in order: an explicit `--host`/`--port` argument, then `PERSONACORE_HOST`/`PERSONACORE_PORT`, then this section — read only if neither of the first two named a value. All three are bounded 1–65535 with a plain-English error, and port `0` is refused everywhere rather than treated as unset, because to the OS it means "pick any free port", which for a published, health-checked container is an assistant nobody can reach. See [Environment Variables](Environment-Variables).

## `[retention]`

Conversation and audit age-out (ADR-0004). See [Audit and Trace](Audit-And-Trace).

| Key | Type | Default | Range | Notes |
|---|---|---|---|---|
| `default_days` | int | `30` | ≥ 1 | Retention window for every surface not named below. |
| `per_surface_days` | table of string → int | `{}` | ≥ 1 each | Per-surface override, keyed by surface name (`voice`, `admin_ui`, `api`, `anonymous`, `system`). |

**Wired and running.** The audit store is constructed with this window, a background task purges once at startup and every six hours after, and shutdown waits up to 30 seconds for a purge already in flight rather than reporting a clean stop while a thread still holds the database. Saving a new window through the admin API applies it to the *next* purge pass immediately — no restart needed. `/health` carries the purge's `last_success`, `last_error` and `consecutive_failures`, so a sweep that has been failing since startup is visible rather than only in a log nobody reads. See [Health and Diagnostics](Health-And-Diagnostics).

An unknown surface name in `per_surface_days` is refused **at this write** — by the admin API, or by startup if you hand-edit the file — with a message naming the bad key and listing the valid surfaces. Earlier behaviour accepted the typo at the API and only discovered it at the next restart, crashlooping the container. A per-surface value below `1` is refused the same way: it used to be silently accepted and would put the purge cutoff in the future, so the next sweep deleted every row on that surface regardless of age.

## `[auth]`

Which single way into the admin interface is open (ADR-0023, ADR-0024). Chosen under **Sign-in** on the Core settings screen, which explains both options in words; this section is what that control writes. See [Security Model](Security-Model).

| Key | Type | Default | Values |
|---|---|---|---|
| `method` | string | `"builtin"` | `"builtin"` — this core's own accounts and sign-in page. With no account yet, the interface opens on a setup page that creates the first one, and there is no default password. `"proxy"` — a login proxy in front (Authelia, Authentik, …) has already signed the person in and names them in a trusted header. |

Anything else is refused: startup fails with `ConfigError`, and the admin API refuses the write, both naming the two valid values and where to set them. There is deliberately no fallback — quietly reading `"buildin"` as one door or the other is a way in nobody chose.

**Changing it needs a restart, and the screen says so.** The method is resolved once at startup: the sign-in and setup routes are mounted on the strength of it, and the account and session stores are built only under `builtin`. The Core settings screen marks a saved change *"restart to apply"* and names it in the save note, rather than reporting a change that has not happened.

**The development bypass is not in here and never will be.** `PERSONACORE_ADMIN_DEV_USER` is an environment variable, it outranks this setting, and that is the point: it is the way back in when a password is lost, when the UI cannot be reached, or when this very file cannot be read. See [Environment Variables](Environment-Variables).

## `[playback]`

Whether a spoken reply starts playing by itself, for the whole household (ADR-0030). Chosen under **Speech playback** on the Core settings screen — "Everyone chooses" / "Always play" / "Never play" — this section is what those three options write. See [Your Profile](Your-Profile) for the per-person side of this feature; this section is only the administrator's rule.

| Key | Type | Default | Values |
|---|---|---|---|
| `autoplay` | string | `"unset"` | `"unset"` — no rule; each person's own choice on their own profile stands. `"on"` — replies play by themselves for everybody. `"off"` — nobody's replies play by themselves. |

**`"unset"` is not the same as `"off"`.** It means the administrator has expressed no opinion, not that playback is switched off — collapsing the two would silence anyone who had chosen to have replies read aloud.

**`"on"` and `"off"` force the state for everybody.** Each person's own control on [Your Profile](Your-Profile) shows the forced state and is disabled, with a line saying an administrator set it. Forcing a state does not erase what a person had chosen — it is what they return to if the rule is later set back to `"unset"`.

## `[image]`

The image generator: a service that takes a prompt and hands back a picture, answering image conversations. Edited on the **Image generation** card of the Model connections screen; everything but the connect timeout is on that card.

| Key | Type | Default | Values |
|---|---|---|---|
| `base_url` | string | absent | The generator's address. Absent or empty means image generation is off, which is a normal state, not an error. |
| `model` | string | absent | Only needed when the server hosts more than one model. |
| `prompt_prefix` | string | `""` | Put in front of the text of every picture request, with one space between. Empty adds nothing. Never appears in the transcript; the stored message is what the person typed. Capped at 4,000 characters from the screen. |
| `connect_timeout_seconds` | float | `5.0` | Time to open the connection. Not on the screen; reachable from the raw editor. |
| `read_timeout_seconds` | float | `600.0` | **Wait for the picture.** A generator sends nothing until the render is finished, so this one read has to outlast the whole render. |
| `total_timeout_seconds` | float | `900.0` | **Ceiling.** The limit on the entire request, and the clock that does not reset. It catches a generator that has stopped, which the wait cannot. |

## `[memory]`

Household-wide defaults for [memory](Memory) — recall ranking, review timing, and how long an unpromoted memory survives. Whether a given persona uses memory at all is a separate switch, on the persona itself (`persona.toml`'s `memory` key); this section only shapes how memory behaves where it's on.

| Key | Type | Default | Range | Notes |
|---|---|---|---|---|
| `quiet_minutes` | int | `10` | 1–1440 | How long a conversation sits with no new messages before the review pass reads it and asks the triage model what's worth keeping. |
| `recall_limit` | int | `8` | 1–50 | How many memories come back on a single recall — the persona's own plus the household's long-term ones, combined. |
| `recall_floor` | float | `0.3` | 0 – 1 | The least similar a memory may be to the message and still be recalled. `0` recalls the top matches whatever their similarity. The match score of each recalled memory is shown on the Memory screen. |
| `half_life_days` | float | `30.0` | > 0 | Recency half-life for recall ranking: a memory unused for this many days scores half of one used today. |
| `duplicate_threshold` | float | `0.92` | 0–1 | How close a new memory has to be, by meaning, to an existing one before it's treated as the same fact and updates that row instead of adding a new one. |
| `short_term_days` | int | `60` | ≥ 1 | A short-term memory with no activity for this many days is purged. A promoted, long-term memory never expires and ignores this. |

A value outside its range is a validation error naming the key, not a clamp, matching every other section here.

## `[workspace]`

The per-conversation scratch folder a tool result can be saved into, and the cap on how much of a tool result reaches the model in the first place.

| Key | Type | Default | Range | Notes |
|---|---|---|---|---|
| `tool_result_chars` | int | `64000` | ≥ 256 | The most characters of one tool result the model receives in a turn; past this the text is cut and the cut is marked. Read once at startup, the same as `[memory] recall_limit` — a saved change needs a restart to take effect. |
| `long_item_chars` | int | `8000` | ≥ 256 | With a workspace turned on, a plain-text tool result longer than this is saved to the conversation's workspace as a file instead of being handed over whole. Must be no greater than `tool_result_chars`; a document that breaks that rule is refused with a plain-English message. |
| `max_file_bytes` | int | `2000000` | ≥ 1024 | The largest a single workspace file may grow. A write past this limit is refused, naming the limit. |
| `max_workspace_bytes` | int | `50000000` | ≥ 1024 | The largest one conversation's whole workspace folder may grow, all its files together. A write that would push the folder past this limit is refused the same way. |

The workspace root itself is not a setting: it is fixed at `<appdata>/workspaces`, the same way every other appdata folder is.

## The starter file

On first run the core writes a commented `core.toml` containing `default_persona`, an `[llm.interactive]` section with timeouts, a `[bus]` section, `[retention] default_days = 30`, and `[auth] method = "builtin"`. It exists so an operator has a file to find and read; without it the settings would exist only as defaults buried in code. It is never rewritten over an existing file.

## Applying a change

Saving through `PUT /admin/api/config` re-resolves every LLM role and swaps only the ones whose settings actually changed — an untouched role keeps its client, its connection pool and its circuit-breaker state — updates the default persona live, and swaps the retention window the next purge pass reads. The playback rule needs nothing to be re-established at all: it is read from the loaded settings on each request rather than captured at start, and saving replaces that object, so a change to it lands on the next page render. The response still says *"Settings saved. Some changes take effect when the core restarts."* because the listener's bind address and the way in (`[auth] method`) are not re-established that way.

Editing the file by hand still works and is sometimes the right tool for recovery, but it needs a restart, and the UI does not assume it is the only writer (ADR-0010).
