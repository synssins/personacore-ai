# Scenario: Building a Tool That Asks Before Acting

You are writing a tool that does something you would want to be asked about first — sending a message, starting an appliance, unlocking a door — and you want to know how to declare that, and exactly what the core will do.

**Read the "what actually happens today" section before you build against this.** The gate is built and enforced; the thing that asks the human is not.

## Pick the level

Declared per tool in `manifest.toml`, and the core enforces it at call time. A tool the manifest does not declare has no risk level and is therefore not callable at all — the core will not invent one, because guessing would mean guessing `safe`.

```toml
[tools.unlock_front_door]
risk = "restricted"
description = "Unlock the front door."
```

| Level | What the core does | For |
|---|---|---|
| `safe` | Runs silently | Reads something, changes nothing. A lookup, a status check, a forecast. |
| `confirm` | A human must say yes first | Anything that changes the world in a way you would want to be asked about. |
| `restricted` | The caller must be permitted to invoke it **and then** confirm | Anything you would not let a guest, a child, or a voice on a television do. |

**The rule: anything irreversible is never `safe`.** If you cannot undo it, cannot un-say it, it costs money, or it opens something — `confirm` at the very least. Spec §7 names door locks, outbound calls, purchases and anything irreversible explicitly.

**Rate the tool, not your intentions.** "It's safe because I validate the argument" is not what these levels mean. The template's test is a good one: what happens the day a television advert says your tool's name out loud in an empty kitchen?

Risk comes from the manifest and **only** from the manifest. A running MCP server cannot promote its own tool to `safe` at runtime — the risk level is deliberately not part of what the server reports about itself.

## What the gate actually does

Every tool call the model asks for goes through the same ordered check. Each step is a refusal on failure, and **no step defaults to allow**:

1. **Is there a tool by that name at all?** A model can hallucinate a tool name, and "unknown" must never mean "harmless".
2. **Is it in this caller's `allowed_tools`?** An allowlist, so a newly installed plugin is unreachable until someone decides otherwise.
3. **Is its risk within the caller's ceiling?** Safe mode clamps that ceiling further. An unrankable risk is refused.
4. **`safe` stops here and runs.** Everything above `safe` continues.
5. **May this caller approve things at all?** (`may_approve_confirm` on the profile.) If not, refused — and audited as a confirmation event with the reason.
6. **Ask the human.** Granted runs it; denied refuses; **no channel refuses**.

For `restricted`, steps 2 and 3 *are* the "is this user permitted?" branch: spec §8 defines a user's permissions as which restricted tools they may invoke — the allowlist plus the ceiling — and which actions they may approve.

Every refusal comes back as a sentence the assistant can say, not an error:

- *"I don't have a tool called `X`, so I can't do that."*
- *"I'm not allowed to use `X` for you."*
- *"`X` needs more permission than you have, so I can't run it."*
- *"`X` needs someone to approve it, and you're not set up to approve things."*
- *"All right, I won't."* (denied)
- *"`X` needs to be confirmed first, and I've no way to ask you here, so I've left it alone."*

Every confirmation — granted or refused — is written to the audit log with the tool, the arguments and the decision, and appears in the trace view. See [Audit and Trace](Audit-And-Trace).

## What actually happens today

**No confirmation channel is wired into the running product.** The agent loop accepts one, the interface for it exists, and the tests drive it — but the production assembly constructs the loop without one, because the surfaces that would ask (voice, and the designed admin UI) are not built.

The consequence, stated plainly: **any `confirm`- or `restricted`-risk tool call is refused today**, on the "no channel" branch, with the last message in the list above. It is refused *safely* — fail-closed is the correct behaviour when there is nobody to ask — but it does not work yet, and a page that said otherwise would be worse than no page.

The P0 phase gate demonstrated all three outcomes (granted, denied, no channel) at the component boundary rather than by asking a model, and the trace showed `tool_call … refused risk: confirm`. That is the honest state: the gate is real and tested; the human on the other end of it is a P1 surface.

The admin chat box will not exercise this either — it is granted exactly the installed **`safe`** tools and a ceiling of `safe`, so a `confirm` tool is refused at step 2, not step 6.

## So build it anyway, and build it right

Nothing above changes what you should write. Declare the true risk level now:

- When the confirmation channel lands, your tool starts asking — no change to your plugin.
- If you declare `safe` today to "make it work", your tool will run silently forever, and the day someone notices is the day it has already happened.

## Getting a restricted tool to a caller

Two switches, on the [policy profile](Policy-Profiles) attached to an API key or to a household member:

- `allowed_tools` must contain the qualified name (`door.unlock_front_door`), and
- `max_tool_risk` must be at least that level, and
- `may_approve_confirm` must be true for anything above `safe`.

The **anonymous tier cannot hold any of this**. The profile model *refuses at construction* an anonymous profile that reaches beyond `safe` tools or that can approve a confirmation — it is not a setting that can be got wrong ([ADR-0003](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0003-anonymous-access-tier.md)). The same applies to keyless callers once that lands, since a keyless request resolves to the anonymous profile ([ADR-0018](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0018-optional-api-keys.md)).

## One more thing the gate does not do

Config-form **lookups** — a settings field filled by asking the plugin — are restricted to `safe`-risk tools, and the core refuses to wire anything else. A settings page must never be a route to a `confirm`-level action ([ADR-0016](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0016-config-field-lookups.md)).

## Related

- [Risk Levels](Risk-Levels) — the reference page.
- [Policy Profiles](Policy-Profiles) — the switches on the other side.
- [Plugin Manifest](Plugin-Manifest) — declaring tools.
- [Audit and Trace](Audit-And-Trace) — where a confirmation decision is recorded.
