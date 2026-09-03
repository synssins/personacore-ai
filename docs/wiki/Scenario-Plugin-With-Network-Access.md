# Scenario: A Plugin That Talks to the Internet

Your plugin needs to call an outside service, and you want to know what you must declare, what the core actually enforces, and — honestly — what it does not.

## Declare the hosts

```toml
[permissions]
network = ["api.open-meteo.com", "geocoding-api.open-meteo.com"]
```

Real hostnames, one per entry, and only the ones you actually call. The list defaults to empty.

Every entry is a place your plugin could send the household's data, and someone reviewing your plugin has to justify each one. Do not add a host "just in case".

The bundled weather plugin is the reference: it declares exactly the two hosts it calls, and no more.

## The honest part: this is not enforced for stdio plugins

**`permissions.network` is a declaration, not a wall.** For a stdio plugin — a subprocess of the core, sharing the core's network stack — the core enforces nothing. Your plugin can `import httpx` and call anywhere it likes, and nothing will stop it.

This is recorded as [ADR-0012](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0012-plugin-network-and-entry-points.md) item 1, which is still **Proposed — needs sign-off**, so the field's future is genuinely open: the alternative under consideration is to drop it until it can be enforced. The ADR's own words are the reason it is documented rather than quietly left: *"A control that appears enforced and is not is worse than no control, because it is trusted."*

Why it is not enforced, in short:

| Approach | Why not, today |
|---|---|
| Network namespace or firewall per subprocess | Needs `NET_ADMIN` and per-process namespaces. The container runs unprivileged and CPU-only by constraint; this trades the whole stack's security posture for one plugin's egress. |
| Compose network segmentation | Works for **HTTP-transport** plugins, which have their own containers. Does nothing for stdio plugins. |
| Hand the plugin a pre-scoped HTTP client | Convenient and honest, but a *facility*, not enforcement — a plugin can always import a client itself. Not built. |
| Proxy all plugin egress through the core | Real enforcement, and a large piece of engineering that needs its own phase. Not built. |

So what does the declaration buy you? **Reviewability.** An admin deciding whether to trust a plugin folder from the internet can read "this plugin says it talks to `api.open-meteo.com`" before installing it. That is real value. It is just not a boundary, and nobody should plan around it as though it were.

For HTTP-transport plugins, Compose network segmentation is the enforcement path spec §7 already calls for and it genuinely works there — but **nothing in this repository ships a segmented network today**, because no HTTP plugin ships with the core. If you deploy one, that segmentation is yours to write. See [Plugin HTTP Transport](Plugin-HTTP-Transport).

## Therefore: police yourself

The reference plugin does, and its habits are the ones to copy.

**Turn redirects off.** A silently followed redirect walks your plugin off its own allowlist and makes the declaration a lie:

```python
httpx.AsyncClient(timeout=timeout, follow_redirects=False)
```

**Put a timeout on every outbound call, and make it a config setting.** No default is right for everyone's connection, and a plugin that hangs is a plugin that makes the assistant go silent.

**Do not echo what the service says.** The weather plugin maps a numeric weather code through its own table, so the only English that reaches the persona is English we wrote. Nothing else from the response is passed through. A remote service's text is untrusted content; passing it straight to the model is handing an outsider a line in your prompt.

**Failure is an outcome, not a crash.** Unreachable should produce structured data plus a sentence the persona can say — the weather plugin returns `available = false` and *"I can't reach the weather service right now."* Structured enough for the agent, speakable enough for the user, and a traceback in neither direction.

**Never build a URL out of something a person said.** Tool arguments came from someone talking, or from a chat bridge, or from text on a camera. Bound them, type-check them, and never concatenate them into a host or a path.

## The privacy question you should ask before adding a host

The weather plugin's config carries this reasoning, and it generalises: it does **not** resolve place names at runtime by default, because anything a person says near a microphone — a street name, an employer, a school — would otherwise reach a third party's request log.

But the same plugin *does* offer a place search **in the settings form**, and that is a different act: one deliberate search, by the person who owns the system, on a string they typed themselves. The two only look alike because both involve a place name ([ADR-0016](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0016-config-field-lookups.md)).

Ask which of those two your network call is. If it is the first, consider whether it should be a toggle the operator can switch off — and if it is, say in the config comment what turning it off actually protects.

Note also that adding a lookup usually means adding a host you did not previously need, and that shows up in your manifest where an admin reviews it. That is the point: the cost of the convenience is stated where it is decided.

## What is enforced

To be clear about the boundaries that *are* real, since one of them being soft does not make the others soft:

- **Secrets** — you get exactly the names you declared, matched byte for byte against the directory listing. See [Scenario: A Plugin With a Secret](Scenario-Plugin-With-A-Secret).
- **The environment** — built from an allowlist, never inherited. No `PERSONACORE_*`, no `PYTHONPATH`.
- **Paths** — `entry` and `permissions.paths` reject absolute paths, `..`, drive-relative values and symlinks pointing out of your folder. Your working directory is your own folder.
- **Tool risk** — comes from the manifest and only from the manifest, so a running server cannot promote its own tool to `safe`.
- **Tool reachability** — a tool the manifest does not declare is not callable; a tool no profile allowlists is not offered.

See [Security Model](Security-Model) for the full list and its limits.

## If enforcement lands later

ADR-0012 is explicit that real stdio egress enforcement would be a **contract minor version** when it arrives, and that plugins which quietly relied on unenforced access will break at that point — which is the intended outcome. Declaring truthfully now is not ceremony; it is what keeps your plugin working later.

## Related

- [Plugin Manifest](Plugin-Manifest) — `permissions` in full.
- [Plugin Walkthrough: Weather](Plugin-Walkthrough-Weather) — the reference plugin, read line by line.
- [Security Model](Security-Model).
