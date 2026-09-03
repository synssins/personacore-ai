# Scenario: What Is Recorded, For How Long, and Who Can See It

You are responsible for a system that listens to a household. You need to be able to answer, accurately, what it keeps.

## The obligation first

**Everyone in the household is recorded, not just the child.** Every message, in and out, on every surface, is stored as a transcript ([ADR-0004](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0004-conversation-audit-and-retention.md)).

The ADR is explicit that the admin UI must say this plainly on the relevant screens, and that **a household member should not discover this by accident.** That obligation is not satisfied by this page. Tell the people who use it, in words, before they use it.

The reason the logging exists is monitorability — a child in the household may use the assistant, and usage needs to be reviewable. The reason it must be *disclosed* is that it captures everyone else too.

## Two stores, deliberately separate

| Store | Holds | Why separate |
|---|---|---|
| **Audit log** | Tool calls with arguments and outcome, events that woke the agent, confirmations given or refused, admin changes, access attempts | Spec §7. Actions, not words. |
| **Transcript store** | Every message in and out, with timestamp, surface and owner | ADR-0004. Words. |

Both live in one SQLite database under `<appdata>/audit/`.

**Message content is deliberately kept out of the audit log.** The agent loop writes the transcript records; duplicating content into the audit detail would put the most privacy-sensitive data in two stores with one retention policy between them. So an API request is audited as *"a request happened, from this profile, on this surface, with this outcome"* — and what was said lives in exactly one place.

## What is recorded, field by field

Every record in both families carries:

- **A timestamp**, timezone-aware. A naive one is refused, because a log is only useful if its ordering is real.
- **A surface** — `voice`, `admin_ui`, `api`, `anonymous`, `system`. Which door it came through.
- **An owner** — the profile or user, or "anonymous". **Never nullable.** Attribution is not something you can retrofit into a message store, which is why spec §13.3 puts it in the P0 schema even though speaker identification is P3.
- **A correlation id**, which follows one turn end to end and is how a tool call is linked back to the message that caused it.

## What is deliberately *not* recorded

Worth knowing, because the absences are decisions:

- **No key material, not even a fingerprint.** A rejected API request is audited with a reason and a path, and nothing derived from the credential — an audit store holding something derived from a credential is one more copy of that credential to protect.
- **No event payloads.** The bus logs source, type, event id and the action taken. `data` could carry anything a camera or a chat bridge put in it.
- **No LLM base URL on the health dashboard.** A hand-edited `core.toml` can carry credentials in one (`http://user:secret@host`), and the dashboard is rendered, logged and screenshotted. A short digest is shown instead — enough to see which roles share a host, without publishing the address.
- **No secret values, anywhere.** Secrets are held as masked values in the core and never rendered; the admin UI can list names only.
- **No connection probes.** Testing an LLM connection changes nothing, and filling the trace view with probes would bury the records that matter.

## What redaction does not catch

Structured logging redacts values it can recognise. **Shape-based detection of an unlabelled bare secret was deliberately not added**, because a rule that fires on "long random-looking string" fires constantly on ordinary conversation text — and a redactor that cries wolf gets switched off.

The consequence, stated plainly:

- **A credential typed into a chat is in the transcript**, like any other message.
- **A credential printed into a log line by a plugin is in the log.**

Neither is caught. If that matters for your household, it is a conversation to have with the people using it, not a setting to turn on.

## How long it is kept

**An age-out with a configurable window; default 30 days.** Set in `core.toml`, and per surface:

```toml
[retention]
default_days = 30
```

`per_surface_days` overrides the default for named surfaces — so, for example, the exposed API and the admin chat box can age out on different schedules. See [Core Settings](Core-Settings).

One behaviour worth knowing, because it was a real defect: **a row whose surface is not a recognised value gets the default retention window rather than being exempt.** Previously such a row was kept forever — a silent retention failure, on the table holding household conversations. Unrecognised now means "purged on the default window", not "purged never".

### The purge runs

ADR-0004 specifies a scheduled purge enforcing the window, and it is now wired in: a background task purges once when the core starts, then every six hours, using whatever `[retention]` currently says. Saving a new window through the admin API applies to the *next* pass immediately — no restart needed. Shutdown waits up to 30 seconds for a purge already in flight, so the process does not report a clean stop while a thread still holds the database.

The purge's outcome is on `/health`: `last_success`, `last_error`, and `consecutive_failures`. A rising failure count with a stale `last_success` means the window is a stated policy again, not an enforced one — check there first if you suspect the purge has stopped. See [Health and Diagnostics](Health-And-Diagnostics).

Two things that were real defects during the wiring, now closed at the point the value is written (the admin API, or startup if you hand-edit the file):

- **A per-surface window below 1 day is refused**, not silently accepted. It used to put the purge cutoff in the future, so the next sweep deleted every row for that surface regardless of age.
- **An unknown surface name is refused with a message naming it and listing the valid surfaces**, rather than being accepted by the API and only discovered at the next restart — which used to crashloop the container so that nothing was purged at all.

## Who can see it

**Anyone the login proxy lets into the admin surface.** The trace view is filterable by owner, surface, correlation id and time range:

```bash
curl -sS "http://127.0.0.1:8053/admin/api/trace?surface=api&limit=50" -H "Remote-User: admin"
```

Filtering by `profile` answers "what did this person do"; filtering by `correlation_id` follows one turn end to end.

There is no finer-grained access control than that. The core does not distinguish admin roles beyond "the proxy named a user", so **every admin can read every household member's conversations.** If that is not the arrangement you want, the control is who gets an account in Authelia — see [Scenario: Putting Authelia in Front](Scenario-Putting-Authelia-In-Front).

**Plugins get no access to transcripts by default**, and there is no contract by which one asks for it.

## Where the data physically is

Inside appdata, which means:

- It is covered by the appdata backup (spec §10) — **and so your backups are as sensitive as the live data.** Treat a snapshot as containing every conversation anyone has had with the assistant, plus the secrets directory.
- It is never inside an image and never touched by an upgrade.
- It inherits spec §7's TLS, authentication and least-privilege requirements.

See [Installation and Upgrades](Installation-And-Upgrades) for the backup procedure, and [Appdata Layout](Appdata-Layout) for the tree.

## The anonymous case

Transcripts still record keyless and anonymous conversations, attributed to the anonymous owner. Retention and the child-safety toggle apply unchanged.

And note the direction of the trade in [ADR-0005](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0005-child-safety-controls.md): filtering is best-effort and defeatable; **monitoring is the effective control.** The log is what actually tells an adult what happened. Every safety block or intervention is logged with the text that triggered it, so a bad rule can be found and fixed rather than guessed at — which also means the triggering text is stored.

See [Scenario: Multi-User and Anonymous](Scenario-Multi-User-And-Anonymous).

## A short checklist for whoever runs this

- [ ] Everyone who uses it has been told, in words, that conversations are recorded.
- [ ] The retention window is set to something you can defend. The purge runs automatically — check `/health`'s `retention` field if you want to confirm it is actually succeeding.
- [ ] Backups of appdata are stored somewhere you would be comfortable putting the transcripts, because that is what they are.
- [ ] Only people who should read everyone's conversations have accounts on the login proxy.
- [ ] Nobody has been told that filtering keeps a child safe. It reduces incidents; the log is what makes them visible.

## Related

- [Audit and Trace](Audit-And-Trace) — record shapes, categories, and the trace API.
- [Security Model](Security-Model) — the whole security picture and its stated limits.
- [Core Settings](Core-Settings) — the `[retention]` block.
