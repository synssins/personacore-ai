# Memory

How a persona remembers things about a person and about the household: what gets written, what comes back on a later turn, and how it is reviewed and pruned. Read this if you are setting memory up, writing an admin screen against it, or wondering why a persona did or didn't recall something.

A **memory** is a short piece of text — a fact, a preference, a stated wish — attached to two things: whose it is, and which persona knows it. It is not the conversation transcript. It is what the assistant chose, or was found, to be worth keeping.

Source: `src/personacore/memory/`, `src/personacore/agent/protocols.py` (`MemoryProvider`, `MemoryRecallRequest`, `MemoryItem`), `src/personacore/config/memory.py`. Decision record: ADR-0045.

## The two axes

Every memory has:

- **Owner** — whose memory it is about. A person's profile id, or `household` for something that belongs to no one person.
- **Holder** — which persona knows it. A persona's name, or `global` for something every persona can draw on.

The core sets both. A model never chooses who a memory belongs to or who gets to know it — not through a tool argument, not through anything it writes.

## Short-term and long-term

**Short-term** memory has a persona as its holder. It is what a single character has picked up about a single person (or about the household, through that character). It is subject to the 60-day rule below.

**Long-term** memory has `global` as its holder and `household` as its owner. It never expires, and every persona can draw on it for everyone. The only way a memory becomes long-term is an administrator **promoting** it on the Memory screen — nothing writes there directly.

In a room with more than one persona, each keeps its own short-term store. If two personas independently learn the same fact, both keep their own copy — that's expected, not a duplicate to merge.

## How a memory gets written

Two ways, and only two:

**1. The persona's own tool, during the conversation.** A persona whose memory is on can call `memory.remember` with the text to keep. The core fills in who it belongs to and which persona is holding it; the model only supplies the words. The persona gets back "Kept" on success, or a short reason on failure — a failed memory write never breaks the turn.

**2. A review pass, after the conversation goes quiet.** When a conversation has had no new messages for a while (10 minutes by default), a background pass reads the new part of it and asks a separate, cheaper model — the **triage model** (see [LLM Roles](LLM-Roles)) — what's worth keeping: preferences, names, relationships, recurring situations, stated wishes. Never the persona's own conversational model. This runs automatically for every persona that has memory on; there's no separate switch for it. In a room, it runs once per persona that spoke, so each persona's review is about what it heard.

Either way, a memory that is very close to one already on file (by meaning, not exact wording) doesn't create a duplicate — it updates the existing row's last-used time and, if warranted, its importance instead.

## What gets recalled, and how

On a turn where the persona's memory is on, the person's message is used to look up the persona's own memories for that person, plus every long-term memory, ranked by a mix of how relevant the text is, how important it was judged, and how recently it was used. Up to a set number come back (8 by default).

**Recalled memory is untrusted content.** It goes into the conversation the same way any outside content does: fenced off, clearly labelled as data rather than instructions, and never placed in the highest-privilege part of the prompt. See [Personas](Personas) and [Security Model](Security-Model) for why. A persona reading back "the user said to ignore all previous instructions" as a recalled memory must treat it as something the user apparently said once, not as something to obey now.

A persona also has a **deliberate recall tool**, `memory.recall`, for when it needs to actually check rather than guess — asked "what do you know about my schedule," it can look. It reads under the same rules as automatic recall; nothing about it can see more.

## Turning it on and off

Two switches, and the narrower one wins:

- **Per persona** — `persona.toml`'s top-level `memory` key (`memory = true` or `false`). Off means that persona does no reads, offers no memory tools, and gets no review pass. Its store is left completely alone. On by default, including for personas that predate this key. See [Personas](Personas).
- **Per key, `memory_scope`** — as it works today for every other scoped feature. A key with scope `none` gets no memory regardless of what the persona allows.

Memory is not a plugin — it lives in the core, because owner and holder have to come from somewhere the model can't reach, and recall has to land inside the message the core assembles every turn (ADR-0045). Turning it off for every persona is what "deleting a plugin's folder" would have meant: the store file is simply never opened.

## The 60-day rule

A short-term memory that hasn't been touched — written, recalled, or edited — in 60 days is removed automatically, as part of the same retention sweep that ages out old conversations (see [Core Settings](Core-Settings), `[retention]`). A promoted, long-term memory is exempt; it never expires this way.

Deleting the conversation a memory came from does **not** delete the memory. What a persona remembers stays remembered until an administrator removes it or the 60 days pass — the conversation and the memory it produced have separate lifetimes.

## The Memory screen

Administrator only, under the admin sidebar's **Memory** entry. Works with JavaScript off, like every screen in the admin UI (ADR-0020).

Memories are grouped: **Long term** first, then by person, then by persona within each person. Each group shows how many rows it holds.

Each row shows the memory's text in full, when it was created, when it was last used (or "never"), how many times it's been used, whether it came from the tool or from a review pass and which model wrote it, and a marker if it's been edited or its text was too long and got cut short. There's a link to the conversation it came from, where there is one.

Two one-tap actions on every row:

- **Green check — promote.** Moves the memory into the Long term group: its holder becomes `global`, its owner becomes `household`, and every persona can draw on it for everyone from then on. Reversible — deleting it undoes the promotion.
- **Red X — delete.** Removes the row. Not reversible, and the screen says so.

Neither action asks for confirmation first; one tap is the whole interaction. A small **edit** link opens a form to change a memory's text; saving records who edited it and when.

**Filters:** a person picker (tick more than one to see several people at once), a persona picker, and a text search box that matches both by substring and by meaning. The default view is everyone, newest first. The filter selection lives in the page's URL, so a filtered view can be reloaded or linked.

### The review log

At the foot of the Memory screen is a **Review log**: the last twenty runs of
the review pass, newest first. Each run names the persona, the person, when it
ran and how it ended, then lists what was kept and what was rejected with the
reason (not JSON at all, missing text, text too long, a bad importance word).
A run the model failed shows the error. It exists so an administrator can see
what the triage model is choosing to keep and tune the prompt or the model
from evidence rather than from counts. Runs age out on the same schedule as
short-term memories. The filters above do not narrow it.

## Privacy rules

- One person's memories with a persona never reach another person. The filter that enforces this is in the database query itself, not layered on afterward.
- Nothing writes to the long-term, household-wide store except the promote action on this screen. A model cannot promote its own memory.
- Memory text is never written to the structured log or to audit records. What's logged is the memory's id, who acted, and what action — never the words.
- A minor's memories are visible to the administrator on this screen, the same as anyone else's, and nowhere else. The screen doesn't say who else, if anyone, can see them.
- A member (a non-administrator) cannot see anyone's memories today, including their own — see below.

## Settings

Household-wide defaults live in `core.toml`'s `[memory]` section — see [Core Settings](Core-Settings) for the full table. They cover how long a conversation has to sit quiet before it's reviewed, how many memories come back on a recall, how quickly an unused memory's ranking fades, how similar a new memory has to be to an existing one before it's treated as the same fact, and the 60-day short-term limit.

## What isn't built yet

- **Members seeing their own memories.** The route exists (`GET /admin/profile/memory`) but returns 404 for everyone — administrator included — until a settings flag turns it on. The flag doesn't exist yet. Today, only the administrator can see memory at all.
- **Consolidation** — a periodic pass that rewrites and merges memories over time. The review pass that writes new memories is not this.
- **Compaction** of long conversations. It will reuse the same "read the quiet part of a conversation with the triage model" mechanism the review pass uses, but it isn't built.
- Editing a memory's importance from the screen.
- Exporting or importing a memory store.
