# Bundled plugins

Source, not runtime. These folders are **copied into** `/appdata/plugins/<name>/`
and loaded from there; appdata is never inside the repo or an image (ADR-0008).

| Folder | What it is |
|---|---|
| `weather/` | The reference plugin (spec §5.1). A real, working MCP server over stdio, written to be read. Start here to see the contract in use. |
| `_template/` | The skeleton to copy. Every section is commented with what to change and why. |

Writing one: **copy `_template`**, rename the folder, and make `plugin.name`
match the new folder name. The full instructions are
[`docs/plugin-author-guide.md`](../docs/plugin-author-guide.md).

The leading underscore keeps `_template` from being mistaken for an installed
plugin; it is not a valid plugin name, which is why the template declares itself
as `example-plugin` and expects to be renamed on the way in.

Tests for both live in `tests/plugins_bundled/`.
