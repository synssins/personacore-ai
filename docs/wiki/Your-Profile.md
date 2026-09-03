# Your Profile

Your own settings, at `/admin/profile` — distinct from [Core Settings](Core-Settings), which is the core's configuration, and distinct from `/admin/account`, which is the admin's view of everybody else's accounts and sessions. This page is yours: it renders for whoever is signed in, on any of the three ways in (the core's own sign-in, a trusted proxy header, or the development bypass), because it keys on the signed-in identity rather than on an account record.

Source: `src/personacore/web/screens/profile.py`, `src/personacore/web/templates/profile.html`, `src/personacore/preferences/`. Decision record: ADR-0030.

## Reaching it

The gear beside your name at the bottom of the sidebar. The page header repeats who you are: "Signed in as **\<your id\>**", with "(admin)" appended if you are one.

## Audio playback — the one setting today

A single control, under "Audio playback": a switch labelled **Play replies automatically**.

A person who has never opened this page gets it **on** — a reply speaks itself for whoever asked for it, without anyone having to find a checkbox first.

Changing it and pressing **Save** stores your own choice. It stays yours even if an administrator later overrides it (below) — an override doesn't erase what you picked, it's what you go back to once the override is lifted.

## The administrator override

An administrator can set a household-wide rule from [Core Settings](Core-Settings), under "Speech playback". It is one of three states, not a toggle:

| State | Meaning |
|---|---|
| **Everyone chooses** | No rule. Each person's own switch on this page decides for them. This is the default, and it is not the same thing as "never" — it means nobody's choice is overridden. |
| **Always play** | Replies play by themselves for everybody. |
| **Never play** | Nobody's replies play by themselves. |

## What you see when it's been forced

When an administrator has set **Always play** or **Never play**, your switch on this page shows that forced state and is disabled — you can't change it here. Underneath it, a line says who is responsible and which way it's set:

> Configured by the administrator — replies play automatically.

or

> Configured by the administrator — replies do not play automatically.

The **Save** button is not shown while the setting is locked, since there is nothing here for you to save.

The lock is enforced on the server, not only by disabling the input in your browser — sending a save request while an override is in force doesn't change your stored choice either.

## Where it's stored

`<appdata>/state/personacore.db` — a separate SQLite file from `audit.db` (see [Appdata Layout](Appdata-Layout)), on purpose: `audit.db` is what the retention purge trims on a timer, and clearing the audit log must never mean losing everyone's settings.
