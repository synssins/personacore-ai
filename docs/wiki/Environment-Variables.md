# Environment Variables

The complete list of environment variables PersonaCore reads, and why the list is deliberately short. Read this if you are writing a Compose file or a deployment script.

## Why the list is closed

Runtime settings — LLM host and model, bus, persona, retention — live in `core.toml`, which the admin UI reads and writes with validation. They are **not** settable from the environment, and this is a decision rather than an omission (ADR-0010, spec §4.4).

An environment variable is worse than a config file for anything a human tunes:

- It is invisible to the admin UI, while silently outranking what the UI shows.
- Changing it means editing the Compose file and recreating the container.
- It splits the truth about a setting across two places, and the one a human looks at first is the one that loses.

So the environment sets only what must be known **before any UI can exist**: where the settings themselves live, what address serves them, and who is allowed to assert an identity to them. Adding to the list requires a reason of the same kind.

## The list

| Variable | Read by | Default | What it does |
|---|---|---|---|
| `PERSONACORE_APPDATA` | `server.py`, `__main__.py`, `deploy/entrypoint.py` | `./appdata` (`/appdata` in the container entry point) | Where the appdata volume is mounted. Everything else is found relative to it. See [Appdata Layout](Appdata-Layout). |
| `PERSONACORE_HOST` | `server.py` (`serve`), `deploy/entrypoint.py` | `0.0.0.0` | Bind address for the HTTP listener. Overridden by `--host`; falls back to `[server].host` in `core.toml` if unset. |
| `PERSONACORE_PORT` | `server.py` (`serve`), `deploy/entrypoint.py` | `8053` | Listener port. Overridden by `--port`; falls back to `[server].port` in `core.toml` if unset. Bounded 1–65535; `0` is refused rather than treated as unset. |
| `PERSONACORE_TRUSTED_USER_HEADER` | `server.py` | `Remote-User` | Name of the header the reverse proxy sets to the signed-in user's id. |
| `PERSONACORE_TRUSTED_PROXY_IPS` | `server.py` | `127.0.0.1`, `::1`, `localhost` | Comma-separated peer addresses whose identity header is believed. From anywhere else the header is stripped before any route sees it. |
| `PERSONACORE_ADMIN_DEV_USER` | `server.py` | unset | **Development bypass.** When set, every admin request is treated as coming from that user. Dangerous — see below and [Security Model](Security-Model). |

The three security-relevant ones are covered in detail in [Security Model](Security-Model). In short: the trusted header is honoured only from an allowlisted peer, because without that allowlist anyone who can reach the port could send the header themselves and be an admin.

**Which way in is open is not here, and there is no variable for it.** It is `[auth] method` in `core.toml`, chosen under Sign-in on the Core settings screen (ADR-0024) — `builtin` for the core's own accounts, `proxy` for a trusted identity header. An operator picking between their own accounts and a login proxy needs the two explained side by side, which is a screen and not a Compose file. See [Core Settings](Core-Settings).

`PERSONACORE_ADMIN_DEV_USER` is off unless explicitly set, is announced with a warning log line at startup, and is reported by the unauthenticated `/health` endpoint as `admin_auth_bypass` so it cannot quietly persist into a deployment that has a proxy in front of it. With it on, **anyone who can reach this port is an admin.**

## Two more that exist in the code

Neither is in ADR-0010's table, and both are packaging concerns rather than runtime configuration:

| Variable | Read by | Default | What it does |
|---|---|---|---|
| `PERSONACORE_BUNDLED_PLUGINS` | `plugins/bundled.py` | `/opt/personacore/plugins` | Where the image keeps its read-only copy of the plugins that ship with the core. They are copied into appdata on first run and never overwritten. Absent outside a container, which is treated as normal. |
| `PUID` / `PGID` | `deploy/entrypoint.py` | `10001` / `10001` | The uid/gid the container drops to before serving, so appdata files are owned by something sensible on the host. |

## What the environment never sets

Anything in `core.toml`: `default_persona`, any `[llm.*]` field, `[bus]`, `[retention]`, `[auth]`. There is no override path for these — the comment in `config/settings.py` says so explicitly, and there is no code that would read one. If a setting seems not to be taking effect, the environment is not the reason.

The one exception that looks like one and is not: `PERSONACORE_ADMIN_DEV_USER` outranks `[auth] method`, but it does not *override* it — it is a third door of its own, reported separately by `/health`, and `admin_auth.chosen` still says which door you get back when you remove it.

Note also that the `[server]` section of `core.toml` is the **last** source consulted for the bind address, not the first: `serve()` resolves it as an explicit CLI flag, then `PERSONACORE_HOST`/`PERSONACORE_PORT`, then `[server]`. An unconfigured install binds `0.0.0.0:8053` either way, because that is both the environment default and `[server]`'s own default — but a `[server]` value only takes effect when both higher-priority sources are unset. See [Core Settings](Core-Settings).

## Plugin subprocesses do not inherit any of this

A stdio plugin's environment is **built, never inherited**. It gets an explicit allowlist — `PATH`, `PATHEXT`, `SYSTEMROOT`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LC_ALL`, `TZ` — plus `PYTHONUNBUFFERED=1` and `PYTHONDONTWRITEBYTECODE=1`, plus exactly the secrets its manifest declares. Nothing `PERSONACORE_*` is passed on, and neither are `PYTHONPATH` or `HOME`/`USERPROFILE`. Adding to that allowlist is a security decision.

One caveat: the MCP SDK merges the built environment over its own `get_default_environment()`, which re-adds the same class of "needed to run" variables from the parent process. See [Security Model](Security-Model).
