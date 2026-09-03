# Appdata Layout

Every directory under the appdata volume: what lives in it, what is yours to edit, what backups must cover, and what an upgrade must never touch. Read this before you back up, restore or upgrade.

**Appdata is the assistant.** The containers are disposable and rebuildable from the Compose file; everything that matters — plugins, personas, voices, memory, users, audit, config, secrets — lives here on a mounted volume. Its location comes from `PERSONACORE_APPDATA` (see [Environment Variables](Environment-Variables)), defaulting to `./appdata` and to `/appdata` inside the container.

Source: `src/personacore/config/appdata.py`, spec Appendix B.

## The directories

Created on every start if missing. Creating them is all the core does — it deliberately does not create or touch any *file* inside them at layout time.

| Directory | Holds | Yours to edit? |
|---|---|---|
| `plugins/` | One folder per stdio-transport plugin: `manifest.toml`, `config.toml`, code. | Yes — but prefer the admin surface's install and uninstall, which validate. Both are on `/admin/plugins` now: an install form on the page, and an uninstall button, alongside the underlying `POST /admin/api/plugins/install`. |
| `plugins-http.d/` | Registrations for HTTP-transport plugins (which run in their own containers). | Yes, same caveat. |
| `personas/` | One folder per persona: prompt file plus optional `persona.toml`. See [Personas](Personas). | **Yes.** Edit the prompt with any text editor; it hot-reloads. |
| `voices/` | Engine-native voice files, laid out `voices/<engine-id>/<voice-id>/` — one folder per engine, one folder per voice underneath. | Yes, but prefer the Voice screen's upload, which validates (no pickles, scripts or executables) the way a plugin package does. Read live by the voice subsystem (ADR-0029) whenever a persona speaks. |
| `memory/` | The memory subsystem's storage. | Memory is a P1 plugin; the directory is created now and is empty in P0. |
| `users/` | Profiles, permissions, speaker enrolment. Currently holds `api-keys.json`. | **No.** Use the admin API for keys. |
| `secrets/` | One file per secret, named for the secret, containing only its value. | **Yes** — this is how you supply a credential. |
| `audit/` | `audit.db` (the SQLite audit and transcript store) and `personacore.jsonl` (the structured log). | **No.** Read-only in practice; it is the evidence trail. |
| `config/` | `core.toml`, and `plugins-disabled.toml`. | Yes for `core.toml`; prefer the admin UI. |

## Files the core writes

| Path | Written by | Notes |
|---|---|---|
| `config/core.toml` | First run, then the admin API | Never overwritten by an upgrade. See [Core Settings](Core-Settings). |
| `config/plugins-disabled.toml` | The enable/disable endpoints | A single `disabled = [...]` list, so a plugin switched off in the UI stays off across a restart. |
| `personas/default/system_prompt.md` | First run only | Written if no prompt file exists there. Never overwritten. |
| `users/api-keys.json` | The key store | Written atomically, `0o600`. Holds a SHA-256 of each key and its policy profile — never a usable key. |
| `audit/audit.db` | The audit store | SQLite in WAL mode, with an explicit versioned migration table. |
| `audit/personacore.jsonl` | The logger | JSON lines, one record per line, redacted. |
| `plugins/.installed-from-image` | Bundled-plugin seeding | Records which plugins the image has already installed, so deleting one is not undone at the next start. |
| `plugins/.staging/<random>/` | Plugin installation | Uploads are extracted and validated here, inside appdata, before anything moves into place. Never a shared temp directory. |
| `voices/.staging/<random>/` | Voice installation | The same treatment a plugin zip gets, applied to a voice zip: extracted and validated here before anything reaches `voices/<engine-id>/<voice-id>/`. |

## Containment: nothing may point outside

Every path the core hands out is resolved — symlinks and all — and checked to be inside the appdata root. A config value, a plugin name or a persona name is input from outside, and outside input does not get to name a location. A symlink inside appdata pointing at `/etc` resolves outside appdata and is refused, whatever its own location suggests.

A path that cannot be resolved at all — a loop, an illegal name, an embedded NUL byte — is refused the same way rather than raising something raw. The refusal names the volume and tells you what to fix.

`personas/` gets one extra check on top: a resolved persona directory must be *directly* inside `personas/`, not merely somewhere in appdata.

## Backups

Spec §10 requires a nightly appdata snapshot with a tested, documented restore procedure. Everything outside appdata is rebuildable from the Compose file, so the snapshot is the whole backup.

Two things follow that are easy to get wrong:

- **`secrets/` is in the backup.** That is the point — a restore has to work — but it means the backup is credential material and needs to be protected like one.
- **`audit/` is in the backup**, and transcripts are the most privacy-sensitive data in the system (ADR-0004). The same handling applies.

Conversely, `core.toml` is in the backup and is pasted into support conversations, which is exactly why it holds the *name* of a secret and never a value.

## Upgrades

**An upgrade must never touch appdata content** (spec §7). Concretely, that means:

- Appdata is never inside an image. It is a mounted volume.
- Upgrading is `docker compose pull && docker compose up -d`. Rollback is re-pinning the previous image tag.
- The core seeds a starter `core.toml`, a default persona and the bundled plugins **only when they are absent**. An existing file, persona or plugin folder is the operator's and is left alone. A bundled plugin you deleted on purpose is not resurrected.
- An appdata format change requires an explicit, documented migration. The audit store's schema is versioned in an append-only migration list; if the database has been opened by a *newer* build than the one now running, the store refuses to start rather than run unreviewed code against a schema it has never seen, and names both versions in the error.

## What breaks first when the volume fills

The audit log. It is the one component whose failure hides every other failure, so the health dashboard warns below 1 GiB free on the appdata volume. See [Health and Diagnostics](Health-And-Diagnostics).
