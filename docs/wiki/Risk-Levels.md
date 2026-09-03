# Risk Levels

What `safe`, `confirm` and `restricted` mean, the exact order the gate checks things in, which boundaries check at all, and why every branch fails closed. Read this if you are writing a plugin manifest or deciding what a profile may reach.

Risk is **declared in the manifest and enforced by the core** (spec §5.1). A plugin cannot promote its own tool at runtime: the risk level comes from the manifest and only from the manifest, even though the tool's schema and description come from the running server, where they are accurate.

Sources: `src/personacore/contracts/manifest.py`, `src/personacore/agent/loop.py`, `src/personacore/plugins/host.py`.

## The three levels

| Level | Meaning |
|---|---|
| `safe` | Runs silently. |
| `confirm` | Requires spoken or UI confirmation before it runs. |
| `restricted` | Requires per-user permission, and then confirmation. |

They are totally ordered — `safe` (0) < `confirm` (1) < `restricted` (2) — so "within the ceiling" is one comparison.

Spec §7 is directive about what belongs where: door locks, outbound calls, purchases, and anything irreversible default to `confirm` or `restricted`, **never** `safe`.

## The gate, in order

`AgentLoop.gate_tool_call` is the authoritative gate — it is the one that knows about the human. It is public precisely because it is the security boundary of that component and deserves to be tested branch by branch rather than only through a conversation.

The order is the flowchart's order (spec §3.2), and no step defaults to allow.

**1. Is there a tool by that name at all?**
The catalogue is what the plugin host actually reported this turn. An unknown name is refused: a model can hallucinate a tool name, and "unknown" must never mean "harmless".
→ *"I don't have a tool called X, so I can't do that."*

**2. Is it in the caller's `allowed_tools`?**
An allowlist, so a newly installed plugin is unreachable until someone decides otherwise. Empty means none.
→ *"I'm not allowed to use X for you."*

**3. Is its declared risk within the caller's ceiling?**
The effective ceiling is `max_tool_risk`, **clamped to `safe` when `safe_mode` is on**. Two independent limits; the tighter wins.
A risk or a ceiling that cannot be ranked — a value from a newer contract version, a corrupted registration — is refused rather than treated as `safe`.
→ *"I can't tell how risky X is, so I'm not going to run it."* or *"X needs more permission than you have, so I can't run it."*

**4a. `safe` stops here.** Allowed.

**4b. `confirm` and `restricted` both end at a human.** First: does the profile have `may_approve_confirm`? Passing the allowlist and the ceiling *is* the "is this user permitted?" branch for `restricted` — spec §8 defines a user's permissions as which restricted tools they may invoke and which actions they may approve, and those are the two switches the profile actually carries.
→ *"X needs someone to approve it, and you're not set up to approve things."* Audited as a confirmation refusal.

**4c. Then: did a human actually say yes?** The question goes to whatever confirmation channel the caller supplied.

| Outcome | Result |
|---|---|
| `granted` | Allowed. |
| `denied` | Refused — *"All right, I won't."* |
| `unavailable` | Refused — *"X needs to be confirmed first, and I've no way to ask you here, so I've left it alone."* |

**No confirmation provider means `unavailable`, and `unavailable` is a refusal — never an assumed yes.** An exception thrown by the channel is the same thing: nobody said yes. In P0 no confirmation channel is wired in, so in practice every `confirm` and `restricted` tool is refused at this step.

Every step 4 outcome — granted or refused — is written to the audit store as a `confirmation` record with the decision and the arguments.

## Two gates, on purpose

The plugin host checks **again**, immediately before a plugin actually runs. Its three refusals, in order:

1. The plugin must exist and the name must have a tool half — otherwise *"I don't have a tool called X, so I couldn't do that."*
2. The tool must be **declared in the manifest**. A tool the manifest does not declare has no risk level, so there is nothing to enforce and it is not callable — even if the running server offers it.
3. Its declared risk must be within the **ceiling passed on this call**, defaulting to `safe` when the caller does not pass one.

Then the plugin must actually be running; a stopped, crashed or backing-off plugin raises a transport error that becomes *"I can't reach the X plugin right now, so I couldn't do that."*

The second gate is not redundant. The agent loop is not the only caller of the host: the exposed API, the event-bus rules, the admin settings-page lookup and anything added later all arrive here. **A ceiling that only exists in the caller is not a boundary.** The host's default ceiling is `safe` — the fail-closed value — because the alternative is a caller that forgets and silently gets everything.

The agent loop passes the profile's `max_tool_risk` as that call's ceiling. Note that this is the profile's raw ceiling, not the safe-mode-clamped one; the loop's own gate has already refused anything above `safe` for a safe-mode profile, so the clamp is enforced — just at the first gate rather than repeated at the second.

The settings-page lookup (ADR-0016) passes an explicit `safe` ceiling, so a config search can never reach a `confirm` tool however the plugin's schema is written.

## Every branch fails closed

There is no default-allow anywhere in the gate. Restated as a list, because it is the property that matters:

- Unknown tool → refused.
- Not in the allowlist → refused.
- Unrankable risk, or unrankable ceiling → refused.
- Risk above the ceiling → refused.
- Profile may not approve → refused.
- No confirmation channel → refused.
- Confirmation channel threw → refused.
- No profile at all, or a disabled profile → the whole turn is refused before a tool is ever considered.
- Tool not in the manifest → refused at the host.
- Plugin not running → refused at the host.

## What the user and the model each see

A refusal produces a `refusal` event carrying the plain-English reason, **and** the reason is fed back to the model as a tool message prefixed `REFUSED BY POLICY:` with an instruction to tell the user briefly, in its own voice, and not to retry the tool this turn. So the refusal reaches the user as the persona would say it, rather than as a system string.

That text is core-authored and is deliberately **not** fenced as untrusted content — fencing it would tell the model to ignore it. Tool *results*, by contrast, always are. See [Security Model](Security-Model).

A refused call never reaches the plugin. It is recorded as a `tool_call` audit entry with outcome `refused`, the reason and the arguments.

## Presentation versus enforcement

The tool list offered to the model is filtered up front by the same allowlist and ceiling, so a caller is not shown tools it cannot use — offering one only invites a refusal the user has to sit through.

That filtering is **presentation, not enforcement**. Every call is gated again at invocation time, and then again at the host. If tool listing fails entirely, the turn proceeds with no tools rather than failing.

## Containment

`call_tool` has no failure path that raises. Crashes, hangs, malformed replies and dead plugins all come back as a failed result carrying a sentence a persona can say out loud, because an exception escaping into the agent loop would end a turn in silence — the one outcome spec §5.1 forbids.

Quoted plugin error text is capped at 200 characters and stripped of control characters: it is untrusted content, and it is also spoken aloud.

A turn is bounded at 6 rounds of tool calls by default. Hitting the cap ends the turn with *"I kept trying to look things up and never got to an answer, so I've stopped."* rather than spinning.
