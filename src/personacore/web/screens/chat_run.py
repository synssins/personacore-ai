"""A runbook run's own line in the Chat screen — contract
``working/contracts/runbook.md`` §3 ("Progress in the chat", "Stop") and §5
("Resume"), PLAN.md's ``web`` row (alpha.19).

**This module never imports anything from ``personacore.runbooks.runner`` or
``personacore.runbooks.state``.** Those are the ``engine`` and ``state``
subtasks of this same alpha, running in this tree at the same time this
module is being written, and may not exist yet or may not match this
module's own guess at their shape at any moment this file is imported.
Everything here reads ``app.state.runner`` duck-typed, in exactly the shape
PLAN.md's Joints section promises (``start``, ``state``, ``stop``,
``resume``, ``is_running``, ``scan_at_boot``) and reads the ``RunState`` it
returns with ``getattr`` (``status``, ``current``, ``reason``) — the same
tolerance :mod:`personacore.web.screens.runbooks` already gives
``app.state.runbooks``. When the attribute is absent this module renders
:data:`RUN_UNAVAILABLE` rather than raising, per this task's own brief.

**The conversation id used throughout this module is the real one**
(:attr:`~personacore.conversations.models.Conversation.conversation_id`, a
UUID4 string minted once by the conversation store) — never the
``started_at`` instant the chat page's own ``?c=`` address is built from.
Owner-checked exactly as
:mod:`personacore.web.screens.chat_workspace`'s own download route is: the
conversation is resolved against this operator's own id first, and a
conversation that exists but is not theirs answers exactly like one that
does not exist at all.

**``RunState.conversation_id``, read with ``getattr``, is this module's own
addition to PLAN.md's Joints — not in the Joints table as written.** The web
side needs *some* way to learn which conversation ``Runner.start`` just
created (the picker/Run… form redirects there, contract §3 "Start"), and the
Joints as written give it nothing: ``RunState`` carries the run's own facts
(runbook, version, plugin, persona, inputs, steps, status) but not the
conversation it runs in. This module assumes ``state.conversation_id`` will
be there and falls back to the Chat inbox (``/admin/chat``, no ``?c=``)
without raising when it is not — flagged here, loudly, for whoever lands
``runner.py`` to either confirm or correct.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from personacore.audit.models import Owner, Surface
from personacore.conversations.service import ConversationService
from personacore.web.screens import chat_turns
from personacore.web.screens.chat_workspace import WORKSPACE_URL_PREFIX, read_workspace_text
from personacore.web.shared import UIContext

RUN_UNAVAILABLE = "Runbook runs are not available in this build."
"""Rendered instead of a 500 whenever one of this module's own routes is
reached with no ``app.state.runner`` wired in — this task's own brief,
verbatim."""

RUN_LOCK_HINT = "A runbook is running here; Stop it to type."
"""Contract §3: "The chat box is locked while a run is active." Shown next
to the disabled composer while it is locked for that reason — a running
step, or an interrupted/parked run with nothing left for a person to answer.
"""

GATE_LOCK_HINT = "A runbook is waiting for your answers above."
"""This task's own finding 7 (the same live run that found "Editor in chief"
above): the composer is also locked while a **questions** gate's card is
showing, and :data:`RUN_LOCK_HINT` is a wrong sentence for that moment —
there is no Stop button on a parked run, only the Answer buttons already on
screen. Shown instead of :data:`RUN_LOCK_HINT` whenever :func:`lock_hint`
finds a pending gate; never shown at the same time as
:data:`TEXT_ANSWER_LABEL`, which unlocks the box rather than explaining why
it is locked."""

RUN_NOT_FOUND = "There is no run there."
"""One sentence for a conversation that is not this operator's own and one
that does not exist at all — the same "a mismatch and a missing file answer
alike" rule :data:`personacore.web.screens.chat_workspace.NOT_FOUND` follows,
applied to a run instead of a file."""

GENERIC_RUN_REFUSAL = "That could not be done."
"""Never shown for a refusal ``Runner`` explains — only for one that raised
without a ``message`` at all, the same last-resort :data:`personacore.web.
screens.runbooks.GENERIC_UPLOAD_REFUSAL` is for an upload."""

TEXT_ANSWER_LABEL = "Your next message answers this gate"
"""WAVE2.md's own wording for text-answer mode: a gate whose questions the
model wrote could not be parsed, so it parks waiting for one typed message
instead. Shown above the composer while :meth:`Runner.awaiting_text` says so
— **the one state that unlocks the box while a run is otherwise locked**
(see :func:`composer_locked`)."""

COMPOSER_MAX_MESSAGE_CHARS = 8000
"""Mirrors :data:`personacore.web.screens.chat_exchange.MAX_MESSAGE_CHARS` by
value, not by import: this module is never allowed to import
``chat_exchange`` (that module already imports this one for the text-answer
branch — see its own module docstring — and a mutual import is a cycle
neither side can resolve). The two are asked to agree by comment because a
single shared constant is not worth the cycle it would cost to reach it."""

_EMPTY_RUN_VIEW: dict[str, Any] = {
    "visible": False,
    "poll": False,
    "cid": "",
    "message": "",
    "show_stop": False,
    "show_resume": False,
    "stop_url": "",
    "resume_url": "",
    "gate": None,
    "items": [],
    "turn_running": False,
}
"""Nothing to show — no run, no runner, or a run that has already ended.
Copied (never mutated) by every branch of :func:`runbook_run_view`.

``gate`` (WAVE2.md's "Gate questions in the chat") and ``items`` (a parent
run's own progress lines) are the two things this view can show *instead of*
the plain message/Stop/Resume line — never alongside it, because the current
step is either running plainly, waiting on a question, or the run is a
parent watching its own children; it is never two of those at once.

``turn_running`` is never set here — :func:`runbook_run_view` knows nothing
of :mod:`chat_turns` (the module docstring's own boundary: this file reads
only ``app.state.runner``). It is filled in afterwards, by ``register``'s own
``_fragment``, the one place that renders ``fragments/run_status.html`` and
therefore the one place a page's reattach can be decided."""


def runner_for(request: Request) -> Any | None:
    """``request.app.state.runner``, or ``None``. See the module docstring
    for why this is never imported and never asserted to be a particular
    type."""
    return getattr(request.app.state, "runner", None)


def _sentence(text: str) -> str:
    """``text`` with exactly one full stop at the end of it.

    The reasons a run parks with are written as whole sentences by whoever
    raised them ("it wrote more than 1,000 tokens."), and the line that shows
    one used to add a full stop of its own regardless — so the status line read
    "…tokens.. Resume?".
    """
    trimmed = text.strip()
    if not trimmed or trimmed.endswith((".", "!", "?", ":")):
        return trimmed
    return f"{trimmed}."


def _current_step(state: Any) -> Any | None:
    """The step ``state.current`` names, out of ``state.steps`` (WAVE2.md's
    Joints: ``state.steps[i].gate``) — found by matching ``.id`` rather than
    assumed to be a particular index, since nothing in the Joints promises an
    order. ``None`` for a state with no current step, no steps at all, or one
    naming a step that is not on the list — every branch here is "nothing to
    ask", never a raise.
    """
    current = getattr(state, "current", None)
    if not current:
        return None
    for step in getattr(state, "steps", None) or ():
        if getattr(step, "id", None) == current:
            return step
    return None


def _step_label(state: Any, step_id: str | None) -> str | None:
    """``step_id``, or ``"id · title"`` when the current step's own
    :class:`~personacore.runbooks.state.StepState` carries a ``title``
    (SPEC alpha.22, "every step says what it is for") — what the "Running
    p5…" status line names the step by.

    A local twin of ``runbooks.runner._label``, not an import of it: this
    module never imports anything from :mod:`personacore.runbooks.runner`
    (see the module docstring), so a build whose ``engine`` subtask has not
    landed yet still renders this line — with the bare id, exactly as it
    always did, since :func:`_current_step` then finds no step at all.
    """
    if not step_id:
        return step_id
    step = _current_step(state)
    title = getattr(step, "title", None) if step is not None else None
    return f"{step_id} · {title}" if title else step_id


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """One field off a ``Question`` — a ``dict`` (however ``gates.py`` hands
    one back over the JSON boundary) or an attribute-carrying object
    (``gates.py``'s own ``Question`` model) read the same tolerant way, so
    :func:`_gate_view` does not have to guess which shape this alpha's
    ``gates`` subtask settled on."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _context_rows(question: Any) -> list[dict[str, str]]:
    """A question's own ``context`` passages, as the card draws them —
    contract §4, added 2026-09-05: "every question carries the passages it
    is about." Read the same tolerant, dict-or-attribute way :func:`_field`
    reads everything else about a question, since a passage is one more
    thing ``gates.py`` may hand back as either shape.

    ``role`` (SPEC alpha.22, contract §4 2026-09-06) is read the same
    tolerant way and defaults to ``""`` — a passage with none is simply
    never a candidate for the comparison card (:func:`_compare_view`), the
    same as a ``.run.json`` gate state parked before the field existed."""
    rows: list[dict[str, str]] = []
    for passage in _field(question, "context", None) or ():
        ref = str(_field(passage, "ref", "") or "")
        quote = str(_field(passage, "quote", "") or "")
        role = str(_field(passage, "role", "") or "")
        if ref or quote:
            rows.append({"ref": ref, "quote": quote, "role": role})
    return rows


_PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n+")
_PARAGRAPH_PREFIX_CHARS = 40
"""Contract §4, added 2026-09-06: the third of the paragraph match's three
tries is "the first 40 characters of the quote" — a model's own word-for-word
quote can still drift from the file (a line wrap, a curly quote) enough that
neither an exact nor a whitespace-normalised match finds it, and a shorter,
plainer needle still usually will."""


def _paragraphs(text: str) -> list[str]:
    """``text`` split into paragraphs — contract §4, added 2026-09-06:
    "paragraph = text between blank lines." Each paragraph keeps its own
    internal line breaks; only the blank lines that separate paragraphs,
    and the leading/trailing ones a paragraph picks up from the split, are
    dropped."""
    return [part.strip("\n") for part in _PARAGRAPH_SPLIT_RE.split(text) if part.strip()]


def _normalized(text: str) -> str:
    """``text`` with every run of whitespace collapsed to one space — the
    second of the paragraph match's three tries, for a quote that reproduces
    the words but not the line wraps."""
    return " ".join(text.split())


def _paragraph_containing(text: str, quote: str) -> str:
    """The whole paragraph of ``text`` that contains ``quote`` — contract
    §4, added 2026-09-06. Tried three ways, in order: the quote exactly as
    written, the quote with whitespace normalised, and the quote's own
    first 40 characters; ``quote`` itself is the fallback when none of the
    three find it, which is exactly what the card showed before this
    feature existed.
    """
    if not quote:
        return quote
    paragraphs = _paragraphs(text)
    for paragraph in paragraphs:
        if quote in paragraph:
            return paragraph
    normalized_quote = _normalized(quote)
    if normalized_quote:
        for paragraph in paragraphs:
            if normalized_quote in _normalized(paragraph):
                return paragraph
    prefix = quote[:_PARAGRAPH_PREFIX_CHARS]
    if prefix:
        for paragraph in paragraphs:
            if prefix in paragraph:
                return paragraph
    return quote


def _compare_view(
    context: list[dict[str, str]],
    source_pins: Mapping[str, str],
    layout: Any | None,
    cid: str,
) -> dict[str, Any] | None:
    """A question's own comparison card — contract §4, added 2026-09-06: a
    passage that exists in two of the files the review step read is shown
    as two panes, the earlier-produced role on the left, the later on the
    right, each expanded to its whole paragraph (:func:`_paragraph_containing`).

    ``None`` when there is no ``layout``/``cid`` to read a file through, no
    ``source_pins`` to open one with, or the question's own context does
    not span exactly two of those roles — every one of those is the
    ordinary options card's job, unchanged.
    """
    if layout is None or not cid or not source_pins:
        return None
    order = list(source_pins.keys())
    found: dict[str, str] = {}
    for passage in context:
        role = passage.get("role") or ""
        if not role or role not in source_pins or role in found:
            continue
        text = read_workspace_text(layout, cid, source_pins[role])
        if text is None:
            continue
        found[role] = _paragraph_containing(text, passage.get("quote", ""))
    roles = [role for role in order if role in found]
    if len(roles) != 2:
        return None
    left_role, right_role = roles
    return {
        "left": {"role": left_role, "label": f"original · {left_role}", "text": found[left_role]},
        "right": {"role": right_role, "label": f"now · {right_role}", "text": found[right_role]},
    }


def _matched_flag_line(text: str, context: list[dict[str, str]]) -> str | None:
    """The line in the gate's own ``from:`` file this question came from —
    contract §4, added 2026-09-05: "the flag line the question came from",
    matched by the first context passage's ``ref``, else by the first 20
    characters of any context passage's ``quote``, else nothing.

    A whole-line substring match, the same rule :func:`~personacore.
    runbooks.gates.evaluate`'s own line conditions use, and for the same
    reason: a flag file is prose or a Markdown table, not something a person
    writing one is matching a pattern against.
    """
    lines = text.splitlines()
    first_ref = context[0]["ref"] if context and context[0]["ref"] else None
    if first_ref:
        for line in lines:
            if first_ref in line:
                return line.strip()
    for passage in context:
        needle = passage["quote"][:20]
        if not needle:
            continue
        for line in lines:
            if needle in line:
                return line.strip()
    return None


def _gate_view(
    state: Any, *, layout: Any | None = None, cid: str = ""
) -> dict[str, Any] | None:
    """The chat's own gate-question card (WAVE2.md "Gate questions in the
    chat"), or ``None``.

    Reads the current step's own ``gate.questions``/``gate.answers`` (the
    Joints' ``GateState``), tolerantly: a question is read as a ``dict`` or
    as an attribute-carrying object either way, since ``gates.py`` (a
    sibling subtask, never imported here — see the module docstring) is free
    to hand back either shape and this module holds neither.

    ``None`` for a step with no gate, a gate not in ``questions`` mode, one
    with no questions at all, or one every question of which
    ``gate.answers`` already covers — the run has moved on and there is
    nothing left to ask.

    ``layout``/``cid`` are contract §4's 2026-09-05 addition: with both
    given, the current question's own passages are matched against the
    gate's ``source_file`` (read through :func:`~personacore.web.screens.
    chat_workspace.read_workspace_text` — the jailed reader, never a path
    opened from state directly) to find the flag line it came from, and a
    link to open that file is built from the same route
    :mod:`personacore.web.screens.chat_workspace` already serves it on.
    Omitted (the default) for :func:`composer_locked`'s own call, which only
    ever asks "is there a gate" and reads neither.

    ``title``/``description`` (the gate step's own) and ``source_role``/
    ``source_title``/``source_description`` (the ``from:`` step's own) are
    SPEC alpha.22's addition — read straight off the gate state
    (:class:`~personacore.runbooks.state.GateState`, the runner's own
    addition of these same fields) and never re-parsed off the runbook file,
    which this module has no business reading at all.

    ``compare`` (SPEC alpha.22, contract §4 2026-09-06) is
    :func:`_compare_view` on this question's own context and the gate's own
    ``source_pins`` — ``None`` for the ordinary options card, or
    ``{"left": {...}, "right": {...}}`` when the question's passages span
    two roles the ``from:`` step could see, in which case the template
    draws the comparison card instead of the options.
    """
    step = _current_step(state)
    if step is None:
        return None
    gate = getattr(step, "gate", None)
    if gate is None or str(getattr(gate, "mode", "") or "") != "questions":
        return None
    questions = list(getattr(gate, "questions", None) or ())
    if not questions:
        return None
    answers = dict(getattr(gate, "answers", None) or {})
    total = len(questions)
    for index, question in enumerate(questions, start=1):
        qid = _field(question, "id")
        if qid is None or qid in answers:
            continue
        other = _field(question, "other", True)
        context = _context_rows(question)
        source_file = getattr(gate, "source_file", None)
        source_url: str | None = None
        flag_line: str | None = None
        if isinstance(source_file, str) and source_file and layout is not None and cid:
            source_url = f"{WORKSPACE_URL_PREFIX}{cid}/{source_file}"
            text = read_workspace_text(layout, cid, source_file)
            if text is not None:
                flag_line = _matched_flag_line(text, context)
        source_pins = dict(getattr(gate, "source_pins", None) or {})
        compare = _compare_view(context, source_pins, layout, cid)
        return {
            "question_id": qid,
            "text": str(_field(question, "text") or ""),
            "options": [str(option) for option in (_field(question, "options") or ())],
            "other": True if other is None else bool(other),
            "index": index,
            "total": total,
            "context": context,
            "flag_line": flag_line,
            "source_file": source_file if isinstance(source_file, str) else None,
            "source_url": source_url,
            # SPEC alpha.22, "every step says what it is for": the gate's own
            # title/description head the card, and the `from:` step's own
            # name the "Questions from …" line below it — both stored on the
            # gate state by the runner (`runbooks/state.py`'s `GateState`),
            # never re-read off the runbook file here.
            "title": getattr(gate, "title", None),
            "description": getattr(gate, "description", None),
            "source_role": getattr(gate, "source_role", None),
            "source_title": getattr(gate, "source_title", None),
            "source_description": getattr(gate, "source_description", None),
            # SPEC alpha.22, contract §4 2026-09-06: the comparison card,
            # or `None` for the ordinary options card — see `_compare_view`.
            "compare": compare,
        }
    return None


async def _items_view(
    state: Any, *, link_for: Callable[[str], Awaitable[str | None]] | None
) -> list[dict[str, Any]]:
    """A parent run's own progress lines (WAVE2.md's Joints: ``RunState.
    items``) — one dict per :class:`ItemState`, its own conversation resolved
    to a chat link when there is one and a resolver was handed in.

    Empty for an ordinary (non-parent) run, or one whose runner predates
    ``items`` — ``getattr`` throughout, the same tolerance every other read
    in this module gives a Joint still in flight.
    """
    rows: list[dict[str, Any]] = []
    for item in getattr(state, "items", None) or ():
        conversation_id = getattr(item, "conversation_id", None)
        link: str | None = None
        if conversation_id and link_for is not None:
            try:
                link = await link_for(conversation_id)
            except Exception:  # noqa: BLE001 - a broken link beats a dead line
                link = None
        rows.append(
            {
                "value": getattr(item, "value", None),
                "status": str(getattr(item, "status", "") or ""),
                "reason": getattr(item, "reason", None),
                "link": link,
            }
        )
    return rows


def conversation_link_for(
    conversations: ConversationService, owner: Owner
) -> Callable[[str], Awaitable[str | None]]:
    """A ``link_for`` closure for :func:`runbook_run_view`'s own ``items``
    rows — WAVE2.md's parent-run "a link to the item's conversation".

    Resolved through the store, by this same owner, the same way every other
    conversation id this module is handed is resolved (:func:`_owned_cid`):
    never a raw id pasted into a URL, which a mismatched or stale one would
    otherwise serve to whoever happened to be looking, rather than answering
    "there is nothing there" the way the rest of this surface does.
    """

    async def _link(conversation_id: str) -> str | None:
        conversation = await conversations.resolve(owner, conversation_id=conversation_id)
        if conversation is None:
            return None
        return f"/admin/chat?c={quote(conversation.started_at.isoformat())}"

    return _link


async def runbook_run_view(
    runner: Any | None,
    cid: str | None,
    *,
    link_for: Callable[[str], Awaitable[str | None]] | None = None,
    layout: Any | None = None,
) -> dict[str, Any]:
    """The chat window's own small account of a runbook run in this
    conversation — the full page load and the polled fragment build the
    exact same dict, so the two can never tell two different stories about
    one run.

    Never raises: a ``runner`` that is ``None``, a conversation with no run
    at all, and a broken read all come back as "nothing to show" rather than
    an error.

    ``link_for`` is only ever asked about a parent run's own item
    conversations (:func:`_items_view`) — pass :func:`conversation_link_for`
    to resolve them, or leave it ``None`` (every item then carries no link,
    which is still an honest — if incomplete — row rather than a raise).

    ``layout`` is contract §4's 2026-09-05 addition, passed straight to
    :func:`_gate_view` so a pending gate's own card can read the flag line
    and file it came from. ``None`` (the default) draws the card with
    neither — a caller that has not been given a layout, or a test that does
    not care about that half of the card.

    A pending **gate** (WAVE2.md "Gate questions in the chat") and a
    parent's own **items** each pre-empt the plain message/Stop/Resume line
    below — checked first, and returned from, regardless of ``status``: a
    gate can park the run while it waits on a person, and a parent run's own
    "current step" is a fiction the plain branches below know nothing about.
    """
    if runner is None or not cid:
        return dict(_EMPTY_RUN_VIEW)
    try:
        state = await runner.state(cid)
    except Exception:  # noqa: BLE001 - a broken read shows nothing, not a 500
        return dict(_EMPTY_RUN_VIEW)
    if state is None:
        return dict(_EMPTY_RUN_VIEW)

    run_status = str(getattr(state, "status", "") or "")
    current = getattr(state, "current", None)
    reason = getattr(state, "reason", None)
    stop_url = f"/admin/chat/run/{cid}/stop"
    resume_url = f"/admin/chat/run/{cid}/resume"

    gate = _gate_view(state, layout=layout, cid=cid)
    if gate is not None:
        return {
            **_EMPTY_RUN_VIEW,
            "visible": True,
            "poll": False,
            "cid": cid,
            "gate": gate,
        }

    items = await _items_view(state, link_for=link_for)
    if items:
        return {
            **_EMPTY_RUN_VIEW,
            "visible": True,
            "poll": run_status == "running",
            "cid": cid,
            "message": (
                f"Running {_step_label(state, current)}…"
                if current and run_status == "running"
                else ""
            ),
            "items": items,
        }

    if run_status == "running":
        message = f"Running {_step_label(state, current)}…" if current else "Running…"
        return {
            **_EMPTY_RUN_VIEW,
            "visible": True,
            "poll": True,
            "cid": cid,
            "message": message,
            "show_stop": True,
            "stop_url": stop_url,
        }
    if run_status == "interrupted":
        message = f"Interrupted at {current}. Resume?" if current else "Interrupted. Resume?"
        return {
            **_EMPTY_RUN_VIEW,
            "visible": True,
            "poll": True,
            "cid": cid,
            "message": message,
            "show_resume": True,
            "resume_url": resume_url,
        }
    if run_status == "parked":
        step = f"{current} " if current else ""
        if reason:
            # The reason is already a sentence — the runner writes "it wrote
            # more than 1,000 tokens." with its own full stop — so this adds
            # one only when there is none, rather than printing "tokens..".
            message = f"{step}failed: {_sentence(reason)} Resume?"
        else:
            message = f"{step}parked. Resume?"
        return {
            **_EMPTY_RUN_VIEW,
            "visible": True,
            "poll": False,
            "cid": cid,
            "message": message.strip().replace("  ", " "),
            "show_resume": True,
            "resume_url": resume_url,
        }
    # stopped / done / failed / anything this module does not recognise yet:
    # terminal, nothing left to say here — the transcript's own notice lines
    # already say how it ended (contract §3's per-step progress rows).
    return dict(_EMPTY_RUN_VIEW)


def awaiting_text_view(runner: Any | None, cid: str | None) -> bool:
    """Whether the next message typed into this conversation answers a gate
    (WAVE2.md's text-answer mode) — :meth:`Runner.awaiting_text`, read
    tolerantly: no runner, no conversation, or a runner that raises all
    answer "no", which only ever costs the label, never a 500.
    """
    if runner is None or not cid:
        return False
    try:
        return bool(runner.awaiting_text(cid))
    except Exception:  # noqa: BLE001 - never a 500 for a composer's own label
        return False


async def composer_locked(runner: Any | None, cid: str | None) -> bool:
    """Whether the composer is disabled right now.

    Locked while a run is ``running`` (contract §3: "The chat box is locked
    while a run is active") **or** while the current step is waiting on a
    **questions** gate (WAVE2.md: "The composer stays locked in questions
    mode") — the second is checked whatever ``status`` says, because a gate
    can park the run while it waits and the lock still applies.

    :meth:`Runner.awaiting_text` is checked first and, when true, wins over
    both: text-answer mode unlocks the box with its own label
    (:data:`TEXT_ANSWER_LABEL`) rather than :data:`RUN_LOCK_HINT`, so a run
    parked for a typed answer must never also read as locked.
    """
    if runner is None or not cid:
        return False
    if awaiting_text_view(runner, cid):
        return False
    try:
        if bool(runner.is_running(cid)):
            return True
    except Exception:  # noqa: BLE001 - never a 500 for a composer's own attribute
        return False
    try:
        state = await runner.state(cid)
    except Exception:  # noqa: BLE001 - never a 500 for a composer's own attribute
        return False
    if state is None:
        return False
    return _gate_view(state) is not None


async def lock_hint(runner: Any | None, cid: str | None) -> str:
    """Which sentence explains a locked composer — this task's own finding
    7: :data:`RUN_LOCK_HINT` (a run in flight) or :data:`GATE_LOCK_HINT` (a
    gate's own questions are showing above the box). Read off the same state
    :func:`composer_locked` already reads for its own last branch, so the two
    can never disagree about *why* the box is disabled — only
    :func:`composer_locked` says *whether*.

    Never raises, the same tolerance every other read in this module gives a
    runner: :data:`RUN_LOCK_HINT` is the answer for no runner, no
    conversation, or a broken read, exactly as it always was before this
    function existed.
    """
    if runner is None or not cid:
        return RUN_LOCK_HINT
    try:
        state = await runner.state(cid)
    except Exception:  # noqa: BLE001 - never a 500 for a composer's own label
        return RUN_LOCK_HINT
    if state is not None and _gate_view(state) is not None:
        return GATE_LOCK_HINT
    return RUN_LOCK_HINT


def _refusal_message(exc: Exception) -> str:
    """``RunRefused.message`` (PLAN.md's Joints), read with ``getattr``
    rather than an ``isinstance`` check — this module never imports the
    class it names (see the module docstring). Anything else caught here is
    shown as :data:`GENERIC_RUN_REFUSAL` rather than left to become a 500."""
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message
    text = str(exc).strip()
    return text or GENERIC_RUN_REFUSAL


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the routes a runbook run has in the chat window: its own
    status fragment, Stop, Resume, and the gate-question answer (WAVE2.md's
    ``picker`` row). All sit under ``/admin/chat/`` (already open to a
    household member, not only an admin —
    :data:`personacore.web.routes.MEMBER_PREFIXES`), because a run's own
    conversation is exactly as much this operator's as the rest of Chat.
    """
    templates = ctx.templates
    require_user = ctx.require_user
    store = ctx.audit
    layout = ctx.layout
    conversations = ConversationService(store, surface=Surface.ADMIN_UI)

    async def _turn_running(request: Request, cid: str) -> bool:
        """Whether a chat turn is registered and running in this conversation
        **right now** — :func:`chat_turns.running_turn`, never this run's own
        ``status``.

        The two differ exactly when a page's reattach depends on it: a run
        moving from one model step to the next ends one turn and starts a
        fresh one (``chat_turns.begin_turn``) while ``status`` reads
        ``running`` the whole time, and a page that attached to the first
        turn has nothing pointing it at the second — its own
        ``data-turn-running`` was spent once, at page load (see
        ``fragments/run_status.html``'s own note on this field).

        ``chat_turns.running_turn`` is keyed by the conversation's own
        ``started_at`` marker, not by ``cid``, so this conversation is
        resolved again to find it. Never raises: this decides one attribute
        on a status line, and a broken read costs a page one reattach it can
        still pick up on the next poll, not a response.
        """
        try:
            user = require_user(request)
            conversation = await conversations.resolve(
                Owner.profile(user.id), conversation_id=cid
            )
            if conversation is None:
                return False
            return chat_turns.running_turn(
                request, user.id, conversation.started_at.isoformat()
            )
        except Exception:  # noqa: BLE001 - a missed reattach beats a dead poll
            return False

    async def _fragment(request: Request, view: dict[str, Any], *, cid: str) -> HTMLResponse:
        """The run's own status fragment, **plus the composer's own lock
        state as an out-of-band swap** (this task's rework item 1): every
        response this module answers with re-renders both, so pressing Stop
        (or Resume, or answering a gate) unlocks or relocks the box in the
        same response — no second round trip, no stale composer left
        disabled after the run that disabled it has already ended.

        **Also the one place a page's reattach across a step boundary can be
        decided** (rework item 2): ``view["turn_running"]`` is always
        ``False`` out of :func:`runbook_run_view` (that module never imports
        :mod:`chat_turns` — see its own docstring), and is filled in here,
        the one function every route in this module renders
        ``fragments/run_status.html`` through — including the poll itself
        (``chat_run_status``, fetched by the element's own ``hx-get`` every
        four seconds while a run is running).
        """
        run_html = templates.TemplateResponse(
            request=request,
            name="fragments/run_status.html",
            context={"run": {**view, "turn_running": await _turn_running(request, cid)}},
        ).body
        runner = runner_for(request)
        composer_html = templates.TemplateResponse(
            request=request,
            name="fragments/composer_lock.html",
            context={
                "locked": await composer_locked(runner, cid),
                "awaiting_text": awaiting_text_view(runner, cid),
                "hint": await lock_hint(runner, cid),
                "awaiting_text_label": TEXT_ANSWER_LABEL,
                "max_message_chars": COMPOSER_MAX_MESSAGE_CHARS,
            },
        ).body
        return HTMLResponse(content=run_html + composer_html)

    async def _unavailable(request: Request, cid: str) -> HTMLResponse:
        return await _fragment(
            request,
            {**_EMPTY_RUN_VIEW, "visible": True, "cid": cid, "message": RUN_UNAVAILABLE},
            cid=cid,
        )

    async def _owned_cid(request: Request, cid: str) -> str:
        """The same id back, once it is proven to be this operator's own
        conversation — never the :class:`~personacore.conversations.models.
        Conversation` itself, because every caller here only ever needs the
        id to hand to ``runner``."""
        user = require_user(request)
        owner = Owner.profile(user.id)
        conversation = await conversations.resolve(owner, conversation_id=cid)
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, RUN_NOT_FOUND)
        return conversation.conversation_id

    def _link_for(request: Request) -> Callable[[str], Awaitable[str | None]]:
        """This request's own :func:`conversation_link_for` closure — built
        fresh per request because it is only ever correct for the operator
        who is asking, never cached across one."""
        user = require_user(request)
        return conversation_link_for(conversations, Owner.profile(user.id))

    @router.get(
        "/chat/run/{cid}",
        response_class=HTMLResponse,
        summary="A runbook run's status line, to its owner only",
    )
    async def chat_run_status(request: Request, cid: str) -> HTMLResponse:
        owned = await _owned_cid(request, cid)
        runner = runner_for(request)
        if runner is None:
            return await _unavailable(request, owned)
        view = await runbook_run_view(runner, owned, link_for=_link_for(request), layout=layout)
        return await _fragment(request, view, cid=owned)

    @router.post(
        "/chat/run/{cid}/stop",
        response_class=HTMLResponse,
        summary="Stop the runbook running here, to its owner only",
    )
    async def chat_run_stop(request: Request, cid: str) -> HTMLResponse:
        user = require_user(request)
        owned = await _owned_cid(request, cid)
        runner = runner_for(request)
        if runner is None:
            return await _unavailable(request, owned)
        try:
            await runner.stop(Owner.profile(user.id), owned)
        except Exception as exc:  # noqa: BLE001 - shown, never a 500
            return await _fragment(
                request,
                {
                    **_EMPTY_RUN_VIEW,
                    "visible": True,
                    "cid": owned,
                    "message": _refusal_message(exc),
                },
                cid=owned,
            )
        view = await runbook_run_view(runner, owned, link_for=_link_for(request), layout=layout)
        return await _fragment(request, view, cid=owned)

    @router.post(
        "/chat/run/{cid}/resume",
        response_class=HTMLResponse,
        summary="Resume the runbook parked or interrupted here, to its owner only",
    )
    async def chat_run_resume(request: Request, cid: str) -> HTMLResponse:
        user = require_user(request)
        owned = await _owned_cid(request, cid)
        runner = runner_for(request)
        if runner is None:
            return await _unavailable(request, owned)
        try:
            await runner.resume(Owner.profile(user.id), owned)
        except Exception as exc:  # noqa: BLE001 - RunRefused's sentence, never a 500
            return await _fragment(
                request,
                {
                    **_EMPTY_RUN_VIEW,
                    "visible": True,
                    "cid": owned,
                    "message": _refusal_message(exc),
                },
                cid=owned,
            )
        view = await runbook_run_view(runner, owned, link_for=_link_for(request), layout=layout)
        return await _fragment(request, view, cid=owned)

    @router.post(
        "/chat/run/{cid}/answer",
        response_class=HTMLResponse,
        summary="Answer the current gate question, to its owner only",
    )
    async def chat_run_answer(request: Request, cid: str) -> HTMLResponse:
        """WAVE2.md "Gate questions in the chat": one option, or an "Other"
        typed answer, posted for the question the card is currently
        showing. Re-renders the same area — the next question in place, or
        the resumed status line once every question has one.
        """
        user = require_user(request)
        owned = await _owned_cid(request, cid)
        runner = runner_for(request)
        if runner is None:
            return await _unavailable(request, owned)
        form = await request.form()
        try:
            question_id = str(form.get("question_id") or "")
            choice = form.get("choice")
            other = form.get("other")
        finally:
            await form.close()
        try:
            await runner.answer(
                Owner.profile(user.id),
                owned,
                question_id,
                str(choice) if choice else None,
                str(other) if other else None,
            )
        except Exception as exc:  # noqa: BLE001 - RunRefused's sentence, never a 500
            return await _fragment(
                request,
                {
                    **_EMPTY_RUN_VIEW,
                    "visible": True,
                    "cid": owned,
                    "message": _refusal_message(exc),
                },
                cid=owned,
            )
        view = await runbook_run_view(runner, owned, link_for=_link_for(request), layout=layout)
        return await _fragment(request, view, cid=owned)


__all__ = [
    "COMPOSER_MAX_MESSAGE_CHARS",
    "GATE_LOCK_HINT",
    "GENERIC_RUN_REFUSAL",
    "RUN_LOCK_HINT",
    "RUN_NOT_FOUND",
    "RUN_UNAVAILABLE",
    "TEXT_ANSWER_LABEL",
    "awaiting_text_view",
    "composer_locked",
    "conversation_link_for",
    "lock_hint",
    "register",
    "runbook_run_view",
    "runner_for",
]
