# Audit and Trace

What PersonaCore records about itself, how the two record families differ, how to follow one turn end to end, and what deliberately never reaches the log stream. Read this if you are investigating what the assistant did, or deciding how long to keep it.

The principle (spec §9): *if you can't see what it did, you can't trust it or debug it.*

Source: `src/personacore/audit/`, ADR-0004.

## Three places things are written

| Where | What | Path |
|---|---|---|
| **Audit store** | What the assistant *did* — tool calls, confirmations, admin changes, access refusals. | `<appdata>/audit/audit.db` |
| **Transcript store** | What was *said* — every message, in and out, on every surface. | the same SQLite file |
| **Log stream** | Structured operational logging: JSON to stdout and to a file. | `<appdata>/audit/personacore.jsonl` |

The first two are the evidence trail. The third is for reading while something is going wrong. **Conversation content and audit detail go to the store, never to the log stream** — the audit store's own log calls pass only ids, categories, surfaces and counts.

## The two record families

They share a schema shape because they answer the same question from two angles, and the trace view needs to group and filter both the same way: by owner, by surface, by time, and by correlation id.

### Audit record

| Field | Notes |
|---|---|
| `record_id` | UUID. |
| `correlation_id` | Groups everything one request did. |
| `timestamp` | **Must be timezone-aware.** A naive value is refused on write. |
| `surface` | `voice` \| `admin_ui` \| `api` \| `anonymous` \| `system` |
| `owner` | `{kind, id}` — see below. |
| `category` | See the table below. |
| `action` | Free text in the publisher's own vocabulary: `weather.get_forecast`, `plugins.install`, `api.chat_completion`. |
| `risk_level` | `safe` \| `confirm` \| `restricted`. Only meaningful for tool calls; null otherwise. |
| `outcome` | `success` \| `failure` \| `refused` \| `pending` |
| `detail` | Action metadata — arguments, reasons, counts. This is metadata, not conversation content. |

### Transcript record

`record_id`, `correlation_id`, `timestamp`, `surface`, `owner`, plus `role` (`user` \| `assistant` \| `system` \| `tool`) and `content`.

`content` is the most privacy-sensitive field in the whole system. It is written here and never passed to the log stream.

## Categories

The four spec §7 names, plus one:

| Category | Covers |
|---|---|
| `tool_call` | Every tool call — run, failed or refused — with its arguments and outcome. |
| `event` | An event that woke the agent. |
| `confirmation` | A confirm/restricted decision, given or refused. |
| `admin_change` | Every admin change. |
| `access` | A turn or request refused before it began: no profile, a disabled profile, a bad API key. |

`access` is not literally on spec §7's list. It is here because that list is a floor, not a ceiling: a refused access attempt is what an intrusion looks like from the inside, and a log that records every door opened but no door rattled is missing the half that matters.

## Attribution

Every record is attributed (spec §8) — nullable-and-hope is exactly what the owner model exists to rule out. A record with no clear owner fails to construct rather than falling back to null.

| `owner.kind` | `owner.id` |
|---|---|
| `profile` | The concrete profile id — household member or API-key client. |
| `household` | The literal string `household`. |
| `anonymous` | The literal string `anonymous`. |

The sentinel ids are pinned by constructor methods so every call site in the codebase uses the same spelling.

## Correlation ids

One id per request, shared by **every** audit record, transcript row and structured log line that request produces.

It is bound to the async context at the start of a turn and unbound at the end, so any log call anywhere in the stack picks it up without being passed it. If the caller supplies one, that is used; otherwise a fresh uuid4 is generated.

Two useful consequences:

- The `/v1` completion id is `chatcmpl-<correlation_id>`. A user quoting the id their client showed them lands an operator directly on that turn in the trace view.
- The settings-page lookup (ADR-0016) leaves **two** entries under one id — the admin action, and the plugin tool call it caused — because the plugin host writes its own record for the call itself.

Transcript records are linked to the audit records their message produced by sharing that id, rather than by a join table. One fewer thing to keep in sync, and it is the same id the log stream already groups by.

## Reading it: the trace view

`GET /admin/api/trace` merges both families into one descending timeline. Filter by `profile`, `surface`, `correlation_id`, `since`/`until` and `kind`; page with `limit` (1–200, default 50) and `offset`. See [Admin API](Admin-API).

The two families are queried separately and merged in code rather than in SQL, because they live in different tables with different columns and a `UNION` would force both into a lowest-common-denominator row that loses exactly the fields — tool arguments, message content — the view exists to show.

`has_more` is measured rather than guessed: the window is always fetched one row longer than the page needs. At most 2000 rows are pulled to satisfy one page, which caps what deep pagination can cost a container.

## What is recorded where, by surface

| Event | Record |
|---|---|
| A turn begins | Transcript, role `user`. |
| The assistant answers | Transcript, role `assistant`. |
| A tool result comes back | Transcript, role `tool`, **fenced** exactly as the model saw it. |
| A tool is called | Audit `tool_call`, with arguments and outcome. |
| A tool is refused by policy | Audit `tool_call`, outcome `refused`, with the reason and arguments. |
| A confirmation is asked or blocked | Audit `confirmation`, with the decision and arguments. |
| A `/v1` request succeeds or fails | Audit `access`, action `api.models` or `api.chat_completion`. |
| A `/v1` request is rejected at the door | Audit `access`, action `api.request_rejected`, owner `anonymous`. |
| A turn is refused for having no usable profile | Audit `access`, action `turn_refused`. No transcript — no content is accepted and there is no owner to attribute one to. |
| Any admin change | Audit `admin_change`, attributed to the proxy-named user, surface `admin_ui`. |

The plugin host writes its own record for every call it handles — plugin, tool, arguments, outcome, duration, risk, correlation id — so a call made from somewhere other than the agent loop is still recorded.

## Writing never fails a request

An audit or transcript write that fails is logged loudly and does not abort what was happening. A full disk should degrade the audit trail, visibly, rather than leave the house without an assistant. Erring the other way — refusing to act when the write fails — was considered and rejected as a denial-of-service on the household.

The health dashboard is the compensating control: it reports the audit directory's writability directly, because an assistant that keeps answering while silently recording nothing is worse than one that stops. See [Health and Diagnostics](Health-And-Diagnostics).

Writability is tested with an access check on the directory rather than by writing a probe record — the store is the evidence trail, and salting it with health-check rows to prove it works corrupts the thing being proven.

## Retention

ADR-0004: an age-out with a configurable window, default 30 days, set per surface. Both families are purged, since both are attributed by surface and both are the highest-value data in appdata from a privacy standpoint.

The purge is implemented and takes a per-surface window with a default fallback. A row whose `surface` column is not a current surface value — schema drift, manual repair — is still purged on the default window rather than kept forever, because a silent retention failure on the most privacy-sensitive table in the system is not acceptable.

**Both gaps are now closed:**

1. The `[retention]` section of `core.toml` **is** connected to the store — the application assembly constructs the audit store with the configured window, and a saved change is applied to the store live via `AuditStore.set_retention`, with no restart needed.
2. **The purge is scheduled.** A background task runs it once at startup and every six hours after, and shutdown waits up to 30 seconds for a pass already in flight rather than reporting done while a thread still holds the database.

The purge's outcome — `last_success`, `last_error`, `consecutive_failures` — is on `/health`, so a sweep that has been failing since startup is visible there rather than only in a log. See [Health and Diagnostics](Health-And-Diagnostics) and [Scenario: Retention and Privacy](Scenario-Retention-And-Privacy).

## Everyone is recorded

ADR-0004 requires that the admin UI say so plainly on the relevant screens. Every message, from every household member on every surface, is stored — not just the child whose usage prompted the requirement. A household member should not discover this by accident.

Transcripts are inside appdata, so they are covered by the nightly backup and are never inside an image. They inherit spec §7's TLS, auth and least-privilege requirements, and plugins get no access to them.

## What never reaches the log stream

A redaction processor runs on **every** structured log record, not opt-in per call site. It replaces the value of any field whose key is a known-sensitive name (`api_key`, `token`, `access_token`, `refresh_token`, `secret`, `client_secret`, `password`, `authorization`, `auth`, `bearer`, and case/hyphen variants) and rewrites labelled shapes inside free text: `Bearer <value>`, `api_key=<value>`, `token: <value>`, `Authorization: <value>`. Exceptions passed as log fields are unwrapped — type name kept, message and args scanned — because `error=exc` is a very common pattern.

Beyond redaction, several things are kept out of the stores by design:

- **Conversation content never goes to the log stream.** The store's own log calls carry only ids and counts.
- **The rejected text of a config write is never recorded** — only the reason and the key names. It is the operator's document and the record goes into backups.
- **A plugin lookup's results are never recorded** — only the query and the number of matches. The results are untrusted text from a plugin.
- **An issued API key is never recorded**, and neither is its note (operator free text destined for backups). Only identifiers.
- **No key material or fingerprint** appears in the rejection record for a bad API key. An audit store holding something derived from a credential is one more copy of that credential to protect.
- **The LLM base URL is never on the dashboard** — only a digest — because a hand-edited config can put credentials in one.
- **Event payloads are never logged**; only topic, source, type and id.

**What redaction does not catch: an unlabelled bare secret** — a raw key logged under an innocuous field name with no surrounding words. Shape-based detection of arbitrary high-entropy strings would flag ordinary conversation text constantly, so it is deliberately not attempted. Do not log a bare secret and rely on the processor.

## The database

SQLite, WAL mode, one file. Migrations are explicit and versioned — a `schema_version` table plus an ordered, append-only list of migration functions — rather than an ORM inferring a diff, because spec §7 requires appdata format changes to be explicit and documented.

Each migration and its version bump apply as one atomic unit, so a crash between them cannot leave the database holding half a step forever.

If the on-disk `schema_version` is **ahead** of what the running build knows how to produce — a downgrade, or a backup restored from a newer build — the store refuses to open and names both versions. Silently proceeding would run unreviewed code against a schema it has never seen.

The current schema version is reported on the health dashboard.
