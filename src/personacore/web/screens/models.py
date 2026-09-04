"""The model-connections screen: one row per role (spec section 4).

Every save goes through the JSON API's own persist-and-apply helper, so a
connection changed here and one changed with ``PUT /admin/api/config`` are the
same write, applied in the same order, audited the same way. The listing fetch
this screen offers lives in ``model_listing``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from personacore.admin.models import (
    ConfigResponse,
)
from personacore.config.appdata import AppdataLayout
from personacore.config.image import ImageSettings
from personacore.config.secrets import SecretError, SecretStore
from personacore.config.settings import LLMRole, LLMRoles, llm_role
from personacore.llm.client import LLMClient, LLMClientConfig
from personacore.llm.errors import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMResponseError,
    LLMTimeoutError,
)
from personacore.web.screens.connection_key import (
    MAX_KEY_CHARS,
    forget_key,
    key_status,
    read_key_form,
    role_key_name,
    store_key,
)
from personacore.web.screens.model_listing import (
    MODEL_FETCH_EMPTY,
    MODEL_FETCH_HOST_ERROR,
    MODEL_FETCH_KEY_UNREADABLE,
    MODEL_FETCH_NEEDS_KEY,
    MODEL_FETCH_NO_ADDRESS,
    MODEL_FETCH_NO_LISTING,
    MODEL_FETCH_NOT_A_LISTING,
    MODEL_FETCH_UNAVAILABLE,
    MODEL_FETCH_UNREACHABLE,
    _role_health_source,
    model_choices,
    model_fetch_summary,
)
from personacore.web.shared import (
    UIContext,
    _readable,
    current_config,
    settings_problems,
)

# ---------------------------------------------------------------------------
# Model connections — ADR-0011's five roles
# ---------------------------------------------------------------------------
#
# One panel per role, ``interactive`` required and the other four optional. The
# screen writes through the **same** persist-and-apply path ``PUT
# /admin/api/config`` uses (``save_config`` below), so a connection saved here
# is validated, written atomically, applied to the live roster and audited
# exactly as one saved through the JSON API — ADR-0010's condition for accepting
# UI-only configuration at all.
#
# **Nothing here trims a pasted URL.** People paste the whole endpoint out of
# another client's config, and ``LLMSettings._normalise_base_url`` already
# strips ``/chat/completions``, ``/completions`` and ``/models`` on the way into
# the settings model. This screen re-reads the saved document after a write and
# renders that, so the box ends up showing the value the core is actually using
# rather than the one that was typed. A second trimmer here would be a second
# opinion about what an endpoint is.

LLM_ROLE_LABELS: dict[LLMRole, str] = {
    LLMRole.INTERACTIVE: "Conversation",
    LLMRole.AUTONOMY: "Background work",
    LLMRole.TRIAGE: "Triage",
    LLMRole.VISION: "Vision",
    LLMRole.COMMANDS: "Commands",
}
"""ADR-0011's roles in the design's words — the sidebar of a settings screen is
not the place to make an operator learn an enum."""

MODEL_ADDRESS_REQUIRED = "Give the conversation model both an address and a model name."

MODEL_ADDRESS_REQUIRED_OPTIONAL = (
    "Give this role both an address and a model name, or clear it and let it "
    "fall back to the conversation model."
)

INTERACTIVE_NOT_CLEARABLE = (
    "The conversation model cannot be cleared — every other role falls back to it."
)


def model_roles(llm_section: Any, layout: AppdataLayout) -> list[dict[str, Any]]:
    """One panel's worth of context per role, from the settings document.

    Read from the document rather than from :class:`CoreSettings` so an empty
    panel and an absent one stay distinguishable: ``configured`` is "this role
    has a section of its own", which is what decides whether the screen says
    "falls back to the conversation model" or shows a live address.

    ``layout`` is here for one thing: asking the secret store whether the key a
    role names is actually present. **Only that** — the row carries whether a
    key is set and never the key, so nothing this function returns can reach
    the page as a credential.
    """
    section = llm_section if isinstance(llm_section, dict) else {}
    rows: list[dict[str, Any]] = []
    for role in LLMRole:
        endpoint = section.get(role.value)
        configured = isinstance(endpoint, dict)
        values: Mapping[str, Any] = endpoint if configured else {}
        model = str(values.get("model", ""))
        rows.append(
            {
                "id": role.value,
                "label": LLM_ROLE_LABELS[role],
                "required": role is LLMRole.INTERACTIVE,
                "configured": configured,
                "addr": str(values.get("base_url", "")),
                "model": model,
                "saved_model": model,
                "key": {
                    **key_status(layout, str(values.get("api_key_secret") or "")),
                    "id": role.value,
                    "maxlen": MAX_KEY_CHARS,
                },
                "test_result": None,
                "save_result": None,
                # The model box is a plain text box until somebody presses
                # Fetch models. Nothing on this screen asks an LLM host
                # anything on page load — see `model_choices` below.
                "choices": None,
                "fetch_note": None,
            }
        )
    return rows


def connection_differs(
    current: ConfigResponse, role: LLMRole, address: str, model: str
) -> bool:
    """Whether the boxes hold something other than what is stored for a role.

    Used only to caveat a connection test: the probe goes through the live
    client, so an unsaved edit in the boxes was not what answered, and the
    answer has to say so.
    """
    section = current.settings.get("llm")
    section = section if isinstance(section, dict) else {}
    endpoint = section.get(role.value)
    if not isinstance(endpoint, dict):
        return bool(address or model)
    return address != str(endpoint.get("base_url", "")) or model != str(
        endpoint.get("model", "")
    )


# ---------------------------------------------------------------------------
# The image generator — a service, not a role
# ---------------------------------------------------------------------------
#
# docs/contracts/image-conversations.md's responder for an image conversation
# is reached over HTTP the same way an `[llm.*]` role is, but it is not one:
# no chat completions, no context window, no persona. It gets its own section
# on this screen rather than a row in `model_roles` above, so nothing here
# invites somebody to pick it as a conversation model.
#
# `[image] base_url` was a `core.toml`-only setting in v0.14.0; the owner
# rejected that, since it should be a UI config item. Spec §9 already says
# so — this screen is click-first, and hand-editing TOML to turn a feature on
# is exactly what that rule exists to prevent.

IMAGE_OFF_NOTE = (
    "No address means image generation is off — an image conversation says so "
    "in the thread instead of trying to answer. Set an address to turn it on."
)
"""Spec §9's plain English, said once, without apologising for the off state:
the build brief is explicit that unconfigured is a normal state here, not an
error, and most cores will simply never set this."""

IMAGE_RAW_EDITOR_NOTE = (
    "The connect timeout is not on this screen — it is rarely touched, and "
    "Core settings' raw JSON tab reaches it."
)
"""**Decided here, not settled by the brief**: the screen edits `base_url`,
`model` and `total_timeout_seconds` — the address turns the feature on at all,
the model matters the moment a server hosts more than one, and the total
timeout is the ceiling an operator on CPU-only hardware genuinely has reason
to raise (`ImageSettings.total_timeout_seconds`'s own docstring: a large
picture with no GPU takes minutes). `connect_timeout_seconds` stays off this
screen — it is reachable from the raw JSON editor Core settings already has,
the same fallback every setting with no field of its own uses.
`read_timeout_seconds` moved onto this screen (prompt-prefix contract §5): a
generator's read timeout is the wait an operator actually has reason to
tune, unlike the connect timeout."""

IMAGE_TIMEOUTS_HELP = (
    "Wait for the picture is how long to wait for one render: nothing "
    "arrives until it is done. Ceiling is the limit on the whole request "
    "and the clock that does not reset; it catches a generator that has "
    "stopped, which the wait cannot."
)

MAX_PREFIX_CHARS = 4_000
"""Ceiling on the image prompt-prefix box (prompt-prefix contract §3). A
prefix is a short lead-in, not a prompt — the persona prompt's own cap is
sixteen times this — and it is still a bound on something arriving from
outside (spec §7)."""

IMAGE_PREFIX_TOO_LONG = (
    "That prompt prefix is longer than {limit} characters, which is far more "
    "than a lead-in should ever be. Nothing was written."
)


def _format_seconds(value: Any) -> str:
    """A stored timeout as the box shows it.

    ``ImageSettings.total_timeout_seconds`` is a float and its default is
    ``900.0``; showing that verbatim in a text box reads as though a decimal
    matters here when it never does. A whole number renders without one, and
    anything else — an operator's own fractional value, or something that
    simply is not a number — is shown exactly as it was so a refused save
    still has something to correct rather than a value quietly replaced.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    return str(int(number)) if number == int(number) else str(number)


def image_context(
    section: Any,
    *,
    address: str | None = None,
    model: str | None = None,
    total_timeout: str | None = None,
    read_timeout: str | None = None,
    prompt_prefix: str | None = None,
    save_result: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The image generator's own panel context.

    Not built by :func:`model_roles`: this is not one of ADR-0011's roles and
    has no ``LLMRole`` to key it by. Read from the settings document exactly as
    a role's row is — an absent ``[image]`` table and an empty one are the same
    "unconfigured" state to an operator — with :class:`ImageSettings`'s own
    defaults standing in for ``total_timeout_seconds`` and
    ``read_timeout_seconds`` so the boxes show the values the core would
    actually use rather than a blank.

    The operator's typed input outranks what is stored, the same rule
    :func:`~personacore.web.screens.core.retention_rows` follows: a
    refused save must re-render what was submitted, not silently revert to the
    file while printing an error underneath it.
    """
    stored = section if isinstance(section, dict) else {}
    defaults = ImageSettings()
    stored_addr = str(stored.get("base_url") or "")
    stored_model = str(stored.get("model") or "")
    stored_timeout = _format_seconds(
        stored.get("total_timeout_seconds", defaults.total_timeout_seconds)
    )
    stored_read_timeout = _format_seconds(
        stored.get("read_timeout_seconds", defaults.read_timeout_seconds)
    )
    stored_prefix = str(stored.get("prompt_prefix") or "")
    shown_addr = stored_addr if address is None else address
    return {
        "addr": shown_addr,
        "model": stored_model if model is None else model,
        "total_timeout": stored_timeout if total_timeout is None else total_timeout,
        "read_timeout": stored_read_timeout if read_timeout is None else read_timeout,
        "prompt_prefix": stored_prefix if prompt_prefix is None else prompt_prefix,
        "configured": bool(shown_addr),
        "off_note": IMAGE_OFF_NOTE,
        "raw_editor_note": IMAGE_RAW_EDITOR_NOTE,
        "timeouts_help": IMAGE_TIMEOUTS_HELP,
        "max_prefix_chars": MAX_PREFIX_CHARS,
        "save_result": save_result,
    }


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the models page, its save and its two fragments."""
    templates = ctx.templates
    layout = ctx.layout
    llm = ctx.llm
    require_user = ctx.require_user
    save_config = ctx.save_config
    _shell = ctx.shell
    _current_config = partial(current_config, ctx.layout)


    def _role_or_404(role_id: str) -> LLMRole:
        """A role name out of the URL, checked against ADR-0011's closed set."""
        try:
            return llm_role(role_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    def _role_row(
        role: LLMRole,
        llm_section: Any,
        *,
        address: str | None = None,
        model: str | None = None,
        save_result: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One role's panel context, with the operator's input kept if given."""
        row = next(
            item for item in model_roles(llm_section, layout) if item["id"] == role.value
        )
        if address is not None:
            row["addr"] = address
        if model is not None:
            row["model"] = model
        row["save_result"] = save_result
        return row

    def _model_fragment(request: Request, row: dict[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="fragments/model_form.html", context={"role": row}
        )

    def _model_field_fragment(request: Request, row: dict[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="fragments/model_field.html", context={"role": row}
        )

    def _image_fragment(request: Request, image: dict[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="fragments/image_form.html", context={"image": image}
        )

    def _role_endpoint(role: LLMRole) -> Any:
        """The settings a role's calls actually use, fallback included.

        Resolved through :class:`LLMRoles` rather than by reading the document
        directly, because ADR-0011 says the "or interactive" fallback has
        exactly one implementation and this is not it. A discovery call is a
        call to the same host as everything else, so it goes out with the same
        API key and the same timeouts a real turn would use.

        Returns ``None`` when the document cannot be read as roles at all — the
        caller then has nothing to build a client from and says so.
        """
        current, _ = _current_config()
        if current is None:
            return None
        section = current.settings.get("llm")
        try:
            return LLMRoles.model_validate(section if isinstance(section, dict) else {}).resolve(
                role
            )
        except ValueError:
            return None

    async def _ask_host_for_models(role: LLMRole, address: str) -> tuple[list[str] | None, str]:
        """Ask the host at ``address`` what it serves. Never raises.

        Returns the model names and an empty note, or ``None`` and the reason —
        a different reason for each thing that can go wrong, because "could not
        fetch" tells an operator nothing about what to do next.

        The endpoint is built by :class:`LLMClientConfig`/:class:`LLMClient`,
        the same pair every other outbound call uses. Nothing here turns a base
        URL into a path: a second way of doing that is how ".../v1/models/models"
        gets shipped.
        """
        endpoint = _role_endpoint(role)
        if endpoint is None:
            return None, MODEL_FETCH_UNAVAILABLE

        api_key = None
        if endpoint.api_key_secret:
            try:
                # `core_secrets()`, not the store itself: the LLM key belongs
                # to the core's own namespace (ADR-0025 section 1), and the
                # plain `get` now needs an owner. Asking through the core view
                # is how this screen says which namespace it means without
                # being able to name a plugin's.
                api_key = SecretStore(layout).core_secrets().get(endpoint.api_key_secret)
            except SecretError:
                return None, MODEL_FETCH_KEY_UNREADABLE

        client = LLMClient(
            LLMClientConfig(
                base_url=address,
                model=endpoint.model,
                api_key=api_key,
                connect_timeout=endpoint.connect_timeout_seconds,
                read_timeout=endpoint.read_timeout_seconds,
            )
        )
        try:
            listing = await client.list_models()
        except (LLMConnectionError, LLMTimeoutError):
            return None, MODEL_FETCH_UNREACHABLE
        except LLMAuthenticationError:
            return None, MODEL_FETCH_NEEDS_KEY
        except LLMResponseError as exc:
            if exc.status_code in (404, 405):
                return None, MODEL_FETCH_NO_LISTING
            if exc.status_code is None:
                # A 2xx whose body could not be read as a listing at all.
                return None, MODEL_FETCH_NOT_A_LISTING
            return None, MODEL_FETCH_HOST_ERROR
        except Exception:  # noqa: BLE001 - a field renders a sentence, never a traceback
            return None, MODEL_FETCH_UNAVAILABLE
        finally:
            await client.aclose()

        if "data" not in listing.model_fields_set:
            # Parsed, but only because every field of a listing is optional.
            # A body with no `data` at all is some other API's answer.
            return None, MODEL_FETCH_NOT_A_LISTING
        names = [str(item.id).strip() for item in listing.data if str(item.id).strip()]
        if not names:
            return None, MODEL_FETCH_EMPTY
        return names, ""

    @router.get("/models", response_class=HTMLResponse, summary="LLM connections per role")
    async def models_page(request: Request) -> HTMLResponse:
        """ADR-0011's five roles: one required, four falling back to it.

        The optional four are the design's disclosure rows rather than four
        more forms, because four empty required-looking boxes is exactly the
        impression ADR-0011's fallback exists to avoid.
        """
        current, config_error = _current_config()
        return templates.TemplateResponse(
            request=request,
            name="models.html",
            context={
                **await _shell(request, "models"),
                "roles": (
                    []
                    if current is None
                    else model_roles(current.settings.get("llm"), layout)
                ),
                "image": (
                    None
                    if current is None
                    else image_context(current.settings.get("image"))
                ),
                "config_error": config_error,
            },
        )

    @router.post(
        "/models/{role_id}", response_class=HTMLResponse, summary="Save or clear one role"
    )
    async def models_save(role_id: str, request: Request) -> HTMLResponse:
        """Write one role's connection, or give an optional role back to its
        fallback — through the JSON API's own save path.

        Only the named role is touched: the rest of the document is read back
        from disk and passed through, so saving one connection can never drop
        another role or a setting this screen has no field for.
        """
        user = require_user(request)
        role = _role_or_404(role_id)
        form = await request.form()
        address = str(form.get("address") or "").strip()
        model = str(form.get("model") or "").strip()
        # Both boxes emptied and Save pressed means the same thing the clear
        # button means: this role falls back to the conversation model. The
        # owner emptied the boxes, pressed Save and was refused; a refusal that
        # tells somebody to do what they just did is the wrong answer.
        clearing = str(form.get("action") or "save") == "clear" or (not address and not model)
        key = read_key_form(form)

        current, config_error = _current_config()
        if current is None:
            return _model_fragment(
                request,
                _role_row(
                    role,
                    None,
                    address=address,
                    model=model,
                    save_result={"kind": "error", "message": str(config_error)},
                ),
            )

        section = current.settings.get("llm")
        section = section if isinstance(section, dict) else {}
        before = section.get(role.value)
        before = dict(before) if isinstance(before, dict) else {}
        label = LLM_ROLE_LABELS[role]

        # The key box is judged before anything is written, so a refusal about
        # the box leaves the secret store and the settings file exactly as they
        # were.
        if key.refusal is not None:
            return _model_fragment(
                request,
                _role_row(
                    role,
                    section,
                    address=address,
                    model=model,
                    save_result={"kind": "error", "message": key.refusal},
                ),
            )

        stored_key_name = str(before.get("api_key_secret") or "")
        forget_after: str | None = None

        payload = dict(current.settings)
        roles = dict(section)
        if clearing:
            if role is LLMRole.INTERACTIVE:
                return _model_fragment(
                    request,
                    _role_row(
                        role,
                        section,
                        address=address,
                        model=model,
                        save_result={"kind": "error", "message": INTERACTIVE_NOT_CLEARABLE},
                    ),
                )
            if not before:
                return _model_fragment(
                    request,
                    _role_row(
                        role,
                        section,
                        save_result={
                            "kind": "nothing",
                            "message": (
                                f"Nothing changed — {label.lower()} was already falling back "
                                "to the conversation model."
                            ),
                        },
                    ),
                )
            roles.pop(role.value, None)
            # The section goes and takes its `api_key_secret` with it, because
            # a key belongs to the connection that uses it. The stored value is
            # left on the volume unless the remove control was ticked: falling
            # back to the conversation model says nothing about a credential,
            # and only a control that says "remove the key" may destroy one.
            if key.clearing:
                forget_after = stored_key_name
        else:
            if not address or not model:
                return _model_fragment(
                    request,
                    _role_row(
                        role,
                        section,
                        address=address,
                        model=model,
                        save_result={
                            "kind": "error",
                            "message": (
                                MODEL_ADDRESS_REQUIRED
                                if role is LLMRole.INTERACTIVE
                                else MODEL_ADDRESS_REQUIRED_OPTIONAL
                            ),
                        },
                    ),
                )
            endpoint = {**before, "base_url": address, "model": model}
            # A typed key is written into the secret store FIRST, under this
            # role's own name, and the settings file goes on naming it — no
            # value reaches `core.toml` (see `connection_key`, which explains
            # why the order is this way round).
            #
            # A typed key wins over a ticked remove. The two together are
            # contradictory, and the reading that keeps a working credential is
            # the safe one when either outcome is plainly on the page after the
            # save.
            if key.typed:
                name = stored_key_name or role_key_name(role.value)
                refusal = store_key(layout, name, key.value)
                if refusal is not None:
                    return _model_fragment(
                        request,
                        _role_row(
                            role,
                            section,
                            address=address,
                            model=model,
                            save_result={"kind": "error", "message": refusal},
                        ),
                    )
                endpoint["api_key_secret"] = name
            elif key.clearing:
                endpoint.pop("api_key_secret", None)
                forget_after = stored_key_name
            # An empty box has said nothing about the key, so `before` carries
            # the existing name straight through. That is the whole point of
            # the field: an operator changing a model name and pressing Save
            # must not silently unset the credential and get a connection that
            # stops working with nothing on screen to say why.
            roles[role.value] = endpoint
        payload["llm"] = roles

        try:
            saved = await save_config(payload, user, action="config.update")
        except HTTPException as exc:
            _, message = settings_problems(exc)
            return _model_fragment(
                request,
                _role_row(
                    role,
                    section,
                    address=address,
                    model=model,
                    save_result={"kind": "error", "message": message},
                ),
            )

        # After the write and never before: the other order would leave
        # `core.toml` naming a file that is no longer there for as long as the
        # save takes to fail.
        forget_key(layout, forget_after)

        # Rendered from what was *written*, never from what was typed: the
        # settings model trims a pasted endpoint URL down to its root, and the
        # box has to end up showing the address the core is really using.
        after_section = saved.settings.get("llm")
        after_section = after_section if isinstance(after_section, dict) else {}
        after = after_section.get(role.value)
        after = dict(after) if isinstance(after, dict) else {}
        if clearing:
            result = {
                "kind": "saved",
                "message": (
                    f"{label} now falls back to the conversation model and shares its "
                    f"connection. In use now — no restart is needed. Written to {saved.source}."
                ),
            }
        elif after == before and not key.typed:
            # `key.typed` is part of this test because replacing a key under a
            # name the connection already used changes nothing in the document
            # — the credential moved and the settings did not, so comparing
            # only the settings would report "nothing changed" over a key that
            # was genuinely replaced.
            result = {
                "kind": "nothing",
                "message": "Nothing changed — the connection is as it was.",
            }
        elif after == before:
            # A key replaced under a name the connection already used, and
            # nothing else. `LLMRoster._resolve` reuses a client whose resolved
            # settings are unchanged — which is what keeps an untouched role's
            # pool and its breaker alive across every save — so this core goes
            # on using the key it read when that client was built. Saying "in
            # use now" here would be the one message on this screen that is not
            # true.
            result = {
                "kind": "saved",
                "message": (
                    f"Replaced the API key for {label.lower()}. Restart the core to use "
                    "it — the connection itself did not change, so nothing was rebuilt."
                ),
            }
        else:
            result = {
                "kind": "saved",
                "message": (
                    f"Wrote the {label.lower()} connection to {saved.source}. In use now — "
                    "no restart is needed."
                ),
            }
        return _model_fragment(request, _role_row(role, after_section, save_result=result))

    @router.post(
        "/models/image/save",
        response_class=HTMLResponse,
        summary="Save or clear the image generator's connection",
    )
    async def models_image_save(request: Request) -> HTMLResponse:
        """The image generator's own connection, saved through the same
        persist-and-apply path every role's save uses.

        A second write path is exactly what the build brief forbids — "do not
        write a second persistence path" — and there is no need for one:
        ``[image]`` is a top-level :class:`~personacore.config.settings.
        CoreSettings` field like ``[llm]``, so the same ``save_config`` helper
        validates, writes, applies and audits it.

        An empty address is not refused. It is the feature's own off switch
        (:meth:`ImageSettings.is_configured`) — the build brief is explicit
        that unconfigured is a normal state, not an error — so this handler
        never asks for something to put in the box.

        Named ``/models/image/save`` rather than ``/models/image`` so it
        cannot be captured by ``POST /models/{role_id}`` above: a bare
        ``/models/image`` is one path segment, exactly the shape a role's own
        save route matches, and registration order is not something a reader
        of this file should have to reason about to know which handler a
        request reaches.
        """
        user = require_user(request)
        form = await request.form()
        address = str(form.get("address") or "").strip()
        model = str(form.get("model") or "").strip()
        total_timeout = str(form.get("total_timeout_seconds") or "").strip()
        read_timeout = str(form.get("read_timeout_seconds") or "").strip()
        prompt_prefix = str(form.get("prompt_prefix") or "").strip()

        if len(prompt_prefix) > MAX_PREFIX_CHARS:
            current, config_error = _current_config()
            before_section = current.settings.get("image") if current is not None else None
            before = dict(before_section) if isinstance(before_section, dict) else {}
            return _image_fragment(
                request,
                image_context(
                    before,
                    address=address,
                    model=model,
                    total_timeout=total_timeout,
                    read_timeout=read_timeout,
                    prompt_prefix=prompt_prefix,
                    save_result={
                        "kind": "error",
                        "message": IMAGE_PREFIX_TOO_LONG.format(limit=MAX_PREFIX_CHARS),
                    },
                ),
            )

        current, config_error = _current_config()
        if current is None:
            return _image_fragment(
                request,
                image_context(
                    None,
                    address=address,
                    model=model,
                    total_timeout=total_timeout,
                    read_timeout=read_timeout,
                    prompt_prefix=prompt_prefix,
                    save_result={"kind": "error", "message": str(config_error)},
                ),
            )

        section = current.settings.get("image")
        before = dict(section) if isinstance(section, dict) else {}

        # Only the fields this screen owns are touched; anything else already
        # in the section — a connect timeout set from the raw editor — is
        # carried straight through, the same rule `models_save` follows for a
        # role's untouched fields.
        updated = dict(before)
        if address:
            updated["base_url"] = address
        else:
            updated.pop("base_url", None)
        if model:
            updated["model"] = model
        else:
            updated.pop("model", None)
        if total_timeout:
            updated["total_timeout_seconds"] = total_timeout
        else:
            updated.pop("total_timeout_seconds", None)
        if read_timeout:
            updated["read_timeout_seconds"] = read_timeout
        else:
            updated.pop("read_timeout_seconds", None)
        if prompt_prefix:
            updated["prompt_prefix"] = prompt_prefix
        else:
            updated.pop("prompt_prefix", None)

        payload = dict(current.settings)
        payload["image"] = updated

        try:
            saved = await save_config(payload, user, action="config.update")
        except HTTPException as exc:
            _, message = settings_problems(exc)
            return _image_fragment(
                request,
                image_context(
                    before,
                    address=address,
                    model=model,
                    total_timeout=total_timeout,
                    read_timeout=read_timeout,
                    prompt_prefix=prompt_prefix,
                    save_result={"kind": "error", "message": message},
                ),
            )

        after_section = saved.settings.get("image")
        after = dict(after_section) if isinstance(after_section, dict) else {}
        if after == before:
            result = {
                "kind": "nothing",
                "message": "Nothing changed — the image generator is as it was.",
            }
        elif not after.get("base_url"):
            result = {
                "kind": "saved",
                "message": f"Cleared. {IMAGE_OFF_NOTE} Written to {saved.source}.",
            }
        else:
            result = {
                "kind": "saved",
                "message": (
                    f"Wrote the image generator's connection to {saved.source}. In use "
                    "now — no restart is needed."
                ),
            }
        return _image_fragment(request, image_context(after, save_result=result))

    @router.post(
        "/models/{role_id}/test/fragment",
        response_class=HTMLResponse,
        summary="Ask one role's LLM host whether it answers",
    )
    async def models_test(role_id: str, request: Request) -> HTMLResponse:
        """Probe the live client for this role and say so in words.

        **It tests the saved connection**, because the saved connection is the
        only one this core has a client for — building a throwaway client for
        an address that has not been saved would be a second way to reach an
        LLM host, with its own timeouts and its own idea of what a secret
        reference means. When the boxes differ from what is stored, the answer
        says so rather than letting it look like the typed address was tried.

        Nothing is audited: spec §7's log covers admin *changes*, and filling
        the trace view with probes would bury the records that matter.
        """
        require_user(request)
        role = _role_or_404(role_id)
        form = await request.form()
        address = str(form.get("address") or "").strip()
        model = str(form.get("model") or "").strip()

        source = _role_health_source(llm, role)
        began = time.monotonic()
        try:
            result = await source.health_check()
        except Exception as exc:  # noqa: BLE001 - a page renders a sentence, never a traceback
            outcome = {"ok": False, "detail": f"The check itself failed: {_readable(exc)}"}
        else:
            elapsed = round((time.monotonic() - began) * 1000)
            facts = getattr(source, "facts", None)
            serving = str(facts.get("model") or "") if isinstance(facts, dict) else ""
            if result.healthy:
                detail = f"answered in {elapsed} ms"
                detail += f", serving “{serving}”." if serving else "."
            else:
                detail = result.detail or "the host did not answer, and gave no reason."
            outcome = {"ok": bool(result.healthy), "detail": detail}

        current, _ = _current_config()
        if current is not None and connection_differs(current, role, address, model):
            outcome["detail"] = (
                f"{outcome['detail']} This tested the saved connection — save first to "
                "test what is in the boxes."
            )
        return templates.TemplateResponse(
            request=request,
            name="fragments/model_test.html",
            context={"role": {"id": role.value, "test_result": outcome}},
        )

    @router.post(
        "/models/{role_id}/choices/fragment",
        response_class=HTMLResponse,
        summary="List the models the LLM host in the box says it serves",
    )
    async def models_choices(role_id: str, request: Request) -> HTMLResponse:
        """Turn the model box into a dropdown of what that host reports.

        **Explicit, never automatic.** This runs when somebody presses Fetch
        models, not when the page opens: a settings screen that fires a request
        at five LLM hosts every time it is looked at is a screen nobody can
        leave open.

        **The address in the box is the one asked**, so an operator pointing the
        role somewhere new sees the new host's models rather than the old host's
        — which is also why this cannot go through the live client the way Test
        connection does; that client is bound to the saved address.

        Nothing is written and nothing is audited: this reads a list.
        """
        require_user(request)
        role = _role_or_404(role_id)
        form = await request.form()
        address = str(form.get("address") or "").strip()
        model = str(form.get("model") or "").strip()

        row: dict[str, Any] = {"id": role.value, "model": model, "choices": None}
        if not address:
            row["fetch_note"] = {"kind": "warn", "message": MODEL_FETCH_NO_ADDRESS}
            return _model_field_fragment(request, row)

        names, why_not = await _ask_host_for_models(role, address)
        if names is None:
            row["fetch_note"] = {"kind": "warn", "message": why_not}
            return _model_field_fragment(request, row)

        choices = model_choices(names, model)
        row["choices"] = choices
        # Selecting an option the operator never chose would be a change made
        # by a button labelled "fetch", so a role with no model yet gets an
        # empty first option instead (see the template).
        row["fetch_note"] = {"kind": "ok", "message": model_fetch_summary(choices)}
        return _model_field_fragment(request, row)
