# Design Tokens

The 41 CSS custom properties the admin surface is built from, what each one is *for*, and the naming rule they follow. Read this if you are writing a plugin view that should look like the rest of PersonaCore, or a theme that changes how all of it looks at once.

A plugin does not get its views rendered by the core. It renders its own markup, inside the core's page, and inherits the core's tokens. **That inheritance is the whole design system** — there is no component library to import and no build step to run. You write ordinary HTML, you reach for `var(--color-surface)` instead of a hex code, and your view matches.

Source: `src/personacore/admin/static/nocturne.css`. Guarded by `tests/server/test_design_tokens.py`.

## The rule

    --<category>-<role>[-<scale or variant>]

**Category** is the kind of value: `color`, `space`, `radius`, `shadow`, `font`. It is never omitted, so a name always says what sort of thing it is before it says what it is for.

**Role** is what the value means, not what it looks like — `surface`, `danger`, `divider`. A role name survives a theme; `--color-purple` would not.

**Scale or variant** is optional and only appears where a role has more than one step: a number for a ramp (`--color-accent-800`), a word for a one-off variant (`--color-danger-text`).

Two consequences worth stating plainly:

- **A bare role is the semantic token; a numbered one is a ramp rung.** `--color-accent` is "the interactive colour". `--color-accent-500` is a specific rung of the purple ramp. They are similar values and **they are not the same value** — see the warning under [Accent](#accent-ramp).
- **Component-local properties are named after their component** and are not part of this contract. `--banner-tone` is set by `.banner.ok` and read by `.banner`. Do not consume it and do not set it; a token with no category prefix is private by construction.

## Colour — the five roles

These five are what you should reach for first. Everything else is a ramp you drop to when a role does not exist for what you are doing.

| Token | Value | What it is for |
|---|---|---|
| `--color-bg` | `#161826` | The page's ground — the darkest surface, the thing everything else sits **on**. Also the inside of a code block, which is recessed rather than raised. |
| `--color-surface` | `#232532` | Anything lifted **off** the page: a card, a text input, a dialog, a row in a list of results. Lighter than `--color-bg`. |
| `--color-text` | `#e9e9ed` | Body text. Also the base every muted shade is mixed from — `color-mix(in srgb, var(--color-text) 55%, transparent)` is the surface's standard secondary text, and there is deliberately no token for it. |
| `--color-accent` | `#9184d9` | The interactive colour: links, the focus ring, the primary button, the text caret, the current item in the nav. If a thing responds to a click, this is how it says so. |
| `--color-divider` | `#e9e9ed` at 16% | A hairline between two things — a border, a rule, the fading edge of a `.row`. Never a fill. |

**`--color-bg` or `--color-surface`?** Ask whether the element is raised or recessed relative to what contains it. A card on the page is `surface`. A code block inside a card is `bg`. Getting this backwards is the most common way a plugin view looks subtly wrong while every individual colour is correct.

## Colour — the ramps

Nine rungs each, `100` lightest through `900` darkest. **The direction is lightness, not prominence**, and it does not flip on a dark surface: `--color-neutral-100` is nearly white here, the same as it would be in a light theme.

### Neutral ramp

| Token | Value | Used for |
|---|---|---|
| `--color-neutral-100` | `#f3f5fe` | Text on a neutral tag. |
| `--color-neutral-200` | `#e4e7f5` | — |
| `--color-neutral-300` | `#cfd3e5` | Secondary detail text that is still meant to be read — a health check's detail line, an access key's summary. |
| `--color-neutral-400` | `#b2b6ca` | — |
| `--color-neutral-500` | `#9397ab` | The neutral banner's tone; a `lg` shadow's ring. |
| `--color-neutral-600` | `#75798c` | An "off" status dot; the dashed border of a field still at its default. |
| `--color-neutral-700` | `#595d6c` | The dashed border of an empty state; an `md` shadow's ring. |
| `--color-neutral-800` | `#3f424d` | A neutral tag's background; an `sm` shadow's ring. |
| `--color-neutral-900` | `#292b31` | The unchecked track of a switch; a dialog's backdrop. |

### Accent ramp

| Token | Value | Used for |
|---|---|---|
| `--color-accent-100` | `#f5f4ff` | Text on an accent tag; a switch's knob when on. |
| `--color-accent-200` | `#e7e5fe` | — |
| `--color-accent-300` | `#d2cefd` | A link on hover; an identifier shown in a code span (a persona slug, a voice name); a URL printed in a model's reply. |
| `--color-accent-400` | `#b5abfc` | — |
| `--color-accent-500` | `#968ae0` | — |
| `--color-accent-600` | `#796cbf` | — |
| `--color-accent-700` | `#5d5294` | A switch's track when on; the border of a panel holding something just created. |
| `--color-accent-800` | `#423a6a` | An accent tag's background; the border of a tool-call chip. |
| `--color-accent-900` | `#2b2741` | The background of your own message in the chat. |

> **`--color-accent` is not `--color-accent-500`.** They are `#9184d9` and `#968ae0` — close enough to look like a mistake and far enough apart to be one. The bare token is the brand's interactive colour; the ramp is a set of tints around it. Use `--color-accent` for anything interactive and reach into the ramp only for a specific tint you have chosen deliberately.

The rungs with no "used for" entry are unused by the core today. They exist so the ramp is not gap-toothed for a theme author re-mapping it. They are still safe to consume.

## Colour — status

| Token | Value | What it is for |
|---|---|---|
| `--color-ok` | `oklch(0.72 0.1 155)` | A healthy state. A green dot, the word beside it, a "saved" note. |
| `--color-warn` | `oklch(0.78 0.11 80)` | Degraded, or something that wants attention but has not failed — an engine that loaded with a caveat, a persona whose voice is missing. |
| `--color-danger` | `oklch(0.72 0.13 25)` | A failed state **and** a destructive control. A down dot, an invalid field's border, the Delete button. |
| `--color-danger-text` | `oklch(0.78 0.1 25)` | The same red, lighter, for a **sentence**. Use this whenever the red is a run of words rather than a dot, a border or a single button label. |

**Never carry meaning in the colour alone.** Every use of these in the core sits beside a word or an icon that says the same thing. A status dot is always next to a state word; an invalid field gets the word "invalid" next to its label, not only a red border. Colour is the second channel, never the first — a plugin view that fails this is a defect, not a style preference.

There is no `--color-ok-text` or `--color-warn-text`. Those two are already light enough to read as body text on this surface; the red was not, which is the only reason `--color-danger-text` exists.

## Space

Every step is a multiple of **2.8px**, and the number in the name *is* the multiplier.

| Token | Value |
|---|---|
| `--space-1` | `2.8px` |
| `--space-2` | `5.6px` |
| `--space-3` | `8.4px` |
| `--space-4` | `11.2px` |
| `--space-6` | `16.8px` |
| `--space-8` | `22.4px` |

**There is no `--space-5` or `--space-7`.** Only the six steps above exist. This is the one place the naming rule will let you write a plausible token that resolves to nothing — CSS answers an undefined custom property by silently falling back to the property's initial value, so `padding: var(--space-5)` renders as no padding at all and reports nothing. The core is protected from this by a test; your plugin is not, so check the list.

`--space-3` is the workhorse — the default gap in a row, the default margin under a paragraph. Reach for it first.

## Radius

| Token | Value | What it is for |
|---|---|---|
| `--radius-sm` | `4px` | Small or inset things: an inline code span, a code block, a trace panel, a strip inset into a card. |
| `--radius-md` | `8px` | The default. Buttons, inputs, cards, banners, list rows, nav links. |
| `--radius-lg` | `14px` | A dialog, and nothing else. |

A pill or a circle — a switch's track, a status dot — sets its own `border-radius` from its geometry rather than from a token, which is correct: those are shapes, not corners.

## Shadow

These are **rings, not drop shadows** — an inset-looking `0 0 0 1px` outline, with a real shadow added at the larger sizes. They are how an element is separated from its background on a dark surface, where an actual shadow is nearly invisible.

| Token | What it is for |
|---|---|
| `--shadow-sm` | A card that needs an edge. Ring only, no shadow. |
| `--shadow-md` | A raised card. |
| `--shadow-lg` | A dialog floating over the page. |

Apply them through the `.elev-sm` / `.elev-md` classes where you can.

## Font

| Token | What it is for |
|---|---|
| `--font-body` | Everything. A system stack — no webfont is loaded, by design. |
| `--font-mono` | Code, identifiers, trace output, anything where character alignment carries meaning. |

Both are full family stacks, not single family names, so assign them to `font-family` directly and do not append a fallback of your own.

## What is not a token

Deliberately, so you do not go looking:

- **Muted text.** Written as `color-mix(in srgb, var(--color-text) 55%, transparent)` at each site. The mix percentage varies with context (50%, 55%, 70%, 78%), and freezing one of them into a token would make the other three look wrong.
- **Font sizes.** Set per component in the stylesheet. The scale is small and does not repeat enough to earn names.
- **Transition timings and z-index.** There are almost none of either.

If you need one of these, say so rather than inventing a name — a token that only your plugin defines is not part of the design language, it is a private variable with a public-looking name.

## The guard

`tests/server/test_design_tokens.py` reads the real stylesheet, the real templates and the real scripts, and fails if any of them names a token that is not defined. It also fails when a token is defined and never used, unless it is listed as a deliberate ramp rung with a reason.

It exists because `var(--line)` shipped on two screens and was defined nowhere. Nothing broke, nothing logged, and two borders quietly became the browser's default for as long as it took somebody to put the screens side by side. An undefined token is invisible from the moment it is written, which is exactly why it needs a test rather than an eye.
