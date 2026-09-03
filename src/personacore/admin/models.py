"""Request and response models for the admin API — spec section 9, ADR-0007.

ADR-0007: "The admin HTTP API is built properly now. It is a contract, it is
versioned and documented like one, and it is what actually survives." These
models *are* that contract. The designed UI and every other consumer read
exactly these shapes, so a field added here is a promise and a field removed
here is a breaking change.

Three rules run through the whole file:

* **Every failure carries a plain-English reason** (spec section 9). A broken
  plugin appears in the listing *with* its error rather than vanishing from it
  (spec 5.1); a rejected config write names the key and says what to do.
* **No secret ever leaves as a value** (spec section 7). Core settings
  reference secrets by name, so :class:`ConfigResponse` carries names and
  :func:`personacore.admin.config_io.read_config` proves it.
* **Unknown is a status.** Where the core genuinely does not know something
  yet — plugin runtime health before the supervisor exists — the model says
  ``unknown`` rather than defaulting to healthy.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from personacore.audit import Surface
from personacore.auth.method import AuthMethod
from personacore.contracts.policy import PolicyProfile

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class AdminUser(BaseModel):
    """Who the reverse proxy says is making this request (spec section 7).

    See :func:`personacore.admin.routes.create_admin_router` for why a header
    is trustworthy here and nowhere else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=256)

    door: AuthMethod
    """Which of the three doors (:class:`personacore.auth.method.AuthMethod`)
    produced this identity — the same word ``/health`` and :class:`WhoAmI` use.

    **Required, with no default, deliberately.** ``id`` alone is not a person:
    the three doors mint it under incompatible rules — the trusted header takes
    any printable string up to 256 characters with its case intact, the
    built-in sign-in uses an account name that has been lowercased and
    pattern-checked, and the development bypass is an always-admin break-glass
    naming whoever the environment said. So the proxy's ``alice``, the account
    ``alice`` and ``PERSONACORE_ADMIN_DEV_USER=alice`` are three different
    operators wearing one string, and anything that keys per-person state on
    ``id`` alone (ADR-0030's preferences, and the child lock and "suitable for
    children" that inherit its shape) would hand all three the same row.

    There are exactly two places that build an :class:`AdminUser`, both in
    :mod:`personacore.admin.authn`. A required field means a *third* door
    cannot be added without its author being asked which key space it writes
    into — which is the whole reason this field exists, and is worth more than
    the convenience of a default.
    """

    is_admin: bool = True
    """Whether this operator may see and change things that belong to other
    people — PC-290's "only an admin sees the list of users".

    Defaults to ``True`` because the two identity sources that predate the
    core's own accounts have no notion of a non-admin: a trusted header from a
    login proxy names somebody the proxy already decided may administer this
    (spec section 7), and the development bypass is a break-glass that is an
    admin by definition (PC-294). Only the built-in sign-in produces an
    operator for whom this is ``False``, and it sets it from the account
    record.
    """


# ---------------------------------------------------------------------------
# Health — spec section 9's dashboard, spec section 10's health checks
# ---------------------------------------------------------------------------


class HealthState(StrEnum):
    """Deliberately three-valued. Two values would force "I don't know" to be
    reported as one of "fine" or "broken", and both of those are lies."""

    OK = "ok"
    FAILING = "failing"
    UNKNOWN = "unknown"


class ComponentHealth(BaseModel):
    """One row of the section 9 dashboard."""

    model_config = ConfigDict(extra="forbid")

    name: str
    state: HealthState
    detail: str | None = None
    """Plain English, shown verbatim. None when the state is ``ok``."""

    facts: dict[str, Any] = Field(default_factory=dict)
    """Component-specific numbers (bus counters, breaker state, byte counts).
    Free-form on purpose: the dashboard renders them as a table, and pinning a
    schema here would make every new counter a contract change."""


class SystemHealth(BaseModel):
    """``GET /admin/api/health`` — spec section 9's "system health dashboard:
    LLM host reachability, event bus, each plugin, disk, backup status".

    Backup status is absent because backups (spec section 10) are not built
    yet; it arrives as another :class:`ComponentHealth` in ``components``
    without changing this shape, which is why ``components`` is a list.
    """

    model_config = ConfigDict(extra="forbid")

    state: HealthState
    """Worst state across ``components``. ``unknown`` never masks a
    ``failing``: one broken dependency makes the system failing."""

    checked_at: datetime
    components: list[ComponentHealth]


# ---------------------------------------------------------------------------
# Plugins — spec section 5.1
# ---------------------------------------------------------------------------


class PluginView(BaseModel):
    """One plugin that loaded. ``state``/``detail`` come from the supervisor if
    one was supplied, otherwise ``unknown`` (see
    :class:`personacore.admin.protocols.PluginHealthSource`)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    transport: str
    description: str = ""
    directory: str
    tools: dict[str, str] = Field(default_factory=dict)
    """Tool name → risk level, straight from the manifest. Spec section 5.1:
    "manifest declares, core enforces" — the admin UI shows what was
    declared."""

    state: HealthState = HealthState.UNKNOWN
    detail: str | None = None
    restarts: int = 0

    waiting_for_secrets: list[str] = Field(default_factory=list)
    """Credentials this plugin's manifest asked for that nobody has supplied.

    **Names only** — ADR-0025 §5: a secret value never reaches a response. A
    name is what the manifest already published and what the settings page
    already draws a field for.

    Non-empty means ``state`` is ``unknown`` rather than ``failing``: a plugin
    nobody has given an API key to has not gone wrong, and calling it failing
    both sent people hunting a fault that was not there and disagreed with the
    page beside it, which says "waiting". The list exists as well as the state
    word because "which credential" is the whole of what the operator has to
    do next, and a three-valued enum cannot carry it.
    """

    enabled: bool = True
    """False when the operator switched this plugin off (ADR-0013).

    Separate from ``state`` because "off on purpose" and "not running" are
    different facts, and a dashboard that conflates them sends somebody hunting
    a fault that does not exist. A disabled plugin stays in this list, with its
    folder and its config still on disk — it is simply not started, and its
    tools are neither offered to the model nor callable.
    """


class PluginFailureView(BaseModel):
    """One plugin that did **not** load, and why — spec section 5.1.

    This model exists because the alternative is a plugin silently missing from
    the list, which is the single most confusing thing a plugin system can do
    to the person who just copied a folder in.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None
    """Best effort — the directory name, if that much survived the failure."""

    source: str
    """The manifest, config file or directory responsible."""

    reason: str
    """Plain English, naming the file and the offending key. Safe to render
    verbatim (spec section 9)."""


class PluginListing(BaseModel):
    """``GET /admin/api/plugins`` and the body of ``POST .../reload``."""

    model_config = ConfigDict(extra="forbid")

    plugins: list[PluginView]
    failures: list[PluginFailureView]
    loaded_count: int
    failed_count: int
    scanned_at: datetime


class ReloadResult(BaseModel):
    """``POST /admin/api/plugins/reload`` — spec section 5.1's "adding a plugin
    = copy a folder, hit reload"."""

    model_config = ConfigDict(extra="forbid")

    reloaded: bool
    listing: PluginListing
    message: str
    """What to tell the operator, in one sentence."""


class PluginInstalled(BaseModel):
    """One plugin package that installed successfully — ADR-0013.

    Nothing here comes from the *upload*: the name, version and transport are
    read from the manifest the core validated, and the directory is the one the
    core chose. The uploaded filename is untrusted (spec section 7) and is
    deliberately absent — it is recorded in the audit log and never echoed into
    a response a browser will render.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    transport: str
    directory: str
    replaced: bool
    config_preserved: bool
    """True when this replaced an installed plugin and kept its ``config.toml``.
    An upgrade must not discard an operator's settings (spec section 7)."""

    files: int
    bytes_written: int


class InstallResult(BaseModel):
    """``POST /admin/api/plugins/install`` — ADR-0013's upload."""

    model_config = ConfigDict(extra="forbid")

    installed: PluginInstalled
    listing: PluginListing
    """The listing after the reload, so the caller sees the plugin actually
    arrive rather than being told it did."""

    message: str


class PluginToggled(BaseModel):
    """``POST /admin/api/plugins/{name}/enable`` and ``.../disable``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool
    changed: bool
    """False when it was already in that state. The action still succeeded —
    the caller asked for a state and got it — but an audit record and a notice
    can say "nothing changed" instead of implying something did."""

    listing: PluginListing
    message: str


class PluginUninstalled(BaseModel):
    """``DELETE /admin/api/plugins/{name}`` — ADR-0013's uninstall.

    ``config_removed`` is the honest answer to "does the config go too": the
    plugin's ``config.toml`` lives inside its own folder (spec section 5.1), so
    removing the folder always removes it. The UI has to say so *before* the
    operator confirms, which is what this field is for.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    directory: str
    config_removed: bool
    files_removed: int
    listing: PluginListing
    message: str


MAX_PLUGIN_CONFIG_CHARS = 256 * 1024
"""Ceiling on a submitted ``config.toml`` — a quarter of a megabyte.

Not a guess at what a plugin needs: it is far more than any settings file, and
a limit has to exist because the body of this request is written straight to
disk in an appdata volume the audit log also depends on (spec section 7).
"""


class PluginConfigResponse(BaseModel):
    """``GET /admin/api/plugins/{name}/config`` — spec section 5.1.

    **The content is the file, verbatim, comments and all.** Spec section 5.1
    puts a plugin's settings in the plugin's own folder and the plugin's own
    manifest documents them in comments — ``plugins/_template/config.toml``
    says outright that its comments "become the help text next to the field".
    Parsing to a dict and re-serialising would throw every one of them away, so
    this carries text and the editor edits text.

    ``valid``/``problem`` describe the file *as it is on disk*: a plugin that
    failed to load because its config is malformed must still be editable here,
    with the syntax error shown above the box rather than replacing it.
    """

    model_config = ConfigDict(extra="forbid")

    plugin: str
    path: str
    """Absolute path of the plugin's ``config.toml``, so an operator knows what
    they are editing (and that it is inside the plugin's own folder)."""

    exists: bool
    """False when the plugin ships no ``config.toml`` yet. ``content`` is then
    empty and a write creates the file."""

    content: str = ""
    valid: bool = True
    problem: str | None = None
    """Why the file on disk is not valid TOML, in plain English. ``None`` when
    it parses."""

    secret_references: dict[str, str] = Field(default_factory=dict)
    """``dotted.path -> secret name`` for every ``*_secret`` key, as with core
    settings. A plugin's real credentials are declared in its manifest under
    ``permissions.secrets`` and handed over by the core; this file never holds
    a value (spec section 7)."""


class PluginConfigUpdateRequest(BaseModel):
    """``PUT /admin/api/plugins/{name}/config`` body.

    Text in, text out. The core checks it is well-formed TOML and nothing else
    — what the keys *mean* belongs to the plugin (spec section 5.1).
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        max_length=MAX_PLUGIN_CONFIG_CHARS,
        description="The whole config.toml file, as text.",
    )


class PluginConfigUpdateResponse(BaseModel):
    """What a successful per-plugin config write returns.

    ``config`` is read back from disk rather than echoed, so the caller sees
    what was actually stored. ``reloaded`` is the honest half: ADR-0010 accepts
    UI-only configuration on the condition that a change takes effect without a
    restart, and when this core has nothing that can restart a plugin, the
    message says the setting applies at the next start instead of implying it
    is already live.
    """

    model_config = ConfigDict(extra="forbid")

    saved: bool
    config: PluginConfigResponse
    reloaded: bool
    message: str


MAX_LOOKUP_QUERY_CHARS = 200
"""Ceiling on what an operator may type into a settings-page search box.

Longer than any place name, shorter than anything worth calling an upload. The
text becomes an argument to a plugin's tool (ADR-0016), and an argument from
outside gets a bound before it gets a use (spec section 7)."""


class PluginLookupRequest(BaseModel):
    """``POST /admin/api/plugins/{name}/config/lookup`` body — ADR-0016.

    ``field`` names the setting whose schema declared the lookup. It is not the
    tool: **the caller never names the tool**, because "only a tool the plugin
    nominated in its schema can be called this way" (ADR-0016), and a request
    that chose the tool would be a request that chose its own permissions.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        min_length=1,
        max_length=128,
        description="The setting being filled, as named in config.schema.json.",
    )
    query: str = Field(
        max_length=MAX_LOOKUP_QUERY_CHARS,
        description="What the operator typed — passed to the tool as its query.",
    )


class PluginLookupResult(BaseModel):
    """One match, as data — ADR-0016: results "are rendered as data in a list,
    never interpreted, and never used to build a path or a request".

    ``values`` holds only the keys the schema's ``fill`` map asked for, each
    already a string. Nothing else the tool returned is carried, so a result
    with an extra field in it cannot reach the page at all.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    """What the list shows for this row. Untrusted text from the tool — safe to
    render escaped, never to interpret."""

    values: dict[str, str] = Field(default_factory=dict)
    """``{setting name: value}``, ready to be put in the boxes."""


class PluginLookupResponse(BaseModel):
    """What one search returns.

    ``results`` being empty is a normal answer and not an error: "nothing found"
    is what an operator most needs told plainly, and an HTTP failure for it would
    make a working lookup with no matches look like a broken one.
    """

    model_config = ConfigDict(extra="forbid")

    plugin: str
    field: str
    tool: str
    """Which tool was called. Named in the answer because ADR-0016 makes this a
    tool call like any other, and an operator should be able to see which."""

    query: str
    results: list[PluginLookupResult] = Field(default_factory=list)
    message: str
    """Plain English: how many matches, or why there are none (spec section 9)."""


# ---------------------------------------------------------------------------
# Personas — spec section 5.5
# ---------------------------------------------------------------------------


class PersonaSummary(BaseModel):
    """One entry in section 9's persona picker."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    description: str | None = None
    voice_engine: str | None = None
    voice_name: str | None = None
    is_default: bool = False
    loadable: bool = True
    problem: str | None = None
    """Why this persona cannot be used, if it cannot. A persona folder with a
    broken ``persona.toml`` still appears in the picker, greyed out with its
    reason — same rule as a broken plugin (spec sections 5.1 and 9)."""


class PersonaListing(BaseModel):
    """``GET /admin/api/personas``."""

    model_config = ConfigDict(extra="forbid")

    personas: list[PersonaSummary]
    default_persona: str


class PersonaDetail(BaseModel):
    """``GET /admin/api/personas/{name}`` — the summary plus the prompt itself,
    which the picker shows as a preview before you swap to it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    description: str | None = None
    voice_engine: str | None = None
    voice_name: str | None = None
    is_default: bool = False
    system_prompt: str
    prompt_prefix: str = ""
    """Goes in front of the system message on every turn; empty means nothing is set."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonaSelected(BaseModel):
    """``POST /admin/api/personas/{name}/select``.

    Spec section 5.5 calls persona swapping hot and restart-free; the write
    here is to ``core.toml``'s ``default_persona``, and the live objects are
    updated through the ``apply_settings`` callback.
    """

    model_config = ConfigDict(extra="forbid")

    default_persona: str
    message: str


# ---------------------------------------------------------------------------
# Trace view — spec section 9's observability requirement
# ---------------------------------------------------------------------------


class TraceKind(StrEnum):
    AUDIT = "audit"
    TRANSCRIPT = "transcript"


class TraceEntry(BaseModel):
    """One line of the trace view — an audit record or a transcript message.

    Both families are merged into one time-ordered stream because that is how
    the question is actually asked: "what happened at 21:04?", not "show me
    audit records, then separately show me transcript rows".
    """

    model_config = ConfigDict(extra="forbid")

    kind: TraceKind
    record_id: str
    correlation_id: str
    timestamp: datetime
    surface: Surface
    owner_kind: str
    owner_id: str

    # Audit-only
    category: str | None = None
    action: str | None = None
    risk_level: str | None = None
    outcome: str | None = None
    detail: dict[str, Any] | None = None
    """Tool-call arguments and outcome. Spec section 9 asks for "every tool
    call with arguments and outcome" by name — this is that field."""

    # Transcript-only
    role: str | None = None
    content: str | None = None


class TraceFilters(BaseModel):
    """Echoed back with every page so a client (and a bug report) can see
    exactly which query produced these rows."""

    model_config = ConfigDict(extra="forbid")

    profile: str | None = None
    surface: Surface | None = None
    correlation_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    kinds: list[TraceKind]


class TracePage(BaseModel):
    """``GET /admin/api/trace``.

    Offset pagination rather than a cursor: the underlying store orders by
    timestamp descending with a ``LIMIT``, the trace view is a human reading
    backwards through a bounded window, and a cursor would be precision nobody
    asked for. If the audit table ever grows past what that can serve, the
    replacement is a new field here, not a new endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[TraceEntry]
    limit: int
    offset: int
    returned: int
    has_more: bool
    filters: TraceFilters


# ---------------------------------------------------------------------------
# API keys — spec section 5.4 ("issued and revoked in the admin UI"),
# spec section 9 ("user/profile management and API-key issuance")
# ---------------------------------------------------------------------------

SHOWN_ONCE_WARNING = (
    "This is the only time you will see this key. Copy it into the client now — "
    "the core stores only a one-way hash of it and cannot show it again. If you "
    "lose it, revoke this key and issue another."
)
"""The sentence the UI must put next to a freshly issued key.

It lives here rather than in the UI because it is part of the contract: a
client that renders :class:`ApiKeyIssued` without saying this leaves an
operator believing they can come back for the value later, and the first time
they find out otherwise is when a device stops working.
"""


class ApiKeyView(BaseModel):
    """One issued key, as the admin UI is allowed to see it.

    **There is no field for the key and no field for its hash, and there must
    never be one.** Spec section 7 keeps credential material out of anything an
    operator can read, screenshot or paste into a bug report; the key exists in
    exactly one response body ever (:class:`ApiKeyIssued`) and the hash is the
    store's private business. ``extra="forbid"`` means a future field cannot be
    smuggled in by an over-helpful ``model_dump`` upstream either.

    ``profile`` is the whole policy the key carries, because spec section 5.4's
    per-client policy — "a dumb display widget should not be able to unlock
    doors" — is only reviewable if the screen shows what the widget may
    actually do. None of it is secret.
    """

    model_config = ConfigDict(extra="forbid")

    key_id: str
    """Public identifier. Safe in the UI, in logs and in audit records; not
    derived from the key material."""

    note: str = ""
    """Whatever the issuer typed — "kitchen display", "study lamp script"."""

    created_at: datetime

    enabled: bool
    """Whether this key opens the door right now. Mirrors ``profile.enabled``,
    surfaced flat because "is this key live?" is the one thing a list view has
    to answer without the reader unpacking a nested object."""

    profile: PolicyProfile
    """The policy this key carries — persona, tool allowlist, risk ceiling,
    memory scope, rate limit (spec section 5.4)."""


class ApiKeyListing(BaseModel):
    """``GET /admin/api/keys``.

    **A revoked key is absent from this list, not flagged in it.** Revocation
    deletes the record outright (see
    :meth:`personacore.api.keys.ApiKeyStore.revoke`): a hash nobody can match
    is not worth keeping, and spec section 7's audit log already holds the
    history of what that key did and who revoked it. The recoverable
    "switch it off for now" state is ``enabled=False`` on a key that is still
    listed.
    """

    model_config = ConfigDict(extra="forbid")

    keys: list[ApiKeyView]
    count: int


class ApiKeyIssueRequest(BaseModel):
    """``POST /admin/api/keys`` body.

    The whole policy is supplied rather than a profile name because there is no
    profile store yet — spec section 8's household profiles are P0 *schema* and
    later *feature*. When one exists this model grows an alternative
    ``profile_id`` field; it does not become a different request.
    """

    model_config = ConfigDict(extra="forbid")

    profile: PolicyProfile
    """Must not be of kind ``anonymous``: spec section 5.4 allows no anonymous
    access on the exposed API, even on the LAN. The store refuses it; the API
    turns that refusal into a 400 with a reason rather than a 500."""

    note: str = Field(default="", max_length=200)
    """Operator label for the key. Never a place for the key itself, and it is
    kept out of the audit record for that reason."""


class ApiKeyIssued(BaseModel):
    """``POST /admin/api/keys`` response — **the only place the key ever
    appears.**

    The core stores a SHA-256 of the key and nothing else, so this response
    body is not a convenience view of something retrievable: it is the single
    moment the plaintext exists outside the client. It is not in
    :class:`ApiKeyListing`, not in the audit record, not in any log line, and
    not recoverable by any endpoint. ``api_key_shown_once`` is named the way it
    is so that nobody writing a client has to read this docstring to find that
    out.
    """

    model_config = ConfigDict(extra="forbid")

    api_key_shown_once: str
    """The plaintext key. Shown once, here, and never again."""

    warning: str = SHOWN_ONCE_WARNING
    """Plain English for the operator, shipped with the key so every client
    says the same thing (spec section 9)."""

    key: ApiKeyView
    """The listing row for the key just issued — same shape the list endpoint
    returns, so a UI can append it without a refetch."""


# ---------------------------------------------------------------------------
# Core config — spec section 9's "validation and plain-English errors"
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    """``GET /admin/api/config``.

    ``settings`` is the validated :class:`personacore.config.settings.CoreSettings`
    dumped to JSON. It contains **secret names, never secret values** (spec
    section 7) — ``secret_references`` lists those names explicitly so the UI
    can render "API key: taken from the secret `llm_api_key`" instead of an
    empty-looking field.
    """

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    secret_references: dict[str, str] = Field(default_factory=dict)
    """Setting path → secret name, e.g. ``{"llm.api_key_secret": "llm_key"}``."""

    source: str
    """Absolute path of ``core.toml``, so an operator knows what they are
    editing."""

    exists: bool
    """False on a first run: the defaults are being served and no file has been
    written yet."""


class ConfigProblem(BaseModel):
    """One reason a config write was refused.

    Spec section 9: "per-plugin config editing with validation and
    plain-English errors". The shape is `key` + `problem` + `hint` because a
    message that does not name the key is not actionable, and a message that
    does not say what to do instead is only half of one.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    problem: str
    hint: str | None = None


class ConfigUpdateRequest(BaseModel):
    """``PUT /admin/api/config`` body.

    Wrapped in a ``settings`` object rather than posted bare so the request can
    grow a sibling field (a change note, an "apply without saving" flag)
    without becoming a different request shape.
    """

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]


class ConfigUpdateResponse(BaseModel):
    """What a successful config write returns: the config as it now reads back
    from disk, so the caller never has to guess whether normalisation changed
    anything."""

    model_config = ConfigDict(extra="forbid")

    saved: bool
    config: ConfigResponse
    message: str


class ApiError(BaseModel):
    """The body of every 4xx/5xx this API returns.

    One shape for every failure so the UI has exactly one error renderer, and
    ``problems`` so a validation failure can name several keys at once instead
    of making the operator fix them one round-trip at a time.
    """

    model_config = ConfigDict(extra="forbid")

    error: str
    """Plain English, safe to show verbatim."""

    problems: list[ConfigProblem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The core's own sign-in — PC-283 to PC-291
# ---------------------------------------------------------------------------


class SignInRequest(BaseModel):
    """``POST /admin/api/auth/sign-in``.

    ``password`` is a plain ``str`` and not a ``SecretStr`` on purpose: it is
    hashed and discarded inside the handler, and a ``SecretStr`` here would
    only move the value one attribute deeper while making every call site write
    ``.get_secret_value()``. What keeps it out of a log is that nothing logs
    it, plus the redaction processor's coverage of the field *name* (see
    :mod:`personacore.audit.logging`), which is why the field is called
    ``password`` rather than anything more inventive.
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SignedIn(BaseModel):
    """What a successful sign-in returns. Never the session token — that goes
    back only in the ``Set-Cookie`` header, so a token cannot end up in a page,
    a JavaScript variable or a shell history."""

    model_config = ConfigDict(extra="forbid")

    username: str
    is_admin: bool
    expires_at: datetime


class UserView(BaseModel):
    """One account, as the admin API describes it.

    No password hash and no session token: neither is any use to a caller and
    both are credential material (spec section 7).
    """

    model_config = ConfigDict(extra="forbid")

    username: str
    is_admin: bool
    created_at: datetime
    sessions: int
    """How many sessions this account currently holds — the number that makes
    "end all sessions" a decision rather than a guess (PC-289)."""

    is_minor: bool = False
    """Whether an administrator has marked this account a minor.

    A review signal and nothing else — see
    :attr:`personacore.auth.accounts.UserRecord.is_minor`. Carried here because
    the listing is the only place the household's accounts are described, and a
    flag an admin can set but not see anywhere is a flag nobody trusts.
    """


class UserListing(BaseModel):
    """``GET /admin/api/users`` — **admin only** (PC-290).

    A non-admin gets 403 rather than an empty list: an empty list is a lie that
    says there is nobody else here.
    """

    model_config = ConfigDict(extra="forbid")

    users: list[UserView]


class CreateUserRequest(BaseModel):
    """``POST /admin/api/users`` — admin only."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    is_admin: bool = False
    is_minor: bool = False
    """Set at creation because that is when it is known. It can be changed
    afterwards on the accounts screen; it is not a permission either way."""


class SetupRequest(BaseModel):
    """``POST /admin/api/auth/setup`` — the first account, which is an admin.

    Accepted only while no account exists (PC-291). There is no default
    password to change afterwards, because there is no default password.
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SessionView(BaseModel):
    """One signed-in device — PC-288's "enough to be recognised".

    When it started, when it was last used, and whether it is this one. No
    address, no user-agent, no location: those would make this a record of
    where each household member has been.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    started_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool


class SessionListing(BaseModel):
    """``GET /admin/api/auth/sessions`` — **your own** sessions (PC-290)."""

    model_config = ConfigDict(extra="forbid")

    username: str
    sessions: list[SessionView]


class SessionsEnded(BaseModel):
    """``POST .../sessions/end`` — PC-288 and PC-289's one control."""

    model_config = ConfigDict(extra="forbid")

    username: str
    ended: int
    message: str


class WhoAmI(BaseModel):
    """``GET /admin/api/auth/me`` — who this request is, and by which door.

    ``method`` is the same word ``/health`` uses (PC-294), so an operator
    reading either one is reading the same vocabulary.
    """

    model_config = ConfigDict(extra="forbid")

    username: str
    is_admin: bool
    method: str
    can_sign_out: bool
    """False under the trusted-header and bypass doors: there is no session to
    end, and a sign-out button that cannot sign anybody out is worse than none.
    """


__all__ = [
    "MAX_LOOKUP_QUERY_CHARS",
    "MAX_PLUGIN_CONFIG_CHARS",
    "SHOWN_ONCE_WARNING",
    "AdminUser",
    "ApiError",
    "ApiKeyIssued",
    "ApiKeyIssueRequest",
    "ApiKeyListing",
    "ApiKeyView",
    "ComponentHealth",
    "ConfigProblem",
    "ConfigResponse",
    "ConfigUpdateRequest",
    "ConfigUpdateResponse",
    "HealthState",
    "InstallResult",
    "PersonaDetail",
    "PersonaListing",
    "PersonaSelected",
    "PersonaSummary",
    "PluginConfigResponse",
    "PluginConfigUpdateRequest",
    "PluginConfigUpdateResponse",
    "PluginFailureView",
    "PluginInstalled",
    "PluginListing",
    "PluginLookupRequest",
    "PluginLookupResponse",
    "PluginLookupResult",
    "PluginToggled",
    "PluginUninstalled",
    "PluginView",
    "ReloadResult",
    "SystemHealth",
    "TraceEntry",
    "TraceFilters",
    "TraceKind",
    "CreateUserRequest",
    "SessionListing",
    "SessionView",
    "SessionsEnded",
    "SetupRequest",
    "SignInRequest",
    "SignedIn",
    "UserListing",
    "UserView",
    "WhoAmI",
    "TracePage",
]
