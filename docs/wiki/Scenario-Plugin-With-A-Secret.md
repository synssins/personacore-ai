# Scenario: A Plugin That Needs a Secret

Your plugin needs an API key or a token, and you want it to have that credential without it ever appearing in a manifest, a config file, a log, a backup you email to yourself, or git.

## The shape of the answer

Three separate things, and keeping them separate is the whole design:

| Thing | Where it lives | Who sees it |
|---|---|---|
| The **value** | A file in `<appdata>/secrets/plugins/<plugin-name>/<name>` — this plugin's own namespace | The core, and only this plugin |
| The **name** | `permissions.secrets` in `manifest.toml` | Everyone; it is meant to be reviewed |
| The **delivery** | An environment variable on the plugin's process | Only that process |

**A plugin never sees the whole store.** It is handed a scoped view containing exactly the names its manifest declared, so a careless or compromised plugin cannot read a secret it never asked for (spec §5.1).

## 1. Create the secret

**There is no terminal step, on purpose.** The operator was never told there is a secret store, never named one and never chose where it goes (ADR-0025 section 4): the plugin's manifest asks for a credential, a box for it appears on a page they are already on, and they paste into it. That is "one action, on the screen they are already on" — nothing here is a file operation an admin has to be trusted to get right.

Two places carry that box:

- **The install screen**, when the package being installed declares `permissions.secrets` — the review step shows one field per request, with its `description` beside it, before the plugin is ever started.
- **The plugin's own admin page** (`Plugins → kitchen-timer`), any time afterwards. Saving posts to `POST /admin/plugins/kitchen-timer/secrets`.

Either way, every posted field is checked against the names *this plugin's manifest* declared before anything is written, so a field that was not asked for cannot land in the namespace, and the namespace is the plugin's own — it could not reach another plugin's credential even if it tried (ADR-0025 §1). The value is never echoed back: once stored, the page only ever says *"A credential is stored."*, never the value, its length, or a masked stand-in of it.

**Leading and trailing whitespace is stripped automatically**, both on the way in and on the way out, so pasting a value with a trailing newline that an editor added is harmless.

Under the hood this still lands as one file per secret, inside a namespace that says who owns it: `<appdata>/secrets/plugins/kitchen-timer/kitchen_timer_api_key` for a plugin's own credential, separate from `<appdata>/secrets/core/` where the core's own live. Worth knowing if you are looking at an appdata backup, not something you are expected to touch by hand.

**Name rules.** Start with a letter; letters, digits, `_`, `.` and `-` after that; up to 64 characters. Windows device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`) are rejected everywhere, including on Linux, because plugin authors develop on workstations and the rules should not differ by host.

**Case matters, exactly.** The name is matched byte for byte against the namespace's own entries, never by constructing a path — otherwise, on a case-insensitive filesystem, a manifest declaring `LLM_KEY` would pass the string check and then be handed the file `llm_key`, a secret it never declared. If you get *"is not present under that exact name … which differs only in case"*, that is this check working.

## 2. Declare it in the manifest

```toml
[permissions]
secrets = [
  { name = "kitchen_timer_api_key", description = "Your Kitchen Timer Cloud API key. Create one at kitchentimer.example under Account -> API keys; the free tier is enough.", required = true },
]
```

Since contract 2.0 each entry is a **table**, not a bare name. One table per line — TOML 1.0 does not allow a newline inside `{ }`.

- **`name`** — required. What you read it as; it becomes a filename.
- **`description`** — **required.** It is printed beside the box the operator pastes into, so write what somebody who has never heard of your service needs: what it is, where to get one, which plan is enough. "API key" is not a description — they can already read the field's name. Your text is shown escaped, never as markup.
- **`required`** — optional, default `true`. `false` means your plugin **starts without it**, and the box is still drawn for an operator whose setup does need one. Use it for a credential that is conditional — an instance that only sometimes sits behind basic auth.

The list defaults to empty, and **empty means none, not everything**. Declaring a secret you do not need is a hole dug for no reason, and a reviewer looking at your manifest is entitled to ask why each entry is there.

If you write the old list-of-names form, the core refuses the plugin and tells you exactly this: see [Contract Versioning](Plugin-Contract-Versioning#what-changed-in-20).

The **value never appears here**, and never in `config.toml` either — that file is backed up, readable by anyone with admin UI access, and routinely committed to a repository by its author.

## 3. Read it in your code

The core hands declared secrets to a stdio plugin as **environment variables named exactly the secret name**:

```python
key = os.environ.get("kitchen_timer_api_key")
if not key:
    raise ConfigError(
        "kitchen_timer_api_key is not set. Add it to the core's secret store "
        "and declare it in manifest.toml under permissions.secrets."
    )
```

Fail with a clear message when it is absent rather than half-working — that message is what appears beside your plugin in the admin UI.

**Your environment is built, not inherited.** A stdio plugin gets a short allowlist of run-the-process variables (`PATH`, `TZ`, `LANG` and a handful more), two forced Python settings, and your declared secrets. Nothing `PERSONACORE_*`, no `PYTHONPATH`, no `HOME`. See [Plugin Runtime Environment](Plugin-Runtime-Environment).

## 4. What happens when it is missing

That depends on the `required` flag you set in step 2.

**`required = false`: the plugin starts anyway.** The variable is simply **absent** from your environment — not present and empty, so `os.environ.get("...")` returns `None` and you can branch on it cleanly. Nothing is shown as waiting or failed. The box stays on the plugin's page for an operator who does need to fill it in later.

**`required = true` (the default): the plugin does not start**, and the failure names the secret:

> Plugin `'kitchen-timer'` cannot start: it declares the secret `'kitchen_timer_api_key'` in its manifest, and Secret `'kitchen_timer_api_key'` has not been supplied for plugin `'kitchen-timer'`. Open the plugin's settings and paste the value into the field asking for it; the plugin is waiting for it.

This is a start-time refusal, before your code runs. It shows in the plugin list beside your plugin, and the plugin's own page shows the same thing as the row's whole point: *"Waiting for a credential: kitchen_timer_api_key — nobody has supplied it yet. Paste the value into the field below and it starts."* That is deliberate: a plugin that starts and then discovers it has no credential fails later, further from the cause, and usually in the middle of somebody's sentence.

The same applies if a plugin declares secrets and the core was started without a secret store at all — it is not started.

## Letting an operator choose which secret

If your plugin should work against a credential the operator names — rather than one hard-coded name — put the field in your `config.schema.json` and mark it as holding a secret's **name**:

```json
"api_key_secret": {
  "title": "API key",
  "type": "string",
  "x-personacore-secret": true
}
```

The marker `x-personacore-secret: true` on a string property does it; a property whose name ends in `_secret` is treated the same way, because that is already what the core means by "this names a secret" everywhere else (`llm.api_key_secret`).

What the admin form then does:

- Offers the **names** already in the store as choices, not a free-text box.
- **Refuses a name that is not there**, with: *"there is no secret called `'x'` in this core's secret store. Add the secret to the secrets folder first, then pick it here. This field holds a secret's name; the value never leaves the store."*
- **Never renders a value.** Values do not leave the store, on any path (ADR-0015).

Note the interaction: a name chosen this way is a *config* value, so your plugin reads it from `config.toml` and then needs that name in `permissions.secrets` too. A secret you did not declare is not one you can read, however it got into your settings.

## What redaction does and does not catch

Structured logging redacts values it can recognise as credentials — fields labelled as such, and `SecretStr` values, which print as a mask if logged by accident.

**Shape-based detection of an unlabelled bare secret was deliberately not added.** A rule that fires on "long random-looking string" fires constantly on ordinary conversation text, and a redactor that cries wolf is one people switch off. The consequence is honest and worth stating: **if you print a raw key into a log line yourself, it will be in the log.** Do not.

The same applies to transcripts. If a person types a credential into a chat, it is in the transcript store like any other message — see [Scenario: Retention and Privacy](Scenario-Retention-And-Privacy).

## Rules to keep

- Never in `manifest.toml`, never in `config.toml`, never in code, never in git.
- Declare by name; receive by environment; fail loudly when absent.
- One secret per file, and treat the appdata backup as a credential store.
- `SecretStr` in the core means `.get_secret_value()` is a deliberate act you can see in review. Keep that habit.

## Related

- [Security Model](Security-Model) — the whole secrets picture, including what the core itself uses.
- [Plugin Manifest](Plugin-Manifest) — `permissions.secrets` in context.
- [Plugin Configuration](Plugin-Configuration) — schemas, forms, and secret-name fields.
- [Scenario: A Plugin With Network Access](Scenario-Plugin-With-Network-Access) — the other permission, and its honest limits.
