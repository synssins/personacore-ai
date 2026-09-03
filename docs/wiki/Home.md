# PersonaCore

A self-hosted, containerised, security-first agentic assistant for a household.
Small core, everything else a plugin, contracts as the product. CPU-only,
permanently.

**New here?** Read [Overview](Overview), then [Glossary](Glossary), then the
scenario closest to what you are trying to do.

---

## Start here

| Page | For |
|---|---|
| [Overview](Overview) | What this is, the problem it solves, how the pieces fit |
| [Glossary](Glossary) | Every term of art, in plain language |
| [Installation-And-Upgrades](Installation-And-Upgrades) | Running it, upgrading it, rolling back, backing up |
| [Scenario-First-Run](Scenario-First-Run) | The first ten minutes |

## Doing something specific

**Operating**

- [Scenario-Connecting-An-LLM](Scenario-Connecting-An-LLM)
- [Scenario-Installing-A-Plugin](Scenario-Installing-A-Plugin)
- [Scenario-Third-Party-Clients](Scenario-Third-Party-Clients) — LobeChat, Open WebUI, Home Assistant
- [Scenario-Multi-User-And-Anonymous](Scenario-Multi-User-And-Anonymous)
- [Scenario-Putting-Authelia-In-Front](Scenario-Putting-Authelia-In-Front)
- [Scenario-Retention-And-Privacy](Scenario-Retention-And-Privacy)
- [Scenario-Debugging-A-Plugin](Scenario-Debugging-A-Plugin) — start here when something does nothing

**Writing a plugin**

- [Scenario-Writing-A-Plugin](Scenario-Writing-A-Plugin) — the short path
- [Plugin-Walkthrough-Template](Plugin-Walkthrough-Template) — from the skeleton
- [Plugin-Walkthrough-Weather](Plugin-Walkthrough-Weather) — a real one, annotated
- [Scenario-Plugin-With-A-Secret](Scenario-Plugin-With-A-Secret)
- [Scenario-Plugin-With-Network-Access](Scenario-Plugin-With-Network-Access)
- [Scenario-Confirm-And-Restricted-Tools](Scenario-Confirm-And-Restricted-Tools)
- [Scenario-Plugin-Publishing-Events](Scenario-Plugin-Publishing-Events)

## Reference

**The plugin contract**

[Plugin-Contract](Plugin-Contract) ·
[Plugin-Manifest](Plugin-Manifest) ·
[Plugin-Tools](Plugin-Tools) ·
[Plugin-Configuration](Plugin-Configuration) ·
[Plugin-Lifecycle](Plugin-Lifecycle) ·
[Plugin-Runtime-Environment](Plugin-Runtime-Environment) ·
[Plugin-Packaging](Plugin-Packaging) ·
[Plugin-Events](Plugin-Events) ·
[Plugin-HTTP-Transport](Plugin-HTTP-Transport) ·
[Plugin-Testing](Plugin-Testing) ·
[Plugin-Contract-Versioning](Plugin-Contract-Versioning)

**The core**

[Core-Settings](Core-Settings) ·
[Environment-Variables](Environment-Variables) ·
[Appdata-Layout](Appdata-Layout) ·
[LLM-Roles](LLM-Roles) ·
[Personas](Personas) ·
[Event-Bus](Event-Bus) ·
[Health-And-Diagnostics](Health-And-Diagnostics)

**Security and privacy**

[Security-Model](Security-Model) ·
[Policy-Profiles](Policy-Profiles) ·
[Risk-Levels](Risk-Levels) ·
[Audit-And-Trace](Audit-And-Trace)

**APIs**

[OpenAI-Compatible-API](OpenAI-Compatible-API) ·
[Admin-API](Admin-API)

**Design**

[Screen-And-Field-Catalogue](Screen-And-Field-Catalogue) — the UI inventory that
feeds the design work

[Design-Tokens](Design-Tokens) — the CSS custom properties a plugin view or a
theme consumes

---

## What is not built yet

This wiki documents the system as it is, and marks anything decided-but-unbuilt
where it comes up. The honest summary:

- **Memory and mood** are P1 and have not started.
- **Voice is substantially built.** The core owns it (ADR-0029, superseding
  ADR-0021): every built-in engine (`espeak`, `vits_onnx`) has its own on/off
  switch, a voice installs as an uploaded zip, switching a persona's engine off
  warns and leaves it replying in text rather than breaking, and speech is
  paced by punctuation. A persona's `voice.engine`/`voice.name` fields are read
  now, not just carried. Still not started: the wake-word/microphone pipeline,
  streaming speech, a remote engine reached over the network, and generation
  statistics. See [Personas](Personas).
- **`confirm` and `restricted` tools are always refused.** The risk gate is real
  and fails closed, but nothing can ask a human for approval yet.
- **Retention runs.** A background task purges on the configured window at
  startup and every six hours; a saved change applies to the next pass without
  a restart.
- **`permissions.network` and `permissions.paths` are declared, not enforced**
  for stdio plugins, and no events reach plugins. See ADR-0012.

The repository README carries the current gap list. Where a page describes
something unbuilt, it says so at that point rather than in a footnote.
