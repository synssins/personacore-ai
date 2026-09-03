"""What an access key is allowed to do, and how a form says it.

Split out of the Keys screen because none of it touches HTTP: reading a policy
profile out of a submitted form, summarising one for a row, and the closed sets
of choices the form offers. A profile that cannot be built is a sentence, never
an exception the screen dies on.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any

from pydantic import ValidationError

from personacore.admin.models import (
    ApiKeyListing,
    PluginListing,
)
from personacore.contracts.manifest import RiskLevel
from personacore.contracts.policy import (
    ADMIN_API_SCOPE,
    MemoryScope,
    PolicyProfile,
    ProfileKind,
    RateLimit,
)
from personacore.plugins.host import TOOL_SEPARATOR

PROFILE_ID_PREFIX = "api-key-"

PROFILE_ID_ENTROPY_BYTES = 4
"""How much randomness goes into a generated profile id.

The id is a label in audit records, not a credential — the credential is minted
by :class:`~personacore.api.keys.ApiKeyStore` with 256 bits of its own. Eight hex
characters is enough that two keys issued a second apart do not collide, and
short enough to read back off a log line."""

TOOLS_ALL = "all"
"""The design's "every tool" checkbox.

``PolicyProfile.allowed_tools`` is an exact-name allowlist with no wildcard, and
that is deliberate (ADR-0018: installing a plugin must never widen what an
existing caller can do). So this expands to the tools that exist **at the moment
the key is issued**, and the form says so — a wildcard stored as a wildcard would
quietly grant tomorrow's plugins as well."""

_RISK_WORDS: dict[RiskLevel, str] = {
    RiskLevel.SAFE: "safe tools only",
    RiskLevel.CONFIRM: "up to tools that ask before acting",
    RiskLevel.RESTRICTED: "up to restricted tools",
}

RISK_CHOICES: tuple[tuple[str, str], ...] = (
    (RiskLevel.SAFE.value, "safe — runs silently, read-only"),
    (RiskLevel.CONFIRM.value, "confirm — may act, asks first"),
    (RiskLevel.RESTRICTED.value, "restricted — needs permission, then asks"),
)
"""``RiskLevel`` in the design's words. Every member, in order, so a value the
enum grows is a visible omission here rather than a silently missing option."""

_MEMORY_WORDS: dict[MemoryScope, str] = {
    MemoryScope.NONE: "remembers nothing",
    MemoryScope.ANONYMOUS: "sees the shared anonymous scratchpad",
    MemoryScope.USER: "sees its own memory and the household's",
    MemoryScope.HOUSEHOLD: "sees the household's memory",
}

MEMORY_CHOICES: tuple[tuple[str, str], ...] = (
    (MemoryScope.NONE.value, "none — remembers nothing between turns"),
    (MemoryScope.USER.value, "its own, plus the household's"),
    (MemoryScope.HOUSEHOLD.value, "the household's only"),
    (MemoryScope.ANONYMOUS.value, "the shared anonymous scratchpad"),
)

KIND_CHOICES: tuple[tuple[str, str], ...] = (
    (ProfileKind.API_KEY.value, "a machine client — a display, a script, an app"),
    (ProfileKind.USER.value, "a person in the household"),
    (ProfileKind.ANONYMOUS.value, "anonymous — heavily capped, and refused on a key"),
)
"""Every ``ProfileKind``, anonymous included.

Offering a choice the core will refuse looks like a mistake and is not one: the
refusal is the point. ``PolicyProfile`` caps the anonymous tier at construction
and ``ApiKeyRecord`` refuses to carry one at all, both with sentences written for
an operator — and an option that is absent from the form can never produce
either. Leaving it here means the rule is stated once, by the code that enforces
it, at the moment somebody tries."""

MAX_TOOLS_SUMMARISED = 3
"""How many tool names a one-line summary spells out before counting the rest."""


def profile_summary(profile: PolicyProfile, persona_names: Mapping[str, str]) -> str:
    """One line describing what a key may do — the design's readable summary.

    Never raw JSON: spec §5.4's per-client policy is only reviewable if an
    operator can read it at a glance, and a nested object printed into a list row
    is not reading, it is decoding.

    Raw passthrough short-circuits everything else because it *is* everything
    else: no persona, no tools, no memory (spec §5.4). Listing a persona beside
    it would describe a character that never speaks.
    """
    parts: list[str] = []
    if profile.raw_passthrough:
        parts.append("raw passthrough — straight to the model, no persona, no tools, no memory")
    else:
        if profile.persona:
            display = persona_names.get(profile.persona, profile.persona)
            parts.append(f"answers as {display}")
        else:
            parts.append("answers as the default persona")
        tools = list(profile.allowed_tools)
        if not tools:
            parts.append("no tools — conversation only")
        elif len(tools) <= MAX_TOOLS_SUMMARISED:
            parts.append(f"{'only ' if len(tools) == 1 else ''}{', '.join(tools)}")
        else:
            shown = ", ".join(tools[:MAX_TOOLS_SUMMARISED])
            parts.append(f"{shown} and {len(tools) - MAX_TOOLS_SUMMARISED} more")
        parts.append(_RISK_WORDS[profile.max_tool_risk])
        scope = _MEMORY_WORDS[profile.memory_scope]
        parts.append(f"{scope}{', and may add to it' if profile.memory_write else ''}")
    parts.append(
        "may approve confirmations"
        if profile.may_approve_confirm
        else "cannot approve confirmations"
    )
    # Named in the row rather than only on the form. It is the widest thing a
    # key can carry — the trace is everybody's conversations and the admin API
    # issues more keys — so somebody reading the list has to be able to see
    # which key holds it without opening anything.
    if ADMIN_API_SCOPE in profile.scopes:
        parts.append("opens the admin API — reads the trace, issues keys")
    if profile.safe_mode:
        parts.append("safe mode on")
    if not profile.enabled:
        parts.append("switched off — this key does not open the door")
    return " · ".join(parts)


def key_rows(
    listing: ApiKeyListing, *, persona_names: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The issued-key list, in the shape ``fragments/key_list.html`` renders.

    **There is no field here for a key value or a key hash, and there must never
    be one.** The rows are built from :class:`~personacore.admin.models.ApiKeyView`,
    which has neither and forbids extras, so the only way one could reach this
    screen is somebody adding it here on purpose.
    """
    return [
        {
            "id": view.key_id,
            "note": view.note.strip() or "Unnamed key",
            "meta": (
                f"issued {view.created_at.astimezone(UTC):%d %b %Y} · id {view.key_id}"
            ),
            "enabled": view.enabled,
            "summary": profile_summary(view.profile, persona_names),
        }
        for view in listing.keys
    ]


def generated_profile_id() -> str:
    """A profile id for a newly issued key.

    A label, not a credential — it goes into audit records so a later reader can
    tell one key's traffic from another's. Random rather than derived from the
    note, because two "Kitchen display" keys issued a year apart must not share
    an identity in the audit log.
    """
    return f"{PROFILE_ID_PREFIX}{secrets.token_hex(PROFILE_ID_ENTROPY_BYTES)}"


def _checked(form: Mapping[str, Any] | Any, name: str) -> bool:
    """An HTML checkbox as a bool. Absent means unchecked — that is how forms
    work, and it is why every dangerous switch on this screen defaults to off."""
    return bool(str(form.get(name) or ""))


def profile_from_form(
    form: Mapping[str, Any] | Any,
    *,
    tool_names: Sequence[str],
) -> PolicyProfile:
    """The issue-a-key form as a :class:`PolicyProfile`.

    Every field the model holds has a control, so nothing a key can do is
    reachable only through the raw tab. Two of them do not map straight across:

    * **Tools.** ``allowed_tools`` is an exact-name allowlist with no wildcard, so
      the design's "every tool" box is expanded here against the tools that exist
      right now (:data:`TOOLS_ALL_NOTE`). Storing a wildcard would grant every
      plugin installed *after* the key was issued, which ADR-0018 is explicit
      must not happen.
    * **The anonymous ceilings are not checked here.** ``PolicyProfile`` refuses
      an over-privileged anonymous profile at construction with a sentence
      written for an operator (ADR-0003), so this function builds what was asked
      for and lets the model say no. A second copy of those rules in a web form
      is a second copy to drift.

    Raises:
        ValueError: including :class:`pydantic.ValidationError`, which is one —
            rendered by :func:`policy_refusal`.
    """
    selected = _multi(form, "tools")
    if TOOLS_ALL in selected:
        allowed = list(tool_names)
    else:
        allowed = [name for name in selected if name != TOOLS_ALL]

    note = str(form.get("note") or "").strip()
    return PolicyProfile(
        id=generated_profile_id(),
        display_name=note or "Unnamed key",
        kind=ProfileKind(str(form.get("kind") or ProfileKind.API_KEY.value)),
        # A key issued from this screen is meant to work. Every *permission* on
        # this form defaults to off; the on/off switch for the credential itself
        # is the one thing that would make issuing it pointless.
        enabled=not _checked(form, "suspended"),
        persona=str(form.get("persona") or "").strip() or None,
        allowed_tools=allowed,
        max_tool_risk=RiskLevel(str(form.get("risk") or RiskLevel.SAFE.value)),
        may_approve_confirm=_checked(form, "approve"),
        memory_scope=MemoryScope(str(form.get("memory") or MemoryScope.NONE.value)),
        memory_write=_checked(form, "memory_write"),
        may_enumerate_plugins=_checked(form, "enumerate"),
        safe_mode=_checked(form, "safe_mode"),
        raw_passthrough=_checked(form, "raw_passthrough"),
        # The admin JSON API takes a key and only a key, and only one carrying
        # this scope — being signed in used to be enough, which is how a
        # household member could mint themselves a key to `/v1` and read
        # everybody's conversations. A list rather than a switch because a
        # second scope is another entry, whereas a boolean would have been a
        # migration. Unticked is the default, so every key already issued and
        # every key issued absent-mindedly stays a `/v1` key and nothing more.
        scopes=[ADMIN_API_SCOPE] if _checked(form, "admin_api") else [],
        rate_limit=RateLimit(
            requests_per_minute=_positive(form.get("requests_per_minute"), 60),
            max_concurrent=_positive(form.get("max_concurrent"), 4),
        ),
    )


def _multi(form: Mapping[str, Any] | Any, name: str) -> list[str]:
    """Every value posted under one field name.

    Starlette's form object keeps repeats; a plain mapping does not. Both are
    accepted so this function is testable without a request.
    """
    if hasattr(form, "getlist"):
        return [str(value) for value in form.getlist(name)]
    value = form.get(name)
    if value is None:
        return []
    if isinstance(value, str | bytes):
        return [str(value)]
    return [str(item) for item in value]


def _positive(value: Any, fallback: int) -> int:
    """A rate-limit box as a whole number, falling back when it is not one.

    The model bounds these at ``ge=1`` and would refuse a zero with its own
    message; the fallback is for an *empty* box, which means "leave it at the
    default" rather than "set it to nothing".
    """
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        return int(text)
    except ValueError:
        # Not a number at all. Handing the model something out of range gets its
        # sentence rather than this function inventing one.
        return -1


def policy_refusal(exc: Exception) -> str:
    """Why a profile was refused, in the words the model used.

    ``PolicyProfile``'s anonymous ceilings (ADR-0003) are already written for a
    person — "the anonymous profile may only reach safe tools — 'confirm' is not
    allowed" — so this unwraps pydantic's envelope and prints what is inside
    rather than restating the rule. Restating it would put the ceilings in two
    places, and the copy on the screen is the one that is not enforced.
    """
    if not isinstance(exc, ValidationError):
        return f"That key was not issued: {exc}"
    reasons: list[str] = []
    for error in exc.errors():
        message = str(error.get("msg", "")).strip()
        message = message.removeprefix("Value error, ").removeprefix("Input should be ")
        location = ".".join(
            str(part) for part in error.get("loc", ()) if isinstance(part, str)
        )
        if location and error.get("type") != "value_error":
            message = f"{location} — {message}"
        if message and message not in reasons:
            reasons.append(message)
    joined = " ".join(f"{reason.rstrip('.')}." for reason in reasons)
    return f"That key was not issued: {joined}" if joined else "That key was not issued."


def tool_names(listing: PluginListing) -> list[str]:
    """Every callable tool, spelled the way ``allowed_tools`` spells it.

    ``<plugin>.<tool>``, from the manifests the scan read — the same string the
    agent loop compares against (``plugins.host.TOOL_SEPARATOR``), so a name
    ticked on this form is a name the loop will match. Disabled plugins are
    included: switching one back on must not silently need every key re-issuing.
    """
    names: list[str] = []
    for plugin in listing.plugins:
        names += [f"{plugin.name}{TOOL_SEPARATOR}{tool}" for tool in plugin.tools]
    return sorted(names)
