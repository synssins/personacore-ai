# Security Model

The whole picture: how each surface authenticates, what the trusted-proxy allowlist defends against, where credentials live, and what is fenced rather than fixed. Read this before exposing any port.

Sources: `src/personacore/server.py`, `src/personacore/admin/routes.py`, `src/personacore/api/keys.py`, `src/personacore/config/secrets.py`, `src/personacore/agent/untrusted.py`, spec §7.

## The deployment this assumes

Spec §7 requires TLS on every HTTP interface, including on the LAN, and real login on the admin UI. PersonaCore does neither itself. It assumes a reverse proxy in the stack that:

1. terminates TLS,
2. performs OIDC login (Authelia or equivalent, ADR-0001),
3. sets a header naming the signed-in user,
4. **strips any inbound copy of that header** before setting its own.

The core's own listener is plain HTTP, published only on the internal Compose network and never mapped to the host. Remove either condition — expose the port, or let the header through from outside — and the admin surface becomes "anyone may claim to be anyone".

There is deliberately no OIDC implementation in the core. Two places that decide who you are is one place too many.

## Surface by surface

| Surface | Path | Authentication |
|---|---|---|
| Liveness | `/health` | **None.** Deliberately — the container's health check must answer even when a surface is missing, otherwise a partial deployment looks identical to a dead one and the orchestrator restarts a container that is fine. |
| OpenAPI docs | `/admin/api/docs`, `/admin/api/openapi.json` | **None at the core.** These are application-level routes and do not carry the admin router's dependency. The proxy is what keeps them private. |
| Admin API | `/admin/api/*` | **A bearer access key carrying the `write:admin` scope, and nothing else.** Not a session, not the trusted header. It used to be "whichever door is open", and that meant any household member with an account could read the trace — everybody's conversations — and issue themselves keys to `/v1`. The scope is off unless the administrator ticks it when issuing the key, so no existing key gained it. |
| Admin UI | `/admin/*` | Whichever single door is open (below), through the one dependency. Needs no key. |
| Sign-in and setup | `/admin/sign-in`, `/admin/setup`, `/admin/sign-out`, `/admin/api/auth/sign-in`, `/admin/api/auth/setup`, `/admin/api/auth/sign-out`, `/admin/sign-in.css` | **None**, and mounted **only** under the `builtin` door — nobody can be signed in while signing in. Every attempt is throttled and audited. Under any other door these paths do not exist. |
| Exposed API | `/v1/*` | Per-client bearer API key. |

## Which door is open — the precedence rule

One function decides, `personacore.auth.method.resolve_auth`, and everything
else is handed its answer (ADR-0023):

1. `PERSONACORE_ADMIN_DEV_USER` set → **`bypass`**. It wins over everything.
2. Otherwise `[auth] method` in `core.toml`, chosen under Sign-in on the Core
   settings screen: **`proxy`** (the trusted header) or **`builtin`** (the
   core's own accounts).
3. Default **`builtin`** (ADR-0024). Removing the bypass has to leave a door
   somebody can actually open; anyone running Authelia sets `proxy`, which is
   one line and a deliberate act.
4. An unrecognised value is a startup error, not a fallback — and the admin API
   refuses it at the write, with the same sentence.

The method is read **once, at startup**. The sign-in routes are mounted or not
mounted on the strength of it, so changing it takes effect when the core
restarts and the Core settings screen says so rather than pretending otherwise.
The bypass stays in the environment because it has to work when `core.toml`
cannot be read and when the UI cannot be reached — which is the moment it is
for.

Under `builtin` the identity header is **not read at all**; under `proxy` and
`bypass` the session cookie is not read at all. There is no state in which both
are accepted, which is what PC-294 asks for.

`/health` reports the whole decision as `admin_auth`: the method in force, the
method chosen underneath it, whether the bypass is open and who it names, the
trusted header and allowlist when they apply, and whether first-run setup is
still waiting.

## The core's own sign-in (`builtin`)

- Accounts in `<appdata>/users/accounts.json`. `hashlib.scrypt`, with the cost
  parameters stored beside every hash so they can be raised later. Nothing else
  about a person is kept.
- **No default password.** First run serves `/admin/setup`, which creates the
  first account — an admin — and closes the moment one exists.
- Sessions in `<appdata>/users/sessions.json`, server-side, addressed by a
  random token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` when the
  request arrived over HTTPS). Only the token's SHA-256 is stored. Several
  sessions per user at once; a seven-day absolute lifetime, and an expired
  session is refused and deleted rather than renewed.
- A user can end all of their own sessions; an admin can end all sessions for
  any account. **Only an admin sees the list of accounts** — a non-admin gets
  `403` with a reason, never an empty list.
- Sign-in attempts are audited as `access` records: who, when, from where,
  success or failure. Never the password.
- Failed sign-ins are slowed twice over: scrypt itself, paid even for an account
  that does not exist so timing cannot say whether one does, and a lockout after
  five failures per (account, address) pair backing off from 30 s to a
  15-minute ceiling.
- **Over plain HTTP the sign-in page says so**, on the page, and only when it is
  true of that connection.

## Proxy authentication and the trusted-header rule

The admin dependency reads the header named by `PERSONACORE_TRUSTED_USER_HEADER`, default `Remote-User`, and turns its value into the caller's identity.

It fails **closed**. A missing header is a `401`; so is an empty value, a value over 256 characters, and a value containing anything unprintable. A proxy misconfiguration that drops the header must lock the door rather than open it.

Header values are treated as untrusted input even when the proxy is trusted, because they end up in audit records and in HTML. Anything that is not a plain printable identifier is **refused rather than sanitised**.

## The trusted-proxy allowlist

`PERSONACORE_TRUSTED_PROXY_IPS` is a comma-separated list of peer addresses whose identity header is believed. It defaults to loopback: `127.0.0.1`, `::1`, `localhost`.

An HTTP middleware runs before any route sees a request and applies two rules:

1. **If the peer is not on the allowlist, the identity header is stripped** from the request entirely.
2. If the development bypass is on, the header is then *overwritten* with that user — not merely defaulted — so the bypass can never be used to choose which user you are.

**What this defends against.** The admin API's whole auth model is "the proxy already authenticated this and set a header". That holds only if nothing else can set the header. A proxy is supposed to strip inbound copies, but the core is not entitled to assume one is in front: with host networking and no proxy, anyone on the LAN could simply send `Remote-User: admin` and be an admin. The allowlist makes the header meaningless from anywhere but the proxy.

Set it to the proxy's address when the proxy runs elsewhere — on a Compose network, the default loopback value covers nothing but the core itself.

The active allowlist is reported by `/health` as `trusted_proxies`, so it is checkable without shell access.

## The development bypass, and its danger

`PERSONACORE_ADMIN_DEV_USER`, when set, makes every admin request arrive as that user.

It exists because the admin surface is unusable until a proxy is wired in, and "unusable" is how a security control gets ripped out rather than configured. It is:

- **off unless explicitly set**,
- **announced loudly at startup** with a warning log line saying exactly what it does,
- **reported by `/health` as `admin_auth_bypass`** and as `admin_auth.method`, so it cannot quietly persist into a deployment that has a proxy.

It is also the **break-glass**: the way back into a container whose admin
password is lost, settable only by somebody who can already edit the Compose
file on the host. Turning on the core's own sign-in deliberately does **not**
disable it — removing the recovery path at the moment it is needed would be the
worse failure (PC-294).

**With it on, anyone who can reach this port is an admin.** Set it only on a network you control, and unset it the moment a login proxy is in front.

## API keys

The exposed `/v1` surface takes `Authorization: Bearer <key>` and nothing else. No anonymous access, even on the LAN.

- Keys live in `<appdata>/users/api-keys.json`, written atomically with `0o600`.
- **Only a SHA-256 of the key is stored.** The plaintext exists exactly once, in the `POST /admin/api/keys` response, and is not recoverable afterwards by any call. Losing it costs one re-issue; storing it would cost the household its front door.
- Not a KDF, on purpose: a key is 256 bits of `secrets.token_urlsafe` entropy, so there is no dictionary to run against it, and a slow hash on every request would spend the latency budget for nothing. That reasoning holds only because nothing but the store mints keys — a human-chosen key would need a KDF.
- Verification is constant-time and does not stop early, so timing reveals nothing about how close a guess was.
- A malformed key file becomes "no keys", loudly, rather than a `500` that confirms to a prober that there is a real store back here.
- The policy travels *with* the key, so authentication and authorisation are one lookup and no code path can authenticate a key and then go looking for its permissions elsewhere. See [Policy Profiles](Policy-Profiles).
- Revocation deletes the record rather than flagging it; the audit log already holds what that key did.
- All four failure modes produce one byte-identical `401`. See [OpenAI-Compatible API](OpenAI-Compatible-API).

## Secrets and scoping

Secrets are files in `<appdata>/secrets/<name>`, one secret per file, containing only the value. That shape is what Docker secrets produce, what a mounted env-file can be rendered into, and what a vault agent can write. Nothing is ever in code, in an image, or in git.

- `core.toml` and a plugin's `config.toml` reference a secret **by name** through a `*_secret` field. The admin API refuses to read *or* write a document containing a credential-shaped key (`api_key`, `access_token`, `passphrase` and similar), with a message telling you to use the `_secret` field instead.
- Reading a secret returns a `SecretStr`, so an accidental log line or `repr` prints a mask; callers must call `.get_secret_value()` deliberately, which makes the leak-prone moment visible in review.
- Secret names must match `^[A-Za-z][A-Za-z0-9_.-]{0,63}$`. Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with or without an extension) are rejected on every platform, because "writing a file called CON" opens a device instead and can hang rather than fail cleanly.
- A trailing newline is stripped — it is what every editor and `echo` adds and is almost never part of the secret, and it saves a class of authentication failure that is miserable to diagnose from a `401`.

**Scoping is the rule that matters.** A plugin never sees the whole store. It gets a view containing only the names its manifest declared, and that view offers no way to enumerate the wider store or to widen itself; the only widening path is editing the manifest, which is visible in the admin UI and in review.

One subtlety worth knowing: a requested name must appear among the directory's real entries **byte for byte**. On a case-insensitive filesystem (NTFS, default APFS) the string check and the file open would otherwise disagree, and a manifest declaring `LLM_KEY` would be handed the file `llm_key` — a secret it never declared. The check is unconditional, because a least-privilege boundary that is only present on some hosts is not a boundary.

## Plugin least privilege

- A stdio plugin's environment is **built, never inherited**: a fixed allowlist of "needed to run" variables, two forced Python settings, and exactly the secrets its manifest declares. Nothing `PERSONACORE_*`, no `PYTHONPATH` (which would let a plugin be pointed at the core's own modules), no `HOME`/`USERPROFILE`. See [Environment Variables](Environment-Variables).
  - **Caveat:** the MCP SDK merges the built environment over its own `get_default_environment()`, which re-adds the same class of variables from the parent process. The allowlist is the intent; it is not a hermetic seal.
- The working directory is the plugin's own folder, so a relative interpreter path like `.venv/bin/python main.py` resolves. Absolute paths in `entry` stay rejected — they are how a plugin points at something outside its folder.
- Installation never executes anything from a package: no setup script, no import, no `pip install`. Validation happens in a staging directory inside appdata, and a package that fails validation leaves nothing on disk. Traversal, symlinks, zip bombs and manifest mismatches are all refusals with a reason.
- **`permissions.network` is declared and not enforced for stdio plugins** (ADR-0012). The manifest can say `network = ["api.open-meteo.com"]` and the core enforces nothing: a stdio plugin is a subprocess of the core sharing its network stack, and real egress control would need capabilities the container does not have. Treat the field as a *declaration* — reviewable before you install a plugin from the internet — not a boundary. Network segmentation does work for HTTP-transport plugins, which have their own containers.

## Untrusted-content fencing

Everything from outside the trust boundary — tool results, recalled memories, event payloads — reaches the model through one module and nowhere else, wrapped in a fence that says in words that the enclosed text is data.

Two properties make the fence worth having:

1. **The fence marker carries a per-turn random token** (16 hex characters from `secrets`). A fixed marker is published in the source and therefore known to anyone who can get text into a tool result or a shared anonymous memory; they would only have to write the closing marker to make the rest of their payload look like trusted context. A token drawn fresh per turn cannot be guessed by content written before the turn started.
2. **Marker-looking text inside the content is defanged anyway** — `BEGIN_UNTRUSTED` becomes `B_E_G_I_N___U_N_T_R_U_S_T_E_D`. Belt and braces: even with a leaked token, the payload cannot close its own fence.

Content is truncated at 8,000 characters by default with a visible note saying how much was cut. The `source` label is defanged too, because a plugin picks its own tool names and the header is part of the prompt.

**This does not make prompt injection impossible — no delimiter does.** It makes the boundary explicit and machine-checkable, which is what the spec asks for.

Two related placements: recalled memory goes in as a `user`-role message rather than a system one, because untrusted data does not belong in the highest-privilege slot (see [Personas](Personas)); and a policy refusal fed back to the model is *not* fenced, because it is core-authored text and fencing it would tell the model to ignore it.

## What the audit log records, and what it never sees

Every tool call (refused or not, with arguments), every confirmation, every admin change, and every access refusal goes to the audit store. Every message in and out goes to the transcript store. See [Audit and Trace](Audit-And-Trace).

Conversation content and audit detail go to the **store**, never to the log stream. The audit store's own logging calls pass only ids, categories, surfaces and counts.

A redaction processor runs on **every** structured log record — not opt-in per call site. It replaces the value of any field whose *key* is a known-sensitive name (`api_key`, `token`, `password`, `authorization`, and similar, normalised for case and hyphens) and rewrites labelled shapes inside free text (`Bearer <value>`, `api_key=<value>`, `Authorization: <value>`). Exceptions passed as log fields are unwrapped and scanned too.

**What redaction does not catch: an unlabelled bare secret** — a raw key logged under an innocuous field name with no surrounding words. Shape-based detection of arbitrary high-entropy strings would flag ordinary conversation text constantly, so it is deliberately not attempted. Do not log a bare secret and rely on this processor.

Two more deliberate omissions:

- The health dashboard shows a **digest** of each LLM endpoint's configuration, never the base URL. A hand-edited `core.toml` can carry credentials in a URL (`http://user:secret@host`), and the dashboard is rendered, logged and screenshotted.
- The API-key listing shows neither the key nor its hash. A fingerprint of a credential on a screen is one screenshot away from being a hint.

## Fail-closed, everywhere

The pattern is consistent enough to state once: every branch that cannot establish permission refuses.

- No profile, or a disabled profile → the turn is refused and audited as an access event. Not an anonymous free-for-all.
- An unknown tool, an unrankable risk, a missing confirmation channel → refusal. See [Risk Levels](Risk-Levels).
- A missing or malformed identity header → `401`.
- A malformed key file → no keys.
- An anonymous policy profile that exceeds its ceilings → refuses to construct at all, rather than being silently corrected. A security control that changes state behind an admin's back is worse than one that makes the admin choose (ADR-0003).

## Known limits, stated plainly

- **Child-safety filtering is best-effort** (ADR-0005). Content filtering driven by a local model is defeatable. The transcript log is the control that actually works.
- **`permissions.network` is not enforced for stdio plugins** (ADR-0012).
- **Redaction misses an unlabelled bare secret.**
- **No rate limiting is implemented** on any surface, though every profile carries a `rate_limit`.
- **The broker password is the one credential kept in `core.toml`** (`[bus].password`), because making an operator hand-create a secret file before they can type an MQTT password is what produced a real incident: the password went into the free-text box labelled *"Password secret"*, which wanted a name. It is write-only in every direction — redacted out of `GET /admin/api/config` and out of the raw config editor, never rendered into the settings form, and a save that carries the redaction marker back leaves the stored value untouched. `bus_password` and `bus.password` are both in the log redaction key set. Nothing else has moved: the LLM API key and every plugin secret keep the reference-by-name design.
- **A missing `bus.password_secret` degrades the bus rather than stopping the core.** The secret is resolved and passed to the bus at assembly, but if it cannot be read the bus connects unauthenticated instead of blocking startup — by design, since the bus is a degradable dependency. `/health`'s `bus_password_degraded` field says when this has happened. See [Event Bus](Event-Bus).
- **TLS and login are the proxy's job.** Without one in front, this stack is not secure and is not meant to be.
