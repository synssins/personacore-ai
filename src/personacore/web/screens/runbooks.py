"""The Runbooks screen (contract ``working/contracts/runbook.md`` section 6,
plan ``working/team/alpha17/PLAN.md`` row ``screen``).

A runbook is a YAML file a plugin author or the household writes; this alpha
builds no runner (PLAN.md: "nothing executes yet") — only upload, listing,
validation and delete, plus the two switches that will gate a run once one
exists. Everything this screen needs from the actual store lives behind
``app.state.runbooks``, built by the ``validator`` subtask running in
parallel. **This module never imports anything from
``personacore.runbooks.*`` or ``personacore.config.runbooks``** — those
packages are somebody else's work in flight and may not exist in this tree
yet. Instead this screen talks to whatever object it finds at
``app.state.runbooks`` in the exact shape PLAN.md's Joints section promises
(``list()``, ``put()``, ``delete()``, ``plugin_enabled()``,
``set_plugin_enabled()``), duck-typed rather than imported. When the
attribute is absent — a core assembled without the runbooks package wired in
yet, or simply not this build — the page renders a plain sentence instead of
raising, per this task's own brief.

The same reasoning applies to the validator's ``ValidationError``: it is
never imported. An upload that a real store refuses is expected to raise
something carrying a ``messages: list[str]`` attribute (PLAN.md's Joints), so
that attribute is read with ``getattr`` and any exception at all — not just
that one class — is turned into a sentence on the page rather than a 500.

**Requires and Installed.** ``RunbookRecord`` carries ``requires: dict[str,
str]`` (plugin name -> minimum-version specifier, contract section 2's
``requires.plugins``) and ``installed: dict[str, str | None]`` (plugin name ->
installed version, or ``None`` when it is not installed) — spec section 6's
two columns, straight from the store, one line per plugin. Both are read with
``getattr(record, "requires", {})`` / ``getattr(record, "installed", {})``
rather than a bare attribute: a store built before these fields landed still
renders, with those two columns simply empty rather than a crash.

``RunbookRecord.verdict`` (Joints: ``Verdict.ok``, ``Verdict.reasons``) still
carries a compatibility *sentence* for the Verdict column — "Compatible." when
``ok``, else the reasons it is not (or "Not compatible." when a non-``ok``
verdict carries no reasons of its own).

**Run… (alpha.19, PLAN.md's ``web`` row) is interim — the Runbooks screen,
not the ``+`` picker the contract eventually wants (contract §1.1 was
revised to the picker the same day this alpha's own brief was written; the
picker is a later alpha).** The same duck-typing rule extends to
``app.state.runner``: this module never imports
``personacore.runbooks.runner`` or ``personacore.runbooks.state`` (the
``engine``/``state`` subtasks running in this same tree at the same time),
only reads the attribute with ``getattr`` and calls it in the exact shape
PLAN.md's Joints promise (``start``), catching whatever it raises for
``RunRefused``'s own ``message`` the same tolerant way an upload's
``ValidationError`` is read above. ``RunbookRecord`` carries no parsed
``inputs:``/``persona:`` of its own (:mod:`personacore.runbooks.store` never
promised the whole document, only the metadata this screen already used) —
:func:`_load_inputs_and_persona` reads ``record.path`` itself, tolerantly,
for exactly those two fields, rather than waiting on a new store method
that would be somebody else's file to add mid-alpha.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.audit.models import Owner, Surface
from personacore.conversations.service import ConversationService
from personacore.web.screens.chat_room import persona_choices
from personacore.web.screens.chat_run import GENERIC_RUN_REFUSAL, RUN_UNAVAILABLE, runner_for
from personacore.web.screens.plugin_common import (
    plugin_name_or_404,
    plugin_supports_runbooks,
)
from personacore.web.shared import UIContext, current_config

RUNBOOKS_PATH = "/admin/runbooks"
"""Where the screen lives — the nav link's own address."""

RUNBOOKS_ROUTE = "/runbooks"

RUNBOOK_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,40}$")
"""Contract section 2: a runbook's own identifier, ``[a-z0-9-]{1,40}``. Bound
before it ever reaches a store call, the same way :func:`plugin_name_or_404`
bounds a plugin name before it reaches one (spec section 7)."""

RUNBOOKS_UNAVAILABLE = "Runbooks are not available in this build."
"""What renders when ``app.state.runbooks`` is absent — a core assembled
without the package wired in, or one running an older build entirely. Never a
500 and never a blank page: the brief for this screen is explicit that this is
the whole of what "not available" looks like."""

RUNBOOKS_OFF_BANNER = "Runbook runs are off. Turn them on in Core settings."
"""Contract section 1.9's exact wording for the off state, shown when
``[runbooks] enabled`` is false. Files still list, still validate and still
delete; only running is gated, and there is no runner yet regardless."""

PLUGIN_OFF_REASON = "runbooks are off for this plugin"
"""Contract section 1.10's exact wording, shown beside a runbook whose own
plugin has not switched runbooks on."""

VERDICT_COMPATIBLE = "Compatible."

INSTALLED_NOT_INSTALLED = "not installed"

SOURCE_BUNDLED = "bundled"

SOURCE_UPLOADED = "uploaded"

UPLOAD_NO_FILE = "Nothing was uploaded: no file was chosen. Pick a .yaml or .zip and try again."

UPLOAD_NO_PLUGIN = "Nothing was uploaded: choose which plugin this runbook is for."

UPLOAD_PLUGIN_NOT_ELIGIBLE = (
    "Nothing was uploaded: {plugin!r} does not declare "
    "“[runbooks] supported = true”, so it cannot take an upload."
)

UPLOAD_UNAVAILABLE_REFUSAL = "Nothing was uploaded: runbooks are not available in this build."

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
"""Contract section 6's upload limit for one runbook file. Enforced here,
before the store ever sees the bytes — this screen reads at most one byte
past this limit, so a large upload is never buffered in full just to be
refused."""

UPLOAD_EXTENSIONS = (".yaml", ".yml", ".zip")
"""The only extensions a runbook upload may carry, checked case-insensitively
against the filename alone, before a single byte is read."""

UPLOAD_WRONG_TYPE = "A runbook is a .yaml file or a .zip holding one."

UPLOAD_TOO_LARGE = "That file is larger than 2 MB, which is the limit for a runbook."

GENERIC_UPLOAD_REFUSAL = "That file was refused."

UPLOAD_OK = "{title} ({version}) uploaded for {plugin}."

DELETE_TITLE = "Delete {title}?"

DELETE_BODY = (
    "This removes {title} ({id}) from {plugin}'s runbooks. The file and any "
    "prompt files that came with it are deleted. This cannot be undone; a "
    "bundled copy is restored the next time {plugin} is installed or "
    "reinstalled, but an uploaded one is gone for good."
)

DELETE_LABEL = "Delete this runbook"

DELETE_NOT_FOUND = "There is no such runbook."

DELETE_UNAVAILABLE = "Runbooks are not available in this build, so nothing can be deleted."

RUN_NOT_FOUND = "There is no such runbook."

RUN_TOO_LONG_HELP = "one number, a range like 1-12, or a list like 1,3,5"
"""Contract §1.12's own wording for a ``range``/``list`` input — this alpha's
schema (``personacore.runbooks.schema.InputType``) does not know either type
yet (PLAN.md: "NOT this alpha: foreach iteration"), so any ``type:`` this
screen does not recognise gets a plain text box with this same help rather
than being refused, which is what lets a runbook author write one ahead of
the core catching up without the Run… form breaking on it."""

RUN_INVALID_REFUSAL = "This runbook is not valid, so it cannot run."


def _run_refusal(row: dict[str, Any], *, core_enabled: bool) -> str | None:
    """Whether **Run…** may be pressed for one row, and the sentence to show
    when it may not (this task's own brief: "greyed with the compat verdict,
    and disabled with the contract's sentence when either switch is off").

    Reuses :func:`runbook_row`'s own fields rather than a second read of the
    record, so the Verdict column and the reason Run… is refused for can
    never disagree about the same runbook.
    """
    if not core_enabled:
        return RUNBOOKS_OFF_BANNER
    if row["greyed"]:
        # The contract's own sentence, verbatim — no formatted variant
        # (this task's rework item 2). It used to read "Nothing was
        # started: runbooks are off for this plugin for 'plugin'.", which
        # is not the sentence the contract names.
        return PLUGIN_OFF_REASON
    if not row["valid"]:
        return RUN_INVALID_REFUSAL
    if not row["verdict_ok"]:
        return row["verdict_text"] or "This runbook is not compatible."
    return None


def _load_inputs_and_persona(record: Any) -> tuple[list[dict[str, Any]], str | None]:
    """The runbook's own ``inputs:`` and default ``persona:`` (contract §2),
    read straight off its file.

    Never raises: a file that has vanished, that is not readable, or that is
    not even YAML any more comes back as "no inputs, no persona default" —
    the same "nothing to show, not a guess" this screen's other tolerant
    reads already practice (see the module docstring). This is a *second*
    parse of a file the validator already proved parses (``RunbookStore``
    would not have listed it as ``valid`` otherwise); it is not re-validated
    here, only read for two fields no ``RunbookRecord`` carries.
    """
    path = getattr(record, "path", None)
    if not path:
        return [], None
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return [], None
    if not isinstance(document, dict):
        return [], None
    inputs: list[dict[str, Any]] = []
    for item in document.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        inputs.append(
            {
                "name": name,
                "type": item.get("type") if isinstance(item.get("type"), str) else "string",
                "prompt": item.get("prompt") if isinstance(item.get("prompt"), str) else name,
                "default": item.get("default"),
            }
        )
    persona = document.get("persona")
    return inputs, persona if isinstance(persona, str) and persona else None


def _load_steps(record: Any) -> list[dict[str, Any]]:
    """The runbook's own ``steps:`` — id, kind, thinking, writes (WAVE2.md's
    picker: "the step preview: id, kind, thinking, writes") — read straight
    off its file, the same tolerant second read
    :func:`_load_inputs_and_persona` already gives ``inputs:``/``persona:``
    (see that function's own docstring for why a second parse of an
    already-validated file is fine here): a file that has vanished, is not
    readable, or is not even YAML any more comes back as "no steps" rather
    than raising.
    """
    path = getattr(record, "path", None)
    if not path:
        return []
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(document, dict):
        return []
    steps: list[dict[str, Any]] = []
    for item in document.get("steps") or []:
        if not isinstance(item, dict):
            continue
        step_id = item.get("id")
        if not isinstance(step_id, str) or not step_id:
            continue
        # There is no literal `writes:` key in the file format (see
        # docs/wiki/Runbooks.md) — a `tool` step names its roles in
        # `files:` (a role -> filename mapping), and a `model` step's own
        # `output:` is filed under the role that is simply its own step id
        # ("its own role is the step id", same doc). Both read here as the
        # role name(s) this preview column shows, never a filename: WAVE2.md
        # asks for "writes", and a role is the stable thing a later step
        # actually pins, where a filename is an implementation detail.
        files = item.get("files")
        if isinstance(files, dict) and files:
            writes: str | None = ", ".join(sorted(str(role) for role in files))
        elif isinstance(item.get("output"), str) and item.get("output"):
            writes = step_id
        else:
            writes = None
        steps.append(
            {
                "id": step_id,
                "kind": item.get("kind") if isinstance(item.get("kind"), str) else "",
                "thinking": bool(item.get("thinking", False)),
                "writes": writes,
            }
        )
    return steps


def _dependents_of(runner: Any, record: Any, step_id: str) -> list[str]:
    """A step's own dependents, from ``Runner.dependents_of`` (WAVE2.md's
    Joints: ``dependents_of(self, runbook, step_id) -> list[str]``) — read
    tolerantly: no runner, no such method, or a call that raises all answer
    "nothing depends on it", which only ever costs a checkbox staying
    enabled that a person could still choose not to touch; the server is
    still where an impossible skip is actually refused (``Runner.start``'s
    own validation).

    **Assumption flagged, not confirmed** (CLAUDE.md: "open the file before
    you write down what it does" — this is the file this module has). The
    Joints type ``runbook`` as a parsed ``Runbook``
    (``personacore.runbooks.schema``), which this module never imports (see
    the module docstring) and so never holds one of. ``record`` — this
    screen's own :class:`RunbookRecord` — is passed here instead, as the
    closest duck-typed stand-in this module has for "the runbook the picker
    is showing". If the real ``Runner`` requires the parsed object instead,
    this call needs a second look once ``iteration``/``gates`` land — noted
    here rather than guessed silently past.
    """
    getter = getattr(runner, "dependents_of", None) if runner is not None else None
    if getter is None:
        return []
    try:
        return [str(one) for one in (getter(record, step_id) or ())]
    except Exception:  # noqa: BLE001 - a broken dependency read enables a box, not a 500
        return []


def _step_rows(
    steps: list[dict[str, Any]],
    runner: Any,
    record: Any,
    *,
    requested_run: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """One row per step for the picker's own step preview (WAVE2.md's
    ``picker`` row): id, kind, thinking, writes, and a "run this step"
    checkbox, ticked by default.

    A step a later un-skipped step depends on is disabled and forced to
    run, with the hint "needed by {id}" — computed against whichever steps
    are, right now, set to run: every step, for the form's first
    (unposted) view, or ``requested_run``'s own reading of a form already
    posted once (:func:`_requested_run_from_form`) when a blocked Start is
    re-rendering the same page.

    Walked **back to front**, not in file order, and that is load-bearing.
    A browser never posts a disabled checkbox's own ``value="1"`` at all —
    only the always-present hidden field's ``"0"`` — so
    :func:`_requested_run_from_form`'s raw reading of a *disabled* step's
    own field is meaningless and must never be trusted. Walking back to
    front means that by the time a row asks "is my dependent running?", that
    dependent's own row has already been resolved — forced ``True`` if it
    was itself disabled — so the answer comes from what a dependent is
    actually going to do, never from a form field a browser could not have
    sent. A dependent is always later in the file (contract §2: a role can
    only be pinned or read once something earlier has produced it), so this
    order always resolves every dependent before the step that names it.
    """
    requested = dict(requested_run or {})
    rows = [{**step, "running": requested.get(step["id"], True)} for step in steps]
    resolved: dict[str, bool] = {}
    for row in reversed(rows):
        dependents = _dependents_of(runner, record, row["id"])
        blocking = next((dep for dep in dependents if resolved.get(dep, True)), None)
        row["disabled"] = blocking is not None
        row["hint"] = f"needed by {blocking}" if blocking else ""
        if row["disabled"]:
            row["running"] = True
        resolved[row["id"]] = row["running"]
    return rows


def _requested_run_from_form(form: Any, steps: list[dict[str, Any]]) -> dict[str, bool]:
    """Which steps a posted form's own checkboxes asked to run, before
    disabling is recomputed against it (:func:`_step_rows`) — read straight
    off the form and never trusted for anything else; :func:`_collect_skip`
    is what actually decides what is skipped, once disabling is known.
    """
    return {step["id"]: ("1" in form.getlist(f"run__{step['id']}")) for step in steps}


def _collect_skip(rows: list[dict[str, Any]]) -> list[str]:
    """Which step ids ``Start`` should skip, from :func:`_step_rows`'s own
    resolved ``running`` flag — already forced ``True`` for a row this
    screen disabled, so a ``disabled`` row is never skipped regardless of
    what a tampered request sent for it (``_step_rows`` never even reads a
    disabled row's own field back off the form). ``Runner.start`` is still
    the one that refuses an impossible skip outright (WAVE2.md: "the server
    still validates at Start and refuses with the sentence") — this is only
    what the picker itself will ever offer.
    """
    return [row["id"] for row in rows if not row["running"]]


def _accepts_skip(runner: Any) -> bool:
    """Whether ``Runner.start`` takes a ``skip`` keyword — the same
    discovery :func:`personacore.web.screens.chat_exchange._takes` makes for
    a chat runner's own newer keywords, and for the same reason: a
    ``Runner`` from before ``skip`` existed raises ``TypeError`` on it, and
    this screen reports a refusal rather than crashing a Start it could
    otherwise have made.
    """
    try:
        return "skip" in inspect.signature(runner.start).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def _input_field(spec: dict[str, Any], values: dict[str, Any] | None) -> dict[str, Any]:
    """One ``inputs:`` entry, as ``runbook_run.html`` draws it.

    ``integer``/``string``/``boolean`` (the schema's own three, contract §2)
    map onto ``number``/``text``/``checkbox``; anything else — ``range``,
    ``list``, or a type this screen has simply never seen — is a text box
    passed through as a string, with :data:`RUN_TOO_LONG_HELP` (this task's
    own brief, and contract §1.12 verbatim).
    """
    name = spec["name"]
    itype = spec.get("type")
    prompt = spec.get("prompt") or name
    default = spec.get("default")
    posted = None if values is None else values.get(name)
    if itype == "boolean":
        checked = bool(posted) if posted is not None else bool(default)
        return {"name": name, "prompt": prompt, "control": "checkbox", "checked": checked}
    control = "number" if itype == "integer" else "text"
    help_text = None if itype in ("integer", "string") else RUN_TOO_LONG_HELP
    value = posted if posted is not None else ("" if default is None else default)
    return {"name": name, "prompt": prompt, "control": control, "value": value, "help": help_text}


def _collect_inputs(form: Any, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Posted form values, typed per :func:`_input_field`'s own ``control``.

    A number field that will not parse is passed through as the raw string
    rather than refused here — ``Runner.start`` is where a bad input becomes
    a plain sentence (``RunRefused``, PLAN.md's Joints), and duplicating that
    judgement in two places is how the two end up disagreeing about what
    "bad" means.
    """
    inputs: dict[str, Any] = {}
    for field in fields:
        name = field["name"]
        if field["control"] == "checkbox":
            inputs[name] = form.get(name) is not None
        elif field["control"] == "number":
            raw = str(form.get(name) or "").strip()
            try:
                inputs[name] = int(raw)
            except ValueError:
                inputs[name] = raw
        else:
            inputs[name] = str(form.get(name) or "")
    return inputs


def _format_requires(requires: dict[str, str]) -> list[str]:
    """One "plugin specifier" line per required plugin (contract section 6),
    e.g. ``storybook >=2.1.0``."""
    return [f"{plugin} {specifier}" for plugin, specifier in requires.items()]


def _format_installed(installed: dict[str, str | None]) -> list[str]:
    """One "plugin version" line per required plugin, e.g. ``storybook
    2.3.1``, or ``storybook not installed`` when the version is ``None``."""
    return [
        f"{plugin} {version}" if version else f"{plugin} {INSTALLED_NOT_INSTALLED}"
        for plugin, version in installed.items()
    ]


def _eligible_plugins(ctx: UIContext, listing: Any) -> list[str]:
    """Every installed plugin whose manifest declares
    ``[runbooks] supported = true`` (contract section 6), alphabetically —
    the whole of what the upload selector and the picker (a later alpha) may
    ever offer."""
    names = [view.name for view in listing.plugins]
    return sorted(name for name in names if plugin_supports_runbooks(ctx.layout, name))


def runbook_row(record: Any, *, plugin_enabled: bool) -> dict[str, Any]:
    """One :class:`~personacore.runbooks.store.RunbookRecord` (duck-typed —
    see the module docstring) as the template wants it.

    ``record.valid is False`` takes over the Verdict cell: an invalid file's
    own problems (from the validator, at upload/scan time) are what an
    operator needs to see, and a compatibility verdict about a file that will
    not even parse would be a second, less useful thing to say about the
    same failure. Requires and Installed render regardless — they are the
    file's own declared needs and the scan's own facts, valid or not.
    """
    verdict = record.verdict
    valid = bool(record.valid)
    ok = bool(getattr(verdict, "ok", False)) if valid else False
    reasons = list(getattr(verdict, "reasons", None) or [])
    if not valid:
        verdict_text = ""
    elif ok:
        verdict_text = VERDICT_COMPATIBLE
    else:
        verdict_text = " ".join(reasons) if reasons else "Not compatible."
    requires = getattr(record, "requires", None) or {}
    installed = getattr(record, "installed", None) or {}
    return {
        "plugin": record.plugin,
        "id": record.id,
        "title": record.title or record.id,
        "version": record.version,
        "requires": _format_requires(requires),
        "installed": _format_installed(installed),
        "source": SOURCE_BUNDLED if record.bundled else SOURCE_UPLOADED,
        "valid": valid,
        "problems": list(record.problems or []),
        "verdict_ok": ok,
        "verdict_text": verdict_text,
        "plugin_enabled": plugin_enabled,
        "greyed": not plugin_enabled,
        "grey_reason": PLUGIN_OFF_REASON,
    }


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the Runbooks screen: the list, the upload, and delete's
    plain-page confirmation (the pattern ``keys.py``'s key revocation uses)."""
    templates = ctx.templates
    _shell = ctx.shell
    layout = ctx.layout

    def _store(request: Request) -> Any | None:
        """``request.app.state.runbooks``, or ``None``.

        ``getattr`` rather than a bare attribute read, the same tolerance
        ``memory.py``'s own ``_store`` gives ``memory_store``: a core
        assembled without this wired in yet must render the "not available"
        line rather than raise ``AttributeError``.
        """
        return getattr(request.app.state, "runbooks", None)

    def _core_runbooks_enabled() -> bool:
        """``[runbooks] enabled`` off ``core.toml`` — read the document the
        same way every other screen's switch is (``current_config``), not off
        ``app.state``, so what this screen says is what the file says."""
        current, _unreadable = current_config(layout)
        if current is None:
            return False
        section = current.settings.get("runbooks")
        if isinstance(section, dict):
            return bool(section.get("enabled", False))
        return False

    async def _rows(request: Request, store: Any) -> list[dict[str, Any]]:
        core_enabled = _core_runbooks_enabled()
        records = store.list()
        rows: list[dict[str, Any]] = []
        for record in records:
            row = runbook_row(record, plugin_enabled=bool(store.plugin_enabled(record.plugin)))
            refusal = _run_refusal(row, core_enabled=core_enabled)
            row["run_refusal"] = refusal
            row["can_run"] = refusal is None
            rows.append(row)
        return rows

    async def _page_context(
        request: Request, *, upload_result: dict[str, str] | None = None
    ) -> dict[str, Any]:
        store = _store(request)
        core_enabled = _core_runbooks_enabled()
        base = {
            **await _shell(request, "runbooks"),
            "available": store is not None,
            "unavailable_message": RUNBOOKS_UNAVAILABLE,
            "core_enabled": core_enabled,
            "banner": None if core_enabled else RUNBOOKS_OFF_BANNER,
            "rows": [],
            "plugin_choices": [],
            "upload_result": upload_result,
        }
        if store is None:
            return base
        listing = await ctx.scans.current()
        base["rows"] = await _rows(request, store)
        base["plugin_choices"] = _eligible_plugins(ctx, listing)
        return base

    @router.get(
        RUNBOOKS_ROUTE, response_class=HTMLResponse, summary="Runbooks: upload, list, delete"
    )
    async def runbooks_page(request: Request) -> HTMLResponse:
        """Contract section 6's screen. Lists and validates whatever switch
        state the core is in — only *running* one is gated (there is no
        runner in this alpha at all)."""
        return templates.TemplateResponse(
            request=request, name="runbooks.html", context=await _page_context(request)
        )

    @router.post(RUNBOOKS_ROUTE, response_class=HTMLResponse, summary="Upload a runbook")
    async def runbooks_upload(request: Request) -> HTMLResponse:
        """Validate on upload (contract section 6) — a refusal is always a
        sentence on this page, never a 500, whatever the store raises.

        Two checks run before the store ever sees the bytes (this task's own
        brief): the filename's extension, checked before anything is read,
        and a 2 MB cap, enforced by never reading past one byte over it."""
        form = await request.form()
        try:
            plugin = str(form.get("plugin") or "").strip()
            upload = form.get("archive")
            filename = str(getattr(upload, "filename", "") or "")
            wrong_type = bool(filename) and not filename.lower().endswith(UPLOAD_EXTENSIONS)
            data = b""
            too_large = False
            if filename and not wrong_type and hasattr(upload, "read"):
                # Read the bytes while the multipart envelope is still open —
                # a spooled upload is not necessarily readable once
                # `form.close()` has run (see `plugin_install.py`'s own
                # `_review_upload`, which reads before closing for the same
                # reason). Capped at one byte past the limit: enough to tell
                # "too large" from "exactly at the limit" without ever
                # buffering a large upload in full.
                data = await upload.read(MAX_UPLOAD_BYTES + 1)
                too_large = len(data) > MAX_UPLOAD_BYTES
        finally:
            await form.close()

        store = _store(request)
        if store is None:
            return templates.TemplateResponse(
                request=request,
                name="runbooks.html",
                context=await _page_context(
                    request,
                    upload_result={"kind": "refused", "message": UPLOAD_UNAVAILABLE_REFUSAL},
                ),
            )
        listing = await ctx.scans.current()
        eligible = _eligible_plugins(ctx, listing)
        if not plugin:
            message = UPLOAD_NO_PLUGIN
        elif plugin not in eligible:
            message = UPLOAD_PLUGIN_NOT_ELIGIBLE.format(plugin=plugin)
        elif not filename:
            message = UPLOAD_NO_FILE
        elif wrong_type:
            message = UPLOAD_WRONG_TYPE
        elif too_large:
            message = UPLOAD_TOO_LARGE
        else:
            try:
                record = store.put(plugin, filename, data)
            except Exception as exc:  # noqa: BLE001 - never a 500 on an upload; see module docstring
                sentences = list(getattr(exc, "messages", None) or [])
                if not sentences:
                    sentences = [str(exc).strip() or GENERIC_UPLOAD_REFUSAL]
                return templates.TemplateResponse(
                    request=request,
                    name="runbooks.html",
                    context=await _page_context(
                        request,
                        upload_result={"kind": "refused", "message": " ".join(sentences)},
                    ),
                )
            message = UPLOAD_OK.format(
                title=getattr(record, "title", None) or getattr(record, "id", plugin),
                version=getattr(record, "version", ""),
                plugin=plugin,
            )
            return templates.TemplateResponse(
                request=request,
                name="runbooks.html",
                context=await _page_context(
                    request, upload_result={"kind": "ok", "message": message}
                ),
            )
        return templates.TemplateResponse(
            request=request,
            name="runbooks.html",
            context=await _page_context(
                request, upload_result={"kind": "refused", "message": message}
            ),
        )

    def _runbook_id_or_404(runbook_id: str) -> str:
        if not RUNBOOK_ID_PATTERN.match(runbook_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, DELETE_NOT_FOUND)
        return runbook_id

    async def _record_or_404(request: Request, plugin: str, runbook_id: str) -> dict[str, Any]:
        plugin_name_or_404(plugin)
        _runbook_id_or_404(runbook_id)
        store = _store(request)
        if store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, DELETE_UNAVAILABLE)
        for record in store.list():
            if record.plugin == plugin and record.id == runbook_id:
                return {"plugin": plugin, "id": runbook_id, "title": record.title or record.id}
        raise HTTPException(status.HTTP_404_NOT_FOUND, DELETE_NOT_FOUND)

    def _delete_confirm_context(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": DELETE_TITLE.format(title=row["title"]),
            "body": DELETE_BODY.format(title=row["title"], id=row["id"], plugin=row["plugin"]),
            "confirm_label": DELETE_LABEL,
        }

    @router.get(
        "/runbooks/{plugin}/{runbook_id}/delete/confirm",
        response_class=HTMLResponse,
        summary="Confirm deleting one runbook (page)",
    )
    async def runbook_delete_confirm_page(
        request: Request, plugin: str, runbook_id: str
    ) -> HTMLResponse:
        """The no-script fallback (ADR-0020), same shape as
        ``keys.py``'s ``key_revoke_confirm_page``."""
        row = await _record_or_404(request, plugin, runbook_id)
        return templates.TemplateResponse(
            request=request,
            name="confirm_page.html",
            context={
                **await _shell(request, "runbooks"),
                **_delete_confirm_context(row),
                "action": f"/admin/runbooks/{plugin}/{runbook_id}/delete",
                "back_href": RUNBOOKS_PATH,
                "back_label": "← Runbooks",
            },
        )

    @router.get(
        "/runbooks/{plugin}/{runbook_id}/delete/confirm/fragment",
        response_class=HTMLResponse,
        summary="Confirm deleting one runbook",
    )
    async def runbook_delete_confirm(
        request: Request, plugin: str, runbook_id: str
    ) -> HTMLResponse:
        row = await _record_or_404(request, plugin, runbook_id)
        return templates.TemplateResponse(
            request=request,
            name="fragments/confirm.html",
            context={
                **_delete_confirm_context(row),
                "action": f"/admin/runbooks/{plugin}/{runbook_id}/delete",
                "target": "body",
            },
        )

    @router.post(
        "/runbooks/{plugin}/{runbook_id}/delete",
        response_class=HTMLResponse,
        response_model=None,
        summary="Delete one runbook",
    )
    async def runbook_delete(
        request: Request, plugin: str, runbook_id: str
    ) -> HTMLResponse | RedirectResponse:
        """Same shape as ``keys.py``'s ``key_revoke``: a plain form post gets a
        real redirect, an htmx caller gets the fragment it asked the dialog
        for — except there is no swap-target list fragment for this screen
        yet, so both paths land on the full page, which is always correct."""
        plugin_name_or_404(plugin)
        _runbook_id_or_404(runbook_id)
        store = _store(request)
        if store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, DELETE_UNAVAILABLE)
        store.delete(plugin, runbook_id)
        if request.headers.get("HX-Request", "").lower() == "true":
            return templates.TemplateResponse(
                request=request, name="runbooks.html", context=await _page_context(request)
            )
        return RedirectResponse(RUNBOOKS_PATH, status_code=status.HTTP_303_SEE_OTHER)

    # -- Run… (interim: this screen, not the + picker; contract §3 "Start") --

    async def _row_or_404(request: Request, plugin: str, runbook_id: str) -> tuple[Any, dict]:
        """The record *and* its row (``runbook_row``'s own dict, plus
        ``run_refusal``/``can_run``) for one runbook — the run form needs
        both: the record for its file (:func:`_load_inputs_and_persona`) and
        the row for whether it may run at all."""
        plugin_name_or_404(plugin)
        _runbook_id_or_404(runbook_id)
        store = _store(request)
        if store is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, RUN_NOT_FOUND)
        core_enabled = _core_runbooks_enabled()
        for record in store.list():
            if record.plugin == plugin and record.id == runbook_id:
                row = runbook_row(record, plugin_enabled=bool(store.plugin_enabled(plugin)))
                refusal = _run_refusal(row, core_enabled=core_enabled)
                row["run_refusal"] = refusal
                row["can_run"] = refusal is None
                return record, row
        raise HTTPException(status.HTTP_404_NOT_FOUND, RUN_NOT_FOUND)

    async def _run_form_context(
        request: Request,
        plugin: str,
        runbook_id: str,
        *,
        blocking_refusal: str | None = None,
        runtime_refusal: str | None = None,
        values: dict[str, Any] | None = None,
        posted_persona: str | None = None,
        requested_run: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """The form's context — twice a refusal can happen, and only one of
        them hides the form.

        ``blocking_refusal`` (or ``row["run_refusal"]`` itself: an off
        switch, an incompatible or invalid runbook) means nothing here could
        ever start, so the form is gone and only the sentence remains.
        ``runtime_refusal`` — ``Runner.start`` raising ``RunRefused`` over
        these particular inputs — is this task's own brief's "renders its
        sentence *on* the form": the sentence appears above a form that is
        still there, because different inputs might not be refused.
        """
        record, row = await _row_or_404(request, plugin, runbook_id)
        inputs, persona_default = _load_inputs_and_persona(record)
        fields = [_input_field(spec, values) for spec in inputs]
        default_persona = posted_persona or persona_default or ctx.personas.default_persona
        blocking = blocking_refusal or row["run_refusal"]
        steps = _load_steps(record)
        step_rows = _step_rows(steps, runner_for(request), record, requested_run=requested_run)
        return {
            **await _shell(request, "runbooks"),
            "plugin": plugin,
            "id": runbook_id,
            "title": row["title"],
            "refusal": blocking or runtime_refusal,
            "runnable": blocking is None,
            "steps": step_rows,
            "fields": fields,
            "personas": persona_choices(ctx.personas, default=default_persona),
        }

    @router.get(
        "/runbooks/{plugin}/{runbook_id}/run",
        response_class=HTMLResponse,
        summary="Run a runbook: the inputs form",
    )
    async def runbook_run_form(request: Request, plugin: str, runbook_id: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="runbook_run.html",
            context=await _run_form_context(request, plugin, runbook_id),
        )

    @router.post(
        "/runbooks/{plugin}/{runbook_id}/run",
        response_class=HTMLResponse,
        response_model=None,
        summary="Start a run",
    )
    async def runbook_run_start(
        request: Request, plugin: str, runbook_id: str
    ) -> HTMLResponse | RedirectResponse:
        """Contract §3 "Start" (interim: this screen's own form, not the +
        picker — see the module docstring). ``RunRefused`` — read the same
        tolerant way an upload's ``ValidationError`` is above — renders its
        sentence back on this same form (200), never a 500 and never a
        redirect that pretends something started."""
        record, row = await _row_or_404(request, plugin, runbook_id)
        form = await request.form()
        try:
            inputs_raw, persona_default = _load_inputs_and_persona(record)
            fields = [_input_field(spec, None) for spec in inputs_raw]
            inputs = _collect_inputs(form, fields)
            posted_persona = str(form.get("persona") or "").strip() or None
            steps = _load_steps(record)
            requested_run = _requested_run_from_form(form, steps)
        finally:
            await form.close()

        runner = runner_for(request)
        step_rows = _step_rows(steps, runner, record, requested_run=requested_run)
        skip = _collect_skip(step_rows)

        if row["run_refusal"] is not None:
            return templates.TemplateResponse(
                request=request,
                name="runbook_run.html",
                context=await _run_form_context(
                    request,
                    plugin,
                    runbook_id,
                    values=inputs,
                    posted_persona=posted_persona,
                    requested_run=requested_run,
                ),
            )

        user = ctx.require_user(request)
        owner = Owner.profile(user.id)
        if runner is None:
            return templates.TemplateResponse(
                request=request,
                name="runbook_run.html",
                context=await _run_form_context(
                    request,
                    plugin,
                    runbook_id,
                    blocking_refusal=RUN_UNAVAILABLE,
                    values=inputs,
                    posted_persona=posted_persona,
                    requested_run=requested_run,
                ),
            )
        try:
            start_kwargs: dict[str, Any] = {
                "owner": owner,
                "plugin": plugin,
                "runbook_id": runbook_id,
                "inputs": inputs,
                "persona": posted_persona,
            }
            # A runner from before `skip` existed (WAVE2.md's `state2`/
            # `iteration` subtasks, landing in this same tree) raises
            # `TypeError` on a keyword it does not know — see `_accepts_skip`
            # — so this is only ever sent to a runner that has it, and never
            # sent at all otherwise (an older runner simply cannot skip a
            # step, which is this feature's own honest starting state).
            if _accepts_skip(runner):
                start_kwargs["skip"] = skip
            state = await runner.start(**start_kwargs)
        except Exception as exc:  # noqa: BLE001 - RunRefused's sentence, never a 500
            message = getattr(exc, "message", None)
            if not isinstance(message, str) or not message.strip():
                message = str(exc).strip() or GENERIC_RUN_REFUSAL
            return templates.TemplateResponse(
                request=request,
                name="runbook_run.html",
                context=await _run_form_context(
                    request,
                    plugin,
                    runbook_id,
                    runtime_refusal=message,
                    values=inputs,
                    posted_persona=posted_persona,
                    requested_run=requested_run,
                ),
            )

        # `state.conversation_id` is this module's own assumption about a
        # Joint PLAN.md does not name — see chat_run.py's module docstring.
        # Missing or unresolvable, this still starts the run; it only fails
        # to know which chat to send the operator straight to.
        target = "/admin/chat"
        conversation_id = getattr(state, "conversation_id", "") or ""
        if conversation_id:
            conversations = ConversationService(ctx.audit, surface=Surface.ADMIN_UI)
            conversation = await conversations.resolve(owner, conversation_id=conversation_id)
            if conversation is not None:
                target = f"/admin/chat?c={quote(conversation.started_at.isoformat())}"
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


__all__ = [
    "DELETE_BODY",
    "DELETE_LABEL",
    "DELETE_NOT_FOUND",
    "DELETE_TITLE",
    "DELETE_UNAVAILABLE",
    "GENERIC_UPLOAD_REFUSAL",
    "INSTALLED_NOT_INSTALLED",
    "MAX_UPLOAD_BYTES",
    "PLUGIN_OFF_REASON",
    "RUNBOOKS_OFF_BANNER",
    "RUNBOOKS_PATH",
    "RUNBOOKS_ROUTE",
    "RUNBOOKS_UNAVAILABLE",
    "RUNBOOK_ID_PATTERN",
    "RUN_INVALID_REFUSAL",
    "RUN_NOT_FOUND",
    "RUN_TOO_LONG_HELP",
    "SOURCE_BUNDLED",
    "SOURCE_UPLOADED",
    "UPLOAD_EXTENSIONS",
    "UPLOAD_NO_FILE",
    "UPLOAD_NO_PLUGIN",
    "UPLOAD_OK",
    "UPLOAD_PLUGIN_NOT_ELIGIBLE",
    "UPLOAD_TOO_LARGE",
    "UPLOAD_UNAVAILABLE_REFUSAL",
    "UPLOAD_WRONG_TYPE",
    "VERDICT_COMPATIBLE",
    "register",
    "runbook_row",
]
