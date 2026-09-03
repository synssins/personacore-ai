# PersonaCore

A self-hosted, containerised, security-first agentic assistant for a multi-person
home. It converses in a configurable persona with a configurable voice, controls
the home, remembers things on request, and grows smarter through scheduled
reflection.

The **core** is deliberately small: persona, voice, conversation, and baseline
abilities. **Everything else is a plugin** — and adding a capability must never
require modifying the core.

Every container in this project is **CPU-only**. Anything genuinely needing a GPU
lives elsewhere and is reached over the network.

- Documentation: [`docs/wiki/`](docs/wiki/Home.md), starting with the
  [overview](docs/wiki/Overview.md).
- Writing a plugin: [`docs/plugin-author-guide.md`](docs/plugin-author-guide.md).
- Voices: [`docs/voice-pack-format.md`](docs/voice-pack-format.md) and
  [`docs/pronunciation-guide.md`](docs/pronunciation-guide.md).

## Status

**P0 complete and published.** The container runs, holds a conversation, calls
tools from installed plugins, and is deployable from
`ghcr.io/synssins/personacore-ai`. P1 — voice, memory, mood — has not started.

| Piece | State |
|---|---|
| Contracts — plugin manifest, event envelope, access policy | done |
| LLM client — OpenAI-compatible, streaming, circuit breaker | done |
| Audit log, transcript store, structured logging | done |
| Plugin discovery — manifest loading, path validation | done |
| Appdata layout, secret store, core settings | done |
| Agent loop — persona, tool gate, untrusted-content fencing | done |
| MCP plugin host — process supervision, both transports | done |
| Exposed OpenAI-compatible API, admin API | done |
| Admin UI — chat, health, models, plugins, core settings, logs | done |
| Admin UI — access keys and personas screens | not started (API-only) |
| Plugin install from a zip, per-plugin settings form | done |
| Dockerfile, Compose, CI, GHCR release | done |
| Voice, memory, mood (P1) | not started |

### Known gaps

Built and tested, but **not connected to the running application**. Documenting
the system surfaced these; each is tracked and none is a silent surprise:

| Gap | Effect |
|---|---|
| `confirm` / `restricted` tool confirmations | The risk gate works and fails closed, but no channel exists to ask a human, so these tools are always refused |
| Event rules, `on_wake` | The rule engine is unwired; every event takes the default `log` action |
| `permissions.network`, `permissions.paths` | Declared and validated, enforced by nothing (ADR-0012) |
| `events.publishes` / `subscribes` | Declared; no events are delivered to plugins |
| Compose network segmentation | Promised by spec §7 and ADR-0012; `compose.yaml` defines no networks |
| Access keys and Personas screens | The sidebar links to `/admin/keys` and `/admin/personas`; both 404. Issuing or revoking an API key, and choosing the default persona, are API-only |
| Plugin install form | The form on `/admin/plugins` is rendered disabled; uploading a package is API-only (`POST /admin/api/plugins/install`) |

The test suite passes with all of these present, because it exercises each
component directly and did not assert that the application wires them.
Assembly-level tests are being added alongside the fixes.

## Developing

Requires Python 3.12 or newer.

```bash
python -m pip install -e ".[dev]"
```

Lint:

```bash
python -m ruff check src
```

## Licence

Code is MIT — see [`LICENSE`](LICENSE). The GLaDOS voice assets carried over from
the predecessor project are CC BY 4.0 and carry their own licence inside the
voice pack.
