"""Text to IPA, the way it was measured to sound right.

This is espeak-ng driven the one way that produces the right English, plus the
per-voice pronunciation substitution that sits in front of it. It is separate
from :mod:`.vits_onnx` because espeak is not that engine's: every espeak-fronted
CPU engine — Piper, VITS, Matcha, sherpa-onnx — phonemises the same way, and the
two rules below are the expensive part.

Two rules, both established by experiment and both audible when broken:

1. **espeak is given whole phrases, never single words.** Asked for the word
   "a" on its own espeak answers ``ˈeɪ`` — the *letter*, "ay" — because a lone
   letter is what it was handed. In a phrase the same word comes back as the
   unstressed article ``ɐ``. This is why :class:`Phonemiser` buffers
   consecutive ordinary text instead of walking words.

2. **Punctuation that leads a run belongs to the previous phoneme output.**
   Splitting text around a pronunciation override leaves the *next* run
   starting with punctuation. Hand espeak ``". This is a test."`` and the
   leading full stop vanishes — espeak drops a mark that has no phonemes in
   front of it — so the pause after the substituted name is lost.

   So a leading mark is appended to what has already been emitted rather than
   being sent to espeak at all. Punctuation *inside* a run is espeak's to keep,
   and it does keep it.

Both bugs were audible in practice. They are separate bugs, and a fix for one
has twice reintroduced the other, so each has its own test and the pair has a
third.

Worked example, with ``glados -> ɡlˈædɒs``::

    Hello. I am GLaDOS. This is a test of the emergency testing protocol.
    ->  həlˈoʊ. aɪˈæm ɡlˈædɒs. ðɪs ɪz ɐ tˈɛst ʌvðɪʲ ɪmˈɜːdʒənsi tˈɛstɪŋ
        pɹˈoʊɾəkˌɑːl.

``ɐ`` for "a" and the ``.`` after the name are both passing conditions.

Phoneme-to-id mapping lives here too, because the ids are what the model eats
and the map that produces them ships inside the voice's own config — never a
separate file from somewhere else.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess  # noqa: S404 — espeak-ng is a local binary, run without a shell
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

BOS = "^"
EOS = "$"
PAD = "_"
"""Piper's sentinels. They are keys in the voice's own ``phoneme_id_map``; a map
without them is not a Piper map and the voice is skipped rather than guessed at."""

WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
"""A word, for the purpose of matching a pronunciation override.

Apostrophes are kept *inside* the token on purpose: "GLaDOS's" is one word and
must not match an override for "glados", because the possessive is not the
thing the override was written for. Digits are excluded so "GLaDOS2" is likewise
its own token.
"""

LEADING_PUNCTUATION = '.,;:!?)"]}'
"""Characters peeled off the front of a run and attached to the previous output.

Deliberately short. These are the ones that close something — a clause, a quote,
a bracket — and therefore belong to what came before. An opening bracket or a
dash leads the text that follows it and is left in place for espeak, which
handles it.
"""

NO_SPACE_BEFORE = ".,;:!?"

_ESPEAK_VOICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+-]{0,31}")
"""What may be passed as ``-v``. The voice name comes out of a JSON file in a
voice folder, which is third-party data (spec §7): it becomes an argument to a
subprocess, so it is checked against a pattern rather than trusted. A name that
fails falls back to the default rather than being passed through.

It must **start** with a letter or digit, so a value like ``--stdout`` cannot be
written into a voice config. Nothing here could reach a shell — the argv list is
fixed and the text arrives on stdin — but a value that reads as an option should
never get as far as being one argument away from working.
"""

_LANG_SWITCH_RE = re.compile(r"\([^)]*\)")
"""espeak brackets a run of another language as ``(fr)…``. Those markers are not
phonemes and are removed, exactly as Piper's own phonemiser removes them."""

DEFAULT_ESPEAK_VOICE = "en-us"
ESPEAK_BINARY = "espeak-ng"


class PhonemiserUnavailable(RuntimeError):
    """espeak-ng is missing or would not run.

    Raised at synthesis time, not at import time: an engine that cannot speak
    should still list its voices and say plainly what is wrong (spec §10). The
    engine wraps this in its own ``EngineError`` before it reaches the core.
    """


def espeak_is_installed(binary: str = ESPEAK_BINARY) -> bool:
    return shutil.which(binary) is not None


def espeak_version(binary: str = ESPEAK_BINARY) -> str | None:
    """The first line of ``espeak-ng --version``, or None if it will not run."""
    if not espeak_is_installed(binary):
        return None
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (done.stdout or done.stderr or "").strip().splitlines()
    return line[0].strip() if line else None


EspeakRunner = Callable[[str, str], str]
"""``(text, voice) -> IPA``. The seam the tests replace, so the phonemisation
rules above are testable on a machine with no espeak-ng installed."""


def run_espeak(text: str, voice: str, *, binary: str = ESPEAK_BINARY, timeout: float = 20.0) -> str:
    """One espeak-ng call for one phrase, over stdin.

    The text goes in on **stdin, never as an argument**: text reaching this
    engine came from a language model and from whoever typed at it, and a string
    beginning with "-" as argv[n] is an option, not a phrase.
    """
    if not _ESPEAK_VOICE_RE.fullmatch(voice):
        _log.warning("ignoring espeak voice %r: not a plain voice name", voice)
        voice = DEFAULT_ESPEAK_VOICE
    argv = [binary, "-q", "--ipa", "-v", voice]
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv; the text arrives on stdin
            argv,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PhonemiserUnavailable(
            f"{binary} is not installed in this container, so nothing can be "
            "spoken. The image is meant to carry it; this build did not."
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PhonemiserUnavailable(f"{binary} did not answer: {exc}") from exc
    if done.returncode != 0:
        raise PhonemiserUnavailable(
            f"{binary} exited {done.returncode}: {(done.stderr or '').strip()[:200]}"
        )
    return clean_espeak_output(done.stdout)


def clean_espeak_output(raw: str) -> str:
    """espeak prints one line per clause. Join them, keeping the punctuation.

    **This joining is correct and is not the run-on bug (PC-342).** It is where
    the fault was first blamed, so it is written down here: espeak returns a
    sentence at a time and these lines are the phonemes of *one* piece of text,
    which the caller handed over as one piece. Whether a paragraph arrives here
    whole or a sentence at a time is core's decision, made in
    :mod:`personacore.voice.pacing` before this module is ever reached — an
    engine never splits and never inserts silence (ADR-0029). Putting a pause
    in here would be that decision made twice, in the one place that cannot
    know what the other pieces are.
    """
    text = _LANG_SWITCH_RE.sub("", raw)
    return tidy(" ".join(part.strip() for part in text.splitlines() if part.strip()))


def tidy(phonemes: str) -> str:
    """Collapse the seams: no double spaces, no space before ``.,;:!?``."""
    out = re.sub(r"\s+", " ", phonemes).strip()
    return re.sub(r" +([" + re.escape(NO_SPACE_BEFORE) + r"])", r"\1", out)


def load_overrides(raw: object) -> dict[str, str]:
    """Read a ``pronunciation.json`` body into a lookup, or nothing.

    Two spellings are accepted because two documents describe this file: the
    pack format (``docs/voice-pack-format.md``) writes the table as ``entries``,
    and the shape agreed for this engine writes it as ``words``. They mean the
    same thing, so both are read rather than one operator's file being refused
    over a key name.

    Anything malformed yields an empty lookup and a log line. A voice with a
    broken pronunciation file still speaks — in espeak's own pronunciation,
    which is the thing the file was overriding.
    """
    if not isinstance(raw, Mapping):
        _log.warning("pronunciation file ignored: it is not a JSON object")
        return {}
    notation = raw.get("notation", "ipa")
    if not isinstance(notation, str) or notation.lower() != "ipa":
        _log.warning(
            "pronunciation file ignored: notation is %r, and this engine reads "
            "only 'ipa' — the alphabet espeak-ng emits and the voice's own "
            "phoneme_id_map is keyed by",
            notation,
        )
        return {}
    table = raw.get("words")
    if table is None:
        table = raw.get("entries")
    if not isinstance(table, Mapping):
        _log.warning("pronunciation file ignored: no 'words' (or 'entries') object in it")
        return {}
    out: dict[str, str] = {}
    for word, ipa in table.items():
        if not isinstance(word, str) or not isinstance(ipa, str) or not word.strip():
            _log.warning("pronunciation entry ignored: %r is not text -> text", word)
            continue
        out[word.strip().casefold()] = ipa.strip()
    return out


@dataclass(frozen=True)
class Phonemiser:
    """Turns text into an IPA string for one espeak voice.

    ``overrides`` is per-voice: the word a voice says its own way travels in the
    voice folder, not in this code.
    """

    espeak_voice: str = DEFAULT_ESPEAK_VOICE
    overrides: Mapping[str, str] = field(default_factory=dict)
    runner: EspeakRunner = run_espeak

    def phonemise(self, text: str) -> str:
        """Text -> one IPA string, punctuation and all.

        Consecutive ordinary text accumulates into a buffer and goes to espeak
        **as one phrase**. The buffer is flushed when an override word is
        reached. Punctuation that leads a run is attached to what has already
        been emitted instead of being sent to espeak, where it would be dropped.
        """
        text = text.strip()
        if not text:
            return ""
        overrides = dict(self.overrides or {})
        if not overrides:
            return tidy(self._say(text))

        out: list[str] = []
        buffer_start = 0
        for match in WORD_RE.finditer(text):
            ipa = overrides.get(match.group(0).casefold())
            if ipa is None:
                continue
            self._emit_run(out, text[buffer_start : match.start()])
            if ipa:
                out.append(ipa)
            buffer_start = match.end()
        self._emit_run(out, text[buffer_start:])
        return tidy(" ".join(part for part in out if part))

    def _emit_run(self, out: list[str], run: str) -> None:
        """Emit one stretch of ordinary text, peeling its leading punctuation.

        The peeled characters are appended to ``out[-1]`` with no separator, so
        the full stop lands against the phonemes of the word before it — which
        is where the pause comes from, and where espeak would have put it had it
        not been handed the mark with nothing in front of it.
        """
        run = run.strip()
        while run and run[0] in LEADING_PUNCTUATION:
            if out:
                out[-1] = out[-1] + run[0]
            run = run[1:].strip()
        if run:
            out.append(self._say(run))

    def _say(self, phrase: str) -> str:
        return self.runner(phrase, self.espeak_voice)


def phonemes_to_ids(phonemes: str, id_map: Mapping[str, Sequence[int]]) -> list[int]:
    """IPA string -> model input ids, using the voice's own map.

    The string is NFD-normalised first, so a combining mark that arrived as part
    of a precomposed character becomes its own entry — Piper's maps key those
    marks separately, and a composed character would simply not be found.

    A phoneme the map does not know is skipped with a log line rather than
    raising. The map is the model's vocabulary: a symbol outside it has no id
    that means anything, and refusing the whole sentence over one stray
    character would trade a slightly wrong reading for silence.
    """
    ids: list[int] = []
    ids.extend(id_map[BOS])
    ids.extend(id_map[PAD])
    missing: list[str] = []
    for phoneme in unicodedata.normalize("NFD", phonemes):
        try:
            mapped = id_map[phoneme]
        except KeyError:
            missing.append(phoneme)
            continue
        ids.extend(mapped)
        ids.extend(id_map[PAD])
    ids.extend(id_map[EOS])
    if missing:
        _log.warning("skipped %d phoneme(s) this voice has no id for: %r", len(missing), missing)
    return ids
