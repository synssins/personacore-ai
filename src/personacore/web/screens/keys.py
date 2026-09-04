"""The access keys screen (spec section 6): what has been issued, issuing one,
and revoking one.

A key is shown once, when it is issued, and never again - so the screen that
issues it is also the only place it is ever displayed. What a key is allowed to
do is in ``key_policy``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore.admin.authn import require_admin
from personacore.admin.models import (
    ApiKeyIssueRequest,
)
from personacore.audit.models import AuditOutcome
from personacore.config.settings import KeylessSettings
from personacore.contracts.policy import PolicyProfile, ProfileKind
from personacore.web.screens.key_policy import (
    KIND_CHOICES,
    MEMORY_CHOICES,
    RISK_CHOICES,
    TOOLS_ALL,
    _multi,
    generated_profile_id,
    key_rows,
    policy_refusal,
    profile_from_form,
    tool_names,
)
from personacore.web.screens.personas import (
    default_persona,
    persona_listing,
)
from personacore.web.shared import (
    NO_KEY_OPERATIONS,
    UIContext,
    api_handler,
    current_config,
    refusal,
)

# ---------------------------------------------------------------------------
# Access keys — one credential, one policy profile
# ---------------------------------------------------------------------------
#
# Two rules run this screen, and both are about what must *not* appear on it:
#
# * **A key value is rendered exactly once, at the moment it is issued.** The
#   core keeps a SHA-256 and nothing else, so there is no second render to
#   suppress — but there is a template that could be handed the value twice, and
#   the listing fragment is deliberately built from `ApiKeyView`, which has no
#   field for it and forbids extras.
# * **The profile form is a form, not a JSON box.** `PolicyProfile` is what
#   decides whether a display widget can unlock a door (spec §5.4), so every
#   field it holds gets a control; the raw tab stays beside it for anything a
#   form cannot say, exactly as the plugin and core settings screens keep theirs.
#
# **Keyless access lives here too** (ADR-0018), because it is the same question
# as a key: who may call `/v1`, and what may they do. It is one switch and one
# tool list, and the ceilings are not offered at all — `KeylessSettings.profile`
# fixes them and `PolicyProfile` refuses the result if they were ever loosened,
# so there is no control here that could quietly make "keyless" mean "anything".
# It is the API's door and only the API's door: the admin surface is `[auth]`,
# decided on the Core settings screen, and nothing on this page reaches it.
#
# The anonymous ceilings are the third thing, and the rule there is *don't repeat
# them*. `PolicyProfile` refuses an over-privileged anonymous profile at
# construction (ADR-0003), with a sentence written for a person. This screen
# offers the choice, lets the model refuse it, and prints what the model said —
# so there is exactly one statement of what anonymous may do, and it is the one
# that is enforced.

KEY_ISSUE_TABS = ("form", "raw")

TOOLS_ALL_NOTE = (
    "“Every tool” means every tool installed right now, by name. A plugin "
    "installed later is not included."
)

KEY_NOTE_REQUIRED = "Say what the key is for."

MAX_KEY_NOTE_CHARS = 200
"""``ApiKeyIssueRequest.note``'s own bound, restated so the box can stop a value
before it becomes a 422."""

KEYLESS_CONSEQUENCE = (
    "With this on, anything that can reach this port can talk to the assistant "
    "and use the tools ticked below. There is no password and no key."
)
"""The one fact the switch itself cannot carry (ADR-0018).

Not a warning and not a hedge — the switch already says what it does, and a
second line reassuring anybody would be noise. This says what the operator is
choosing, which is the thing a checkbox has no room for: the reach is "whatever
can get to the port", and that is a different sentence from "keyless"."""

KEYLESS_TOOLS_NOTE = (
    "None ticked means conversation only. Names are exact — installing a plugin "
    "later never adds to this list."
)

KEYLESS_SAVED_ON = "Keyless access is on. {tools}"

KEYLESS_SAVED_OFF = "Keyless access is off. The API needs a key again."

KEYLESS_TOOLS_NONE = "A keyless caller can talk to the assistant and nothing else."

KEYLESS_TOOLS_SOME = "A keyless caller may use: {names}."

KEYLESS_UNCHANGED = "Nothing changed."


KEY_REVOKE_TITLE = "Revoke {note}?"

KEY_REVOKE_LABEL = "Revoke this key"


def key_revoke_body(*, note: str, summary: str) -> str:
    """The revocation confirmation, in the specific terms spec §9 asks for.

    Names the client, restates what the key is *for* — because "revoke key
    a1b2c3d4?" is a question nobody can answer — and says plainly that the value
    cannot come back: revocation deletes the record, and the plaintext was never
    stored, so whatever holds it needs a new key rather than the same one again.
    """
    return (
        f"“{note}” stops working the moment this is confirmed — it is the key that "
        f"{summary}. The value cannot be recovered or re-issued: it was shown once "
        "and only a hash was kept, so whatever holds it will need a new key."
    )


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the keys list, the issue form and post, and revocation."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import KEY_ID_PATTERN, _record_change

    templates = ctx.templates
    scans = ctx.scans
    _shell = ctx.shell
    _default_persona = partial(default_persona, ctx)
    _persona_listing = partial(persona_listing, ctx)
    _refusal = refusal


    # -- access keys -------------------------------------------------------

    async def _persona_names() -> dict[str, str]:
        """Identifier → display name, for reading a profile back in words."""
        listing = await _persona_listing(_default_persona())
        return {summary.name: summary.display_name for summary in listing.personas}

    async def _keys_context(
        request: Request,
        *,
        refused_message: str | None = None,
        save_result: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            **await _shell(request, "keys"),
            **await _key_list_context(request),
            **await _keyless_context(),
            "refused_message": refused_message,
            "keyless_save": dict(save_result) if save_result else None,
        }

    async def _keyless_context() -> dict[str, Any]:
        """The keyless panel, read from the settings file each time.

        Off the document rather than off a cached object, so the switch on
        screen is the one in ``core.toml``. A file that cannot be read leaves
        the switch off and says why: with nothing to merge a change into,
        offering a working control would be offering a save that discards a
        document nobody has been shown.
        """
        current, unreadable = current_config(ctx.layout)
        keyless = KeylessSettings()
        if current is not None:
            section = current.settings.get("keyless")
            if isinstance(section, Mapping):
                try:
                    keyless = KeylessSettings.model_validate(dict(section))
                except ValueError as exc:
                    unreadable = policy_refusal(exc)
        listing = await scans.current()
        return {
            "keyless": {
                "enabled": keyless.enabled,
                "tools": list(keyless.allowed_tools),
            },
            "keyless_tools": tool_names(listing),
            "keyless_consequence": KEYLESS_CONSEQUENCE,
            "keyless_tools_note": KEYLESS_TOOLS_NOTE,
            "keyless_unreadable": unreadable,
        }

    def _keyless_summary(*, enabled: bool, tools: Sequence[str]) -> str:
        """What the save actually did, in the terms the operator chose it in."""
        if not enabled:
            return KEYLESS_SAVED_OFF
        allowed = (
            KEYLESS_TOOLS_SOME.format(names=", ".join(tools))
            if tools
            else KEYLESS_TOOLS_NONE
        )
        return KEYLESS_SAVED_ON.format(tools=allowed)

    @router.post(
        "/keys/keyless",
        response_class=HTMLResponse,
        summary="Turn keyless access on or off",
    )
    async def keyless_save(request: Request) -> HTMLResponse:
        """ADR-0018's switch, written to ``[keyless]`` in ``core.toml``.

        Through the settings document and ``save_config``, the same
        persist-and-apply path the speech and recogniser switches take, so the
        file and what ``/v1`` is actually doing cannot disagree — the router
        asks for this setting on every request.

        **Only the exposed API.** Nothing here touches ``[auth]``, which is the
        admin surface's own door (ADR-0032): this post merges one section and
        leaves the rest of the document exactly as it found it.

        A tool name this core does not offer is dropped rather than saved. The
        allowlist is exact names, and a name nothing answers to is either a typo
        or a plugin that has gone — neither is something to write into a
        security setting on the operator's behalf.
        """
        user = require_admin(ctx.require_user(request))
        form = await request.form()
        try:
            enabled = bool(form.get("enabled"))
            wanted = _multi(form, "tools")
        finally:
            await form.close()

        current, unreadable = current_config(ctx.layout)
        if current is None:
            return templates.TemplateResponse(
                request=request,
                name="keys.html",
                context=await _keys_context(
                    request,
                    save_result={"kind": "refused", "message": unreadable or ""},
                ),
            )

        listing = await scans.current()
        offered = set(tool_names(listing))
        tools = sorted({name for name in wanted if name in offered})

        settings = dict(current.settings)
        before = settings.get("keyless")
        after = {"enabled": enabled, "allowed_tools": tools}
        if isinstance(before, Mapping) and dict(before) == after:
            return templates.TemplateResponse(
                request=request,
                name="keys.html",
                context=await _keys_context(
                    request, save_result={"kind": "none", "message": KEYLESS_UNCHANGED}
                ),
            )
        settings["keyless"] = after

        try:
            await ctx.save_config(settings, user, action="config.update")
        except HTTPException as exc:
            return templates.TemplateResponse(
                request=request,
                name="keys.html",
                context=await _keys_context(
                    request, save_result={"kind": "refused", "message": _refusal(exc)}
                ),
            )

        await _record_change(
            ctx.audit,
            user,
            action="keyless.enable" if enabled else "keyless.disable",
            outcome=AuditOutcome.SUCCESS,
            detail={"allowed_tools": tools},
        )
        return templates.TemplateResponse(
            request=request,
            name="keys.html",
            context=await _keys_context(
                request,
                save_result={
                    "kind": "ok",
                    "message": _keyless_summary(enabled=enabled, tools=tools),
                },
            ),
        )

    async def _key_list_context(request: Request) -> dict[str, Any]:
        handler = api_handler(request.app, "list_keys")
        if handler is None:
            return {"keys": [], "keys_note": NO_KEY_OPERATIONS}
        try:
            listing = await handler()
        except HTTPException as exc:
            return {"keys": [], "keys_note": _refusal(exc)}
        return {
            "keys": key_rows(listing, persona_names=await _persona_names()),
            "keys_note": None,
        }

    @router.get("/keys", response_class=HTMLResponse, summary="Issued access keys")
    async def keys_page(request: Request) -> HTMLResponse:
        """Spec §5.4's per-client keys, and what each one is allowed to do.

        The list is the reviewable half of "a dumb display widget should not be
        able to unlock doors": every key's policy is rendered as a sentence
        rather than as the object it is, because a nested profile printed into a
        row is not something anybody reviews.
        """
        return templates.TemplateResponse(
            request=request,
            name="keys.html",
            context=await _keys_context(request),
        )

    def _key_list_fragment(request: Request, context: Mapping[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="fragments/key_list.html", context=dict(context)
        )

    async def _new_key_context(
        request: Request,
        *,
        tab: str = "form",
        submitted: Mapping[str, Any] | None = None,
        raw: str = "",
        error: str | None = None,
    ) -> dict[str, Any]:
        listing = await scans.current()
        names = tool_names(listing)
        persona_listing = await _persona_listing(_default_persona())
        return {
            "tab": tab if tab in KEY_ISSUE_TABS else "form",
            "personas": [
                {"slug": summary.name, "name": summary.display_name}
                for summary in persona_listing.personas
            ],
            "tools": names,
            "tools_all": TOOLS_ALL,
            "tools_all_note": TOOLS_ALL_NOTE,
            "risk_choices": RISK_CHOICES,
            "memory_choices": MEMORY_CHOICES,
            "kind_choices": KIND_CHOICES,
            "note_limit": MAX_KEY_NOTE_CHARS,
            "submitted": dict(submitted or {}),
            "raw": raw or _example_profile_json(names),
            "error": error,
        }

    def _example_profile_json(names: Sequence[str]) -> str:
        """What the raw tab starts with — the default profile, spelled out.

        A blank textarea makes an operator guess the field names of a model they
        cannot see; the dumped default is the documentation. Every switch in it
        is off, which is also what ``PolicyProfile`` defaults to, so pasting it
        back unedited issues the least-privileged key there is.
        """
        example = PolicyProfile(
            id=generated_profile_id(),
            display_name="Example",
            kind=ProfileKind.API_KEY,
            enabled=True,
            allowed_tools=list(names[:1]),
        )
        return json.dumps(example.model_dump(mode="json"), indent=2)

    @router.get(
        "/keys/new/fragment",
        response_class=HTMLResponse,
        summary="The issue-a-key form",
    )
    async def key_new_fragment(request: Request, tab: str = "form") -> HTMLResponse:
        """The form, in the modal. **Nothing is issued by opening it.**"""
        return templates.TemplateResponse(
            request=request,
            name="fragments/key_new.html",
            context=await _new_key_context(request, tab=tab),
        )

    async def _refused_dialog(
        request: Request, *, tab: str, form: Mapping[str, Any] | Any, message: str
    ) -> HTMLResponse:
        """The form again, with what was typed still in it and the reason above.

        A refused issue that came back blank would make an operator re-enter
        eleven controls to fix one of them — and the refusal that matters most
        here (the anonymous ceilings) is precisely the one somebody will want to
        adjust and resubmit.
        """
        return templates.TemplateResponse(
            request=request,
            name="fragments/key_new.html",
            context=await _new_key_context(
                request,
                tab=tab,
                submitted=_submitted(form),
                raw=str(form.get("raw") or "") if tab == "raw" else "",
                error=message,
            ),
        )

    def _submitted(form: Mapping[str, Any] | Any) -> dict[str, Any]:
        """The posted form as a plain dict the template can read back.

        ``tools`` keeps its repeats — it is the one multi-valued field — so the
        boxes that were ticked come back ticked.
        """
        values = {
            key: str(value)
            for key, value in (
                form.multi_items() if hasattr(form, "multi_items") else form.items()
            )
            if key != "tools"
        }
        values["tools"] = _multi(form, "tools")
        return values

    @router.post("/keys", response_class=HTMLResponse, summary="Issue one access key")
    async def key_issue(request: Request) -> HTMLResponse:
        """Mint a key through the JSON API's own ``issue_api_key``.

        **The value is in the response this renders and nowhere else, ever.** The
        core keeps a SHA-256; there is no endpoint that can return it again and no
        listing that carries it. So it is rendered into
        ``fragments/key_created.html`` once, with the sentence that says so, and
        the refreshed list swapped in beside it is built from ``ApiKeyView``,
        which has no field for a key value at all.
        """
        form = await request.form()
        tab = str(form.get("tab") or "form")
        tab = tab if tab in KEY_ISSUE_TABS else "form"
        note = str(form.get("note") or "").strip()[:MAX_KEY_NOTE_CHARS]

        handler = api_handler(request.app, "issue_key")
        if handler is None:
            return await _refused_dialog(
                request, tab=tab, form=form, message=NO_KEY_OPERATIONS
            )
        if not note:
            return await _refused_dialog(
                request, tab=tab, form=form, message=KEY_NOTE_REQUIRED
            )

        try:
            if tab == "raw":
                profile = PolicyProfile.model_validate_json(str(form.get("raw") or ""))
            else:
                listing = await scans.current()
                profile = profile_from_form(form, tool_names=tool_names(listing))
        except ValueError as exc:
            # Includes pydantic's ValidationError, which is where the anonymous
            # ceilings (ADR-0003) refuse. Their wording comes from the model.
            return await _refused_dialog(
                request, tab=tab, form=form, message=policy_refusal(exc)
            )

        try:
            issued = await handler(
                body=ApiKeyIssueRequest(profile=profile, note=note), request=request
            )
        except HTTPException as exc:
            return await _refused_dialog(request, tab=tab, form=form, message=_refusal(exc))

        return templates.TemplateResponse(
            request=request,
            name="fragments/key_created.html",
            context={
                "key": {"note": note or issued.key.key_id, "value": issued.api_key_shown_once},
                "warning": issued.warning,
                **await _key_list_context(request),
            },
        )

    def _key_id_or_404(key_id: str) -> str:
        """The JSON API's own door check on a key id, applied here too.

        ``{key_id}`` reaches an audit record and this screen's markup, and the
        JSON route bounds its shape with ``KEY_ID_PATTERN`` before either
        happens. Calling that handler as a function rather than over HTTP skips
        the path validation FastAPI would have run, so the same bound is applied
        here — the same constant, not a second spelling of it.
        """
        if not re.match(KEY_ID_PATTERN, key_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no key with that id.")
        return key_id

    async def _key_or_404(request: Request, key_id: str) -> dict[str, Any]:
        """One key's row, or a 404 — so a confirmation can name what it revokes."""
        _key_id_or_404(key_id)
        context = await _key_list_context(request)
        for row in context["keys"]:
            if row["id"] == key_id:
                return row
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no key with that id.")

    def _key_confirm_context(row: Mapping[str, Any]) -> dict[str, Any]:
        """The two lines both the dialog and the page say — see
        :func:`key_revoke_body`."""
        return {
            "title": KEY_REVOKE_TITLE.format(note=f"“{row['note']}”"),
            "body": key_revoke_body(note=row["note"], summary=row["summary"]),
            "confirm_label": KEY_REVOKE_LABEL,
        }

    @router.get(
        "/keys/{key_id}/revoke/confirm",
        response_class=HTMLResponse,
        summary="Confirm revoking one key (page)",
    )
    async def key_revoke_confirm_page(request: Request, key_id: str) -> HTMLResponse:
        """The no-script fallback (ADR-0020) for :func:`key_revoke_confirm`
        below — the same two facts, as a real page with a real form, reached
        by the same link the dialog's own `hx-get` decorates.
        """
        row = await _key_or_404(request, key_id)
        return templates.TemplateResponse(
            request=request,
            name="confirm_page.html",
            context={
                **await _shell(request, "keys"),
                **_key_confirm_context(row),
                "action": f"/admin/keys/{key_id}/revoke",
                "back_href": "/admin/keys",
                "back_label": "← Access keys",
            },
        )

    @router.get(
        "/keys/{key_id}/revoke/confirm/fragment",
        response_class=HTMLResponse,
        summary="Confirm revoking one key",
    )
    async def key_revoke_confirm(request: Request, key_id: str) -> HTMLResponse:
        """Names the client and what the key is for — see :func:`key_revoke_body`."""
        row = await _key_or_404(request, key_id)
        return templates.TemplateResponse(
            request=request,
            name="fragments/confirm.html",
            context={
                **_key_confirm_context(row),
                "action": f"/admin/keys/{key_id}/revoke",
                "target": "#key-list",
            },
        )

    @router.post(
        "/keys/{key_id}/revoke",
        response_class=HTMLResponse,
        response_model=None,
        summary="Revoke one access key",
    )
    async def key_revoke(request: Request, key_id: str) -> HTMLResponse | RedirectResponse:
        """Revoke through the JSON API's own handler, then re-render the list.

        That handler answers 204 whether or not the key existed, on purpose (a
        404 would be an oracle for "does this id exist?"), so this screen has
        nothing to distinguish either — it shows the list as it now is, which is
        the state ``DELETE`` promised.

        A plain form post — no script, spec §9's click-first — gets a real
        redirect back to the list instead of the bare ``#key-list`` fragment
        the dialog's own swap expects: a browser that submitted a form has
        nowhere to put a fragment. A refusal is shown in place rather than
        redirected to, the same as the dialog path shows it today.
        """
        _key_id_or_404(key_id)
        handler = api_handler(request.app, "revoke_key")
        if handler is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NO_KEY_OPERATIONS)
        hx_request = bool(request.headers.get("HX-Request"))
        try:
            await handler(key_id=key_id, request=request)
        except HTTPException as exc:
            error = f"Not revoked: {_refusal(exc)}"
            if hx_request:
                context = await _key_list_context(request)
                context["revoke_error"] = error
                return _key_list_fragment(request, context)
            page_context = await _keys_context(request)
            page_context["revoke_error"] = error
            return templates.TemplateResponse(
                request=request, name="keys.html", context=page_context
            )
        if hx_request:
            return _key_list_fragment(request, await _key_list_context(request))
        return RedirectResponse("/admin/keys", status_code=status.HTTP_303_SEE_OTHER)
