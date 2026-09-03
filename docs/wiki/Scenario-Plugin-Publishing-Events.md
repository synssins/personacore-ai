# Scenario: A Plugin That Pushes Events

Your plugin knows something happened — a doorbell rang, a schedule fired, a sensor changed — and you want to tell the assistant rather than wait to be asked.

**Read "what is wired today" before building against this.** The envelope, the topic convention and the rule engine are built. The last hop, from a rule to the agent, is not.

## Pull versus push

MCP tool calls are the **pull** channel: the model asks, your plugin answers. The event bus is the **push** channel: the world speaks first.

The core holds one subscription — `personacore/events/#` — and decides per rule what each message deserves. **Plugins publish; the core listens.** No plugin ever addresses the core directly, which is exactly what lets a new publisher appear without the core knowing anything about it. Spec §13.5: if the core contains the string "spotify", something has gone wrong.

## Declare it

```toml
[events]
publishes  = ["doorbell/pressed"]
subscribes = []
```

Both default to empty. Like `permissions.network`, `publishes` is a **declaration for review** — it tells an admin reading your manifest what your plugin intends to put on the bus. Nothing enforces it.

`subscribes` is part of the contract and is **not delivered today**: the core does not forward bus messages back out to plugins. Declaring it is harmless and future-facing; relying on it will not work.

## The topic

```
personacore/events/<source>/<type>
```

- `<source>` — who published it, usually your plugin name.
- `<type>` — what happened, in your own vocabulary: `person-detected`, `pressed`, `window-open`.

Each segment must be lowercase letters, digits, dots, hyphens or underscores, start with a letter or digit, and be at most 64 characters. Slashes, `+` and `#` are **rejected rather than escaped** — an MQTT wildcard inside a segment would let one publisher subscribe to or spoof another's traffic.

## The envelope

Every message carries a versioned JSON wrapper, so a subscriber written today still parses a message published by a plugin written in three years:

```json
{
  "envelope_version": 1,
  "event_id": "b6f1…",
  "source": "doorbell",
  "type": "pressed",
  "timestamp": "2026-08-24T14:05:11.204000+00:00",
  "data": {"camera": "front"}
}
```

Two rules that will reject your message if you get them wrong:

- **`timestamp` must be timezone-aware.** A naive timestamp from a plugin in another container is ambiguous, and an audit log is only useful if its ordering is real. Emit UTC with an offset.
- **`data` is an object, and it is opaque.** The core does not parse meaning out of it, because it is untrusted content — data, never instructions (spec §7). It is handed to the agent quoted and fenced, if it gets there at all.

`envelope_version` is bumped only for a breaking change. Additive fields do not bump it.

## Publishing

Your plugin connects to the broker itself, as an ordinary MQTT client, and publishes the envelope's JSON to the topic. There is no PersonaCore library to import for this — the envelope is a documented JSON shape, not an API.

**The broker's address is not handed to you.** A stdio plugin's environment is built from a short allowlist and does not include any `PERSONACORE_*` variable, so the broker host and port are yours to take as your own settings, in your own `config.toml`, like any other configuration. On the shipped stack that is the `mosquitto` service on port 1883, reachable from inside the Compose network only.

Inside the core, publishing returns **false rather than raising** when the broker is away, and the same posture is right for you: a plugin publishing an event should not have to defend against the bus being down.

## What the core does when it arrives

Three outcomes, decided by ordered rules, **first match wins** (spec §5.2):

| Action | Meaning |
|---|---|
| `ignore` | Dropped without a trace. For genuinely high-volume noise. |
| `log` | Recorded and visible in the trace view; the agent never sees it. |
| `wake` | Handed to the agent, which decides whether to act or announce. |

Rules match a glob against `"<source>/<type>"` — `doorbell/*`, `*/person-detected` — and each carries a note saying why it exists, because a rule nobody remembers the reason for is one nobody dares delete.

**The default is `log`, deliberately.** `ignore` would make an unrecognised event vanish silently, which is how a camera stops working for a fortnight before anyone notices. `wake` would let any publisher interrupt the household by inventing a new event type.

Deciding this in the core rather than in each publisher is what keeps publishers dumb and the policy in one reviewable place. A motion sensor that fires two hundred times a day must not wake the agent two hundred times.

## What is wired today

- **Rules are not loaded from configuration.** `core.toml` has no rules section, so the running core uses the default rule set — which is empty, with a fallback of `log`. Everything that arrives is parsed, counted and logged.
- **`wake` does not reach the agent.** The bus accepts a wake handler; the production assembly does not pass one. Even a rule that said `wake` would stop at the log line.
- **`subscribes` is not delivered**, as above.

So today the bus is a working, observable **ingest and logging** path, and the last hop to the agent is a seam waiting for the surfaces that use it. Publish now if it is the right shape for your plugin — the envelope and topic contract will not change under you — but do not expect the assistant to react.

## What the bus will not do to you

- **It never trusts a payload.** Anything on the bus came from outside. It is parsed into the envelope and handed on as data; a malformed message is dropped and counted, never allowed to raise into the core's main loop. A publisher with a bug, or something hostile putting rubbish on the topic, cannot stop the core receiving everything else.
- **It never logs `data`.** The log line carries source, type, event id and the action taken — and not the payload, which could carry anything a camera or a chat bridge put in it.
- **It never takes the core down.** With the broker unreachable the assistant loses its push channel and keeps doing everything else, reconnecting in the background (spec §10). The health dashboard shows connected state, counts received, malformed, published and reconnects.

## Setting up the broker

The shipped Compose file runs `eclipse-mosquitto:2`, deliberately **not published** outside the Compose network — an MQTT broker on a LAN with no authentication is an open door. It needs a config file that this repository does not ship; see step 3 of [Installation and Upgrades](Installation-And-Upgrades).

The core can authenticate to the broker with a username and a password read by **name** from the secret store. Never put a broker password in `core.toml`.

## Related

- [Event Bus](Event-Bus) — the reference page: settings, topics, envelope fields, health counters.
- [Plugin Events](Plugin-Events) — declaring events in the manifest.
- [Health and Diagnostics](Health-And-Diagnostics) — reading the bus counters.
