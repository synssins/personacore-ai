# Plugin Configuration

Your plugin's settings: where they live, who validates them, and how to turn a raw TOML textarea into a real form by shipping one optional file. Read this when you want your plugin configurable by someone who is not you.

Source: `src/personacore/admin/plugin_schema.py`, `src/personacore/admin/plugin_form.py`, `src/personacore/admin/plugin_lookup.py`, `src/personacore/admin/plugin_config_io.py`. Decisions: ADR-0015, ADR-0016.

## `config.toml`

Your settings live in `config.toml` **in your own folder**. The core never stores plugin settings centrally (spec §5.1).

The core's involvement is deliberately thin:

- Discovery parses the file as TOML and puts the raw dictionary on the `PluginRecord` for the admin UI. It is **unvalidated** — you own that shape, not the core.
- A file that is not valid TOML is a load failure (`"config.toml: not valid TOML -- …"`).
- No file at all is fine. `PluginRecord.config` is simply `None`.

**The core does not validate your config.** It has no idea what your settings mean, and spec §13.5 forbids it learning. So you validate it yourself, at startup, and fail with a sentence a non-programmer can act on:

```
config.toml: 'units' Input should be 'metric' or 'imperial'
```

That sentence, printed to stderr before exiting non-zero, is what appears beside your plugin in the admin UI. Both bundled plugins use pydantic for this because it makes readable messages cheap; hand-rolled checks are equally acceptable. Checking nothing is not.

### Conventions that pay off

- **Keep keys flat and obviously named** — they become form fields.
- **Keep your comments** — they are the field help for anyone editing the raw file.
- **Ship working defaults**, so a fresh copy of your plugin runs as it lands.
- **Put your settings under one named table** (`[weather]`, `[random_prompt]`). See "Which table" below for why this matters.

### Never put a secret in it

`config.toml` is backed up with appdata and readable by anyone who can open the admin UI. Declare credentials by name in `permissions.secrets` and take them from the environment ([Runtime Environment](Plugin-Runtime-Environment)).

The admin config editor enforces this from both directions — a config containing a credential-shaped key is **neither shown nor saved**. The key names that trigger it:

`api_key`, `apikey`, `access_token`, `auth_token`, `bearer_token`, `credential`, `credentials`, `passphrase`, `password`, `private_key`, `secret`, `secret_key`, `token`

**This catches innocent settings too.** A plugin with a `token` setting that holds a bracket-matching token, or a `secret` boolean, will find its config page refusing to open:

> `<path>` contains a setting that looks like a live secret, so it is not shown or edited here.

The refusal applies to the whole file, not one key, and it applies on the way out as well as in — a value that should never have reached disk must not be handed onward to a browser or a screenshot either. Rename the setting, or give it a name ending in `_secret`, which means "this holds the *name* of a secret" and is exempt.

A submitted `config.toml` is capped at 256 KiB (`MAX_PLUGIN_CONFIG_CHARS`).

### Saving restarts your plugin

A plugin reads `config.toml` when it starts, so the admin UI restarts it after a successful save (ADR-0010). Do not build a config-file watcher; do read your config once, at startup, and validate it there.

## `config.schema.json` — the optional form

ADR-0015: a plugin may ship `config.schema.json` beside its `config.toml`, describing the settings object as JSON Schema. The core renders a form from **the subset it understands**, validates submissions against it, and falls back to the raw TOML editor for anything else.

**Shipping one is never required.** Not shipping one costs you nothing but the textarea. Requiring it would break every plugin written before ADR-0015.

Three rules the core holds itself to:

- **The schema is data and is never executed.** No expressions, no callbacks, no remote `$ref`.
- **A secret value is never rendered.** A field may be marked as holding the *name* of a secret; the UI offers the names in the store.
- **The schema is never enforced against the plugin.** You still validate your own config at startup — you own the meaning.

### Limits

| Limit | Value | What it caps |
|---|---|---|
| `MAX_SCHEMA_BYTES` | 256 KiB | the file itself |
| `MAX_PROPERTIES` | 200 | top-level settings read from one schema |
| `MAX_ENUM_CHOICES` | 200 | options in one dropdown |
| `MAX_ENTRY_FIELDS` | 20 | fields inside one repeating-group entry |
| `MAX_REF_DEPTH` | 8 | in-document `$ref` hops |
| `LONG_TEXT_CHARS` | 200 | above this `maxLength`, a string renders as a textarea |

### Every keyword the core reads

This is the complete list. Anything not here is ignored (or makes the property unrenderable — see below).

**At the root of the document**

| Keyword | Effect |
|---|---|
| `type` | Must be `"object"` or absent. Anything else refuses the whole schema. |
| `properties` | The settings. Must be an object. |
| `required` | List of strings; marks fields required. |
| `title` | Page title. |
| `description` | Page description. |
| `$ref` | Followed only if it starts with `#`. |

**On a property — annotations, all kinds**

| Keyword | Effect |
|---|---|
| `title` | The field's label. Falls back to the property name. |
| `description` | Help text under the field. |
| `default` | Pre-fills an empty field. **Offered, never imposed** — it is not written on the operator's behalf. |
| `$ref` | In-document only. |

**Which control you get**

| Schema | Control (`FieldKind`) |
|---|---|
| `"type": "boolean"` | `toggle` |
| `"type": "string"` with `enum` (all strings, non-empty, ≤200) | `choice` — a dropdown |
| `"type": "string"` | `text`, or `textarea` if `maxLength` > 200 or `"format": "textarea"` |
| `"type": "integer"` or `"number"` | `number` |
| `"type": "array"` with `items.type == "string"` and no `items.enum` | `string_list` — a list editor |
| `"type": "object"` with an object `additionalProperties` | `entry_group` — repeating entries |
| a string marked as a secret name (see below) | `secret_name` — a picker of names from the store |

**Constraint keywords honoured per control**

| Control | Keywords |
|---|---|
| `number` | `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` (integers vs. floats decided by `type`) |
| `text` / `textarea` | `minLength`, `maxLength` |
| `string_list` | `minItems`, `maxItems`, and `items.minLength` / `items.maxLength` |
| `entry_group` | `minProperties`, `maxProperties`, plus each entry field's own constraints |

A numeric keyword whose value is a boolean is ignored — `"minimum": true` silently becoming `1` would be a bound nobody wrote. `minLength`/`maxLength`/`minItems` and friends must be non-negative integers.

**A string with no `maxLength` stays single-line.** An author who declared no limit has said nothing about length, and guessing "probably long" from silence would put a 20-row box under a field holding a city name. Say `"format": "textarea"` if you want one regardless.

### What makes a property unrenderable

Unrenderable is not the same as dropped. The property is listed on the page by name, with the reason, and it is still editable in the raw TOML tab. A setting the form does not show and does not save is an operator's setting quietly lost.

| Schema | Reason given |
|---|---|
| `oneOf`, `anyOf`, `allOf`, `not`, `if` | *This setting is described with 'oneOf', which this page does not render. Edit it in the raw config.toml tab.* |
| `"type": ["string", "null"]` | *This setting allows more than one kind of value…* |
| `type` present but not a string | *This setting's 'type' is not a type name…* |
| `enum` that is not a non-empty list | *This setting lists its choices in a way this page cannot read…* |
| `enum` containing non-strings | *Some of this setting's choices are not text…* |
| More than 200 enum choices | *This setting offers more than 200 choices…* |
| `array` with no object `items` | *This list does not say what it holds…* |
| `array` of anything but plain strings | *This is a list of something other than plain text, which this page does not render yet.* |
| `object` without `additionalProperties` | *This setting is a group of other settings, which this page does not render yet.* |
| No `type` at all | *This setting does not say what kind of value it holds…* |
| Beyond the 200th property | *This schema declares more than 200 settings…* |

### `$ref` — in-document only, and nothing is ever fetched

Any `$ref` that does not begin with `#` refuses the **whole schema**, not just that property:

> config.schema.json points at something outside itself ('https://example.com/s.json'). This core never fetches a schema from anywhere: a plugin's schema has to be complete on its own, so these settings are edited as text instead.

That covers `http://`, `file://` and every reference to another file on disk. No HTTP client is imported in that module and no path is opened, so a remote reference cannot be followed even by accident (ADR-0015).

An in-document `#/a/b` pointer is walked over the document's own **object keys** — no array indices, and only the two escapes JSON Pointer defines (`~1` for `/`, `~0` for `~`). A pointer that leads nowhere, or lands on something that is not an object, is a refusal rather than an empty schema: "this field silently has no rules" is the failure mode a validator must never have. Cycles and depth beyond 8 hops are refused as *"refers back to itself"*.

A refused schema is **not** a failed page. The plugin's settings fall back to the raw TOML editor with the refusal message shown, because a plugin with a broken schema is still a plugin whose settings need fixing.

## The secret marker

Two ways to mark a string property as holding the *name* of a secret:

```json
"upstream_api_key_secret": { "type": "string" }
```

```json
"upstream_key": { "type": "string", "x-personacore-secret": true }
```

- **`x-personacore-secret: true`** on a string property (or one with no declared `type`).
- **A property name ending in `_secret`** — the suffix the core already uses everywhere else for "this holds the name of a secret" (`llm.api_key_secret`). A property the rest of the core already treats as a secret reference must not render as a free-text box here, and a second, disagreeing convention would be a hole rather than a feature.

Either way the field renders as a picker of names from the secret store. **The value never leaves the store and is never rendered.**

An `x-` keyword is used rather than a `format` because `format` is a JSON Schema annotation with published meanings, and minting a private one there invites some other validator to try to interpret it.

## The lookup marker — `x-personacore-lookup`

ADR-0016. A field can declare that one of the plugin's *own* tools can fill it: the admin types, the core calls that tool, results are listed, and picking one fills the fields.

The core learns nothing about geography — it learns "this field can be filled by calling that tool, and the result maps onto these keys". The same mechanism gives a music plugin a device picker without the core knowing what a device is.

### Exact shape

```json
"locations": {
  "type": "object",
  "minProperties": 1,
  "x-personacore-lookup": {
    "tool": "search_locations",
    "query_argument": "query",
    "results": "results",
    "label": "label",
    "fill": {
      "label": "label",
      "latitude": "latitude",
      "longitude": "longitude"
    }
  },
  "additionalProperties": { "type": "object", "properties": { … } }
}
```

| Key | Required | Default | Meaning |
|---|---|---|---|
| `tool` | yes | — | The bare tool name as `manifest.toml` declares it. |
| `fill` | yes | — | `{setting name: key in the result}`. |
| `query_argument` | no | `"query"` | The tool argument the typed text is passed as. |
| `results` | no | `"results"` | Where the list of matches sits in what the tool returned. Ignored when the tool returns a bare list. |
| `label` | no | — | The result key shown as that row's text. Without one the row shows its filled values. |

A marker that is not an object, has no `tool`, has no non-empty `fill` object, or whose `fill` contains a non-string key or value, is **silently not parsed** — but the page then says so out loud:

> The search declared for 'locations' does not say which tool to call and which settings a result fills, so it is not offered.

### The five ways a lookup gets refused

Every one of them tells the author on the page, because an author whose typo cost them the feature will otherwise report the missing search box as a mystery.

| Refusal | Note shown |
|---|---|
| The marker is unreadable | *…does not say which tool to call and which settings a result fills, so it is not offered.* |
| `fill` names a setting that is not there | *The search for 'x' says it fills y, which is not a setting in this schema, so it is not offered.* |
| `fill` names a repeating group | *…which holds entries rather than a single value, so it is not offered.* |
| The tool is not declared in the manifest | *…calls a tool this plugin does not declare (x), so it is not offered. A tool the manifest does not declare has no risk level, and the core will not call one.* |
| The tool's declared risk is not `safe` | *…which this plugin declares as 'confirm' rather than 'safe', so it is not offered. A settings page never runs anything that needs confirmation or permission (ADR-0016).* |

**A lookup may only be declared on a top-level property.** A marker on a field *inside* a repeating-group entry is cleared with a note telling you to move it up:

> The search declared on 'locations.label' is not offered: declare it on 'locations' itself and a result then fills the entry's fields.

The reason: a lookup belongs to a whole entry, not to one box inside it. What a result fills is the marked field's *siblings*, and a marker on a single entry field has no siblings to name.

**Where `fill` names things.** For a repeating group, the names are the *entry's* own properties. Anywhere else, they are the marked field and its siblings at the same level.

### What happens at search time

1. The request names a **field**, never a tool. The schema decides which tool that field authorises.
2. `authorise_lookups()` deletes every lookup whose tool the manifest does not declare, or declares as anything but `safe`. Deletion rather than a flag: a refused lookup does not exist for anything downstream, so there is no second code path where a check might be forgotten.
3. The tool is called through the ordinary plugin-host path with `risk_ceiling=safe`, so it is audited and subject to every other rule. The admin's search is separately recorded — *"an admin searching for a town is a thing that happened, and the log should say so"*.
4. Results are read as data.

The query is capped at 200 characters (`MAX_LOOKUP_QUERY_CHARS`). An empty query is refused before anything is called.

### How results are read

Untrusted from the first character. What the tool returned is parsed as JSON and then *read*, never interpreted.

- Content over 256 KiB is refused unread.
- Two payload shapes are accepted: **a bare list**, or **an object holding the list under the `results` key**. Anything else is refused with *"…it has no 'results' list in it, so nothing is shown."* An object whose `results` key is absent yields no rows rather than an error.
- At most **25** rows are kept (`MAX_RESULTS`).
- From each row, only the keys `fill` names are taken. A key the schema did not ask for never leaves the reader.
- Each value is coerced to text and truncated to **200** characters. `7.0` becomes `7`, `true` becomes `"true"`, and a value that is a list or an object yields nothing at all — a coordinate is not a structure, and flattening one would be interpreting the result.
- A row that fills nothing is dropped rather than shown as an empty option.

Nothing here builds a path, a URL or a command out of any of it.

### The tool on your side

Return the three things the setting needs and nothing else. Weather's:

```python
class LocationMatch(BaseModel):
    label: str
    latitude: float
    longitude: float

class LocationSearchResult(BaseModel):
    available: bool
    query: str
    summary: str
    results: list[LocationMatch] = []
```

Anything extra would be data the core carries around and never uses.

## Repeating groups

An `object` whose `additionalProperties` describes one entry becomes a repeatable set of entries, each keyed by a name the operator supplies — `[weather.locations.home]`, `[weather.locations.cabin]`.

Rules worth knowing before you design one:

- Each entry property goes back through the same classifier, so an entry's `latitude` is the same bounded number control a top-level `latitude` would be.
- **One unrenderable entry field makes the whole group unrenderable.** Deliberately: the alternative is a form that saves the fields it understood and leaves the rest of the entry behind, which is how an operator loses a setting without being told.
- **Entries inside entries are not supported.** One level only — a tree is a different control with a different set of things to get wrong.
- More than 20 fields per entry is unrenderable.
- `minProperties` is what makes "removing the last entry" refusable. Weather sets `"minProperties": 1` because the plugin will not start without a location; without it, the form would happily empty the list and break the plugin.
- At most **200** entries and **500** list rows are accepted per submission.

### Entry names

The name an operator types becomes a bare TOML key in a header the core writes, so it is checked against a pattern, before use, rather than escaped into something plausible:

```
^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$
```

1–64 characters of letters, digits, hyphens and underscores, starting with a letter or a digit. Nothing outside that set can end a header early, start a second table, open a comment, or introduce a newline — so there is no quoting to get right and no injection to miss.

> 'my.place' cannot be an entry name. Names are 1-64 characters of letters, digits, hyphens and underscores, starting with a letter or a digit — they become the name of a section in config.toml.

## Which table the schema describes

ADR-0015 says the schema describes "the same table `config.toml` already holds" without naming that table, so the core works it out from the file, in this order:

1. If any property the schema declares is already a **top-level key** of the document, the schema describes the document root.
2. Otherwise, if the document has exactly one top-level key and it is a table, the schema describes **that table**. This is the shape every bundled plugin uses.
3. Otherwise, if the document is empty, the **plugin's own name with hyphens turned into underscores** — `random-prompt` → `[random_prompt]`.
4. Anything else — several top-level tables, none matching — is ambiguous, so **the form is not offered** and the raw editor takes over.

The practical advice: put your settings under a single named table and make sure a fresh `config.toml` already contains it. Guessing here would write an operator's settings into a table your plugin never reads, which looks exactly like the save silently failing.

An unwritten `config.toml` is not an error — the form renders from the schema's defaults with nothing filled in.

## See also

[Plugin Contract](Plugin-Contract) · [Manifest](Plugin-Manifest) · [Runtime Environment](Plugin-Runtime-Environment) · [Weather walkthrough](Plugin-Walkthrough-Weather)

Deferred and worth knowing about so you do not design around its absence: ADR-0015 records that plugins contributing their **own UI** — a page per plugin, a plugin's functions surfaced on a main page — is a larger contract needing its own decision. This page covers configuration only.
