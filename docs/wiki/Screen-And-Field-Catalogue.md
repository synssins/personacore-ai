# Screen and field catalogue

**Purpose:** this is the input to the UI design work. It inventories every
screen the admin surface has today, every control on it, every state it can be
in, and the constraints a design has to satisfy. It is deliberately about
*what is there*, not *what it should look like* — the look is the design's job.

**Status of this page:** it was written as the input to the UI design work, and
it inventories the pre-design test surface that has since been deleted. The
designed admin UI now serves `/admin` — Chat, Health, Models, Plugins, Core
settings, Logs — while Access keys and Personas are not built yet and are
API-only. The *structure* recorded below is still accurate about what the core
can do, and is what the design had to satisfy; the screen names and paths in
Screens 1 and 2 below are not what ships. Screens 3 and 4 were added later and
describe the real shipped `/admin/chat` and `/admin/voice` screens directly,
by their real names and paths.

**Read alongside:** [Security-Model](Security-Model),
[Policy-Profiles](Policy-Profiles), [Plugin-Configuration](Plugin-Configuration),
[LLM-Roles](LLM-Roles).

---

## Constraints the design must satisfy

From spec §9 and the decision records, these are requirements rather than
preferences:

| # | Constraint | Source |
|---|---|---|
| 1 | **Click-first.** Anything an operator does routinely is done by clicking, not by typing a file. Hand-editing is a fallback, never the only route. | spec §9 |
| 2 | **Plain-English errors.** Every failure says what happened and what to do about it. No stack traces, no `KeyError`, no bare HTTP codes. | spec §9 |
| 3 | **Thematic consistency.** One visual language across every surface. | spec §9 |
| 4 | **Secrets are never displayed.** Secret pickers list *names*. No screen ever renders a secret value, not even masked. | ADR-0015 |
| 5 | **A dangerous state must be visible.** The admin auth bypass and (later) keyless access appear in health, not only in a config file. | ADR-0018 |
| 6 | **Defaults are marked as defaults.** A box pre-filled from a schema default must be visibly different from one holding a saved value — "the plugin is using this" and "the plugin would use this if you saved" are different facts. | ADR-0015 |
| 7 | **A broken plugin stays configurable.** The most likely reason to open a plugin's settings is that a bad value stopped it starting. | plugin-author-guide §8 |
| 8 | **Destructive actions are distinguishable at a glance** from ordinary ones. Uninstall sits next to switch-off today. | spec §9 |

---

## Screen 1 — Dashboard (`/test`)

One long page today. A real design will almost certainly split it; the sections
below are the natural seams.

### 1.1 Banner

Names the running instance.

### 1.2 API endpoints

A static list of the URLs this instance exposes, so an operator can copy one
into a client. Read-only.

### 1.3 System health

| Element | Type | Notes |
|---|---|---|
| Overall state | Status word | One of `ok`, `degraded`, `down` |
| Checked-at | Timestamp | |
| Component table | Table | Columns: name, state, detail, facts |

Plugin rows translate the supervisor's internal words into operator words:
`healthy`→running, `degraded`→**crashed**, `starting`→starting,
`failed`→crashed, `disabled`→stopped. The `degraded`→crashed mapping is
deliberate and load-bearing: a plugin that has never started successfully is
"degraded" internally, and calling that "running" put a healthy-looking row next
to a plugin that could not run at all.

**Design need:** the `detail` column carries the actual reason a thing is broken
and is currently a cramped table cell. It is the single most useful string on
the page.

### 1.4 LLM connections

One panel per role — see [LLM-Roles](LLM-Roles). `interactive` is required and
comes first; every other role falls back to interactive's connection, and a role
with its own connection also gets its own circuit breaker, so one dead host
cannot take the others down with it.

Per panel:

| Control | Type | Constraint |
|---|---|---|
| Role name + "(required)" marker | Heading | |
| Purpose | Prose | One line saying what this role is for |
| Base URL | Single-line text | An OpenAI-compatible endpoint. **A pasted URL is trimmed** — `/chat/completions`, `/completions` and `/models` suffixes are stripped, because pasting the URL out of a client's config is the common case (ADR-0011) |
| Model name | Single-line text | Exactly as the LLM host spells it |
| Test connection | Button | Runs a live probe |
| Save connection | Button | |
| Clear and fall back to interactive | Button | Not offered for `interactive` itself |

**States:** untested / reachable / not reachable, each with a detail line. A
whole-section error state exists for "the settings file could not be read", in
which case the panels are not editable at all.

**Design need:** five stacked panels is the current shape and it reads as five
mandatory forms. The optional roles need to be visibly secondary.

### 1.5 Chat

The diagnostic that matters most. One turn, not a conversation.

| Control | Type |
|---|---|
| Message | Multi-line text |
| Send one turn | Button |

**Last turn** shows the reply and — critically — **how many tools were offered
and which were called**. That one line is what distinguishes "no plugin is
loaded" from "the model chose not to call it", and it is the first thing to look
at when a plugin appears to do nothing. See
[Scenario-Debugging-A-Plugin](Scenario-Debugging-A-Plugin).

### 1.6 Plugins

| Control | Type | Notes |
|---|---|---|
| Reload plugins | Button | Rescans the folder *and* tells the supervisor |
| Plugin package | File picker | `.zip` only (ADR-0013) |
| Replace if already installed | Checkbox | |
| Install plugin package | Button | |

Two lists:

- **Loaded (n)** — per row: switch off, switch on, uninstall, and a link to the
  plugin's own page.
- **Failed to load (n)** — per row: uninstall, plus the reason it failed.

**Uninstalling a running plugin stops it first.** A plugin must be stopped
before its folder can be removed.

### 1.7 Plugin settings (legacy raw editor)

A raw `config.toml` textarea per plugin, with a character limit. Superseded by
the per-plugin page below but still present. A design should treat the
per-plugin page as the route and this as the escape hatch.

### 1.8 Personas

| Control | Type |
|---|---|
| Persona list | Table |
| Make default | Button per row |

**Unbuilt but decided (ADR-0017):** several personas installed at once, each
suggesting a voice with the pairing breakable, selection by model name through
`/v1/models`, and a persona manager with create and edit. Design should
anticipate a persona being a first-class installable thing alongside plugins,
not a dropdown.

### 1.9 Core settings

| Control | Type | Notes |
|---|---|---|
| Whole settings document | Multi-line JSON | The entire settings file as raw JSON |
| Save settings | Button | |

**This is the least designed thing on the page** and the most obviously in need
of it. See [Core-Settings](Core-Settings) for the field inventory a real form
would have to cover.

### 1.10 API keys

| Control | Type | Notes |
|---|---|---|
| Issued (n) list | Table | Note, profile summary, revoke |
| Revoke | Button per row | |
| Note | Single-line text | What this key is for |
| Policy profile | Multi-line JSON | Raw profile document |
| Issue key | Button | **The key value is shown once, on issue, and never again** |

**Design need:** the policy profile is raw JSON today and is the second
strongest candidate for a real form after core settings. The fields it needs are
in [Policy-Profiles](Policy-Profiles).

**Unbuilt but decided (ADR-0018):** keys become optional. A keyless request maps
to the anonymous profile, which is conversation-only by default. The screen will
need a toggle for that, an optional source-address allowlist, and a visible
indication in health when it is on.

### 1.11 Trace

Recent request traces. Read-only. See [Audit-And-Trace](Audit-And-Trace).

---

## Screen 2 — Plugin page (`/test/plugins/{name}`)

One page per installed plugin, reached from the plugins list.

### 2.1 Header and health

Plugin name, plus its health: state, detail, restart count, recent errors.
**This section renders even when the plugin failed to load** — that is the point
of it.

### 2.2 Switch on, switch off, uninstall

Three buttons. Uninstall is destructive and currently looks identical to the
other two.

### 2.3 Settings form

Generated from the plugin's `config.schema.json` (ADR-0015). The core renders a
**bounded set of control kinds** and never executes anything from a schema.

| Kind | Control | Notes |
|---|---|---|
| `toggle` | Checkbox | |
| `choice` | Dropdown | `(not set)` option when the field is optional |
| `number` | Number input | Min/max enforced |
| `text` | Single-line text | Length limits enforced |
| `textarea` | Multi-line text | |
| `string_list` | List editor | Per item: move up, move down, remove. Plus "add an entry" |
| `secret_name` | Dropdown of secret **names** | Never values. `(not set)` option |
| `entry_group` | Repeating group | Several fields per entry, each entry named by the operator. Per entry: its own fields and its own search. Plus "add an entry" |

**Per-field state:** a field pre-filled from the schema's default is marked as
such (constraint 6 above).

**Search-and-fill (ADR-0016):** any field may declare a lookup. The operator
types a query ("Jordan MN"), presses Search, picks a result, and one or more
fields fill in. Weather's coordinates are the first user of this; the mechanism
is generic and is not special-cased to weather. **At most one search runs per
request**, so at most one place on the page shows results at a time.

**Save settings** applies immediately by restarting that plugin. The save notice
names the file written and what went into it.

### 2.4 Raw config.toml

A textarea with a character limit, for anything the form cannot express. Present
as a tab alongside the form, not a replacement for it.

---

## Screen 3 — Chat (`/admin/chat`)

Added after this catalogue was written, and not part of the design canvas in
its original form — see `chat.py`'s module docstring for the approved
deviation. Recorded here because it is now the screen every operator lands on
first.

### 3.1 Layout

A rail of earlier conversations down the left, the message list in the middle,
the composer pinned below it. Picking a conversation from the rail swaps the
message list and updates the address bar without a full page navigation; the
same link works as an ordinary page load with no script running.

### 3.2 Header

| Control | Type | Notes |
|---|---|---|
| Persona and voice | Dropdown | Chooses which persona answers **this conversation only** — it does not move the core's default persona |
| Use this persona | Button | Applies the picker's selection |

### 3.3 Composer

| Control | Type | Notes |
|---|---|---|
| Message | Multi-line text, auto-growing | |
| Microphone | Toggle button | Dictation through the browser's own speech recognition API, not a core speech-to-text engine. Sending the message or leaving the page ends it |
| Send | Button | Disabled if chat has no runner wired in |

**Last exchange** shows the reply plus the same "how many tools were offered
and which were called" line the old dashboard's chat panel had (§1.5 above).
A sent message is drawn immediately and marked pending; the rendered version
from the server replaces it in place. A reply is appended as one fragment —
the rest of the page, including scroll position, is untouched unless the
reader was already at the bottom.

**Not present:** generation statistics (tokens in/out, tokens per second,
time to first token). Designed, not built.

---

## Screen 4 — Speech engines and voices (`/admin/voice`, `/admin/voice/voices`)

Voice reached the admin UI after this catalogue's "deliberately not in this
catalogue" note below was written; that note is now stale for this part and
is corrected here rather than silently deleted.

### 4.1 Speech engines (`/admin/voice`)

One row per built-in engine, each with its own switch — turning one off stops
only that engine and leaves every other persona's voice untouched.

| Control | Type | Notes |
|---|---|---|
| Engine switch | Toggle | Absent entirely (not just disabled) for an engine this build cannot run, with the reason printed beside it — a greyed-out switch is still a control somebody will click |
| Save | Button | Applies immediately: starts or stops the engine, no restart |

**States:** running / switched off / switched on but not yet running / failed,
each with a plain-English detail. Switching off an engine that a persona's
voice belongs to warns, in place, that the persona will reply in text.

### 4.2 Voices (`/admin/voice/voices`)

| Control | Type | Notes |
|---|---|---|
| Choose a .zip… | File picker | Any zip whose contents were never repackaged — a stock voice download works unmodified |
| Engine | Dropdown | Which engine the voice belongs to |
| Id | Single-line text | |
| Replace if already installed | Checkbox | |
| Install | Button | |

Every installed voice is shown in **one list across every enabled engine**,
labelled `Name (engine-id)` — e.g. `GLaDOS (vits-onnx)` — so an operator never
picks an engine as a separate step.

**Per-voice manage/export form:** name, description, language, pack version,
oldest supported engine version, engine-specific synthesis numbers (e.g.
length/noise scale for `vits_onnx`), pacing (sentence/clause/paragraph gap,
sentence/clause marks), pronunciation corrections (one per line), licence
(SPDX id, source, full text) and attribution (author, contact, text). All of
it is optional — a voice with nothing filled in still exports as a valid pack.

A **test-speak** control speaks a fixed sample sentence through the voice,
never a bare word.

---

## Screen 5 — Your Profile (`/admin/profile`)

Added after this catalogue was written (ADR-0030). Deliberately a separate
screen from `/admin/account`, which stays the admin's view of everybody else;
this one belongs to whoever is signed in and renders on all three identity
doors, because it keys on the signed-in id rather than on an account record.
Reached from a gear beside your name at the bottom of the sidebar. Full detail
in [Your Profile](Your-Profile).

### 5.1 Audio playback

| Control | Type | Notes |
|---|---|---|
| Play replies automatically | Switch | On by default for a person who has never chosen. Disabled, showing the forced state, when an administrator has set a household-wide rule from Core settings |
| Save | Button | Hidden while the switch is locked by an administrator override |

**States:** editable (no override) / locked on / locked off, the last two each
with a line naming that an administrator set it and which way.

---

## Cross-cutting states every screen needs

| State | Where it appears today |
|---|---|
| **Notice after an action** | A one-line message carried through a redirect. Names what happened, and for saves, what was written where |
| **Unchanged** | A save that changed nothing says so rather than claiming success |
| **Refusal** | An action the current profile may not take |
| **Validation failure** | Per-field, in plain English, with the submitted values preserved |
| **Empty** | No plugins, no keys, no personas, no traces |
| **Broken-but-editable** | A plugin that failed to load, whose settings must still open |

---

## Deliberately not in this catalogue

- ~~**Voice.**~~ No longer true: voice is core, shipped, and has its own
  screens — see Screen 4 above. Left struck through rather than deleted so a
  reader who remembers this line knows where it went.
- **Memory and mood.** P1. An inspectable mood state was agreed; it has no
  screen yet.
- **Plugin-contributed UI.** Approved and deliberately last
  (plugin-author-guide §7). It is a contract, not a screen: a plugin would
  declare panels and actions from a bounded vocabulary. A design that assumes
  the admin surface is entirely core-authored will need revisiting.
- **Config history and rollback.** Approved, item 21, needs a decision about
  where history lives before any design.
