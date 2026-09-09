"""Plugin manifest — spec section 5.1.

The manifest is how a plugin *declares*; the core *enforces*. Nothing a plugin
does at runtime can widen what its manifest asked for.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Plugin names become directory names and appear in topics and audit records.
# Keep them boring so none of those three places need escaping rules.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# "1.x" style: which contract major the plugin targets.
_CONTRACT_RE = re.compile(r"^\d+\.(x|\d+)$")

# `sha256:` plus the 64 lowercase hex characters of a SHA-256 digest — contract 2.2.
_TLS_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# An endpoint-set entry's own pin. The same digest, with the `sha256:` prefix
# optional: the entries are written by an enrolment flow that has just computed
# a fingerprint, and a bare 64-hex digest is what every tool that prints one
# hands over. Normalised to the prefixed form on the way in, so everything
# downstream compares one shape (`mcp_client._check_tls_fingerprint`).
_ENDPOINT_PIN_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")

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


def tls_fingerprint_format_message(value: str) -> str:
    """Shown when ``tls_fingerprint`` does not look like a SHA-256 in the pinned form."""
    return (
        f"tls_fingerprint {value!r} must look like 'sha256:' followed by 64 "
        "lowercase hex characters -- the SHA-256 fingerprint of the server's leaf "
        "certificate, in DER. For example, openssl x509 -in cert.pem -outform DER "
        "| sha256sum prints the hex half."
    )


def tls_fingerprint_needs_https_message(plugin: str, url: str) -> str:
    """Shown when ``tls_fingerprint`` is declared alongside a non-https ``url``."""
    return (
        f"plugin {plugin!r} pins a certificate but the URL is not https: {url!r}. "
        "tls_fingerprint verifies a TLS connection, so it has nothing to check "
        "against an http:// url. Use https:// or remove tls_fingerprint."
    )


# -- the endpoint set (`urls`) ---------------------------------------------
#
# These messages are the endpoint set's half of the wording above. They
# are separate functions rather than parameters on the two above, because the
# single ``url``/``tls_fingerprint`` pair's text is frozen: a plugin that
# declares only ``url`` must read exactly as it did before this field existed,
# and the surest way to keep that true is for nothing on its path to have
# gained an argument.


def endpoint_pin_format_message(url: str, value: str) -> str:
    """Shown when an ``urls`` entry's ``pin`` is not a SHA-256 digest."""
    return (
        f"the pin for {url!r} is {value!r}, which must be 64 lowercase hex "
        "characters -- the SHA-256 fingerprint of that address's leaf "
        "certificate, in DER -- optionally written with a 'sha256:' prefix. For "
        "example, openssl x509 -in cert.pem -outform DER | sha256sum prints the "
        "hex half."
    )


def endpoint_pin_needs_https_message(plugin: str, url: str) -> str:
    """Shown when an ``urls`` entry carries a ``pin`` but is not an https address."""
    return (
        f"plugin {plugin!r} pins a certificate for {url!r}, but that URL is not "
        "https. A pin verifies a TLS connection, so it has nothing to check "
        "against an http:// url. Use https:// or remove that entry's pin."
    )


def endpoint_auth_secret_is_blank_message(url: str) -> str:
    """Shown when an entry's ``auth_secret`` is present but says nothing."""
    return (
        f"the auth_secret for {url!r} is blank. It is the *name* of a secret in "
        "this plugin's own store, not the token itself -- give the name, or "
        "remove the line and the plugin's own auth_secret is sent instead."
    )


ENDPOINT_SET_MUST_NOT_BE_EMPTY = (
    "urls is present but empty. An http plugin reaches its addresses through "
    "url, or through urls with at least one { url = ..., pin = ... } entry -- "
    "an empty list declares nothing at all. Remove the line, or add an entry."
)
"""Shown when ``urls = []``. Refused rather than treated as absent, because an
empty list is far more likely to be a generator that produced nothing than an
author deliberately writing "no addresses"."""


def endpoint_set_pin_belongs_in_the_entry_message(plugin: str) -> str:
    """Shown when ``urls`` and ``tls_fingerprint`` are declared with no ``url``.

    ``tls_fingerprint`` pins the certificate at ``url``. With no ``url`` there
    is nothing for it to pin, and the plugin almost certainly meant to put the
    digest on the entry it belongs to -- so say that, rather than let the
    single field's own message report that a URL which was never written is not
    https.
    """
    return (
        f"plugin {plugin!r} declares tls_fingerprint and urls but no url. "
        "tls_fingerprint pins the certificate at url; each urls entry carries "
        "its own pin instead. Put the digest in that entry's pin = ..., or add "
        "the url it was meant for."
    )


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


class RunbooksDeclaration(BaseModel):
    """``[runbooks]`` in the manifest — ``working/contracts/runbook.md`` §1.10.

    A plugin opts in to the runbooks feature by declaring this, and only
    then does its plugin page grow a **Runbooks enabled** checkbox and does
    it appear as a group in the runbook picker. Absent is the same as
    ``supported = false``: a plugin written before runbooks existed keeps
    loading unchanged and simply is not one of them.
    """

    model_config = ConfigDict(extra="forbid")

    supported: bool = False
    """Whether this plugin may carry, and be the target of, a runbook."""


class EventDeclaration(BaseModel):
    """What the plugin puts on, and takes off, the bus. Spec section 5.2."""

    model_config = ConfigDict(extra="forbid")

    publishes: list[str] = Field(default_factory=list)
    subscribes: list[str] = Field(default_factory=list)


class EndpointDeclaration(BaseModel):
    """One address in an http plugin's optional endpoint set — see :attr:`PluginIdentity.urls`.

    An address and, optionally, the pin for the certificate served *at that
    address*. Deliberately its own two fields rather than a reuse of
    :attr:`PluginIdentity.url` and :attr:`PluginIdentity.tls_fingerprint`: the
    set exists because one plugin can front several machines, and each machine
    terminates TLS itself with its own self-signed certificate. A pin shared
    across the set would be a pin that matches nothing.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    """The address of this one endpoint. Required — an entry with no address is
    not an endpoint."""

    pin: str | None = None
    """``sha256:`` plus 64 lowercase hex, or the bare 64 hex characters, of this
    endpoint's leaf certificate in DER. Normalised to the prefixed form.

    Optional, exactly as :attr:`PluginIdentity.tls_fingerprint` is: an endpoint
    with a certificate the system trust store already accepts does not need
    one. When present the address must be ``https://``
    (:func:`endpoint_pin_needs_https_message`).
    """

    auth_secret: str | None = None
    """The **name** of the secret to send as this endpoint's bearer token.

    The third of the three facts that belong to one machine, and it travels
    with the other two for the same reason the pin does: each machine issues
    its own token, so a credential shared across the set is a credential the
    other machines will refuse. Looked up in the *plugin's* namespace, which is
    already per-plugin, so N named secrets under one plugin need no new
    machinery.

    **It is also what says which entries are one machine.** Because the token
    is minted per machine, entries carrying the same name are that machine's
    several addresses and are held as one connection and one health row
    (:func:`personacore.plugins.supervisor.group_endpoints_by_machine`). That is
    a consequence of what the field already means rather than a second job given
    to it — a machine is the only thing that can accept its own token.

    Optional. An entry that does not name one is sent the plugin's own
    :attr:`PluginIdentity.auth_secret`, if it has one — which is what a set of
    machines standing behind a single shared credential looks like.

    This exists **only inside an entry**. A plugin declaring one ``url`` reads
    its bearer token from :attr:`PluginIdentity.auth_secret` exactly as it
    always has, and never from a list.
    """

    def model_post_init(self, _context: object) -> None:
        """Check and normalise the pin here, where the address is also in hand.

        A field validator would have the digest but not the URL it belongs to,
        and "that is not a SHA-256" is a much worse sentence for an author with
        four entries than "the pin for https://... is not a SHA-256".
        """
        if self.pin is None:
            return
        if not _ENDPOINT_PIN_RE.fullmatch(self.pin):
            raise ValueError(endpoint_pin_format_message(self.url, self.pin))
        if not self.pin.startswith("sha256:"):
            self.pin = f"sha256:{self.pin}"


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

    urls: list[EndpointDeclaration] | None = None
    """An optional *set* of addresses for one http plugin — ADR-0048.

    **Additive, and beside :attr:`url` rather than instead of it.** A plugin
    declaring only ``url`` never sees this field: it is validated by nothing it
    reaches, connected by the path it has always used, and reported on the
    health row it has always had. That is not an accident of the implementation
    — it is the shape of the change, taken deliberately over the tidier
    alternative of making every plugin an endpoint set with one entry.

    It exists for the one plugin that fronts several machines. Each entry
    carries its own address and its own :attr:`EndpointDeclaration.pin`,
    because each machine terminates TLS itself with its own certificate. All of
    them sit under this one plugin record: the machines are part of the plugin,
    not plugins of their own.

    **An entry is an address, not a machine.** A machine that listens on an
    IPv4, an IPv6 and a hostname writes three entries, and the core holds one
    connection and one health row for it, trying its addresses in the order
    written until one answers. What tells the entries of one machine apart from
    another machine's is :attr:`EndpointDeclaration.auth_secret` — see
    :func:`personacore.plugins.supervisor.group_endpoints_by_machine`.

    ``None`` (absent) and ``[]`` are different: absent is the ordinary case,
    empty is refused (:data:`ENDPOINT_SET_MUST_NOT_BE_EMPTY`).

    ``url`` and ``urls`` may both be present. ``url`` is then the plugin's own
    address and keeps every behaviour it has today; the entries are additional.

    Meaningful only when ``transport = "http"``. A stdio plugin declaring it is
    accepted and ignored, with one warning line, for the same reason
    :attr:`auth_secret` is.
    """

    auth_secret: str | None = None
    """Contract 2.2. The **name** of a secret in the core's store, sent as
    ``Authorization: Bearer <value>`` on every request to an http plugin.

    Meaningful only when ``transport = "http"``. A stdio plugin declaring this
    is accepted and ignored, with one warning line, because the field is
    additive (spec 4.5): an old manifest that never mentions it is unaffected.

    Read through :meth:`~personacore.config.secrets.SecretStore.scoped` with
    only this one name, never the whole store, and never through
    ``permissions.secrets`` — an http plugin declares that list empty, because
    it never runs on this machine and never receives a secret itself; this is
    the core's own credential to send, not the plugin's.
    """

    tls_fingerprint: str | None = None
    """Contract 2.2. ``sha256:`` plus 64 lowercase hex characters — the SHA-256
    of the server's leaf certificate, in DER. Verified **instead of** the
    system trust store when present.

    Meaningful only when ``transport = "http"``, and only with an ``https://``
    URL: pinning a certificate for a connection with no TLS on it is refused at
    load time (:func:`tls_fingerprint_needs_https_message`). A stdio plugin
    declaring this is accepted and ignored, with one warning line, for the same
    reason as :attr:`auth_secret`.
    """

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
    runbooks: RunbooksDeclaration = Field(default_factory=RunbooksDeclaration)

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
        if transport is Transport.HTTP and not self.plugin.url and self.plugin.urls is None:
            raise ValueError("an http plugin must declare 'url' — where the core reaches it")
        # Runs first, and returns immediately unless `urls` is present, so a
        # plugin that does not declare an endpoint set reaches
        # `_check_http_auth_fields` in exactly the state it always has —
        # including which error it hits first.
        self._check_endpoint_set(transport)
        self._check_http_auth_fields(transport)

    def _check_endpoint_set(self, transport: Transport) -> None:
        """``urls`` — ADR-0048. Inert for every plugin that does not declare it.

        Written as its own method, called before
        :meth:`_check_http_auth_fields` and returning on the first line unless
        ``urls`` is present, so that the single-``url`` path is not merely
        *equivalent* to what it was but is literally the same code reached in
        the same order. The endpoint set is a second path beside it, never a
        generalisation of it.

        Each entry's ``pin`` format is already checked, and normalised, by
        :meth:`EndpointDeclaration.model_post_init`. What is left here is the
        pair of checks that need something the entry does not hold: the plugin
        name, for the wording, and the scheme rule the single field gets.
        """
        urls = self.plugin.urls
        if urls is None:
            return
        if transport is not Transport.HTTP:
            self._warn_http_only_fields(["urls"])
            return
        if not urls:
            raise ValueError(ENDPOINT_SET_MUST_NOT_BE_EMPTY)
        if self.plugin.tls_fingerprint is not None and not self.plugin.url:
            raise ValueError(endpoint_set_pin_belongs_in_the_entry_message(self.plugin.name))
        for endpoint in urls:
            if endpoint.auth_secret is not None and not endpoint.auth_secret.strip():
                raise ValueError(endpoint_auth_secret_is_blank_message(endpoint.url))
            if endpoint.pin is None:
                continue
            if urlparse(endpoint.url).scheme.lower() != "https":
                raise ValueError(
                    endpoint_pin_needs_https_message(self.plugin.name, endpoint.url)
                )

    def _warn_http_only_fields(self, fields: list[str]) -> None:
        """One warning line for an http-only field on a stdio plugin."""
        # Deferred import, for the reason given in `_check_http_auth_fields`.
        from personacore.audit import get_logger

        get_logger(__name__).warning(
            "http_only_field_ignored_on_stdio",
            plugin=self.plugin.name,
            fields=fields,
        )

    def _check_http_auth_fields(self, transport: Transport) -> None:
        """``auth_secret`` and ``tls_fingerprint`` — contract 2.2.

        Both are meaningful only for an http plugin. On stdio they are accepted
        and ignored (additive, spec 4.5): a manifest naming either still loads,
        with one warning so the author can see the field is doing nothing
        rather than silently trusting a stdio plugin never checks. On http,
        ``tls_fingerprint`` is checked here because it is a shape a load-time
        reader can already see; whether the fingerprint *matches* the server it
        connects to is checked when the core connects (mcp_client._connect_http),
        the same way the url scheme itself is (spec 5.1: manifest declares, core
        enforces — but a malformed declaration is refused before that).
        """
        fingerprint = self.plugin.tls_fingerprint
        auth_secret = self.plugin.auth_secret
        if transport is Transport.HTTP:
            if fingerprint is not None:
                if not _TLS_FINGERPRINT_RE.fullmatch(fingerprint):
                    raise ValueError(tls_fingerprint_format_message(fingerprint))
                scheme = urlparse(self.plugin.url or "").scheme.lower()
                if scheme != "https":
                    raise ValueError(
                        tls_fingerprint_needs_https_message(self.plugin.name, self.plugin.url or "")
                    )
            return
        if fingerprint is None and auth_secret is None:
            return
        # Deferred import: `personacore.audit` imports `personacore.contracts`
        # (for `RiskLevel`) at module scope, so a module-level import here would
        # be circular. By the time a manifest is actually being validated, both
        # modules exist to import fresh -- this file just cannot demand audit be
        # importable before it is.
        from personacore.audit import get_logger

        ignored = [
            name
            for name, value in (("auth_secret", auth_secret), ("tls_fingerprint", fingerprint))
            if value is not None
        ]
        get_logger(__name__).warning(
            "http_only_field_ignored_on_stdio",
            plugin=self.plugin.name,
            fields=ignored,
        )

    def risk_of(self, tool_name: str) -> RiskLevel:
        """Risk for a tool. An undeclared tool is not callable, so this raises
        rather than defaulting — defaulting here would fail open."""
        try:
            return self.tools[tool_name].risk
        except KeyError:
            raise KeyError(
                f"plugin {self.plugin.name!r} does not declare a tool named {tool_name!r}"
            ) from None
