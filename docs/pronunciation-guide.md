# Fixing how a voice says a word

A speech engine guesses pronunciation from spelling. It gets names, brands and
made-up words wrong. This file is how you correct it.

You do not need to understand phonetics. You need the vowel table below and the
two stress marks.

## Where the fix goes

In the voice's own folder, beside the model:

```
appdata/voices/vits-onnx/glados/
    glados.onnx
    glados.onnx.json
    pronunciation.json      <- this file. Optional.
```

```json
{
  "notation": "ipa",
  "words": {
    "glados": "ɡlˈædɒs",
    "jordan": "dʒˈɔːdən"
  }
}
```

Keys are lowercase. Matching ignores case, so `GLaDOS`, `Glados` and `glados`
all hit the same entry.

**The voice still works without this file.** It just mispronounces things.

## The vowel table

The example word is the *sound*, not the spelling. `far` and `car` share one
vowel; that vowel is `ɑː`.

Every line below was checked by asking the engine itself, then listening to the
result. The written form is what the engine actually produces for that word.

| Symbol | Word | Written in full |
|---|---|---|
| `ɪ` | s**i**t | `sˈɪt` |
| `iː` | s**ee** | `sˈiː` |
| `ɛ` | b**e**d | `bˈɛd` |
| `æ` | b**a**d | `bˈæd` |
| `ɑː` | f**ar** | `fˈɑːɹ` |
| `ɑː` | d**o**g | `dˈɑːɡ` |
| `ɔː` | s**aw** | `sˈɔː` |
| `ʊ` | p**u**t | `pˈʊt` |
| `uː` | b**oo**t | `bˈuːt` |
| `ʌ` | c**u**p | `kˈʌp` |
| `ɜː` | b**ir**d | `bˈɜːd` |
| `ə` | probl**e**m | `pɹˈɑːbləm` |
| `ɐ` | **a** cup | `ɐ kˈʌp` |
| `eɪ` | d**ay** | `dˈeɪ` |
| `aɪ` | m**y** | `mˈaɪ` |
| `ɔɪ` | b**oy** | `bˈɔɪ` |
| `aʊ` | n**ow** | `nˈaʊ` |
| `oʊ` | g**o** | `ɡˈoʊ` |
| `ɚ` | butt**er** | `bˈʌɾɚ` |

`ː` after a vowel means **hold it longer**. Dropping it makes words sound
clipped.

### This voice is American. That changes three things.

**1. `ɑː` covers both *far* and *dog*.** American English merges them. There is a
separate British symbol `ɒ` for *dog*, and this voice barely uses it — it was
trained on American speech, so `ɒ` is unfamiliar territory and may come out
unpredictably. Prefer `ɑː`.

**2. An `r` you can hear needs `ɹ` written in.** *far* is `fˈɑːɹ`, not `fˈɑː`.
British English drops that r; American does not. Leave it out and *far* becomes
*"fah"*.

**3. `ɜː` in *bird* carries no separate `ɹ`.** The r is baked into the vowel.
Writing `bˈɜːɹd` doubles it.

### The two that trip people up

- **`æ` vs `ɑː`** — `æ` is *bad, mad, dad*. `ɑː` is *far, dog*. GLaDOS was wrong
  for years because her name used `ɑː`: "glah-dahs" instead of "glad-oss".
- **`ə` vs `ɐ`** — both are the lazy "uh". `ə` sits unstressed inside a word
  (*probl**e**m*, *vis**io**n*). `ɐ` is the word "a" standing on its own.

## Stress marks

English needs stress or it sounds robotic in the wrong way.

| Mark | Means |
|---|---|
| `ˈ` | the **strong** syllable |
| `ˌ` | a **secondary** stress, in long words |

**The mark goes immediately before the syllable it hits**, not after, and not on
the vowel.

```
ˈɡlædɒs      wrong — mark is outside the syllable
ɡlˈædɒs      right — mark sits just before the stressed part
```

`pɹˈoʊɾəkˌɑːl` — *protocol*. Strong on "pro", secondary on "col".

Every word needs at least one `ˈ`. A word with none sounds flat and hurried.

## Consonants worth knowing

Most consonants are the letter you expect. These are not:

| Symbol | Sound | Word | Written in full |
|---|---|---|---|
| `ʃ` | sh | **sh**oe | `ʃˈuː` |
| `ʒ` | zh | vi**si**on | `vˈɪʒən` |
| `tʃ` | ch | **ch**air | `tʃˈɛɹ` |
| `dʒ` | j | **j**udge | `dʒˈʌdʒ` |
| `θ` | th, hard | **th**ink | `θˈɪŋk` |
| `ð` | th, soft | **th**is | `ðˈɪs` |
| `ŋ` | ng | si**ng** | `sˈɪŋ` |
| `ɹ` | r | **r**ed — **not** plain `r` | `ɹˈɛd` |
| `ɡ` | g | **g**o — U+0261, **not** the keyboard `g` | `ɡˈoʊ` |
| `ɾ` | the fast d/t | bu**tt**er | `bˈʌɾɚ` |
| `j` | y | **y**es | `jˈɛs` |
| `ʔ` | glottal stop | uh-**oh** | |

Two of these bite:

- **`ɹ` not `r`.** Plain `r` is a rolled Spanish r.
- **`ɡ` not `g`.** They look identical in most fonts. Copy `ɡ` from this file.

## Worked example

*GLaDOS*. Say it out loud: **glad** + **oss**.

1. `ɡl` — the g and l together. Use `ɡ`, not `g`.
2. `æ` — the *bad* vowel.
3. `d`
4. `ɒ` — the *dog* vowel.
5. `s`

Stress lands on the first part, so `ˈ` goes before `æ`:

```
ɡlˈædɒs
```

Getting the vowel wrong here is what the old system did: `ɡlˈɑːdɑːs` reads as
"glah-dahs".

## If you use a symbol the voice does not know

Each voice accepts a fixed set of symbols, listed in its own `.json` config
under `phoneme_id_map`. This voice accepts 157. Anything outside that set is
**dropped silently** — the word comes out with a piece missing rather than
failing loudly.

So if a fix sounds like it half-worked, suspect a symbol from outside the set —
usually a plain `g` or `r` that should have been `ɡ` or `ɹ`.

## Check your work — never on a single word

**Always test inside a sentence. A word spoken on its own comes out wrong even
when your fix is perfect.**

This is not a warning about being careful. It is measured behaviour of this kind
of voice. Asked for one word alone it will clip the first sound off, flatten a
long vowel, or move the stress:

| Asked for | Alone, it says | In a sentence |
|---|---|---|
| about | "bout" — first sound gone | correct |
| put | "putt" | correct |
| saw | "sow" | correct |
| day | "deh" | correct |

Every one of those is the same symbols either way. Only the surroundings changed.

So a one-word test will send you chasing a fault that is not there, and — worse
— can make a genuinely broken fix sound fine. Use a carrier sentence:

```
Say <your word> again.
```

Then listen to the middle word. If it is right there, it is right.

## Where the symbols come from

This is IPA, the International Phonetic Alphabet, restricted to what English
uses. The engine phonemises with espeak-ng, so anything espeak accepts for
`en-us` works here.
