# Walkthrough: the Weather Plugin

The reference plugin, read section by section. Spec §5.1 calls it "living documentation": it is the worked example of the whole contract, and it is written to be read rather than skimmed. Read this after [Plugin Contract](Plugin-Contract) and before writing anything of your own.

Source: `plugins/weather/` — `manifest.toml`, `config.toml`, `config.schema.json`, `main.py`. Tests: `tests/plugins_bundled/`.

It ships inside the image and is copied into `<appdata>/plugins/weather/` on first run ([Lifecycle](Plugin-Lifecycle)).

## Why this plugin exists

It is deliberately dull. It reads a public forecast API that **needs no account and no API key**, which is exactly why the spec names it: a reference plugin that needed a credential would stop being the simple example. The source says so out loud:

> Do not swap in a service that needs a credential — that would drag a secret into the reference plugin and stop it being the simple example.

## `manifest.toml`

### `[plugin]`

```toml
name        = "weather"          # must equal this folder's name, exactly
version     = "1.1.0"            # the plugin's own version; semver -- bumped whenever this plugin's files change
contract    = "2.x"              # plugin contract targeted — "2.x" = any 2.y core
transport   = "stdio"            # spawned as a subprocess of the core
entry       = "python main.py"   # relative to this folder; absolute paths are refused
description = "Current conditions and a short forecast for locations set up in config.toml."
```

Nothing surprising, and that is the point: `contract = "2.x"` is the right answer for almost every plugin ([Contract Versioning](Plugin-Contract-Versioning)), and `entry` is a bare interpreter name plus a relative script, which is the shape the security checks are built around ([Manifest](Plugin-Manifest)).

### `[permissions]`

```toml
network = ["api.open-meteo.com", "geocoding-api.open-meteo.com"]
secrets = []
```

**Two hosts, and both are stated so an admin reviewing this plugin can see exactly what it reaches**: the forecast API, and the place-lookup service used to turn "SW1A 1AA" or "Paris, France" into coordinates.

The second host arrived with ADR-0016's search-and-fill feature, and the ADR names the trade explicitly: *"A plugin that offers a lookup will usually need a network permission it did not previously need. That is visible in its manifest, which is the point: the cost of the convenience is stated where an admin reviews it."*

`secrets = []` because Open-Meteo needs no key. `paths` is absent, which means the same as empty.

**Remember that `permissions.network` is not enforced for stdio plugins (ADR-0012).** This plugin polices itself instead — see `new_client()` below. The test suite is what actually keeps the declaration true: `test_weather_code_and_manifest_agree_on_the_allowlisted_host` asserts the manifest's host list equals the module's `NETWORK_HOSTS` constant.

### `[tools.*]`

```toml
[tools.get_forecast]
risk = "safe"
description = "Look up the forecast for a configured location."

[tools.search_locations]
risk = "safe"
description = "Find the coordinates of a place by name, for setting up a location."
```

The manifest's own comment explains the risk choice better than a rule would:

> safe: runs without asking. Correct here because reading a public forecast changes nothing and cannot be undone wrongly. Anything irreversible — unlocking, sending, buying, deleting — is never "safe" (spec 7).

`search_locations` **must** be `safe`, because it is the tool `config.schema.json` nominates for its `locations` setting. The core refuses to wire a lookup to anything else: a settings page is never a route to something irreversible (ADR-0016). Reading a public gazetteer is exactly as safe as reading a public forecast — it changes nothing, and the only thing it sends is a string an admin typed into a form on purpose.

### `[events]`

```toml
publishes  = []
subscribes = []
```

Empty, with a comment noting that a weather-alert publisher would list its topics under `publishes`. As [Events](Plugin-Events) explains, these lists are currently read by nothing.

## `config.toml`

```toml
[weather]
default_location = "home"
units = "metric"
look_up_unknown_places = true
forecast_days = 3
timeout_seconds = 10

[weather.locations.home]
label     = "London"
latitude  = 51.5072
longitude = -0.1276
aliases   = ["London", "home"]
```

Everything under one named table, `[weather]`, which is what lets the core work out which table the schema describes ([Configuration](Plugin-Configuration)).

**The comments are the documentation.** The file's header says why:

> The admin UI edits this file in place, with validation, so keep the keys flat and obvious and keep these comments — they are the field help.

And it states the rule that matters most:

> Nothing secret ever goes in here. This file is backed up with appdata and is readable by anyone who can open the admin UI.

### `look_up_unknown_places` — a privacy trade, stated

On by default, and the reasoning is the interesting part:

> telling someone their own postcode does not exist, while holding the coordinates for it, is not a privacy feature.

Turn it off and only the configured locations answer, and nothing anyone says reaches a lookup service. The setting is not a toggle someone invented; it is a stated trade with both sides written down where the person making the choice will read it.

**Note the asymmetry with `search_locations`.** The admin's settings-page search *does not consult this setting*, and the code says why: `look_up_unknown_places` is about what happens to something a person said near a microphone. An admin searching in their own settings form is one deliberate act by the person who owns the system, on a string they typed themselves. Refusing an admin their own settings page protects nobody.

### `[weather.locations.<name>]`

The repeating group. `label` is what the assistant calls the place back to you; `aliases` are the other names a person might actually use for it.

Aliases exist because refusing them was indefensible: a place configured as `home`, labelled "Jordan", asked for by its own postcode, was answered with "I don't know that" — while the plugin held the coordinates. **Matching a name against a place the household set up is not a lookup; it is recognising something you were told.**

## `config.schema.json`

This is what turns the settings page from a TOML textarea into a form (ADR-0015). Every construct on it is one the core actually renders — see [Configuration](Plugin-Configuration) for the full supported subset.

| Property | Schema | Control |
|---|---|---|
| `default_location` | `"type": "string", "minLength": 1` | single-line text |
| `look_up_unknown_places` | `"type": "boolean", "default": true` | toggle |
| `units` | `"enum": ["metric", "imperial"]` | dropdown |
| `forecast_days` | `"type": "integer", "minimum": 1, "maximum": 7` | bounded number |
| `timeout_seconds` | `"type": "number", "exclusiveMinimum": 0, "maximum": 60` | bounded number |
| `locations` | `"type": "object"` with `additionalProperties` | repeating group |

Three details worth copying:

**`"minProperties": 1` on `locations`.** The plugin refuses to start without a location, so the form must not be the thing that empties the list. This is how the core knows to refuse removing the last entry — and it is asserted in the test suite so a tidy-up cannot delete it.

**The entry's own constraints are real.** `latitude` is `"minimum": -90, "maximum": 90`; `longitude` is `-180`/`180`. Each entry field is rendered by the same branch and validated by the same check a top-level field of that type would be.

**`"required": ["label", "latitude", "longitude"]` inside the entry**, with `aliases` optional.

### The lookup marker

```json
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
}
```

Declared on `locations` itself — the **group**, not a field inside it — because a result fills a whole entry. A marker on `locations.label` would be cleared with a note telling you to move it up ([Configuration](Plugin-Configuration)).

The `fill` map's keys are the *entry's* properties. The values are keys in whatever the tool returned.

Type a town in the settings page, pick a match, and the three coordinate fields fill in. The coordinate fields stay editable for anyone who already knows them, or whose place is not in a gazetteer.

## `main.py`

### Configuration, validated by the plugin

```python
class WeatherConfig(BaseModel):
    default_location: str = "home"
    units: Literal["metric", "imperial"] = "metric"
    forecast_days: int = Field(default=3, ge=1, le=7)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    locations: dict[str, Location] = Field(default_factory=dict)
    look_up_unknown_places: bool = True
```

The core does not validate `config.toml` — the plugin owns that shape, so the plugin checks it. Pydantic is used only because it turns a typo into one readable sentence; plain `dict` handling would satisfy the contract too.

`load_config()` handles every way the file can be wrong, and each one produces a `ConfigError` with a sentence rather than a traceback:

| Failure | Message |
|---|---|
| missing | `config.toml is missing — the plugin needs its own settings` |
| unreadable | `config.toml could not be read — …` |
| bad TOML | `config.toml is not valid TOML — …` |
| schema violation | `config.toml: 'units' Input should be 'metric' or 'imperial'` |
| no locations | `config.toml: no locations are set up — add a [weather.locations.<name>] block` |
| `default_location` not among them | `config.toml: default_location 'x' is not one of the locations set up here (home, cabin)` |

The last two are cross-field checks pydantic cannot express by itself, done afterwards by hand. Both are the kind of mistake a real operator makes.

And `main()` prints it to **stderr**, not stdout, then exits 1:

```python
except ConfigError as exc:
    # stdout belongs to the MCP protocol on a stdio plugin. Diagnostics go
    # to stderr or they corrupt the conversation.
    print(f"weather plugin cannot start: {exc}", file=sys.stderr)
    return 1
```

That sentence is what an operator reads next to the plugin in the admin UI ([Runtime Environment](Plugin-Runtime-Environment)).

### `new_client()` — one place builds the HTTP client

```python
def new_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)
```

**`follow_redirects=False` is deliberate**: the manifest allowlists specific hosts, and silently following a redirect somewhere else would make that declaration a lie. Since the core does not enforce the allowlist for stdio plugins, this line *is* the enforcement.

It is also the one seam the tests swap for an `httpx.MockTransport` ([Testing](Plugin-Testing)).

### Everything from the network is hostile until proved otherwise

This is the part worth reading closely. The rule: *nothing should be able to do more than shorten the forecast.*

**Size first.** `MAX_RESPONSE_BYTES = 256 * 1024`, checked before parsing. A forecast is a few kilobytes; anything vastly larger is a malfunction or an attempt to make the plugin chew through memory.

**Structure, defensively.** `_column()` returns `[]` for a `daily` that is not a dict or a column that is not a list. No `KeyError`, no assumption that the arrays are the same length.

**Numbers, coerced and bounded.**

```python
def _number(values, index, *, low, high):
    if index >= len(values): return None
    value = values[index]
    if isinstance(value, bool) or not isinstance(value, int | float): return None
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high: return None
    return round(number, 1)
```

Booleans are excluded explicitly because they are `int`s in Python. Strings are not coerced. `NaN` and `inf` are rejected. Temperatures are bounded to ±150.

**Strings, almost never.** The date is the only string passed through, and only if it matches `^\d{4}-\d{2}-\d{2}$` exactly.

**The one text field is looked up locally.** The API returns human-readable weather text in some responses; the plugin **ignores it** and looks the numeric WMO code up in its own `_CONDITIONS` table instead, so the only English that reaches the persona is English the author wrote. An unrecognised code becomes `"unsettled"` rather than repeating the number at the user — *a code we do not recognise is a code we cannot describe.*

The geocoder results get the same treatment: `as_location()` coerces the coordinates, lets the `Location` model bound them, assembles the label from at most three fields, and truncates it to 120 characters.

**Why this matters beyond correctness:** everything a tool returns is fenced and handed to a model ([Tools](Plugin-Tools)). Text you pass through from a third party is text a third party got to put in front of the model. The fence is the boundary; not passing it through in the first place is the belt.

### Failure is an outcome, not a crash

`ServiceUnavailable` carries the sentence the persona says — no exception class names, no URLs, no stack. Every outbound path raises only this one type, and every tool catches it and returns a result:

```python
except ServiceUnavailable as exc:
    return ForecastResult(
        available=False, location=asked, units=config.units, summary=str(exc)
    )
```

Four sentences it can carry, and none of them leaks anything:

- *I can't reach the weather service right now.* (connect failure, DNS, timeout, TLS, protocol error)
- *The weather service turned me away (error 503).*
- *The weather service sent back something I couldn't make sense of.* (unparseable **and** absurdly large — from where the listener is standing they are the same thing)
- the place-lookup equivalents of the first three

`available=False` is the honest bit: the agent can see there is no forecast without parsing prose.

### `get_forecast` — the resolution order

```python
asked = (location or config.default_location).strip()

# 1. exact key match
place = config.locations.get(asked.casefold())

# 2. label or alias match
if place is None:
    place = next((p for p in config.locations.values() if p.matches(asked)), None)

# 3. only then, the network — and only if the operator allowed it
if place is None and config.look_up_unknown_places:
    place = await geocode(config, asked)
```

**Configured places first: no network, and the household's own names ("home", "the cabin") beat anything a gazetteer thinks they mean.** The argument arrived from the model, which got it from a person talking; it is not a lookup key until it matches one.

One sharp edge in step 1: the entry *key* is compared against `asked.casefold()`, so a location keyed `Home` (which the settings form permits — entry keys allow uppercase) will not match `"home"` by key. It will still be found by its `label` or an alias in step 2. Lowercase your entry keys.

The `matches()` method casefolds the label and every alias, so alias matching is case-insensitive either way.

When nothing is found, the message adapts to the setting: *"I know about: home, cabin."* when lookups are off, *"Try a town and its state or country."* when they are on.

### `search_locations` — the settings-page tool

Returns exactly what the schema's `fill` map needs and nothing else:

```python
class LocationMatch(BaseModel):
    label: str
    latitude: float
    longitude: float
```

> Deliberately the three things a `[weather.locations.<name>]` block needs and nothing else. The admin form's schema maps these keys onto its fields, so anything extra here would be data the core carries around and never uses.

The wrapper has the same shape every other tool here has: `available` says whether the answer is real, `summary` is a sentence, and the structured part is separate. The admin form reads `results`; a persona would read `summary`.

An outage is still an outcome, not a crash — *a settings page gets a sentence rather than a protocol error it has to guess the meaning of.*

### One nicety worth stealing

```python
def expand_us_state(query: str) -> str | None:
    """"Jordan MN" -> "Jordan, Minnesota", or None if it does not look like one."""
```

`"Jordan MN"` finds nothing in the gazetteer; `"Jordan, Minnesota"` finds it. Americans type the first form, so the abbreviation is expanded and the search retried rather than the person being told their own address does not exist.

This is not a weather feature. It is the same principle as `aliases`: the plugin is obliged to try the thing a real person would actually say before it gives up.

## See also

[Template walkthrough](Plugin-Walkthrough-Template) · [Manifest](Plugin-Manifest) · [Configuration](Plugin-Configuration) · [Tools](Plugin-Tools) · [Testing](Plugin-Testing)
