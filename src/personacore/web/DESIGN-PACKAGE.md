# PersonaCore admin — HTMX/Jinja2 templates

Server-rendered UI for the PersonaCore admin interface. No build step, no CDN,
no npm. Everything is served by the application itself.

## Contents

    static/
      nocturne.css      the one stylesheet — Nocturne tokens + components,
                        system font stack, inline-SVG icon friendly
      htmx.min.js       ⚠ STUB — vendor the real htmx 2.x file (see comment
                        inside; BSD-0, single-file drop-in)
      admin.js          ~100 lines: modal plumbing, list edits that must not
                        round-trip, copy buttons, file-input label
    templates/
      base.html         app shell + sidebar nav (Chat first; Security group
                        holds Access keys, with Users/Authentication marked
                        "later"; Logs last)
      _controls.html    THE GENERATED-FORM VOCABULARY — one macro per control
                        (toggle, dropdown, number, text, multiline, list of
                        strings, secret picker, repeating group) plus the
                        render_field dispatcher and the inline SVG icon set
      chat.html · health.html · models.html · plugins.html ·
      plugin_detail.html · personas.html · core.html · keys.html · logs.html
      fragments/        everything that swaps: chat_exchange, model_form,
                        model_test, settings_form, group_entry, search_results,
                        plugin lists via plugins.html blocks, confirm,
                        key_new, key_created

## Fragment routes to implement

Pages (full render, extend base.html):

    GET  /admin/chat  /admin/health  /admin/models  /admin/plugins
    GET  /admin/plugins/{name}[?tab=raw]   /admin/personas  /admin/core
    GET  /admin/keys  /admin/logs

Fragments (return only the named template):

    POST /admin/chat/fragment                          → chat_exchange.html
    GET  /admin/health/fragment                        → health body (poll, 15s)
    POST /admin/models/{role}/test/fragment            → model_test.html
    POST /admin/models/{role}                          → model_form.html
         (body carries action=save|clear; respond with save_result of kind
          saved / nothing / error)
    POST /admin/plugins/install                        → #plugin-list (multipart)
    POST /admin/plugins/{name}/enable|disable/fragment → one plugin row
    GET  /admin/plugins/{name}/uninstall/confirm/fragment → confirm.html
    POST /admin/plugins/{name}/uninstall               → #plugin-list
    POST /admin/plugins/{name}/settings                → settings_form.html
    POST /admin/plugins/{name}/settings/raw            → plugin_detail (or form)
    GET  /admin/plugins/{name}/settings/{key}/entry/fragment?index=N
                                                       → group_entry.html (blank)
    GET  /admin/plugins/{name}/settings/{key}/search/fragment?q=…&entry=N
                                                       → search_results.html
    GET  /admin/plugins/{name}/settings/{key}/fill/fragment?entry=N&pick=I
                                                       → group_entry.html (filled;
          hx-include sent the entry's current inputs — merge, don't reset)
    POST /admin/personas/install · /admin/personas/{slug}/default
    POST /admin/personas/{slug}/voice (hx-swap="none")
    GET  /admin/personas/{slug}/delete/confirm/fragment → confirm.html
    POST /admin/personas/{slug}/delete                 → page
    POST /admin/core                                   → core form re-render
    POST /admin/core/auth-bypass · /admin/core/bus-auth
    POST /admin/core/purge/run/fragment                → #purge-status banner
    GET  /admin/keys/new/fragment                      → key_new.html
    POST /admin/keys                                   → key_created.html
         (+ hx-swap-oob refresh of #key-list; NEVER persist or re-serve the value)
    GET  /admin/keys/{id}/revoke/confirm/fragment      → confirm.html
    POST /admin/keys/{id}/revoke                       → #key-list, or the page
         with refused_message when the key authorized the current session
    POST /admin/keys/keyless/enable|disable/fragment   → #keyless card
    POST /admin/keys/keyless/addresses|tools/add|remove/fragment → #keyless card
    GET  /admin/logs/fragment?surface=…                → #log-entries

## Swap boundaries — the two interactions the stack was chosen for

**Repeating groups** (`_controls.html::group` + `fragments/group_entry.html`)

- The group's entry stack is `#group-{key}`. "Add an entry" appends ONE blank
  entry fragment `beforeend`; nothing else re-renders, so unsaved edits in
  other entries survive.
- Remove and reorder are client-side (admin.js) for the same reason: a server
  re-render of the group would wipe sibling values that only exist in the DOM.
- Entry input names are flat: `{groupkey}-{index}-{fieldkey}`
  (e.g. `locations-0-lat`). Indexes may be sparse after removals; treat the
  posted set as authoritative.

**Search-and-fill** (`group_entry.html` + `fragments/search_results.html`)

- Search GETs into that entry's own `#results-{key}-{index}` div.
- Picking a hit re-renders ONLY the owning `<fieldset class="entry">`
  (hx-target="closest fieldset", outerHTML), with the operator's current
  inputs sent along via hx-include — merge the picked values over them.
- "Only one search runs at a time": when serving results, emit an empty
  `<div class="search-results" id="results-…" hx-swap-oob="innerHTML">` for
  any other entry with open results.

## State conventions the markup expects

- Field `state`: `default` | `saved` | `unsaved` | `invalid`. Default renders a
  dashed border + the words "plugin default"; unsaved an accent border +
  "unsaved change"; invalid a danger border + "needs attention" + the error
  under the control. Never colour alone — the words are part of the pattern.
- Save responses are one of: **saved** ("Wrote 2 settings to
  data/plugins/weather.json and restarted weather."), **nothing** ("Nothing
  changed — weather was not restarted."), **invalid** ("Not saved — 2 fields
  need attention above. Nothing was written."). Invalid responses echo the
  operator's input back verbatim (number inputs are type=text for this).
- Refused renders the `banner danger` with the reason and the way out.
- Broken-but-editable: `plugin_detail.html` renders health + the same settings
  form whether the plugin loaded or not.
- Secrets: `secret()` renders NAMES only. The server must never put a secret
  value (masked or otherwise) in any template context.

## Accessibility

Semantic buttons/labels/fieldsets throughout; switches are real checkboxes
with `role="switch"`; errors use `aria-invalid` + `aria-describedby`; swap
targets that announce use `aria-live`/`role="status"`/`role="alert"`; focus is
the 2px accent `:focus-visible` ring; log entries are native `<details>`.
