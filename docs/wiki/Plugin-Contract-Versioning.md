# Plugin Contract Versioning

What `contract = "2.x"` in your manifest actually promises, how compatibility is decided, what a minor or major bump means — and why a `1.x` manifest no longer loads. Short page; read it once and pick `"2.x"`.

Source: `src/personacore/__init__.py`, `src/personacore/plugins/discovery.py`. Spec §4.5, §5.1.

## The promise, precisely

Spec §4.5 is careful about what is being promised:

> **Honest future-proofing.** "Never needs modification" is promised only for the *contract*, which is versioned (semver). New contract minor versions never break existing plugins; a major version bump — expected to be rare to never — keeps a compatibility path.

The promise is about the **contract**, not about features and not about the core's own version. `personacore.__version__` and `personacore.CONTRACT_VERSION` are different numbers and move independently.

**This core implements contract `2.1`.** A manifest declaring a `1.x` contract is refused — see [What changed in 2.0](#what-changed-in-20) below. `2.1` added one optional field; see [What changed in 2.1](#what-changed-in-21).

## Declaring a target

```toml
[plugin]
contract = "2.x"
```

The value must match `^\d+\.(x|\d+)$` — a major, a dot, and either an exact minor or the letter `x`.

```
contract '2' must look like '2.x' or '2.0' — the contract version this plugin
targets
```

Note what is *not* accepted: `"2"`, `"2.0.0"`, `"^2.0"`, `">=2.0"`. Two parts, and the second is a number or `x`.

## How compatibility is decided

`_contract_compatible(declared, core_version)` in `discovery.py`:

1. **The major must match exactly.** No exceptions, in either direction.
2. **`x` as the minor means any minor.** `"2.x"` loads on any `2.y` core.
3. **An exact minor loads on that minor or any later one in the same major** — never an earlier one.

| Your `contract` | Core `2.0` | Core `2.1` (this one) | Core `2.3` | Core `3.0` |
|---|---|---|---|---|
| `"2.x"` | loads | loads | loads | **refused** |
| `"2.0"` | loads | loads | loads | **refused** |
| `"2.1"` | **refused** | loads | loads | **refused** |
| `"2.2"` | **refused** | **refused** | loads | **refused** |
| `"1.x"` | **refused** | **refused** | **refused** | **refused** |
| `"3.x"` | **refused** | **refused** | **refused** | loads |

**A major mismatch and a minor one get different sentences**, because they are different situations. A minor gap means nothing is broken: the core is simply older than the plugin asked for. A `contract = "2.2"` manifest on this 2.1 core:

```
plugins/kitchen-timer/: this plugin needs plugin contract '2.2' and this core
implements '2.1' -- an earlier minor version, so something the plugin asks for
is not here yet and it has not been loaded. Update this core to contract 2.2 or
later, or -- if the plugin does not really need anything newer -- edit its
manifest.toml to contract = "2.x", which loads on any 2.y core (spec 4.5).
```

It names the version the plugin asked for, because that is the thing the operator has to act on. Calling it "a different major version, which is incompatible" — the wording used while 1.x-meets-2.0 was the only mismatch that could happen — would send them hunting for a breaking change that is not there.

The refusal, on this core, for the one older major that exists:

```
plugins/kitchen-timer/: this plugin was written for plugin contract '1.x' and
this core implements '2.1' -- a different major version, which is incompatible,
so it has not been loaded. Contract 2.0 changed one thing: permissions.secrets
is now a list of tables instead of a list of names. Each credential is written
as { name = "openweather_key", description = "What this is and where to get
one.", required = true } — description is required, and required may be left
out because it defaults to true. A plugin that asks for no credentials keeps
secrets = [] and only has to change the contract line. Edit the plugin's
manifest.toml and install it again (spec 4.5).
```

The refusal carries the migration because an operator meeting it is usually not the plugin's author. "Incompatible, update the plugin" leaves them holding a folder with nothing to edit.

It is a load failure, so the plugin appears in the admin UI as `failed` with that sentence — not a crash halfway through a call.

## Which one should you write?

**`"2.x"`, unless you know why not.** Minor versions only ever add, so they cannot break you, and `"2.x"` means you never have to touch the file when the core moves forward.

**An exact minor is a requirement, not a preference.** `contract = "2.2"` says "I need something that was added in 2.2". It buys you one thing: on a 2.1 core you get a clear refusal saying so, instead of failing halfway through a call on a feature that is not there. Pin like this only when you genuinely depend on a newer feature — otherwise you have made your plugin refuse to load on cores where it would have worked fine.

The template says the same thing in its comments, which is where most authors will read it first.

## What a minor version may add

A minor bump is **additive only**. It may:

- add a new optional manifest field;
- add a new optional section;
- add a capability a plugin can choose to use;
- widen what an existing field accepts.

It may **not**:

- remove a field, or make an optional field required;
- narrow what a field accepts;
- change what an existing field means;
- change the meaning of a risk level;
- change the shape of anything a plugin already relies on.

That is the whole basis on which `"2.x"` is safe advice. A plugin written against 2.0 and left untouched must load on 2.9.

One case is already named as a future minor bump: ADR-0012 item 5 records that real egress enforcement for stdio plugins would be **a contract minor version when it lands, "because plugins that quietly relied on unenforced access will break — which is the point."** Worth knowing if you are relying on today's unenforced `permissions.network` ([Manifest](Plugin-Manifest)).

## What changed in 2.1

**`[plugin] provides` was added** — an optional list naming what kind of service the plugin *is*, rather than what it offers.

```toml
[plugin]
name     = "tts-vits-onnx"
contract = "2.x"
provides = ["tts"]
```

A manifest could always say who a plugin is, what it wanted permission for, what tools it offered and what events it sent. It had no line for *what kind of service it is*, so a speech engine could not exist as a plugin at all — not for any protocol reason, but because the label had nowhere to go.

Two values so far: `"tts"` (a speech engine — it turns text into audio) and `"stt"` (a transcriber — it turns audio into text). **It is always a list**, even for one entry, because one box can genuinely be both a speaker and a microphone: `provides = ["tts", "stt"]`. An unknown name is refused by name rather than ignored, and a duplicate is an error rather than being deduped. Full rules and messages: [Manifest](Plugin-Manifest#provides--what-the-plugin-is).

### Why this is 2.1 and not 3.0

**Adding an optional field breaks nothing.** A manifest written against 2.0 does not mention `provides`, gets the default empty list, and loads on this core untouched — which is exactly what "a minor bump is additive" promises above, so honouring it here is not a favour, it is the rule. A plugin declaring `contract = "2.x"` needs no edit at all.

The one thing that changed for existing manifests is the *option* of pinning `contract = "2.1"`, and that is only worth doing if the plugin genuinely needs `provides` to work — a 2.0 core refuses such a manifest, naming the version it needs, instead of loading a speech engine that silently never registers as one.

## What changed in 2.0

**`permissions.secrets` became a list of tables, and the list-of-names form was removed.**

```toml
# contract 1.x — REFUSED by this core
secrets = ["openweather_key"]

# contract 2.x
secrets = [
  { name = "openweather_key", description = "Your OpenWeather API key. Free tier at openweathermap.org/api.", required = true },
]
```

Two problems in one field made it worth breaking (ADR-0026):

1. **A bare name is not an explanation.** The operator saw a box labelled `openweather_key` and nothing telling them what to paste or where to get one. The author knew; the manifest had nowhere to say it. `description` is therefore **required** — making it optional would leave the bad outcome available.
2. **Every declared credential blocked the plugin from starting.** A credential that is conditional had no honest spelling: a self-hosted service that only sometimes sits behind basic auth forced its author either to declare a credential and break every instance that does not need one, or to omit it and have no way to receive one. `required = false` is that spelling. A missing optional credential means the plugin **starts anyway**, with the box still drawn for whoever does need it.

### Why it was removed rather than deprecated

Nothing was public. Three manifests existed — the template, the reference weather plugin, and one search plugin — and all three were in reach, so a compatibility layer would have been two shapes to parse, two to test and two to document for the benefit of nobody. It would also have created a version trap: a plugin using the table form could not honestly claim `1.x`, because a string-typed field refuses a table.

**This was the last cheap moment.** Once a plugin exists that neither of us wrote, the same change costs a migration, a deprecation window and a compatibility path — exactly the cost just avoided. Contract changes are made now, in a batch, while they are still free. That is also why the version went to 2.0 rather than a number that flatters the project: the shape changed incompatibly, and saying so is worth more.

### Migrating a 1.x plugin

Two edits, and the core's refusal message says both if you meet it first:

1. Rewrite each entry in `permissions.secrets` as `{ name = …, description = …, required = true }`. One table per line — TOML 1.0 does not allow a newline inside `{ }`. A plugin that asks for no credentials keeps `secrets = []` and skips this step entirely.
2. Change `contract` to `"2.x"`.

Nothing else in the manifest changed, and no plugin code has to change: the credential still arrives as an environment variable named exactly `name`. An optional credential nobody supplied is simply **absent** from that environment rather than present and empty, so test for it before reading it.

## What a major version would mean

A major bump means an existing plugin can no longer be trusted to work unchanged — a removed field, a changed meaning, a different enforcement model.

Spec §4.5 sets two expectations: it is **rare to never**, and it **keeps a compatibility path**. 2.0 is the first one, and the compatibility path it kept is documentation rather than machinery — the refusal itself carries the migration, and every manifest in existence was updated in the same change (ADR-0026). There is no migration code, deliberately: it would have been a second shape to maintain for nobody. What *is* decided is the failure mode: a `1.x` plugin on a `2.0` core is refused, deliberately and legibly, rather than being run on a contract it was not written for.

## Checking the core's version

- In code: `personacore.CONTRACT_VERSION` — `"2.1"` in this core.
- Over HTTP: `GET /health` returns `{"status": ..., "version": ..., "contract": ...}` — `contract` is the value your manifest is checked against.

## Your own `version` field

`plugin.version` is **your** version, and the core does nothing with it beyond recording it and showing it. It is not pattern-checked; semver by convention. It appears in the install result and in the admin UI.

Keep the two straight: `contract` is which PersonaCore you were written against; `version` is which you.

## See also

[Plugin Contract](Plugin-Contract) · [Manifest](Plugin-Manifest) · [Lifecycle](Plugin-Lifecycle)
