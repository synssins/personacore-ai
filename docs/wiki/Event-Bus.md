# Event Bus

Topics, the versioned envelope, the ignore/log/wake rules, and what happens when no broker is reachable. Read this if you are publishing events into PersonaCore or wondering why the assistant did not react to one.

MCP tool calls are the **pull** channel — the assistant reaching out. The bus is the **push** channel: the world telling the assistant something happened. Plugins publish; the core listens. No plugin ever addresses the core directly, which is what lets a new publisher appear without the core knowing anything about it.

Source: `src/personacore/bus/client.py`, `src/personacore/bus/rules.py`, `src/personacore/contracts/events.py`, spec §5.2.

## Transport and configuration

MQTT. The `[bus]` section of `core.toml` sets host (default `mosquitto`), port (default `1883`), username, `password`, `password_secret` and `client_id` (default `personacore`). See [Core Settings](Core-Settings).

**`password` is the ordinary way to authenticate**, typed into a write-only box on the Core settings screen and stored in `[bus]`. It is redacted out of every read of the configuration, and a save carrying the redaction marker leaves the stored value alone. `password_secret` still works and is described below; `password` wins when both are set.

Whatever the source, `/health` and the Health dashboard report only *whether* a password is in use, alongside the host, port, client id and user name the **running** bus holds — see [Health and Diagnostics](Health-And-Diagnostics).

**`password_secret` is resolved at assembly time.** It is read from `<appdata>/secrets/` and handed to the bus as a `SecretStr`, unwrapped only at the moment of connection. If the named secret cannot be read, the core does not stop — the bus is a degradable dependency (spec §10), so it connects unauthenticated instead, and the failure is reported on `/health` as `bus_password_degraded`. `username` is passed either way.

## Topics

All events live under one root:

```
personacore/events/<source>/<type>
```

- `source` — who published it, usually a plugin name.
- `type` — what happened, in the publisher's own vocabulary: `person-detected`, `button-pressed`.

Each segment must match `^[a-z0-9][a-z0-9._-]{0,63}$`: lowercase letters, digits, dots, hyphens or underscores, starting with a letter or digit. Slashes, `+` and `#` are **rejected rather than escaped** — an MQTT wildcard inside a segment would let one publisher subscribe to or spoof another's traffic.

The core holds exactly **one** subscription, `personacore/events/#`, and decides per rule what each message deserves. The topic convention is what makes a single subscription possible.

## The envelope

Every message is a JSON object with a versioned wrapper, so a subscriber written today still parses a message published by a plugin written in three years.

| Field | Type | Default | Notes |
|---|---|---|---|
| `envelope_version` | int | `1` | Bumped only for a **breaking** envelope change. Additive fields do not bump it. |
| `event_id` | UUID | generated | |
| `source` | string | required | Validated as a topic segment. |
| `type` | string | required | Validated as a topic segment. |
| `timestamp` | datetime | required | **Must be timezone-aware.** When it happened, not when it was received. |
| `data` | object | `{}` | Opaque to the core. |

Unknown top-level fields are rejected.

Two of those are worth the extra sentence:

**`timestamp` must carry a timezone.** A naive timestamp from a plugin in another container is ambiguous, and the audit log is only useful if its ordering is real. This mirrors the same requirement on every stored audit and transcript record.

**`data` is deliberately untyped.** Spec §7 is explicit that anything arriving from outside is untrusted input and is data, never instructions — so the core treats the payload as opaque and hands it to the agent quoted, rather than parsing meaning out of it here. When an event payload does reach the model it goes through the same fence tool results and memories use. See [Security Model](Security-Model).

## Event rules: ignore, log, wake

Most of what arrives does not deserve the assistant's attention. A motion sensor that fires two hundred times a day must not wake the agent two hundred times, and deciding that in the core — rather than in each publisher — is what keeps publishers dumb and the policy in one reviewable place.

| Action | Effect |
|---|---|
| `ignore` | Dropped without a trace. For genuinely high-volume noise. |
| `log` | Recorded and visible in the trace view; the agent never sees it. |
| `wake` | Handed to the agent, which decides whether to act or announce. |

A rule is a glob `match` against `"<source>/<type>"` — `doorbell/*`, `*/person-detected` — plus an `action` and an optional `note`. The note exists because a rule nobody remembers the reason for is one nobody dares delete, and the admin UI shows it beside the rule.

**First match wins, so order is meaningful.**

### The default is `log`, and that is a decision

An unmatched event is logged.

- `ignore` as the default would make an unrecognised event vanish silently, which is how a camera stops working for a fortnight before anyone notices.
- `wake` as the default would let any publisher interrupt the household by inventing a new event type.

Logging keeps it visible and harmless.

### Rules are configuration, not code

The core never learns the vocabulary of any particular plugin (spec §13.5 — if the core contains the string "spotify", something has gone wrong).

**Not yet wired to config.** `BusSettings` has no rules field, and the application assembly constructs the bus with an empty rule list, so today every event takes the default action: `log`. There is also no `on_wake` handler wired in, so `wake` currently has no effect even if a rule produced it. The rule engine and its tests exist; the configuration surface and the agent hand-off do not.

## Degradation when the broker is away

The bus is a degradable dependency. With no broker reachable the assistant loses its push channel and **keeps doing everything else**, reconnecting in the background (spec §10).

Concretely:

- `start()` returns immediately and connects on a background task, so a missing broker never blocks the core coming up.
- The connect loop catches everything (except cancellation), records the reason, logs a disconnect and retries after 5 seconds. An unexpected client error must not escape into the core's main loop.
- `publish()` returns `False` rather than raising when the broker is away. A caller publishing an event should not have to defend against the bus being down, and the health record already carries the reason.
- A handler that throws does not kill the subscription loop and lose every subsequent event.

## Malformed messages are dropped and counted

Every parse failure is a drop-and-count, never a raise. A publisher with a bug — or something hostile putting rubbish on the topic — must not be able to stop the core receiving everything else.

Dropped when: the payload is not valid UTF-8, is not text at all, is not valid JSON, is not a JSON object, or does not validate as an envelope. Each increments a counter and logs a warning carrying **the topic only** — the payload is not logged, because it is untrusted content.

The same rule applies to accepted events: the log line carries source, type, event id and the action taken, and never `data`.

## What the health dashboard shows

The `event_bus` component reports `ok` when connected and `failing` when not, with the last error in the detail. Its `facts` carry the counters:

| Fact | Meaning |
|---|---|
| `connected` | Currently connected to the broker. |
| `last_error` | Type and message of the most recent failure, or null. |
| `received` | Messages seen on the subscription. |
| `malformed` | Of those, how many were dropped as unparsable. |
| `published` | Messages successfully sent. |
| `reconnects` | How many times the connect loop has gone round again. |

A rising `malformed` count against a steady `received` is a publisher writing the wrong shape. See [Health and Diagnostics](Health-And-Diagnostics).
