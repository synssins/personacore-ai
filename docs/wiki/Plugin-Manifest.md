# Plugin Manifest

Every field of `manifest.toml`, what it rejects, and the exact message you get back. Reference material — read [Plugin Contract](Plugin-Contract) first for what the file is *for*.

Source: `src/personacore/contracts/manifest.py` (the schema), `src/personacore/plugins/discovery.py` (the load-time checks), `src/personacore/plugins/errors.py` (how errors are worded).

## The whole file

```toml
[plugin]
name        = "example-plugin"
version     = "0.1.0"
contract    = "2.x"
transport   = "stdio"
entry       = "python main.py"
description = "One line, shown in the admin UI plugin list."
provides    = []          # optional, contract 2.1 — see below

[permissions]
network = []
secrets = [
  { name = "example_api_key", description = "Your API key for example.com. Create one under Account -> API keys; the free tier is enough.", required = true },
]
paths   = []

[tools.hello]
risk        = "safe"
description = "Say hello."

[events]
publishes  = []
subscribes = []
```

`[plugin]` is the only required section. `[permissions]`, `[tools]` and `[events]` all default to empty.

**Unknown keys are rejected, not ignored.** Every model in the manifest schema sets `extra="forbid"`. A typo in `permissions.secrets` does not become a permission you silently never got; it becomes an error naming the key.

## `[plugin]`

| Key | Type | Required | Default | Rule |
|---|---|---|---|---|
| `name` | string | yes | — | `^[a-z][a-z0-9-]{1,63}$` — lowercase letters, digits and hyphens; 2–64 characters; starts with a letter. Must equal the folder name. Must be unique across both plugin directories. |
| `version` | string | yes | — | Your plugin's own version. Not pattern-checked by the core; semver by convention. Unrelated to `contract`. |
| `contract` | string | yes | — | `^\d+\.(x|\d+)$` — `"2.x"` or `"2.1"`. See [Contract Versioning](Plugin-Contract-Versioning). |
| `transport` | string | yes | — | `"stdio"` or `"http"`. Must match the directory it was found in. |
| `description` | string | no | `""` | One line for humans, shown in the plugin list. **Not** read by the model — the description the model reads is the one on the tool in your code. |
| `entry` | string | stdio only | `null` | The command that starts you, relative to your folder. Required for stdio, ignored for http. |
| `url` | string | http only | `null` | Where the core reaches you. Required for http, ignored for stdio. Must be `http://` or `https://`. |
| `provides` | list of strings | no | `[]` | What kind of service the plugin **is** — `"tts"`, `"stt"`. Added in contract 2.1. Always a list. Unknown names and duplicates are refused. |

### `name`

The name becomes a directory name, appears in every event topic, and appears in every audit record. It is kept boring so none of those three places needs escaping rules.

```
plugin name 'Weather Plugin' must be lowercase letters, digits and hyphens,
start with a letter, and be 2-64 characters
```

Two further checks happen at load time, in `discovery.py`:

**Folder name must match.**

```
plugins/weather-2/: folder name 'weather-2' does not match the manifest's
declared plugin name 'weather' -- a plugin's folder and its declared name must
be identical (spec 7)
```

**Names must be unique across both directories.** A duplicate disables *both* copies rather than the core picking one:

```
plugin name 'weather' is declared more than once: plugins/weather,
plugins-http.d/weather -- plugin names must be unique across plugins/ and
plugins-http.d/; none of them will be loaded until this is resolved
```

### `contract`

```
contract '1' must look like '2.x' or '2.0' — the contract version this plugin
targets
```

Compatibility is decided against `personacore.CONTRACT_VERSION`, which is `"2.1"` in the core this wiki documents. A manifest from an older major is refused, and the refusal says **what changed**, not only that something did:

```
plugins/kitchen-timer/: this plugin was written for plugin contract '1.x' and
this core implements '2.1' -- a different major version, which is incompatible,
so it has not been loaded. Contract 2.0 changed one thing: permissions.secrets
is now a list of tables instead of a list of names. […] Edit the plugin's
manifest.toml and install it again (spec 4.5).
```

A **minor** gap is a different sentence, because it is a different situation: nothing is broken, this core is simply older than the plugin asked for. A manifest pinning `contract = "2.2"` on this 2.1 core gets:

```
plugins/kitchen-timer/: this plugin needs plugin contract '2.2' and this core
implements '2.1' -- an earlier minor version, so something the plugin asks for
is not here yet and it has not been loaded. Update this core to contract 2.2 or
later, or -- if the plugin does not really need anything newer -- edit its
manifest.toml to contract = "2.x", which loads on any 2.y core (spec 4.5).
```

### `transport`

Declared transport and directory must agree, and the error tells you which way to move:

```
plugins/vision/: manifest declares transport 'http' but was found under
plugins/ -- move it to plugins-http.d/ instead
```

### `entry` — the strictest field in the file

`entry` is the command line the core will actually execute, so it gets checked harder than anything else (spec §7).

**How it is tokenised.** Plain `str.split()` on whitespace, at both check time and launch time. That is deliberate: using a smarter tokenizer at launch than at validation is how sandbox escapes are built. **Quoted paths containing spaces are therefore not supported in `entry`.**

**What is refused.** Each check runs against the *raw, untouched string* first — before any tokenizer sees it — because both available tokenizers destroy evidence (`str.split()` loses quoting; `shlex.split()` eats a backslash traversal).

| Rejected | Example | Message |
|---|---|---|
| Absolute path | `/usr/bin/python main.py` | `entry '/usr/bin/python main.py' is an absolute path (token '/usr/bin/python') -- plugin manifests may only reference paths relative to the plugin's own folder` |
| Windows drive prefix, anywhere, including inside quotes | `"C:\Python\python.exe" main.py` | `entry '…' contains a Windows drive prefix -- this is an absolute path; plugin manifests may only reference paths relative to the plugin's own folder (spec 7)` |
| A `..` segment, either slash style | `../shared/python main.py` | `entry '…' contains a '..' segment -- a plugin may not reference anything outside its own folder` |
| Home-relative | `~/bin/python main.py` | absolute-path message, naming the token |
| Empty | `entry = ""` | `entry is empty -- nothing to run` |
| A relative token that resolves outside the folder via symlink | `link/python main.py` | `entry token 'link/python' resolves to '/opt/other/python', outside the plugin's own folder '/appdata/plugins/x' -- refusing (possible symlink escape)` |

Tokens starting with `-` are treated as flags and skipped. A token that does not exist on disk is skipped too — that is how a bare interpreter name like `python` works: it is not a path inside your folder, so it falls through to `PATH`.

**What this means in practice.** A plugin shipping its own virtualenv writes `entry = ".venv/bin/python main.py"`, which is legal today and resolves because the working directory is your own folder (ADR-0012, item 2). A plugin that wants the system interpreter writes `entry = "python3 main.py"` and takes `PATH` as it comes.

Missing `entry` on a stdio plugin:

```
a stdio plugin must declare 'entry' — the command that starts it
```

### `url`

```
an http plugin must declare 'url' — where the core reaches it
```

The scheme is checked when the core connects rather than at load:

> Plugin 'vision' declares the address 'file:///x', which is not an http:// or https:// URL. HTTP plugins are reached over the network and nothing else.

### `provides` — what the plugin *is*

Added in **contract 2.1**. Every other field says what a plugin *has*; this one says what it *is* — the job the system can hand to it.

```toml
[plugin]
provides = ["tts"]
```

| Value | Meaning |
|---|---|
| `"tts"` | A speech engine. It turns text into audio — the voice the assistant speaks in. |
| `"stt"` | A transcriber. It turns audio into text — what somebody said, as words. |

**It is a list, always**, even for one entry. One box can genuinely be both a speaker and a microphone, and that is written `provides = ["tts", "stt"]`. Order is kept as declared.

**Absent or empty is the normal case, and stays normal.** A plugin that only offers tools does not mention the field and is entirely unaffected by its existing. That is what makes 2.1 a minor rather than a major.

**An unknown name is refused, not ignored.** A plugin whose author believes it registered a service and did not fails silently, somewhere else, much later — which is worse than a plugin that did not load:

```
'plugin.provides' names a service this core does not know: 'speech'. The
services a plugin may provide are "tts" (a speech engine, turning text into
audio) and "stt" (a transcriber, turning speech into text). Check the spelling,
or leave provides out altogether — a plugin that offers no service of its own
declares nothing here. An unknown name is refused rather than ignored: a plugin
whose author believes it registered a service and did not is worse than one
that failed to load.
```

**A duplicate is an error too**, rather than being quietly deduped — a repeated entry is nearly always a line copied to add a *second* service and then not edited, so dropping it silently throws away the only evidence that the second service is missing:

```
'plugin.provides' names 'tts' twice. Each service is declared once. […]
```

And it is a list even when it holds one thing, so `provides = "tts"` is refused with the one-entry form spelled out:

```
'plugin.provides' must be a list, even when there is only one entry:
provides = ["tts"]. […]
```

**The install disclosure shows it.** A package declaring `provides = ["tts"]` is asking to become the voice everything is spoken in, so the review screen says so before the operator agrees, naming what the package registers as: for a single `tts` declaration, *"It registers as a speech engine (the voice this assistant speaks in)."* A package that declares nothing prints nothing there.

## `[permissions]`

Least privilege, spec §7. All three lists default to empty, and **empty means none, not everything**. A plugin that declares nothing gets nothing.

| Key | Type | Default | Enforced? |
|---|---|---|---|
| `network` | list of hostnames | `[]` | **No, for stdio plugins.** See below. |
| `secrets` | list of credential requests | `[]` | **Yes.** The one permission with real teeth. |
| `paths` | list of relative paths | `[]` | **No at runtime.** Validated at load time only. |

### `permissions.network` — declared, not enforced

**`permissions.network` is a declaration, not a wall. For stdio plugins the core enforces nothing (ADR-0012).**

A stdio plugin is a subprocess of the core sharing its network stack. Enforcing egress per subprocess would need `NET_ADMIN` and per-process namespaces, which trades the security posture of the whole (unprivileged, CPU-only) stack for one plugin's egress. ADR-0012 records the four approaches considered and why each was rejected or deferred.

What the field *is* worth: it is reviewable. An admin looking at a plugin folder from the internet can see "this plugin says it talks to `api.open-meteo.com`" before installing it. That is real value; it is just not enforcement. The reference plugin polices itself — it sets `follow_redirects=False` so a redirect cannot walk it off its own allowlist — and yours should too.

For HTTP-transport plugins, Compose network segmentation is the intended enforcement (spec §7, ADR-0012 item 3). See [HTTP Transport](Plugin-HTTP-Transport) for the current state of that.

### `permissions.secrets` — the one that is enforced

Names, never values. Each entry is a **table**, not a bare name (contract 2.0, ADR-0026):

```toml
[permissions]
secrets = [
  { name = "openweather_key", description = "Your OpenWeather API key. Free tier at openweathermap.org/api — 'Current Weather Data' is enough.", required = true },
  { name = "proxy_password", description = "Only if your instance sits behind HTTP basic auth. Leave empty otherwise.", required = false },
]
```

One table per line — TOML 1.0 does not allow a newline inside `{ }`. The `[[permissions.secrets]]` array-of-tables form is equivalent, but must come after every other key in `[permissions]`.

| Key | Type | Required | Default | Rule |
|---|---|---|---|---|
| `name` | string | yes | — | What the plugin reads it as. `^[A-Za-z][A-Za-z0-9_.-]{0,63}$` — 1–64 characters starting with a letter (it becomes a filename), and not a Windows reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with or without an extension). Checked by the secret store when the file is written. |
| `description` | string | **yes** | — | What the credential is and where to get one. Printed beside the box the operator pastes into, **escaped, never as markup**. An empty or whitespace-only description is refused. |
| `required` | bool | no | `true` | Whether the plugin can run without it. |

**`description` is required on purpose.** The whole reason the request became a table is that a field appeared labelled `openweather_key` with nothing explaining what to paste. Making the explanation optional would leave that outcome available.

```
'permissions.secrets.0.description' Field required
'permissions.secrets.0.description' must say what the credential is and where to
get one. It is the only thing an operator has to go on when the field appears,
so an empty one is refused rather than shown as a blank line.
```

**`required` decides whether a missing credential stops the plugin.**

| `required` | Credential missing | What the operator sees |
|---|---|---|
| `true` (default) | Plugin is **not started** | The `waiting` state, naming this credential. One paste starts it. |
| `false` | Plugin **starts anyway** | The box is still drawn, marked *optional*. No waiting state, no red row. |

An optional credential that nobody supplied is **absent** from the plugin's environment rather than present and empty, so a plugin can test for the variable and get a clean answer. `waiting` therefore means *missing **and** required*; a plugin waiting on an optional credential is a contradiction and is not renderable.

The core reads `<appdata>/secrets/plugins/<plugin>/<name>` and puts each supplied secret into the child's environment under exactly its `name`. A plugin only ever receives the credentials it named, in its own namespace, so one it did not name cannot be read even if it is compromised — there is no API for widening the set at runtime, and the scoped view offers no way to enumerate the store.

**The old list-of-names form is refused, not migrated:**

```
'permissions.secrets' no longer takes a list of names. Contract 2.0 made each
credential a table: { name = "openweather_key", description = "What this is and
where to get one.", required = true }. […] Rewrite this list in that form and
set contract = "2.x" in the [plugin] section.
```

Details and messages for the start-time behaviour: [Runtime Environment](Plugin-Runtime-Environment).

### `permissions.paths`

Extra filesystem paths beyond your own folder, relative to it. Most plugins need none.

Three refusals at load time:

```
permissions.paths entry '/etc' is an absolute path -- plugin manifests may only
reference paths relative to the plugin's own folder

permissions.paths entry '../other' contains a '..' segment -- a plugin may not
reference anything outside its own folder

permissions.paths entry 'secrets/keys' points at the appdata secrets directory
-- declare needed secrets in permissions.secrets instead, plugins never get
filesystem access to the secret store
```

The secrets check compares the first path segment case-insensitively against `secrets`.

**Nothing reads `permissions.paths` after that.** No code in `src/personacore/` grants or restricts filesystem access based on it. Spec §7 calls for plugin subprocesses to run with minimal filesystem visibility; that is not implemented for stdio plugins, which inherit the core process's view. Treat the field as you treat `network`: a reviewable statement of intent.

## `[tools.<name>]`

One block per tool your MCP server exposes. A tool with no block has no risk level and is not callable — and, since load-time reconciliation compares the two sets, a mismatch in either direction stops the plugin loading at all.

| Key | Type | Required | Notes |
|---|---|---|---|
| `risk` | `"safe"` \| `"confirm"` \| `"restricted"` | yes | Enforced by the core at call time. |
| `description` | string | no | For humans reading the manifest. Used as the model-facing description only if the server does not supply one. |

### Tool name rules

The check is `^[a-z][a-z0-9-]{1,63}$` applied after underscores are replaced with hyphens — so underscores are allowed, but the rest of the rule still holds: **lowercase only, must start with a letter, minimum two characters.** `get_forecast` is fine; `getForecast`, `_hidden` and `x` are not.

```
tool name 'getForecast' must be lowercase letters, digits, hyphens or underscores
```

The two-character minimum and the lowercase rule are easy to trip over because MCP itself does not impose them.

### Risk levels

| Level | What the core does |
|---|---|
| `safe` | Runs silently. No prompt, no permission check. |
| `confirm` | The assistant asks first and does not act without a yes. |
| `restricted` | Only permitted household members may invoke it at all, and they confirm as well. |

Rules of thumb, in the order they matter:

1. **Anything irreversible is never `safe`.** If you cannot undo it, un-say it, or un-spend it, the floor is `confirm`.
2. `safe` is for reading — a lookup, a status check, a forecast.
3. `restricted` is for what you would not let a guest, a child, or a voice coming out of the television do.
4. **Rate the tool, not your intentions.** "It's safe because I validate the argument" is not what these levels mean.
5. When torn, pick the stricter one. A `confirm` that should have been `safe` is mildly annoying; the reverse gets somebody's front door opened.

Risk comes from the manifest and *only* from the manifest. The running server's tool list carries no risk field, deliberately, so a plugin cannot promote its own tool to `safe` at runtime. Full enforcement path: [Tools](Plugin-Tools).

## `[events]`

| Key | Type | Default |
|---|---|---|
| `publishes` | list of strings | `[]` |
| `subscribes` | list of strings | `[]` |

Both are validated as lists of strings and then **read by nothing in the core today**. They are documentation of intent for whoever reviews the plugin. See [Events](Plugin-Events) for what the bus actually does and how a plugin actually publishes.

## Load-order and error handling

`PluginDiscovery.scan()` never raises for a single bad plugin. Every failure becomes a `PluginLoadFailure` carrying a plain-English message, and the scan returns successes and failures side by side so the admin UI can list a broken plugin *next to its error* rather than losing the whole scan (spec §5.1, §9).

Checks run in this order per folder:

1. Does the folder resolve inside the plugins directory? (Catches a symlinked plugin folder: *"this plugin folder resolves to '…', outside '…' -- refusing to follow a symlink out of the plugins directory"*.)
2. Does `manifest.toml` exist? (*"missing manifest.toml -- every plugin folder needs one (spec 5.1)"*)
3. Is it readable, and valid TOML? (*"not valid TOML -- …"*, quoting the parser's line)
4. Does it validate against the schema? Pydantic errors are rendered one line per problem as `'plugin.transport' Input should be 'stdio' or 'http'`, with pydantic's redundant `Value error, ` prefix stripped.
5. Folder name, transport location, contract compatibility, then the §7 path checks on `entry` and `permissions.paths`.
6. Finally `config.toml`, if present, is parsed as TOML — an invalid one is a load failure too.

Folders whose name starts with `_` or `.` are skipped before any of this, and are not errors.

## See also

- [Plugin Contract](Plugin-Contract) · [Runtime Environment](Plugin-Runtime-Environment) · [Tools](Plugin-Tools) · [Contract Versioning](Plugin-Contract-Versioning)
- The core's side of the same rules: [Risk Levels](Risk-Levels), [Security Model](Security-Model)
- Worked examples: [Weather](Plugin-Walkthrough-Weather), [Template](Plugin-Walkthrough-Template)
