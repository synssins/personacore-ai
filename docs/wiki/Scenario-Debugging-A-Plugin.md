# Scenario: Debugging a Plugin

Your plugin is not doing what you expect, and you want to find out which of three completely different problems you actually have.

The three shapes, in the order they get harder:

1. **It will not start.**
2. **It starts and does nothing.**
3. **It runs, but its tool is never called.**

They look similar from the chat box and have opposite fixes. Work out which one you have before changing anything.

---

## The diagnostic that settles it

Under the reply in the admin surface's chat box is a line reporting **how many tools were offered to the model and which it called**. Read it first, every time.

| Line | What it means | Which problem |
|---|---|---|
| *"0 tools offered · the model called none of them"* | Nothing reached the model. No plugin is running, or none of its tools are in this caller's allowlist. | 1 or 2 |
| *"3 tools offered · the model called none of them"* | The schema went out. The model declined, which is not a fault. | 3 |
| *"3 tools offered · the model called weather.get_forecast"* | The whole path works. | Neither |

This line exists because of a specific afternoon: someone asked for the weather, was told the assistant had no tools, and had no way to distinguish a plugin whose schema never went out from a model that was offered it and declined. Those look identical in the reply and have opposite fixes.

---

## 1. It will not start

### Is it even in the list?

The plugin list returns **two** lists: what loaded, and what failed with a plain-English reason naming the file and the offending key. One bad plugin never takes the scan down — it is listed beside its error.

```bash
curl -sS http://127.0.0.1:8053/admin/api/plugins -H "Remote-User: admin"
```

If it is in neither list, the core never saw the folder. Check:

- It is in `<appdata>/plugins/` — the **appdata** volume, not the repository. A folder in the source tree is not installed.
- The folder name does not begin with `_` or `.`. Those are deliberately not treated as plugins, which is how `_template` sits in the plugins directory without being run.
- An `http`-transport plugin belongs in `<appdata>/plugins-http.d/` instead.
- You hit reload after copying it.

### Load failures, and what each means

| Message | Cause |
|---|---|
| `missing manifest.toml` | Wrong folder level — the manifest sits directly in the plugin folder. |
| `folder name … does not match … plugin name` | Rename one to match the other. This is the most common first error. |
| `not valid TOML` | Quote your strings; the message names the line. |
| `'permissions.secrets' Extra inputs are not permitted` | A typo'd or invented key. The manifest model forbids unknown fields on purpose. |
| `declares transport 'http' but was found under plugins/` | Move it to `plugins-http.d/`. |
| `this plugin was written for plugin contract '3.x' and this core implements '2.1' -- a different major version, which is incompatible` | See [Plugin Contract Versioning](Plugin-Contract-Versioning). |
| `name … is declared more than once` | Two folders claim the same name. **Neither** loads. |
| A message naming a secret | It declares a secret that is not in the store. See [Scenario: A Plugin With a Secret](Scenario-Plugin-With-A-Secret). |

### Reload actually restarts things

Reload does two things in order: it tells the supervisor to start, stop and restart plugins to match what is now on disk, **and then** rescans the listing. Without the first step a copied-in plugin appeared in the list with no live status and never ran — that was a real defect, found by running the P0 gate rather than by any test.

So if a plugin appears but has no live status, you are probably on an old build or looking at a cached page. Reload again.

---

## 2. It starts and does nothing

Read the **state** and the **detail** beside it. There are five states, kept to five on purpose:

| State | Means | Reported by the admin API as |
|---|---|---|
| `starting` | Connecting for the first time, or waiting out a restart backoff | `starting` |
| `healthy` | Connected, handshake done, tools match the manifest | `running` |
| `degraded` | Loaded but not currently answering; a restart is on its way | `crashed` |
| `failed` | Not running and not coming back on its own; always carries a reason | `crashed` |
| `disabled` | Deliberately not running | `stopped` |

**`degraded` is not "fine".** It includes a plugin that has *never* successfully started — a config error that stops it dead reads as `degraded` while the supervisor retries it. It is deliberately **not** reported as running: a healthy-looking green row next to a plugin that cannot run at all once sent someone hunting a fault that was not there, and the reason was sitting in the `detail` field the whole time.

Tools stay listed while a plugin is `degraded`, because the very next call may well succeed. The "say so plainly" happens at call time rather than by hiding the tool.

### The backoff, and when it gives up

A crashed plugin is killed, logged and restarted with exponential backoff — starting at 1 second, doubling, capped at 60. After **5 consecutive failed attempts** it becomes `failed` and nothing further is attempted until a human reloads or fixes it. An endless restart loop is worse than a dead plugin: it hides the fault and burns CPU the rest of the house is sharing.

`restart_count` on the plugin's health tells you which of those you are watching.

### Common causes of a plugin that starts and stops

- **Its own config validation failed.** The core does not validate what your settings mean — your plugin does, at startup. Its message is what appears in `detail`. Look there first.
- **It printed to stdout.** On stdio, **stdout is the MCP conversation**. Anything printed to it corrupts the protocol. Diagnostics go to stderr, always.
- **Its `entry` command is not runnable.** The command is relative to your own folder, which is also the working directory the host sets. `python main.py` takes `python` from `PATH`; a plugin shipping its own interpreter writes `.venv/bin/python main.py`.
- **A missing dependency.** Your process is not the core's process and does not share its site-packages unless the interpreter you named does.
- **A declared secret is absent.** Refused before your code runs.
- **The manifest and the running server disagree.** A manifest declaring a tool the server does not expose is a load failure, not a warning.

### One failure mode that used to lie

The MCP SDK raises one error type both for "the server answered with an error" and for "the connection is dead". A killed plugin therefore looked like a live one politely declining — forever, with no restart and nothing visible in the UI. On that path the plugin is now asked whether it is still there. If you are running a build from before the P0 gate, this is why a dead plugin looked healthy.

---

## 3. It runs, but its tool is never called

You have `N tools offered … none called`. The schema reached the model. Work down this list.

### Is the tool in the caller's allowlist?

`allowed_tools` is an **allowlist**, and empty means none — not "all the safe ones". Installing a plugin never widens what a caller can do. Use the **qualified** name: `weather.get_forecast`, not `get_forecast`.

The admin chat box is the one exception: it is granted exactly the installed `safe` tools, deliberately, because an admin trying the assistant and silently getting nothing would conclude the tools were broken.

See [Policy Profiles](Policy-Profiles).

### Is it above the caller's ceiling?

`max_tool_risk` clamps what may run, and safe mode clamps it further. A `confirm` tool is refused for a `safe`-ceiling caller — and today it is refused for *everyone*, because no confirmation channel is wired. See [Scenario: Confirm and Restricted Tools](Scenario-Confirm-And-Restricted-Tools).

### Is the description any good?

**The tool's `description` is read by the model, and it is how the model decides whether to call you.** Vague descriptions are the single most common reason a working tool never gets called. Say what the tool does *and when to use it*, in plain words.

### Does the persona forbid it?

A persona describes **manner**; capability is the tool list. A persona saying "you have no tools connected" will be obeyed, and the model will refuse to use tools it demonstrably has. That exact sentence sat in the starter persona once — true when written, false a week later — and it cost real time. Check the prompt before blaming the plumbing.

### Does the model support tool calling at all?

This is the one that catches people last and should be checked first when everything else looks right. During the P0 phase gate: the tool schema was captured on the outgoing request, the plugin answered a direct call with real data, and the model declined and said it could not.

That is the model, or its llama.cpp chat template — not the pipeline. The fix is a tool-capable model in the `interactive` role. See [Scenario: Connecting an LLM](Scenario-Connecting-An-LLM).

---

## Where the evidence is

| Question | Where to look |
|---|---|
| Did the core see the folder? | The plugin list's failed-plugins array |
| Why did it stop? | The plugin's `detail` / `last_error` |
| What did the agent actually do? | The trace view — every tool call with arguments and outcome, every refusal with its reason. See [Audit and Trace](Audit-And-Trace) |
| Is the LLM up, is the bus up, is the disk full? | [Health and Diagnostics](Health-And-Diagnostics) |
| Startup problems, surface mounts, auth posture | Container logs, and `/health` |
| Does the tool work at all, independent of the model? | Call it directly through the plugin's own tests — see [Plugin Testing](Plugin-Testing) |

A tool that answers a direct call and is never called by the model is not a plugin bug.
