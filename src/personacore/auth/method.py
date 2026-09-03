"""Which way in is in force — the precedence rule, in one place (PC-294).

PC-294 does not forbid a second way in. It forbids an **unintended** one:

    a trusted-header path accepting an identity header from any address, or two
    authentication methods both accepting a request without either being
    chosen.

The development bypass is the deliberate one and stays: it is the route back
into a container whose admin password is lost, and only somebody who can edit
the Compose file on the host can set it. Enabling the core's own sign-in must
**not** disable it, because it is needed at exactly the moment sign-in is not
working.

So this module answers one question — *which single method authenticates a
request* — and every caller asks it here rather than deciding for itself.

**The rule, in full:**

1. If :data:`~personacore.server.DEV_ADMIN_USER_ENV` is set, the method is
   ``bypass``. It wins over everything, on purpose, and is announced at
   startup, on ``/health`` and on the admin dashboard.
2. Otherwise the method is whatever the operator chose in the ``[auth]``
   section of ``core.toml`` — the **Sign-in** control on the Core settings
   screen: ``proxy`` (the trusted identity header, PC-121 / PC-122) or
   ``builtin`` (the core's own accounts, PC-283).
3. The default is ``builtin`` (:data:`DEFAULT_METHOD`). Removing the bypass
   from a core that was never told about a login proxy has to leave a door
   somebody can actually open; defaulting to ``proxy`` sent that operator to a
   proxy they do not have and told them to go and use it. Anyone who *has* a
   proxy configures it deliberately, which is the right way round — the
   deliberate case is the configured one.
4. An unrecognised value is a startup error, not a fallback. Silently
   defaulting a misspelled ``method = "buildin"`` to the other method is
   precisely "a method nobody chose".

Exactly one of the three is ever in force. There is no state in which the
trusted header and a session cookie are both accepted.

**Why this is a setting and the bypass is not.** ADR-0010 puts runtime
configuration in the admin UI rather than the environment, and choosing between
the two doors is exactly that: a decision an operator makes once, with words in
front of them explaining both. The bypass is the opposite kind of thing — it is
break-glass, it has to keep working when the settings file cannot be read or the
UI cannot be reached, and a setting that could be switched off from inside the
UI would be no way back in at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from personacore.config.settings import ConfigError


class AuthMethod(StrEnum):
    """How a caller proved who they are.

    The first three are **the doors a person comes through, and never two at
    once** — that is PC-294's rule and this module exists to enforce it.
    :data:`API_KEY` is not one of them: it is not choosable, ``resolve_auth``
    never returns it, and nothing configures it. It names the credential a
    *machine* presents to one surface, which is why it can coexist with
    whichever door is open without breaking "never two at once" — the two never
    answer the same request.
    """

    BYPASS = "bypass"
    """``PERSONACORE_ADMIN_DEV_USER`` — the deliberate break-glass. Every
    request is that user, and that user is an admin."""

    PROXY = "proxy"
    """A trusted identity header from an allowlisted address (PC-121, PC-122).
    The header is stripped from every other peer before a route sees it."""

    BUILTIN = "builtin"
    """The core's own accounts and session cookie (PC-283)."""

    API_KEY = "api_key"
    """An access key carrying :data:`~personacore.contracts.policy.ADMIN_API_SCOPE`,
    presented as ``Authorization: Bearer`` to the admin JSON API.

    **Not a door in the sense the other three are**, and the difference is why
    this member exists rather than one of them being reused. It admits nobody to
    the rendered admin UI, it is never what ``[auth] method`` says, and it names
    a key rather than a person — so per-person state keyed on
    :attr:`~personacore.admin.models.AdminUser.door` cannot confuse a key called
    ``alice`` with the household member of that name, which is the entire reason
    that field is required.
    """


CHOOSABLE = (AuthMethod.BUILTIN, AuthMethod.PROXY)
"""What an operator may put in ``[auth] method``. ``bypass`` is not choosable
here — it is turned on by setting the bypass user, which is one decision
expressed once rather than two that can disagree.

Ordered with the default first, because the admin UI renders the choices in
this order and the first of them is what an unconfigured core is already doing.
"""

DEFAULT_METHOD = AuthMethod.BUILTIN
"""What an unconfigured core uses. See rule 3 in the module docstring."""

METHOD_SETTING = "[auth] method"
"""Where the choice lives, named the same way everywhere it is mentioned."""

WHERE_TO_SET_IT = (
    "Choose it under Sign-in on the Core settings screen, or set "
    f"{METHOD_SETTING} in core.toml"
)
"""The one sentence saying where the setting is. Used by the startup refusal
and by the admin API's refusal alike, so the two cannot drift.

No full stop, following the convention every other validator message here
keeps: the admin API renders a problem as ``'<key>': <message>.`` and adds one,
so a message that carried its own would print two."""

_DESCRIPTIONS = {
    AuthMethod.BUILTIN: "'builtin' uses this core's own accounts and sign-in page",
    AuthMethod.PROXY: "'proxy' believes an identity header set by a login proxy in front",
    AuthMethod.BYPASS: (
        "'bypass' treats every request as one user and is set in the environment, never here"
    ),
}


_LABELS = {
    AuthMethod.BUILTIN: "This core's own accounts",
    AuthMethod.PROXY: "A login proxy in front",
    AuthMethod.BYPASS: "The development bypass",
}
"""What each door is called on screen — and in every sentence that tells
somebody to go and change it.

One dictionary rather than a label in the template and a quoted name in
``personacore.admin.authn``: a message that says "change Sign-in to X" is wrong
the moment X is not what the control actually says, and that is the message a
locked-out operator is reading.
"""

_HELP = {
    AuthMethod.BUILTIN: (
        "Names and passwords kept by this core, with a sign-in page of its own. "
        "With no account yet, the interface opens on a setup page that creates "
        "the first one."
    ),
    AuthMethod.PROXY: (
        "A login proxy in front — Authelia, Authentik and the like — signs the "
        "person in and tells this core who they are in a header. Only choose "
        "this if such a proxy is actually there: without one, nobody can get in."
    ),
}
"""The words an operator chooses between, on the Core settings screen."""


def describe(method: AuthMethod) -> str:
    """One clause per door, for a message that has to offer a choice."""
    return _DESCRIPTIONS[method]


def label(method: AuthMethod) -> str:
    """What this door is called wherever a person reads about it."""
    return _LABELS[method]


def help_text(method: AuthMethod) -> str:
    """The sentence under the choice on the Core settings screen."""
    return _HELP[method]


CHANGE_IT_HERE = (
    f"change Sign-in on the Core settings screen to '{_LABELS[AuthMethod.BUILTIN]}' "
    "and restart the core"
)
"""How every refusal tells somebody to switch to the built-in sign-in."""

BREAK_GLASS_HINT = (
    "If you cannot reach that screen — getting in is what is failing — set "
    "PERSONACORE_ADMIN_DEV_USER to a name of your choosing in the Compose file "
    "and restart. That bypass always lets you in, and you can change the "
    "setting from there."
)
"""The other half of every one of those refusals.

Without it they are a circle: the way to fix being locked out is a screen you
have to be signed in to reach. The bypass is the way back in, and a message
that names the fix without naming how to reach it is a wall.
"""


def coerce_method(value: str | None) -> AuthMethod:
    """A configured value as one of :data:`CHOOSABLE`, or a plain-English refusal.

    The **only** place a method name is turned into a method, so the settings
    model and :func:`resolve_auth` refuse the same typo with the same sentence.

    Args:
        value: What ``[auth] method`` says. ``None`` or empty means
            :data:`DEFAULT_METHOD`.

    Raises:
        ValueError: The value names a method that does not exist. A
            ``ValueError`` rather than a :class:`ConfigError` so a pydantic
            field validator can carry it; each caller turns it into whichever
            refusal its surface needs.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return DEFAULT_METHOD
    try:
        method: AuthMethod | None = AuthMethod(raw)
    except ValueError:
        method = None
    if method not in CHOOSABLE:
        names = ", ".join(item.value for item in CHOOSABLE)
        clauses = "; ".join(describe(item) for item in CHOOSABLE)
        raise ValueError(
            f"'{value}' is not a way of signing in that the core knows about. "
            f"Use one of: {names}. {clauses}. {WHERE_TO_SET_IT}"
        )
    return method


@dataclass(frozen=True)
class AuthDecision:
    """The single answer to "who is allowed to assert an identity, and how".

    Built once at startup and passed to everything that needs it. Nothing reads
    the settings again afterwards, so what ``/health`` reports is what the
    running process is actually doing — and a saved change to
    :data:`METHOD_SETTING` is visibly pending a restart rather than half in
    force.
    """

    chosen: AuthMethod
    """What the operator configured, ignoring the bypass. Reported beside
    ``method`` so an operator can see what they will get back when they remove
    the bypass again."""

    method: AuthMethod
    """What is actually in force for the next request."""

    bypass_user: str | None
    """The user every request is treated as, when the bypass is on."""

    @property
    def bypassed(self) -> bool:
        return self.method is AuthMethod.BYPASS

    @property
    def uses_trusted_header(self) -> bool:
        """Whether a request's identity comes from the header.

        True for the bypass too: the bypass is implemented by the identity
        guard *setting* that header, so there is one credential-reading path
        rather than two (see ``personacore.server._install_identity_guard``).
        """
        return self.method in (AuthMethod.BYPASS, AuthMethod.PROXY)

    @property
    def uses_builtin(self) -> bool:
        """Whether a request's identity comes from a session cookie."""
        return self.method is AuthMethod.BUILTIN

    def as_health(self, *, trusted_header: str, trusted_proxies: list[str]) -> dict[str, Any]:
        """What ``/health`` says about the door — PC-294's "state plainly".

        ``/health`` is unauthenticated, so this carries no account names and no
        count of them. It carries the bypass user because that is already
        reported there and because a bypass nobody notices is the whole risk:
        naming it is what makes the warning legible.
        """
        return {
            "method": self.method.value,
            "chosen": self.chosen.value,
            "bypass": self.bypassed,
            "bypass_user": self.bypass_user,
            "trusted_header": trusted_header if self.uses_trusted_header else None,
            "trusted_proxies": trusted_proxies if self.uses_trusted_header else [],
            "warning": BYPASS_WARNING if self.bypassed else None,
        }


BYPASS_WARNING = (
    "The development authentication bypass is on: every request to the admin "
    "interface is treated as one user, and anyone who can reach this port is an "
    "admin. It is the deliberate way back in when an admin password is lost. "
    "Unset PERSONACORE_ADMIN_DEV_USER and restart once you are back in."
)
"""Said the same way in the startup log, on ``/health`` and on the dashboard,
so the three cannot drift into describing different postures."""


def resolve_auth(method_setting: str | None, dev_user: str | None) -> AuthDecision:
    """Apply the rule above. The only place it is applied.

    Args:
        method_setting: ``[auth] method`` from ``core.toml``; ``None`` or empty
            means :data:`DEFAULT_METHOD`.
        dev_user: Raw ``PERSONACORE_ADMIN_DEV_USER``; ``None`` or empty means
            the bypass is off. Read from the environment by the caller and
            deliberately not from the settings: it has to work at the moment
            the settings cannot be read.

    Raises:
        ConfigError: The setting names a method that does not exist. Refused at
            startup rather than defaulted, per rule 4 above.
    """
    try:
        chosen = coerce_method(method_setting)
    except ValueError as exc:
        # The full stop is added here rather than carried in the message: this
        # is a whole sentence on its own, while the admin API renders the same
        # text as a clause inside one it punctuates itself.
        raise ConfigError(f"{exc}.") from exc
    bypass = (dev_user or "").strip() or None
    method = AuthMethod.BYPASS if bypass else chosen
    return AuthDecision(chosen=chosen, method=method, bypass_user=bypass)


__all__ = [
    "BREAK_GLASS_HINT",
    "BYPASS_WARNING",
    "CHANGE_IT_HERE",
    "CHOOSABLE",
    "DEFAULT_METHOD",
    "METHOD_SETTING",
    "WHERE_TO_SET_IT",
    "AuthDecision",
    "AuthMethod",
    "coerce_method",
    "describe",
    "help_text",
    "label",
    "resolve_auth",
]
