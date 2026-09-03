# Plugin Runtime Environment

What a stdio plugin's process actually receives when the core starts it: which environment variables, which secrets, which working directory, and which stream it must never print to. Read this before you debug a plugin that "works when I run it by hand".

Source: `src/personacore/plugins/mcp_client.py`, `src/personacore/config/secrets.py`.

This page is about **stdio** plugins. An HTTP plugin is started by you, not by the core, and gets none of this — see [HTTP Transport](Plugin-HTTP-Transport).

## `os.environ` is not inherited

The single most important fact on this page. A subprocess normally inherits its parent's environment. If the core did that, your plugin would receive the LLM host address, the core's own API key, every `PERSONACORE_*` setting, and anything else the operator exported into the container — while its manifest said `secrets = []`.

That would be a complete bypass of the least-privilege rule (spec §5.1: *"plugins receive only the secrets their manifest declares"*). So the child environment is **built, never inherited**:

```
environment = BASE_ENV_KEYS (read from os.environ by name)
            + FORCED_ENV
            + exactly the secrets permissions.secrets declares
```

`build_child_environment()` reads `os.environ` only through the allowlist below. Nothing else crosses.

### `BASE_ENV_KEYS` — the entire inherited surface

| Variable | Why it is allowed |
|---|---|
| `PATH` | finding an interpreter |
| `PATHEXT` | same, on Windows |
| `SYSTEMROOT` | Windows process startup |
| `COMSPEC` | Windows process startup |
| `TEMP` | a temp directory |
| `TMP` | a temp directory |
| `TMPDIR` | a temp directory |
| `LANG` | locale |
| `LC_ALL` | locale |
| `TZ` | local time |

Each is copied only if it is actually set in the core's environment. Nothing in this list carries application configuration or a credential. Adding to it is a security decision: every entry is something a plugin can read that its manifest never asked for.

**Notably absent, and deliberately:**

- `PYTHONPATH` — would let a plugin be pointed at the core's own modules.
- `HOME` / `USERPROFILE` — a plugin's home is its own folder.
- Anything `PERSONACORE_*`.

`tests/plugins/test_host_stdio.py::test_child_process_environment_is_the_built_one` asserts this from *inside* a real child process, reporting variable names only. It checks that a declared secret is present, an undeclared one is absent, `PERSONACORE_LLM_API_KEY` and `PERSONACORE_ADMIN_PASSWORD` are absent, no `PERSONACORE_`-prefixed name is present at all, and `PYTHONPATH` is absent.

### `FORCED_ENV` — set unconditionally

| Variable | Value | Why |
|---|---|---|
| `PYTHONUNBUFFERED` | `1` | stdio *is* the protocol channel; a buffered child looks like a hung one. |
| `PYTHONDONTWRITEBYTECODE` | `1` | Plugin folders are read-mostly and often mounted read-only; scattering `__pycache__` into them fails noisily for no benefit. |

### The one caveat worth knowing

The MCP SDK merges the core's environment over its own `get_default_environment()`, which re-adds the same *class* of "needed to run" variables (`PATH`, `TEMP`, `SYSTEMROOT` and friends) from `os.environ`. That set is a fixed, hardcoded list in the SDK containing no application configuration and no credentials, so the guarantee that matters — a plugin cannot see a secret it did not declare, or any of the core's own settings — holds either way.

The allowlist is still built explicitly rather than relying on that, because a least-privilege boundary that depends on a dependency's default is not a boundary.

## Secrets

A secret is a file in `<appdata>/secrets/<name>` containing only the value. That shape is what Docker secrets produce, what a mounted env-file can be rendered into, and what a vault agent can write.

**How a secret reaches you:** as an environment variable named exactly as you declared it. If your manifest says

```toml
[permissions]
secrets = [
  { name = "EXAMPLE_API_KEY", description = "Your example.com API key, from Account -> API keys.", required = true },
]
```

then `os.environ["EXAMPLE_API_KEY"]` holds the value inside your process, and nothing else from the store does. Each entry is a table since contract 2.0 — the `description` is what an operator reads beside the box they paste into, and it is required. See [Manifest](Plugin-Manifest#permissionssecrets--the-one-that-is-enforced).

A trailing newline is stripped (`\r\n`), because every editor and `echo` adds one and it is almost never part of the secret — that saves a class of authentication failure that is miserable to diagnose from a 401.

### Failing closed

A **required** secret that cannot be read raises `ChildEnvironmentError`, and the supervisor treats that as **terminal**: the plugin is not started, and retrying will not be attempted. A plugin that asked for a credential it cannot run without does not get started without it, because the failure mode otherwise is a plugin that runs and quietly does nothing.

A request carrying `required = false` does none of that. If nobody has supplied it, the plugin **starts** and the variable is **absent** from its environment — absent rather than empty, so `os.environ.get()` gives you a clean `None` to branch on, and `""` stays available as a value somebody could deliberately have pasted. Only required credentials appear in what the core says a plugin is waiting for.

The two messages, verbatim:

> Plugin 'x' asks for the secrets A, B, but this core was started without a secret store, so it cannot be given them. It has not been started.

(That one names only the **required** ones, and is not raised at all when every request is optional.)

> Plugin 'x' cannot start: it declares the secret 'A' in its manifest, and Secret 'A' is not present. Add a file named 'A' to /appdata/secrets containing only the secret value.

### Why you cannot widen the set

The core holds the real store; your plugin's environment is built from a `ScopedSecrets` view limited to the names your manifest declared. That view offers no way to enumerate the wider store and no way to widen itself. The only widening path is editing the manifest — which is visible in the admin UI and in review.

**Never put a secret value in `config.toml`.** That file is backed up with appdata, readable by anyone who can open the admin UI, and frequently committed to a repo by its author. The config editor actively refuses a file containing a credential-shaped key — see [Configuration](Plugin-Configuration).

## Working directory

**Your working directory is your own plugin folder.** `StdioServerParameters(cwd=str(record.directory))`.

This is what makes a relative `entry` meaningful. A plugin shipping its own virtualenv writes:

```toml
entry = ".venv/bin/python main.py"
```

and it resolves. ADR-0012 records this: absolute paths stay rejected because they are how a plugin points at something outside its folder, and the gap that made the rejection feel unreasonable was simply that nothing said what the working directory would be.

Both bundled plugins still resolve their own directory explicitly rather than relying on it:

```python
PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.toml"
```

That is worth copying. It costs one line and it means your plugin also works when someone runs `python main.py` from somewhere else while debugging.

## stdout is the protocol

On a stdio plugin, **standard output is the MCP conversation**. Anything you print to it corrupts the protocol — usually surfacing as a parse failure the core reports as a transport error, which is a confusing thing to read when the actual cause was a stray `print()`.

**Diagnostics go to stderr, always.** Both bundled plugins do exactly this on their startup-failure path:

```python
print(f"weather plugin cannot start: {exc}", file=sys.stderr)
return 1
```

### What happens to your stderr

The core captures it to a size-capped file (256 KiB by default), because the SDK hands the child's stderr straight to the OS and it must therefore be a real file with a real descriptor — an in-memory buffer cannot be used. An unbounded log would be a genuine disk-exhaustion risk from a plugin stuck in a print loop, so the file is truncated once it passes the limit: a plugin can be noisy, it just cannot be noisy *and* persistent.

The tail of that file is what the operator reads. When a connection attempt fails, the last 400 characters are appended to the failure message:

> could not start 'python main.py': … — the plugin said: weather plugin cannot start: config.toml: no locations are set up

That sentence is usually the entire diagnosis. Write it for a human who is not holding your source open.

Exiting non-zero after printing to stderr is how a stdio plugin says "I cannot start". `tests/plugins/test_host_stdio.py::test_plugin_that_dies_on_startup_is_contained` asserts the printed sentence reaches the health row.

## Timeouts you are living inside

| Bound | Default | What it covers |
|---|---|---|
| `startup_timeout` | 20 s | Spawn, MCP handshake and the first `list_tools()`, together. |
| `call_timeout` | 30 s | One tool call. |
| `heartbeat_interval` | 30 s | How often an idle plugin is pinged. |
| `shutdown_timeout` | 10 s | Per escalation step when stopping. |

If your plugin needs longer than 20 seconds to reach the point where it can list its tools — downloading a model, warming a cache — do that lazily after startup, or run as an [HTTP plugin](Plugin-HTTP-Transport) you start yourself. Full state machine: [Lifecycle](Plugin-Lifecycle).

## See also

[Plugin Contract](Plugin-Contract) · [Manifest](Plugin-Manifest) · [Lifecycle](Plugin-Lifecycle) · [Configuration](Plugin-Configuration)

Where the secret store and the plugin directories sit: [Appdata Layout](Appdata-Layout). The wider picture: [Security Model](Security-Model).
