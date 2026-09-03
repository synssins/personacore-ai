"""Markdown in a chat reply — rendered here, by hand, on already-escaped text.

**Why this module exists at all.** The model emits ordinary markdown, and the
chat screen was printing the asterisks. Rendering it is a presentation problem
for about one minute, and then it is a security problem for good, because of
what a reply *is*:

    A reply is untrusted output.

Tool results flow into the text the model writes back (``AgentLoop`` fences
them into the prompt through :mod:`personacore.agent.untrusted` precisely
because a tool result is content some third-party API controls), and the model
is under no obligation to keep that content out of its answer. So the weather
plugin's upstream can influence bytes that reach this page. Markdown rendering
is therefore the act of turning attacker-influencable text into markup, and the
only safe order to do it in is:

1. **Escape the whole reply first.** :func:`markupsafe.escape` runs over the
   raw text before a single markdown rule is applied, so every ``<``, ``>``,
   ``&``, ``"`` and ``'`` in the model's output is already an entity by the
   time this module starts looking for ``**bold**``. There is no path by which
   a character the model wrote can become part of a tag.
2. **Then render markdown from the escaped text**, emitting a fixed, closed set
   of tags this file writes itself. Not one attribute anywhere in the output
   comes from the reply — the ``class`` names are literals in this file, and no
   element that takes a URL, an event handler or a script is ever produced.

Escaping first has a pleasant side effect worth naming: the markdown syntaxes
that use ``<`` and ``>`` (autolinks, blockquotes) simply cease to exist, since
those characters are entities before any rule sees them. Neither is in the
approved subset, and neither can be smuggled back in. The approved subset
is paragraphs and line breaks, bold, italic, bulleted and numbered lists,
inline code and fenced code blocks, tables, headings, and links — the last two
with deliberate restrictions:

* **A heading renders as bold, not as a heading element.** A chat reply is
  content *inside* a page, and letting the model put ``<h1>`` into the document
  outline would let a reply restructure the screen it is displayed on.
* **A link renders as visible text, never as an anchor.** This is the one place
  the subset costs the reader something, and it is worth it: a clickable link
  in untrusted output is an untrusted navigation one click away, and the
  reader's own judgement is the only filter that would stand between them and
  it. Printing the URL costs a copy-paste and removes the whole class. Targets
  outside ``http``/``https``/``mailto`` are not printed at all — ``javascript:``
  and ``data:`` never reach the page in any form, not even as letters.

**No markdown library.** CLAUDE.md requires the owner's approval for a new
runtime dependency, and a general markdown implementation is a very large surface —
raw-HTML passthrough, autolinking, and a plugin system, all of which would have
to be switched off and kept off — to buy a subset this file implements in a few
hundred lines that can be read in one sitting. :mod:`markupsafe` is not a new
dependency: Jinja2 requires it, so it is already installed and already the
thing escaping every other template variable on this surface.

**The plain text is not replaced.** :func:`render_markdown` is display only. The
transcript store keeps what the model actually said, because the voice pipeline
(P1) has to speak these replies and ``**Today**`` must never be read aloud as
"asterisk asterisk Today".
"""

from __future__ import annotations

import re
from collections.abc import Callable

from markupsafe import Markup, escape

# ---------------------------------------------------------------------------
# Ceilings
# ---------------------------------------------------------------------------

MAX_TABLE_COLUMNS = 24
"""Cells rendered from one table row. A reply is not a spreadsheet, and a row
claiming two hundred columns would push the conversation off the screen."""

MAX_LIST_DEPTH = 6
"""Nesting levels. Beyond this, deeper items join the level above rather than
indenting further — an unbounded indent is a reply that walks off the right."""

MAX_URL_LENGTH = 300
"""Characters of a link target printed. Longer targets are shown truncated with
an ellipsis; the reply's own text is untouched by this."""

SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
"""The only schemes whose text is printed. Everything else — ``javascript``,
``data``, ``vbscript``, ``file``, anything invented — is replaced by a note.
An allowlist rather than a blocklist, because a blocklist is a list of the
schemes somebody has thought of so far."""

# ---------------------------------------------------------------------------
# Block-level patterns. All of these run on escaped text.
# ---------------------------------------------------------------------------

#: Control characters are stripped before anything else. Tab and newline are
#: kept because they mean something to a reader; the rest are invisible in the
#: page and would otherwise let a payload hide the shape of what it wrote (and
#: NUL is what this module uses as its own internal placeholder marker, so it
#: must not be able to arrive from outside).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^([ \t]*)(?:([-*+])|(\d{1,9})[.)])\s+(.*)$")
_DELIMITER_CELL = re.compile(r":?-+:?")

# ---------------------------------------------------------------------------
# Inline patterns.
# ---------------------------------------------------------------------------

#: Runs of backticks. Code spans are matched by scanning these rather than with
#: one regex, because a span is delimited by a run of the *same length* as the
#: one that opened it — ``` ``a `b` `` ``` is one span containing a backtick,
#: and a regex that pairs the first run it sees with the next one cuts it in
#: half. Scanning runs is also what keeps the cost linear in the number of runs
#: rather than quadratic in the length of the line.
_BACKTICK_RUN = re.compile(r"`+")

#: ``[label](target)``. Neither part may span a line. The destination allows one
#: level of balanced parentheses, because a target that ends in ``)`` is common
#: enough (Wikipedia, and ``javascript:alert(1)``) that stopping at the first
#: one would leave a stray bracket sitting next to a refused link.
_LINK = re.compile(r"\[([^\]\n]*)\]\(((?:[^()\n]|\([^()\n]*\))*)\)")

#: Emphasis. Every content class excludes its own delimiter for the same reason
#: the code span does — a run is matched between adjacent delimiters and cannot
#: backtrack over a long line hunting for a closer that is not there.
_BOLD_ITALIC = re.compile(r"\*\*\*([^\s*](?:[^*]*[^\s*])?)\*\*\*")
_BOLD_STAR = re.compile(r"\*\*([^\s*](?:[^*]*[^\s*])?)\*\*")
_BOLD_UNDER = re.compile(r"(?<!\w)__([^\s_](?:[^_]*[^\s_])?)__(?!\w)")
_ITALIC_STAR = re.compile(r"\*([^\s*](?:[^*]*[^\s*])?)\*")
#: The lookarounds are what keep ``snake_case_names`` out of italics.
_ITALIC_UNDER = re.compile(r"(?<!\w)_([^\s_](?:[^_]*[^\s_])?)_(?!\w)")

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

#: Restores stashed fragments. NUL cannot appear in the input (see ``_CONTROL``)
#: so a placeholder cannot be forged by the text being rendered.
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")

_PLACEHOLDER_PASSES = 4
"""How many times restoration re-scans. A stashed fragment can itself contain a
placeholder — ``[`code`](url)`` stashes the code span, then stashes the link
around it — so one pass is not always enough. Bounded rather than looped to
exhaustion so a bug here cannot become a hang."""


def render_markdown(text: str) -> Markup:
    """The approved markdown subset of ``text``, as markup safe to place in the
    page.

    The return value is :class:`~markupsafe.Markup`, so Jinja renders it as
    markup without a ``|safe`` in the template. That is deliberate: ``|safe``
    on a page is a claim made at the point of *use*, far from the code that
    would have to be correct for it to be true. Here the claim is made by the
    function that did the escaping, one line after doing it.
    """
    if not text or not text.strip():
        return Markup("")
    cleaned = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    # The one call that matters. Everything below this line works on text in
    # which no character can any longer begin a tag.
    lines = str(escape(cleaned)).split("\n")
    # S704 exists to make exactly this call reviewable, so it is left visible
    # here rather than hidden behind a helper: this is *the* line where text
    # from a model becomes markup, and it is one line below the escape that
    # earns it. Everything _blocks appends is either escaped text or a tag
    # literal from this file. If that ever stops being true, it stops being
    # true here.
    return Markup("".join(_blocks(lines)))  # noqa: S704 - escaped above; see the module docstring


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def _blocks(lines: list[str]) -> list[str]:
    """Every line of the reply, as a sequence of rendered blocks."""
    out: list[str] = []
    cursor = 0
    while cursor < len(lines):
        if not lines[cursor].strip():
            cursor += 1
            continue
        cursor, html = _one_block(lines, cursor)
        out.append(html)
    return out


def _one_block(lines: list[str], cursor: int) -> tuple[int, str]:
    """The block starting at ``cursor``, and where the next one begins."""
    line = lines[cursor]
    fence = _FENCE_OPEN.match(line)
    if fence is not None:
        return _code_block(lines, cursor, fence.group(1))
    heading = _HEADING.match(line)
    if heading is not None:
        # Bold, not <h1>-<h6>: a reply does not get to write the page's outline.
        return cursor + 1, f'<p class="md-h"><strong>{_inline(heading.group(2))}</strong></p>'
    if _starts_table(lines, cursor):
        return _table(lines, cursor)
    if _LIST_ITEM.match(line) is not None:
        return _list(lines, cursor)
    return _paragraph(lines, cursor)


def _starts_block(lines: list[str], cursor: int) -> bool:
    """Whether ``cursor`` begins something that is not a paragraph line."""
    line = lines[cursor]
    return (
        _FENCE_OPEN.match(line) is not None
        or _HEADING.match(line) is not None
        or _LIST_ITEM.match(line) is not None
        or _starts_table(lines, cursor)
    )


def _paragraph(lines: list[str], cursor: int) -> tuple[int, str]:
    """Consecutive lines of prose.

    A single newline inside a paragraph becomes ``<br>`` rather than a space.
    That is GitHub's reading of markdown rather than CommonMark's, and it is the
    right one here: people write chat replies with one thought per line and mean
    the line to be there.
    """
    body: list[str] = []
    while cursor < len(lines) and lines[cursor].strip():
        if body and _starts_block(lines, cursor):
            break
        body.append(_inline(lines[cursor].strip()))
        cursor += 1
    return cursor, "<p>" + "<br>".join(body) + "</p>"


def _code_block(lines: list[str], cursor: int, opener: str) -> tuple[int, str]:
    """A fenced block, rendered verbatim.

    The info string — the ``python`` in ```` ```python ```` — is **read and
    discarded**. Nothing on this surface highlights syntax, and a language name
    from untrusted text has exactly one job it could be given (choosing code to
    run over the content) and no job it is wanted for. Not emitting it as a
    class is one less thing for a future highlighter to pick up by accident.
    """
    closer = re.compile(f"^ {{0,3}}{re.escape(opener[0])}{{{len(opener)},}}\\s*$")
    body: list[str] = []
    cursor += 1
    while cursor < len(lines) and closer.match(lines[cursor]) is None:
        body.append(lines[cursor])
        cursor += 1
    if cursor < len(lines):
        cursor += 1  # the closing fence itself
    return cursor, '<pre class="md-pre"><code>' + "\n".join(body) + "</code></pre>"


# -- tables -----------------------------------------------------------------


def _cells(line: str) -> list[str]:
    """One table row's cells, outer pipes dropped."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _starts_table(lines: list[str], cursor: int) -> bool:
    """A header row followed by a delimiter row with the same column count.

    Insisting the counts match is what stops a paragraph that happens to
    mention a pipe, followed by a line of dashes, from becoming a table.
    """
    if "|" not in lines[cursor] or cursor + 1 >= len(lines):
        return False
    delimiter = _cells(lines[cursor + 1])
    if len(delimiter) != len(_cells(lines[cursor])):
        return False
    return all(_DELIMITER_CELL.fullmatch(cell) is not None for cell in delimiter)


def _table(lines: list[str], cursor: int) -> tuple[int, str]:
    """A pipe table. Alignment markers are parsed and ignored."""
    header = _cells(lines[cursor])[:MAX_TABLE_COLUMNS]
    cursor += 2
    out = ['<div class="md-table-wrap"><table class="md-table"><thead><tr>']
    out += [f"<th>{_inline(cell)}</th>" for cell in header]
    out.append("</tr></thead><tbody>")
    while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
        out.append("<tr>")
        out += [f"<td>{_inline(cell)}</td>" for cell in _cells(lines[cursor])[:MAX_TABLE_COLUMNS]]
        out.append("</tr>")
        cursor += 1
    out.append("</tbody></table></div>")
    return cursor, "".join(out)


# -- lists ------------------------------------------------------------------


def _list(lines: list[str], cursor: int) -> tuple[int, str]:
    """A run of list items, including nested ones and their continuations."""
    items: list[tuple[int, bool, list[str]]] = []
    while cursor < len(lines):
        line = lines[cursor]
        item = _LIST_ITEM.match(line)
        if item is not None:
            indent = len(item.group(1).expandtabs(4))
            items.append((indent, item.group(3) is not None, [item.group(4).strip()]))
            cursor += 1
            continue
        if not line.strip():
            # A blank line ends the list unless an item follows it — "loose"
            # lists, which a model writes as often as tight ones.
            if cursor + 1 < len(lines) and _LIST_ITEM.match(lines[cursor + 1]) is not None:
                cursor += 1
                continue
            break
        if items and line[:1] in (" ", "\t"):
            items[-1][2].append(line.strip())  # a wrapped item, still that item
            cursor += 1
            continue
        break
    return cursor, _render_list(items)


def _render_list(items: list[tuple[int, bool, list[str]]]) -> str:
    """Items with their indents, as properly closed nested lists.

    A stack of open levels rather than recursion, because the input is a flat
    sequence with ragged indentation and the invariant that matters — every tag
    opened is closed, in order — is easier to see kept in one loop than spread
    across recursive calls.
    """
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for indent, ordered, body in items:
        tag = "ol" if ordered else "ul"
        while len(stack) > 1 and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack:
            stack.append((indent, tag))
            out.append(f'<{tag} class="md-list">')
        elif indent > stack[-1][0] and len(stack) < MAX_LIST_DEPTH:
            stack.append((indent, tag))
            out.append(f'<{tag} class="md-list">')
        else:
            out.append("</li>")
            if tag != stack[-1][1]:
                # A different marker at the same level is a different list.
                out.append(f"</{stack.pop()[1]}>")
                stack.append((indent, tag))
                out.append(f'<{tag} class="md-list">')
        out.append("<li>" + "<br>".join(_inline(part) for part in body))
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Inline
# ---------------------------------------------------------------------------


def _inline(text: str) -> str:
    """Inline markdown in one line of already-escaped text.

    Code spans and links are replaced by placeholders before emphasis runs, so
    neither the contents of a code span nor a URL full of underscores can be
    mangled into ``<em>`` — and so no fragment this function built can be
    matched again by a later rule.
    """
    kept: list[str] = []

    def stash(html: str) -> str:
        kept.append(html)
        return f"\x00{len(kept) - 1}\x00"

    text = _code_spans(text, stash)
    text = _LINK.sub(lambda m: stash(_link(m.group(1), m.group(2))), text)
    text = _BOLD_ITALIC.sub(r"<strong><em>\1</em></strong>", text)
    text = _BOLD_STAR.sub(r"<strong>\1</strong>", text)
    text = _BOLD_UNDER.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_STAR.sub(r"<em>\1</em>", text)
    text = _ITALIC_UNDER.sub(r"<em>\1</em>", text)
    for _ in range(_PLACEHOLDER_PASSES):
        if "\x00" not in text:
            break
        text = _PLACEHOLDER.sub(lambda m: kept[int(m.group(1))], text)
    return text


def _code_spans(text: str, stash: Callable[[str], str]) -> str:
    """Replace every code span in one line with a stashed ``<code>`` element.

    An opening run of *n* backticks is closed by the next run of exactly *n*;
    an opener with no such closer is left alone as literal text, which is what
    a single stray backtick in prose is.
    """
    runs = [(match.start(), match.end() - match.start()) for match in _BACKTICK_RUN.finditer(text)]
    out: list[str] = []
    cursor = 0
    index = 0
    while index < len(runs):
        start, length = runs[index]
        closer = next(
            (other for other in range(index + 1, len(runs)) if runs[other][1] == length), None
        )
        if closer is None:
            index += 1
            continue
        end = runs[closer][0]
        out.append(text[cursor:start])
        # One space either side is the writer separating the delimiter from the
        # content (`` ` `` is how a literal backtick is written), not content.
        out.append(stash(f"<code>{text[start + length : end].strip(' ')}</code>"))
        cursor = end + length
        index = closer + 1
    out.append(text[cursor:])
    return "".join(out)


def _printable_target(target: str) -> bool:
    """Whether a link target's text may appear on the page at all.

    Two rules, and the second is the one doing the real work:

    * a target with a scheme must have one of :data:`SAFE_SCHEMES`;
    * a target *without* a scheme — a relative path — must contain neither a
      colon nor an ampersand. That second clause is what stops
      ``&#x6a;avascript:alert(1)`` from being printed as text a browser would
      then display as ``javascript:alert(1)``. It is inert either way, since
      nothing here is ever an anchor, but the rule is that those words do
      not reach the page in any form, and a relative link containing an
      entity is not a thing worth keeping.
    """
    scheme = _SCHEME.match(target)
    if scheme is not None:
        return scheme.group(1).lower() in SAFE_SCHEMES
    return ":" not in target and "&" not in target


def _link(label: str, destination: str) -> str:
    """A markdown link as text. Never an anchor, under any circumstances.

    There is no branch of this function that emits ``<a``, and that is the
    point — a reader can read the URL and decide, which is the filter a
    clickable link in untrusted output removes.
    """
    label = label.strip()
    # A title — [x](url "title") — is escaped to &#34;… by now, so splitting on
    # whitespace drops it along with any other trailing noise.
    parts = destination.strip().split()
    target = parts[0] if parts else ""
    if target and not _printable_target(target):
        removed = '<span class="md-link-removed">(link removed)</span>'
        return f"{label} {removed}" if label else removed
    if len(target) > MAX_URL_LENGTH:
        target = target[:MAX_URL_LENGTH] + "…"
    if not target:
        return label
    shown = f'<span class="md-url">{target}</span>'
    if not label or label == target:
        return shown
    return f"{label} ({shown})"


__all__ = [
    "MAX_LIST_DEPTH",
    "MAX_TABLE_COLUMNS",
    "MAX_URL_LENGTH",
    "SAFE_SCHEMES",
    "render_markdown",
]
