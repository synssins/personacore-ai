# Plugin HTTP Transport

When to run a plugin as its own container instead of a subprocess, how to register it, and everything that genuinely differs. Read this if stdio is not going to work for you — and read [Plugin Contract](Plugin-Contract) first if you have not decided yet.

Source: `src/personacore/plugins/discovery.py`, `src/personacore/plugins/mcp_client.py`. Spec §5.1, §7.

## When to choose it

**Choose stdio unless you have a reason not to.** No port, no container, no network exposure, and the plugin lives and dies with the core.

Reasons to choose http:

- The plugin is **heavy** — a vision model, something with a large warm-up. A stdio plugin has 20 seconds from spawn to listing its tools; a service you start yourself has as long as it likes.
- It is **written in something other than Python**, or needs dependencies you would rather not install next to the core.
- You want it **at arm's length** — a less-trusted plugin behind a Compose network boundary, which is the one place network permissions can actually be enforced (spec §7, ADR-0012 item 3).
- It is genuinely a **shared service** that other things in the house already talk to.

Repackaging a stdio plugin as an HTTP one is a change of two manifest lines and how you start it. The core treats the two identically once discovered: same manifest schema, same risk levels, same namespace, same health rows, same audit records.

## Registration

An HTTP plugin's *code* lives wherever you run it. What goes into appdata is a **registration**: a folder that mirrors the stdio layout, minus the code.

```
<appdata>/plugins-http.d/<name>/
    manifest.toml     required
    config.toml       optional — only if you want the admin UI to edit it
```

```toml
[plugin]
name        = "vision"
version     = "0.1.0"
contract    = "2.x"
transport   = "http"
url         = "http://vision:8080/mcp"
description = "Describes what a camera can see."

[permissions]
network = []
secrets = []
paths   = []

[tools.describe_scene]
risk = "confirm"

[events]
publishes  = []
subscribes = []
```

The folder name must equal `plugin.name`, exactly as for stdio. Declaring `transport = "http"` and putting the folder in `plugins/` is a load failure telling you which way to move it, and vice versa.

Names must be unique **across both directories**. A name declared in each loads neither, and installing a package into that state is refused ([Packaging](Plugin-Packaging)).

> The registration layout mirroring stdio — a manifest plus an optional `config.toml` in a per-plugin folder — is the core's own choice. Spec Appendix A/B are explicitly illustrative rather than final on this point; the code in `discovery.py` is the authority.

## `url`

Required, and checked when the core connects rather than at load time:

> Plugin 'vision' declares the address 'file:///x', which is not an http:// or https:// URL. HTTP plugins are reached over the network and nothing else.

Only `http` and `https` are accepted, case-insensitively. Anything else fails closed.

The core connects with the MCP **streamable HTTP** client, so the URL should be your MCP endpoint, not your service root.

## What differs from stdio

| | stdio | http |
|---|---|---|
| Started and stopped by | the core | **you** |
| `entry` | required | ignored |
| `url` | ignored | required |
| Working directory | your plugin folder | whatever you gave your own process |
| Environment | built from an allowlist by the core | **entirely yours** |
| **Declared secrets delivered** | **yes** | **no** — see below |
| stderr captured and surfaced in the admin UI | yes, size-capped | **no** — only the transport error is shown |
| `permissions.network` enforceable | no (ADR-0012) | in principle, via Compose networks — see below |
| Health, restart, backoff, heartbeat, timeouts | yes | **yes**, identically |

### Secrets are not delivered to an HTTP plugin

**This is the one that catches people.** `build_child_environment()` is called only on the stdio path. An HTTP plugin's `permissions.secrets` list is validated, shown in the admin UI, and then never acted on — there is no channel through which the core could hand a secret to a process it did not start.

An HTTP plugin gets its credentials the same way any other container does: Docker secrets, an env-file outside the image, or a vault agent — the same secrets facility, reached directly rather than through the core (spec §7).

Practical consequence: **declare `secrets = []` on an HTTP plugin.** Declaring names you will never receive is a manifest that lies to whoever reviews it. (It is not a load failure — nothing checks — which is precisely why it is worth saying here.)

### Diagnostics are thinner

For a stdio plugin, whatever the process printed to stderr is captured and appended to the failure message — usually the entire diagnosis. There is no equivalent for HTTP. A failed connection reports only what the transport said:

> could not reach http://vision:8080/mcp: &lt;the connection error&gt;

Your service's own logs are where the answer lives, so make sure they are somewhere the operator can reach.

### Network enforcement: what is actually true today

ADR-0012 records that Compose network segmentation is the one approach that genuinely works for HTTP plugins, and spec §7 calls for it: *"HTTP plugins get network segmentation via Compose networks."*

**The shipped `compose.yaml` defines no custom networks.** Everything sits on the default Compose network. If you want the isolation ADR-0012 describes, you build it yourself in your own Compose file — put the plugin on a network that reaches only what it needs, and leave it off any network that reaches the LAN.

Do not read `permissions.network` on an HTTP plugin as a thing the core is doing for you. It is not, on either transport.

## The lifecycle you still get

Everything in [Lifecycle](Plugin-Lifecycle) applies, because the supervisor is transport-agnostic:

- The connection is opened, the MCP handshake completed, and `list_tools()` run — all inside `startup_timeout` (20 s).
- The tool list is **reconciled against the manifest**, and a mismatch in either direction is a terminal load failure. This bites harder on HTTP: you can redeploy your service with a new tool and break its registration without touching appdata.
- The connection is held open with a heartbeat ping every 30 s.
- A dropped connection is a crash: recycled, backed off (1s, 2s, 4s, 8s, 16s), terminal after 5 consecutive failures.
- Health states, `last_error` and `next_retry_at` all behave identically.

**What the core will not do is start your service.** If it is down, the plugin is `failed` or `degraded` with a connection error, and it comes back when your service does — up to the restart limit, after which a human has to press reload.

## The one place the two are compared in tests

`tests/plugins/conftest.py` exercises the HTTP transport through the *same fake session* the stdio one uses, and says why: what the host promises is that the two are indistinguishable to a caller, which is a statement about the host rather than about `httpx`. Nothing in the plugin test suite opens a socket. See [Testing](Plugin-Testing).

## See also

[Plugin Contract](Plugin-Contract) · [Manifest](Plugin-Manifest) · [Runtime Environment](Plugin-Runtime-Environment) · [Lifecycle](Plugin-Lifecycle) · [Events](Plugin-Events)
