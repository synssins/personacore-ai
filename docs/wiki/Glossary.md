# Glossary

Every term of art used across this wiki, in plain language. If a page uses a word you have not met, it should be here.

Terms marked **(project)** are ones PersonaCore added; the rest are industry vocabulary and are defined in spec §2.

---

### Admin UI **(project)**
The themed, click-first browser interface served under `/admin`, and a consumer of the [admin API](Admin-API) like any other client. The root path and `/admin/` both redirect here, onto Chat. Its screens: Chat (`/admin/chat`) — the front door: multi-turn, with a rail of resumable earlier conversations and a persona picker that does not move the core's default — Health, Model connections, Plugins (with per-plugin health, logs, settings, raw config and an install form), Personas, Speech engines with the Voices list beneath it, Core settings, Access keys (under Security) and Logs.

### Agent / agentic
An LLM that can take actions — call tools — rather than only producing text. The **agent loop** is the piece of the core that runs a turn: build the prompt, ask the model, run whatever tool it asks for if that is allowed, feed the result back, stream the answer out.

### Anonymous tier **(project)**
A [policy profile](Policy-Profiles) with everything switched down, for callers with no identity. Its ceilings are enforced in the profile model itself, not in configuration: `safe` risk at most, its own isolated memory scope, no household or per-user memory ever, cannot approve a confirmation, cannot list what plugins exist. A profile that tries to exceed any of those is refused at construction. [ADR-0003](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0003-anonymous-access-tier.md). See [Scenario: Multi-User and Anonymous](Scenario-Multi-User-And-Anonymous).

### Appdata **(project)**
The single mounted volume holding everything that is not rebuildable: plugins, personas, voices, memory, users, secrets, audit, config. **Appdata is the assistant**; the containers are disposable. An upgrade never touches it. Default mount point `/appdata` inside the container. See [Appdata Layout](Appdata-Layout).

### Audit log
A record of every tool call, every event that woke the agent, every confirmation given or refused, and every admin change — timestamped and attributed. Distinct from the **transcript**, which records conversation content. See [Audit and Trace](Audit-And-Trace).

### Barge-in
Talking over the assistant to interrupt it mid-sentence. Part of the P1 voice pipeline; not built.

### Bundled plugin **(project)**
A plugin that ships inside the image (the weather reference plugin and the template). On first run the core **copies** them into appdata and never overwrites what is already there, because appdata is where plugins are actually read from.

### Circuit breaker
Stop hammering a service that is failing, and retry sensibly later. Each [LLM role](LLM-Roles) has its own, so a dead vision host cannot take conversation down with it.

### Confirmation
A human saying yes before a `confirm`- or `restricted`-risk tool runs. The **confirmation channel** is whatever asks the question — a voice prompt, a UI dialog. If there is no channel, the call is refused, not assumed. See [Risk Levels](Risk-Levels).

### Consolidation ("dreaming")
A scheduled reflection pass that merges redundant facts, extracts patterns and promotes stable facts. Part of the P1 memory plugin; not built.

### Contract **(project)**
The versioned promise between the core and everything that plugs into it — the plugin manifest, the event envelope, the policy profile, and the two OpenAI-compatible interfaces. Semver: minor versions only ever add, so existing plugins keep working. A plugin declares which contract version it targets. See [Plugin Contract Versioning](Plugin-Contract-Versioning).

### Core memory blocks
Always-in-context facts about the home and its people, editable by both human and AI. Memory tier L1. P1; not built.

### Dictation **(project)**
Typing by speaking, from the microphone button on the Chat screen. It is the browser's own speech recogniser (the Web Speech API) — not a core STT engine — so the audio goes to the browser vendor's servers, not to this project's LLM host. It ends when you send the message or leave the page. See **STT / TTS**.

### Engine (voice) **(project)**
A built-in speech engine — `espeak` or `vits_onnx` today, in `src/personacore/voice/engines/`. Core code, not a plugin: every engine is compiled into the one image and has its own independent on/off switch (ADR-0029, superseding ADR-0021's plan to run each in its own container). Off means off — no memory, no CPU — and saving the switch starts or stops it with no restart. A *remote* engine, reached over the network the way a plugin would be, is designed for but not built. See [Personas](Personas).

### Event bus
A shared message channel (MQTT) that lets the world *push* information to the assistant — a doorbell, a schedule firing, a camera — rather than the assistant having to ask. See [Event Bus](Event-Bus).

### Event envelope **(project)**
The versioned JSON wrapper every message on the bus carries: `envelope_version`, `event_id`, `source`, `type`, `timestamp`, `data`. `data` is deliberately untyped, because it is untrusted content and the core does not parse meaning out of it.

### Fence **(project)**
The wrapper the core puts around anything from outside — a tool result, a recalled memory, an event payload — before it reaches the model, so the model reads it as quoted data rather than as instructions. A unique per-turn token marks the boundary. A tool result that says "ignore your previous instructions" arrives inside the fence, labelled as content someone else wrote. Mitigation, not a guarantee.

### HTTP transport
A plugin that runs as its own container or service and is reached over the network. For heavy things (a vision model), things in another language, or things you deliberately want at arm's length. Its registration lives in `/appdata/plugins-http.d/`. See [Plugin HTTP Transport](Plugin-HTTP-Transport).

### Human-in-the-loop
Consequential actions require explicit confirmation before executing. Implemented as the `confirm` and `restricted` [risk levels](Risk-Levels).

### Least privilege
Every component gets only the access it needs. In practice: a plugin declares the secrets, hosts and paths it needs, gets exactly those, and cannot widen the list at runtime. Both lists default to empty, and **empty means none, not everything**.

### LLM role **(project)**
One of five named endpoint slots — `interactive`, `autonomy`, `triage`, `vision`, `commands` — routed by what a request is *for* rather than by address. Only `interactive` is required; every other role falls back to it, so one endpoint is a complete setup. [ADR-0011](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0011-llm-roles.md), and [LLM Roles](LLM-Roles).

### Lookup **(project)**
A settings field that can be filled by asking the plugin, instead of by typing a number you had to find elsewhere. The plugin's `config.schema.json` names one of its own tools; the admin form shows a search box; picking a result fills the fields. The lookup tool must be `safe` risk, and it is audited like any other tool call. [ADR-0016](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0016-config-field-lookups.md), and [Plugin Configuration](Plugin-Configuration).

### Manifest
The small file in each plugin folder — `manifest.toml` — declaring what the plugin is, what it needs, and what it is allowed to do. **The manifest declares; the core enforces.** Nothing a plugin does at runtime widens what its manifest asked for. See [Plugin Manifest](Plugin-Manifest).

### MCP (Model Context Protocol)
Open standard for plugging tools into an agent. It defines *what* plugins say, not *where* they run. A PersonaCore plugin is an MCP server plus a manifest — there is no PersonaCore library to import.

### Memory scope **(project)**
Which pool of memory a caller may read and write: none, its own, the household's, or the isolated anonymous pool. Carried on the [policy profile](Policy-Profiles). The memory subsystem itself is P1 and not built; the scope field is in the P0 data design because ownership is miserable to retrofit.

### Observability
Being able to see what the system did and why: structured logs, the trace view, health status.

### OIDC / OAuth2
Standards for login (authentication) and permissions (authorization). PersonaCore does not implement either: a reverse proxy performs the login and tells the core who arrived. See [Scenario: Putting Authelia in Front](Scenario-Putting-Authelia-In-Front).

### OpenAI-compatible API
The de facto standard HTTP interface for talking to LLM servers (`/v1/chat/completions`, `/v1/models`). The core is a **client** of one — your LLM host — and a **server** of one, for everything else in the house. See [OpenAI-Compatible API](OpenAI-Compatible-API).

### Persona
Standing instructions defining the assistant's personality and rules — the system prompt. Files in `/appdata/personas/<name>/`: a prompt file, plus an optional `persona.toml` for metadata. Hot-swappable with no restart. A persona describes *manner*; it does not describe capability, and writing "you have no tools" into one will make the model refuse to use tools it actually has. See [Personas](Personas).

### Plugin package **(project)**
A plugin distributed as a zip archive containing one plugin directory, uploaded to the admin API (`POST /admin/api/plugins/install`; the admin UI has no working install form yet). The zip is a transport, not a new format — the contract is still the folder and the manifest. Nothing in a package is ever executed during installation. [ADR-0013](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0013-plugin-packages-and-installation.md), and [Plugin Packaging](Plugin-Packaging).

### Policy profile **(project)**
What a caller is allowed to do: which persona answers, which tools are allowed (an allowlist — empty means none), the maximum risk level, whether they may approve confirmations, their memory scope, whether safe mode is on, and their rate limit. Attached to an API key or to the anonymous tier. See [Policy Profiles](Policy-Profiles).

### Profile **(project)**
Used two ways, and they are the same object. A *policy* profile is the permissions attached to a caller (above). A *household member's* profile is that person's identity plus their permissions (spec §8). Speaker identification, which would connect a voice to one, is P3.

### RAG (retrieval-augmented generation)
Looking up relevant stored information and handing it to the model before it answers. The retrieval plumbing inside the memory design. P1.

### Raw passthrough **(project)**
A per-key switch that hands a caller the underlying model directly, skipping the persona, the risk gate and the tool layer. It exists so the core can be the single LLM doorway for the whole house, and it is off unless an admin deliberately turns it on for one key.

### Repeating group **(project)**
A settings field that holds a list of similar named things — the weather plugin's locations, for example — rendered in the admin form as "add another entry" rather than as one text box holding a whole TOML table. Bounded on purpose: there are ceilings on the number of entries and the fields inside one. See [Plugin Configuration](Plugin-Configuration).

### Risk level
Per-tool, declared in the manifest, enforced by the core at call time: `safe` runs silently, `confirm` requires a human to say yes, `restricted` requires per-user permission *and then* confirmation. The rule is that anything irreversible is never `safe`. See [Risk Levels](Risk-Levels).

### Role
See **LLM role**. (The word is used for nothing else in this project.)

### Safe mode **(project)**
A child-safety toggle on a profile, default on for the anonymous tier. It composes a safety instruction ahead of the persona, clamps the tool ceiling further, and refuses to coexist with a non-empty anonymous memory scope. **Best-effort, and must not be described otherwise** — the transcript log is the control that actually works. [ADR-0005](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0005-child-safety-controls.md).

### SBOM
Software bill of materials — the list of libraries the system is built on. Generated per release by the CI pipeline.

### Secret store **(project)**
Secrets are files in `/appdata/secrets/<name>`, one per file. A plugin declares secret *names* in its manifest and is handed only those values, as environment variables, when it starts. A plugin never sees the whole store, and a secret value never appears in a manifest, a config file, a log, or the admin UI.

### Semantic versioning
`MAJOR.MINOR.PATCH` numbering where each digit has defined meaning.

### stdio transport
A plugin the core runs as a small subprocess inside its own container, talking over standard input and output. No port, no extra container, no network. The default for compact plugins; its folder lives in `/appdata/plugins/<name>/`. Note that on stdio, **stdout belongs to the protocol** — a plugin printing to it corrupts the conversation, so diagnostics go to stderr.

### STT / TTS
Speech-to-text / text-to-speech. TTS is built: the core's own switchable engines (see **Engine (voice)**) speak a persona's replies. STT, as a core capability, is not built — the Chat screen's microphone button uses the browser's own recogniser instead (see **Dictation**), which is a different thing and not this.

### Surface **(project)**
Which door a request came through: `voice`, `admin_ui`, `api`, and so on. It is recorded on every audit record and every transcript row, and retention can be set per surface — so, for example, the admin chat box and the exposed API can age out on different schedules. Also used for the two HTTP *surfaces* the one listener serves: `/v1` (the exposed API) and `/admin` (the admin API and the admin UI); `/health` reports which are mounted.

### Trace view
The historical and live record of what the agent did — tool calls with arguments and outcome, events received, confirmations. If you cannot see what it did, you cannot trust it or debug it. See [Audit and Trace](Audit-And-Trace).

### Transcript **(project)**
The message-level record of conversation: every message in and out, on every surface, with timestamp, surface and owner, aged out on a configurable window (default 30 days). This is the most privacy-sensitive data in appdata. [ADR-0004](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0004-conversation-audit-and-retention.md), and [Scenario: Retention and Privacy](Scenario-Retention-And-Privacy).

### Trusted header / trusted proxy **(project)**
One of the admin API's two doors, chosen with `[auth] method = "proxy"`: a reverse proxy performs the login and sets a header (`Remote-User` by default) naming the signed-in user. The other door is the core's own accounts, which is the default. Because anyone who can reach the port could send that header themselves, it is honoured **only** from an allowlisted peer address — loopback by default. From anywhere else it is stripped before any route sees it. See [Security Model](Security-Model).

### Untrusted content
Anything from outside: voice transcripts, chat bridges, camera-derived text, API responses, event payloads, tool arguments, uploaded files, header values. It is data, never instructions. See **fence**.

### Vector store
A database that finds text by meaning rather than keywords. Memory tier L2; chosen engine is sqlite-vec ([ADR-0001](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0001-spec-15-open-decisions.md) item 4). P1.

### Voice library **(project)**
The one list of every installed voice across every enabled engine, rendered as `GLaDOS (vits-onnx)` — picking an engine is not a step an operator takes. A voice belonging to a disabled engine is still listed, marked unable to speak, with the reason (PC-336). See [Personas](Personas), [Appdata Layout](Appdata-Layout).

### Voice pack **(project)**
The zip a voice arrives as, and the engine-native files it unpacks into: `/appdata/voices/<engine-id>/<voice-id>/`. Installed through the admin UI's Voice screen, like a plugin package — its contents are never repackaged, so a stock voice zipped exactly as its author published it is valid, and no wrapper file (`voice.toml` is optional metadata) is ever required. There is deliberately no universal voice-pack format: each engine loads its own files. No pickles, scripts or executables are accepted in one. [ADR-0029](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0029-core-owns-voice-engines-are-switches.md), superseding [ADR-0021](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0021-voice-engines-and-voice-packs.md)'s plan to ship an engine as a plugin in its own container.

### Wyoming protocol
Open standard, used by Home Assistant, for wiring together wake word, speech-to-text and text-to-speech components. P1.
