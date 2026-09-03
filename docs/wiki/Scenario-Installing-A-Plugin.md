# Scenario: Installing a Plugin

You have a plugin as a zip file and you want the assistant to be able to use its tools.

For writing one, see [Scenario: Writing a Plugin](Scenario-Writing-A-Plugin). For what a plugin *is*, see [Plugin Contract](Plugin-Contract).

## What a plugin package is

A zip archive containing **one plugin directory**, with `manifest.toml` at its root or one level down. Nothing new to author: it is the folder that already works, zipped ([ADR-0013](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0013-plugin-packages-and-installation.md)).

The zip is a *transport*, not a format. The contract is still the folder and the manifest, so a plugin can equally be installed by copying a directory — see the fallback at the bottom of this page.

## 1. Upload it

**From the admin surface**, the Plugins screen has a working install control: choose a `.zip` with the file picker, and the core reads the package and shows you a disclosure dialog before anything is installed — what the plugin says it is, its tools and their risk, any secrets it declares (each with the author's own description beside it, so you can paste a credential right there on the same screen), whether it declares `provides` (registering itself as a speech engine or a transcriber for the whole core), the hosts and paths it names, and where it actually runs. Only clicking **Install** in that dialog writes anything to disk.

**From a script or the command line**, there is a separate, lower-level endpoint that takes the zip's raw bytes in the request body, not as a form upload:

```bash
curl -sS -X POST "http://127.0.0.1:8053/admin/api/plugins/install?filename=kitchen-timer.zip" -H "Remote-User: admin" -H "Content-Type: application/zip" --data-binary @kitchen-timer.zip
```

Three things about that command:

- **`Remote-User` is the trusted identity header**, and it is honoured only from an allowlisted peer — loopback by default. From anywhere else it is stripped before any route sees it, and you get a 401. Run this on the host, or put your proxy in front. See [Security Model](Security-Model).
- **This endpoint refuses a multipart form upload, in words**, telling you to send the zip's raw bytes instead. That is what makes it a scripting endpoint rather than something a browser `<form>` can post to directly — the admin-surface install control above is a separate route built for the browser case.
- **`filename` is cosmetic.** It exists only so the audit record can say which file an operator chose. It is never used to build a path and never appears in a response.

Enable, disable, uninstall and settings all have buttons on `/admin/plugins`, and installing, enabling and disabling from there already reload the supervisor for you — there is no separate reload button to click. `POST /admin/api/plugins/reload` still exists for the one case that needs it: a plugin folder dropped in by hand (see the fallback below).

## 2. What the core does with it

In this order, and the order is the point:

1. Writes the upload to a **staging directory inside appdata** — `<appdata>/plugins/.staging/<random>/` — never a shared temp location.
2. Extracts it there and **validates the manifest before anything is moved** into the plugins directory.
3. Only then moves the validated directory into place, and reloads.

**Installation never executes anything from the package.** No setup script, no import, no `pip install`. A plugin runs when the supervisor starts it, under its manifest's declared permissions — not while it is being unpacked, when nothing has been checked yet.

The staging directory is removed on every path, including failure.

## 3. Refusals you may hit, and what each means

An uploaded zip is the most exposed surface in the product: a path from "a file" to "code the core will execute". Every one of these is a **refusal with the reason in plain English and nothing left on disk** — not a warning.

| Refusal | What tripped it |
|---|---|
| Path traversal | An entry whose resolved destination is outside the staging directory: `../`, an absolute path, a drive-relative path, or a symlink pointing out. Classic zip-slip. |
| Symlink or non-regular entry | Rejected outright, not followed. |
| Too large / too many entries | Ceilings on the archive (32 MB), the **uncompressed** total (128 MB) and the entry count (2000). The uncompressed limit is checked twice — against what the archive's headers declare, and again against the bytes actually written, because a header can lie. |
| Manifest name does not match its directory | Rename one to match the other. |
| Name collides with an installed plugin | Pass `?replace=true` if you meant to upgrade it. |
| Invalid manifest | The message names the file and the offending key. See [Plugin Manifest](Plugin-Manifest). |

**Replacing keeps the existing `config.toml`.** An upgrade must not discard settings (spec §7). If you genuinely want fresh settings, uninstall first.

Packages are **not signed**, deliberately: a signature is only worth anything with a key distribution story, and there is no plugin ecosystem to sign. That is revisited if plugins ever come from anywhere but your own hand — until then, read a manifest before you install it.

## 4. Check it started

Reload and the plugin list will show it. Two different things can be true and they look nothing alike:

- **It loaded and is running** — its tools are now offered to the model.
- **It loaded and is not running** — the supervisor is retrying it with backoff. A config error that stops it dead shows here, with the reason in the detail. A plugin that has never successfully started is *not* reported as healthy.

A plugin that crashes is killed, logged, restarted with backoff and shown as unhealthy. A bad plugin never takes the core down (spec §5.1).

If it does not appear at all, [Scenario: Debugging a Plugin](Scenario-Debugging-A-Plugin) walks the three failure shapes.

## 5. Configure it

A plugin's settings live in **its own folder**, as `config.toml`. The core never stores plugin settings centrally (spec §5.1).

Open the plugin's page in the admin surface. What you get depends on whether the plugin shipped a `config.schema.json`:

- **With a schema**: a real form. Booleans become toggles, enums become dropdowns, numbers carry their minimum and maximum, `title` and `description` become the label and help text. Values are validated *before* anything is written; a rejected value names the field, says what was wrong in plain English, and leaves the file on disk untouched. ([ADR-0015](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0015-plugin-config-schema.md).)
- **Without one**: the raw `config.toml` in a text box, comments preserved — a plugin's comments are its field help, and re-serialising a parsed document would delete them. A plugin is never *required* to ship a schema.

Some settings offer a **search box** instead of asking you to find a number somewhere else. The weather plugin's locations are the worked example: type a town, pick it from the list, and the label and coordinates fill in. That is a real tool call on the plugin — audited, shown in the trace, and permitted only because the plugin nominated a `safe`-risk tool of its own for the job ([ADR-0016](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0016-config-field-lookups.md)).

Saving is atomic, restarts the plugin, and is audited. **The core does not validate what your settings mean** — the plugin does that at startup, and its error message is what appears beside it in the UI. A plugin that fails to start after a settings change has almost certainly told you why in that message. See [Plugin Configuration](Plugin-Configuration).

**A file that does not parse is still returned to you**, with the syntax error attached — the broken one is the one you need to open.

## 6. Let something actually use it

Installing a plugin does **not** make its tools reachable. `allowed_tools` on a [policy profile](Policy-Profiles) is an allowlist, and empty means none, not "all the safe ones". A newly installed plugin is unreachable until someone decides otherwise.

- The admin chat box is the exception, and a deliberate one: it is granted exactly the `safe` tools currently installed, because an admin trying the assistant and silently getting no tools would conclude they were broken.
- For an API client, the tools go in the profile attached to its key. See [Scenario: Third-Party Clients](Scenario-Third-Party-Clients).

Tool names are qualified: `<plugin>.<tool>` — for example `weather.get_forecast`. Use the qualified name in an allowlist.

## Switching one off without removing it

A plugin can be **disabled** rather than uninstalled: it stays on disk with its settings, the supervisor does not start it, its tools are not offered to the model and not callable. The state lives in `<appdata>/config/plugins-disabled.toml` and survives restarts.

In the plugin list a disabled plugin reads as *"Switched off in the admin interface. Its folder and its settings are still here…"* rather than the bare word "disabled", because the same row appears on the health dashboard and a state with no explanation next to it reads as a fault.

## Uninstalling

Explicit and confirmed, and it tells you what it will delete — including **whether the plugin's config goes with it**, which it does, because config lives inside the folder.

## The fallback: copying a folder

Still supported, and sometimes the right tool:

```bash
cp -r ./kitchen-timer /srv/personacore/appdata/plugins/kitchen-timer
```

Then hit reload. Reload does two things, and the second one was once missing: it tells the supervisor to start, stop and restart plugins to match what is now on disk, **and then** rescans the listing. Without the first, a copied-in plugin appeared in the list with no live status and never ran.

HTTP-transport plugins go in `<appdata>/plugins-http.d/` instead — see [Plugin HTTP Transport](Plugin-HTTP-Transport).

Folder names beginning with `_` or `.` are not treated as plugins, which is why the bundled `_template` can sit in the plugins directory without the core trying to run it.

## Related

- [Plugin Lifecycle](Plugin-Lifecycle) — load, start, backoff, reload.
- [Plugin Packaging](Plugin-Packaging) — building a package.
- [Admin API](Admin-API) — the endpoints behind every button here.
