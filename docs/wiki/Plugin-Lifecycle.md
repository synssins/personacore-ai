# Plugin Lifecycle

From a folder on disk to a running subprocess and back again: discovery, load, health states, timeouts, restarts, terminal failure, reload, and the enable/disable/uninstall switches. Read this when a plugin is not in the state you expect.

Source: `src/personacore/plugins/discovery.py`, `src/personacore/plugins/supervisor.py`, `src/personacore/plugins/host.py`, `src/personacore/plugins/health.py`.

## Discovery

`PluginDiscovery.scan()` reads two directories fresh, every time:

- `<appdata>/plugins/<name>/` — stdio plugins
- `<appdata>/plugins-http.d/<name>/` — HTTP registrations

Within each, every **directory** is a candidate; stray files at that level are ignored rather than treated as errors. A directory whose name starts with `_` or `.` is skipped — that is how `_template` and the `.staging` upload directory can live in `plugins/` without ever being scanned as plugins.

The scan **never raises for a single bad plugin**. It returns every success and every failure side by side (`DiscoveryResult`), so a broken plugin can be listed in the admin UI next to its error instead of taking the whole scan down (spec §5.1). Every check and every message: [Manifest](Plugin-Manifest).

Discovery does not launch anything and does not speak MCP. It produces `PluginRecord`s: name, directory, parsed manifest, manifest path, config path, and the parsed (unvalidated) config.

## Start

`PluginHost.start()` scans and starts everything found that is not on the disabled list. One `PluginSupervisor` owns exactly one plugin.

**The shape that makes this tractable: one task owns the connection.** The transport context managers are entered and exited in the same task (anyio requires it), that task holds them open, and callers use the published session object concurrently. Nothing outside that task ever touches the subprocess, so teardown has exactly one owner and cannot race.

One connection attempt is: connect (spawn the subprocess, or open the HTTP stream) → MCP handshake → `list_tools()` → reconcile against the manifest → stay up. Spawn, handshake and tool listing together are bounded by `startup_timeout`.

`start()` waits only for the **first attempt to resolve**, either way. A plugin that fails to start is a health row, not an exception, and core startup is never held hostage by a plugin that is going to spend the next minute backing off — the runner keeps retrying behind it. The outer wait is `startup_timeout + 5s`; if even that expires the plugin is noted as *"took longer than expected to start, and is still being watched"* and the runner carries on.

Plugins come up **after** the HTTP listener in `server.py`, so a slow or broken plugin delays tools rather than the whole service.

## Health states

Five, kept to five on purpose: every extra state is another row of the admin UI someone has to learn the meaning of.

| State | Meaning | Callable? |
|---|---|---|
| `starting` | Being connected for the first time, or waiting out a restart backoff. | no |
| `healthy` | Connected, handshake done, tools match the manifest. | yes |
| `degraded` | Loaded but not currently answering — a call failed or the connection dropped, and a restart is on its way. | **yes** |
| `failed` | Not running and not coming back on its own. Always carries `last_error` saying which. | no |
| `disabled` | Deliberately not running. Never an error. | no |

**`degraded` stays callable, and its tools stay listed.** The connection may already be back by the time the next call lands, and refusing outright would turn one bad answer into a permanently missing capability. Spec §10's "say so plainly" happens at call time rather than by hiding the tool.

A health row also carries `transport`, the current tool names, `restart_count`, `last_error` and `last_error_at`, `started_at`, `next_retry_at`, and `terminal`. **`terminal` is the difference between "wait a moment" and "go and fix it."**

Load failures appear as `failed` rows too, with the discovery message as `last_error` — a plugin whose manifest will not parse is exactly the plugin an operator is looking for, and leaving it out of the list is how it stays broken for a month.

`last_error` is plain English and safe to show verbatim. It never contains a secret value.

## Timeouts and restart policy

`SupervisorConfig` — every value is a defence, not a tuning knob.

| Setting | Default | Covers |
|---|---|---|
| `startup_timeout` | 20 s | spawn, handshake and tool listing, together |
| `call_timeout` | 30 s | one tool call |
| `shutdown_timeout` | 10 s | **per escalation step**: graceful, then cancelled, then abandoned |
| `backoff_initial` | 1.0 s | first restart delay |
| `backoff_factor` | 2.0 | multiplier per attempt |
| `backoff_max` | 60 s | ceiling on the delay |
| `max_restarts` | 5 | consecutive failed attempts before the plugin is written off |
| `heartbeat_interval` | 30 s | how often an idle plugin is pinged; `0` disables |

Backoff is `min(initial × factor^(attempt-1), max)` — so 1s, 2s, 4s, 8s, 16s, and then terminal. A plugin that keeps dying is written off within about a minute rather than restarting forever: **an endless restart loop is worse than a dead plugin.** It hides the fault and burns the CPU the rest of the house is sharing.

The heartbeat exists so a plugin that died quietly shows as unhealthy in the admin UI *before* somebody asks it for something.

The restart counter resets to zero on a successful connection.

## What "bad" covers

Spec §5.1's promise is that a bad plugin never takes the core down. "Bad" covers more than "crashes":

| What a bad plugin does | What happens |
|---|---|
| Fails to start | Attempt recorded, backoff, retry, terminal after `max_restarts`. |
| Crashes mid-conversation | The call becomes a failure *value*; the plugin is recycled. |
| Hangs forever | Every call is under `asyncio.timeout`; then recycled. |
| Never answers the handshake | Startup is under a timeout too; treated as a crash. |
| Floods stderr | Bounded, truncating capture file. |
| Speaks malformed MCP | Parse failures surface as transport errors → recycle. |
| Refuses to die | Escalated: stop, cancel, then abandoned and marked failed. The core keeps running with it written off. |
| Declares tools it lacks | Terminal load failure naming both sides — no restart. |
| Exits cleanly on its own | Treated as a crash. *"closed its connection"* — from the household's point of view it is one. |

That last row surprises people. A clean exit the core did not ask for is still a plugin that stopped working.

## Terminal failure

Four ways a plugin reaches `terminal = True`, meaning nothing further is attempted without human action:

1. **`PluginContractMismatch`** — the manifest and the running server disagree about which tools exist. Restarting cannot fix a manifest.
2. **`ChildEnvironmentError`** — a declared secret is missing or unreadable, or the plugin declared secrets and the core has no secret store. Retrying cannot conjure a credential.
3. **Exceeding `max_restarts`** — *"has been restarted 5 times and keeps failing, so it has been switched off. Last problem: …"*
4. **Refusing to shut down** — after both escalation steps the task is let go of: *"did not shut down when asked. It has been abandoned and is no longer used; restart the core to clear it."* The SDK's own teardown has already tried terminate-then-kill on the process tree by then; what is left is an OS-level zombie the core cannot do anything about except refuse to wait for it.

## Calling a tool

`PluginSupervisor.call()` is always bounded in time. On a timeout the plugin is noted as *"did not answer &lt;tool&gt; within 30 seconds"*, a recycle is requested, and the caller gets a transport error — **recycling has already been requested by the time the error is raised, so the next call has a chance of working.**

A `PluginToolError` — a live plugin answering "no" — does *not* recycle anything. Refusing a bad request is correct behaviour.

A successful call on a `degraded` plugin promotes it back to `healthy`. It answered; whatever was wrong is over.

## Reload

`PluginHost.reload()` — `POST /admin/api/plugins/reload`. There is no reload button in the admin UI; this is API-only.

**It is a diff, not a restart.** Plugins whose folder is unchanged are left strictly alone, including their subprocess and their uptime. Adding a plugin is copying a folder and pressing reload; it must not interrupt the music.

Per rescan the host computes:

- **stop** — running but no longer wanted (removed, or newly disabled)
- **start** — wanted but not running
- **restart** — running, wanted, and *materially changed*

"Materially changed" means the **manifest**, the parsed **config**, or the **directory** differs. Anything else — your plugin's own source files changing — is invisible to discovery.

**So editing `main.py` and calling reload does nothing.** Toggle the plugin off and on, or restart the core. (Saving a plugin's config through the admin UI *does* restart it, because the config changed.)

## Enable, disable, uninstall

**Disable** stops the plugin and removes it from the catalogue: its tools are no longer offered to the model and no longer callable. It stays on disk with its config. The state is persisted in `<appdata>/config/plugins-disabled.toml` so it survives a restart (ADR-0013).

That file is read strictly. If it exists and cannot be parsed, the core raises rather than treating it as "nothing is disabled" — quietly switching a plugin back on because its state file was corrupt is not an acceptable failure:

> The list of switched-off plugins at &lt;path&gt; could not be read: … Until it is fixed or deleted, the core cannot tell which plugins you turned off. Deleting the file switches all of them back on.

It is written atomically (temp file plus `os.replace`), because a half-written state file is a plugin whose on/off state is now a coin toss.

**Enable** clears the entry and triggers a reload.

**Uninstall** deletes the folder and everything in it, including `config.toml`. It refuses anything that is not a direct child of one of the two plugin directories, and refuses outright to follow a plugin folder that is a symlink — deleting through it would delete somebody else's files. A name that is uninstalled is also removed from the disabled list, so a later install of the same name does not arrive already off with nothing on screen explaining why. Full detail: [Packaging](Plugin-Packaging).

## First run: the bundled plugins

The weather plugin and the template ship inside the image at `/opt/personacore/plugins` (overridable with `PERSONACORE_BUNDLED_PLUGINS`) and are copied into `<appdata>/plugins/` on first run. Appdata is where plugins are read from, and a container's appdata starts empty.

The rule is **seed on first run, never overwrite**. A marker file `.installed-from-image` records what has been seeded, so a later start can tell "the operator deleted this" from "this was never installed". Without it, seeding on every start would silently resurrect a plugin somebody removed on purpose. An installed plugin belongs to the operator, including one they edited or deliberately deleted (spec §7 — an upgrade must not touch appdata content).

One plugin that will not copy is logged and skipped; it must not stop the core starting.

## See also

[Plugin Contract](Plugin-Contract) · [Runtime Environment](Plugin-Runtime-Environment) · [Tools](Plugin-Tools) · [Packaging](Plugin-Packaging) · [HTTP Transport](Plugin-HTTP-Transport)
