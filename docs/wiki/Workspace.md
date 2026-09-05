# Workspace

A scratch folder for one conversation: where a plugin's fetched files land, where the persona can save its own drafts, and how a long tool result stops eating the model's context. Read this if you are turning workspace on for a persona, writing a plugin that hands back a file, or wondering where a file a card points to actually lives.

A **workspace** is not memory. Memory is a fact worth keeping across conversations; a workspace is working material for *this* conversation only — the plugin author's guide to a longer document, the middle draft of something the persona is editing, the whole reply a tool returned rather than the cut-down piece that reached the model. See [Memory](Memory) for the other one.

Source: `src/personacore/workspaces.py`, `workspace_tools.py`, `config/workspace.py`, `agent/loop.py` (`_apply_workspace`, `_workspace_blocks`), `plugins/mcp_client.py` (`render_call_result`), `web/screens/chat_workspace.py`, `review_delete.py`, `review_workspace.py`.

## Where files live

`<appdata>/workspaces/<conversation id>/` — one folder per conversation, next to `attachments/` in the same appdata layout. It is **created on first write, never at conversation start**: a persona whose workspace is on but who never fetches or saves anything leaves no folder behind.

There is no database table for what's inside. The folder is the truth for what files exist; the conversation itself is the truth for whether the folder should still exist. That second half is what hiding, deleting and the sweep (below) all enforce.

**Files are flat.** A file inside a workspace is a bare filename — letters, numbers, `.`, `_` or `-`, up to 120 characters, and it cannot start with a dot. No folders, no `..`, no leading slash, no symlink. Anything else is refused with a plain message before it ever touches a path.

## Turning it on: the persona switch

Off by default — the opposite of [Memory](Memory), which is on by default. A persona gets a workspace only once its `persona.toml` sets `workspace = true`, ticked from the *Workspace* checkbox on the persona edit page. A turn with no persona at all, or a persona with the switch off, offers none of the three tools below and gets no manifest — it composes exactly as it did before workspaces existed.

## Pinned files

`persona.toml`'s `workspace_pins` — filename patterns (globs, comma-separated on the edit screen: `report-*.md`) whose matching files are shown to the persona **whole**, fenced, every turn, rather than only listed in the manifest. Default: none pinned. A pin that matches nothing adds nothing; a pinned file is still capped at the `tool_result_chars` setting below.

## The three tools

Offered only when the persona's workspace is on. All risk level `safe` — each one touches this one conversation's folder and nothing else.

| Tool | Arguments | Returns |
|---|---|---|
| `workspace.list_files` | — | One line per file: name, character count, when it last changed. `"The workspace is empty."` when there is nothing there yet. |
| `workspace.read_file` | `path`, `start?`, `end?` (character offsets) | The file's text. A short file comes back whole; a long one returns the first `tool_result_chars` characters plus a line naming the offset to pass as `start` for the rest. |
| `workspace.write_file` | `path`, `content`, `append?` (default false) | A short confirmation naming the file it actually saved and its size, e.g. `Wrote notes.md (2,481 chars).` |

## Never overwrite

`workspace.write_file` never replaces a file that is already there. Writing under a name already in use saves the new text under the next free version instead — `notes.md` becomes `notes.2.md`, then `notes.3.md` — and the reply says which name it used. A name with no extension gets `.2` appended the same way.

Set `append` to `true` to add text to the end of a file the persona itself wrote earlier in this conversation. A missing name is simply created, exactly like a fresh write. Appending to a file that came from a tool (a fetch, not something the persona typed) is refused — `NAME came from a tool; write your version under another name.` — so a fetched source can never be quietly edited in place; the persona always saves its changes under a name of its own.

## What the model is told each turn

When the workspace is on and holds at least one file, one fenced, untrusted block is added to the prompt after the recalled-memory block and before the user's own message — the same lowest-privilege slot memory sits in, and for the same reason: this is data the persona reads, not an instruction it follows. It lists every file, who produced it, and when:

```
Files in this conversation's workspace (read them with workspace.read_file):
notes.md — 2,481 chars — written by you — 14:02
article.md — 9,114 chars — from research.fetch — 14:03
```

Any file matching a pin is then added again, this time whole, in its own fenced block. A workspace that exists but is empty this turn adds nothing at all — a persona with an idle workspace prompts exactly as it would with none.

## The long-result rule

A plugin's answer that is too long to hand the model whole does not simply get cut anymore, if the workspace is on. Past `long_item_chars` (default 8,000 characters), a plain-text tool result is saved to the workspace as a file — `<plugin>.<tool>.txt` — and the model sees only the first 1,000 characters plus a line saying where the rest went: `Saved to workspace: research.fetch.txt (9,114 chars, 1,402 words)`. With the workspace off, the older behaviour stands: the result is simply cut at the `tool_result_chars` cap and marked as truncated.

A tool that hands back an actual file (see [Plugin Tools](Plugin-Tools) — "Returning a file") follows the same save-and-say pattern, one line per file. With no workspace, each file the tool tried to hand over gets its own line instead: `File NAME was not kept: this persona has no workspace.` The persona is never shown a file's contents it cannot also keep.

## File cards and download

A file a turn's tool calls left behind — fetched or written — appears as a card under the reply, labelled `Workspace · Markdown` (or `JSON`, or `Text`, by extension). It is a link, nothing more: no in-page editor, no preview, no rename.

The owner downloads it from `GET /admin/chat/workspace/{conversation id}/{name}`; an administrator reviewing somebody else's conversation reaches the same file from its own route on the review screen. Both check ownership first and answer the same not-found message either for a file that is genuinely gone or one that was never that person's — telling the two apart from outside is exactly the kind of thing this is built not to leak.

## What happens on hide

A person hiding their own conversation (an ordinary, reversible delete from the chat screen) takes the workspace with it **immediately** — the whole folder is removed, not just marked. There is no un-hide today, but if one is ever built, it will bring back the words and not the files: they are already gone.

## What happens on an administrator's delete

The review screen's **Delete** — on a row or on an open conversation — is not a hide. It removes the conversation, its messages, its attachments and its workspace, in that order, and writes one audit record naming the conversation id and how many files went with it. This is admin-only and permanent; the confirmation page says so before it runs.

## The sweep

A background pass — the same one that ages out old conversations and attachments — removes any folder under `workspaces/` whose name is not a real, currently visible conversation. It runs at startup and every six hours after. This is cleanup for the rare case a crash left a folder behind after its conversation was hidden or deleted; ordinary hides and deletes already remove their own folder on the spot. Counted and logged, never a request anyone waits on.

## Settings

Household-wide, in `core.toml`'s `[workspace]` section — see [Core Settings](Core-Settings) for the full table with ranges. The four knobs:

| Key | Default | What it does |
|---|---|---|
| `tool_result_chars` | 32,000 | The most characters of one tool result, or one file read, the model receives in a turn before it is cut. |
| `long_item_chars` | 8,000 | With a workspace on, a plain-text tool result longer than this is saved as a file instead of being cut — see "The long-result rule" above. |
| `max_file_bytes` | 2,000,000 | The largest a single workspace file may grow. A write past this is refused, naming the limit. |
| `max_workspace_bytes` | 50,000,000 | The largest one conversation's whole workspace folder may grow. A write that would push the folder over this is refused the same way. |

The workspace root itself is not a setting — it is fixed at `<appdata>/workspaces`, the same way every other appdata folder is.

## See also

[Memory](Memory) · [Personas](Personas) · [Plugin Tools](Plugin-Tools) — "Returning a file" · [Core Settings](Core-Settings) · [Security Model](Security-Model)
