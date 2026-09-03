# PersonaCore plugin-author guide

For a developer who has never read the spec. Read it once, top to bottom; it is
short. The worked example throughout is `plugins/_template/`, and the finished
version of the same thing is `plugins/weather/`.

**Contract version covered:** 2.x · **Status:** P0 draft, tracks the code in
`src/personacore/contracts/manifest.py`, which is the authority if the two ever
disagree.

---

## 1. What a plugin is

**A plugin is an MCP server.** That is the entire contract.

[MCP](https://modelcontextprotocol.io) — the Model Context Protocol — is the
standard way to expose tools to a language model. PersonaCore is an MCP
*client*. Your plugin is an MCP *server*. There is no PersonaCore library to
import, no base class to inherit, and no API of ours to learn. If you already
have an MCP server in any language, it is already a plugin; it just needs a
manifest.

What a plugin adds on top of a plain MCP server is one file, `manifest.toml`,
which declares who it is, what privileges it wants, and how dangerous each of
its tools is. **The manifest declares; the core enforces.** Your code cannot
widen what the manifest asked for. That cuts both ways: a permission you forget
to declare is one you do not get, and a permission you declare and do not need
is a hole you dug yourself.

Everything except conversation, persona, voice, and the baseline weather and web
abilities is a plugin. If a feature seems to need a core change, the contract is
wrong or the feature is a plugin — say so rather than working around it.

## 2. Which transport

| | **stdio** | **http** |
|---|---|---|
| Where it runs | subprocess of the core | its own service or container |
| Lives in | `/appdata/plugins/<name>/` | `/appdata/plugins-http.d/<name>/` |
| Manifest says | `entry = "python main.py"` | `url = "http://host:port/mcp"` |
| Lifecycle | started, restarted and stopped by the core | yours to run |

**Choose stdio unless you have a reason not to.** No port, no container, no
network exposure, and it lives and dies with the core: if it crashes, the core
restarts it with backoff and shows it as unhealthy. This is right for almost
everything.

**Choose http** when the plugin is heavy (a vision model), is written in
something other than Python, needs its own dependencies you would rather not
install next to the core, or is something you deliberately want at arm's length
behind a Compose network boundary.

The core treats the two identically once discovered. Same manifest, same risk
levels, same permissions. Repackaging a stdio plugin as an HTTP one is a change
of two manifest lines and how you start it.

## 3. Folder layout

```
/appdata/plugins/<name>/
    manifest.toml     identity, permissions, per-tool risk — the core reads this
    config.toml       your settings — you read this, the admin UI edits it
    main.py           your code, or whatever the entry command runs
```

The folder name **must equal** the `name` in the manifest. That name also shows
up in event topics and in the audit log, which is why it is restricted to
lowercase letters, digits and hyphens.

Your folder is the only place you are guaranteed. Read and write inside it.
Absolute paths, `..`, Windows drive letters and symlinks that point out of the
folder are all refused at load time, not at runtime — the plugin simply will not
start, with a message saying which field was the problem.

## 4. Every manifest field

```toml
[plugin]
name        = "example-plugin"
version     = "0.1.0"
contract    = "2.x"
transport   = "stdio"
entry       = "python main.py"
description = "One line, shown in the admin UI plugin list."
provides    = []                   # optional; see below

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

| Field | Meaning |
|---|---|
| `plugin.name` | Identity, and the folder name. Lowercase letters, digits, hyphens; 2–64 characters; starts with a letter. Unique across all installed plugins — a duplicate disables **both** copies rather than picking one. |
| `plugin.version` | Your plugin's own version, semver. **Bump it whenever you change your plugin's files.** Unrelated to `contract` — see below. |
| `plugin.contract` | Which PersonaCore plugin contract you were written against. `"2.x"` or an exact `"2.1"`. See §9. |
| `plugin.transport` | `"stdio"` or `"http"`. Must match the directory you installed into, or you get an error telling you which one to move it to. |
| `plugin.entry` | stdio only, required. The command that starts you, relative to your folder. |
| `plugin.url` | http only, required. Where the core reaches you. |
| `plugin.description` | One line for humans. Not read by the model — tool descriptions are. |
| `plugin.provides` | Optional, default `[]`. What kind of service your plugin **is** — `"tts"`, `"stt"`. **A list**, always. Added in contract 2.1; almost every plugin leaves it out. See below. |
| `permissions.network` | Hostname allowlist. Empty means no outbound network at all. |
| `permissions.secrets` | Credentials you are asking the operator for — a list of tables, one per credential. You get these and nothing else. See §6. |
| `permissions.secrets[].name` | Required. What you read it as. It becomes a filename: letters, digits, dots, hyphens, underscores, starting with a letter. |
| `permissions.secrets[].description` | **Required.** What the credential is and where to get one, in your words. Printed beside the box the operator pastes into. |
| `permissions.secrets[].required` | Optional, default `true`. `false` means the plugin starts without it. |
| `permissions.paths` | Extra filesystem paths beyond your own folder, relative to it. Most plugins need none. Pointing at the appdata secrets directory is refused outright. |
| `tools.<name>.risk` | `safe`, `confirm` or `restricted`. Required for every tool. See §5. |
| `tools.<name>.description` | Optional; for humans reading the manifest. The description the *model* reads is the one on the tool in your code. |
| `events.publishes` | Event-bus topics you emit (spec §5.2). |
| `events.subscribes` | Topics you want delivered to you. |

Unknown keys are **rejected, not ignored**. A typo is an error you can see
rather than a permission you silently never got.

### `provides` — say what your plugin *is*, if it is anything

Every other field says what your plugin **has**. This one says what it **is** —
a job the system can hand to it rather than a tool it offers. Added in contract
2.1, and **almost every plugin leaves it out**: a weather plugin offers a tool;
it is not a speech engine.

Two values so far:

| Value | Meaning |
|---|---|
| `"tts"` | A speech engine. It turns text into audio — the voice the assistant speaks in. |
| `"stt"` | A transcriber. It turns audio into text — what somebody said, as words. |

**It is a list, always** — even for one entry, and because one box can genuinely
be both a speaker and a microphone:

```toml
[plugin]
provides = ["tts"]
provides = ["tts", "stt"]     # a device that speaks and listens
```

Three ways to get it wrong, all of which stop your plugin loading with a
sentence saying which:

- **A name that is not on the list above** is refused, naming what you wrote and
  listing what is accepted. It is not quietly dropped, deliberately: a plugin
  that thinks it registered a service and did not fails silently, somewhere
  else, much later.
- **The same value twice** is an error rather than being deduped — a repeated
  entry is nearly always a line you copied to add a *second* service and then
  forgot to edit.
- **A bare string** (`provides = "tts"`) is refused with the one-entry list form
  spelled out.

Leaving it out and writing `provides = []` mean the same thing.

**The operator is told.** A package declaring `provides = ["tts"]` shows up on
the install screen as *"It registers as a speech engine (the voice this
assistant speaks in). That is a job it can be given here, not only a tool it
offers."* That is somebody agreeing to let your code become the voice of their
house, so declare it only if it is true.

### Change your plugin's files, bump `plugin.version`

**Every time.** Code, config schema, or the manifest itself — if the folder is
not byte-identical to the last one you handed out, the version moves. Patch for
a fix, minor for a new tool or setting, major when you break something somebody
was relying on.

It is the only way anybody can tell two copies of your plugin apart. The version
is what the admin UI shows in the plugin list and in the install result, and
what somebody reports when they tell you it is broken. A plugin that has said
`1.0.0` through four releases makes "which one are you running?" unanswerable,
and the answer to that question is the first thing you need.

**`version` and `contract` are independent numbers**, and conflating them is the
common mistake:

| | Moves when | Chosen by |
|---|---|---|
| `plugin.version` | **your** files change | you, every release |
| `plugin.contract` | the **core's** plugin contract changes | the core; you follow it, rarely (§9) |

A release of your plugin that changes nothing about the contract still bumps
`version` and leaves `contract` alone. That is the normal case.

## 5. Choosing a risk level

This is the most consequential thing in the manifest. The level is enforced by
the core at call time and configured per household member.

| Level | What happens |
|---|---|
| `safe` | Runs silently. No prompt, no permission check. |
| `confirm` | The assistant asks first and does not act without a yes. |
| `restricted` | Only permitted users may invoke it at all, and they confirm as well. |

Rules of thumb, in order:

1. **Anything irreversible is never `safe`.** If you cannot undo it, un-say it,
   or un-spend it, the floor is `confirm`.
2. `safe` is for reading. A lookup, a status check, a forecast. If getting it
   wrong is merely embarrassing, it is safe.
3. `restricted` is for what you would not let a guest, a child, or a voice
   coming out of the television do: unlocking doors, dialling phones, spending
   money, deleting things.
4. **Rate the tool, not your intentions.** "It's safe because I validate the
   input" is not what these levels mean.
5. When genuinely torn, pick the stricter one. A `confirm` that should have been
   `safe` is mildly annoying; the reverse gets somebody's front door opened.

A tool the manifest does not declare has no risk level, so the core will not
call it. This fails closed on purpose — but it does mean a tool you forgot to
list looks broken rather than absent. If your tool is never called, check that
its name in the manifest matches its name in the code exactly.

## 6. Permissions: declared, never assumed

Both `network` and `secrets` default to empty, and **empty means none, not
everything**. A plugin that declares nothing gets nothing.

**Network.** List real hostnames, one per entry, and only the ones you actually
call. Every entry is a place your plugin could send the household's data, and a
reviewer has to justify each one. The weather plugin declares two —
`api.open-meteo.com` for forecasts and `geocoding-api.open-meteo.com` for place
lookup — and turns HTTP redirects off, because silently following a redirect to
a third host would make the declaration a lie.

**These declarations are not currently enforced for stdio plugins.** The core
validates them at load and shows them to an admin, and there it stops. Write
your plugin as though they were enforced, and police your own egress the way the
weather plugin does; do not read the absence of enforcement as permission. See
ADR-0012.

### Do not hardcode a host you cannot know

A public API you call is yours to declare — `api.open-meteo.com` is the same
address for everybody. **An operator's own service is not.** Their SearXNG,
their Home Assistant, their Immich lives at whatever address *they* use: an IP,
a name on their LAN, a name only their resolver knows. You cannot guess it, and
you should not try.

**This is not hypothetical. It has already happened, in the first plugin written
against this guide.**

A search plugin shipped with:

```toml
# manifest.toml
network = ["searxng.lan"]
```

```toml
# config.toml
base_url = "http://searxng.lan:8080"
```

and code that refused to start unless the two agreed. The operator's SearXNG was
not called `searxng.lan`. They changed `base_url` — the setting the plugin told
them to change — and the plugin killed itself five times and switched off, with:

> config.toml points at '…', which manifest.toml does not declare under
> permissions.network (it lists: searxng.lan). Set both to the same hostname or
> IP — this plugin will not contact a host it has not declared.

Everything about that is well-built. The message is clear, it names both values,
the plugin was policing its own egress exactly as this guide asks. **And it was
wrong**, because it made the author's guess into the operator's problem, and the
only way out was to edit somebody else's manifest.

**What to do instead.** The configured host wins. Read it from your config, use
it, and **say on stderr which host you are about to contact** every time you
start — that line lands on the plugin's own log page, so the operator can see
where their data is going. Keep the manifest entry as a *default* and say in a
comment that it is one.

The mistake to avoid is not "declaring a host". It is treating your declaration
as authoritative over the operator's configuration. You know what your plugin
talks to; you do not know where the operator keeps it.

Declaring such a host as operator-supplied, so the manifest can be honest without
being a guess, is **PC-309 and not built yet**.

**Secrets.** You declare the credentials you need; the core hands you those
values at runtime, in environment variables named exactly as you named them, and
nothing else. A plugin never sees the whole secret store. Secret *values* never
appear in your manifest, in your config, in your code, or in git.

Since contract 2.0 each one is a **table**, not a bare name:

```toml
[permissions]
secrets = [
  { name = "openweather_key", description = "Your OpenWeather API key. Free tier at openweathermap.org/api — 'Current Weather Data' is enough.", required = true },
  { name = "proxy_password", description = "Only if your instance sits behind HTTP basic auth. Leave empty otherwise.", required = false },
]
```

One table per line: TOML 1.0 does not allow a newline inside `{ }`. The roomier
`[[permissions.secrets]]` form works too, but it has to come after every other
key in `[permissions]`.

**`description` is required, and it is the point.** The operator sees a box
labelled with your `name` and your sentence beneath it. They have never heard of
your service. Write what it is, where to get one, and which plan or scope is
enough. "API key" is not a description — they can already read the field's name.
Your text is printed **escaped, never as markup**.

**`required = false` changes behaviour, not just wording.** A missing required
credential means the plugin is not started and the operator sees it as
*waiting*, naming what it needs; one paste starts it. A missing optional one
means the plugin **starts anyway** and the variable is simply absent from your
environment — check for it and carry on. Use it for a credential that is
conditional: an instance that only sometimes sits behind basic auth, an optional
paid tier. Declaring such a credential as required would break every instance
that does not need it.

Whatever you declare, fail with a clear message when something you genuinely
need is absent, rather than half-working.

**Paths.** You already have your own folder. Needing more is unusual; say so
explicitly if you do.

## 7. Config

Your settings live in `config.toml` **in your own folder**. The core never
stores plugin settings centrally.

The admin UI reads that file, presents it, and writes it back, so:

- keep keys flat and obviously named — they become form fields;
- keep your comments — they become the help text beside the field;
- ship working defaults, so a fresh copy of your plugin runs as it lands;
- **never put a secret in it** (see §6).

**The core does not validate your config** — it has no idea what your settings
mean. You do, so you check it, at startup, and you fail with a sentence a
non-programmer can act on: `config.toml: 'units' Input should be 'metric' or
'imperial'`. That sentence is what appears beside your plugin in the admin UI.
Both bundled plugins use pydantic for this because it makes readable messages
cheap; hand-rolled checks are equally acceptable. Checking nothing is not.

## 8. Installing, reloading, and diagnosing

**Write one:**

```
cp -r plugins/_template  /appdata/plugins/kitchen-timer
```

then edit `name` in `manifest.toml` to `kitchen-timer` to match the folder, and
work down the file. Everything in the template is commented with what to change
and why. "Write a new plugin" means "copy the template" — start anywhere else
and you will rediscover these rules the hard way.

**Install it:** copy the folder into `/appdata/plugins/` (or
`/appdata/plugins-http.d/` for HTTP). That is the whole installation. Plugins
are appdata, never baked into an image, so upgrading the core never touches
them.

**Reload:** hit reload in the admin UI, or `POST /admin/api/plugins/reload`.
The core rescans both directories. No container rebuild, no stack restart. The
response tells you how many loaded and how many did not.

**When it does not load:** `GET /admin/api/plugins` (and the plugin list in the
UI) returns two lists — the plugins that loaded, and the ones that failed, each
with a plain-English reason naming the file and the offending key. A broken
plugin is listed beside its error rather than taking the scan down. Typical
reasons:

| Message | Cause |
|---|---|
| `missing manifest.toml` | wrong folder level — the manifest sits directly in the plugin folder |
| `folder name … does not match … plugin name` | rename one to match the other |
| `not valid TOML` | quote your strings; the message names the line |
| `'permissions.secrets' Extra inputs are not permitted` | a typo'd or invented key; check §4 |
| `declares transport 'http' but was found under plugins/` | move it to `plugins-http.d/` |
| `written for plugin contract '1.x' and this core implements '2.0'` | see §9 — the message says what changed |
| `'permissions.secrets' no longer takes a list of names` | a contract-1.x manifest; rewrite each entry as a table, see §6 |
| `name … is declared more than once` | two folders claim the same name; **neither** loads |

A plugin that loads but then crashes is a different problem: it is killed,
logged, restarted with backoff, and shown as unhealthy. A bad plugin never takes
the core down.

**Read what your plugin printed.** Anything your plugin writes to **stderr** is
captured and shown at **Plugins → Logs** in the admin UI, per plugin. That is
the place to look when it starts and then misbehaves, and it is why stderr is
worth using: `print()` to stdout is the MCP channel and will corrupt the
protocol, so log to stderr and nowhere else.

The capture is bounded and truncating, and the page says so when it has dropped
output — a plugin in a crash loop can produce more than anyone wants to keep.
An HTTP-transport plugin runs in its own container, so its output belongs to
whatever started it, and the page says that rather than showing an empty box.

**Is it being offered to the model?** The chat screen reports how many tools
were offered and which were called. That single line separates "my plugin is not
loaded" from "it loaded and the model chose something else" — two problems that
look identical from a disappointing answer.

## 9. Contract versioning

The plugin contract is versioned with semver, and the promise is about the
contract, not about features:

- **Minor versions only ever add.** A 2.3 core runs a plugin written for 2.0
  unchanged. That is the whole point of the version.
- **`contract = "2.x"`** means "any 2.y core". Right for almost everyone.
- **`contract = "2.1"`** means "I need something added in 2.1". It loads on 2.1
  and later, and is refused on 2.0 with a message saying so, instead of failing
  halfway through a call. Pin like this only when you genuinely depend on a
  newer feature.
- **A major bump is breaking.** A core refuses a manifest from an older major,
  and the refusal names what changed rather than only that something did.

Check the core's current contract version in `personacore.CONTRACT_VERSION`. It
is `"2.1"` today.

**A minor gap and a major gap read differently, and they should.** A manifest
pinning a minor this core does not have yet is told exactly that — *"this plugin
needs plugin contract '2.2' and this core implements '2.1' — an earlier minor
version"* — and told it can drop to `"2.x"` if it did not really need anything
newer. Nothing is broken in that case; the core is just behind.

### What changed in 2.1

**`provides` was added** (§4): an optional `[plugin]` field naming what kind of
service your plugin is. Purely additive — a manifest written for 2.0 does not
mention it, gets an empty list, and loads on a 2.1 core untouched. Nothing to
migrate, and `contract = "2.x"` still needs no edit.

Pin `contract = "2.1"` **only if your plugin does not work without `provides`** —
a speech engine, for instance. Then an older core refuses it in words instead of
loading something that silently never registers as anything.

### What changed in 2.0

`permissions.secrets` became a list of tables (§6). It used to be a list of
names:

```toml
secrets = ["openweather_key"]      # contract 1.x — REFUSED by a 2.x core
```

That form is **removed, not deprecated**, so a `1.x` manifest does not load at
all. Two reasons it was worth breaking: a bare name gave the operator a box with
nothing beside it explaining what to paste, and every declared credential
blocked the plugin from starting, so a credential that is only sometimes needed
had no honest spelling.

Migrating is two edits: rewrite each entry as `{ name = …, description = …,
required = true }`, and change `contract` to `"2.x"`. A plugin that asks for no
credentials keeps `secrets = []` and only changes the contract line. The core's
refusal message says the same thing if you meet it before you read this.

## 10. Rules worth stealing from the reference plugin

Read `plugins/weather/main.py`. It is mostly comments, and it is meant to be
read rather than skimmed.

- **Everything from outside is data, never instructions.** Tool arguments came
  from someone talking, or from text on a camera, or from a chat bridge. API
  responses came from the internet. Bound them, type-check them, and never hand
  them to a shell, a path, or a query unvalidated.
- **Do not echo what a service says.** The weather plugin maps a numeric weather
  code through its own table, so the only English that reaches the persona is
  English we wrote. Nothing else from the response is passed through.
- **Failure is an outcome, not a crash.** The service being unreachable returns
  `available = false` plus a sentence the persona can say aloud — "I can't reach
  the weather service right now." Structured enough for the agent, speakable
  enough for the user, and not a traceback in either direction.
- **Put a timeout on every outbound call**, and make it a config setting.
- **On stdio, stdout belongs to the protocol.** Print anything to it and you
  corrupt the conversation. Diagnostics go to stderr.
- **Build the server in a function** (`build_server(config)`), separate from
  `main()`. It costs nothing and it means your tools can be tested without
  spawning a subprocess — see `tests/plugins_bundled/`.

## 11. What the operator is told about you

When someone installs your zip, PersonaCore shows them what your manifest
declares **before** it installs anything: your name and description in your own
words, what kind of service you register as if you declare one (§4), every tool
with the risk level you asked for, every host you declare, and every secret you
name.

Write those fields for that screen. Your `description` is the one line a person
reads while deciding whether to trust a stranger's code, and a tool's declared
risk is what they will judge you on.

Be aware of what that screen does **not** promise, because you should not rely on
it either. Secrets are genuinely enforced — you receive the ones you named and no
others. Risk levels are genuinely enforced — the core holds your tool to the
level you declared and you cannot widen it at runtime. **Hosts and paths are
not.** A stdio plugin is a subprocess of the core sharing its network namespace,
so the list is your statement of intent, not a boundary anyone is holding you to.
That is exactly why it is worth being accurate: it is the only thing an operator
has to go on, and the first plugin written against this guide got it wrong in a
way that broke itself (§6).

## 12. Checklist before you ship

- [ ] `plugin.version` bumped — you changed files, so it moves (§4).
- [ ] Folder name equals `plugin.name`.
- [ ] Every tool in the code has a `[tools.<name>]` block, and vice versa.
- [ ] `plugin.provides` declared only if your plugin genuinely *is* one of those services, and left out otherwise (§4).
- [ ] Every risk level justified; nothing irreversible marked `safe`.
- [ ] `permissions.network` lists only hosts you actually call.
- [ ] No secret value in any file you are shipping.
- [ ] Every credential in `permissions.secrets` has a description somebody who has never heard of your service could act on, and `required` set honestly (§6).
- [ ] Config validated at startup with a message a non-programmer could act on.
- [ ] The unreachable-dependency path returns words, not an exception.
- [ ] It loads: copy it in, hit reload, and check the plugin list.
