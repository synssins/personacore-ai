"""Turning the Core settings form back into a settings document.

Separate from the screen because it is the part with no HTTP in it: what a
posted form means, which fields it is allowed to touch, and what happens to the
broker password when the box is left alone. Everything here is a pure function
of the form and the stored document, which is what makes it testable without a
request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from personacore.admin.models import (
    ConfigResponse,
)
from personacore.config.settings import REDACTED_VALUE
from personacore.web.shared import (
    RETENTION_DEFAULT_KEY,
    _section,
)


def split_listen(listen: str, existing: Any) -> dict[str, Any] | None:
    """A typed ``host:port`` back into ``[server]``, or ``None`` for "default".

    The port is passed through as **text**, unparsed: the settings model bounds
    it at 1–65535 and says so in plain English, and a second opinion here would
    be a second place for that message to come from (and to drift).
    """
    text = listen.strip()
    if not text:
        return None
    section: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    host, separator, port = text.rpartition(":")
    if not separator:
        section["host"] = text
        return section
    section["host"] = host
    section["port"] = port
    return section


PLAYBACK_AUTOPLAY_FIELD = "playback_autoplay"
"""The household playback rule — ``[playback] autoplay`` (ADR-0030).

A select with three values, not a checkbox, and handled like
:data:`AUTH_METHOD_FIELD` rather than like the text boxes: an absent control has
said nothing, and must leave the stored rule alone rather than resetting the
household to "everyone chooses".
"""


AUTH_METHOD_FIELD = "auth_method"
"""The Sign-in control's form field — ``[auth] method`` (ADR-0023).

Not in :data:`CORE_FORM_FIELDS`: those are text boxes whose empty value means
"unset", and an absent Sign-in field must mean "leave it alone" rather than
"put it back to the default". A form posted without it — an older client, a
scripted post, a fragment that never rendered the control — must not silently
change which door is open.
"""


DICTATION_BROWSER_FIELD = "dictation_browser"
"""The browser-dictation switch's own field — ``[dictation] browser`` (PLAN.md,
"Next — Speech to text, in the container").

Not handled like :data:`AUTH_METHOD_FIELD` or :data:`PLAYBACK_AUTOPLAY_FIELD`:
a radio group and a select always submit *something* once they are on the
page, so the field's own presence is the signal that somebody chose. A
checkbox does not — an unticked one submits nothing at all, which native HTML
makes indistinguishable from "this control was never on the form". So this
field is read only alongside :data:`DICTATION_BROWSER_PRESENT_FIELD`, the same
device :mod:`personacore.web.screens.voice` uses for its own switches.
"""

DICTATION_BROWSER_PRESENT_FIELD = "present.dictation_browser"
"""Says the switch above was actually on the form.

Without this, a cleared checkbox and a page that never rendered the control
post the same thing — no ``dictation_browser`` key either way — and the safe
reading of an ambiguous submission is to leave the stored value alone rather
than silently resetting a household's ``true`` back to ``false``.
"""


WYOMING_PRESENT_FIELD = "present.wyoming_enabled"
"""Says the Wyoming block was actually drawn on this form.

One marker for all three of its fields, not one each, because they are one
control between them: the switch has the checkbox hazard
:data:`DICTATION_BROWSER_PRESENT_FIELD` describes, and ``host`` and ``port``
have the *same* hazard wearing text-box clothes — an empty box on this screen
means "put it back to the default", so a form that never rendered the block
would post two empty strings and quietly move a widened ``host`` back to
loopback. With the marker absent, ``[wyoming]`` is left exactly as it was
found.
"""

WYOMING_ENABLED_FIELD = "wyoming_enabled"
"""``[wyoming] enabled`` — whether Home Assistant may use this core for speech."""

WYOMING_HOST_FIELD = "wyoming_host"
"""``[wyoming] host``. Empty means the default, which is loopback."""

WYOMING_PORT_FIELD = "wyoming_port"
"""``[wyoming] port``, passed through as **text** for the same reason the
retention windows are: :class:`~personacore.config.wyoming.WyomingSettings`
bounds it at 1–65535 and says so in plain English, and a second opinion here
would be a second place for that sentence to come from."""


CORE_FORM_FIELDS = (
    "listen",
    "bus_host",
    "bus_port",
    "bus_client_id",
    "bus_username",
)
"""The core form's flat text fields. ``retention_*`` is deliberately not here —
it is matched by prefix so that a name nobody recognises still reaches the
validator. Neither is ``bus_password``: it is write-only and is not trimmed,
so it is read on its own terms below.
"""

BUS_PASSWORD_FIELD = "bus_password"  # noqa: S105 - a form field name, not a credential
"""The broker password box. Write-only: it posts up and never renders back."""

BUS_PASSWORD_CLEAR_FIELD = "bus_password_clear"  # noqa: S105 - a form field name
"""The explicit "remove the stored password" control. Only a control that says
what it does may clear a credential — an empty box never means "delete this"."""

RETENTION_FIELD_PREFIX = "retention_"


def bus_password(stored_bus: Mapping[str, Any], form: Mapping[str, Any] | Any) -> str | None:
    """What ``[bus].password`` should be after this save, or ``None`` for unset.

    Three states, and the whole point is that only one of them can destroy a
    working credential:

    * The **clear control** was pressed — ``None``, the password goes.
    * **Something was typed** — that, exactly as typed. Not stripped: a leading
      or trailing space is a legitimate part of a password, and quietly editing
      one is a login failure nobody can see the cause of.
    * **The box is empty** — the stored value stands. What is carried forward is
      the redaction marker the config API handed this screen, which
      ``config_io.restore_write_only_values`` resolves back against the file. A
      blank box must never clear a password: the operator saving a retention
      window has said nothing whatever about their broker credentials.
    """
    if str(form.get(BUS_PASSWORD_CLEAR_FIELD) or ""):
        return None
    typed = str(form.get(BUS_PASSWORD_FIELD) or "")
    if typed:
        return typed
    return REDACTED_VALUE if stored_bus.get("password") else None


def core_payload(
    current: ConfigResponse, form: Mapping[str, Any] | Any
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """The submitted form as a whole settings document, plus what was typed.

    Whole, not partial: :func:`~personacore.admin.config_io.validate_settings`
    validates the entire document rather than merging over disk, so the parts
    this screen has no field for are read back from the stored document and
    passed through untouched.

    **Numbers are passed through as text.** A window of ``ninety`` reaches the
    settings model and comes back as "must be a whole number, written without
    quotes" — the message spec §9 asked for, written once, in the one place
    that validates. Parsing here would mean writing it a second time.
    """
    settings = current.settings
    payload = dict(settings)
    fields = {name: str(form.get(name) or "").strip() for name in CORE_FORM_FIELDS}

    server = split_listen(fields["listen"], settings.get("server"))
    if server is None:
        payload.pop("server", None)
    else:
        payload["server"] = server

    stored_bus = settings.get("bus")
    stored_bus = stored_bus if isinstance(stored_bus, dict) else {}

    bus: dict[str, Any] = {}
    for key, field in (
        ("host", "bus_host"),
        ("port", "bus_port"),
        ("client_id", "bus_client_id"),
        ("username", "bus_username"),
    ):
        # An empty box means "unset", never an empty value: TOML has no null,
        # and ``bus.username = ""`` would be a real, wrong user name.
        if fields[field]:
            bus[key] = fields[field]

    # ``password_secret`` has no box on this screen any more — the password is
    # typed directly. It stays supported for anyone already using it, so it is
    # carried through rather than dropped: this form must not silently delete a
    # setting it stopped showing.
    if stored_bus.get("password_secret"):
        bus["password_secret"] = stored_bus["password_secret"]

    password = bus_password(stored_bus, form)
    if password is not None:
        bus["password"] = password

    if bus:
        payload["bus"] = bus
    else:
        payload.pop("bus", None)

    # Which way in is open (ADR-0023). Passed through as **text**, unvalidated,
    # for the same reason the retention windows are: `AuthSettings` already
    # refuses a method the core does not have, and says which ones it does, so
    # a second opinion here would be a second place for that sentence to come
    # from. A form that did not carry the field leaves the stored value alone —
    # `payload` already holds it — because an absent control has said nothing
    # about which door should be open.
    chosen_method = str(form.get(AUTH_METHOD_FIELD) or "").strip()
    if chosen_method:
        fields[AUTH_METHOD_FIELD] = chosen_method
        payload["auth"] = {**_section(settings, "auth"), "method": chosen_method}

    chosen_autoplay = str(form.get(PLAYBACK_AUTOPLAY_FIELD) or "").strip()
    if chosen_autoplay:
        fields[PLAYBACK_AUTOPLAY_FIELD] = chosen_autoplay
        payload["playback"] = {
            **_section(settings, "playback"),
            "autoplay": chosen_autoplay,
        }

    # Browser dictation (PLAN.md). The marker, not the checkbox, decides
    # whether this save has an opinion at all — see
    # :data:`DICTATION_BROWSER_PRESENT_FIELD`. With it present, the checkbox's
    # own presence or absence is the answer: ticked posts a value, unticked
    # posts nothing, and both are meaningful once the marker says the control
    # was genuinely on the page.
    if str(form.get(DICTATION_BROWSER_PRESENT_FIELD) or ""):
        dictation_browser = bool(str(form.get(DICTATION_BROWSER_FIELD) or ""))
        fields[DICTATION_BROWSER_FIELD] = "on" if dictation_browser else ""
        payload["dictation"] = {
            **_section(settings, "dictation"),
            "browser": dictation_browser,
        }

    # The Wyoming server (`[wyoming]`). Gated whole on its marker — see
    # :data:`WYOMING_PRESENT_FIELD` — so a save from a page that never drew the
    # block cannot switch it off or narrow its address back to loopback.
    if str(form.get(WYOMING_PRESENT_FIELD) or ""):
        stored_wyoming = _section(settings, "wyoming")
        wyoming: dict[str, Any] = dict(stored_wyoming)
        enabled = bool(str(form.get(WYOMING_ENABLED_FIELD) or ""))
        fields[WYOMING_ENABLED_FIELD] = "on" if enabled else ""
        wyoming["enabled"] = enabled
        for key, field in (("host", WYOMING_HOST_FIELD), ("port", WYOMING_PORT_FIELD)):
            typed_value = str(form.get(field) or "").strip()
            fields[field] = typed_value
            # An empty box means "the default", not an empty value: TOML has no
            # null, and `wyoming.host = ""` would be a real, wrong address. The
            # key is removed so the settings model supplies its own default,
            # which is the loopback the module docstring argues for.
            if typed_value:
                wyoming[key] = typed_value
            else:
                wyoming.pop(key, None)
        payload["wyoming"] = wyoming

    retention: dict[str, Any] = {}
    per_surface: dict[str, Any] = {}
    typed: dict[str, str] = {}
    items = form.multi_items() if hasattr(form, "multi_items") else form.items()
    for name, value in items:
        if not str(name).startswith(RETENTION_FIELD_PREFIX):
            continue
        key = str(name)[len(RETENTION_FIELD_PREFIX) :]
        raw = str(value or "").strip()
        typed.setdefault(key, raw)
        if not raw:
            continue
        if key == RETENTION_DEFAULT_KEY:
            retention["default_days"] = raw
        else:
            per_surface[key] = raw
    if per_surface:
        retention["per_surface_days"] = per_surface
    if retention:
        payload["retention"] = retention
    else:
        payload.pop("retention", None)

    return payload, fields, typed
