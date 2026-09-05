"""One reply, in the shape the message template renders.

What a finished turn *cost* (:class:`TurnMetrics`), what it produced
(:func:`chat_exchange`), what it called and what that took, where its audio
lives, what the model reasoned on the way there — and, in the same shape, a
turn that never reached the model at all (:func:`_refused`). One dictionary
for one message, so ``fragments/chat_exchange.html`` is the only thing in this
interface that knows what a message looks like.

**Reasoning is handed in, not read back, here.** ``chat_exchange``'s
``reasoning`` parameter is whatever the caller already gathered while the turn
was streaming (:class:`~personacore.web.screens.chat_streaming._Spoken`'s
own field) — this module still does not touch a store; a *replayed* reply's
reasoning is read back by :mod:`personacore.web.screens.chat_thread`
instead, from its own table (:class:`~personacore.audit.models.ReasoningRecord`),
and lands in this same dict shape under the same key.

A file of its own because none of it needs a request, a store or a runner: it
is handed the object the agent loop produced and it copies out what the screen
is allowed to see. That copy is the point — a template reaching into the
runner's own result would couple this surface to whatever that object grows
into — and it is also the one place "render the failure as a sentence, never a
traceback" can be enforced.

**It deliberately does not read anything back.** Reading rows out of the store
and naming who said them is
:mod:`personacore.web.screens.chat_thread`'s; producing the audio is
:mod:`personacore.web.screens.chat_audio`'s. Nothing here synthesises
speech, and nothing here writes to a store.

Split out of ``chat.py`` unchanged (ADR-0040). Every name below is still
importable from that module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from personacore.audit.models import AuditCategory
from personacore.web.markdown import render_markdown
from personacore.web.screens.chat_attachments import AttachmentChip
from personacore.web.screens.chat_workspace import WorkspaceChip
from personacore.web.shared import PERSONA_UNRECORDED, _human_gap

# ---------------------------------------------------------------------------
# One finished turn
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnMetrics:
    """What one turn actually cost, measured while it happened.

    The owner asked for these by name and they are all the same question:
    *where did twenty-five seconds go.* A wall-clock total already existed and cannot
    answer it — a turn that spent twenty seconds waiting for a model to start
    and a turn that spent twenty seconds running a slow tool look identical
    from one number.

    Every field is optional because every one of them can genuinely be
    unknown: a turn that produced no text has no first token, and a reply that
    was never spoken has no first audio. ``None`` is printed as nothing rather
    than as a zero, which would be a claim.

    ``tokens`` is counted as **text fragments the model streamed**, and that is
    the honest name for it: no OpenAI-compatible host reports usage on a
    streamed response, so nothing in this process ever sees a real token count.
    One fragment is one token on every host this project has met, and the
    figure is labelled ``tok/s`` because that is what an operator is comparing
    it against.
    """

    first_token_ms: float | None = None
    total_ms: float | None = None
    tokens: int = 0
    first_audio_ms: float | None = None
    tools: tuple[tuple[str, float | None], ...] = ()
    """Each tool the model called, in order, with how long it ran. The
    duration comes from the agent loop, which timed the call itself; ``None``
    is a loop that did not report one."""

    prompt_tokens: int | None = None
    """The conversation's own current cost — what the *last* request this turn
    made actually sent, in the backend's own tokenizer. Not ``tokens`` above,
    which counts fragments streamed back and was never a real count; this one
    is real, read off ``ChatCompletionResponse``/``ChatCompletionChunk.usage``
    by :mod:`personacore.agent.loop`, which only exists there when the backend
    was asked and answered — llama.cpp does, checked directly, 2026-09-02.
    ``None`` on a backend that never reported one: absent, not estimated,
    because a guess that disagrees with the model is worse than nothing —
    it's the number a person would plan a message's length around."""

    @property
    def tokens_per_second(self) -> float | None:
        """Fragments per second **of generation**, not of the whole turn.

        Measured from the first fragment rather than from the start, on
        purpose: the wait for a model to answer at all is a different fault
        from a model that answers slowly, and averaging the two together hides
        both. ``None`` until there is enough of a turn to divide by.
        """
        if self.total_ms is None or self.first_token_ms is None or self.tokens < 2:
            return None
        generating = (self.total_ms - self.first_token_ms) / 1000.0
        if generating <= 0:
            return None
        return (self.tokens - 1) / generating


def _latency(milliseconds: float | None) -> str:
    """A short duration as an operator reads it.

    Not :func:`~personacore.web.shared._human_gap`, which answers "under a
    second" — true, and useless for the numbers on this line, where the whole
    point is telling 200 ms from 900 ms.
    """
    if milliseconds is None or milliseconds < 0:
        return ""
    if milliseconds < 1000:
        return f"{milliseconds:.0f} ms"
    return f"{milliseconds / 1000:.1f} s"


def _token_usage(prompt_tokens: int | None, context_limit: int | None) -> str:
    """The conversation's own token cost, as an operator reads it.

    ``prompt_tokens`` is what the *last* turn sent to the model — the
    conversation's current cost, not a running total of everything ever said
    (which would be a much larger, and much less useful, number). Empty when
    it was never measured, exactly like every other figure on this bar
    (:class:`TurnMetrics`'s own rule): a conversation with no turns, or one
    answered by a backend that never reports usage, has no line to show, not
    a zero.

    ``context_limit`` is read fresh from the backend
    (:meth:`~personacore.llm.client.LLMClient.context_length`) and is allowed
    to be unknown independently of the count itself — a host with no `/props`
    still has a real prompt-token count to show, it just cannot be measured
    against anything. Showing a percentage against a ceiling this process
    invented would be a number a person could act on that was never true, so
    the denominator is dropped rather than guessed.
    """
    if prompt_tokens is None:
        return ""
    if context_limit:
        percent = round(prompt_tokens / context_limit * 100)
        return f"{prompt_tokens:,} / {context_limit:,} tokens ({percent}%)"
    return f"{prompt_tokens:,} tokens"


# ---------------------------------------------------------------------------
# What a turn cost, persisted — so a reload can say it again
# ---------------------------------------------------------------------------
#
# First token, tokens and first audio used to live only in `_Spoken`, the
# in-memory counters `chat_streaming` fills in as a turn streams, and they
# died with the process the moment the reply was rendered. The owner asked
# for them to survive a reload and be swept with the conversation they measured —
# not kept forever, and not orphaned once their conversation is gone. Written
# onto an `AuditRecord` sharing the turn's own correlation id, the same seam
# `_tool_calls_for` already reads a turn's tool calls back through: no new
# store, no migration, and a purge or a conversation delete that already
# knows how to age out that table ages this out with it.


TURN_METRICS_CATEGORY = AuditCategory.EVENT
"""Which shelf a turn's own timing sits on.

None of :class:`~personacore.audit.models.AuditCategory`'s five members names
this. It is not a tool call, a confirmation, an admin change, or a refused
door — and adding a sixth is not this screen's call to make alone: that enum
lives in :mod:`personacore.audit.models`, which this feature reads from and
does not edit. ``EVENT`` is the least wrong of what already exists, and the
category docstring says the list is a floor, not a ceiling — ``ACCESS`` was
added to it on exactly that reasoning. The reuse stays honest two ways:
``TURN_METRICS_ACTION`` below says plainly what the record actually is, and
nothing that reads a category back — the Logs screen's own
``logs._tools_called``, this package's own ``chat_thread._tool_calls_for`` —
matches on anything looser than ``AuditCategory.TOOL_CALL``, so a turn's
timing can never surface as a tool it did not call.
"""

TURN_METRICS_ACTION = "chat.reply_metrics"
"""The one ``action`` a turn's own timing is filed under, so a caller can find
it back by correlation id without also matching a tool call, a confirmation,
or anything else on the same id."""


def _metrics_detail(metrics: TurnMetrics) -> dict[str, Any]:
    """One turn's timing, as an :class:`~personacore.audit.models.AuditRecord`'s
    ``detail``.

    ``None`` is written as ``None`` — JSON's ``null`` — never as a zero and
    never left out of the dict. A zero would be a claim (see
    :class:`TurnMetrics`); a missing key would make a row written before this
    existed indistinguishable from a turn that measured nothing, and the two
    are different facts once :func:`_metrics_fields` reads this back.

    ``total_ms`` rides along even though nobody asked to see it printed on its
    own: it is what :attr:`TurnMetrics.tokens_per_second` divides by, and it is
    **not** the same number ``chat_thread._fill_reply`` recovers for
    ``duration`` (the gap between two independently-stamped transcript rows).
    That gap is honest enough for a duration an operator reads to the nearest
    tenth of a second, but a live turn's rate is a genuinely different
    quantity — timed with ``time.monotonic()`` from the same clock
    ``first_token_ms`` was, so it is always at least ``first_token_ms``. Two
    wall-clock timestamps written a beat apart are not guaranteed to keep that
    relationship (see ``_fill_reply``'s own docstring on the difference), and
    on a fast turn — the ordinary case with a local model — that beat is
    enough to make the divisor land at or below zero, which
    ``tokens_per_second`` treats as unmeasurable and prints as nothing. A rate
    that was really seen would then vanish on the very first reload. Persisting
    the number the rate was actually computed from is what makes replaying it
    an exact repeat rather than a second, worse measurement.
    """
    return {
        "tokens": metrics.tokens,
        "first_token_ms": metrics.first_token_ms,
        "first_audio_ms": metrics.first_audio_ms,
        "total_ms": metrics.total_ms,
        "prompt_tokens": metrics.prompt_tokens,
    }


def _metrics_fields(
    detail: Mapping[str, Any], *, context_limit: int | None = None
) -> dict[str, str]:
    """``first_token``/``token_rate``/``first_audio``/``token_usage``, from one
    turn's persisted timing.

    Built through the same :class:`TurnMetrics` and :func:`_latency` a fresh
    turn's own line is built from (see :func:`chat_exchange`), so a replayed
    exchange cannot print a different number, or a different shape of number,
    than the turn that made it — ``total_ms`` included, per :func:`_metrics_detail`.

    ``context_limit`` is not part of what was persisted — the backend's
    context window is a fact about *today's* configuration, not about the turn
    that ran months ago, so it is asked fresh by the caller
    (:func:`~personacore.llm.client.LLMClient.context_length`) and handed in
    here rather than read back off ``detail``. A row written before
    ``prompt_tokens`` existed has none in ``detail`` and reads the same as one
    whose write failed — absent, via :func:`_token_usage`'s own rule.
    """
    metrics = TurnMetrics(
        first_token_ms=detail.get("first_token_ms"),
        total_ms=detail.get("total_ms"),
        tokens=int(detail.get("tokens") or 0),
        first_audio_ms=detail.get("first_audio_ms"),
        prompt_tokens=detail.get("prompt_tokens"),
    )
    rate = metrics.tokens_per_second
    return {
        "first_token": _latency(metrics.first_token_ms),
        "token_rate": f"{rate:.0f} tok/s" if rate else "",
        "first_audio": _latency(metrics.first_audio_ms),
        "token_usage": _token_usage(metrics.prompt_tokens, context_limit),
    }


def chat_exchange(
    message: str,
    turn: Any,
    *,
    seconds: float,
    speech: Any = None,
    metrics: TurnMetrics | None = None,
    author: str = "",
    reply_author: str = "",
    attachments: Sequence[AttachmentChip] = (),
    attachment_notice: str = "",
    reasoning: str = "",
    context_limit: int | None = None,
    workspace_files: Sequence[WorkspaceChip] = (),
) -> dict[str, Any]:
    """One finished turn in the shape ``fragments/chat_exchange.html`` renders.

    A copy rather than the runner's own object: the result comes from the
    agent loop, and a template reaching into it would couple this screen to
    whatever that object grows into. It is also the one place "render the
    failure as a sentence, never a traceback" can be enforced.

    ``tools_offered`` stays ``None`` when the runner did not report — ``None``
    and ``0`` are different answers, and the design's line says which.

    **The reply comes back twice, and that is the point.** ``reply`` is the
    plain text the model produced, unchanged; ``reply_html`` is the same text
    rendered through :func:`~personacore.web.markdown.render_markdown` for
    the screen. Rendering in place would destroy the plain form, and the plain
    form is what the voice pipeline speaks — ``**Today**`` read aloud as
    "asterisk asterisk Today" is the failure that costs. Nothing here writes to
    the transcript store either way: the agent loop recorded the model's own
    words before this function ever saw them.

    ``speech`` is a :class:`~personacore.voice.reply.ReplySpeech` — the whole
    of what the voice side exposes to this screen. It is *offered*, never
    synthesised, so a stopped or slow engine costs the audio and cannot reach
    the reply; the URL it carries is checked below before it is printed.

    ``metrics`` is what the turn cost (:class:`TurnMetrics`), and it is
    ``None`` for a turn nobody instrumented — the plain form post, and a
    message read back out of the transcript. Absent numbers print as nothing.

    ``attachments`` is this message's own — attachments.md contract §6a: one
    tile shape for every kind, drawn from :class:`AttachmentChip`. Never the
    reply's — a file the *reply's own turn* produced is a different thing,
    covered next. ``attachment_notice`` is the one sentence this screen
    owes when something about an attachment needs saying out loud — an
    image refused because no connection is known to see (§4.3), or a part
    that could not be kept (§6a/§10) — and is empty for the ordinary turn.

    ``workspace_files`` is what this turn's own tool calls left in the
    conversation's workspace — workspace contract §7, drawn from
    :class:`~personacore.web.screens.chat_workspace.WorkspaceChip`. Handed
    in already built, the same way ``reasoning`` is: a fresh turn gathers the
    names as its tool results arrive
    (:class:`~personacore.web.screens.chat_streaming._Spoken.workspace_files`)
    and turns them into cards here; a replayed turn's own names come back
    through a different seam (``chat_thread._fill_reply`` /
    ``chat.py``'s ``_attach_replay_workspace_files``) and land in this same
    key. Empty for the ordinary turn, which is most of them: a persona with
    no workspace, or one whose tools produced nothing to keep, sends none.

    ``reasoning`` is the model's own thinking, when there was any — the same
    text ``chat.js`` streamed live into the collapsed "Thinking…" line while
    this turn was running (``chat_streaming._stream_one``'s ``reasoning``
    events, accumulated on ``_Spoken.reasoning``). Empty for the ordinary
    reply, which is most of them: a model that never reasons sends no
    ``reasoning`` events at all, so this is ``""`` and the template draws
    nothing extra. It is handed here rather than read back afterwards because
    a fresh turn already has it in memory — the store round trip
    (``chat_streaming._record_reasoning``) is for the *next* time this
    exchange is read, not for rendering it the first time.

    ``author``, ``reply_author`` and ``reply_model`` all arrive here empty and
    are filled in afterwards, by ``chat_exchange._attributed_all``, which reads
    them off the rows the agent loop has just written — because the loop
    learns the model from the response that answered and this object does not
    carry it. ``reply_author`` is the bare name (:func:`author_parts`'
    first half); the model, when there is one, is ``reply_model`` rather than
    riding along in the same string — the reply's header prints one, its
    collapsed bar the other. Empty means there was nothing to say, not a
    guess, and neither is ever re-decorated afterwards.

    ``context_limit`` is the backend's own context window, read fresh by the
    caller (:meth:`~personacore.llm.client.LLMClient.context_length`) — never
    guessed here, and independent of whether ``metrics.prompt_tokens`` itself
    is known: a host with no way to report its ceiling can still have a real
    count to show, just with nothing to measure it against (see
    :func:`_token_usage`).
    """
    offered = _tool_names(getattr(turn, "tools_offered", None))
    called = _tool_names(getattr(turn, "tools_called", None))
    ok = bool(getattr(turn, "ok", False))
    reply = str(getattr(turn, "reply", "") or "")
    timed = metrics or TurnMetrics()
    rate = timed.tokens_per_second
    return {
        "message": message,
        "attachments": list(attachments),
        "attachment_notice": attachment_notice,
        "workspace_files": list(workspace_files),
        # Who said each half (§5.3). Spelled by `author_label` before it got
        # here, so this function cannot disagree with the replayed rendering
        # of the very same message a page load later.
        "author": author,
        "reply_author": reply_author if ok else "",
        # Filled in later, by `chat_exchange._attributed_all`, from the same
        # row `reply_author` is read back from — this function is only ever
        # handed the composed string a caller already had (see its own
        # docstring), never the row itself.
        "reply_model": "",
        "ok": ok,
        "reply": reply,
        "reply_html": render_markdown(reply),
        # Unconditional, like `reply`/`reply_html` above and unlike
        # `reply_author`: the model may have reasoned even on a turn whose
        # final answer came back marked not-ok, and hiding the thinking that
        # led to a failure would be the opposite of what the owner asked this
        # feature to do.
        "reasoning": reasoning,
        "error": None if ok else str(getattr(turn, "error", "") or "The turn produced no reply."),
        # What answered, in the design's model slot. The *model* name is not
        # reported by the runner and is not written to any store — the same gap
        # PERSONA_UNRECORDED describes on the log view — but the persona is, and
        # naming it lets a persona swap be seen to have taken effect (spec §5.5).
        "persona": str(getattr(turn, "persona", "") or PERSONA_UNRECORDED),
        "tools_offered": None if offered is None else len(offered),
        "tools_called": called or [],
        # The same calls with what each one cost. Built here rather than in the
        # template so a turn that was not instrumented is an empty duration
        # beside a name, not a branch in the markup.
        "tool_calls": _tool_calls(called or [], timed.tools),
        # Wall clock around the turn, measured here.
        "duration": _human_gap(timedelta(seconds=seconds)),
        # Where the twenty-five seconds went. Empty strings rather than zeroes
        # for anything that was genuinely not measured — see `TurnMetrics`.
        "first_token": _latency(timed.first_token_ms),
        "token_rate": f"{rate:.0f} tok/s" if rate else "",
        "first_audio": _latency(timed.first_audio_ms),
        "token_usage": _token_usage(timed.prompt_tokens, context_limit),
        "audio_url": _audio_url(speech),
        "audio_report_url": _audio_report_url(_audio_url(speech)),
        "voice_note": getattr(speech, "reason", None),
        "replayed": False,
    }


AUDIO_URL_PREFIX = "/admin/chat/reply/"
"""Where a reply's audio may live, and nowhere else.

The URL arrives on an object built outside this module, and a page that
printed whatever it was handed into a ``src`` would be one bad value away from
fetching audio — or anything else — from somewhere off this host. Checking the
prefix here costs nothing and means the template never has to trust it.
"""


def _audio_url(speech: Any) -> str | None:
    """The reply's audio, if there is any and it is on this surface."""
    if speech is None or not getattr(speech, "can_speak", False):
        return None
    url = str(getattr(speech, "audio_url", "") or "")
    return url if url.startswith(AUDIO_URL_PREFIX) else None


def _audio_report_url(audio_url: str | None) -> str | None:
    """Where the page asks how that audio was produced.

    Derived from the audio URL rather than carried separately, so the two
    cannot name different replies — and derived here rather than in the
    template, where the prefix check above would have to be repeated to keep
    the same promise. A reply with no audio has nothing to report.
    """
    if not audio_url or not audio_url.endswith(".wav"):
        return None
    return audio_url[: -len(".wav")] + ".report"


def _tool_calls(
    names: Sequence[str], timings: Sequence[tuple[str, float | None]]
) -> list[dict[str, str]]:
    """The tools the model called, each with what it cost, in order.

    Paired by position rather than by name, because a model may call the same
    tool twice in one turn and a dictionary keyed by name would report one of
    those calls twice and the other never. Where there are no timings — a turn
    run through the non-streaming path, a message read back out of the
    transcript — every duration is empty and the line reads exactly as it did
    before durations existed.
    """
    out: list[dict[str, str]] = []
    for index, name in enumerate(names):
        took: float | None = None
        if index < len(timings) and timings[index][0] == name:
            took = timings[index][1]
        out.append({"name": name, "took": _latency(took)})
    return out


MAX_TOOL_NAMES_SHOWN = 50
MAX_TOOL_NAME_LENGTH = 120
"""Ceilings on what one turn may print into the page.

Tool names come from plugin manifests, which are operator-supplied but not
core-controlled (spec §7). The template escapes them, so this is not about
injection: it is that a plugin with a thousand tools, or one name a kilobyte
long, must not turn this screen into something unreadable.
"""


def _tool_names(value: object) -> list[str] | None:
    """Normalise a runner's tool list, or ``None`` if it reported none.

    Defensive on purpose: this reads an attribute off an object built outside
    the admin package, so a string, a ``None``, or something not iterable at all
    has to degrade to "not reported" rather than raise inside a page render.
    """
    if value is None or isinstance(value, str | bytes):
        return None
    try:
        items = list(value)  # type: ignore[call-overload]
    except TypeError:
        return None
    return [str(item)[:MAX_TOOL_NAME_LENGTH] for item in items[:MAX_TOOL_NAMES_SHOWN]]


# ---------------------------------------------------------------------------
# One turn that never happened
# ---------------------------------------------------------------------------


def _refused(
    message: str,
    reason: str,
    *,
    author: str = "",
    attachments: Sequence[AttachmentChip] = (),
    attachment_notice: str = "",
    workspace_files: Sequence[WorkspaceChip] = (),
) -> dict[str, Any]:
    """An exchange that never reached the model, in the same shape as one
    that did — so a refusal appears in the conversation where the reply
    would have been, rather than somewhere else on the screen.

    The question keeps its author; the refusal has none, because nobody
    said it — this core declined, and putting a persona's name over that
    would be words in a character's mouth it did not speak.

    ``attachments``/``attachment_notice`` exist for the one case a turn can
    fail *after* a message's attachments were already stored (the model
    raised partway through): what was kept is still shown, exactly as it
    would be beside a reply that succeeded. ``workspace_files`` is the same
    idea for a turn's own tool calls (workspace contract §7) — a stopped or
    broken turn can still have left files behind before it gave out, and
    what was kept there is shown too.
    """
    return {
        "message": message,
        "attachments": list(attachments),
        "attachment_notice": attachment_notice,
        "workspace_files": list(workspace_files),
        "author": author,
        "reply_author": "",
        "reply_model": "",
        "ok": False,
        "reply": "",
        "reply_html": render_markdown(""),
        "reasoning": "",
        "error": reason,
        "persona": PERSONA_UNRECORDED,
        "tools_offered": None,
        "tools_called": [],
        "tool_calls": [],
        "duration": "",
        "first_token": "",
        "token_rate": "",
        "first_audio": "",
        "token_usage": "",
        "audio_url": None,
        "audio_report_url": None,
        "voice_note": None,
        "replayed": False,
    }
