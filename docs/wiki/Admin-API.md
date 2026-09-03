# Admin API

Every endpoint under `/admin/api`: method, path, purpose, request, response and errors. Read this if you are building against the admin surface or driving it from a script.

A running instance serves its own OpenAPI document at **`/admin/api/openapi.json`** and an interactive browser at **`/admin/api/docs`**. Those are generated from the code and are authoritative on exact field types; this page is the narrative version and covers the behaviour the schema cannot express.

This API was built before any UI on purpose, because it is the contract and it is what survives. The designed admin UI is served under `/admin` — it is a consumer of this API like any other, and its pages are excluded from the OpenAPI schema.

Source: `src/personacore/admin/routes.py`, `src/personacore/admin/models.py`.

## Authentication — an access key, and only an access key

Every endpoint under `/admin/api` requires an **access key carrying the `write:admin` scope**, presented as `Authorization: Bearer <key>`. Three sign-in endpoints are the exception and are named below.

**A browser session is not a credential here, and neither is the trusted header.** It used to be: this surface asked only "are you signed in", so any household member with an account could read `GET /admin/api/trace` — everybody's conversations — and `POST /admin/api/keys`, which mints credentials to `/v1`. The designed UI hides those screens from a member (ADR-0032); this API was the way round that gate.

Keys are issued on the **Keys** screen, and the scope is a checkbox there — *"May use the admin API"* — which is **off unless you tick it**. A key issued so a display or a script can hold a conversation on `/v1` therefore reaches nothing here, and no key issued before this existed gained anything.

Refusals:

* `401` — no key, a malformed `Authorization` header, an unknown key, a revoked one, or one that has been switched off. One sentence for all of them, so nobody learns which keys exist by reading error messages.
* `403` — a real, working key that does not carry `write:admin`. A different problem with a different fix, and the holder already has the key, so there is nothing to disclose.
* `503` — this core was assembled without a key store, so no key can be checked. Answered only to a caller who actually presented one; a caller with no credential gets the `401` and learns nothing about how this core is configured.

A key carrying the scope is an **administrator** on this surface: the endpoints that call `require_admin` (the account routes and `PUT /config`) accept it. That is what granting the scope means — these routes do not divide into a harmless half and a dangerous half, since reading the trace is reading everybody's conversations.

Audit records for changes made here name the key, as `key:<key_id>`, rather than a person — because a key is who called. The same handlers driven from the admin screens still record the person who clicked: those pages call them directly rather than over HTTP.

The HTML admin UI is unaffected by all of this. It needs no key, and is guarded by the sign-in it always was: whichever single door `[auth] method` names — the core's own session cookie under `builtin` (the default), or the trusted-user header under `proxy` (by default `Remote-User`, set by the reverse proxy after it has authenticated the request). That check fails **closed** in every branch: a missing cookie, an unknown or expired session, and a missing, empty, over-long (>256 characters) or unprintable header are all `401`, never an anonymous session. See [Security Model](Security-Model).

This is only safe under the deployment spec §7 describes. Read [Security Model](Security-Model) before exposing this port.

Note that `/admin/api/docs` and `/admin/api/openapi.json` are served by the application itself rather than by this router, so they do **not** carry the auth dependency. In the intended deployment the proxy is what keeps them private.

## The error shape

Every failure leaves as an HTTP error whose `detail` is one object:

```json
{ "error": "plain English sentence", "problems": [ { "key": "...", "problem": "...", "hint": "..." } ] }
```

`problems` is populated when several keys are wrong at once, so an operator fixes them in one pass rather than three. Messages are written to be shown verbatim (spec §9).

## Health

### `GET /admin/api/health`

The system health dashboard: one row per LLM role, the event bus, the audit store, appdata free space, and one row per plugin.

**Always `200`.** The caller reads `state`. An HTTP error here would make "the dashboard is down" and "the system is down" indistinguishable, and no probe is allowed to raise.

Response `SystemHealth`: `state` (`ok` | `failing` | `unknown`), `checked_at`, `components[]` where each component is `{name, state, detail, facts}`. Full component list and meanings in [Health and Diagnostics](Health-And-Diagnostics).

## Plugins

Plugin names in a path must match `^[a-z][a-z0-9-]{1,63}$` — the manifest's own name rule. Anything else is rejected by path validation before a handler runs.

### `GET /admin/api/plugins`

Every plugin found, loaded or broken, each failure with the plain-English reason.

Served from a cache, not a fresh scan — otherwise "reload" would be a call with no observable effect. Response `PluginListing`: `plugins[]`, `failures[]`, `loaded_count`, `failed_count`, `scanned_at`. Each `PluginView` carries `name`, `version`, `transport`, `description`, `directory`, `tools` (name → risk level), `state`, `detail`, `restarts`, `enabled`.

A plugin the operator switched off stays in the listing with `enabled: false`, `state: unknown` and a sentence explaining it was switched off — not `failing`. A supervisor that stopped a plugin on request has not failed at anything, and colouring it red trains people to ignore red.

### `POST /admin/api/plugins/reload`

Rescan the plugin directories, and start/stop/restart plugins to match what is now on disk. Also drops the persona cache, so the same button covers "I edited a persona and something is stale".

Response `ReloadResult`: `reloaded`, `listing`, `message`. Audited as `plugins.reload`.

### `POST /admin/api/plugins/install`

Install a plugin from an uploaded zip package (ADR-0013).

**The request body is the `.zip` bytes themselves**, with `Content-Type: application/zip`. Multipart form data is refused with `415` and a sentence saying so — parsing multipart would need a runtime dependency that is not in the image.

Query parameters: `replace` (bool, default `false`), `filename` (string, max 200 chars, optional). `filename` is recorded in the audit entry so an investigator can see which file an operator chose; it is **never** used to build a path and never appears in a response. The installed folder's name comes from the validated manifest.

Nothing in the package is executed, imported or `pip install`ed. Validation happens in a staging directory inside appdata, and a package that fails is rejected with nothing written.

Success: `201` with `InstallResult` (`installed`, `listing`, `message`).

| Failure | Status |
|---|---|
| Multipart body | `415` |
| Body over the archive limit (32 MiB by default) | `413` |
| Uncompressed total or entry count over the limit (128 MiB / 2000 entries) | `413` |
| Name collision without `replace=true` | `409` |
| Traversal, symlink, invalid or missing manifest | `422` |
| Install failed after validation | `500` |

### `POST /admin/api/plugins/{name}/enable` and `.../disable`

Switch a plugin on or off without uninstalling it. Idempotent: switching on something already on succeeds and says nothing changed.

The choice is written to `<appdata>/config/plugins-disabled.toml` **first**, then applied to the running process, so a crash between the two halves leaves a core that comes back in the state you asked for. Response `PluginToggled`: `name`, `enabled`, `changed`, `listing`, `message`.

If the core has no live toggle wired in, the message adds *"It takes effect when the core next starts."* rather than claiming an effect it did not have.

`404` if no plugin by that name is installed. Audited as `plugins.enable` / `plugins.disable`.

### `DELETE /admin/api/plugins/{name}`

Uninstall: stop the plugin, delete its folder and everything in it, then reload. Stopping first is not tidiness — a running stdio plugin holds its own files open, and Windows simply refuses to delete them underneath it.

Response `PluginUninstalled`: `name`, `directory`, `config_removed`, `files_removed`, `listing`, `message`.

Unlike key revocation, this **does** answer `404` for something that is not installed. It destroys data, and an operator who deletes the wrong name has to find out rather than be told "done".

### `GET /admin/api/plugins/{name}/config`

Read one plugin's own `config.toml`, as text, comments included — a plugin's comments are its field help, and re-serialising a parsed document would delete them.

Response `PluginConfigResponse`: `plugin`, `path`, `exists`, `content`, `valid`, `problem`, `secret_references`.

A file that does not currently parse is still returned, with `valid: false` and the syntax error in `problem`. The broken one is the one you need to open.

`404` if the plugin has no config file; `409` if the path is unsafe; `500` if the file cannot be read.

### `PUT /admin/api/plugins/{name}/config`

Replace a plugin's `config.toml`. Body: `{"content": "..."}`, max 256 KiB.

**The core checks the text is well-formed TOML and nothing else.** What the keys mean belongs to the plugin — the core does not know that `forecast_days` must be 1–7; the plugin does, and checks it at startup. Do not expect schema validation here.

A syntax error is a `422` naming the problem in plain English, **and the file on disk is untouched** — the text is parsed before anything is opened for writing. `404` for a plugin with no config; `409` for an unsafe path; `500` for a write failure.

On success the plugin is restarted (off, then on) so the setting is actually in use, and the message says either *"…was restarted, so they are in use now"* or *"They take effect when the plugin next starts."* A plugin that is switched off is deliberately left off. Response `PluginConfigUpdateResponse`: `saved`, `config` (read back from disk), `reloaded`, `message`.

Audited as `plugins.config.update`, recording the top-level key names only — never the settings themselves, because the record goes into backups.

### `POST /admin/api/plugins/{name}/config/lookup`

Fill a setting by asking the plugin (ADR-0016) — the "search for your town" button on a settings page.

Body: `{"field": "...", "query": "..."}`. `field` names the **setting**, max 128 chars; `query` is what the operator typed, max 200 chars. **The caller never names the tool.** Which tool runs is read from the plugin's own `config.schema.json`, and it runs only if the plugin's *manifest* declares that tool `safe`. Being installed does not make a tool reachable from a settings page.

The call then goes through the ordinary tool path, so the plugin's declared permissions apply and the plugin host writes its own audit record. The admin action is recorded separately as `plugins.config.lookup`, with the query and the result *count* — never the results, which are untrusted text.

**No match is a `200` with an empty list** and a sentence saying so. "Nothing found" is an answer, and an error status for it would make a working lookup indistinguishable from a broken one.

| Failure | Status |
|---|---|
| Empty query, over-long query, unknown field, field declares no lookup | `422` |
| Plugin ships no `config.schema.json` | `404` |
| Plugin is not loaded (so its tools' risk cannot be checked), unsafe config path | `409` |
| This core has nothing that can call a plugin tool | `503` |
| The tool could not be called, or its answer could not be read | `502` |

## Personas

### `GET /admin/api/personas`

Response `PersonaListing`: `personas[]`, `default_persona`. A persona whose files are broken is listed with `loadable: false` and its reason rather than dropped — "it isn't there" is never a useful answer about something you can see on disk.

### `GET /admin/api/personas/{name}`

One persona with its full system prompt, so a picker can preview what swapping actually changes. Response `PersonaDetail`: `name`, `display_name`, `description`, `voice_engine`, `voice_name`, `is_default`, `system_prompt`, `metadata`.

`404` if not found; `422` if its files are present but invalid.

### `POST /admin/api/personas/{name}/select`

Make this persona the default. No body.

The persona is **loaded first and the config written second**: a default pointing at a persona that cannot load is a core that will not answer, and finding that out at the next turn rather than at the click is the wrong order.

Response `PersonaSelected`: `default_persona`, `message`. Audited as `personas.select`.

## Trace

### `GET /admin/api/trace`

The observability view: audit records and transcript messages merged into one descending timeline.

Query parameters:

| Name | Type | Default | Notes |
|---|---|---|---|
| `profile` | string, ≤256 chars | — | Filter by owner. |
| `surface` | `voice` \| `admin_ui` \| `api` \| `anonymous` \| `system` | — | |
| `correlation_id` | string, ≤256 chars | — | Follows one turn end to end. |
| `since` / `until` | datetime | — | A naive timestamp is read as UTC rather than rejected. |
| `kind` | `audit` \| `transcript`, repeatable | both | |
| `limit` | int | `50` | 1–200. |
| `offset` | int | `0` | ≥ 0. |

Response `TracePage`: `entries[]`, `limit`, `offset`, `returned`, `has_more`, `filters`. `has_more` is measured, not guessed — the window is always fetched one row longer than the page needs. At most 2000 rows are pulled from the store to satisfy one page, which bounds deep pagination.

Field meanings are in [Audit and Trace](Audit-And-Trace).

## Core configuration

### `GET /admin/api/config`

Response `ConfigResponse`: `settings` (the whole validated document), `secret_references` (dotted path → secret *name*, so the UI can render "API key: comes from the secret 'llm_key'" instead of a blank field inviting a paste), `source` (the file path), `exists`.

Secrets are named here, never valued. A belt-and-braces check refuses to return a document containing a credential-shaped key at all.

`500` if the file exists but cannot be read — the message names the file and the offending key.

### `PUT /admin/api/config`

Body: `{"settings": { ... }}` — the **whole** document, not a patch. A partial write whose meaning depends on the current file content is impossible to review and impossible to roll back by hand.

A rejected write returns `422` with one problem per offending key, each naming the key and saying what to do, and **nothing is written**: the file is only replaced once the whole document validates. A submitted document containing a live secret value is refused with a hint pointing at the matching `_secret` field.

On success the settings are applied to the running core where possible (LLM roles, default persona) and the message says *"Settings saved. Some changes take effect when the core restarts."* Audited as `config.update`, recording key names only.

See [Core Settings](Core-Settings) for every field.

## API keys

If this core was assembled without a key store, nothing under `/admin/api` can be opened at all — a key is the only credential it takes — and a caller who presents one gets `503` with a plain-English reason rather than a blank refusal. The Keys **screen** says the same thing in its own words, because it reaches these handlers directly rather than through the door.

### `GET /admin/api/keys`

Response `ApiKeyListing`: `keys[]`, `count`. Each `ApiKeyView` is `key_id`, `note`, `created_at`, `enabled`, `profile`. Ordered oldest first, then by id, so the list does not reshuffle between page loads.

**Neither the key nor its hash appears here.** The core keeps only a one-way hash, and the hash stays in the store because a fingerprint of a credential on a screen is one screenshot away from being a hint. A revoked key is gone from this list rather than flagged in it.

### `POST /admin/api/keys`

Mint a key. Body `ApiKeyIssueRequest`: `profile` (a full `PolicyProfile` — see [Policy Profiles](Policy-Profiles)), `note` (≤200 chars).

Success: `201` with `ApiKeyIssued`: `api_key_shown_once`, `warning`, `key`.

**The plaintext key is in this response and nowhere else, ever.** Not in the listing, not in the audit record this write produces, not in any log line, not recoverable by any later call. The field is named `api_key_shown_once` for that reason and the `warning` field carries the sentence a client must print beside it. Losing the value costs one re-issue.

`400` for a profile of kind `anonymous`: the exposed API has no anonymous tier even on the LAN, and a key attributing its traffic to one would launder one access tier into another. `400` also for any other profile the record model refuses. `500` if the key file cannot be written.

Audited as `api_keys.issue` with identifiers only — no key, and no note (operator free text that would end up in backups).

### `DELETE /admin/api/keys/{key_id}`

Revoke a key. `key_id` must match `^[A-Za-z0-9_-]{1,64}$`.

**Always `204`, never `404`**, once the caller is authenticated. Two reasons: `DELETE` promises a state, so a retry must agree with the first call and not look like a failure; and a `404` would answer "does this key id exist?" for anyone who can reach the endpoint. The audit record keeps the difference — `existed` says whether anything was actually removed.

`500` if the key file cannot be written.

## Accounts and sessions (PC-283 to PC-291)

The core's own sign-in has a set of endpoints of its own, alongside the ones described in [Security Model](Security-Model). Most require an access key like everything else under `/admin/api`; three do not, because nobody has a credential of any kind yet when they run — they are how a household gets its first one.

### `GET /admin/api/auth/me`

Who this request is, and by which door — present under every auth method, `builtin` or `proxy`, because "who does this core think I am" is the first question asked when a proxy is misconfigured. Response `WhoAmI`: `username`, `is_admin`, `method` (the same word `/health`'s `admin_auth.method` uses), `can_sign_out`.

The following endpoints exist **only** when this core has the built-in sign-in door open (`[auth] method = "builtin"`):

### `GET /admin/api/auth/sessions`

Your own signed-in devices — never anybody else's; the user comes from the authenticated request, not from a parameter. Response `SessionListing`: `username`, `sessions[]`, each a `SessionView` (`session_id`, `started_at`, `last_seen_at`, `expires_at`, `current`).

### `POST /admin/api/auth/sessions/end`

End all of your own sessions, including the one that made the call — PC-288. Response `SessionsEnded`: `username`, `ended`, `message`.

### `GET /admin/api/users`

Every account — **admin only** (PC-290). A non-admin gets `403` with a reason, never an empty list. Response `UserListing`: `users[]`, each a `UserView` (`username`, `is_admin`, `created_at`, `sessions` — the current session count).

### `POST /admin/api/users`

Add an account — admin only. Body `CreateUserRequest`: `username` (1-64 chars), `password` (1-1024 chars), `is_admin` (default `false`). There is no default password anywhere (PC-291); this is how every account after the first gets one. `422` if the username or password is rejected. Success: `201` with a `UserView`. Audited as `users.create`.

### `POST /admin/api/users/{username}/sessions/end`

End all sessions for one named account — admin only (PC-289), the other half of PC-288. `404` if no such account exists. Response `SessionsEnded`: `username`, `ended`, `message`.

### `POST /admin/api/auth/sign-in`, `/auth/sign-out`, `/auth/setup`

The three unauthenticated exceptions on this surface, mounted only under the `builtin` door, because nobody can be signed in while signing in. Each is throttled and audited, and none of them ever return the session token in a response body — it goes back only as a `Set-Cookie`.

- `POST /admin/api/auth/sign-in` — body `SignInRequest` (`username`, `password`); sets the session cookie and returns `SignedIn` (`username`, `is_admin`, `expires_at`). Wrong name and wrong password give the same `401`; repeated failures from one address give `429` with `Retry-After` (PC-283, PC-287).
- `POST /admin/api/auth/sign-out` — ends the session the request carries and clears the cookie. Unauthenticated on purpose, so a browser holding an already-ended or unknown token can still clear it.
- `POST /admin/api/auth/setup` — body `SetupRequest` (`username`, `password`); creates the first account, which is an admin (PC-291), and answers `409` the instant one account already exists. Success: `201` with `SignedIn`.

## What is deliberately not here

There is no chat endpoint. The turn API is the OpenAI-compatible one at `/v1` (see [OpenAI-Compatible API](OpenAI-Compatible-API)); a second way in would be a second thing to secure. The admin UI's Chat screen at `/admin/chat` has a chat box for trying the assistant, and it is not part of this contract.

There is no rate limiting on this surface either. `PolicyProfile.rate_limit` is carried by every key, but enforcing it belongs in front of every surface at once.
