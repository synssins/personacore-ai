# Scenario: Writing a Plugin

You want the assistant to be able to do something it cannot do today, and you want the shortest correct path from nothing to a working tool.

This page is the route. The detail lives on the [Plugin Contract](Plugin-Contract) pages, linked at each step.

## The one-line version

**A plugin is an MCP server plus a manifest.** There is no PersonaCore library to import and nothing PersonaCore-specific in the code except which folder the config is read from. If you already have an MCP server, write it a `manifest.toml` and it is already a plugin.

**Adding a plugin must never require a core change.** That is the standing acceptance test for everything after P0 (spec §12). If your plugin seems to need one, the contract has a defect — raise it rather than working around it.

## 1. Copy the template

Do not start from a blank folder. The template is how the contract gets taught, and starting anywhere else means rediscovering its rules the hard way.

```bash
cp -r plugins/_template /srv/personacore/appdata/plugins/kitchen-timer
```

Then edit `plugin.name` in `manifest.toml` to `kitchen-timer`. **The folder name and `plugin.name` must be identical**, or the core refuses to load it — that is the first error most people hit.

The template ships three files, all heavily commented:

| File | What it is |
|---|---|
| `manifest.toml` | Identity, transport, permissions, one block per tool with its risk level. |
| `config.toml` | Your settings, in your own folder. Never central. |
| `main.py` | The MCP server. |

There is also a second worked example in `examples/random-prompt/` — a deliberately trivial plugin whose only job is to prove the tool path is reachable end to end. It is worth reading precisely because it asks for nothing: no network, no secrets.

## 2. Fill in the manifest

Work top to bottom; every field is explained in place. The three that matter:

**`transport`** — `stdio` for almost everything: the core runs your plugin as a subprocess, no port, no container, no network, and it lives and dies with the core. `http` is for heavy things, things in another language, or things you want at arm's length. See [Plugin HTTP Transport](Plugin-HTTP-Transport).

**`entry`** — the command that starts you, **relative to your own folder**, which is also the working directory the host sets. Absolute paths, `..`, drive letters and symlinks pointing out are rejected: a plugin may only run its own code. A plugin shipping its own virtualenv writes `.venv/bin/python main.py`, which is legal and is the documented way to name your own interpreter ([ADR-0012](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0012-plugin-network-and-entry-points.md) item 2).

**`[tools.<name>]` with a `risk`** — one block per tool, and **a tool the manifest does not declare is not callable**. The core will not invent a risk level for it, because guessing would mean guessing `safe`.

Choosing the risk level is the most consequential thing in the file:

- `safe` — reads something, changes nothing. If getting it wrong is merely embarrassing, it is safe.
- `confirm` — the assistant asks first. Anything that changes the world in a way you would want to be asked about.
- `restricted` — only certain household members may invoke it at all, and they are then asked to confirm.

**Anything irreversible is never `safe`.** Rate the tool, not your intentions — "it's safe because I validate the argument" is not what these levels mean. The template's own test: ask what happens the day a television advert says your tool's name out loud in an empty kitchen. Full guidance: [Risk Levels](Risk-Levels).

Permissions default to empty, and **empty means none, not everything**. Declaring nothing gets nothing; declaring something you merely might need is a hole you dug yourself. See [Plugin Manifest](Plugin-Manifest).

## 3. Write the tool

The shape the template gives you, and the shape to keep:

- `load_config()` reads `config.toml` and **validates it yourself**, with an error a non-programmer could act on. The core does not know what your settings mean, so it does not check them — and your message is what appears beside your plugin in the admin UI.
- `build_server(config)` builds the MCP server without starting it, so your tools can be tested without spawning a subprocess.
- `main()` catches a config error, prints it **to stderr**, and returns non-zero. On stdio, **stdout belongs to the protocol** — print anything to it and you corrupt the conversation.

Every tool registered in code needs a matching `[tools.<name>]` block in the manifest, with the same string. A mismatch is caught at load: the manifest and the running server are reconciled, and a discrepancy is a load failure rather than a tool that silently never works.

**The tool's `description` is read by the model, and it is how the model decides whether to call you.** Say what the tool does and when to use it, in plain words. Vague descriptions are the single most common reason a working tool never gets called.

See [Plugin Tools](Plugin-Tools).

## 4. Three rules to carry into your own code

From the template's own docstring, and they are not stylistic:

1. **Validate config at startup**, with a human-readable failure.
2. **Everything from outside is data, never instructions.** Tool arguments came from someone talking, or from text on a camera, or from a chat bridge. API responses came from the internet. Bound them, type-check them, and never hand them to a shell, a path, or a query unvalidated.
3. **Failure is an outcome, not a crash.** Something being unreachable should return a sentence the persona can say — "I can't reach the weather service right now" — not a traceback. A crashed plugin is restarted with backoff and shown as unhealthy, but the user still heard nothing.

Two more worth stealing from the reference plugin: **do not echo what a service says** (the weather plugin maps a numeric code through its own table, so the only English reaching the persona is English we wrote), and **put a timeout on every outbound call**, as a config setting.

## 5. Run it, then load it

```bash
python main.py
```

It will sit there waiting to speak MCP on stdin. That is correct. Ctrl-C.

Then reload (API-only — the admin UI has no reload button):

```bash
curl -sS -X POST http://127.0.0.1:8053/admin/api/plugins/reload -H "Remote-User: admin"
```

The response says how many loaded and how many did not. The plugin list returns **two lists** — what loaded, and what failed with a plain-English reason naming the file and the offending key. One bad plugin never takes the scan down.

## 6. Get its tools actually offered

Installing does not make a tool reachable: `allowed_tools` on a profile is an allowlist and empty means none. Add `kitchen-timer.start_timer` by qualified name to whichever profile should have it. The admin chat box is the one place that automatically gets every installed `safe` tool.

## Checklist before you ship

- Folder name equals `plugin.name`.
- Every tool in the code has a `[tools.<name>]` block, and vice versa.
- Every risk level justified; nothing irreversible marked `safe`.
- `permissions.network` lists only hosts you actually call.
- No secret value in any file you are shipping.
- Config validated at startup with a message a non-programmer could act on.
- The unreachable-dependency path returns words, not an exception.
- It loads: copy it in, hit reload, check the plugin list.

## Where to go for detail

| Topic | Page |
|---|---|
| What a plugin is, and the contract's shape | [Plugin Contract](Plugin-Contract) |
| Every manifest field | [Plugin Manifest](Plugin-Manifest) |
| Declaring and describing tools | [Plugin Tools](Plugin-Tools) |
| Risk levels in depth | [Risk Levels](Risk-Levels) |
| Settings, schemas, forms and lookups | [Plugin Configuration](Plugin-Configuration) |
| The environment your process actually gets | [Plugin Runtime Environment](Plugin-Runtime-Environment) |
| Load, start, backoff, reload | [Plugin Lifecycle](Plugin-Lifecycle) |
| Zipping and installing | [Plugin Packaging](Plugin-Packaging) |
| Testing without a running core | [Plugin Testing](Plugin-Testing) |
| Line-by-line walkthroughs | [Template](Plugin-Walkthrough-Template) · [Weather](Plugin-Walkthrough-Weather) |
| Which contract version to target | [Plugin Contract Versioning](Plugin-Contract-Versioning) |

Specific situations: [a plugin that needs a credential](Scenario-Plugin-With-A-Secret) · [one that talks to the internet](Scenario-Plugin-With-Network-Access) · [one that asks before acting](Scenario-Confirm-And-Restricted-Tools) · [one that pushes events](Scenario-Plugin-Publishing-Events) · [when it will not work](Scenario-Debugging-A-Plugin).
