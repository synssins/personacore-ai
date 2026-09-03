# The PersonaCore voice pack format

**Version 1** · **Status:** proposed, nothing implements it yet ·
**Decided by:** ADR-0021, ADR-0002

A voice pack is a zip file containing one voice: its model, its configuration,
its pronunciation and its licence. Install it through the admin UI and it
appears in the voice dropdown as `VOICENAME (Engine)`, alongside every other
voice from every other engine.

This document is the contract. Package against it and your voice works in any
PersonaCore installation that has the matching engine.

---

## The rule that shapes everything else

**A voice pack contains data. It never contains code.**

Model weights, configuration, text, audio. That is the whole list. The core
reads a pack; it never executes anything from one.

**Specifically refused, and the pack is rejected naming the file:**

| Refused | Why |
|---|---|
| `.pkl`, `.pickle`, `.joblib`, `.npy` with pickled objects | **Loading a pickle executes arbitrary code.** A format that allows them makes installing a voice equivalent to running a stranger's program. |
| `.py`, `.sh`, `.exe`, `.dll`, `.so`, or any executable bit | A voice does not need to run |
| Nested archives | Nothing legitimate needs them, and they hide the above |
| Absolute paths, or any path containing `..` | Zip-slip: writing outside the install directory |
| Symlinks | The same escape by another route |

If your voice needs a pickle today — several existing VITS and Piper toolchains
produce them — **convert it once, at packaging time**, to JSON. That conversion
is yours to do, not the core's to work around, and it is a one-off.

**Also enforced:** a total uncompressed size cap, an entry-count cap, and a
refusal to overwrite anything outside the pack's own directory. Nothing in the
archive is run, imported or opened as code while it is being unpacked.

---

## Layout

Inside the zip, either at the top level or inside a single folder:

```
voice.toml            required — the metadata this document specifies
model/                required — whatever your engine loads
  <your model files>
pronunciation.json    optional — lexicon overrides
LICENSE               required — the licence the voice is under
ATTRIBUTION.md        required when the licence needs attribution
sample.wav            optional — a few seconds, for previewing in the UI
```

Installed, it lands at `appdata/voices/<engine>/<voice-id>/`, which is where
ADR-0002 says voices live.

---

## `voice.toml`

```toml
# The format this pack is written against. The core refuses a version it
# does not understand rather than guessing.
format_version = 1

[voice]
id           = "glados"        # lowercase, [a-z0-9_-], unique per engine.
                               # This is a filesystem name and a stable
                               # reference. It never changes.
name         = "GLaDOS"        # what a person sees in the dropdown
description  = "Flat, precise, faintly disappointed."
language     = "en-US"         # BCP 47
version      = "1.0.0"         # semver, this pack's version

[engine]
id            = "vits-onnx"    # which engine plugin can speak this voice.
                               # Engine ids never carry a voice name.
min_version   = "1.0.0"        # oldest engine release this pack works with

[audio]
sample_rate   = 22050          # Hz, as the model actually produces
channels      = 1

[model]
# Paths are relative to the pack root and must stay inside it.
# The key names are the engine's business; the core only checks the paths
# resolve inside the pack and that the files are not executable.
weights       = "model/glados.onnx"
config        = "model/glados.json"

[synthesis]
# Defaults an operator may override in the UI. Ranges are the engine's to
# validate; the core passes them through.
length_scale  = 1.0            # speed — higher is slower
noise_scale   = 0.667          # expressiveness
noise_w       = 0.8            # pitch variance

[pacing]
# How this voice is paced (PC-342). The CORE splits the text at its stops and
# puts the silence between the pieces; an engine never does either, and never
# reads punctuation as timing (ADR-0029).
sentence_gap_ms   = 450        # silence after a full stop. Empty is 450
clause_gap_ms     = ""         # at any of clause_marks below.
                               # Empty is half the sentence gap
paragraph_gap_ms  = ""         # between paragraphs. Empty is double it
sentence_marks    = ""         # characters that end a sentence. Empty is .!?…
clause_marks      = ""         # characters that end a clause. Empty is —–;,
# 0 is legal in all three gaps and means no gap -- the sentences run together,
# which is what a voice did before this table existed and may be what a fast
# voice wants. Range is 0-5000 ms: refused at save, clamped with a note in the
# log when it is hand-edited past the ends.
#
# The two mark lists REPLACE the defaults rather than adding to them, which is
# how one box adds, removes and replaces: `sentence_marks = ".!?…:"` adds the
# colon, `clause_marks = "—–;"` removes the comma break. Empty means the usual
# list, NOT "never break" -- that is a gap of 0, and the two are different
# settings. A blank line is always a paragraph break and is not a character
# anybody can name. Letters, digits, spaces and repeats are refused at save and
# dropped with a note when hand-edited in; if nothing usable is left the voice
# falls back to the default list. Sixteen characters per box.

[licence]
spdx          = "CC-BY-4.0"    # SPDX identifier where one exists
file          = "LICENSE"
attribution   = "ATTRIBUTION.md"
source        = "https://example.org/where-this-voice-came-from"

[author]
name          = "Someone"
contact       = "https://example.org/contact"   # optional
```

### Required, and why each is required

- **`format_version`** — so a pack from the future is refused clearly rather
  than half-read.
- **`voice.id`** — a filesystem name and the thing a persona's `voice_name`
  refers to. Changing it breaks every reference, so it is fixed at publication.
- **`engine.id` + `min_version`** — a pack installed without its engine must say
  *"this voice needs the vits-onnx engine, which is not installed"*, not fail
  obscurely at first speech.
- **`audio.sample_rate`** — the pipeline has to know before it plays anything.
  Getting this wrong is the difference between a voice and a chipmunk.
- **`licence`** — a voice model is someone's work. A pack without a licence is
  not installable, because an operator sharing it needs to know what they may
  do with it.

---

## `pronunciation.json`

Optional. Overrides how specific words are spoken, applied before the engine's
own prediction.

```json
{
  "format_version": 1,
  "notation": "ipa",
  "entries": {
    "glados": "ɡlˈɑːdɑːs",
    "don't": "dˈoʊnt"
  }
}
```

**This exists because pronunciation belongs in the pack, not in code.** The
GLaDOS voice currently gets its pronunciation from a hardcoded table inside the
old project's phonemizer. A voice that needs someone to patch the core to sound
right is not a portable voice.

`notation` names the alphabet the values are in — `ipa`, or an engine-specific
one your engine documents. The core does not interpret these; it hands them to
the engine, which is the only thing that knows what they mean.

---

## Streaming

Nothing here says anything about streaming, deliberately.

Whether speech begins before the reply finishes is a **core setting** the
operator controls (PC-256), and whether it *can* is a **capability the engine
declares** (ADR-0021). A voice pack is data — it has no say in either, and a
pack that tried to would be describing its engine's behaviour rather than its
own.

---

## What the core does with a pack

1. **Refuses** anything in the list above, naming the file.
2. **Reads** `voice.toml`. A missing or unparseable one is a plain-English
   refusal naming what is wrong.
3. **Checks** the engine exists and satisfies `min_version`. If not, it installs
   anyway and shows the voice as unavailable with the reason — an operator who
   installs the voice before the engine should not lose the file.
4. **Unpacks** into `appdata/voices/<engine>/<voice-id>/`.
5. **Lists** it in the one voice field as `NAME (engine)`.
6. **Paces** it. Before anything is synthesised the core splits the text at its
   paragraph, sentence and clause boundaries, asks the engine for each piece on
   its own, and joins the audio with `[pacing]`'s silence between the pieces.
   **Where those boundaries are is this pack's own business**: `sentence_marks`
   and `clause_marks` are characters carried in the file and edited on the voice
   page, so a voice tuned by ear stays tuned when the pack is handed on. They
   are escaped one character at a time on their way into the splitter — a mark
   list that is a regular expression is a list of characters, never a pattern —
   and a set with nothing usable in it costs this voice the pacing, never the
   voice and never the reply.

The core never loads the model. That is the engine's job, in the engine's
container. It is also the core that splits and pads: an engine is handed one
piece of text and returns audio for it, because every engine has the pacing
problem and one that solved it would solve it again, differently, for the next
one (ADR-0029).

---

## Not settled yet

- **Multi-speaker models**, where one file holds many voices. Piper has these.
  Probably one pack listing several `voice.id`s, but nobody has needed it yet
  and guessing would produce the wrong shape.
- **Preview generation** — whether `sample.wav` is required so the UI can always
  preview, or whether the UI asks the engine to speak a line on demand.
- **Signing.** A pack is data and cannot execute, which removes the sharpest
  risk, but "this is really the voice its author published" is unanswered.
