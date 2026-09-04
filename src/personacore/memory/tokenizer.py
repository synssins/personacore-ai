"""WordPiece tokenizer for the bundled embedding model (contract memory.md §4).

Plain Python, no new dependency: the BERT-uncased WordPiece algorithm used by
``all-MiniLM-L6-v2``, reimplemented from the published algorithm rather than
imported from a tokenizer library, because the model already ships its own
``vocab.txt`` and nothing else about BERT tokenization is needed here.

Scope: lower-case, NFD accent stripping, whitespace/punctuation splitting,
greedy longest-match WordPiece with ``##`` continuations, ``[UNK]`` for a
word with no match, ``[CLS]``/``[SEP]`` wrapping, truncation to ``max_len``
tokens including the specials. CJK character spacing (which the reference
BERT tokenizer also does) is not implemented — this model and this project
are English-text memory, and adding it untested would be a guess dressed as
a feature.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

#: Longest single "word" (post punctuation-split) WordPiece will attempt to
#: subdivide before giving up and emitting [UNK]. Matches the reference
#: implementation's own ceiling — a runaway string (no spaces at all) does
#: not turn into an O(n^2) scan.
_MAX_WORD_CHARS = 200


class TokenizerError(RuntimeError):
    """The vocab file is missing, empty, or missing a required special token."""


def _is_whitespace(ch: str) -> bool:
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_control(ch: str) -> bool:
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch).startswith("C")


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    # The ASCII ranges the reference tokenizer special-cases: characters like
    # `^` and `$` are "Sm"/"Sc" in the Unicode database, not "P", but BERT
    # still treats them as punctuation to split on.
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _clean_text(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        out.append(" " if _is_whitespace(ch) else ch)
    return "".join(out)


def _strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _split_on_punctuation(token: str) -> list[str]:
    if not token:
        return []
    output: list[list[str]] = []
    start_new_word = True
    for ch in token:
        if _is_punctuation(ch):
            output.append([ch])
            start_new_word = True
        else:
            if start_new_word:
                output.append([])
            start_new_word = False
            output[-1].append(ch)
    return ["".join(piece) for piece in output]


def _basic_tokenize(text: str) -> list[str]:
    """Lower-case, accent-strip, whitespace- and punctuation-split.

    Mirrors the reference BERT ``BasicTokenizer`` for the ASCII/Latin case
    this project actually sees; see the module docstring for what is
    deliberately not implemented.
    """
    text = _clean_text(text)
    tokens: list[str] = []
    for word in text.strip().split():
        word = _strip_accents(word.lower())
        tokens.extend(_split_on_punctuation(word))
    return [t for t in tokens if t]


class WordPieceTokenizer:
    """Greedy longest-match WordPiece over a BERT-uncased ``vocab.txt``."""

    def __init__(self, vocab_path: Path) -> None:
        self._vocab_path = Path(vocab_path)
        vocab: dict[str, int] = {}
        try:
            with self._vocab_path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle):
                    token = line.rstrip("\n")
                    if token:
                        vocab[token] = line_no
        except OSError as exc:
            raise TokenizerError(f"could not read {self._vocab_path}: {exc}") from exc
        if not vocab:
            raise TokenizerError(f"{self._vocab_path} is empty or unreadable")
        self._vocab = vocab
        try:
            self.unk_id = vocab["[UNK]"]
            self.cls_id = vocab["[CLS]"]
            self.sep_id = vocab["[SEP]"]
        except KeyError as exc:
            raise TokenizerError(
                f"{self._vocab_path} is missing a required special token: {exc}"
            ) from exc

    def _wordpiece(self, word: str) -> list[int]:
        if len(word) > _MAX_WORD_CHARS:
            return [self.unk_id]
        sub_ids: list[int] = []
        start = 0
        chars = word
        while start < len(chars):
            end = len(chars)
            matched: int | None = None
            while start < end:
                piece = chars[start:end]
                if start > 0:
                    piece = "##" + piece
                found = self._vocab.get(piece)
                if found is not None:
                    matched = found
                    break
                end -= 1
            if matched is None:
                return [self.unk_id]
            sub_ids.append(matched)
            start = end
        return sub_ids

    def encode(self, text: str, max_len: int = 256) -> tuple[list[int], list[int], list[int]]:
        """Tokenize ``text`` into ``(input_ids, attention_mask, token_type_ids)``.

        Wrapped with ``[CLS]``/``[SEP]`` and truncated to ``max_len`` tokens
        *including* those two. Empty text (or text that reduces to nothing,
        e.g. all whitespace) still returns the two-token ``[CLS] [SEP]``
        sequence rather than raising — an empty memory embeds, it does not
        error.
        """
        if max_len < 2:
            raise ValueError("max_len must allow room for [CLS] and [SEP]")
        ids: list[int] = []
        for word in _basic_tokenize(text):
            ids.extend(self._wordpiece(word))
        budget = max_len - 2
        ids = ids[:budget]
        input_ids = [self.cls_id, *ids, self.sep_id]
        attention_mask = [1] * len(input_ids)
        token_type_ids = [0] * len(input_ids)
        return input_ids, attention_mask, token_type_ids
