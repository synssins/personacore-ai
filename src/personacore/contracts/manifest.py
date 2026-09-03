"""Plugin manifest — spec section 5.1.

The manifest is how a plugin *declares*; the core *enforces*. Nothing a plugin
does at runtime can widen what its manifest asked for.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Plugin names become directory names and appear in topics and audit records.
# Keep them boring so none of those three places need escaping rules.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# "1.x" style: which contract major the plugin targets.
_CONTRACT_RE = re.compile(r"^\d+\.(x|\d+)$")

# Both are matched with `fullmatch`, never `match` (the 2026-08 security review).
#
# Python's `$` also matches immediately before a trailing newline, so
# `_NAME_RE.match("alpha\n")` succeeded and a manifest could write
# `name = "alpha\n"` with a TOML escape and be accepted. That name then becomes
# a directory name and appears in bus topics, audit records and log lines, where
# an embedded newline is its own problem regardless of whether it can traverse.
#
# `\Z` is the usual fix and cannot be used on `_NAME_RE`: this exact pattern
# string is re-exported as `plugins.packages.PLUGIN_NAME_PATTERN` and given to
# pydantic as a path-parameter constraint, compiled by the Rust `regex` crate,
# which has no `\Z` and rejects the pattern outright — the admin routes would
# stop importing. (`\A…\z` compiles in Rust but `\z` is not valid in Python's
# `re` before 3.14, and CI runs 3.12 and 3.13.) `_CONTRACT_RE` matches the same
# way for consistency rather than necessity.
#
# `fullmatch` requires the pattern to consume the whole string, so a trailing
# newline has nowhere to go, and it needs no anchor the two engines disagree
# about. A plain `.match` on either pattern is a defect.


CONTRACT_2_0_CHANGE = (
    "Contract 2.0 changed one thing: permissions.secrets is now a list of "
    "tables instead of a list of names. Each credential is written as "
    '{ name = "openweather_key", description = "What this is and where to get '
    'one.", required = true } — description is required, and required may be '
    "left out because it defaults to true. A plugin that asks for no "
    "credentials keeps secrets = [] and only has to change the contract line."
)
"""What actually changed between contract 1.x and 2.0, in one paragraph.

Kept here, beside the schema it describes, so the manifest error, the loader's
refusal and the author guide all say the same thing. An operator who meets it
should be able to fix the manifest without going and finding ADR-0026.
"""

OLD_SECRETS_FORM_REFUSED = (
    "no longer takes a list of names. Contract 2.0 made each credential a "
    'table: { name = "openweather_key", description = "What this is and where '
    'to get one.", required = true }. The description is required — it is the '
    "text shown beside the box an operator pastes into — and required may be "
    "left out, because it defaults to true. Set it to false for a credential "
    "the plugin can run without. Rewrite this list in that form and set "
    'contract = "2.x" in the [plugin] section.'
)
"""Shown when a 1.x manifest's ``secrets = ["a_key"]`` reaches this core.

Rendered by ``plugins.errors.describe_validation_error`` as
``'permissions.secrets' no longer takes a list of names. …``, so it is written
to follow the field name rather than to stand alone.
"""

REQUIRED_MUST_BE_BOOLEAN = (
    "must be written as true or false, with no quotes around it. TOML booleans "
    "are bare words, so required = \"no\" is the text 'no' and required = 0 is a "
    "number — neither is an answer to 'can this plugin run without the "
    "credential?', and this core refuses them rather than guessing. Leave the "
    "line out for a credential the plugin needs (it defaults to true), or write "
    "required = false for one it can run without."
)
"""Shown when ``required`` is written as anything but a TOML boolean.

Refusing is the point (the 2026-08 security review). Pydantic would happily coerce the *string*
``"no"`` to ``False``, so the plugin started without the credential — while the
admin UI, which reads the manifest's raw TOML for its own good reasons, saw a
value that was not ``false`` and drew the plugin as *waiting for a credential*.
An operator was then looking at a page that said one thing about a plugin that
was doing another, and ADR-0026 says a plugin waiting on an optional credential
is a contradiction that must not be renderable at all.

The two were made to agree by removing the third state rather than by teaching
the page to imitate pydantic's coercion table: what the loader enforces is what
the page shows, and a value neither of them can read as a boolean is a manifest
error the author is told to fix, in the one place that already refuses manifest
errors in words.
"""


PROVIDES_MUST_BE_A_LIST = (
    'must be a list, even when there is only one entry: provides = ["tts"]. One '
    "box can be both a speaker and a microphone, so a plugin may declare more "
    'than one service — provides = ["tts", "stt"] — and the field is always '
    "written as a list."
)
"""Shown when ``provides`` is written as a bare string, or as anything but a list.

``provides = "tts"`` is the obvious typo for a field that is a list almost every
time it is used at all, and pydantic's own message for it ("Input should be a
valid list") does not say what the one-entry form looks like.
"""


class ServiceKind(StrEnum):
    """A kind of service a plugin can register as being — contract 2.1.

    A manifest could always say who a plugin is, what it wanted permission for,
    what tools it offered and what events it sent. It had no line for **what
    kind of service it is**, so a speech engine could not exist as a plugin —
    not for any protocol reason, but because the label had nowhere to go.

    The set is closed and deliberately small. A name that is not in it is
    refused (:func:`unknown_service_message`) rather than ignored: a plugin
    whose author believes it registered a service and did not is worse than one
    that failed to load, because the failure surfaces somewhere else entirely,
    as silence.
    """

    TTS = "tts"
    """Speech out. It turns text into audio — the voice the assistant speaks in."""

    STT = "stt"
    """Speech in. It turns audio into text — what is said out loud, as words."""


_SERVICE_MEANINGS: dict[ServiceKind, str] = {
    ServiceKind.TTS: "a speech engine, turning text into audio",
    ServiceKind.STT: "a transcriber, turning speech into text",
}
"""One plain-English gloss per service, for the refusal that lists what is accepted.

Keyed by the enum member rather than written out beside it, so adding a service
without a gloss is a ``KeyError`` at import rather than a refusal message that
quietly stops listing one of the options it accepts.
"""


def _english_list(parts: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c`` — for a sentence, not a log line."""
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


ACCEPTED_SERVICES = _english_list(
    [f'"{kind.value}" ({_SERVICE_MEANINGS[kind]})' for kind in ServiceKind]
)
"""What ``provides`` accepts, spelled out for an operator reading the refusal."""


def unknown_service_message(value: object) -> str:
    """The refusal for a service name this core does not know, naming it.

    Rendered by ``plugins.errors.describe_validation_error`` as
    ``'plugin.provides' names a service this core does not know: 'ttts'. …``,
    so it is written to follow the field name rather than to stand alone.
    """
    return (
        f"names a service this core does not know: {value!r}. The services a "
        f"plugin may provide are {ACCEPTED_SERVICES}. Check the spelling, or "
        "leave provides out altogether — a plugin that offers no service of its "
        "own declares nothing here. An unknown name is refused rather than "
        "ignored: a plugin whose author believes it registered a service and "
        "did not is worse than one that failed to load."
    )


def duplicate_service_message(value: str) -> str:
    """The refusal for the same service declared twice.

    Deduping silently would be the friendlier-looking choice and the wrong one:
    a repeated entry is nearly always a line that was copied to add a *second*
    service and then not edited, so quietly removing it throws away the only
    evidence that the second service is missing.
    """
    return (
        f"names {value!r} twice. Each service is declared once. The repeat is "
        "refused rather than quietly dropped, because a duplicated entry is "
        "usually a line that was copied to add a second service and then not "
        "edited."
    )


class Transport(StrEnum):
    """How the core reaches the plugin. It treats both identically otherwise."""

    STDIO = "stdio"
    """Subprocess of the core, living in /appdata/plugins/<name>/."""

    HTTP = "http"
    """Its own container or service, reached over the network."""


class RiskLevel(StrEnum):
    """Per-tool risk. Declared here, enforced by the core at call time."""

    SAFE = "safe"
    """Runs silently."""

    CONFIRM = "confirm"
    """Requires spoken or UI confirmation before it runs."""

    RESTRICTED = "restricted"
    """Requires per-user permission, and then confirmation."""


class ToolDeclaration(BaseModel):
    """One tool the plugin exposes."""

    model_config = ConfigDict(extra="forbid")

    risk: RiskLevel
    description: str | None = None


class SecretRequest(BaseModel):
    """One credential a plugin asks an operator for — ADR-0026.

    A request, never an entitlement. The plugin receives what was actually
    supplied, in its own namespace, and nothing else (ADR-0025 section 1).

    All three fields exist because a bare name was not enough to put a box in
    front of somebody: it said what the file would be called and nothing about
    what to paste into it, and it made every credential a reason not to start.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """What the plugin will find it under. It becomes a filename in the
    plugin's own namespace, so the store's own name rules apply to it — checked
    where the file is written (``config.secrets``) rather than duplicated
    here."""

    description: str
    """What this credential is and where to get one, in the author's words.

    **Required, and deliberately so** (ADR-0026). The whole reason this shape
    exists is that a field appeared labelled ``openweather_key`` with nothing
    beside it; making the explanation optional would leave that outcome
    available. Shown beside the box at install and on the plugin's page, as
    **plain text and never markup** — it is third-party content on a page the
    operator trusts.
    """

    required: bool = True
    """Whether the plugin can run without it.

    ``true`` (the default) keeps ADR-0025's behaviour: missing means the plugin
    is not started and is shown as *waiting*, naming what it needs.

    ``false`` means **missing is fine and the plugin starts anyway** — no
    waiting state, no red row. The field is still drawn, so an operator whose
    instance does need one can supply it. A self-hosted service behind optional
    basic auth is the case this exists for.

    Those are the only two values. Anything else is refused rather than coerced
    — see :data:`REQUIRED_MUST_BE_BOOLEAN`.
    """

    @field_validator("required", mode="before")
    @classmethod
    def _check_required_is_a_boolean(cls, v: object) -> object:
        """Refuse anything that is not a genuine TOML boolean (the 2026-08 security review).

        ``mode="before"`` so this sees what the file actually said, ahead of
        pydantic's own coercion — which is the whole problem: left to it,
        ``required = "no"`` becomes ``False`` and the plugin runs without the
        credential, while every other reader of the manifest sees a string.

        A missing ``required`` never reaches here: pydantic applies the default
        without validating it, which is correct — the default is already a
        boolean.
        """
        if not isinstance(v, bool):
            raise ValueError(REQUIRED_MUST_BE_BOOLEAN)
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "must say what the credential is and where to get one. It is the "
                "only thing an operator has to go on when the field appears, so "
                "an empty one is refused rather than shown as a blank line."
            )
        return v


class Permissions(BaseModel):
    """Least privilege, spec section 7. Every list defaults to empty — a plugin
    that declares nothing gets nothing, rather than getting everything."""

    model_config = ConfigDict(extra="forbid")

    network: list[str] = Field(default_factory=list)
    """Hostname allowlist. Empty means no outbound network at all."""

    secrets: list[SecretRequest] = Field(default_factory=list)
    """Credentials this plugin asks for. Plugins never see the whole store.

    A list of tables since contract 2.0 (ADR-0026). The bare-string form was
    **removed rather than deprecated**: nothing was public, every manifest in
    existence was in reach, and a compatibility layer would have been two
    shapes to parse, test and document for the benefit of nobody.
    """

    paths: list[str] = Field(default_factory=list)
    """Extra filesystem paths beyond the plugin's own folder."""

    @field_validator("secrets", mode="before")
    @classmethod
    def _refuse_the_old_form(cls, v: object) -> object:
        """A 1.x manifest's list of names, caught before pydantic's own error.

        Left to pydantic this reads "Input should be a valid dictionary or
        instance of SecretRequest", which tells an operator holding a plugin
        somebody else wrote precisely nothing. This says which field changed,
        what it changed to, and what to type instead.
        """
        if isinstance(v, list) and any(isinstance(item, str) for item in v):
            raise ValueError(OLD_SECRETS_FORM_REFUSED)
        return v

    @property
    def secret_names(self) -> list[str]:
        """Every credential asked for, required or not, in the manifest's order."""
        return [request.name for request in self.secrets]

    @property
    def required_secrets(self) -> list[str]:
        """The ones the plugin cannot run without.

        The list the *waiting* state is computed from: waiting means missing
        **and** required, and a plugin waiting on an optional credential is a
        contradiction (ADR-0026).
        """
        return [request.name for request in self.secrets if request.required]


class EventDeclaration(BaseModel):
    """What the plugin puts on, and takes off, the bus. Spec section 5.2."""

    model_config = ConfigDict(extra="forbid")

    publishes: list[str] = Field(default_factory=list)
    subscribes: list[str] = Field(default_factory=list)


class PluginIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    contract: str
    transport: Transport
    description: str = ""

    entry: str | None = None
    """Command line for stdio plugins. Required for stdio, ignored for http."""

    url: str | None = None
    """Base URL for http plugins. Required for http, ignored for stdio."""

    provides: list[ServiceKind] = Field(default_factory=list)
    """What kind of service this plugin *is* — contract 2.1.

    **A list, and zero entries is the ordinary case.** A plugin that only
    offers tools says nothing here and is unaffected by this field existing;
    the field was added because there was no way for a plugin to say it is a
    speech engine, which is why one could not be written as a plugin at all.

    It is a list rather than a single value because one plugin can genuinely be
    two things: a box that both speaks and listens declares
    ``provides = ["tts", "stt"]``.

    Unknown names and repeats are both refused, by name — see
    :func:`unknown_service_message` and :func:`duplicate_service_message`.
    """

    @field_validator("provides", mode="before")
    @classmethod
    def _check_provides(cls, v: object) -> object:
        """Check the raw list ahead of pydantic's own coercion, for the wording.

        Left to pydantic, an unknown name reads "Input should be 'tts' or
        'stt'", which names neither the offending value in the operator's own
        spelling nor what either option means. Duplicates it would not notice at
        all.
        """
        if isinstance(v, str) or not isinstance(v, list):
            raise ValueError(PROVIDES_MUST_BE_A_LIST)
        known = {kind.value for kind in ServiceKind}
        seen: set[str] = set()
        for item in v:
            if not isinstance(item, str) or item not in known:
                raise ValueError(unknown_service_message(item))
            if item in seen:
                raise ValueError(duplicate_service_message(str(item)))
            seen.add(str(item))
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.fullmatch(v):
            raise ValueError(
                f"plugin name {v!r} must be lowercase letters, digits and hyphens, "
                "start with a letter, and be 2-64 characters"
            )
        return v

    @field_validator("contract")
    @classmethod
    def _check_contract(cls, v: str) -> str:
        if not _CONTRACT_RE.fullmatch(v):
            raise ValueError(
                f"contract {v!r} must look like '2.x' or '2.0' — the contract version "
                "this plugin targets"
            )
        return v


class PluginManifest(BaseModel):
    """The whole manifest file, parsed.

    Validation errors from this model are shown to a human in the admin UI
    (spec section 9), so messages here are written to be read by someone who is
    not holding the source open.
    """

    model_config = ConfigDict(extra="forbid")

    plugin: PluginIdentity
    permissions: Permissions = Field(default_factory=Permissions)
    tools: dict[str, ToolDeclaration] = Field(default_factory=dict)
    events: EventDeclaration = Field(default_factory=EventDeclaration)

    @field_validator("tools")
    @classmethod
    def _check_tool_names(cls, v: dict[str, ToolDeclaration]) -> dict[str, ToolDeclaration]:
        for name in v:
            if not _NAME_RE.fullmatch(name.replace("_", "-")):
                raise ValueError(
                    f"tool name {name!r} must be lowercase letters, digits, "
                    "hyphens or underscores"
                )
        return v

    def model_post_init(self, _context: object) -> None:
        transport = self.plugin.transport
        if transport is Transport.STDIO and not self.plugin.entry:
            raise ValueError("a stdio plugin must declare 'entry' — the command that starts it")
        if transport is Transport.HTTP and not self.plugin.url:
            raise ValueError("an http plugin must declare 'url' — where the core reaches it")

    def risk_of(self, tool_name: str) -> RiskLevel:
        """Risk for a tool. An undeclared tool is not callable, so this raises
        rather than defaulting — defaulting here would fail open."""
        try:
            return self.tools[tool_name].risk
        except KeyError:
            raise KeyError(
                f"plugin {self.plugin.name!r} does not declare a tool named {tool_name!r}"
            ) from None
