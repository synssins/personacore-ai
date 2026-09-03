# The designed admin UI

The approved design, delivered from Claude Design as HTMX and Jinja2 templates
rather than as mockups (ADR-0020). These are the templates the real admin
interface is built from; `DESIGN-PACKAGE.md` is the designer's own handover
notes.

`routes.py` renders them. It is built by
`personacore.admin.routes.create_admin_router` and mounted from there rather
than from the application, so it is handed **the same `require_user` dependency
the JSON API is guarded by** — one check, from one configured header, in front
of both presentations. That is the point ADR-0020 makes about this layer, and
`tests/server/test_admin_ui.py` asserts it over HTTP for every route.

ADR-0007's test surface, which stood in for this UI, has been deleted — the
promise the ADR made was "deleted, not evolved", and this is what replaced it.
`GET /admin/` redirects here, to `/admin/chat`.

## What is served

Slice one — the shell and the read-only screens:

| Route | Template |
|---|---|
| `GET /admin/health` | `health.html` |
| `GET /admin/health/fragment` | `fragments/health_body.html` (15s poll) |
| `GET /admin/plugins` | `plugins.html` |
| `GET /admin/logs` | `logs.html` |
| `GET /admin/logs/fragment?surface=…` | `fragments/log_entries.html` |
| `GET /admin/static/{name}` | the three vendored assets |

Slices two and three added chat, model connections, core settings, the
per-plugin screen and every action that writes. The last two screens
`DESIGN-PACKAGE.md` lists — Personas and Access keys — are below.

The plugins page is now whole: every control on it works, and each one reaches
the JSON API's own handler through `api_handler` rather than doing the work
itself.

| Route | Template |
|---|---|
| `POST /admin/plugins/install` | `fragments/plugin_installed.html` |
| `POST /admin/plugins/{name}/enable/fragment` · `.../disable/fragment` | `fragments/plugin_installed.html` |
| `GET /admin/plugins/{name}/uninstall/confirm/fragment?where=list` · `POST .../uninstall/fragment` | `fragments/confirm.html`, then `fragments/plugin_installed.html` |

All three answer with the re-rendered `#plugin-list` and swap the sentence
saying what happened into `#install-result` out of band, so a row updates in
place and never as a status code. The same confirmation dialog serves the
per-plugin screen, where `where` is absent and the confirmed post swaps the
whole body instead — there is no list left behind it to swap.

## Personas and access keys

| Route | Template |
|---|---|
| `GET /admin/personas` | `personas.html` |
| `GET /admin/personas/new` · `POST /admin/personas` | `persona_edit.html` |
| `GET /admin/personas/{slug}/edit` · `POST /admin/personas/{slug}` | `persona_edit.html` |
| `POST /admin/personas/{slug}/default` | `personas.html` |
| `GET /admin/personas/{slug}/delete/confirm/fragment` · `POST .../delete` | `fragments/confirm.html`, then `personas.html` |
| `GET /admin/keys` | `keys.html` (+ `fragments/key_list.html`) |
| `GET /admin/keys/new/fragment[?tab=raw]` · `POST /admin/keys` | `fragments/key_new.html` → `fragments/key_created.html` |
| `GET /admin/keys/{id}/revoke/confirm/fragment` · `POST .../revoke` | `fragments/confirm.html`, then `fragments/key_list.html` |

Four things about these two are load-bearing:

- **The persona identifier is on every card, copyable.** ADR-0017 selects a
  persona by putting its name in a client's model field, so a screen that shows
  the character without the string that selects it shows nothing actionable.
  The `/v1/models` half of that mechanism is **not built** (PC-104), so the page
  says so and marks it `later` rather than promising it.
- **Deleting a persona names what it breaks** — the keys bound to it, and, for
  the default, the fact that the result is a silent identity swap rather than an
  error: the default is repointed (writing the starter persona when nothing is
  left) and every client pinned to `personacore` changes character.
- **The persona identifier is fixed at creation.** The canvas said renaming
  changes it and that pinned clients fall back to the default; they do not — a
  key naming a folder that no longer exists raises `PersonaNotFoundError` on the
  next turn. The Name box therefore sets the display name only. This is the
  second knowing departure from the canvas after Chat.
- **A key value is rendered once, at issue, and never again.** The copy button
  reads the `<code>` element rather than carrying the value in an attribute, so
  the credential is in the page exactly once. Everything else on the screen is
  built from `ApiKeyView`, which has no field for a key or a hash.

Writes go through the JSON API's own handlers wherever one exists
(`API_HANDLERS`): `select_persona`, `list_api_keys`, `issue_api_key`,
`revoke_api_key`. Creating, editing and deleting a persona has no JSON handler —
spec §5.5 defines a persona as files in a folder — so this surface writes them,
through `PersonaStore.resolve_dir` for the path check.

Marked `later` on these two screens: installing a persona from a `.zip`
(packaging is unbuilt), the suggested-voice field on both persona screens (the
voice pipeline is P1 and nothing reads the suggestion), the model-name selection
mechanism, and the whole keyless-access panel (ADR-0018 has no backend).

The per-plugin screen is at the design's own path:

| Route | Template |
|---|---|
| `GET /admin/plugins/{name}[?tab=raw]` | `plugin_detail.html` |
| `POST /admin/plugins/{name}/settings` | `fragments/settings_form.html` |
| `POST /admin/plugins/{name}/settings/raw` | `plugin_detail.html` |
| `GET /admin/plugins/{name}/settings/{key}/…/fragment` | the entry, search and fill fragments |

The screen is the plugin's own path; everything *about its settings* sits one
segment deeper. One consequence is worth knowing: `/admin/plugins/health` and
`/admin/plugins/logs` are declared before `/admin/plugins/{name}`, so a plugin
actually named `health` or `logs` would find those two screens where its own
settings screen should be. Its output page (`/admin/plugins/health/logs`) and
every action on it still work. This is the design's path, so the collision is
recorded here rather than routed around.

Two screens about plugins alone, added afterwards at the owner's request:

| Route | Template | Why |
|---|---|---|
| `GET /admin/plugins/health` | `plugin_health.html` | PC-280 — state, transport, declared tools, restarts and last error per plugin, without the disk and the broker in between |
| `GET /admin/plugins/logs` | `plugin_logs.html` | PC-279 — what every plugin has printed, the sweep |
| `GET /admin/plugins/{name}/logs` | `plugin_logs.html` | PC-279 — one plugin's output; the destination every failing row links to, so the errand is one click |

Both are read-only, and both are alphabetical by plugin name (PC-281), through
`plugin_page.by_name` — the same function `plugin_rows` sorts the plugin list
with, so the three screens agree by construction rather than by coincidence.

The output comes from the bounded per-plugin stderr capture in
`plugins/mcp_client.py`, reached through `PluginHealthSource.output_for`, which
is optional and discovered with `getattr` exactly as `reload` is. Two things
about it are load-bearing:

- **It is untrusted.** It is third-party code's own output, so it is escaped and
  rendered as text, never markup (spec §7).
- **It is bounded and truncating**, so the page says when output has been
  clipped or thrown away rather than passing off a tail as the whole thing.

## Conventions this layer keeps

- **Static assets are served at `/admin/static/…`**, by this application, from
  `../static/`. Not a CDN (ADR-0020), and not the root `/static`, which the
  admin UI has no business claiming. A three-name allowlist rather than a
  directory mount, so no spelling of the URL reaches outside the folder.
- **A control with no backend is disabled and marked, never removed.** The
  token is `<span class="later">later</span>`, which `base.html` established
  beside Users and Authentication. The shape of the finished product is part of
  what the design is for, so hiding an unbuilt control hides the design.
- **Nothing is invented to fill a template.** Where a record carries no value
  for a field the design shows — how many tools a turn offered, which model
  answered it — the fact is marked `later` rather than guessed at.
- **A fragment and the page that first renders it share one template.** The
  swap targets (`fragments/health_body.html`, `fragments/log_entries.html`) are
  `{% include %}`d by their pages, so a poll or a filter cannot drift from what
  the page first showed.
