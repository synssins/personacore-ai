# Scenario: Putting Authelia in Front

You want a real login on the admin interface, and you want to understand exactly what the core is trusting when you do it.

Authelia is the chosen provider ([ADR-0001](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0001-spec-15-open-decisions.md) item 5): Keycloak is a Java stack, heavy for a CPU-only home box and more than a household needs. Nothing here is Authelia-specific, though — any proxy that authenticates and sets a header works the same way.

## The model, in one sentence

**Under the `proxy` door, a reverse proxy authenticates the user and tells the core who arrived, in a header — and the core believes that header only from an address on an allowlist.**

There is deliberately no OIDC implementation in the core. Two places that decide who you are is one place too many — which is also why choosing this door switches the core's own sign-in **off** rather than leaving it beside this one.

## What the pieces do

```
browser ──TLS──▶ proxy ──plain HTTP──▶ core :8053
                  │                      │
                  │ terminates TLS       │ /admin  → trusted header
                  │ performs OIDC login  │ /v1     → API keys
                  │ STRIPS inbound       │ /health → open
                  │   Remote-User        │
                  └─ sets Remote-User    │
```

Three conditions have to hold together. Remove any one and the model collapses into "anyone may claim to be anyone":

1. **The proxy strips any inbound copy of the identity header** before setting its own. A client sending its own `Remote-User` must not have it survive.
2. **The core's port is not reachable except through the proxy.** The shipped `compose.yaml` binds the published port to `127.0.0.1` for this reason, with a comment saying not to publish it to anything you do not control.
3. **The core only honours the header from an allowlisted peer.** This is the core's own backstop, and it is on by default.

## 1. Configure the proxy

Two route classes, and they need different treatment:

| Path | Auth | Notes |
|---|---|---|
| `/admin` and `/admin/api` | **OIDC login required**, then set `Remote-User` | The whole admin surface. |
| `/v1` | **No OIDC.** Pass through | Authenticated by API key at the core. Putting a browser login in front of it breaks every OpenAI client. |
| `/health` | Your choice | Unauthenticated at the core by design, so the container health check answers even when a surface is missing. Exposing it publicly leaks version and auth-posture information; keep it internal. |

Caddy is the approved proxy for this stack. Note that P0 ships a **static, hand-written proxy config**: [ADR-0009](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0009-proxy-configuration-in-admin-ui.md) puts proxy configuration inside PersonaCore's own admin UI eventually, driving Caddy's admin API — but that is built with the designed UI, not now. Today you write the config file yourself.

The Compose file deliberately does **not** include a proxy or an OIDC provider: the stack is meant for a host that already runs both, and a second copy means two auth providers to keep in step.

## 2. Tell the core which peer to trust

```yaml
      - PERSONACORE_TRUSTED_PROXY_IPS=<proxy-container-ip>
```

Comma-separated. The default, when unset, is `127.0.0.1`, `::1`, `localhost` — which covers a proxy on the same host and nothing else.

Every request passes through an identity guard **before any route sees it**: if the peer is not on the list, the identity header is stripped from the request. Not rejected, not logged as an attack — removed, so the route below sees an unauthenticated request and fails closed with a 401.

**Two things to know about the allowlist, because they will bite you:**

- **It is exact string matching on the peer address. There is no CIDR support.** A CIDR block such as `<network>/24` is not a range here; it is a string that will never match. List the actual addresses.
- **The peer is the immediate TCP peer.** If the proxy runs in the same Compose network, that is the proxy container's IP, which Docker may reassign when the container is recreated. Give the proxy a static address on that network, or run it on the same host and leave the loopback default. Getting this wrong produces a working proxy, a successful login, and a 401 from the core — which reads like the proxy is broken when it is doing its job perfectly.

Check what the core thinks:

```bash
curl -s http://127.0.0.1:8053/health
```

`trusted_proxies` in the response is the live list.

## 3. Choose the proxy door

The core defaults to **its own accounts** (ADR-0024), so a proxy in front changes nothing until you say so. On the **Core settings** screen, under **Sign-in**, choose *A login proxy in front*, save, and restart the core:

```bash
docker compose restart personacore
```

Or, hand-editing `<appdata>/config/core.toml`:

```toml
[auth]
method = "proxy"
```

It is **not** an environment variable and there is none to set. Confirm with `curl -s http://127.0.0.1:8053/health` — `admin_auth.method` must read `proxy`.

Under this door the core's own sign-in and setup pages are **not mounted at all** (404, not a guarded route), so there is no second way in beside the one the proxy controls.

## 4. Set the header name, if yours differs

```yaml
      - PERSONACORE_TRUSTED_USER_HEADER=Remote-User
```

`Remote-User` is the default and is what Authelia sets. Change it only to match a proxy that uses a different name.

**The value is validated, not sanitised.** It must be non-empty after stripping, at most 256 characters, and entirely printable. Anything else is a 401. Header values are untrusted input even when the proxy is trusted — they end up in audit records and in HTML, so anything that is not a plain printable identifier is refused rather than cleaned up.

A missing or empty header is a 401 telling you to open the interface at the proxy's address — and, because that is a dead end if there is no proxy, also telling you that this core has accounts of its own and how to switch to them, and that `PERSONACORE_ADMIN_DEV_USER` is the way back in if you cannot reach the screen that holds the setting.

That is the correct behaviour: a proxy misconfiguration that drops the header must lock the door rather than open it — but a locked door has to say where the key is.

## 5. Turn the development bypass off

If you used `PERSONACORE_ADMIN_DEV_USER` to get started ([Scenario: First Run](Scenario-First-Run)), **remove it now**.

```yaml
      # - PERSONACORE_ADMIN_DEV_USER=admin
```

```bash
docker compose up -d personacore
```

### Why it is dangerous

With it set, the identity guard **strips whatever arrived and sets the header to that user**. Every request to the admin API is that user. It is not a way to choose which user you are — it is a way for **anyone who can reach the port to be an admin**.

It exists for one honest reason: the admin surface is unusable until a proxy is wired in, and "unusable" is how a security control gets ripped out rather than configured. So it is:

- **off unless explicitly set**;
- **announced with a warning at startup**, saying in full that anyone who can reach the port is an admin;
- **reported by `/health` as `admin_auth_bypass`**, so it cannot quietly persist into a deployment that has a proxy in front of it.

Confirm it is gone:

```bash
curl -s http://127.0.0.1:8053/health
```

`admin_auth_bypass` must be `null`.

Note the deliberate asymmetry: the bypass **overrides** the allowlist. It sets the header regardless of peer. So a bypass left on plus a published port is a full compromise of the admin surface, and neither the allowlist nor the proxy will save you.

## 6. Verify

Try the core directly, bypassing the proxy, from a machine that is not on the allowlist:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Remote-User: admin' http://<your-host>:8053/admin/api/plugins
```

**401 is the correct answer.** If you get 200, one of the three conditions is broken — most likely the port is published where it should not be, or the client's address is on the trusted list.

Then log in through the proxy and confirm the admin surface opens.

## What this does and does not protect

**Protects:** the admin API and the admin UI — every settings change, plugin install, key issuance, persona edit and trace view.

**Does not protect:** `/v1`. That surface has its own authentication — per-client API keys, one byte-identical 401 for absent, malformed, unknown and disabled keys alike, so an unauthenticated caller cannot use error messages to map the surface. See [OpenAI-Compatible API](OpenAI-Compatible-API) and [Scenario: Third-Party Clients](Scenario-Third-Party-Clients).

**Also does not protect:** anything with a valid admin session. The trusted header names a user; the core does not currently distinguish admin roles beyond that. Everyone the proxy lets through is an admin.

## Related

- [Security Model](Security-Model) — the whole picture, including what is deliberately not implemented.
- [Environment Variables](Environment-Variables) — the three security-relevant variables in one table.
- [Health and Diagnostics](Health-And-Diagnostics) — reading the auth posture from one endpoint.
