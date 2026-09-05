# Plugin Packaging

How to package a plugin as a zip, what installation refuses and why, and what happens to an operator's settings when you ship an upgrade. Read this before you hand your plugin to someone else.

Source: `src/personacore/plugins/packages.py`, `src/personacore/admin/routes.py`. Decision: ADR-0013.

## Why a zip

Spec §5.1 describes installing a plugin as "copy a folder, hit reload". That is true of the mechanism and useless as an experience: it needs shell access to the machine, knowledge of the appdata layout, and correct file ownership.

ADR-0013 replaces it with **a zip uploaded through the admin UI**. The Plugins screen carries a two-step install form: one post shows what the package declares — its permissions, its tools, any service it registers as — before anything is written to disk, and the dialog's Install button then sends the same upload. That form's post is a thin wrapper: the bytes it carries go to the same installer as the JSON endpoint, `POST /admin/api/plugins/install`, so a package installed from the page and one installed with a direct API call get identical checks, staging and audit records. The zip is a *transport*, not a new format — the contract is still the folder and the manifest, so nothing you already know becomes wrong, and copying a directory in by hand still works exactly as before.

## The format

**A zip archive containing one plugin directory, with the manifest at its root or one level down.** Both shapes work, because both are what people actually produce:

```
kitchen-timer.zip
└── kitchen-timer/          ← "zip this folder"
    ├── manifest.toml
    ├── config.toml
    └── main.py
```

```
kitchen-timer.zip           ← "zip the contents of this folder"
├── manifest.toml
├── config.toml
└── main.py
```

Refusing either would be a rule nobody can remember.

**`__MACOSX` is ignored.** macOS adds it to every zip made in Finder, and a package rejected for "more than one folder" because of it would be a refusal nobody can act on.

**The folder name matters when it is there.** In the wrapped shape, the folder's name is used as the plugin's folder name and must match `plugin.name`. In the flat shape the name comes from the manifest.

**A folder named with a leading `_` or `.` is refused**, because it would never load:

> The folder inside the package is named '_template'. A leading underscore or dot marks a folder the core deliberately ignores, so it would never load. Rename it to the plugin's own name — the template ships as '_template' precisely so it is not loaded until you copy and rename it.

## How installation works

1. The upload is written to a staging directory **inside appdata** — `<appdata>/plugins/.staging/<random token>/` — never a shared temp location. The machine's `/tmp` may be a different volume, world-readable, or absent in a container; being a sibling of the destination also makes the final move a rename rather than a copy across filesystems. The leading dot keeps discovery out of it, so a half-unpacked upload can never be scanned as a plugin.
2. It is extracted there, member by member, refusing every hostile member.
3. The staged folder is arranged into a miniature appdata layout and handed to **the real `PluginDiscovery`** — the same scanner that loads plugins at startup. **Validation is reused, not re-implemented**, so there is exactly one idea in the core of what a valid plugin folder is, and a package cannot be installable and unloadable at once.
4. Only then is the validated folder moved into place.
5. The staging directory is removed on every path, including failure.

A validation failure comes back as the discovery message with the staging path stripped out, leaving `plugins/kitchen-timer/manifest.toml` — where the file is inside *your* package.

## Nothing from a package is ever executed

**No setup script, no `pip install`, no import of anything inside the archive.** Extraction is inert byte copying.

A plugin runs when the supervisor starts it, under the permissions its manifest declares — not while it is being unpacked, when nothing has been checked yet (ADR-0013). This module never touches a process; even the enable/disable functions only write a file, leaving "stop the running plugin" to the host.

Practical consequence for authors: **your dependencies are your problem.** There is no install hook. Either depend on what the core image already has (`mcp`, `pydantic`, `httpx`, and the standard library — what both bundled plugins use), or ship a virtualenv in your folder and point `entry` at it: `entry = ".venv/bin/python main.py"` ([Runtime Environment](Plugin-Runtime-Environment)).

## What installation refuses

Every one of these is a refusal, not a warning. Sanitising a name and carrying on installs the package that just tried to write outside its own folder — and turns *"this archive tried to write to /etc"* into *"this archive installed fine"*.

`ZipFile.extractall` is deliberately not used, for exactly that reason.

### Hostile members

| Refused | Message |
|---|---|
| An entry with no name, or a NUL in the name | *The package contains an entry with no usable name. It is malformed or deliberately built to confuse an unpacker; nothing was installed.* |
| A symbolic link | *The package contains a symbolic link ('x'). Links are refused outright, never followed: a link is how an archive reaches out of the folder it is allowed to write to. Zip the plugin folder with links resolved, or without them.* |
| Anything that is not a plain file or folder | *The package entry 'x' is not a plain file or folder. A plugin package holds only files and folders.* |
| An absolute path | *The package entry '/etc/passwd' is an absolute path. Every entry must be relative to the plugin's own folder; nothing was installed.* |
| A Windows drive letter | *The package entry 'C:\x' carries a drive letter, which makes it an absolute path on Windows…* |
| A `..` segment | *The package entry '../x' contains a '..' segment, which points out of the folder it is being unpacked into. Nothing was installed.* |
| An entry that would land outside the extraction root by any other route | *The package entry 'x' would be written outside the folder it is being unpacked into. Nothing was installed.* |

The member's destination is built from its **parts**, not by joining the raw name, so a name the platform would read as drive-relative or rooted cannot re-anchor the join.

One deliberate subtlety on file types: only the file-type bits decide, and only when the archive set them. Plenty of honest zips carry permission bits with no type bits at all (`ZipFile.writestr` stores `0o600`, and archives written on Windows store FAT attributes instead), so "no type recorded" has to mean "a plain file" or every ordinary package would be refused.

### Size and count limits

A zip bomb is a denial-of-service against the machine that runs the house.

| Limit | Default |
|---|---|
| `max_archive_bytes` | 32 MiB |
| `max_uncompressed_bytes` | 128 MiB |
| `max_entries` | 2000 |

The uncompressed total is the number that matters: 40 KB of zip can be 4 GB of disk. It is checked **twice** — against the sum the archive's headers declare, and again against the bytes actually written, because a header is a claim and only the measurement is a measurement.

> The package is 41.0 MB, which is over the 32.0 MB limit for a plugin package. A plugin is a folder of code and a manifest; if yours needs more than this, it wants a model or a dataset that belongs outside the package.

> The package says it unpacks to 200.0 MB, over the 128.0 MB limit. Refusing to unpack it.

> The package unpacks to more than 128.0 MB. Its own size headers said otherwise, which is what a zip bomb looks like. Nothing was installed.

### Structural refusals

| Refused | Message |
|---|---|
| Not a readable zip | *That file is not a readable zip archive (…). A plugin package is the plugin's folder, zipped.* |
| An empty upload | *The upload was empty. Choose the .zip file containing the plugin folder and try again.* |
| No manifest anywhere | *The package has no manifest.toml. A plugin package is the plugin's own folder, zipped — the manifest belongs either at the top of the archive or in the single folder inside it.* |
| More than one plugin folder | *The package contains more than one plugin folder ('a', 'b'). Install them one at a time — a package is one plugin.* |
| Anything the real scanner would reject | *The package was refused: &lt;the discovery message&gt;* |

Calling `POST /admin/api/plugins/install` directly, the upload is sent as **the raw zip in the request body**, not as a multipart form — a multipart request to that endpoint is answered with `415` and an explanation, because the endpoint reads `.zip` bytes and does not parse multipart form data. The Plugins screen's own form is the one exception to that: a browser file input can only post multipart, so its handler unwraps the envelope and hands the same installer the bytes in the shape it expects — see [Why a zip](#why-a-zip).

## Replace semantics and config preservation

By default, a name that is already installed is refused:

> A plugin named 'weather' is already installed. Choose 'replace the installed plugin' to upgrade it — its config.toml settings are kept — or uninstall it first.

Silently replacing a working plugin with an upload is not a decision the core gets to make.

With `replace` chosen:

- **The installed `config.toml` is copied over the one in the package.** The settings an operator typed outrank whatever the package ships (spec §7 — an upgrade must never discard appdata content). The response reports `config_preserved`.
- The existing folder is moved aside first, and **put back if the move fails** — a failed upgrade must not also be an uninstall.

There is one collision `replace` cannot help with: a plugin of the same name already installed under the *other* transport directory.

> A plugin named 'x' is already installed under plugins-http.d/, with a different transport. Remove that one first — two plugins may not share a name, and the core would refuse to load either of them.

Discovery refuses a name declared in both directories and loads *neither*, so installing into that state would break the plugin that already works.

**A fresh install clears any stale switched-off entry** for that name, so a plugin is not born disabled because something of that name was turned off months ago.

## Uninstall

Explicit, and it says what it will delete before you confirm — including whether the plugin's `config.toml` goes with it (`config_removed`) and how many files (`files_removed`).

The folder is located in either plugins directory, then:

- **A symlinked plugin folder is refused outright**, never followed: *"Removing it would delete whatever it points at, so the core refuses."*
- The resolved path must be inside appdata **and** be a direct child of the plugins directory itself: *"The core only removes plugin folders it put there."*
- `config.toml` lives inside the folder, so it always goes with it. That is why the response says whether there was one.
- Afterwards the name is removed from the switched-off list.

Unlike API-key revocation, uninstall answers `404` for something that is not installed. It destroys data, and an operator who deletes the wrong name has to find out rather than be told "done" by an interface that quietly did nothing.

## Bundling runbooks

A plugin that declares `[runbooks] supported = true` may ship one or more [runbooks](Runbooks) inside its own package: `runbooks/*.yaml`, each optionally paired with its own `prompts/*.md` for the model steps that need them. On install (and on every reinstall), the core copies each one into `<appdata>/runbooks/<plugin>/<id>.yaml` and marks it bundled — the same place an upload for that plugin would land, so the two are indistinguishable on the Runbooks screen except for their `Source` column.

**A bad bundled runbook is a warning, not a refusal.** It is validated the same way an upload is, but a mistake in a prompt file must not be the reason the whole plugin fails to install: the runbook is copied in and listed with its problems on the Runbooks screen, and the plugin installs and runs exactly as it would without it.

## Signing

**Deliberately out of scope for now** (ADR-0013). A signature is only worth anything with a key distribution story, and there is no plugin ecosystem to sign. Revisit if plugins ever come from anywhere but the operator's own hand.

Which means: the hardening above is not optional detail; it is the reason accepting uploads is acceptable at all.

## See also

[Plugin Contract](Plugin-Contract) · [Manifest](Plugin-Manifest) · [Lifecycle](Plugin-Lifecycle) · [Configuration](Plugin-Configuration) · [Runbooks](Runbooks)

Walked through end to end in [Scenario: Installing a Plugin](Scenario-Installing-A-Plugin).
