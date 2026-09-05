"""Core settings — ``/appdata/config/core.toml``.

Spec section 5.3 sets the bar this file has to clear: swapping the LLM backend
is a settings change with **zero code changes**. Everything that could differ
between llama.cpp, Ollama and vLLM is a value here, never a branch in code.

TOML throughout, per ADR-0001 item 9. Reading is stdlib ``tomllib``.

**Secrets are referenced by name, never written here.** A config file gets read,
copied into backups and pasted into support conversations; a secret in it is a
secret leaked. Fields that need one name a secret, and the store (section 7)
resolves it.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from personacore.audit.models import Surface
from personacore.config.appdata import AppdataLayout
from personacore.config.hearing import HearingSettings
from personacore.config.image import ImageSettings
from personacore.config.memory import MemorySettings
from personacore.config.runbooks import RunbooksSettings
from personacore.config.voice import VoiceSettings
from personacore.config.workspace import WorkspaceSettings
from personacore.config.wyoming import WyomingSettings
from personacore.contracts.manifest import RiskLevel
from personacore.contracts.policy import MemoryScope, PolicyProfile, ProfileKind


class ConfigError(RuntimeError):
    """The core config is missing or wrong.

    Text is written for whoever is setting the system up, naming the file and
    the key, per spec section 9's plain-English requirement.
    """


class LLMSettings(BaseModel):
    """Spec section 5.3 — the outbound LLM client."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:8080/v1"
    model: str = "local-model"

    api_key_secret: str | None = None
    """NAME of a secret in the store, not the key itself."""

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, value: str) -> str:
        """Store the root, not whatever endpoint URL was pasted (ADR-0011).

        The client already trims this when it builds a request, but trimming
        only there would leave core.toml and the settings screen showing a value
        different from the one actually in use — which is how someone spends an
        afternoon debugging a URL that was never the problem.
        """
        trimmed = value.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions", "/models"):
            if trimmed.endswith(suffix):
                trimmed = trimmed[: -len(suffix)]
                break
        return trimmed.rstrip("/")

    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=300.0, gt=0)
    """Generous by design and separate from connect: a long generation is not a
    dead host, and conflating the two is how streaming gets killed mid-answer.

    **300 seconds, and it is measuring the wrong thing.** On a streamed request
    this resets with every chunk, so it never sees a long *reply* -- what it
    actually bounds is the silence before the first token, which is the model
    reading the prompt. That silence grows with the prompt and is not linear:
    measured on the owner's own host, 5,569 tokens took 139.6 s, of which the
    first 2,540 went in under 10. A pasted chapter is several times that, and
    120 seconds cut it off while the model was working perfectly.

    So this number was raised to five minutes on 2026-09-02 to unblock
    long pastes, and it is a stopgap. Waiting for a first token and waiting
    between tokens are two different failures -- one is a model chewing, the
    other is a model that has died mid-sentence -- and a single value has to be
    high enough for the first, which makes it far too slow to catch the second.
    Splitting them is the real fix."""

    failure_threshold: int = Field(default=5, ge=1)
    cooldown_seconds: float = Field(default=30.0, gt=0)


class LLMRole(StrEnum):
    """The closed set of LLM roles — ADR-0011.

    Closed on purpose. An open-ended set would put a capability's name into
    config and invite the core to learn about specific plugins, which spec
    section 13.5 forbids; adding a role is a contract minor version.
    """

    INTERACTIVE = "interactive"
    """Conversation with a person. The big model; latency is felt."""

    AUTONOMY = "autonomy"
    """Background subagents. Unattended and frequent, so a small fast model."""

    TRIAGE = "triage"
    """Disambiguation and classification. Short, structured, high volume."""

    VISION = "vision"
    """Scene description. Cannot be served by a text-only model at all."""

    COMMANDS = "commands"
    """Command interpretation. Latency-critical."""


class LLMRoles(BaseModel):
    """Several LLM endpoints, routed by what the request is for — ADR-0011.

    Spec section 5.3 never said "one endpoint", and one was the obvious
    reading; the workload does not actually decompose that way. Each role
    carries the full :class:`LLMSettings` shape because each one is a genuinely
    different host, port and model in a real deployment.

    **Only ``interactive`` is required.** Every other role falls back to it, so
    a single-endpoint setup stays a single-endpoint setup with nothing extra to
    configure — see :meth:`resolve`, which is the ONE place that fallback is
    implemented. Callers never write "or interactive" themselves; that is how a
    fallback becomes inconsistent between the health dashboard and the client
    that actually made the request.
    """

    model_config = ConfigDict(extra="forbid")

    interactive: LLMSettings = Field(default_factory=LLMSettings)
    autonomy: LLMSettings | None = None
    triage: LLMSettings | None = None
    vision: LLMSettings | None = None
    commands: LLMSettings | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_a_flat_table(cls, value: Any) -> Any:
        """Read a pre-ADR-0011 flat ``[llm]`` table as the ``interactive`` role.

        Real deployments already have a ``core.toml`` whose ``[llm]`` section
        holds ``base_url``/``model`` directly. Spec section 7's rule that an
        upgrade never touches appdata means that file has to keep working, so
        the shape is detected here rather than being migrated on disk: a flat
        table IS an interactive-only configuration, and saying so once at the
        boundary keeps every reader downstream ignorant of the old shape.

        Detection is by key name, which is unambiguous — no role name is also
        an :class:`LLMSettings` field name, so the two shapes cannot collide.
        A document mixing both is refused rather than guessed at.
        """
        if not isinstance(value, dict):
            return value
        flat_keys = sorted(set(value) & set(LLMSettings.model_fields))
        if not flat_keys:
            return value
        role_keys = sorted(set(value) & {role.value for role in LLMRole})
        if role_keys:
            raise ValueError(
                "the [llm] section mixes the old flat layout "
                f"({', '.join(flat_keys)}) with per-role sections "
                f"({', '.join(role_keys)}). Move the flat settings into "
                "[llm.interactive] and remove them from [llm]."
            )
        return {LLMRole.INTERACTIVE.value: value}

    def resolve(self, role: LLMRole | str) -> LLMSettings:
        """The settings a role actually uses, fallback included.

        The single implementation of ADR-0011's "every other role falls back to
        ``interactive``". Nothing else in the codebase is allowed to reproduce
        it.
        """
        return getattr(self, llm_role(role).value) or self.interactive

    def is_configured(self, role: LLMRole | str) -> bool:
        """Whether the role has its own endpoint rather than borrowing one.

        The admin UI and the section 9 dashboard both need to say "falling
        back" rather than implying five mandatory setups, and both ask here.
        """
        return getattr(self, llm_role(role).value) is not None

    def falls_back_to(self, role: LLMRole | str) -> LLMRole | None:
        """Which role's configuration this one borrows, or ``None`` if its own."""
        return None if self.is_configured(role) else LLMRole.INTERACTIVE

    def configured_roles(self) -> list[LLMRole]:
        """Roles with an endpoint of their own, ``interactive`` always first."""
        return [role for role in LLMRole if self.is_configured(role)]


def llm_role(role: LLMRole | str) -> LLMRole:
    """Coerce a role name — from a config file, a form field or a caller — into
    the closed set.

    One function so every entry point refuses a typo the same way and with the
    same message. Spec section 9's plain-English bar applies: an unknown role
    says what the valid ones are rather than failing with an attribute error
    three frames later.
    """
    if isinstance(role, LLMRole):
        return role
    try:
        return LLMRole(str(role))
    except ValueError as exc:
        known = ", ".join(item.value for item in LLMRole)
        raise ValueError(f"There is no LLM role called '{role}'. The roles are: {known}.") from exc


REDACTED_VALUE = "***"  # noqa: S105 - a marker that replaces a credential, not one
"""What a write-only value looks like once it has left this process.

Read back through the admin API, dumped into the raw config editor, printed by
anything at all: :attr:`BusSettings.password` serialises as this string and not
as itself. Saving a document that still carries the marker means "leave the
stored password alone" — see
:func:`personacore.admin.config_io.restore_write_only_values`, which is the
only thing that ever turns the marker back into a value.
"""

REVEAL_SECRETS = "reveal_secrets"  # noqa: S105 - a serialisation-context key
"""Pass ``context={REVEAL_SECRETS: True}`` to ``model_dump`` to get the real
password. Exactly one caller does: ``config_io.write_config``, writing the file
the value belongs in. Everything else gets :data:`REDACTED_VALUE` by default,
which is the point — a new call site cannot leak the password by forgetting a
precaution it never had to know about."""


class BusSettings(BaseModel):
    """Spec section 5.2 — the MQTT event bus."""

    model_config = ConfigDict(extra="forbid")

    host: str = "mosquitto"
    port: int = Field(default=1883, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    """The broker password, held here rather than in the secret store.

    The exception to this module's "secrets are referenced by name" rule, and a
    deliberate one: an operator typing an MQTT password into a settings screen
    should not first have to hand-create a file in ``/appdata/secrets`` and then
    name it. ``password_secret`` still works for anyone already using it, and
    this wins when both are set (see ``server._resolve_bus_password``).

    :class:`~pydantic.SecretStr` so that the default behaviour of every dump,
    repr and log line is to mask it; the one caller that needs the value asks
    for it explicitly through :data:`REVEAL_SECRETS`.
    """
    password_secret: str | None = None
    client_id: str = "personacore"

    @field_serializer("password", when_used="always")
    def _mask_password(
        self, value: SecretStr | None, info: SerializationInfo
    ) -> str | None:
        """Redact on the way out unless the caller asked for the value.

        Safe by default and unsafe only on request, rather than the other way
        round: ``GET /admin/api/config`` and the raw config editor both render
        this document, and both were one forgotten argument away from printing
        a live broker password onto a screen and into a pasted support thread.
        """
        if value is None:
            return None
        context = info.context if isinstance(info.context, dict) else {}
        if context.get(REVEAL_SECRETS):
            return value.get_secret_value()
        return REDACTED_VALUE


class ServerSettings(BaseModel):
    """What the core itself listens on. TLS terminates at the proxy (section 7),
    so this binds plain inside the Compose network and is never published.

    ``PERSONACORE_HOST`` / ``PERSONACORE_PORT`` override these when set
    (ADR-0010) -- that is what the container actually binds, so these are the
    default an unconfigured install describes, not the last word."""

    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # noqa: S104 — inside the Compose network, fronted by the proxy
    port: int = Field(default=8053, ge=1, le=65535)


def _default_auth_method() -> str:
    """:data:`personacore.auth.method.DEFAULT_METHOD`, imported at call time.

    See :attr:`AuthSettings.method` for why the import cannot be at module
    scope.
    """
    from personacore.auth.method import DEFAULT_METHOD

    return DEFAULT_METHOD.value


class AuthSettings(BaseModel):
    """Spec §7 — which single way into the admin surface is open (ADR-0023).

    A setting rather than an environment variable, per ADR-0010: choosing
    between the core's own sign-in and a login proxy in front is a decision an
    operator makes once, with words in front of them explaining both, and the
    admin UI is where those words are. The development bypass is **not** here
    and never will be — it is break-glass, it has to work when this file cannot
    be read, and a switch that could turn it off from inside the interface
    would be no way back in at all.
    """

    model_config = ConfigDict(extra="forbid")

    method: str = Field(default_factory=lambda: _default_auth_method())
    """``builtin`` (the core's own accounts and sign-in page) or ``proxy`` (a
    trusted identity header set by a login proxy in front).

    Held as a plain string rather than the enum, and defaulted through a
    factory rather than a literal, for the same reason: this module must not
    import ``personacore.auth.method`` at module scope, because that module
    imports :class:`ConfigError` from here. Both indirections buy one thing —
    ``personacore.auth.method`` stays the single definition of which methods
    exist and which one an unconfigured core uses, so this field cannot drift
    away from the function that resolves it.
    """

    @field_validator("method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        """Refuse a method the core does not have, here rather than at startup.

        Same reasoning as :meth:`RetentionSettings._known_surfaces`: this is the
        boundary the admin API writes through, so a typo is refused while the
        operator is still looking at the settings screen instead of being
        accepted by the API and then stopping the container coming up.

        The check is imported at call time rather than at module import because
        ``personacore.auth.method`` imports :class:`ConfigError` from this
        module. Deferring it keeps ONE definition of which methods exist and
        one sentence refusing everything else.
        """
        from personacore.auth.method import coerce_method

        return coerce_method(value).value


KEYLESS_PROFILE_ID = "keyless"
"""The id every keyless request is attributed to, in the audit log and in
``/health``. A name, not an absence: ADR-0018's whole argument is that a request
with no key is *the anonymous profile*, so it has to be findable in the log like
any other caller."""

KEYLESS_PROFILE_NAME = "Keyless callers"


class KeylessSettings(BaseModel):
    """ADR-0018 — the exposed ``/v1`` API answers a caller carrying no key.

    **Off by default, and only the exposed API.** The admin surface has its own
    door and its own decision (ADR-0032); nothing here reaches it.

    The ceilings are not configurable and that is the point. This model names
    one thing an operator chooses — which tools, by name — and
    :meth:`profile` hands the rest to :class:`~personacore.contracts.policy.
    PolicyProfile`, which refuses an over-privileged anonymous profile at
    construction. "Keyless" therefore cannot come to mean "unlimited" by way of
    a setting somebody typed wrong: safe risk at most, no household or per-user
    memory, no approving a confirmation, no enumerating plugins.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    """Whether ``/v1`` answers a request with no valid key.

    With this on, anything that can reach the port can talk to the assistant
    and use whatever :attr:`allowed_tools` names."""

    allowed_tools: list[str] = Field(default_factory=list)
    """Exact ``<plugin>.<tool>`` names a keyless caller may use. Empty — the
    default — means none at all: conversation only.

    An allowlist with no wildcard, so installing a plugin never widens what a
    keyless caller can do."""

    def profile(self) -> PolicyProfile:
        """The profile a keyless request resolves to.

        Built rather than stored, so there is no second copy of the ceilings to
        drift: everything except the tool list is fixed here, and
        ``PolicyProfile`` refuses the result if any of it were ever loosened.
        """
        return PolicyProfile(
            id=KEYLESS_PROFILE_ID,
            display_name=KEYLESS_PROFILE_NAME,
            kind=ProfileKind.ANONYMOUS,
            enabled=self.enabled,
            allowed_tools=list(self.allowed_tools),
            max_tool_risk=RiskLevel.SAFE,
            may_approve_confirm=False,
            memory_scope=MemoryScope.NONE,
            memory_write=False,
            may_enumerate_plugins=False,
            raw_passthrough=False,
        )

    @model_validator(mode="after")
    def _profile_is_constructible(self) -> KeylessSettings:
        """Refuse a document whose profile could not be built.

        Here rather than at the request, so a bad setting is a refusal on the
        screen that saved it instead of a 500 on the API hours later.
        """
        self.profile()
        return self


_SURFACE_NAMES = frozenset(item.value for item in Surface)
"""Valid ``[retention.per_surface_days]`` keys — :class:`Surface`'s values.

Named here so the settings model can check a key without every caller having
to know that the audit store's ``Surface`` enum is the authority on it.
"""


class RetentionSettings(BaseModel):
    """ADR-0004 — conversation age-out. Default 30 days, per surface."""

    model_config = ConfigDict(extra="forbid")

    default_days: int = Field(default=30, ge=1)

    per_surface_days: dict[str, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    """Days per surface. Bounded at 1 for the same reason ``default_days`` is:
    ``AuditStore._purge_older_than`` computes ``now - timedelta(days=...)``, so
    a zero or negative window puts the cutoff at or after `now` and the next
    purge silently deletes every record on that surface regardless of age."""

    @field_validator("per_surface_days")
    @classmethod
    def _known_surfaces(cls, value: dict[str, int]) -> dict[str, int]:
        """Refuse a surface name that does not exist, here rather than at
        assembly.

        This is the boundary the admin API writes through, so a typo is
        refused while the operator is still looking at the settings screen
        (spec section 9). Catching it later — when the core next starts — would
        mean the API accepted a config that stops the container coming up, and
        a core that will not start is a core whose purge never runs.
        """
        for key in value:
            if key not in _SURFACE_NAMES:
                valid = ", ".join(sorted(_SURFACE_NAMES))
                raise ValueError(
                    f"there is no surface called '{key}'. The surfaces are: {valid}"
                )
        return value


class PlaybackSettings(BaseModel):
    """What the administrator has decided about speech playback for everybody.

    ADR-0030: three states, not a boolean. "unset" means the administrator has
    no opinion and each person's own choice stands, which is a different thing
    from forcing playback off for the whole household.
    """

    model_config = ConfigDict(extra="forbid")

    autoplay: Literal["unset", "on", "off"] = "unset"


class DictationSettings(BaseModel):
    """Whether the chat screen's microphone may use the browser's own speech
    recogniser (PLAN.md, "Next — Speech to text, in the container").

    **A boolean, not a tri-state like** :class:`PlaybackSettings`. Playback's
    "unset" exists because a person has a standing choice of their own that the
    administrator can *override* — there are genuinely three states to be in.
    Dictation has no per-person half to override: where the household's audio
    goes is a single household fact, decided once, by the person who owns the
    house's network — not a preference anybody else holds that this could
    silence. So there is nothing for a third state to mean here.

    **Default ``False``.** Local speech-to-text is landing in the same release,
    which makes the browser's recogniser the opt-in alternative rather than the
    default path. A household assistant that promises nothing leaves the house
    should not ship sending audio to Google — the recogniser every browser that
    has one actually uses — unless somebody has deliberately chosen that.
    """

    model_config = ConfigDict(extra="forbid")

    browser: bool = False


class ChatSettings(BaseModel):
    """How a conversation with more than one persona in it behaves.

    One setting, and it is the many-voices contract's §4.3: the hard cap on how
    many persona turns one exchange may run. A **backstop** rather than the
    mechanism — the addressing rules end almost every exchange long before it,
    and an exchange that reaches this has usually found the loop the repetition
    check is meant to catch and did not.

    The default and the ceiling live beside the code that reads it
    (:mod:`personacore.web.screens.chat_voices`), because what the number
    *means* is a property of an exchange rather than of this file. Here it is a
    plain integer, and the section exists at all so that a ``[chat]`` an
    administrator writes is **accepted** — a settings model with
    ``extra="forbid"`` turns an unknown section into a refusal of the whole
    document, and an ordinary edit must never be able to do that.

    It applies only to a room with more than one persona in it. A single
    persona answers once and stops, which is what it has always done.
    """

    model_config = ConfigDict(extra="forbid")

    max_persona_turns: int | None = None
    """``None`` is "the default", not "no limit": there is no way to switch the
    cap off, because two characters keeping a conversation alive for ever is
    the failure it exists to prevent."""


class CoreSettings(BaseModel):
    """The whole of core.toml."""

    model_config = ConfigDict(extra="forbid")

    default_persona: str = "default"
    llm: LLMRoles = Field(default_factory=LLMRoles)
    bus: BusSettings = Field(default_factory=BusSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    keyless: KeylessSettings = Field(default_factory=KeylessSettings)
    """ADR-0018 — whether the exposed ``/v1`` API answers a caller with no key.

    Beside ``[auth]`` and emphatically not part of it: that setting is the admin
    surface's door, this one is the API's, and they are two doors with two
    decisions. Turning this on does nothing to the admin interface.
    """

    playback: PlaybackSettings = Field(default_factory=PlaybackSettings)
    dictation: DictationSettings = Field(default_factory=DictationSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    """ADR-0029 — one switch per speech engine.

    The one section of this file that never refuses a document: see
    :mod:`personacore.config.voice`. A malformed voice setting is dropped and
    named rather than raising, because this model is validated inside
    ``create_app`` and speech must not be able to stop the core starting.
    """

    hearing: HearingSettings = Field(default_factory=HearingSettings)
    """One switch per recogniser — the listening half of the same argument.

    Mirrors ``[voice]`` deliberately, down to never refusing a document: a
    malformed recogniser setting is dropped and named rather than raising, so
    the ears cannot stop the core starting any more than the mouth can.
    """

    wyoming: WyomingSettings = Field(default_factory=WyomingSettings)
    """Home Assistant's speech, served from this core.

    **Off by default, and that is a security decision rather than a taste.**
    The protocol carries no authentication, no authorisation and no encryption
    — upstream says so in as many words — so anything that can reach the port
    can transcribe through this core and make it speak. An operator turning it
    on should be choosing that.
    """

    image: ImageSettings = Field(default_factory=ImageSettings)
    """docs/contracts/image-conversations.md — the image generation service
    an image conversation's `kind` routes to, instead of ``[llm.*]``.

    Unconfigured by default (:meth:`ImageSettings.is_configured`), and that is
    a real, expected state rather than a placeholder waiting to be filled in
    like ``[llm.interactive]``'s guessable ``localhost:8080`` — there is no
    address to guess for a service most cores will never run. An image
    conversation asked to answer with nothing configured says so in the
    thread; it never falls back to anything and never crashes.
    """

    memory: MemorySettings = Field(default_factory=MemorySettings)
    """``working/contracts/memory.md`` §9 — the quiet interval, recall shape
    and retention window memory uses household-wide. Per-persona on/off is
    not here: it is ``persona.toml``'s own ``memory`` key.
    """

    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    """``working/contracts/workspace.md`` §9 — the tool-result cap, the file
    threshold, and the per-file and per-workspace size ceilings. The
    workspace root itself is not here: it is fixed at
    ``<appdata>/workspaces`` (contract §9), not a setting.
    """

    runbooks: RunbooksSettings = Field(default_factory=RunbooksSettings)
    """``working/contracts/runbook.md`` §1.9 — the one household-wide switch
    a runbook run checks before it may start. Default off. Per-plugin
    switches are not here; they live in appdata beside each plugin's own
    runbooks (``personacore.runbooks.store.RunbookStore``).
    """

    def llm_for(self, role: LLMRole | str) -> LLMSettings:
        """The endpoint settings for one role — ADR-0011's only accessor.

        Delegates to :meth:`LLMRoles.resolve` so the fallback lives in exactly
        one place. Every caller that needs an LLM asks for a role and gets
        whatever that role currently resolves to; nothing in the core hardcodes
        an address, and nothing re-implements "or interactive".
        """
        return self.llm.resolve(role)



STARTER_CONFIG = """# PersonaCore core settings.
#
# Edit this file and restart the container to apply changes, or edit it from the
# admin UI. Secrets are referenced BY NAME here and read from the files in
# <appdata>/secrets/ — never paste a key into this file.

default_persona = "default"

[llm.interactive]
# The one required LLM endpoint: conversation with a person (ADR-0011).
#
# Any OpenAI-compatible endpoint. Swapping llama.cpp for Ollama for vLLM is this
# line and nothing else (spec section 5.3). The /v1 suffix is required.
#
# NOTE: from inside a container, "localhost" is the container, not your host.
# Use the host's address, or the service name if the LLM runs in the same stack.
base_url = "http://localhost:11434/v1"
model = "llama3.1:8b"
# api_key_secret = "llm_key"

connect_timeout_seconds = 5.0
read_timeout_seconds = 300.0

# The other roles are OPTIONAL. Each one falls back to [llm.interactive] until
# you give it a section of its own, so one endpoint is a complete setup. Add a
# section only where a different model is genuinely the right tool:
#
#   [llm.autonomy]   background subagents — small and fast
#   [llm.triage]     classification — the cheapest model that is accurate
#   [llm.vision]     scene description — must be a vision-capable model
#   [llm.commands]   command interpretation — latency-critical
#
# A role with its own section also gets its own circuit breaker, so a dead
# vision host cannot take conversation down with it.

[bus]
# MQTT event bus. With no broker reachable the assistant simply has no push
# channel and everything else keeps working.
host = "mosquitto"
port = 1883

[retention]
# Conversation age-out in days (ADR-0004).
default_days = 30

[auth]
# How somebody signs in to the admin interface. Change it here or, better, on
# the Core settings screen, which explains both choices. It takes effect when
# the core restarts.
#
#   "builtin"  this core's own accounts and sign-in page. On a core with no
#              account yet, the interface opens on a setup page that creates
#              the first one. There is no default password.
#   "proxy"    a login proxy in front (Authelia, Authentik, ...) has already
#              signed the person in and tells this core who they are in a
#              header. Only useful if such a proxy is actually there.
#
# Neither of these is the development bypass. That stays an environment
# variable, PERSONACORE_ADMIN_DEV_USER, because it is the way back in when the
# password is lost or this file cannot be read.
method = "builtin"

# [keyless]
# Whether the OpenAI-compatible API at /v1 answers a client that presents no
# key (ADR-0018). Off. Turn it on from the Access keys screen, which says what
# it costs: with it on, anything that can reach this port can talk to the
# assistant and use whatever tools are named below.
#
# It is the API's door only. The admin interface is [auth] above and is not
# affected by this in any way.
#
#   [keyless]
#   enabled = true
#   allowed_tools = ["weather.get_forecast"]
#
# An empty tool list — the default — means conversation and nothing else. Names
# are exact: installing a plugin never widens what a keyless caller can do.

# [image]
# docs/contracts/image-conversations.md — the image generation service an
# image conversation talks to instead of an LLM. Not part of [llm.*]: a
# different request shape (a prompt in, a picture back), a different service.
#
# Absent — the default — means the feature is unconfigured, not pointed at a
# guessed address: unlike [llm.interactive], there is no localhost port an
# image service could be assumed to be listening on. An image conversation
# asked to answer with nothing set here says so in the thread, plainly.
#
#   [image]
#   base_url = "http://localhost:7860"
#   # model = "sd3.5-large"
#   connect_timeout_seconds = 5.0
#   read_timeout_seconds = 120.0

# [voice]
# Speech engines (ADR-0029). Every engine this image was built with has its own
# switch, and they are independent: turning one off does nothing to the others.
# An engine that is off is not loaded — no memory, no CPU — so an engine you
# never enable costs disk space and nothing else.
#
# Switch them on the Voice screen rather than here; saving takes effect
# immediately, with no restart. Written out, one table per engine:
#
#   [voice.engines.vits-onnx]
#   enabled = true
#
# An engine with no table is off. A persona whose voice belongs to an engine
# that is off keeps working and replies in text — it is not broken, and no
# other persona is affected.
"""
"""Written on first run when no config exists.

A container never runs the `init` command, so without this the config directory
stays empty and there is no discoverable way to point the assistant at an LLM —
the settings exist only as defaults buried in code. A file on disk is something
an operator can find, read and edit.
"""


def ensure_core_config(layout: AppdataLayout) -> bool:
    """Write the starter config if none exists. Returns whether it wrote one.

    Never overwrites: an existing file is the operator's, and spec section 7 is
    explicit that an upgrade must not touch appdata content.
    """
    path = layout.core_config_file
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER_CONFIG, encoding="utf-8")
    return True


# There are deliberately NO environment overrides for runtime settings.
#
# Spec section 4.4 is the direct lesson of the predecessor project: configuration
# happens in the admin UI with validation, never by editing a file and hoping.
# An environment variable is worse still — it is invisible to the UI, it silently
# outranks whatever the UI shows, and it can only be changed by editing the
# Compose file and recreating the container.
#
# Runtime settings live in core.toml, which the admin UI reads and writes. The
# only things the environment sets are the ones that must be known before any UI
# exists: where appdata is, what address to bind, who may assert an identity to
# that address, and the break-glass bypass.
#
# WHICH way in is open is NOT one of them: it is `[auth] method` above, chosen
# on the Core settings screen (ADR-0024). It was briefly an environment variable
# and that was this comment's own mistake — an operator picking between their
# own accounts and a login proxy needs the two explained side by side, which is
# a screen, not a Compose file. The bypass stays in the environment because it
# has to work at the moment this file cannot be read.


def _describe(exc: ValidationError, source: Path) -> str:
    """Turn a pydantic error into something an operator can act on.

    A raw pydantic traceback in the admin UI is precisely the failure spec
    section 9 was written to prevent, so the translation happens here rather
    than being left to whoever displays it.
    """
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(top level)"
        message = str(err["msg"])
        # Pydantic prefixes anything a field validator raised with "Value
        # error, ". That is jargon dropped into the middle of a sentence
        # written for whoever is setting the system up, and the admin API
        # already strips it (``config_io._describe_error``) — so without this
        # the file and the API give the same refusal in two different voices.
        prefix = "Value error, "
        if message.startswith(prefix):
            message = message[len(prefix) :]
        lines.append(f"  {where}: {message}")
    return f"{source} could not be read:\n" + "\n".join(lines)


def load_core_settings(layout: AppdataLayout) -> CoreSettings:
    """Load core.toml, or return defaults if it does not exist yet.

    A missing file is a legitimate first run, not an error: the defaults are
    chosen to start a working stack against a local LLM host. A file that exists
    but is wrong IS an error — silently falling back to defaults there would
    hide a typo and start the assistant pointed somewhere unintended.
    """
    path = layout.core_config_file
    if not path.exists():
        return CoreSettings()

    try:
        raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be opened: {exc.strerror or exc}") from exc

    try:
        return CoreSettings(**raw)
    except ValidationError as exc:
        raise ConfigError(_describe(exc, path)) from exc
