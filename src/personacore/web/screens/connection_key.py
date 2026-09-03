"""The API key on a model connection — one field, two screens.

The Models screen and the persona form both point at an OpenAI-compatible
address, and either of them may be pointed at something that wants a bearer
token. This module is the one place that knows how a typed key becomes a stored
credential, so the two screens cannot drift into two conventions for the same
thing.

**The key is still stored by name.** ``LLMSettings.api_key_secret`` holds the
NAME of a secret under ``appdata/secrets/core/`` and the value lives in the
store, exactly as it did before there was a box (ADR-0025 §1). All this module
adds is a way to *put* the value there without a shell: the form writes it
through :class:`~personacore.config.secrets.SecretStore` and the config file
keeps naming it. ``core.toml`` and ``persona.toml`` get read, copied into
backups and pasted into support threads, so a credential in either is a
credential leaked.

**A stored key is never rendered back to the page.** Not in a value attribute,
not in a placeholder, not in a data attribute, not in an error message and not
in a log line. What the screen shows is *whether* a key is set. A key echoed
into markup is readable over a shoulder, saved by a password manager, cached by
the browser and pasted into a screenshot by whoever is debugging.

**An empty box means "leave it as it is".** That is the trap this module exists
to close: an operator changing a model name and pressing Save would otherwise
unset the credential and get a connection that stops working with nothing on
screen to say why. Only the remove control removes a key, because only a
control that says what it does may destroy one — the same rule
:func:`~personacore.web.screens.core_form.bus_password` follows for the
broker password.

The typed value is carried as a :class:`~pydantic.SecretStr` from the moment it
is read. Not decoration: a plain ``str`` in a tuple or a model is one
``repr()`` away from a traceback, a log line or a validation error carrying the
credential with it, which has already happened once in this codebase.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from pydantic import SecretStr

from personacore.config.appdata import AppdataLayout
from personacore.config.secrets import SecretError, SecretStore

KEY_FIELD = "api_key"  # noqa: S105 - a form field name, not a credential
"""The box itself. Write-only: it posts a key up and never renders one back."""

KEY_CLEAR_FIELD = "api_key_clear"  # noqa: S105 - a form field name
"""The explicit "remove the stored key" control.

A checkbox rather than a second submit button, for two reasons that both come
down to the form still working with no scripting. A submit button is the
*default* button of whichever form it appears in first, so pressing Enter in
the name box of the persona form would have removed a credential instead of
saving. And a checkbox applied on Save is the same gesture on both screens,
where one form is swapped by HTMX and the other is an ordinary post.
"""

MAX_KEY_CHARS = 4_000
"""Ceiling on the box, because it arrives from outside (spec §7).

Generous by an order of magnitude against any real bearer token. The value is
**refused** past this rather than truncated: every other bounded box on these
screens trims, but a trimmed credential is a credential that authenticates
against nothing, and a 401 is a miserable thing to debug back to a form field.
"""

KEY_TOO_LONG = (
    "That key is longer than {limit} characters, which is longer than any API "
    "key. Nothing was written."
)

KEY_UNWRITABLE = "The key could not be stored — {why}. Nothing was written."
"""Said when the secret store refuses the write.

``why`` is :class:`~personacore.config.secrets.SecretError`'s own sentence,
which names the secret and its owner and never its value (ADR-0025 §5).
"""


class KeySubmission(NamedTuple):
    """The key half of a submitted form.

    ``value`` is a :class:`~pydantic.SecretStr`, so the tuple's own ``repr`` —
    and therefore any traceback, log line or exception that happens to carry
    it — shows asterisks rather than a credential.
    """

    value: SecretStr
    clearing: bool
    refusal: str | None

    @property
    def typed(self) -> bool:
        """Whether the operator actually put something in the box."""
        return bool(self.value.get_secret_value())


def read_key_form(form: Mapping[str, Any] | Any) -> KeySubmission:
    """The key box and its remove control, as one submission.

    Surrounding whitespace goes, which is the one edit made to a typed key and
    is made here so that a box holding only spaces reads as empty. The secret
    store strips on the way in anyway — a trailing newline is what every paste
    adds and is almost never part of the value — so this is the same rule, not
    a second one.
    """
    typed = str(form.get(KEY_FIELD) or "").strip()
    clearing = bool(str(form.get(KEY_CLEAR_FIELD) or ""))
    if len(typed) > MAX_KEY_CHARS:
        # Refused whole. Nothing keeps the over-long value, so nothing can
        # render it back or write half of it.
        return KeySubmission(
            SecretStr(""), clearing, KEY_TOO_LONG.format(limit=MAX_KEY_CHARS)
        )
    return KeySubmission(SecretStr(typed), clearing, None)


def role_key_name(role_id: str) -> str:
    """The secret a role's key is stored under when the form invents the name.

    One per role, so pointing Triage at a different provider does not overwrite
    the conversation model's key — the owner asked for a key that is a unique
    value per connection, and sharing one name is how two
    connections quietly become one credential.

    A connection that already names a secret keeps that name: an operator who
    hand-wrote ``api_key_secret`` chose it, and a form that renamed their
    secret underneath them would leave the old file behind holding a live key.
    """
    return f"llm_{role_id}_key"


def persona_key_name(slug: str) -> str:
    """The secret one persona's key is stored under. See :func:`role_key_name`.

    The slug is letters, digits, dots, dashes and underscores, and the prefix
    guarantees the leading letter the store's name rule asks for. It is checked
    by :func:`~personacore.config.secrets.check_secret_name` before it is used
    as a filename either way — nothing here is the safety, it just makes a
    likely name.
    """
    return f"persona_{slug}_key"


def key_status(layout: AppdataLayout, secret_name: str | None) -> dict[str, Any]:
    """What the screen shows about a key: whether there is one, never what it is.

    Three states, because two would hide the one worth acting on:

    * **nothing named** — no key, which is the ordinary state of a local model.
    * **named and present** — a key is set.
    * **named and absent** — the connection names a secret that is not in the
      store. That is the state a hand-written ``api_key_secret`` leaves behind
      when the file was never created, and the connection fails on it with a
      401 that says nothing. The screen names it and offers the box.
    """
    name = (secret_name or "").strip()
    if not name:
        return {"named": "", "set": False, "missing": False}
    try:
        present = SecretStore(layout).has(name)
    except SecretError:
        # An unusable name cannot be a file, so nothing answers to it. Saying
        # "not in the store" is true, and it is the thing to do about it.
        present = False
    return {"named": name, "set": present, "missing": not present}


def store_key(layout: AppdataLayout, name: str, value: SecretStr) -> str | None:
    """Write one key into the core's namespace. Returns a refusal, or ``None``.

    Called **before** the settings write it belongs to, never after. A save that
    named a secret the store had not been given is refused outright by
    ``check_secret_references``, and even where it is not, config naming a
    missing file is a connection that fails at the next request. Writing the
    value first means the worst a failed save can leave behind is a secret
    nothing references yet, which costs nothing and is picked up by the retry.
    """
    try:
        SecretStore(layout).set(name, value)
    except SecretError as exc:
        return KEY_UNWRITABLE.format(why=exc)
    return None


def forget_key(layout: AppdataLayout, name: str | None) -> None:
    """Remove one stored key, after the write that stopped naming it.

    The other way round would leave config naming a file that is no longer
    there for as long as the save takes to fail. Never raises: the reference is
    already gone by the time this runs, and a leftover file that could not be
    deleted must not turn a completed save into an error message.
    """
    if not name:
        return
    try:
        SecretStore(layout).delete(name)
    except SecretError:
        pass
