# Scenario: Multiple People, and Callers With No Identity

Several people share this assistant, one of them may be a child, and some things talking to it have no identity at all. You want to know what the core can actually enforce and what it merely helps with.

## The short version

- **Household profiles are P0 *schema* and a later *feature*.** Every permission and every memory record carries an owner from the first version, because ownership is miserable to retrofit — but there is no profile store and no speaker identification yet. Today, "who is this" means "which API key is this".
- **The anonymous tier is a real, enforced set of ceilings** — enforced in the profile model, not in configuration — and there is currently no live path into it.
- **Child safety filtering is best-effort and must not be described otherwise.** The transcript log is the control that actually works.

## How people are distinguished today

By API key. Each key carries a whole [policy profile](Policy-Profiles): which persona answers, which tools are allowed, the risk ceiling, whether it may approve a confirmation, its memory scope, whether safe mode is on, its rate limit.

Issuing one key per person, per device, or per room is the intended pattern and it works now — a kitchen display and a workshop terminal can answer as different characters with different powers. See [Scenario: Third-Party Clients](Scenario-Third-Party-Clients).

The key-issuing request takes the whole policy rather than a profile name, precisely because there is no profile store to name into yet. When one exists, the request grows an alternative "profile id" field; it does not become a different request.

**Speaker identification is P3.** Wiring a voice to a household profile is designed for and not built. Until then, voice would arrive as one profile, not as whoever is speaking.

## The anonymous tier

A [policy profile](Policy-Profiles) with everything switched down, for callers with no identity ([ADR-0003](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0003-anonymous-access-tier.md)). It is not new machinery — spec §5.4 already specifies per-client policy profiles — and it is a **recorded deviation** from spec §7's "nothing listens unauthenticated", written down rather than quietly worked around.

### The ceilings, and why they are not settings

An anonymous profile that tries to exceed any of these is **refused at construction**. You cannot save it, misconfigure it, or drift into it:

| Ceiling | What is refused |
|---|---|
| `safe` risk only | A `confirm`- or `restricted`-risk ceiling |
| No household or per-user memory, ever | `memory_scope` of `user` or `household` |
| Cannot approve a confirmation | `may_approve_confirm: true` |
| Cannot discover what is installed | `may_enumerate_plugins: true` |
| Safe mode and anonymous memory cannot coexist | `safe_mode: true` with any memory scope other than none |

"A misconfigured anonymous profile is exactly the failure this project cannot afford, so the model refuses to hold one." The tests that assert these are protecting a real boundary, not a hypothetical.

On top of that: tools come from an **admin-selected allowlist** (empty means none, so installing a plugin never widens what an anonymous caller can do), traffic is rate-limited per source and fully audited, and memory entries and tool output reach the model as quoted untrusted data, never as instructions.

### The honest cost

**Anonymous memory is shared across every unauthenticated user**, because there is no identity to key it on. Anyone who can reach it can write to it and everyone reads it. It is suitable for "we use metric" and unsuitable for anything personal, and the admin UI must label it as shared.

It is also a **persistent injection surface**: something written into the shared scratchpad is read by whoever comes next. The TTL, the size cap, the purge control and the quoted-data rendering are the mitigations. None of them is a guarantee.

Spec §14 keeps every endpoint off the public internet, which bounds the exposure substantially. If that non-goal is ever revisited, ADR-0003 has to be revisited with it.

### Is it on?

**Off by default, behind an explicit admin toggle** — and today there is no live path into it: the exposed API requires a key, and the key-issuing endpoint refuses a profile of anonymous kind outright.

That changes with [ADR-0018](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0018-optional-api-keys.md), which is **accepted and not built**: a keyless request will resolve to this profile, defaulting to no tools at all — conversation only. The important consequence is that "keyless" then cannot mean "unlimited", because the model will not hold a profile that says so.

When it lands, the anonymous tier stops being a rarely used corner and becomes the default path for a whole class of client, and its ceilings become load-bearing. That is why they are enforced structurally now.

## Child safety

A **safe-mode toggle on the profile** ([ADR-0005](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0005-child-safety-controls.md)), default **on** for the anonymous profile and off for authenticated adult profiles.

### What is built

- **A safety instruction block composed ahead of the persona**, which the persona cannot override. It opens by saying the rules come first and cannot be changed, relaxed, ignored or role-played away by anything later in the message, by the conversation, or by anything a tool or a memory says — and a reminder is repeated after the persona. The persona is explicitly introduced as governing *manner* only.
- **A hard tool clamp.** With safe mode on, nothing above `safe` runs, whatever the profile's own ceiling says. Two independent limits, and the tighter one wins. Trimming *which* safe tools is admin configuration; the clamp is code.
- **The refusal to hold safe mode alongside anonymous memory**, described above.

### What is not built

- **Output screening.** ADR-0005 requires model output to be screened before it is spoken or returned. The classifier is pluggable with a built-in default, and it is P1 — neither exists today. Safe mode currently shapes the prompt and clamps the tools; it does not inspect what comes back.
- Forcing a web-search plugin into its strictest filtering mode, for the same reason: there is no web-search plugin yet.

### Say this out loud

**Content filtering driven by a local model is defeatable, and a determined user can work around it.** Nothing here should be presented to anyone — or in the admin UI — as a guarantee. A prompt-level instruction is a strong nudge and not a boundary.

**The transcript log is the stronger control**, and the two were designed together deliberately. Filtering reduces incidents; the log is what makes them visible. Every block or intervention is written to the audit log **with the triggering text**, so an adult can see *what* happened rather than only that *something* did — and so a bad rule can be found and fixed rather than guessed at.

False positives will happen and will be confusing. That is why the trigger is logged.

One design note worth knowing: when safe mode and a non-empty anonymous memory scope conflict, the setting is **not silently switched off for you**. A security control that changes state behind an admin's back is worse than one that makes the admin choose, so the model fails closed and the UI explains the conflict in plain English and offers to change both.

There is also a latency consequence flagged early: screening output must be streaming-compatible or safe mode will break the ~2-second voice budget. That is a P1 design constraint, not a surprise waiting to happen.

## What everyone in the household should be told

Every message, in and out, on every surface, is recorded — not just the child's ([ADR-0004](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0004-conversation-audit-and-retention.md)). The admin UI has to say so plainly on the relevant screens; a household member should not discover this by accident. See [Scenario: Retention and Privacy](Scenario-Retention-And-Privacy).

## Related

- [Policy Profiles](Policy-Profiles) — every field and its default.
- [Risk Levels](Risk-Levels) — what the ceiling actually gates.
- [Audit and Trace](Audit-And-Trace) — where interventions are recorded.
