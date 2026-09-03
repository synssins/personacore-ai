# Plugin Contract

What the core expects from a plugin, and what it guarantees back. Read this first if you are writing a plugin; everything else in the plugin section is detail hanging off this page.

Source: `docs/persona-core-spec.md` §5.1, `src/personacore/contracts/manifest.py`, `src/personacore/plugins/`.

## A plugin is an MCP server

That is the whole contract. [MCP](https://modelcontextprotocol.io) — the Model Context Protocol — is a published standard for exposing tools to a language model. PersonaCore is an MCP **client**; your plugin is an MCP **server**.

There is no PersonaCore library to import, no base class to inherit from, and no API of ours to learn. If you already have an MCP server in any language, it is already a plugin — it needs a `manifest.toml` next to it and nothing else. The bundled plugins import only `mcp`, `pydantic` and the standard library; the only PersonaCore-specific line in either of them is which folder they read their config from.

The core speaks MCP through the official SDK (`src/personacore/plugins/mcp_client.py`). It never hand-rolls framing, the handshake, or request correlation. What it adds on top is the part the SDK deliberately leaves to the host: deciding what the child process is allowed to see.

## Two transports, treated identically

| | **stdio** | **http** |
|---|---|---|
| Where it runs | a subprocess of the core | its own service or container |
| Registered in | `<appdata>/plugins/<name>/` | `<appdata>/plugins-http.d/<name>/` |
| Manifest declares | `entry = "python main.py"` | `url = "http://host:port/mcp"` |
| Started by | the core | you |
| Receives declared secrets | yes | **no** — see [HTTP Transport](Plugin-HTTP-Transport) |

Once discovered, everything above `mcp_client.py` — the supervisor, the host, the agent loop — deals in the same three shapes (`RemoteTool`, `RemoteToolResult`, the `PluginSession` protocol) regardless of transport. The tool namespace, the risk gate, the audit record and the health row are identical.

Choose stdio unless you have a reason not to. Choose http for something heavy, something written in another language, or something you deliberately want at arm's length. [HTTP Transport](Plugin-HTTP-Transport) covers what genuinely differs.

## Manifest declares, core enforces

This is the sentence the whole design hangs on (spec §5.1). `manifest.toml` is a **request for privileges**, checked before any of your code runs. Nothing your code does at runtime can widen it.

It cuts both ways:

- A permission you forget to declare is one you do not get.
- A permission you declare and do not need is a hole you dug yourself.
- A tool you forget to declare has no risk level, so the core will not call it at all. It looks broken rather than absent — see [Tools](Plugin-Tools).

The manifest is also the *only* thing the core knows about your plugin before it runs, which is why every field in it is validated with `extra="forbid"`: an unknown key is an error you can see rather than a permission you silently never got.

### The reconciliation that surprises people

At load time the core lists the running server's tools and compares that set against the manifest's `[tools.*]` blocks. **They must match exactly, in both directions.** A manifest naming a tool the server does not implement, or a server exposing a tool the manifest never declared, is a `PluginContractMismatch` — a terminal load failure, not a restart, with a message naming both sides:

> Plugin 'weather' does not match its own manifest. manifest.toml declares [get_forecast]; the plugin offers [get_forecast, search_locations]. offered by the plugin but not declared in manifest.toml: search_locations. Fix whichever side is wrong — the core will not load a plugin whose manifest it cannot trust (spec 5.1).

This exists because the earlier behaviour was silence: the tool simply never appeared and the author had nothing to read (ADR-0012, item 2 of the "already fixed" list). `reconcile_tools()` in `src/personacore/plugins/supervisor.py`.

## What a plugin folder holds

```
<appdata>/plugins/<name>/
    manifest.toml        required — identity, permissions, per-tool risk
    config.toml          optional — your settings, edited by the admin UI
    config.schema.json   optional — turns those settings into a real form
    <your code>          whatever `entry` runs
```

Only `manifest.toml` is required. A folder without one is a load failure naming the missing file; a folder whose name starts with `_` or `.` is skipped entirely and is not an error (that is how `_template` can sit in the plugins directory without showing as a permanent red row).

The folder name **must equal** `plugin.name`, exactly. Full rules and every rejection message: [Manifest](Plugin-Manifest).

## What the core guarantees back

| Guarantee | Where it is implemented |
|---|---|
| Your working directory is your own folder, so `.venv/bin/python main.py` resolves. | `mcp_client.McpSessionFactory._connect_stdio` (`cwd=str(record.directory)`), ADR-0012 item 2 |
| You receive **only** the secrets your manifest names — never the core's environment, never the whole store. | `mcp_client.build_child_environment`, [Runtime Environment](Plugin-Runtime-Environment) |
| Your settings stay in your own folder. The core never stores plugin settings centrally. | spec §5.1, `discovery.PluginRecord.config_path` |
| A crash is contained: killed, logged, restarted with backoff, shown as unhealthy. A bad plugin never takes the core down. | `plugins/supervisor.py`, [Lifecycle](Plugin-Lifecycle) |
| A load failure is listed next to a plain-English reason rather than losing the whole scan. | `plugins/errors.py`, `discovery.DiscoveryResult` |
| Every tool call is audited: plugin, tool, arguments, outcome, duration, risk, correlation id. | `plugins/host.py` |
| Your tool's result reaches the model fenced as data, never as instructions. | `agent/untrusted.py`, [Tools](Plugin-Tools) |
| An upgrade never touches your installed `config.toml`. | `plugins/packages.py`, [Packaging](Plugin-Packaging) |

## What the core deliberately does not do

- **It does not validate your config.** It parses `config.toml` as TOML and hands the result to the admin UI. It does not know what your settings *mean*, so you validate them yourself, at startup, with a message a non-programmer could act on. See [Configuration](Plugin-Configuration).
- **It does not enforce `permissions.network` for stdio plugins.** Declared, reviewable, not a wall — ADR-0012. Stated again wherever network permissions come up, because a control that looks enforced and is not is worse than no control.
- **It does not enforce `permissions.paths` at runtime either.** The value is checked at load time (it must be relative, and must not point at the secrets directory) and then nothing reads it again. A stdio plugin is a subprocess of the core with the core's filesystem view.
- **It does not deliver events to plugins.** `events.subscribes` is recorded and, today, read by nothing. See [Events](Plugin-Events).

## The rest of the plugin section

- [Manifest](Plugin-Manifest) — every field, every rejection, every exact error message.
- [Runtime Environment](Plugin-Runtime-Environment) — what your process actually gets.
- [Tools](Plugin-Tools) — defining them, risk levels, results, errors.
- [Configuration](Plugin-Configuration) — `config.toml` and `config.schema.json`.
- [Lifecycle](Plugin-Lifecycle) — discovery, health, restarts, reload, enable/disable.
- [Packaging](Plugin-Packaging) — the zip, and what installation refuses.
- [Events](Plugin-Events) — the push channel.
- [HTTP Transport](Plugin-HTTP-Transport) — when and how to run as your own container.
- [Testing](Plugin-Testing) — how the bundled plugins are tested.
- [Contract Versioning](Plugin-Contract-Versioning) — what `contract = "2.x"` buys you, and why a `1.x` manifest no longer loads.
- [Weather walkthrough](Plugin-Walkthrough-Weather) and [Template walkthrough](Plugin-Walkthrough-Template) — the worked examples.

Start-to-finish narratives live in [Scenario: Writing a Plugin](Scenario-Writing-A-Plugin) and [Scenario: Installing a Plugin](Scenario-Installing-A-Plugin).
