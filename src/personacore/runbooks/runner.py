"""Running a runbook — contract §3, and §5's half of resume.

This is the organ that actually *does* something. :mod:`.state` holds what a
run is and :mod:`.roles` resolves what a step's files are called; everything
that talks to a plugin tool, to the model, to the workspace or to the chat's
transcript is here.

**A run is a background task, not a request.** :meth:`Runner.start` writes
``.run.json``, schedules the step loop with :func:`asyncio.create_task` and
returns; nothing about the run depends on a browser being attached to it. That
is the same posture ADR-0043 took for an ordinary streamed turn, reached from
the other side: there, a turn was lifted *out* of the HTTP response so a
sleeping tablet could not end it; here, a turn never had a request to begin
with. What both rest on is the one fact the ADR turns on — **the transcript is
written when the turn completes, by the agent loop itself**, so a turn nobody
watched is recorded exactly like one somebody did.

**Gates park, and are woken from outside.** A run that reaches a human gate
parks and its task ends — there is no coroutine sitting on an
:class:`asyncio.Event` waiting for a person, because a person may answer in
four days and through a different container than the one that asked. So
:meth:`Runner.answer` and :meth:`Runner.answer_text` rebuild the run from
``.run.json`` exactly the way :meth:`Runner.resume` does, and start a fresh
step-loop task from the step after the gate. What a gate *is* — the questions,
the answer lines, the typed conditions — is :mod:`personacore.runbooks.gates`;
what it does to a run is here.

**A runbook-level ``foreach`` is N runs, not one long one.** The run started
in the conversation somebody pressed Start in is a *parent*: it has no steps
of its own, it holds one :class:`~personacore.runbooks.state.ItemState` per
item, and it starts an ordinary run per item in a conversation of its own,
one after another, waiting for each. That is contract §1.12 read the way the
plan settled it — a single conversation looping twelve chapters would carry
every earlier chapter in the prompt of the last one. A **step**-level
``foreach`` is the other shape and stays inside one run: one tool step,
repeated per item, its roles holding a list. **One item is not a parent**
(owner, 2026-09-06): when the ``foreach`` input resolves to exactly one
value, :meth:`Runner.start` runs it as an ordinary single run in the
conversation the person is already in, with no parent, no item line and no
Open link.

**A run's scripted turn is a person's turn** (owner's decision, 2026-09-05).
It used to be a thinner thing: this module drove the chat runner itself and
read the events with :func:`_watched`, which understood ``text`` and discarded
every other kind, registered nothing a page could attach to, and wrote neither
the metrics record nor the reasoning record the chat page draws its chrome
from. A conversation a run was working in therefore showed a prompt and then,
after a refresh, a bare reply. That is fixed by *not having a second
implementation*: :meth:`Runner._streamed` hands the turn to the chat screen's
own engine (``app.state.chat_turn_engine``, ``web/screens/chat_turns.py``),
which registers it, streams it and records it exactly as a typed message's turn
is. What stays here is only what is a run's: the output ceiling, the deadline,
and the partial file written from what arrived before either tripped.

The direction still holds — :mod:`personacore.web` may import this package,
and this package may never import :mod:`personacore.web`. The engine is read
off ``app.state`` and called duck-typed, exactly as
``web/screens/chat_run.py`` reads ``app.state.runner`` from the other side, so
no import crosses. :func:`_watched` survives as the fallback for a core with no
chat screen (every runner the tests build): the run runs identically and simply
cannot be watched.

Two things here are still deliberately *not* shared:

* The plain sentences a refusal shows. They match the Runbooks screen's own
  (``web/screens/runbooks.py``) because they are answers to the same question.
* The registry of what is running. A runbook run and a chat turn are keyed
  differently — one conversation, one run, for its whole life — so they are
  counted separately rather than folded together.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from personacore.agent.loop import save_tool_files
from personacore.agent.protocols import ToolProvider, ToolSpec
from personacore.audit.models import (
    Author,
    AuthorKind,
    MessageRole,
    Owner,
    Surface,
    TranscriptRecord,
)
from personacore.config.appdata import AppdataLayout
from personacore.config.workspace import WorkspaceSettings
from personacore.contracts.policy import RiskLevel
from personacore.conversations.models import Conversation
from personacore.runbooks import gates as gate_tools
from personacore.runbooks import roles as role_tools
from personacore.runbooks import state as run_state
from personacore.runbooks.schema import (
    ITERABLE_INPUT_TYPES,
    GateStep,
    ModelStep,
    Runbook,
    ToolStep,
)
from personacore.runbooks.state import GateState, RunState, StepStatus
from personacore.runbooks.store import RunbookStore
from personacore.runbooks.validate import ValidationError, template_roles, validate_runbook
from personacore.workspace_tools import WorkspaceTools
from personacore.workspaces import FILENAME_PATTERN, Workspace, WorkspaceError

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# The sentences a person is shown
# ---------------------------------------------------------------------------

RUNS_OFF = "Runbook runs are off. Turn them on in Core settings."
"""Contract §1.9: the household-wide switch. The same sentence the Runbooks
screen's banner already uses, because it is the answer to the same question —
and it is checked on every start, every resume and every boot-time offer."""

PLUGIN_OFF = "Runbook runs are off for this plugin. Turn them on on its plugin page."
"""Contract §1.10: the per-plugin switch, which the core switch overrides."""

WAITING_FOR_ANSWERS = "waiting for answers"
"""Contract §4: a human gate parks the run until a person answers it. The same
reason for click-option questions and for the typed fallback, because it is
the same wait — which of the two the chat should draw is
:meth:`Runner.awaiting_text`'s answer, read off the gate itself, and not
something to be inferred from a sentence."""

NOT_AT_A_GATE = "This run is not waiting for an answer."
"""What answering a run that is not parked at a gate is refused with — a page
left open while somebody else answered, most often."""

NOT_AWAITING_TEXT = "This run is waiting for you to pick an answer, not to type one."
"""Contract's text-answer handshake: the composer only routes a typed message
to a gate while :meth:`Runner.awaiting_text` is true, so a message arriving
any other time is refused rather than guessed at."""

SKIPPED = "skipped"
"""Contract §1.11: the reason a step nobody ran carries. It is marked *done*
rather than given a status of its own, because "done" is what the step loop
already means by "do not run this, carry on past it" — and because a run's
progress is a question about what is left, which a fourth kind of finished
step would only make harder to answer."""

_CHILD_POLL_SECONDS = 0.25
"""How often a parent run looks at the item it is waiting for.

A parent is not doing anything else, and what it is waiting for is minutes to
hours long, so the interval only has to be short enough that the next item
starts without a person noticing the join. Polling (rather than waiting on an
event) is deliberate: the child may be running in this process or may have
been left parked by a previous one, and ``.run.json`` is the only thing both
of those have in common."""

_ITEM_ENDED = frozenset({"done", "stopped", "failed", "parked"})
"""The item statuses a parent does not revisit. A parent picked up again
after a restart starts from the first item that is not one of these."""

STATE_UNREADABLE = "run state unreadable"
"""Contract §5: a torn ``.run.json`` is reported, never silently reset. The
unreadable file is kept beside the new one — see :meth:`Runner.scan_at_boot`."""


class RunRefused(Exception):
    """A run could not be started or resumed, and here is the sentence to show.

    Raised rather than returned because every caller is a route that has to
    turn it into a message on a screen, and a boolean would make "off",
    "greyed" and "that input is not a number" the same answer.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class _StepFailed(Exception):
    """One step could not finish. Internal: it is caught by the step loop,
    which turns it into ``state.fail`` plus a progress row. The message is the
    reason a person reads, so it is written as one."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# One live run
# ---------------------------------------------------------------------------


@dataclass
class _Live:
    """What this process is holding for one running run.

    Only the *live* parts: everything durable is in ``.run.json``, which is
    why a container restart loses this object and loses nothing else.
    """

    conversation: Conversation
    owner: Owner
    plugin: str
    runbook: Runbook
    folder: Path
    """The runbook file's own directory — where a step's ``prompt:`` is
    resolved from (contract §2: "relative to the runbook file")."""

    state: RunState
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    iterating: Any = None
    """The item a step-level ``foreach`` is on, while it is on one — what
    ``{{ item }}`` resolves to (contract §1.12). ``None`` at every other
    moment, including between two items, so a template outside an iterating
    step can never quietly pick up the last one."""

    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id


class Runner:
    """Runs runbooks. One per application, on ``app.state.runner``.

    ``settings_enabled`` and ``plugin_enabled`` are callables rather than
    values for the reason :class:`~personacore.runbooks.store.RunbookStore`
    already takes one: a switch saved on the Core settings screen has to be in
    force on the next call, with no restart, so it is read at the moment it
    matters and never captured.

    ``audit`` and ``workspaces`` are keyword-only additions to the interface
    the plan fixed. Neither is reachable from the web layer — nothing but
    ``server.py`` constructs this — and both are things the run genuinely
    needs: a run writes progress rows to the transcript store, and it writes
    files through the same :class:`~personacore.workspace_tools.WorkspaceTools`
    the agent loop was handed, so the ceilings a run writes under are the
    household's own rather than this module's idea of them. Omitting either
    gives a runner that still starts, still refuses correctly and simply
    cannot record progress — which is what the tests that do not care about
    progress want.

    ``app`` is where the chat screen's turn engine lives
    (``app.state.chat_turn_engine``), and it is what makes a run's scripted
    turn a turn like any other: registered where a page can attach to it,
    streaming the same frames, leaving the same metrics and reasoning records
    on the reply row. Read at the moment a turn is taken and never captured,
    the same shape ``settings_enabled`` takes, because the admin surface is
    mounted after this object is built. ``None`` — or an application with no
    chat screen on it — falls back to :meth:`_streamed`'s own reader: the run
    still runs and its rows are still written, it simply cannot be watched.
    """

    def __init__(
        self,
        layout: AppdataLayout,
        store: RunbookStore,
        chat: Any,
        tools: ToolProvider | None,
        conversations: Any,
        settings_enabled: Callable[[], bool],
        plugin_enabled: Callable[[str], bool],
        *,
        audit: Any | None = None,
        workspaces: WorkspaceTools | None = None,
        app: Any | None = None,
    ) -> None:
        self._layout = layout
        self._store = store
        self._chat = chat
        self._tools = tools
        self._conversations = conversations
        self._settings_enabled = settings_enabled
        self._plugin_enabled = plugin_enabled
        self._audit = audit
        self._workspaces = workspaces or WorkspaceTools(layout, WorkspaceSettings())
        self._app = app
        self._live: dict[str, _Live] = {}

    # -- the four things a screen asks -------------------------------------

    async def start(
        self,
        *,
        owner: Owner,
        plugin: str,
        runbook_id: str,
        inputs: Mapping[str, Any],
        persona: str | None,
        skip: Iterable[str] = (),
    ) -> RunState:
        """Begin a run: a new conversation, ``.run.json``, and the first step.

        Every refusal happens before anything is created, so a refused start
        leaves no conversation, no workspace and no state file behind. The
        order is the order contract §3 names: the core switch, the plugin's
        switch, the compatibility verdict, then the inputs — and then
        ``skip``, which is checked last because its refusal is about what the
        *steps* need of each other and is only worth asking once the runbook
        is known to be runnable at all.

        ``skip`` is contract §1.11: the picker's per-step checkboxes. A
        skipped step is marked done with the reason
        :data:`SKIPPED` and produces nothing, so a later step that pins one of
        its roles would fail four steps in — which is why that is refused
        here, before a conversation exists, with the sentence naming both
        steps and the role.

        A runbook with ``foreach:`` (contract §1.12) starts a **parent run**
        instead: this conversation holds the item list and one progress row
        per item, and each item gets an ordinary run of its own in its own
        conversation. See :meth:`_start_parent`. **Except when the input
        resolves to exactly one item** (owner, 2026-09-06): that is an
        ordinary single run in this conversation, with no parent, no item
        line and no Open link — a parent exists only for two items or more.
        """
        self._require_switches(plugin)
        runbook, folder = self._loaded(plugin, runbook_id)
        checked = _checked_inputs(runbook, inputs)
        skipped = _checked_skip(runbook, skip)

        if runbook.foreach is not None:
            name = runbook.foreach
            values = checked.get(name)
            if isinstance(values, list) and len(values) == 1:
                # One item is not a foreach in anything a person can see: no
                # parent, no item row, no second conversation to link to. The
                # value is folded into `inputs` exactly as `_start_item` folds
                # it for a parent's own children, and the run proceeds in this
                # same conversation as if `foreach:` had never been there —
                # except that the conversation still carries the runbook's
                # title, the way a parent's would, since this is the
                # conversation the person landed in from the picker.
                return await self._start_one(
                    owner=owner,
                    plugin=plugin,
                    runbook=runbook,
                    folder=folder,
                    inputs={**checked, name: values[0]},
                    persona=persona,
                    skip=skipped,
                    title=runbook.title,
                )
            return await self._start_parent(
                owner=owner,
                plugin=plugin,
                runbook=runbook,
                folder=folder,
                inputs=checked,
                persona=persona,
                skip=skipped,
            )
        return await self._start_one(
            owner=owner,
            plugin=plugin,
            runbook=runbook,
            folder=folder,
            inputs=checked,
            persona=persona,
            skip=skipped,
        )

    # -- one run, and the parent that may own it ---------------------------
    #
    # Everything from here to `state` is the iteration half of contract §1.12
    # and §1.11. `_start_one` is what `start` used to be inline; the parent
    # machinery around it exists because a runbook-level `foreach` is *not* a
    # loop inside one run — it is N ordinary runs, one per item, each in its
    # own conversation, with a parent run holding the list and waiting.

    async def _start_one(
        self,
        *,
        owner: Owner,
        plugin: str,
        runbook: Runbook,
        folder: Path,
        inputs: Mapping[str, Any],
        persona: str | None,
        skip: Sequence[str],
        parent: str | None = None,
        item: Any = None,
        title: str | None = None,
    ) -> RunState:
        """One ordinary single-item run: a conversation, a state file, a task.

        ``parent``/``item`` are set only when this run is one item of a
        parent's list; they are written into ``.run.json`` so the run can say
        where it came from after a restart, when the parent's own memory of it
        is gone. ``title`` renames the new conversation and is left ``None``
        for a run somebody started themselves — a conversation gets its name
        from what was said in it, and only a run nobody will ever type in
        needs one imposed.
        """
        conversation = await self._conversations.start(owner)
        if conversation is None:
            raise RunRefused("This core could not open a conversation for the run.")

        state = run_state.new_state(
            runbook,
            plugin=plugin,
            persona=persona or runbook.persona or "",
            inputs=inputs,
        )
        state.parent = parent
        state.item = item
        _mark_skipped(state, skip)
        live = _Live(
            conversation=conversation,
            owner=owner,
            plugin=plugin,
            runbook=runbook,
            folder=folder,
            state=state,
        )
        self._write(live)
        self._live[live.conversation_id] = live
        if title is not None:
            await self._retitled(owner, live.conversation_id, title)
        if live.state.current is None and live.state.status == "running":
            # Every step was skipped (`_mark_skipped`): there is nothing for
            # the step loop to do, so this finishes the run itself rather
            # than leaving it "running" with nothing pending until the task
            # below gets its first turn — a caller reading the `RunState`
            # this method returns should see a done run, not one that will
            # become done shortly.
            await self._finish_live(live)
        live.task = asyncio.create_task(
            self._drive(live), name=f"runbook-{runbook.runbook}-{live.conversation_id}"
        )
        return _with_conversation(state, live.conversation_id)

    async def _start_parent(
        self,
        *,
        owner: Owner,
        plugin: str,
        runbook: Runbook,
        folder: Path,
        inputs: Mapping[str, Any],
        persona: str | None,
        skip: Sequence[str],
    ) -> RunState:
        """The run that owns the items — contract §1.12, one conversation per
        item.

        A parent has **no steps of its own**: its ``steps`` list is empty and
        its ``items`` list is the whole of it. That is deliberate rather than
        an omission — a parent that also held step states would be two
        different documents in one file, and every reader would have to know
        which of the two it was looking at. ``items`` is not empty exactly
        when this is a parent, and that is the only test anything needs.
        """
        name = runbook.foreach or ""
        values = inputs.get(name)
        if not isinstance(values, list) or not values:
            raise RunRefused(f"{name} is needed before this run can start.")

        conversation = await self._conversations.start(owner)
        if conversation is None:
            raise RunRefused("This core could not open a conversation for the run.")

        state = run_state.new_state(
            runbook,
            plugin=plugin,
            persona=persona or runbook.persona or "",
            inputs=inputs,
        )
        state.steps = []
        state.current = None
        state.items = [run_state.ItemState(value=value) for value in values]
        live = _Live(
            conversation=conversation,
            owner=owner,
            plugin=plugin,
            runbook=runbook,
            folder=folder,
            state=state,
        )
        self._write(live)
        self._live[live.conversation_id] = live
        await self._retitled(owner, live.conversation_id, runbook.title)
        live.task = asyncio.create_task(
            self._drive_parent(live, list(skip)),
            name=f"runbook-parent-{runbook.runbook}-{live.conversation_id}",
        )
        return _with_conversation(state, live.conversation_id)

    async def _drive_parent(self, live: _Live, skip: Sequence[str]) -> None:
        """The parent's background task — the same shape as :meth:`_drive`,
        and wrapped for the same reason: it is a bare task, and anything that
        escapes it would leave a run that merely stops looking like one."""
        try:
            await self._items(live, skip)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a run never crashes the core
            log.error("runbook_parent_failed", error=repr(exc))
            live.state = run_state.park(live.state, "the run stopped unexpectedly.")
            self._write(live)
        finally:
            if self._live.get(live.conversation_id) is live:
                del self._live[live.conversation_id]

    async def _items(self, live: _Live, skip: Sequence[str]) -> None:
        """Every item, in order: start its run, wait for it, record it.

        The item is re-read out of ``live.state`` on every line that touches
        it rather than held in a local, because :meth:`stop` replaces
        ``live.state`` wholesale with a stopped copy — a local
        :class:`~personacore.runbooks.state.ItemState` would go on writing
        into the state nobody is going to save.
        """
        label = live.runbook.foreach or "item"
        for index in range(len(live.state.items)):
            if live.stopping.is_set():
                return
            # Contract §1.9, the same question :meth:`_steps` asks between
            # steps: turning the switch off stops the run at the end of the
            # work in flight. For a parent, the work in flight is one item.
            if not self._settings_enabled():
                live.state = run_state.park(live.state, RUNS_OFF)
                self._write(live)
                return
            if live.state.items[index].status in _ITEM_ENDED:
                continue
            if live.state.items[index].conversation_id is None:
                started = await self._start_item(live, index, skip, label)
                if not started:
                    continue
            child_id = live.state.items[index].conversation_id or ""
            status, reason = await self._awaited(live, child_id)
            item = live.state.items[index]
            item.status = status
            item.reason = reason
            live.state.updated = datetime.now(UTC).isoformat()
            self._write(live)
            await self._say(live, _item_row(label, item))
            if live.stopping.is_set():
                return
        if live.stopping.is_set() or live.state.status != "running":
            return
        live.state.status = "done"
        live.state.current = None
        live.state.updated = datetime.now(UTC).isoformat()
        self._write(live)
        await self._say(live, f"{len(live.state.items)} of {len(live.state.items)} done")

    async def _start_item(self, live: _Live, index: int, skip: Sequence[str], label: str) -> bool:
        """One item's own run, in its own conversation. ``False`` when it
        could not be started at all, which is the item's failure and not the
        parent's — the remaining items still run."""
        value = live.state.items[index].value
        try:
            child = await self._start_one(
                owner=live.owner,
                plugin=live.plugin,
                runbook=live.runbook,
                folder=live.folder,
                inputs={**live.state.inputs, (live.runbook.foreach or ""): value},
                persona=live.state.persona or None,
                skip=skip,
                parent=live.conversation_id,
                item=value,
                title=f"{live.runbook.title} — {value}",
            )
        except RunRefused as exc:
            item = live.state.items[index]
            item.status = "failed"
            item.reason = exc.message
            self._write(live)
            await self._say(live, _item_row(label, item))
            return False
        item = live.state.items[index]
        item.conversation_id = getattr(child, "conversation_id", None)
        item.status = "running"
        self._write(live)
        await self._say(live, _item_row(label, item))
        return True

    async def _awaited(self, live: _Live, child_id: str) -> tuple[str, str | None]:
        """Wait for one item's run to end, and say how it ended.

        A child **parked at a gate is not an ending**: a person is being asked
        something, and the parent's job is to wait rather than to call the
        item finished and move on to the next chapter while this one is still
        half-edited. Any other park — a failed step, a switch turned off — is
        an ending, recorded on the item with the child's own reason, and the
        parent goes on to the next item rather than abandoning the rest of the
        book for one bad chapter.

        Read through :meth:`state`, so it is this process's own memory when
        the child is running here and ``.run.json`` when it is not.
        """
        while True:
            if live.stopping.is_set():
                # Contract §3's Stop, one level up: stopping the parent stops
                # the child it is inside, or the person would press Stop and
                # watch a chapter carry on regardless.
                with contextlib.suppress(RunRefused):
                    await self.stop(live.owner, child_id)
                return "stopped", None
            child = await self.state(child_id)
            if child is None:
                return "failed", "its run could not be read."
            if child.status != "running" and not _still_answerable(child):
                return str(child.status), child.reason
            await asyncio.sleep(_CHILD_POLL_SECONDS)

    async def _resume_parent(self, owner: Owner, conversation_id: str, state: RunState) -> bool:
        """Contract §5 for a parent: resume the item it was on.

        Two halves, and either may be the one that matters. The item's own
        conversation is resumed first — that is the run that actually has work
        left in it. Then, if this process is not already driving the parent
        (it died with the container), the parent's own loop is started again
        from the first item that has not ended.

        **A skip list does not survive a restart.** It is applied to each
        run's state when that run starts, so every item already started keeps
        its skips; items this parent has not reached yet run whole. Recording
        it would need a field in ``.run.json`` that the state format does not
        have.
        """
        item = next((one for one in state.items if one.status not in _ITEM_ENDED), None)
        resumed = False
        if item is not None and item.conversation_id:
            with contextlib.suppress(RunRefused):
                resumed = await self.resume(owner, item.conversation_id)
        if self._live.get(conversation_id) is not None:
            return resumed

        runbook, folder = self._loaded(state.plugin, state.runbook)
        conversation = await self._conversations.resolve(owner, conversation_id=conversation_id)
        if conversation is None:
            raise RunRefused("That conversation could not be opened, so the run cannot resume.")
        state.status = "running"
        state.reason = None
        live = _Live(
            conversation=conversation,
            owner=owner,
            plugin=state.plugin,
            runbook=runbook,
            folder=folder,
            state=state,
        )
        self._write(live)
        self._live[conversation_id] = live
        live.task = asyncio.create_task(
            self._drive_parent(live, ()), name=f"runbook-parent-resume-{conversation_id}"
        )
        return True

    async def _retitled(self, owner: Owner, conversation_id: str, title: str) -> None:
        """Name a conversation a run made, best-effort.

        Contract §1.12 asks for the parent to carry the runbook's title and
        each item's conversation to carry "<runbook> — <item>", which is how a
        person tells eleven chapter conversations apart in a list. Failing to
        rename one costs a good name and nothing else, so it never costs the
        run — the same posture every other write-to-somebody-else's-store in
        this module takes.
        """
        rename = getattr(self._conversations, "rename", None)
        if rename is None:
            return
        with contextlib.suppress(Exception):
            await rename(owner, conversation_id, title)

    def dependents_of(self, runbook: Runbook | Any, step_id: str) -> list[str]:
        """The steps that need a role only ``step_id`` produces — contract
        §1.11's other half, and the reason the picker can grey a checkbox
        instead of letting somebody tick it and be refused at Start.

        Accepts either a parsed ``Runbook`` or an object with a ``path``
        attribute (e.g. ``RunbookRecord``); parses the file if needed.
        Returns ``[]`` if the file cannot be read or does not validate.

        Only roles *nothing else* produces count: a role two steps both leave
        behind is still there when one of them is skipped, so skipping it
        breaks nothing and the checkbox stays live.
        """
        # If the runbook doesn't have steps, it's likely a record with a path;
        # parse the file.
        if not hasattr(runbook, "steps"):
            try:
                path = getattr(runbook, "path", None)
                if path is None:
                    return []
                text = Path(path).read_text(encoding="utf-8")
                raw = yaml.safe_load(text)
                runbook = Runbook.model_validate(raw)
            except Exception:  # noqa: BLE001 - a broken dependency read enables a box
                return []

        step = next((one for one in runbook.steps if one.id == step_id), None)
        if step is None:
            return []
        others = [one for one in runbook.steps if one.id != step_id]
        elsewhere = {role for one in others for role in _roles_made(one)}
        only_here = _roles_made(step) - elsewhere
        return [one.id for one in others if _roles_needed(one) & only_here]

    async def state(self, conversation_id: str) -> RunState | None:
        """This conversation's run, or ``None``.

        From memory when the run is this process's own, from ``.run.json``
        otherwise — a run parked before a restart is still a run, and the page
        that offers Resume is reading it off disk.
        """
        s = self._state_now(conversation_id)
        return None if s is None else _with_conversation(s, conversation_id)

    def is_running(self, conversation_id: str) -> bool:
        """Whether a run is executing here right now — what the chat screen
        locks the composer on (contract §3: a person's message can never
        interleave with a run's turns)."""
        live = self._live.get(conversation_id)
        return live is not None and live.state.status == "running"

    async def stop(self, owner: Owner, conversation_id: str) -> bool:
        """Contract §3: stop the run, and the reply it is in the middle of.

        ``True`` when something was stopped. The flag is set *and* the state is
        written here rather than left to the step loop, so the composer is
        usable the moment the control is pressed instead of whenever the model
        next produces a token. Every file stays.
        """
        live = self._live.get(conversation_id)
        if live is not None:
            if live.owner.id != owner.id:
                return False
            live.stopping.set()
            live.state = run_state.stop(live.state)
            self._write(live)
            return True
        # A run this process did not start — parked before a restart, most
        # likely. There is no turn to interrupt; the state is all there is.
        current = await self.state(conversation_id)
        if current is None or current.status not in {"running", "interrupted"}:
            return False
        run_state.write_state(self._workspace_dir(conversation_id), run_state.stop(current))
        return True

    async def resume(self, owner: Owner, conversation_id: str) -> bool:
        """Contract §5: restart the step a run died or parked on.

        The interrupted attempt's own output is **renamed, never deleted**
        (``<name>.interrupted.N``), and the step starts again from its
        predecessor's files. Refused with :data:`RUNS_OFF` while the core
        switch is off — the same sentence Start uses, because it is the same
        refusal.
        """
        if not self._settings_enabled():
            raise RunRefused(RUNS_OFF)
        current = await self.state(conversation_id)
        if current is None or current.status not in {"interrupted", "parked"}:
            return False
        if not self._plugin_enabled(current.plugin):
            raise RunRefused(PLUGIN_OFF)
        if self.is_running(conversation_id):
            return False
        # A parent run has no steps of its own to restart; what it resumes is
        # the item it was on. Asked here, before `plan_resume`, which would
        # have nothing to plan from.
        if current.items:
            return await self._resume_parent(owner, conversation_id, current)

        step_id, partials = run_state.plan_resume(current)
        workspace = self._workspaces.workspace_for(conversation_id)
        for name in partials:
            _set_aside(workspace, name)

        runbook, folder = self._loaded(current.plugin, current.runbook)
        step = current.step(step_id)
        if step is not None:
            step.status = StepStatus.PENDING
            step.outputs = {}
            step.reason = None
            step.finished = None
        current.status = "running"
        current.current = step_id
        current.reason = None

        live = self._live.get(conversation_id)
        conversation = live.conversation if live is not None else None
        if conversation is None:
            conversation = await self._conversations.resolve(owner, conversation_id=conversation_id)
        if conversation is None:
            raise RunRefused("That conversation could not be opened, so the run cannot resume.")

        live = _Live(
            conversation=conversation,
            owner=owner,
            plugin=current.plugin,
            runbook=runbook,
            folder=folder,
            state=current,
        )
        self._write(live)
        self._live[conversation_id] = live
        live.task = asyncio.create_task(self._drive(live), name=f"runbook-resume-{conversation_id}")
        return True

    async def scan_at_boot(self) -> int:
        """Contract §5: find the runs this core was in the middle of.

        A run that was ``running`` when the process died is marked
        ``interrupted`` and left for a person to resume; **nothing restarts on
        its own**. A ``.run.json`` that cannot be read is parked with
        :data:`STATE_UNREADABLE` and the unreadable file is kept beside the
        new one as ``.run.json.unreadable``, because contract §5 forbids a
        silent reset and a state nobody can parse cannot be repaired.

        Returns how many runs were marked interrupted. **Never raises** — this
        is called while the core is coming up, and a stray run must not be able
        to stop the listener.
        """
        root = self._layout.workspaces
        if not root.is_dir():
            return 0
        marked = 0
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            try:
                found = run_state.read_state(folder)
            except run_state.StateUnreadable as exc:
                self._park_unreadable(folder, exc.reason)
                continue
            except Exception as exc:  # noqa: BLE001 - a run never stops the boot
                log.warning("runbook_boot_scan_failed", error=repr(exc))
                continue
            if found is None or found.status != "running":
                continue
            try:
                run_state.write_state(folder, run_state.mark_interrupted(found))
            except Exception as exc:  # noqa: BLE001 - ditto
                log.warning("runbook_interrupt_write_failed", error=repr(exc))
                continue
            marked += 1
        if marked:
            log.info("runbook_runs_interrupted", count=marked)
        return marked

    # -- refusals ----------------------------------------------------------

    def _require_switches(self, plugin: str) -> None:
        """Contract §3, "switch first": the core one, then the plugin's. Core
        off overrides everything, so it is asked first and its sentence is the
        one shown."""
        if not self._settings_enabled():
            raise RunRefused(RUNS_OFF)
        if not self._plugin_enabled(plugin):
            raise RunRefused(PLUGIN_OFF)

    def _loaded(self, plugin: str, runbook_id: str) -> tuple[Runbook, Path]:
        """The parsed runbook and the folder its prompt files live in.

        The store lists records and reports a verdict; it has no "give me this
        one, parsed" call, so this finds the record and re-validates the file
        behind it. Re-validating rather than trusting ``record.valid`` is not
        belt and braces — :meth:`~personacore.runbooks.store.RunbookStore.list`
        answers with the *facts* about a file, and what a run needs is the
        :class:`~personacore.runbooks.schema.Runbook` object itself.

        The compatibility verdict is checked **here, at start**, and not only
        at upload: a plugin can be removed, switched off or downgraded between
        the two (contract §6, "again at start").
        """
        record = next(
            (r for r in self._store.list() if r.plugin == plugin and r.id == runbook_id),
            None,
        )
        if record is None:
            raise RunRefused(f"There is no runbook called {runbook_id} for {plugin}.")
        if not record.valid:
            problem = record.problems[0] if record.problems else "it did not validate."
            raise RunRefused(f"That runbook cannot run: {problem}")
        if not record.verdict.ok:
            reasons = "; ".join(record.verdict.reasons) or "it cannot run here."
            raise RunRefused(f"That runbook cannot run: {reasons}")
        folder = record.path.parent
        try:
            text = record.path.read_text(encoding="utf-8")
            runbook = validate_runbook(text, _prompt_files(folder))
        except (OSError, ValidationError) as exc:
            raise RunRefused(f"That runbook could not be read: {exc}") from exc
        return runbook, folder

    # -- the step loop -----------------------------------------------------

    async def _drive(self, live: _Live) -> None:
        """Every step, in order, until the run ends or parks.

        Wrapped whole: this is a bare task, so anything that escapes it would
        surface as "task exception was never retrieved" and the run would
        simply stop looking like it was ever running. A failure here parks the
        run with a sentence instead.
        """
        try:
            await self._steps(live)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a run never crashes the core
            log.error("runbook_run_failed", error=repr(exc))
            live.state = run_state.park(live.state, "the run stopped unexpectedly.")
            self._write(live)
        finally:
            if self._live.get(live.conversation_id) is live:
                del self._live[live.conversation_id]

    async def _steps(self, live: _Live) -> None:
        by_id = {step.id: step for step in live.runbook.steps}
        while True:
            if live.stopping.is_set():
                return
            # Contract §1.9: turning the switch off mid-run stops the run at
            # the end of its current step and keeps every file. Asked between
            # steps, which is exactly where "the end of its current step" is.
            if not self._settings_enabled():
                live.state = run_state.park(live.state, RUNS_OFF)
                self._write(live)
                return
            step_id = live.state.current
            if step_id is None:
                if live.state.status == "running":
                    # Reached with nothing run at all — every step was
                    # skipped at start (`_mark_skipped`), or the runbook has
                    # no steps at all — since the ordinary way a run finishes
                    # calls :meth:`_finish` itself, from inside this loop,
                    # below.
                    await self._finish_live(live)
                return
            if live.state.status != "running":
                return
            step = by_id.get(step_id)
            if step is None:
                live.state = run_state.fail(
                    live.state, step_id, f"step {step_id} is not in this runbook any more."
                )
                self._write(live)
                return

            if isinstance(step, GateStep):
                # Contract §4. The whole of it is below, in one delimited
                # block; a gate either lets the loop carry on (an auto gate
                # that passed, or one sending the run back round) or ends this
                # task, having parked the run for a person or for good.
                if await self._gate(live, step):
                    continue
                return

            self._begin(live, step_id)
            label = _label(step)
            kind_suffix = "" if getattr(step, "title", None) else f": {step.kind}"
            await self._say(live, f"{label}{kind_suffix} — running")
            started = time.perf_counter()
            try:
                if isinstance(step, ToolStep):
                    # Contract §1.12: a tool step with `foreach:` runs once
                    # per item and leaves a list under each of its roles.
                    outputs = (
                        await self._tool_step_per_item(live, step)
                        if step.foreach is not None
                        else await self._tool_step(live, step)
                    )
                else:
                    outputs = await self._model_step(live, step)
            except _StepFailed as exc:
                live.state = run_state.fail(live.state, step_id, exc.reason)
                self._write(live)
                await self._say(live, f"{label}: failed — {exc.reason}")
                return
            if live.stopping.is_set():
                return
            live.state = run_state.advance(live.state, step_id, outputs)
            self._write(live)
            await self._say(live, f"{label}: done, {_took(started)}, {self._sizes(live, outputs)}")
            if live.state.status != "running":
                if live.state.status == "done":
                    await self._finish_live(live)
                return

    # -- a tool step, once or once per item ---------------------------------

    async def _tool_step_per_item(self, live: _Live, step: ToolStep) -> dict[str, list[str]]:
        """Contract §1.12: this one step, once per item, in the order given.

        Everything a single run of the step does is unchanged —
        :meth:`_tool_step` is called as it always is — and the only difference
        is that ``{{ item }}`` resolves while it runs and that each role
        collects one filename per item instead of one filename. The item is
        put on ``live`` rather than passed down through three signatures
        because :meth:`_substituted` is where it is needed and is shared with
        model steps; it is cleared in a ``finally``, so nothing outside this
        loop can ever see an ``{{ item }}`` resolve.

        A failure names the item ("item 3: matched 0 files.") and fails the
        whole step: eleven chapters stamped and one missed is not a step that
        half-worked, it is a step whose next reader would be wrong about what
        it left behind.
        """
        items = _items_of(live.state, step.foreach or "")
        collected: dict[str, list[str]] = {role: [] for role in step.files}
        try:
            for value in items:
                if live.stopping.is_set():
                    return collected
                started = time.perf_counter()
                live.iterating = value
                try:
                    outputs = await self._tool_step(live, step)
                except _StepFailed as exc:
                    raise _StepFailed(f"item {value}: {exc.reason}") from exc
                for role, name in outputs.items():
                    collected.setdefault(role, []).append(name)
                await self._say(
                    live,
                    f"{_label(step)} [{value}]: done, {_took(started)}, "
                    f"{self._sizes(live, outputs)}",
                )
        finally:
            live.iterating = None
        return collected

    async def _tool_step(self, live: _Live, step: ToolStep) -> dict[str, str]:
        """Contract §2: one plugin tool, fixed arguments, files by role.

        The call goes through the composite ``ToolProvider`` the agent loop
        uses, with the same attribution on it, and the files it hands back are
        saved by :func:`~personacore.agent.loop.save_tool_files` — the same
        function the loop calls for a model's own tool call, so a file a
        runbook fetched and a file the assistant fetched land under the same
        rules about versioning and pinning.
        """
        if self._tools is None:
            raise _StepFailed(f"I can't reach {step.tool} right now.")
        spec = await self._spec(step.tool)
        if spec is None:
            raise _StepFailed(f"I don't have a tool called {step.tool}, so I can't do that.")
        if spec.risk is not RiskLevel.SAFE:
            # The same ceiling a turn from the admin chat runs under
            # (`boot/chat.py`'s `_profile`: `max_tool_risk=SAFE`), so a
            # `confirm` or `restricted` tool is refused here exactly as it is
            # in the chat box — contract §2's own rule. Nobody is watching a
            # run closely enough to confirm anything.
            raise _StepFailed(
                f"{step.tool} needs more permission than you have, so I can't run it."
            )

        known = self._known_files(live)
        args = {
            key: self._substituted(live, value) if isinstance(value, str) else value
            for key, value in step.args.items()
        }
        workspace = self._workspaces.workspace_for(live.conversation_id)
        before = {entry.name for entry in workspace.list()}
        result = await self._tools.call_tool(
            step.tool,
            args,
            risk_ceiling=RiskLevel.SAFE,
            correlation_id=uuid.uuid4().hex,
            owner=live.owner,
            surface=Surface.ADMIN_UI,
            # What this boundary knows and the provider cannot see — the same
            # four keys `AgentLoop._handle_tool_call` sends, so a run's tool
            # call is attributed on the provider's own record exactly as a
            # turn's is. No model answered this one, so `model` is `None`.
            caller_detail={
                "confirmation": None,
                "persona": live.state.persona or None,
                "model": None,
                "conversation_id": live.conversation_id,
            },
        )
        if not result.ok:
            raise _StepFailed(result.error or f"{step.tool} did not finish.")
        save_tool_files(workspace, step.tool, result.files)
        after = {entry.name for entry in workspace.list()}

        outputs: dict[str, str] = {}
        for role, pattern in step.files.items():
            try:
                outputs[role] = role_tools.resolve_new_file(
                    self._substituted(live, pattern), before, after
                )
            except role_tools.RoleError as exc:
                raise _StepFailed(f"{role}: {exc}") from exc
        self._pin(workspace, step.pin, {**known, **outputs})
        return outputs

    # -- one scripted turn, however this core can take it -------------------

    def _turns(self) -> Any:
        """The chat screen's turn engine, or ``None``.

        Read at the moment a turn is taken, duck-typed, exactly as
        ``web/screens/chat_run.py`` reads ``app.state.runner`` from the other
        side: the two packages are wired to each other through the application
        and through nothing else, so this module still imports nothing from
        :mod:`personacore.web`.
        """
        if self._app is None:
            return None
        return getattr(self._app.state, "chat_turn_engine", None)

    async def _streamed(
        self,
        live: _Live,
        *,
        message: str,
        thinking: bool,
        temperature: float | None,
        pins_by_role: Mapping[str, str],
        seconds: int | None = None,
        budget: int | None = None,
    ) -> tuple[str, str | None]:
        """One scripted turn, under this run's Stop and this step's watchdog.

        Returns the reply text and the watchdog's reason, or ``None`` when it
        did not trip — the same two values this always returned, so what a step
        does with them is unchanged.

        **The turn itself is the chat screen's** (PLAN.md alpha.21, the owner's
        decision): registered where a page open on this conversation can attach
        to it, streaming the same ``delta``/``thinking``/``tool`` frames a
        person's turn streams, and leaving the same metrics and reasoning
        records on the reply row — so a run's conversation behaves like a
        conversation and not like a log that fills in on refresh. What stays
        here is what is genuinely a *run's*: the ceiling, the deadline, and the
        partial file the caller writes from the text this returns. The engine
        knows about neither; it takes a callback for each delta and offers one
        way to stop.

        A core with no chat screen — every runner the tests build, and any
        assembly without the admin surface — falls through to :func:`_watched`,
        the reader this used to have. The run runs identically; it simply
        cannot be watched while it does.
        """
        engine = self._turns()
        since = datetime.now(UTC)
        if engine is None:
            events = self._chat.stream(
                message,
                user=live.owner.id,
                persona=live.state.persona or None,
                conversation_id=live.conversation_id,
                thinking=thinking,
                temperature=temperature,
                pins_by_role=pins_by_role,
                author_kind=AuthorKind.RUNBOOK,
            )
            streamed, tripped = await _watched(
                events, stopping=live.stopping, seconds=seconds, budget=budget
            )
        else:
            streamed, tripped = await self._through_engine(
                engine,
                live,
                message=message,
                thinking=thinking,
                temperature=temperature,
                pins_by_role=pins_by_role,
                seconds=seconds,
                budget=budget,
            )
        # The loop writes this turn's rows with no conversation on them (the
        # surface that resolved the conversation claims them afterwards) — the
        # same call the chat screen makes after a turn, for the same reason.
        # The engine has already made it for its own path; making it again
        # costs one UPDATE over rows that are already claimed and keeps the two
        # paths ending the same way.
        with contextlib.suppress(Exception):
            await self._conversations.append(live.conversation, since=since)
        return streamed, tripped

    async def _through_engine(
        self,
        engine: Any,
        live: _Live,
        *,
        message: str,
        thinking: bool,
        temperature: float | None,
        pins_by_role: Mapping[str, str],
        seconds: int | None,
        budget: int | None,
    ) -> tuple[str, str | None]:
        """The watchdog and the Stop, over the engine's two seams.

        ``on_text`` counts and ``stop()`` ends — that is the whole of the
        interface, and it is deliberately no wider: a ``Watchdog`` type on the
        engine's side would be the chat screen knowing what a runbook is.

        **Nothing is cancelled from underneath the turn**, exactly as
        :func:`_watched` promises: a ceiling or a deadline calls ``stop()``,
        which ends the model's stream through the path it already had, and this
        then waits for the turn to wind down and hand back everything it wrote
        — including the words that arrived before the ceiling, which are the
        only copy the ``.partial`` file can be written from.
        """
        parts: list[str] = []
        tripped: str | None = None
        handle: Any = None

        def counted(text: str) -> None:
            nonlocal tripped
            parts.append(text)
            if budget is None or tripped is not None:
                return
            if _tokens("".join(parts)) > budget:
                tripped = f"it wrote more than {budget:,} tokens."
                if handle is not None:
                    handle.stop()

        handle = await engine.start(
            owner=live.owner,
            conversation=live.conversation,
            message=message,
            persona=live.state.persona or None,
            # Contract §3, "thinking rule": the step's own `thinking:` wins
            # while a run is on, whatever the chat header's checkbox says.
            thinking=thinking,
            temperature=temperature,
            # Contract §1.5: the step's roles are the *entire* pinned set for
            # this turn, so an empty `pins:` sends an empty mapping and pins
            # nothing — never flattened to `None`, which is the one value that
            # would hand the choice back to the conversation's pin sidecar.
            pins_by_role=pins_by_role,
            # Contract §3: the rows this turn writes are a run's, not a
            # person's. They are kept and drawn on the chat page; the marking
            # is what keeps them out of the prompt the next typed message
            # composes (`web/screens/chat_thread.conversation_history`).
            author_kind=AuthorKind.RUNBOOK,
            on_text=counted,
        )

        deadline = None if seconds is None else time.monotonic() + seconds
        halt: asyncio.Task[Any] | None = asyncio.ensure_future(live.stopping.wait())
        finished: asyncio.Task[Any] = asyncio.ensure_future(handle.result())
        overran = False
        try:
            while True:
                watched: set[asyncio.Future[Any]] = {finished}
                if halt is not None:
                    watched.add(halt)
                timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                done, _ = await asyncio.wait(
                    watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if finished in done:
                    outcome = finished.result()
                    break
                if halt is not None and halt in done:
                    # Somebody pressed Stop on the run. End the reply and stay
                    # here until the turn has actually finished winding down —
                    # its rows and its `done` frame are still owed to whoever
                    # is watching.
                    handle.stop()
                    halt = None
                    deadline = None
                    continue
                # Nothing finished, so the only thing that can have happened is
                # the deadline running out.
                overran = True
                handle.stop()
                deadline = None
        finally:
            if halt is not None:
                halt.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await halt
            finished.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await finished
        if tripped is None and overran:
            tripped = f"it ran longer than {seconds} seconds."
        # A ceiling this loop itself timed or counted takes priority over the
        # engine's own report — both are never set at once in practice, and a
        # trip this loop caused describes the failure in the run's own words.
        # Otherwise `outcome.error` is the only account of what went wrong: a
        # turn whose frames failed before `handle.settle(...)` ever ran (a bad
        # persona, `markers_html` itself raising) used to come back here as an
        # empty, untripped reply — a false success the step below then failed
        # anyway, but for the wrong, uninformative reason ("the assistant
        # returned nothing.") instead of the one that actually happened.
        if tripped is None and outcome.error:
            tripped = outcome.error
        return outcome.reply_text, tripped

    # -- a model step ------------------------------------------------------

    async def _model_step(self, live: _Live, step: ModelStep) -> dict[str, str]:
        """Contract §2 and §3: one scripted turn, its reply written to a file.

        The prompt file is the user turn, the step's ``pins:`` are the pinned
        files — labelled by role, because a prompt file may never name a file
        (§1.5) — and the reply body becomes ``output:``. The turn is consumed
        *here*, by this task: that is what makes it a detached turn in the
        sense ADR-0043 means, and it is why a run whose chat page is closed
        still finishes and is still recorded.
        """
        prompt = step.prompt_text
        if prompt is None:
            path = live.folder / (step.prompt or "")
            try:
                prompt = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise _StepFailed(f"the prompt file {step.prompt} could not be read.") from exc
        message = self._substituted(live, prompt)
        known = self._known_files(live)
        try:
            pins = role_tools.resolve_pins(step.pins, known)
        except role_tools.RoleError as exc:
            raise _StepFailed(str(exc)) from exc

        workspace = self._workspaces.workspace_for(live.conversation_id)
        budget = _output_budget(step, workspace, pins)
        seconds = step.watchdog.max_seconds if step.watchdog else None

        streamed, tripped = await self._streamed(
            live,
            message=message,
            thinking=step.thinking == "on",
            temperature=step.temperature,
            pins_by_role=pins,
            seconds=seconds,
            budget=budget,
        )
        # The reply *body*, which is what `output:` names — the same
        # `.strip()` `_AdminChat` applies before it hands a finished result to
        # a screen, so a run's file and the chat's own rendering of the same
        # reply cannot differ by a newline.
        reply = streamed.strip()

        if live.stopping.is_set():
            # Stopped, not failed. Contract §3: every file stays, and the
            # partial reply is not a product — only the watchdog keeps one,
            # because only the watchdog is a failure worth reading back.
            return {}
        if tripped is not None:
            partial = self._written(workspace, live, f"{step.output}.partial", reply)
            recorded = live.state.step(step.id)
            if recorded is not None and partial is not None:
                # Recorded under the role so `plan_resume` finds it and sets it
                # aside on the way back in, instead of a resumed step colliding
                # with the dead attempt's leftovers.
                recorded.outputs = {step.id: partial}
            raise _StepFailed(tripped)
        if not reply.strip():
            raise _StepFailed("the assistant returned nothing.")

        final = self._written(workspace, live, step.output, reply)
        if final is None:
            raise _StepFailed(f"{step.output} could not be written to the workspace.")
        return {step.id: final}

    # =======================================================================
    # Gates — contract §4. Everything from here to the next banner is the
    # gate build and nothing else in this class reaches into it: the step
    # loop's only door is `_gate`, and a person's only doors are `answer`,
    # `answer_text` and `awaiting_text`.
    # =======================================================================

    def awaiting_text(self, conversation_id: str) -> bool:
        """Whether this run's next message should be read as a gate answer.

        The explicit half of the handshake the design decision asks for: the
        chat unlocks its composer on this and routes that one message to
        :meth:`answer_text`, so a typed answer is never *guessed* from a run
        happening to be parked. False for every other state a run can be in,
        including a gate whose questions parsed and are waiting to be clicked.
        """
        gate = _parked_gate(self._state_now(conversation_id))
        return gate is not None and gate.mode == "text" and not gate.answers

    async def answer(
        self,
        owner: Owner,
        conversation_id: str,
        question_id: str,
        choice: str | None = None,
        other: str | None = None,
    ) -> RunState:
        """Record one answer at a human gate; finish the gate on the last one.

        Contract §1.2 and §4: the person's picks and typed answers become the
        step's answer file, and the next step gets it pinned. Nothing is
        written until every question has an answer — a half-answered gate is a
        gate, not a file — and the answer that completes it starts the run
        going again from the step after the gate.
        """
        live, step, gate = await self._at_gate(owner, conversation_id)
        if gate.mode != "questions":
            raise RunRefused(NOT_AT_A_GATE)
        question = _question_by_id(gate, question_id)
        if question is None:
            raise RunRefused(f"{question_id} is not one of this gate's questions.")
        compare = _compare_roles(question, gate.source_pins)
        try:
            gate.answers[question.id] = gate_tools.answer_line(
                question, choice, other, compare=compare
            )
        except gate_tools.GateError as exc:
            raise RunRefused(exc.message) from exc
        self._write(live)
        if len(gate.answers) < len(gate.questions):
            return _with_conversation(live.state, conversation_id)
        try:
            await self._finish_gate(live, step)
        except _StepFailed as exc:
            raise RunRefused(exc.reason) from exc
        self._relaunch(live)
        return _with_conversation(live.state, conversation_id)

    async def answer_text(self, owner: Owner, conversation_id: str, text: str) -> RunState:
        """The fallback answer: one typed message, written as the answer file.

        Only while :meth:`awaiting_text` is true — which is only after the
        questions could not be read (contract §4's "a plain 'answer in the
        box' fallback"). The text is written **verbatim**: it is the person's
        answer, not an instruction, and nothing reads it as one.
        """
        live, step, gate = await self._at_gate(owner, conversation_id)
        if gate.mode != "text" or gate.answers:
            raise RunRefused(NOT_AWAITING_TEXT)
        written = text.strip()
        if not written:
            raise RunRefused("An answer needs something in it.")
        gate.answers[_TEXT_ANSWER_KEY] = written
        try:
            await self._finish_gate(live, step)
        except _StepFailed as exc:
            raise RunRefused(exc.reason) from exc
        self._relaunch(live)
        return _with_conversation(live.state, conversation_id)

    # -- the step loop's door ----------------------------------------------

    async def _gate(self, live: _Live, step: GateStep) -> bool:
        """One gate step. ``True`` when the step loop should carry on.

        Carrying on means one of two things and never a third: an auto gate
        passed (the run moves to the next step), or an auto gate did not and
        has sent the run back to its ``goto``. Every other outcome — a human
        gate, a failure, an auto gate out of loops — ends this task, because
        what happens next needs somebody outside it.
        """
        try:
            return await self._gate_inner(live, step)
        except _StepFailed as exc:
            live.state = run_state.fail(live.state, step.id, exc.reason)
            self._write(live)
            await self._say(live, f"{_label(step)}: failed — {exc.reason}")
            return False

    async def _gate_inner(self, live: _Live, step: GateStep) -> bool:
        recorded = live.state.step(step.id)
        standing = recorded.gate if recorded is not None else None
        if standing is not None and standing.mode != "auto":
            # Reached again: a resume, or a restart, onto a gate that has
            # already asked. Contract §5 — "a run that was at a gate is still
            # at the gate" — so nothing is asked twice and no model turn is
            # spent asking it.
            # `or 1` for text mode, which has one answer and no questions.
            if standing.answers and len(standing.answers) >= (len(standing.questions) or 1):
                await self._finish_gate(live, step)
                return live.state.status == "running"
            return await self._park_at_gate(live, step, standing, _gate_row(step, standing))
        self._begin(live, step.id)
        if step.auto is not None:
            return await self._auto_gate(live, step)
        return await self._human_gate(live, step)

    # -- a human gate ------------------------------------------------------

    async def _human_gate(self, live: _Live, step: GateStep) -> bool:
        """Contract §4: the questions, from a model turn or from the file."""
        name, text = self._gate_file(live, step)
        source_title, source_description = _source_titles(live, step)
        try:
            if step.questions == "file":
                source = text
            else:
                source = await self._questions_turn(live, step, name, text)
            if live.stopping.is_set():
                return False
            asked = gate_tools.parse_questions(source)
        except gate_tools.GateError as exc:
            # Contract §4's fallback. Not a failure: the flags are real, the
            # person can read them, and a text box is a worse gate than click
            # options but an infinitely better one than a stopped run.
            gate = GateState(
                mode="text",
                title=step.title,
                description=step.description,
                source_title=source_title,
                source_description=source_description,
            )
            row = f"{_label(step)}: waiting for you — {exc.message} Answer in the box below; {name}"
            return await self._park_at_gate(live, step, gate, row)
        if not asked.questions:
            # Contract §4, added 2026-09-05: every flag said "consistent" —
            # nothing here needs a person, so this is a pass rather than a
            # park.
            return await self._pass_no_questions(live, step)
        gate = GateState(
            mode="questions",
            questions=[question.model_dump() for question in asked.questions],
            source_file=name,
            source_role=step.from_,
            # SPEC alpha.22: stored here, once, so the web never re-parses
            # the runbook file to draw the card's heading or its "Questions
            # from …" line — see `_source_titles`.
            title=step.title,
            description=step.description,
            source_title=source_title,
            source_description=source_description,
            # SPEC alpha.22, contract §4 2026-09-06: the comparison card's
            # own doorway into the workspace — see `_source_pins`.
            source_pins=_source_pins(live, step, name, self._known_files(live)),
        )
        return await self._park_at_gate(live, step, gate, _gate_row(step, gate))

    async def _pass_no_questions(self, live: _Live, step: GateStep) -> bool:
        """Contract §4, added 2026-09-05: an empty ``questions`` list is a
        pass, not a park. The gate's own role still gets a file — the fixed
        single line ``"No questions."`` — so a later step's ``pins:
        [<gate id>]`` resolves to something real rather than needing a
        special case for the gate nobody had to answer.
        """
        workspace = self._workspaces.workspace_for(live.conversation_id)
        wanted = self._substituted(live, step.answer or f"{step.id}.md")
        name = self._written(workspace, live, wanted, "No questions.\n")
        if name is None:
            raise _StepFailed(f"{wanted} could not be written to the workspace.")
        live.state = run_state.advance(live.state, step.id, {step.id: name})
        self._write(live)
        await self._say(live, f"{_label(step)}: resolved: no questions, continuing")
        if live.state.status == "done":
            await self._finish_live(live)
        return live.state.status == "running"

    async def _questions_turn(self, live: _Live, step: GateStep, name: str, text: str) -> str:
        """One extra scripted turn, asking for the questions as JSON.

        Thinking off and temperature 0 because this turn has one right shape
        and no room for invention (contract §4: "one extra model turn,
        thinking off"). The file is pinned as well as quoted, so a model that
        would rather read the pinned block than the prompt is reading the same
        bytes either way. The ``from_`` step's own pins are also pinned, so a
        model quoting a passage from one of those files knows which role to
        name on the context entry.
        """
        # Build pins_by_role: the from_ file FIRST, then the from_ step's own pinned files
        source = _source_pins(live, step, name, self._known_files(live))
        pins_by_role = {step.from_: name, **{r: f for r, f in source.items() if r != step.from_}}
        streamed, _tripped = await self._streamed(
            live,
            message=gate_tools.questions_prompt(text),
            thinking=False,
            temperature=0.0,
            pins_by_role=pins_by_role,
        )
        return streamed.strip()

    async def _park_at_gate(self, live: _Live, step: GateStep, gate: GateState, row: str) -> bool:
        """Park the run *on* the gate, with the gate's own state recorded.

        Always ``False``: a human gate never lets the step loop carry on.
        ``current`` stays on the gate, so the run resumes onto it rather than
        past it, and every file the run has made stays exactly where it is.
        """
        live.state = run_state.park(live.state, WAITING_FOR_ANSWERS)
        live.state.current = step.id
        recorded = live.state.step(step.id)
        if recorded is not None:
            recorded.gate = gate
            recorded.status = StepStatus.PARKED
        self._write(live)
        await self._say(live, row)
        return False

    async def _finish_gate(self, live: _Live, step: GateStep) -> None:
        """Every answer, in order, written as the gate's own file.

        The gate's role is its step id, like every other step (contract §2),
        so the next step's ``pins: [<gate id>]`` resolves to this file and the
        answers reach the model as a pinned block — never as an instruction.
        """
        recorded = live.state.step(step.id)
        gate = recorded.gate if recorded is not None else None
        if gate is None:  # pragma: no cover - only reached through a gate
            raise _StepFailed("this gate has no answers to write.")
        order = [str(item.get("id")) for item in gate.questions] or sorted(gate.answers)
        body = "\n".join(gate.answers[key] for key in order if key in gate.answers)
        workspace = self._workspaces.workspace_for(live.conversation_id)
        wanted = self._substituted(live, step.answer or f"{step.id}.md")
        name = self._written(workspace, live, wanted, f"{body}\n")
        if name is None:
            raise _StepFailed(f"{wanted} could not be written to the workspace.")
        live.state = run_state.advance(live.state, step.id, {step.id: name})
        live.state.reason = None
        if live.state.status == "parked":
            live.state.status = "running"
        self._write(live)
        await self._say(live, f"{_label(step)}: answered, {self._sizes(live, {step.id: name})}")
        if live.state.status == "done":
            await self._finish_live(live)

    # -- an auto gate ------------------------------------------------------

    async def _auto_gate(self, live: _Live, step: GateStep) -> bool:
        """Contract §2: no person — pass, or go back round, or give up.

        An auto gate **produces nothing**. Its answer is a decision about
        where the run goes next, and a file saying "it passed" would be a
        file nobody ever pins.
        """
        auto = step.auto
        if auto is None:  # pragma: no cover - only called for an auto gate
            raise _StepFailed("this gate has no condition.")
        name, text = self._gate_file(live, step)
        recorded = live.state.step(step.id)
        gate = (recorded.gate if recorded is not None else None) or GateState(mode="auto")
        if recorded is not None:
            recorded.gate = gate
        try:
            passed = gate_tools.evaluate(auto.pass_when, text, self._known_files(live))
        except gate_tools.GateError as exc:
            raise _StepFailed(exc.message) from exc

        if passed:
            live.state = run_state.advance(live.state, step.id, {})
            self._write(live)
            await self._say(live, f"{_label(step)}: passed on {name}")
            if live.state.status == "done":
                await self._finish_live(live)
            return live.state.status == "running"

        gate.loops += 1
        if gate.loops > auto.else_.max_loops:
            # `reason` is stored on the run state (parked-run message,
            # `chat_run.py`), so its own text keeps the bare step id — only
            # the progress row's own lead segment gets the title treatment.
            reason = f"auto gate {step.id}: {auto.else_.max_loops} loops without passing"
            live.state = run_state.park(live.state, reason)
            live.state.current = step.id
            self._write(live)
            await self._say(live, f"{_label(step)}: parked — {reason}")
            return False

        self._rewind(live, step.id, auto.else_.goto)
        live.state.current = auto.else_.goto
        live.state.updated = datetime.now(UTC).isoformat()
        self._write(live)
        await self._say(
            live,
            f"{_label(step)}: did not pass, back to {auto.else_.goto} "
            f"(loop {gate.loops} of {auto.else_.max_loops})",
        )
        return True

    def _rewind(self, live: _Live, gate_id: str, goto_id: str) -> None:
        """Put every step between the ``goto`` and the gate back to pending.

        Including the gate itself, so it is asked again after the loop has
        run — without it the run would walk straight past the condition it
        just failed. **Nothing on disk is touched**: the previous attempt's
        files stay exactly where they are and the re-run's own outputs land
        under versioned names, which is the workspace's ordinary behaviour
        and the reason a person can read every pass afterwards.

        The gate's own :class:`~personacore.runbooks.state.GateState` is kept,
        because the loop counter lives in it and resetting the counter is how
        a loop becomes forever.
        """
        ids = [item.id for item in live.state.steps]
        if goto_id not in ids:
            raise _StepFailed(
                f"'goto' names step {goto_id}, which is not in this runbook any more."
            )
        first = min(ids.index(goto_id), ids.index(gate_id))
        last = max(ids.index(goto_id), ids.index(gate_id))
        for item in live.state.steps[first : last + 1]:
            item.status = StepStatus.PENDING
            item.outputs = {}
            item.started = None
            item.finished = None
            item.reason = None

    # -- shared by both kinds ----------------------------------------------

    def _gate_file(self, live: _Live, step: GateStep) -> tuple[str, str]:
        """The file the gate's ``from:`` role names, and its content."""
        name = self._known_files(live).get(step.from_)
        if not isinstance(name, str) or not name:
            raise _StepFailed(f"from names role {step.from_}, which no earlier step produced.")
        workspace = self._workspaces.workspace_for(live.conversation_id)
        try:
            return name, workspace.read(name)
        except WorkspaceError as exc:
            raise _StepFailed(f"{name} could not be read: {exc}") from exc

    async def _at_gate(
        self, owner: Owner, conversation_id: str
    ) -> tuple[_Live, GateStep, GateState]:
        """The run, the gate step and its state — or the sentence why not.

        Rebuilt from ``.run.json`` every time, exactly as :meth:`resume` does
        and for the same reason: the task that asked the question ended when
        it parked, and the container that asked may not be the container being
        answered. The switches are checked here too, because contract §3's
        "switch first" covers every way a run can be made to move.
        """
        if not self._settings_enabled():
            raise RunRefused(RUNS_OFF)
        state = self._state_now(conversation_id)
        gate = _parked_gate(state)
        if state is None or gate is None or self.is_running(conversation_id):
            raise RunRefused(NOT_AT_A_GATE)
        if not self._plugin_enabled(state.plugin):
            raise RunRefused(PLUGIN_OFF)
        runbook, folder = self._loaded(state.plugin, state.runbook)
        step = next((item for item in runbook.steps if item.id == state.current), None)
        if not isinstance(step, GateStep):
            raise RunRefused(NOT_AT_A_GATE)
        held = self._live.get(conversation_id)
        conversation = held.conversation if held is not None else None
        if conversation is None:
            conversation = await self._conversations.resolve(owner, conversation_id=conversation_id)
        if conversation is None:
            raise RunRefused("That conversation could not be opened, so the run cannot go on.")
        return (
            _Live(
                conversation=conversation,
                owner=owner,
                plugin=state.plugin,
                runbook=runbook,
                folder=folder,
                state=state,
            ),
            step,
            gate,
        )

    def _state_now(self, conversation_id: str) -> RunState | None:
        """This run's state, without awaiting anything — what
        :meth:`awaiting_text` needs, since the composer asks that question
        while it is deciding what to render."""
        live = self._live.get(conversation_id)
        if live is not None:
            return live.state
        try:
            return run_state.read_state(self._workspace_dir(conversation_id))
        except run_state.StateUnreadable as exc:
            log.warning("runbook_state_unreadable", reason=exc.reason)
            return None

    def _relaunch(self, live: _Live) -> None:
        """Start the step loop again after a gate was answered."""
        if live.state.status != "running" or live.state.current is None:
            return
        self._live[live.conversation_id] = live
        live.task = asyncio.create_task(
            self._drive(live), name=f"runbook-gate-{live.conversation_id}"
        )

    # =======================================================================
    # End of the gate build
    # =======================================================================

    # -- small shared pieces -----------------------------------------------

    async def _spec(self, name: str) -> ToolSpec | None:
        if self._tools is None:
            return None
        try:
            listed: Sequence[ToolSpec] = await self._tools.list_tools()
        except Exception as exc:  # noqa: BLE001 - no tools is a refusal, not a crash
            log.warning("runbook_tool_listing_failed", error=repr(exc))
            return None
        return next((spec for spec in listed if spec.name == name), None)

    def _known_files(self, live: _Live) -> dict[str, str]:
        """Every role produced so far, in step order — the map ``{{ files.role
        }}`` and ``pins:`` are both resolved against. A later step wins a role
        an earlier one also produced, which is the order a person reading the
        file top to bottom would expect.

        A role an **iterating** step produced holds many filenames rather than
        one (contract §1.12), and nothing in the file format can say which of
        them a later ``pins:`` or ``{{ files.role }}`` means — so those roles
        are left out, and a step that asks for one is refused naming it rather
        than handed a list where a filename belongs.
        """
        known: dict[str, str] = {}
        for step in live.state.steps:
            known.update(
                {role: name for role, name in step.outputs.items() if isinstance(name, str)}
            )
        return known

    def _substituted(self, live: _Live, template: str) -> str:
        # `{{ item }}` while a step-level `foreach` is running, and nothing
        # named `item` at any other time (contract §1.12).
        inputs: Mapping[str, Any] = live.state.inputs
        if live.iterating is not None:
            inputs = {**live.state.inputs, "item": live.iterating}
        try:
            return role_tools.substitute(template, inputs, self._known_files(live))
        except role_tools.RoleError as exc:
            raise _StepFailed(str(exc)) from exc

    def _pin(self, workspace: Workspace, wanted: Sequence[str], known: Mapping[str, str]) -> None:
        for role in wanted:
            name = known.get(role)
            if name is None:
                raise _StepFailed(f"pin names role {role!r}, which no step produced.")
            try:
                workspace.pin(name)
            except WorkspaceError as exc:
                raise _StepFailed(str(exc)) from exc

    def _written(self, workspace: Workspace, live: _Live, name: str, text: str) -> str | None:
        """Write one of a run's own files, returning the name it landed under.

        ``source`` names the runbook rather than a tool, so the workspace
        listing and the manifest the model reads both say where the file came
        from — "written by you" would be a lie about a file the run produced.
        """
        try:
            return workspace.write(name, text, source=f"runbook:{live.runbook.runbook}")
        except WorkspaceError as exc:
            log.warning("runbook_output_refused", error=str(exc))
            return None

    def _sizes(self, live: _Live, outputs: Mapping[str, Any]) -> str:
        """``ch.p2.md 18,410 bytes`` — the tail of a step's finished line.

        A role an iterating step produced holds a *list* of filenames
        (contract §1.12), and naming all twelve of them would make the one
        line the step gets unreadable — so those are counted instead:
        ``text: 12 files, 240,110 bytes``. Each item already had a line of its
        own as it finished.
        """
        if not outputs:
            return "no files"
        workspace = self._workspaces.workspace_for(live.conversation_id)
        by_name = {entry.name: entry.size_bytes for entry in workspace.list()}
        parts: list[str] = []
        for role, value in outputs.items():
            if isinstance(value, list):
                total = sum(by_name.get(name, 0) for name in value)
                parts.append(f"{role}: {len(value)} files, {total:,} bytes")
            else:
                parts.append(f"{value} {by_name.get(value, 0):,} bytes")
        return ", ".join(parts)

    def _leaf_outputs(
        self, runbook: Runbook, state: RunState
    ) -> list[tuple[run_state.StepState, str, str | list[str]]]:
        """The run's own leaf outputs — owner finding 2026-09-06: a run ended
        and he could not find the result, because the run's own last steps
        are a review and a gate, and the actual finished chapter is an
        earlier step's file.

        A leaf is every role a non-skipped step produced that no later,
        non-skipped step went on to consume — pinned, read through
        ``{{ files.role }}``, or named as a gate's ``from:``.
        :func:`_roles_needed` already answers "what does this step ask of
        the steps before it"; this only adds the ordering, checking each
        role against the steps that come **after** the one that made it,
        since nothing later can consume a role its own maker has not
        produced yet.

        Takes the parsed runbook and the run's own state directly, rather
        than a live run, so a finished run — no longer held in ``self._live``
        once its task ends — can still be asked about, the same way
        :meth:`result_groups` needs to for a screen that opens long after
        the run itself is gone.

        When every output was consumed by something later — the ordinary
        shape, and why this is usually one file — the leaf set is empty and
        the **last produced file** stands in instead, so a finished run
        always has something to point a person at.
        """
        order = [step.id for step in runbook.steps]
        schema_by_id = {step.id: step for step in runbook.steps}

        def position(step_id: str) -> int:
            try:
                return order.index(step_id)
            except ValueError:
                return -1

        done_steps = [
            s
            for s in state.steps
            if s.status == StepStatus.DONE and s.reason != SKIPPED and s.outputs
        ]
        produced = [(s, role, value) for s in done_steps for role, value in s.outputs.items()]
        if not produced:
            return []

        leaves: list[tuple[run_state.StepState, str, str | list[str]]] = []
        for step, role, value in produced:
            pos = position(step.id)
            consumed_later = any(
                position(other.id) > pos and role in _roles_needed(schema_by_id[other.id])
                for other in done_steps
                if other.id in schema_by_id
            )
            if not consumed_later:
                leaves.append((step, role, value))
        return leaves or [produced[-1]]

    def _grouped_leaf_files(
        self, runbook: Runbook, state: RunState
    ) -> list[tuple[run_state.StepState, list[str]]]:
        """:meth:`_leaf_outputs`, folded into one filename list per step, in
        step order — shared by :meth:`_result_summary` (the transcript's own
        sentence) and :meth:`result_groups` (what a screen draws instead)."""
        groups: list[tuple[run_state.StepState, list[str]]] = []
        for step, _role, value in self._leaf_outputs(runbook, state):
            names = list(value) if isinstance(value, list) else [value]
            if groups and groups[-1][0] is step:
                groups[-1][1].extend(names)
            else:
                groups.append((step, names))
        return groups

    def _result_summary(
        self, runbook: Runbook, state: RunState, workspace: Workspace
    ) -> dict[str, Any]:
        """A finished run's own result: the exact sentence its last progress
        row carries, plus its leaf files (:meth:`_leaf_outputs`) as plain
        dicts (``{"name": "ch.p6.md", "size": 23401}``) a screen can draw a
        download link per file from.

        **Pure** — no side effects, no memory of having been called before.
        It may be called any number of times for the same finished run and
        answer exactly the same thing every time, which is what lets
        :meth:`done_summary` be a plain read and :meth:`_finish` say the
        message this builds without either one having to guess whether the
        other already has.

        A single step behind the whole leaf set gets the titled message form,
        ``Done. Result of p6 · Enhance: ...`` (:func:`_label`), when that
        step names one — the bare ``Done. Result: ...`` otherwise, which is
        every runbook already written. More than one step contributing
        leaves (a review step and a step it did not fully supersede, say)
        gets each step named beside its own files. A file the workspace no
        longer has is tolerated — its size is reported as 0 rather than
        raising, the same tolerance :meth:`_sizes` already gives a listing
        that may be stale by the time it is read.
        """
        groups = self._grouped_leaf_files(runbook, state)
        by_name = {entry.name: entry.size_bytes for entry in workspace.list()}

        def sized(name: str) -> str:
            return f"{name} ({by_name.get(name, 0):,} bytes)"

        if not groups:
            message = "Done."
        elif len(groups) == 1:
            step, names = groups[0]
            files = ", ".join(sized(name) for name in names)
            if step.title:
                message = f"Done. Result of {_label(step)}: {files}"
            else:
                message = f"Done. Result: {files}"
        else:
            parts = [
                f"{_label(step)}: {', '.join(sized(name) for name in names)}"
                for step, names in groups
            ]
            message = "Done. Result: " + "; ".join(parts)

        files = [
            {"name": name, "size": by_name.get(name, 0)}
            for _step, names in groups
            for name in names
        ]
        return {"message": message, "files": files}

    async def done_summary(self, conversation_id: str) -> dict[str, Any] | None:
        """A finished run's own status line — see :meth:`_result_summary`,
        which is the whole of what this does: read the state and the
        runbook, then hand them to that pure function. Read once, together,
        so the run-status box and the transcript's own row can never
        disagree about how a run ended.

        ``None`` for a run that is not ``done``, or one this build cannot
        even read any more (its plugin removed, its runbook file gone) —
        the same tolerance every other read here gives a screen that has
        nothing to show, never a raise.

        Duck-typed on purpose: ``web/screens/chat_run.py`` reads only
        ``app.state.runner`` (its own module boundary) and never
        :mod:`personacore.runbooks.state`, the same as :meth:`state` and
        :meth:`is_running` already are.
        """
        state = await self.state(conversation_id)
        if state is None or state.status != "done":
            return None
        try:
            runbook, _folder = self._loaded(state.plugin, state.runbook)
        except RunRefused:
            return None
        workspace = self._workspaces.workspace_for(conversation_id)
        return self._result_summary(runbook, state, workspace)

    async def _finish_live(self, live: _Live) -> None:
        """:meth:`_finish`, for a run this process is still holding — the
        pieces are all on ``live`` already, so this only unpacks them."""
        await self._finish(
            owner=live.owner,
            conversation_id=live.conversation_id,
            runbook=live.runbook,
            state=live.state,
            workspace=self._workspaces.workspace_for(live.conversation_id),
        )

    async def _finish(
        self,
        *,
        owner: Owner,
        conversation_id: str,
        runbook: Runbook,
        state: RunState,
        workspace: Workspace,
    ) -> None:
        """The one place a run's status becomes ``"done"``.

        Every path that can finish a run — the ordinary end of :meth:`_steps`,
        a gate with nothing left after it (:meth:`_pass_no_questions`,
        :meth:`_finish_gate`), an auto gate that passes on its last loop
        (:meth:`_auto_gate`), and a run with nothing to do at all because
        every step was skipped at start (:func:`_mark_skipped`) — calls this
        instead of setting ``status`` itself. It sets the status, writes
        ``.run.json``, and says the result row, in that order, exactly once
        per call — and every call site above calls it exactly once per real
        completion, so a run never gets two "Done." rows regardless of which
        of those paths finished it.

        Takes the pieces a finish needs rather than a :class:`_Live`, so a
        run this process has forgotten — parked at a gate, answered through
        :meth:`answer`, which rebuilds a fresh, unregistered ``_Live`` off
        ``.run.json`` — finishes exactly the same way a live one does.

        Idempotent on the status itself (a state already ``"done"`` is left
        as it found it) so a defensive second call costs a rewrite and a
        repeated row rather than a wrong one — but no call site here makes a
        second call, by construction.
        """
        if state.status != "done":
            state.status = "done"
            state.current = None
            state.updated = datetime.now(UTC).isoformat()
        try:
            run_state.write_state(self._workspace_dir(conversation_id), state)
        except OSError as exc:
            log.error("runbook_state_write_failed", error=repr(exc))
        row = self._result_summary(runbook, state, workspace)["message"]
        await self._say_to(owner, conversation_id, runbook.runbook, row)

    def _begin(self, live: _Live, step_id: str) -> None:
        """Mark one step ``running``. Not in :mod:`.state` because every
        transition there answers "what happened"; this one only says "starting
        now", and it is the one thing the runner is allowed to know that the
        state module does not."""
        step = live.state.step(step_id)
        if step is not None:
            step.status = StepStatus.RUNNING
            step.started = datetime.now(UTC).isoformat()
        live.state.updated = datetime.now(UTC).isoformat()
        self._write(live)

    def _write(self, live: _Live) -> None:
        try:
            run_state.write_state(self._workspace_dir(live.conversation_id), live.state)
        except OSError as exc:
            log.error("runbook_state_write_failed", error=repr(exc))

    def _workspace_dir(self, conversation_id: str) -> Path:
        return self._layout.workspaces / conversation_id

    def _park_unreadable(self, folder: Path, reason: str) -> None:
        """Keep the torn file, park the run beside it.

        Contract §5 says a torn ``.run.json`` is never silently reset — so the
        file is renamed to ``.run.json.unreadable`` and kept, and what replaces
        it is a state that says exactly one thing: this run is parked because
        nobody could read it. Both halves matter; writing the parked state
        without keeping the original would be the reset the contract forbids,
        and keeping the original without writing anything would leave a
        conversation that looks like it has no run at all.
        """
        log.warning("runbook_state_unreadable", reason=reason)
        torn = folder / run_state.STATE_FILENAME
        with contextlib.suppress(OSError):
            os.replace(torn, folder / f"{run_state.STATE_FILENAME}.unreadable")
        now = datetime.now(UTC).isoformat()
        with contextlib.suppress(OSError):
            run_state.write_state(
                folder,
                RunState(
                    runbook="",
                    version="",
                    plugin="",
                    persona="",
                    inputs={},
                    steps=[],
                    status="parked",
                    current=None,
                    started=now,
                    updated=now,
                    reason=STATE_UNREADABLE,
                ),
            )

    async def _say(self, live: _Live, text: str) -> None:
        """One progress line, as a ``system`` transcript row — see
        :meth:`_say_to`, which is the whole of what this does once the
        pieces are unpacked off ``live``."""
        await self._say_to(live.owner, live.conversation_id, live.runbook.runbook, text)

    async def _say_to(
        self, owner: Owner, conversation_id: str, runbook_name: str, text: str
    ) -> None:
        """One progress line, as a ``system`` transcript row.

        ``system`` because it was not said by anybody: the chat draws these as
        a quiet notice, and ``conversation_history`` already drops the role, so
        a progress line can never reach the model as something a person said.
        Best-effort, like every other transcript write in this codebase — a
        run that cannot report itself is still a run.

        Authored by the runbook all the same, so that *every* row a run leaves
        behind carries the same mark and "is this row a run's?" is one question
        with one answer rather than two rules that could drift apart. The role
        filter and the author filter both already drop this row; agreeing is
        the point.

        Takes ``owner``/``conversation_id``/``runbook_name`` directly rather
        than a :class:`_Live` so :meth:`_finish` can say a run's result row
        for a run this process never held live at all — one answered from
        ``.run.json`` after this process forgot it, in particular.
        """
        if self._audit is None:
            return
        record = TranscriptRecord(
            correlation_id=uuid.uuid4().hex,
            timestamp=datetime.now(UTC),
            surface=Surface.ADMIN_UI,
            owner=owner,
            role=MessageRole.SYSTEM,
            content=text,
            conversation_id=conversation_id,
            author=Author(name=runbook_name, kind=AuthorKind.RUNBOOK),
        )
        try:
            await self._audit.record_transcript(record)
        except Exception as exc:  # noqa: BLE001 - reporting never costs the run
            log.warning("runbook_progress_row_failed", error=repr(exc))


# ---------------------------------------------------------------------------
# The watchdog, and the stop it shares a path with
# ---------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4
"""How many characters a token is counted as when nothing better is available.

The loop reports the *prompt*'s token count off the backend's own tokenizer
(``AgentEvent.detail['prompt_tokens']``) and nothing reports the reply's while
it is still being written — which is exactly when a watchdog has to decide.
Four characters is the usual English approximation, it is applied to both
sides of a ratio so the two are measured the same way, and it is a ceiling on
a runaway reply rather than a billing figure.
"""


async def _watched(
    events: Any,
    *,
    stopping: asyncio.Event,
    seconds: int | None,
    budget: int | None,
) -> tuple[str, str | None]:
    """Consume one turn under a stop flag and a watchdog.

    Returns the text produced and the watchdog's reason, or ``None`` when it
    did not trip.

    **Nothing is cancelled from underneath the turn.** The stop flag is waited
    on *beside* the pending ``__anext__``, so it is felt in the middle of a
    reply rather than after the model's next token — a turn spends nearly all
    of its life suspended on that read, and a flag checked only between events
    would not be felt for minutes. When either the flag or a ceiling wins, this
    returns; the ``finally`` cancels the pending read and closes the generator,
    which winds the turn down through the same path it takes when the model
    simply stops — which is what ``chat_streaming``'s Stop control does, and
    the reason it does it that way.
    """
    deadline = None if seconds is None else time.monotonic() + seconds
    parts: list[str] = []
    tripped: str | None = None
    iterator = events.__aiter__()
    pending: asyncio.Task[Any] | None = None
    halt: asyncio.Task[Any] = asyncio.ensure_future(stopping.wait())
    try:
        while True:
            if stopping.is_set():
                return "".join(parts), None
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, _ = await asyncio.wait(
                {pending, halt}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if halt in done:
                return "".join(parts), None
            if pending not in done:
                # The only thing a timeout can mean here: `seconds` ran out.
                return "".join(parts), f"it ran longer than {seconds} seconds."
            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:
                break
            if getattr(event, "kind", "") == "text":
                parts.append(event.text)
                if budget is not None and _tokens("".join(parts)) > budget:
                    tripped = f"it wrote more than {budget:,} tokens."
                    break
    finally:
        halt.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await halt
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        closer = getattr(events, "aclose", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()
    return "".join(parts), tripped


# ---------------------------------------------------------------------------
# Gates — the module-level half (contract §4)
# ---------------------------------------------------------------------------


_TEXT_ANSWER_KEY = "answer"
"""The single key a typed answer is recorded under. Not a question id because
there were no questions: text mode is reached precisely when the questions
could not be read."""


def _parked_gate(state: RunState | None) -> GateState | None:
    """The gate a run is parked on, waiting for a person — or ``None``.

    ``None`` for a run that is not parked, is parked for some other reason, or
    is parked on an **auto** gate, which is out of loops rather than waiting:
    nobody can answer that one, and offering to would be a lie about what the
    button does.
    """
    if state is None or state.status != "parked" or state.current is None:
        return None
    step = state.step(state.current)
    if step is None or step.gate is None or step.gate.mode == "auto":
        return None
    return step.gate


def _parked_at_a_gate(state: RunState | None) -> bool:
    """Whether this run is stopped on a gate a person can still answer.

    The question a *parent* run asks about one of its items (contract §1.12):
    an item parked at a gate has not finished and has not failed, so the
    parent waits rather than moving on to the next one.
    """
    return _parked_gate(state) is not None


def _question_by_id(gate: GateState, question_id: str) -> gate_tools.Question | None:
    """One recorded question, back as the model it was validated as.

    Re-validated rather than trusted: the dicts have been through
    ``.run.json`` and a person's answer is checked against *these* options, so
    "is this one of the options" has to be asked of something with a shape.
    """
    for item in gate.questions:
        if str(item.get("id")) != question_id:
            continue
        with contextlib.suppress(Exception):
            return gate_tools.Question.model_validate(item)
    return None


def _label(step: Any) -> str:
    """The lead segment of a progress row (SPEC alpha.22, "every step says
    what it is for"): the step's bare id, or ``"id · title"`` when the step
    names one.

    The one place a row's step-identifying text is built, so a titled step
    reads the same way in every row that names it rather than "p5 · Audit"
    in one line and bare "p5" in the next depending on which call site wrote
    it. A step with no title returns exactly its id — today's text,
    unchanged, for every runbook already written.
    """
    title = getattr(step, "title", None)
    return f"{step.id} · {title}" if title else step.id


def _gate_row(step: Any, gate: GateState) -> str:
    """The progress line a parked human gate posts (contract §1.2: "a short
    message … and parks")."""
    label = _label(step)
    if gate.mode == "questions":
        return f"{label}: waiting for you, {len(gate.questions)} questions"
    return f"{label}: waiting for you, answer in the box below"


def _source_titles(live: _Live, step: GateStep) -> tuple[str | None, str | None]:
    """The ``from:`` step's own ``title``/``description``, off the parsed
    runbook (SPEC alpha.22) — what the gate card's "Questions from …" line
    names that step by, so a person reads *why* the flags exist and not only
    which step id they came from. ``(None, None)`` when the ``from`` step
    cannot be found, which :func:`validate_runbook` already refuses at
    upload, so this is only ever reached for a runbook already proven to
    have one.
    """
    source = _find_source_step(live, step)
    if source is None:
        return None, None
    return getattr(source, "title", None), getattr(source, "description", None)


def _find_source_step(live: _Live, step: GateStep) -> Any | None:
    """The step that produced the ``from:`` role — tried first by matching
    a step id (a model step's own output is pinned under its own id,
    contract §2) and, when that misses, by which step's own
    :func:`_roles_made` includes it (a tool step's ``files:`` can name a
    role different from its own id), taking the last such step in file
    order the same way :func:`_known_files` would resolve it. ``None``
    when nothing produces the role, which :func:`validate_runbook` already
    refuses at upload for a runbook this could ever be reached against.
    """
    source = next((one for one in live.runbook.steps if one.id == step.from_), None)
    if source is not None:
        return source
    makers = [one for one in live.runbook.steps if step.from_ in _roles_made(one)]
    return makers[-1] if makers else None


def _source_pins(
    live: _Live, step: GateStep, name: str, known: Mapping[str, str]
) -> dict[str, str]:
    """The ``from:`` step's own resolved pins, role -> filename, plus its
    own output under its own role — SPEC alpha.22, contract §4 2026-09-06:
    what the chat's comparison card (``web/screens/chat_run.py``) opens the
    right file per role from, without re-reading the runbook itself.

    Ordered the way ``known`` already is — by the step that produced each
    role (:meth:`Runner._known_files`), not by the order the ``from:`` step
    happened to list its own pins in — so a later reader can tell which of
    two roles was produced earlier in the run purely from this mapping's
    own key order (contract §4: "left is the role produced earlier in the
    run … which the runner writes in step order").

    Read off whichever of ``pins`` (a model step) or ``pin`` (a tool step —
    schema.py's own note: "singular for a tool step, ``pins`` for a model
    step") the source step actually has; a step with neither, or one that
    cannot be found at all, leaves this holding only the gate's own file.
    Never raises — this only ever makes a card richer, never a run refused.
    """
    source = _find_source_step(live, step)
    wanted = list(getattr(source, "pins", None) or getattr(source, "pin", None) or ())
    try:
        resolved = role_tools.resolve_pins(wanted, known)
    except role_tools.RoleError:
        resolved = {}
    ordered = {role: resolved[role] for role in known if role in resolved}
    ordered[step.from_] = name
    return ordered


def _compare_roles(
    question: gate_tools.Question, source_pins: Mapping[str, str]
) -> tuple[str, str] | None:
    """Which two roles, if any, ``question`` itself compares — SPEC
    alpha.22, contract §4 2026-09-06: a ``left``/``right`` answer only
    means anything when the question's own passages span exactly two of
    the roles the gate's ``from:`` step could see, and which one is
    ``left`` (produced earlier in the run) is :attr:`~personacore.
    runbooks.state.GateState.source_pins`'s own order — never anything a
    form could claim.

    ``None`` for every other question (one passage, no roled passages, or
    more than two distinct roles) — :func:`personacore.runbooks.gates.
    answer_line` then refuses a ``left``/``right`` choice exactly like any
    option the question never offered.
    """
    order = list(source_pins.keys())
    roles: list[str] = []
    for passage in question.context:
        role = passage.role
        if role and role in source_pins and role not in roles:
            roles.append(role)
    if len(roles) != 2:
        return None
    roles.sort(key=order.index)
    return roles[0], roles[1]


def _with_conversation(state: RunState, conversation_id: str) -> RunState:
    """Stamp which conversation this run belongs to onto the state going out.

    ``RunState`` does not carry it and does not need to: a run's state lives
    in ``workspaces/<conversation id>/.run.json``, so the id is the *address*
    of the document rather than a field in it, and writing it inside would be
    one more thing that could disagree with where the file actually is. But
    the screen that starts a run has to redirect to the conversation it just
    created, and ``start`` hands it back a state and nothing else — so the id
    is attached here, on the way out, and is never written to disk.

    Set as a plain attribute rather than through a constructor argument
    because :class:`~personacore.runbooks.state.RunState` is that module's
    shape and not this one's; nothing here changes what ``.run.json`` holds.
    """
    with contextlib.suppress(AttributeError):
        state.conversation_id = conversation_id  # type: ignore[attr-defined]
    return state


def _tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _output_budget(step: ModelStep, workspace: Workspace, pins: Mapping[str, str]) -> int | None:
    """This step's output ceiling in tokens, or ``None`` for no ceiling.

    ``max_output_tokens`` is that number. ``max_output_ratio`` is a multiple of
    **the largest pinned file** — the input this step is working from — counted
    with the same approximation the output is, so the ratio compares like with
    like. A step with a ratio and nothing pinned has no input to be a ratio of,
    and gets no output ceiling rather than a ceiling of zero.
    """
    watchdog = step.watchdog
    if watchdog is None:
        return None
    if watchdog.max_output_tokens is not None:
        return watchdog.max_output_tokens
    if watchdog.max_output_ratio is None:  # pragma: no cover - schema forbids
        return None
    largest = 0
    for name in pins.values():
        with contextlib.suppress(WorkspaceError):
            largest = max(largest, _tokens(workspace.read(name)))
    if largest == 0:
        return None
    return int(largest * watchdog.max_output_ratio)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

_PYTHON_TYPE_FOR: dict[str, type] = {"integer": int, "string": str, "boolean": bool}

_TYPE_WORD = {"integer": "a whole number", "string": "text", "boolean": "yes or no"}


def _checked_inputs(runbook: Runbook, given: Mapping[str, Any]) -> dict[str, Any]:
    """Contract §2: every declared input, present and of its declared type.

    Checked at start rather than trusted from the form, because a runbook can
    also be started by something that is not the form, and because the
    substitution downstream would otherwise turn a missing input into a
    confusing failure four steps later. A declared default fills in for an
    input nobody gave.

    Contract §1.12's two iterating types are the exception to "of its declared
    type": ``range`` and ``list`` arrive as *text* ("1-12", "1,3,5") because
    that is what a person types, and what is stored in ``.run.json`` is what
    that text means — the items themselves, parsed once here rather than
    re-read by everything downstream that wants to count them.
    """
    checked: dict[str, Any] = {}
    for declared in runbook.inputs:
        value = given.get(declared.name, declared.default)
        if value is None:
            raise RunRefused(f"{declared.name} is needed before this run can start.")
        if declared.type in ITERABLE_INPUT_TYPES:
            checked[declared.name] = _checked_items(declared.name, declared.type, value)
            continue
        expected = _PYTHON_TYPE_FOR[declared.type]
        ok = (
            isinstance(value, bool)
            if declared.type == "boolean"
            else isinstance(value, expected) and not isinstance(value, bool)
        )
        if not ok:
            raise RunRefused(f"{declared.name} must be {_TYPE_WORD[declared.type]}.")
        checked[declared.name] = value
    return checked


def _checked_items(name: str, input_type: str, value: Any) -> list[Any]:
    """One ``range``/``list`` input's items, from text or from a list.

    A list is accepted as it stands so that a caller holding items already —
    a parent starting nothing, a state read back off disk — does not have to
    turn them back into a string to be told what they mean.
    """
    if isinstance(value, list):
        items = list(value)
    else:
        try:
            items = role_tools.parse_items(input_type, str(value))
        except role_tools.RoleError as exc:
            raise RunRefused(f"{name}: {exc}") from exc
    if not items:
        raise RunRefused(f"{name} is needed before this run can start.")
    return items


def _items_of(state: RunState, name: str) -> list[Any]:
    """The items a step-level ``foreach`` iterates. A run always has them:
    :func:`_checked_inputs` parsed them before the run started, and the
    validator proved the name is an input of a type that has them. The
    refusal is here for the run that reached this line anyway — a state file
    written by an older build, most likely."""
    value = state.inputs.get(name)
    if isinstance(value, list) and value:
        return list(value)
    raise _StepFailed(f"'foreach' names input {name}, which this run has no items for.")


def _still_answerable(state: RunState) -> bool:
    """The one park a parent waits through rather than records as an ending:
    an item stopped on a question somebody can still answer (contract §1.12,
    "gates inside a foreach park per item").

    :func:`_parked_at_a_gate` is the whole of the question except for one
    case it does not ask about — a gate step that *failed* after it had
    already recorded its questions still carries them, and a parent that read
    only the gate would wait for an answer to a run that has stopped for
    good.
    """
    if not _parked_at_a_gate(state):
        return False
    step = state.step(state.current) if state.current else None
    return step is not None and step.status is not StepStatus.FAILED


def _item_row(label: str, item: run_state.ItemState) -> str:
    """One line in the parent's chat about one item.

    The item's conversation id is in the text because that is all the web has
    to link with: the row is an ordinary transcript row, and a link to the
    chapter's own conversation is the only thing that makes a parent run
    usable rather than a list of names.
    """
    where = f" ({item.conversation_id})" if item.conversation_id else ""
    reason = f" — {item.reason}" if item.reason else ""
    return f"{label} {item.value}: {item.status}{reason}{where}"


def _checked_skip(runbook: Runbook, skip: Iterable[str]) -> list[str]:
    """Contract §1.11: the steps this run is not going to do.

    Two refusals, both before anything is created. A step id nobody has heard
    of is a picker and a runbook that no longer agree — the file was replaced
    between the form being drawn and Start being pressed — and running the
    rest of it would silently do something other than what was ticked. A step
    that is still in the run needing a role only a skipped step produces is
    the one contract §1.11 names, and the sentence says all three things: who
    needs it, what, and who was going to make it.
    """
    wanted = list(dict.fromkeys(str(one) for one in skip))
    if not wanted:
        return []
    known = {step.id for step in runbook.steps}
    for step_id in wanted:
        if step_id not in known:
            raise RunRefused(f"There is no step called {step_id} in this runbook.")
    skipped = set(wanted)
    for step in runbook.steps:
        if step.id in skipped:
            continue
        own = _roles_made(step)
        for role in sorted(_roles_needed(step)):
            if role in own:
                # A tool step may pin a role its own `files:` just made.
                continue
            makers = [one.id for one in runbook.steps if role in _roles_made(one)]
            if not makers or any(one not in skipped for one in makers):
                # Nothing in this file makes it (`canon`, an input) or
                # something still in the run does.
                continue
            raise RunRefused(
                f"step {step.id} needs role {role}, which the skipped step {makers[0]} produces"
            )
    return wanted


def _mark_skipped(state: RunState, skip: Sequence[str]) -> None:
    """Mark every skipped step done, and point the run at the first step that
    is actually going to run.

    Done *before* the run starts and written into ``.run.json`` with it, so
    the step loop needs to know nothing about skipping at all: it asks for the
    next pending step, and a step marked done is not one. A run with every
    step skipped is a run with nothing to do — ``current`` is left ``None``
    and ``status`` is left ``"running"``; :meth:`Runner._steps` reads that
    combination, on the loop's first turn, as its cue to call
    :meth:`Runner._finish` itself, the one place a run's status becomes
    ``"done"``.
    """
    if not skip:
        return
    wanted = set(skip)
    for step in state.steps:
        if step.id in wanted:
            step.status = StepStatus.DONE
            step.outputs = {}
            step.reason = SKIPPED
    first = next((step for step in state.steps if step.status == StepStatus.PENDING), None)
    state.current = first.id if first is not None else None
    state.updated = datetime.now(UTC).isoformat()


def _roles_made(step: Any) -> set[str]:
    """The file roles one step leaves behind: a tool step's ``files:`` keys,
    and — for every kind — its own id, which is what a model step's single
    output and a gate's answer file are pinned as (contract §2)."""
    made = {step.id}
    if isinstance(step, ToolStep):
        made.update(step.files)
    return made


def _roles_needed(step: Any) -> set[str]:
    """The file roles one step asks of the steps before it: what it pins,
    what it reads, and what its templates name."""
    if isinstance(step, ToolStep):
        return set(step.pin) | template_roles(*step.args.values(), *step.files.values())
    if isinstance(step, ModelStep):
        needed = set(step.pins) | template_roles(step.output, step.prompt_text)
        if step.apply is not None:
            needed.add(step.apply.onto)
        return needed
    if isinstance(step, GateStep):
        return {step.from_} | template_roles(step.answer)
    return set()


def _prompt_files(folder: Path) -> dict[str, str]:
    """Every prompt file beside a runbook, keyed the way a step's ``prompt:``
    names it (``prompts/p1.md``) — the mapping
    :func:`~personacore.runbooks.validate.validate_runbook` validates against.

    The store loads these for its own listing and keeps that private; a
    four-line reader here is cheaper than a public accessor nobody else wants,
    and it reads the same files by the same rule.
    """
    prompts_dir = folder / "prompts"
    if not prompts_dir.is_dir():
        return {}
    found: dict[str, str] = {}
    for path in sorted(prompts_dir.rglob("*.md")):
        if not path.is_file():
            continue
        with contextlib.suppress(OSError, UnicodeDecodeError):
            found[path.relative_to(folder).as_posix()] = path.read_text(encoding="utf-8")
    return found


def _set_aside(workspace: Workspace, name: str) -> None:
    """Contract §5: a dead attempt's output is renamed, never deleted.

    ``name`` is whatever the dead attempt recorded under the step's role —
    ``ch.p2.md`` for a step that finished and was rerun, ``ch.p2.md.partial``
    for one the watchdog cut off. Either way the *output's* name is the base,
    so both become ``ch.p2.md.interrupted.N`` with ``N`` the first free
    number: a resumed step writes its own output under the clean name, and
    every previous attempt is still on disk to read.

    Done with :func:`os.replace` inside the workspace folder rather than
    through :class:`~personacore.workspaces.Workspace`, which has no rename.
    Both names are checked against the workspace's own filename rule first, so
    this cannot address anything outside the folder, and the ``.sources.json``
    entry is deliberately left behind: the sidecar records where a file came
    from, and a dead attempt's leftovers came from nowhere anybody should be
    told about.
    """
    suffix = ".partial"
    base = name[: -len(suffix)] if name.endswith(suffix) else name
    for candidate in (name, f"{base}{suffix}"):
        source = workspace.path / candidate
        if not FILENAME_PATTERN.fullmatch(candidate) or not source.is_file():
            continue
        for index in range(1, 1000):
            target_name = f"{base}.interrupted.{index}"
            if not FILENAME_PATTERN.fullmatch(target_name):
                break
            target = workspace.path / target_name
            if target.exists():
                continue
            with contextlib.suppress(OSError):
                os.replace(source, target)
            break


def _took(started: float) -> str:
    """``2 min 40 s`` — how long a step took, told the way a person says it."""
    total = int(round(time.perf_counter() - started))
    if total < 60:
        return f"{total} s"
    return f"{total // 60} min {total % 60} s"


__all__ = [
    "NOT_AT_A_GATE",
    "NOT_AWAITING_TEXT",
    "PLUGIN_OFF",
    "RUNS_OFF",
    "SKIPPED",
    "STATE_UNREADABLE",
    "WAITING_FOR_ANSWERS",
    "RunRefused",
    "Runner",
]
