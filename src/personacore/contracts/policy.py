"""Access policy — who may do what.

Spec sections 5.4 (per-client policy), 7 (action policy), 8 (multi-user), plus
ADR-0003 (anonymous tier) and ADR-0005 (child safety).

Spec section 13.3 is why this lands in P0 rather than later: permissions and
memory ownership are miserable to retrofit, so the schema exists before the
features that surface it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from personacore.contracts.manifest import RiskLevel


class ProfileKind(StrEnum):
    USER = "user"
    """A household member, identified by login or speaker ID."""

    API_KEY = "api_key"
    """A machine client of the exposed OpenAI-compatible API."""

    ANONYMOUS = "anonymous"
    """Unauthenticated access. ADR-0003. Off unless explicitly enabled."""


class MemoryScope(StrEnum):
    NONE = "none"

    ANONYMOUS = "anonymous"
    """The shared unauthenticated scope. Isolated, never promoted to L1."""

    USER = "user"
    """This profile's own memories, plus household."""

    HOUSEHOLD = "household"
    """Shared household memory only."""


class RateLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(default=60, ge=1)
    max_concurrent: int = Field(default=4, ge=1)


ADMIN_API_SCOPE = "write:admin"
"""The scope that opens the admin JSON API under ``/admin/api``.

**Shaped ``<verb>:<resource>``, the way Gitea spells its token scopes**, so the
second scope this product needs does not look bolted on beside the first: a
later ``read:trace`` or ``write:keys`` is another string in the same list, read
by the same check. ``write`` implies read, as it does there — there is no
``read:admin``, because this surface issues keys and rewrites settings and
nobody has asked for a listen-only variant of it yet.

``admin`` is the resource because the grant is the whole of that surface: a key
carrying it reaches every route on ``/admin/api``, at administrator level. That
is deliberate rather than lax — the surface's routes are not separable into
harmless and dangerous halves (reading the trace is reading everybody's
conversations), and splitting it would be inventing scopes nobody asked for.
"""

KNOWN_SCOPES = frozenset({ADMIN_API_SCOPE})
"""Every scope the core understands.

Checked rather than ignored: an unknown scope grants nothing, so a mistyped
``write:admins`` would produce a key that silently does less than the person
issuing it believed. That is the failure worth catching at the moment somebody
types it, which is why :class:`PolicyProfile` refuses one instead of storing it.
"""


class PolicyProfile(BaseModel):
    """What one caller is allowed to do.

    Anonymous access is not separate machinery — it is this model with the
    switches down. That was the point of ADR-0003: one policy engine, not two.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    kind: ProfileKind
    enabled: bool = False

    persona: str | None = None
    """Which persona answers. None means the system default."""

    allowed_tools: list[str] = Field(default_factory=list)
    """Explicit allowlist of "<plugin>.<tool>" entries. Empty means no tools at
    all. An allowlist rather than a blocklist, so a newly installed plugin is
    unreachable by everyone until someone decides otherwise."""

    max_tool_risk: RiskLevel = RiskLevel.SAFE
    """Ceiling on tool risk, applied on top of the allowlist."""

    may_approve_confirm: bool = False
    """Whether this profile can satisfy a confirm prompt, per spec section 8."""

    memory_scope: MemoryScope = MemoryScope.NONE
    memory_write: bool = False

    may_enumerate_plugins: bool = False
    """Whether the caller can discover what is installed."""

    safe_mode: bool = False
    """Child-safety controls, ADR-0005. Best-effort by nature: it reduces
    incidents, it does not guarantee anything. The transcript log is the control
    that actually works."""

    raw_passthrough: bool = False
    """Optional per-key raw LLM proxy, spec section 5.4 — no persona, no tools,
    no memory."""

    scopes: list[str] = Field(default_factory=list)
    """Named grants on surfaces that are not ``/v1`` — today, exactly
    :data:`ADMIN_API_SCOPE`.

    **Empty by default, and that default is the security property.** The admin
    JSON API used to be reachable by anyone signed in, so a household member
    with an account could issue themselves a key to ``/v1`` and read every other
    person's conversations through ``GET /admin/api/trace``. That surface now
    takes a key and only a key, and only one carrying the scope — so a key
    issued so a display can hold a conversation cannot also mint keys and read
    the trace. Every key already in the world has no scopes, gains none from
    this field appearing, and is therefore unchanged.

    A list rather than a boolean, before there is a second scope to hold:
    growing a list is additive, and turning a boolean into a list later would be
    a migration of the key table, of every issued key and of the screen they are
    issued from.

    Only names in :data:`KNOWN_SCOPES` are accepted — see there for why a typo
    is refused rather than kept.
    """

    rate_limit: RateLimit = Field(default_factory=RateLimit)

    @model_validator(mode="after")
    def _refuse_unknown_scopes(self) -> PolicyProfile:
        """A scope the core does not understand is a typo, not a future feature.

        It would grant nothing, which is safe and is exactly the problem: the
        person issuing the key ticked something and was told it worked. Refused
        here, once, rather than checked wherever a scope happens to be read.
        """
        unknown = [name for name in self.scopes if name not in KNOWN_SCOPES]
        if unknown:
            known = ", ".join(sorted(KNOWN_SCOPES))
            raise ValueError(
                f"{', '.join(repr(name) for name in unknown)} is not a scope this "
                f"core knows about. The scopes it has are: {known}."
            )
        return self

    @model_validator(mode="after")
    def _enforce_anonymous_ceilings(self) -> PolicyProfile:
        """Hard limits on the anonymous tier, enforced in code.

        ADR-0003 is explicit that these must not be left to correct
        configuration: a misconfigured anonymous profile is exactly the failure
        this project cannot afford, so the model refuses to hold one.
        """
        if self.kind is not ProfileKind.ANONYMOUS:
            return self

        if self.max_tool_risk is not RiskLevel.SAFE:
            raise ValueError(
                "the anonymous profile may only reach safe tools — "
                f"{self.max_tool_risk.value!r} is not allowed"
            )
        if self.may_approve_confirm:
            raise ValueError("the anonymous profile cannot approve confirmations")
        if self.memory_scope in (MemoryScope.USER, MemoryScope.HOUSEHOLD):
            raise ValueError(
                "the anonymous profile cannot reach household or per-user memory; "
                "use the anonymous scope, or none"
            )
        if self.may_enumerate_plugins:
            raise ValueError("the anonymous profile cannot enumerate installed plugins")
        # The anonymous tier is the one caller nobody vouched for. A scope on it
        # would hand the admin JSON API — the trace, the settings, the key
        # issuance — to whoever reaches the chat surface without signing in.
        if self.scopes:
            raise ValueError(
                "the anonymous profile cannot carry a scope — scopes open the "
                "admin surfaces, and nobody vouched for an anonymous caller"
            )
        # ADR-0005: the shared anonymous scratchpad is readable by whoever comes
        # next, including a child. Safe mode turns it off rather than trusting
        # that nothing unpleasant was written there earlier.
        if self.safe_mode and self.memory_scope is not MemoryScope.NONE:
            raise ValueError(
                "with safe_mode on, the anonymous profile must have memory scope "
                "none — the anonymous scope is shared and readable by anyone"
            )
        return self
