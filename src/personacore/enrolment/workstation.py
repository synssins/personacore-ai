"""Enrolling a workstation from a pairing code — the one unauthenticated call.

What the owner sees is in ``docs/wiki``: a code on the Plugins screen, a Join
button on the workstation, and a named healthy row a moment later. What this
module is responsible for is everything between those two, and the property it
exists to hold is this one:

**The workstation's bearer token never travels over the plaintext wire.**

The core's own surface is plain HTTP on a household LAN. The enrolment request
therefore carries no credential at all — a pairing code, an address, a
certificate fingerprint and a tool list, nothing more. The core *mints* the
token and pushes it back to the workstation over the workstation's own TLS,
pinned to the fingerprint the request just supplied, with the pairing code
alongside it so the Agent can tell the push came from the console its owner is
standing at rather than from a stranger. Only if that push succeeds is anything
written to disk.

The residual risk is stated where it belongs, in the ADR: an attacker already
present on the LAN and actively interfering can substitute their own address and
fingerprint and receive a token, enrolling a counterfeit workstation. They gain
nothing on the real one, and the real one visibly fails to join. Passive
observation of the wire yields nothing usable. That was weighed and accepted.

**Order is the design.** Redeem, validate, derive, mint, push, *then* persist.
Persisting after the push means a failed push has nothing to roll back, which is
better than a rollback that has to be specified and tested. The only residue is
an Agent holding a token for an enrolment that did not complete, which is inert
and is replaced by the next attempt.

Nothing here builds an HTTP route or reads a FastAPI object; see
:mod:`personacore.admin.api_enrol` for the route and its door — or rather its
deliberate lack of one.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import re
import secrets as secrets_module
import ssl
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx2
import tomli_w

from personacore.audit import get_logger
from personacore.config.appdata import AppdataLayout
from personacore.config.secrets import SecretError, SecretStore
from personacore.contracts.manifest import PluginManifest, RiskLevel
from personacore.plugins.mcp_client import PluginTransportError, build_pinned_client_kwargs
from personacore.plugins.packages import (
    DEFAULT_PACKAGE_LIMITS,
    PackageLimits,
    PackageRejected,
    install_package,
    set_plugin_enabled,
    uninstall_package,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# The shape of the call
# ---------------------------------------------------------------------------

ENROL_PATH = "/enrol/workstation"
"""Where the Agent sends its Join.

**Top level, not under ``/admin``**, and the reason is operational rather than
aesthetic. The admin surface is the one an operator is most likely to have
fenced off at a reverse proxy, and enrolment has to be reachable from the
workstation on the LAN at the moment the owner presses Join. A route whose
whole purpose is to be reached without a credential does not belong inside the
namespace whose whole purpose is to require one.
"""

AGENT_TOKEN_PATH = "/enrol/token"  # noqa: S105 - a url path, not a credential
"""Where the core pushes the minted token, on the Agent's own origin."""

PLUGIN_NAME_PREFIX = "workstation"
"""Every enrolled machine is ``workstation-<slug>`` so it is recognisable in a
flat plugin catalogue that also holds weather and timers."""

TOKEN_BYTES = 48
"""Bytes of entropy in the minted bearer token — 384 bits, ~64 characters of
url-safe text. Generous on purpose: it is never typed by a person, it grants
``shell_run`` on somebody's desktop, and it costs nothing to make guessing
hopeless."""

MAX_BODY_BYTES = 256 * 1024
"""Ceiling on the enrolment request body. A tool list is a few kilobytes; this
is room for a hundred of them and a refusal for anything that is not a Join."""

MAX_TOOLS = 200
MAX_CODE_CHARS = 128
MAX_NAME_CHARS = 64
MAX_URL_CHARS = 512

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
MAX_PUSH_RESPONSE_BYTES = 64 * 1024
"""Short timeouts and a capped read, because step 5 makes this core connect to
an address an unauthenticated caller chose. A slow-loris on the other end must
cost one connection for a few seconds, not a worker for as long as it likes."""


CODE_REFUSED = (
    "That pairing code is not valid. A code lasts a few minutes and works once. "
    "On the core, open the Plugins screen and click Add a workstation to get a "
    "fresh one, then type that."
)
"""**One refusal, one sentence, for every way a code can be wrong** — never
issued, expired, superseded, already spent, mistyped. Anything that told the
two apart would say whether a code is currently live, which is the one fact
worth knowing to somebody guessing at them."""

RATE_LIMITED = (
    "Too many enrolment attempts from this address. Wait {seconds} seconds and "
    "try again with a fresh code."
)

BODY_REFUSED = (
    "That is not an enrolment request. Send a JSON object with the pairing "
    "code, this machine's name, its https address, its certificate "
    "fingerprint, its version and its tools."
)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class EnrolmentRefused(Exception):
    """One refusal, carrying the status code and the sentence a human reads.

    Every message on this class is written to be shown verbatim to whoever
    pressed Join (spec section 9): it names the problem and says what to do
    about it, and it never quotes the token or the code.
    """

    def __init__(self, status_code: int, message: str, *, redeemed: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.redeemed = redeemed
        """Whether a valid pairing code had already been burnt when this was
        raised.

        The route needs this to decide whether the refusal is worth an audit
        row. Recorded as a fact rather than inferred from the status code: a
        malformed body is a ``400`` raised *before* the code is looked at, and
        auditing that would let an unauthenticated caller make this core write
        to appdata as fast as it can send nonsense. Refusals past the code are
        bounded by the number of codes the owner issued.
        """


# ---------------------------------------------------------------------------
# Validation — everything here arrives from an unauthenticated caller
# ---------------------------------------------------------------------------

_DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
"""What a machine may call itself.

A hostname, in other words, with room for a space. Deliberately narrow rather
than escaped-on-render: this string ends up in a manifest description, an audit
record, a log line and a plugin page, and the cheapest way to be sure it is
harmless in all four is for it never to contain anything that needs thinking
about. A name outside this set is refused with a sentence saying so, not
silently mangled.
"""

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_FINGERPRINT_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")

MAX_SLUG_CHARS = 64 - len(PLUGIN_NAME_PREFIX) - 1
"""How much of a normalised name fits after ``workstation-`` inside the
manifest's 64-character plugin-name limit."""


def normalise_display_name(raw: object) -> str:
    """The name the owner is shown, normalised rather than applied silently.

    Windows hostnames are commonly uppercase and may contain underscores, so
    what a machine calls itself and what reads well in a plugin list are not
    the same string. The normalisation here is only whitespace — case is left
    alone, because the uppercase form is what is printed on the machine and quietly
    lowercasing it makes the row harder to recognise, not easier.
    """
    if not isinstance(raw, str):
        raise EnrolmentRefused(400, "This machine did not say what it is called.")
    collapsed = " ".join(raw.split())
    if not collapsed:
        raise EnrolmentRefused(400, "This machine did not say what it is called.")
    if not _DISPLAY_NAME_RE.fullmatch(collapsed):
        raise EnrolmentRefused(
            400,
            f"{collapsed[:MAX_NAME_CHARS]!r} cannot be used as a workstation name. "
            "Use up to 64 letters, digits, spaces, dots, hyphens or underscores, "
            "starting with a letter or a digit.",
        )
    return collapsed


def derive_plugin_name(display_name: str) -> str:
    """``FRONT-DESK`` becomes ``workstation-front-desk``.

    Lowercase, runs of anything that is not a letter or a digit become a single
    hyphen, and the result is prefixed. The prefix is what guarantees the
    manifest's "starts with a letter" rule holds even for a machine called
    ``2ND-FLOOR``, and what makes an enrolled machine recognisable beside the
    weather plugin.

    **Never auto-suffixed.** Two machines whose names normalise to the same
    slug collide, and the collision is refused rather than turned into
    ``…-2``: a wiped and reinstalled workstation would otherwise become a
    silent second row beside a dead one, which is worse than being told.
    """
    slug = _SLUG_STRIP_RE.sub("-", display_name.lower()).strip("-")
    if not slug:
        raise EnrolmentRefused(
            400,
            f"{display_name!r} has no letters or digits in it, so there is no name "
            "to give this workstation. Rename the machine and try again.",
        )
    slug = slug[:MAX_SLUG_CHARS].strip("-")
    return f"{PLUGIN_NAME_PREFIX}-{slug}"


def auth_secret_name(plugin_name: str) -> str:
    """The name the minted token is stored under: ``workstation_front_desk_token``.

    The plugin's own name with hyphens turned into underscores, plus
    ``_token``. Underscores because that is how every other credential in the
    store is spelled, and the plugin name because the secret is scoped to that
    plugin and a constant ``workstation_token`` stopped being usable the moment
    a household could have two machines.
    """
    return f"{plugin_name.replace('-', '_')}_token"


def normalise_fingerprint(raw: object) -> str:
    """``sha256:`` plus 64 lowercase hex, from either form the Agent may send.

    Contract 2.2 writes the pin with the prefix and
    :func:`personacore.plugins.mcp_client._check_tls_fingerprint` compares
    against that spelling, so that is what is stored. A bare digest is accepted
    and prefixed, and hex is lowercased, because both are what a caller
    computing a digest naturally produces and neither is ambiguous.
    """
    if not isinstance(raw, str):
        raise EnrolmentRefused(400, "This machine did not send a certificate fingerprint.")
    match = _FINGERPRINT_RE.fullmatch(raw.strip())
    if match is None:
        raise EnrolmentRefused(
            400,
            "The certificate fingerprint is not a SHA-256 digest. It must be 64 "
            "hexadecimal characters, on its own or written as 'sha256:' followed "
            "by them.",
        )
    return f"sha256:{match.group(1).lower()}"


def validate_url(raw: object) -> tuple[str, str]:
    """The workstation's address, and the origin the token push goes to.

    Returns ``(url, origin)``. The origin is **rebuilt from the parsed parts**
    and never sliced out of the caller's string, so nothing smuggled into the
    authority survives into the address this core dials.

    What is refused, and why each one:

    * anything but ``https`` — the pin means nothing without TLS, and the whole
      point of this design is that the token crosses an encrypted hop;
    * a url carrying a username or password — a credential in an address this
      core is about to connect to is somebody trying to make it authenticate
      somewhere;
    * a loopback, unspecified or link-local IP literal — a workstation is not
      the core, and pointing this at the core's own interfaces is the shape of
      a request that wants the core to talk to itself. Hostnames are not
      resolved to check this: with the certificate pinned, a name that resolves
      somewhere unhelpful cannot complete the push anyway, and resolving here
      would only add a lookup an attacker controls the answer to.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise EnrolmentRefused(400, "This machine did not send an address to reach it on.")
    candidate = raw.strip()
    if len(candidate) > MAX_URL_CHARS:
        raise EnrolmentRefused(400, "That address is too long to be a workstation address.")
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        raise EnrolmentRefused(
            400, "That address could not be read as a URL."
        ) from None

    if parsed.scheme.lower() != "https":
        raise EnrolmentRefused(
            400,
            "A workstation must be reachable over https. The core pins its "
            "certificate and sends it a credential, and neither means anything "
            "over a plain http address.",
        )
    if parsed.username or parsed.password:
        raise EnrolmentRefused(
            400, "A workstation address must not carry a username or a password in it."
        )
    host = parsed.hostname
    if not host:
        raise EnrolmentRefused(400, "That address does not name a host to reach.")
    if port is not None and not (0 < port < 65536):
        raise EnrolmentRefused(400, "That address does not name a usable port.")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback or address.is_unspecified or address.is_link_local
    ):
        raise EnrolmentRefused(
            400,
            f"{host} is not an address another machine can be reached at. Give the "
            "workstation's address on the network the core is on.",
        )

    authority = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
    if port is not None:
        authority = f"{authority}:{port}"
    origin = f"https://{authority}"
    url = urlunparse(("https", authority, parsed.path, parsed.params, parsed.query, ""))
    return url, origin


def validate_version(raw: object, *, what: str) -> str:
    """A version string that is safe in a filename-adjacent manifest field."""
    if not isinstance(raw, str) or not _VERSION_RE.fullmatch(raw.strip()):
        raise EnrolmentRefused(
            400,
            f"{what} is missing or is not a version. Use letters, digits, dots, "
            "hyphens, underscores or plus signs.",
        )
    return raw.strip()


ACCEPTED_RISK = frozenset({RiskLevel.SAFE.value})
"""The only risk level a workstation may declare at enrolment — ``safe``.

**Narrower than the manifest allows, and narrowed on purpose.** The manifest
knows three levels; this core can honour one of them today. ``AgentLoop`` is
built with no confirmation channel anywhere in ``src`` — ``confirmations``
defaults to ``None`` — so ``_ask_confirmation`` returns ``UNAVAILABLE`` and
``gate_tool_call`` refuses, correctly: nobody said yes. A ``restricted`` tool
does not get that far, refused earlier for want of a permission to check
against. Both are therefore dead on arrival.

Accepting them here would let a workstation join, report a healthy row, and have
part of itself silently never run — a failure landing a long way from its cause,
and one the owner would have to debug from the wrong end. Validation that admits
what the system will later refuse is worse than refusing at the door.

**Before widening this, check that issue #6 has landed** and that the assembled
core actually passes a ``ConfirmationProvider`` to ``AgentLoop`` — not merely
that the enum has three members. ``working/contracts/workstation.md`` section 4
and ADR-0044 (the same decision for Home Assistant) record the arrangement this
replaces: until the core can ask, the Agent is the gate, and the registration
says ``safe`` because that is the honest description of what *this* side
enforces. When confirmation lands the registration is regenerated with honest
levels and the Agent's own prompt becomes a second gate rather than the only one.
"""


def risk_refused_message(name: str, risk: object) -> str:
    """Why a tool was refused, in terms somebody can act on without the contract.

    Spec section 9: a refusal names the thing to change and says why. "Must be
    'safe'" on its own reads as an arbitrary rule and invites somebody to
    argue with it; the reason is the part that makes it obviously right, and
    the part that stops an Agent author declaring ``confirm`` in good faith and
    shipping a workstation whose best tools never run.
    """
    return (
        f"The tool {name!r} is declared {risk!r}. Every tool in a workstation "
        "registration must be declared 'safe'. That is not a judgement about what "
        "the tool does: this core has no way to ask a person to confirm anything "
        "yet, so a tool declared 'confirm' or 'restricted' is refused every single "
        "time it is called, and the workstation would join looking healthy with "
        "part of it dead. Until confirmation is built, the workstation Agent is "
        "the gate — declare the tool 'safe' here and let the Agent prompt for it."
    )


def validate_tools(raw: object) -> dict[str, dict[str, str]]:
    """The tool list, checked against the manifest's own rules and one narrower one.

    Risk is checked here rather than left to the manifest for two reasons. The
    wording: left to pydantic an unknown level reads "Input should be 'safe',
    'confirm' or 'restricted'", which names neither the tool it came from nor
    why. And the rule itself is *narrower* than the manifest's — see
    :data:`ACCEPTED_RISK`. The tool names are checked by
    :class:`~personacore.contracts.manifest.PluginManifest` when the document is
    validated, which is the rule that actually governs them.
    """
    if not isinstance(raw, list):
        raise EnrolmentRefused(
            400, "This machine did not send a list of the tools it offers."
        )
    if not raw:
        raise EnrolmentRefused(
            400,
            "This machine offers no tools, so enrolling it would add a plugin that "
            "can do nothing.",
        )
    if len(raw) > MAX_TOOLS:
        raise EnrolmentRefused(
            400, f"A workstation may offer at most {MAX_TOOLS} tools."
        )

    tools: dict[str, dict[str, str]] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise EnrolmentRefused(
                400, "Each tool must be sent as an object with a name and a risk."
            )
        name = entry.get("name")
        risk = entry.get("risk")
        if not isinstance(name, str) or not name:
            raise EnrolmentRefused(400, "One of the tools was sent without a name.")
        if len(name) > MAX_NAME_CHARS:
            raise EnrolmentRefused(400, "One of the tool names is too long to be a tool name.")
        if name in tools:
            raise EnrolmentRefused(
                400, f"The tool {name!r} was sent twice. Each tool is listed once."
            )
        # ``safe`` and nothing else — see ACCEPTED_RISK above before widening
        # this. It is narrowed because of issue #6, not out of caution, and the
        # comment there says what has to be true before `confirm` may be
        # accepted again.
        if not isinstance(risk, str) or risk not in ACCEPTED_RISK:
            raise EnrolmentRefused(400, risk_refused_message(name, risk))
        tools[name] = {"risk": risk}
    return tools


# ---------------------------------------------------------------------------
# The request, once it has been read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnrolmentPayload:
    """One Join, with every field already checked and normalised."""

    display_name: str
    plugin_name: str
    secret_name: str
    url: str
    origin: str
    tls_fingerprint: str
    agent_version: str
    contract_version: str
    tools: dict[str, dict[str, str]]
    host: str


def read_code(document: object) -> str:
    """The pairing code out of a decoded body, and nothing else.

    Separate from :func:`read_payload` because the code is redeemed *before* any
    other field is looked at. It comes back **unvalidated and unexamined**: its
    only handling is a length cap, because the pairing store compares it in
    constant time and any shape check made before that call would be a cheaper
    oracle than the comparison it precedes.
    """
    if not isinstance(document, Mapping):
        raise EnrolmentRefused(400, BODY_REFUSED)
    raw = document.get("code")
    return raw[:MAX_CODE_CHARS] if isinstance(raw, str) else ""


def read_payload(document: object) -> EnrolmentPayload:
    """Everything but the code, checked and normalised.

    Only ever called once the code has been redeemed, so its refusals may say
    what is actually wrong (spec section 9) without telling a stranger anything.
    """
    if not isinstance(document, Mapping):
        raise EnrolmentRefused(400, BODY_REFUSED)
    _refuse_unknown_fields(document)

    display_name = normalise_display_name(document.get("display_name"))
    plugin_name = derive_plugin_name(display_name)
    url, origin = validate_url(document.get("url"))
    payload = EnrolmentPayload(
        display_name=display_name,
        plugin_name=plugin_name,
        secret_name=auth_secret_name(plugin_name),
        url=url,
        origin=origin,
        tls_fingerprint=normalise_fingerprint(document.get("tls_fingerprint")),
        agent_version=validate_version(document.get("agent_version"), what="The Agent version"),
        contract_version=validate_version(
            document.get("contract_version"), what="The contract version"
        ),
        tools=validate_tools(document.get("tools")),
        host=urlparse(url).hostname or "",
    )
    return payload


REQUEST_FIELDS = frozenset(
    {
        "code",
        "display_name",
        "url",
        "tls_fingerprint",
        "agent_version",
        "contract_version",
        "tools",
    }
)
"""Every field an enrolment request may carry. **There is no ``token``.**"""

CREDENTIAL_IN_REQUEST = (
    "An enrolment request must not carry a credential. The core mints the "
    "workstation's token itself and pushes it back over the workstation's own "
    "TLS connection, so nothing secret crosses the wire in this direction. "
    "Send the pairing code, the machine's name, its https address, its "
    "certificate fingerprint, its version and its tools, and nothing else."
)


def _refuse_unknown_fields(document: Mapping[str, Any]) -> None:
    """Refuse a body carrying anything this call does not take.

    Ignoring an unknown field would be the quiet failure here. The direction of
    the token was reversed after this feature was designed, and an Agent built
    against the earlier shape sends one; accepting that request and dropping the
    field would enrol the machine with a *different* token than the one it kept,
    and the mismatch would surface later as a 401 with no obvious cause. Refusing
    says so at the moment it can still be fixed.
    """
    unknown = sorted(str(key) for key in document if key not in REQUEST_FIELDS)
    if not unknown:
        return
    if any("token" in key or "secret" in key for key in unknown):
        raise EnrolmentRefused(400, CREDENTIAL_IN_REQUEST)
    raise EnrolmentRefused(
        400,
        f"This enrolment request carries fields the core does not take: "
        f"{', '.join(repr(key) for key in unknown[:5])}. "
        f"It takes {', '.join(sorted(REQUEST_FIELDS))} and nothing else.",
    )


def decode_body(body: bytes) -> object:
    """JSON in, object out, with the same sentence for every way it can fail."""
    if len(body) > MAX_BODY_BYTES:
        raise EnrolmentRefused(413, BODY_REFUSED)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EnrolmentRefused(400, BODY_REFUSED) from None


# ---------------------------------------------------------------------------
# The manifest this writes
# ---------------------------------------------------------------------------


def build_manifest_document(payload: EnrolmentPayload) -> dict[str, Any]:
    """The registration, as the contract's section 2 writes it.

    Built as a document and validated by the core's own
    :class:`~personacore.contracts.manifest.PluginManifest` before it is
    serialised, so the tool-name rule, the risk levels, the contract version and
    the "a pin needs an https url" rule are the manifest's own and not a second
    copy of them living here.
    """
    return {
        "plugin": {
            "name": payload.plugin_name,
            "version": payload.agent_version,
            "contract": payload.contract_version,
            "transport": "http",
            "url": payload.url,
            "auth_secret": payload.secret_name,
            "tls_fingerprint": payload.tls_fingerprint,
            "description": (
                f"Workstation {payload.display_name} — acts on that machine and the "
                "devices plugged into it."
            ),
        },
        # A declaration rather than a gate (ADR-0012), written down because the
        # plugin page shows an operator what the manifest asked for.
        "permissions": {"network": [payload.host], "secrets": [], "paths": []},
        "tools": payload.tools,
        "events": {"publishes": [], "subscribes": []},
    }


def render_manifest(payload: EnrolmentPayload) -> str:
    """Validate the document, then serialise it with ``tomli_w``.

    Never string-formatted by hand. The contract's own section 2 carries a
    correction because an earlier hand-written form put ``[tools.x]`` on one
    line as an inline table, which is not valid TOML in that position; a writer
    cannot make that mistake, and it cannot be made to emit a value a caller
    smuggled quotes or newlines into either.
    """
    document = build_manifest_document(payload)
    try:
        PluginManifest.model_validate(document)
    except Exception as exc:  # noqa: BLE001 - pydantic's own message is the useful one
        raise EnrolmentRefused(
            400,
            "The details this machine sent do not make a valid plugin registration: "
            f"{_first_problem(exc)}",
        ) from None
    return tomli_w.dumps(document)


def _first_problem(exc: Exception) -> str:
    """One line out of a pydantic error, for a person who is not holding the source."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
        except (IndexError, TypeError):
            return str(exc).splitlines()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "the registration"
        return f"{location}: {first.get('msg', 'is not valid')}"
    return str(exc).splitlines()[0]


def build_package(payload: EnrolmentPayload, manifest: str | None = None) -> bytes:
    """The registration folder, as bytes ``install_package`` accepts.

    An archive built in memory and handed straight to the installer, rather than
    a folder written into ``plugins-http.d/`` from here. That is deliberate and
    it is the point of the whole function: :func:`install_package` stages inside
    appdata, validates with the real scanner *before* anything moves into place,
    checks the name against the manifest's rule on the line that joins it to a
    directory, refuses a collision, and clears an orphaned secret namespace
    left by a previous plugin of the same name. Writing the folder here would
    be a second opinion about which directory is safe to write, which is exactly
    what spec section 7 wants none of.

    The zip is not a *format* this feature has: nothing is uploaded, exported or
    kept. It is the argument type the one safe installer takes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{payload.plugin_name}/manifest.toml", manifest or render_manifest(payload)
        )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Step 5 — the outbound push
# ---------------------------------------------------------------------------

ClientFactory = Callable[..., httpx2.AsyncClient]


def _pinned_client(origin: str, fingerprint: str) -> httpx2.AsyncClient:
    """The client the token is pushed over. Every argument is a refusal.

    ``verify`` and ``transport`` come from
    :func:`personacore.plugins.mcp_client.build_pinned_client_kwargs`, so the
    certificate is checked by the same code path a running HTTP plugin's
    connection is — including that transport's own refusal to send anything to a
    scheme that is not https or to a host that is not the pinned one.

    ``follow_redirects`` is off, so a 302 cannot walk this connection to
    somewhere the pin does not cover. ``trust_env`` is off on both the client
    and the transport, so no ``HTTPS_PROXY`` in the container's environment can
    put a third party between the core and the workstation. The timeouts are
    short because the address came from an unauthenticated caller and must not
    be able to hold a worker.
    """
    kwargs = build_pinned_client_kwargs(origin, fingerprint, trust_env=False)
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(
            READ_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
        ),
        follow_redirects=False,
        trust_env=False,
        **kwargs,
    )


PUSH_FAILED = (
    "The core could not hand the credential to {name} at {origin}: {reason}. "
    "Nothing was installed. Check the workstation is running and reachable, then "
    "get a fresh code and press Join again."
)


async def push_token(
    payload: EnrolmentPayload,
    *,
    code: str,
    token: str,
    client_factory: ClientFactory | None = None,
) -> None:
    """Hand the minted token to the Agent, over the Agent's own pinned TLS.

    The pairing code goes with it. That is not authentication of this core —
    nothing here can be — but it is the one secret the owner carried across the
    out-of-band channel of reading a screen and typing, so an Agent that gets a
    push quoting its own code knows the push came from the console the owner is
    standing at rather than from something else that answered.

    Raises :class:`EnrolmentRefused` on every failure, with the reason in it.
    Nothing has been written when this raises, which is why it runs before the
    writes rather than after them.
    """
    factory = client_factory or _pinned_client
    target = f"{payload.origin}{AGENT_TOKEN_PATH}"
    try:
        async with factory(payload.origin, payload.tls_fingerprint) as client:
            # Streamed rather than `client.post`, which buffers the whole reply
            # before this code sees any of it -- a cap applied after buffering
            # is a cap that has already been exceeded, and this address came
            # from an unauthenticated caller.
            async with client.stream(
                "POST",
                target,
                json={"code": code, "token": token},
            ) as response:
                status = response.status_code
                # **2xx only.** A 3xx is not a success and it is not followed
                # either (`follow_redirects` is off): treating one as a
                # delivered token would mean the Agent never received it while
                # the core went on to enrol the machine.
                if not (200 <= status < 300):
                    raise EnrolmentRefused(
                        502,
                        PUSH_FAILED.format(
                            name=payload.display_name,
                            origin=payload.origin,
                            reason=f"it answered {status}",
                        ),
                    )
                await _read_capped(response)
    except EnrolmentRefused:
        raise
    except PluginTransportError as exc:
        # The pin. Its message names both prints, which is the one line an
        # operator's incident turns on, so it is passed through unchanged.
        raise EnrolmentRefused(
            502,
            PUSH_FAILED.format(name=payload.display_name, origin=payload.origin, reason=str(exc)),
        ) from None
    except (httpx2.HTTPError, ssl.SSLError, OSError) as exc:
        raise EnrolmentRefused(
            502,
            PUSH_FAILED.format(
                name=payload.display_name,
                origin=payload.origin,
                reason=f"the connection failed ({type(exc).__name__})",
            ),
        ) from None

    logger.info(
        "workstation_token_pushed",
        plugin=payload.plugin_name,
        origin=payload.origin,
        status=status,
    )


async def _read_capped(response: httpx2.Response) -> None:
    """Drain at most :data:`MAX_PUSH_RESPONSE_BYTES` of the Agent's answer.

    Nothing in the answer is used for anything but the status code already
    checked, so there is no reason to accept a reply of unbounded size from an
    address a stranger chose. Drained rather than ignored so the connection
    closes cleanly on the ordinary path.
    """
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_PUSH_RESPONSE_BYTES:
            raise EnrolmentRefused(
                502,
                "The workstation sent back more than the core is willing to read. "
                "Nothing was installed.",
            )


# ---------------------------------------------------------------------------
# The whole call
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Enrolled:
    """What the Agent is told when it worked. Never the token, never the code."""

    plugin: str
    display_name: str
    state: str
    message: str


@dataclass
class EnrolmentService:
    """Everything the route needs, so the route itself is twenty lines.

    Every collaborator is passed in rather than built here. ``redeem`` in
    particular is a seam: the pairing store is a sibling module, and taking it
    as a callable is what lets this half be built, tested and reviewed without
    reaching into the other half's file.
    """

    layout: AppdataLayout
    secrets: SecretStore
    redeem: Callable[[str], bool]
    reload: Callable[[], Awaitable[Any]]
    set_enabled: Callable[[str, bool], Awaitable[None]] | None = None
    package_limits: PackageLimits = DEFAULT_PACKAGE_LIMITS
    client_factory: ClientFactory | None = None
    installed_names: Callable[[], Awaitable[Sequence[str]]] | None = None
    #: ``default_factory`` rather than ``default``, and it matters: a plain
    #: function as a dataclass ``default`` becomes a *class* attribute, and
    #: ``self.mint`` would then bind as a method and be called with ``self``.
    #: A factory puts it on the instance, where it stays a plain callable.
    mint: Callable[[], str] = field(default_factory=lambda: mint_token, repr=False)

    async def enrol(self, body: bytes) -> Enrolled:
        """Redeem, validate, derive, mint, push, persist, rescan, enable.

        The order is the design and is written out in the module docstring. The
        one thing worth repeating here is that the code is redeemed **first**,
        before a single field is looked at: a caller without a valid code must
        not be able to learn anything about what this core would accept, and
        validating first would answer that for free.
        """
        document = decode_body(body)
        code = read_code(document)
        if not await asyncio.to_thread(self.redeem, code):
            raise EnrolmentRefused(403, CODE_REFUSED)

        # Past this line the caller has proved they hold the code the owner is
        # looking at, so refusals may say what is actually wrong -- and every
        # one of them is marked as having got past the code, which is what tells
        # the route it is worth an audit row. Marked in one place rather than at
        # each `raise`, so a refusal added later cannot forget to.
        try:
            payload = read_payload(document)
            # The registration is built and validated **here**, not inside the
            # persist step where it is written. Everything this core will refuse
            # has to be refused before a credential is minted for it: leaving
            # the manifest's own validation until after the push handed a live
            # token to a machine that was then not enrolled, over a tool name.
            manifest = render_manifest(payload)
            await self._refuse_collision(payload)

            token = self.mint()
            await push_token(
                payload, code=code, token=token, client_factory=self.client_factory
            )
            await self._persist(payload, token, manifest)
        except EnrolmentRefused as refused:
            refused.redeemed = True
            raise

        state = await self._start(payload)
        return Enrolled(
            plugin=payload.plugin_name,
            display_name=payload.display_name,
            state=state,
            message=(
                f"{payload.display_name} joined as {payload.plugin_name} and is "
                "switched on."
            ),
        )

    async def _refuse_collision(self, payload: EnrolmentPayload) -> None:
        """Refuse a name already taken, before a token is minted for it.

        :func:`install_package` refuses this too, and that refusal is the one
        that actually guards the directory. This one exists so a collision is
        found *before* a credential is minted and pushed to a machine that is
        not going to be enrolled — the check is cheap and the alternative leaves
        a live token on somebody's desktop for nothing.
        """
        if self.installed_names is not None:
            names = await self.installed_names()
            if payload.plugin_name in set(names):
                raise EnrolmentRefused(409, _collision_message(payload))
        for root in (self.layout.plugins, self.layout.plugins_http):
            if (root / payload.plugin_name).exists():
                raise EnrolmentRefused(409, _collision_message(payload))

    async def _persist(self, payload: EnrolmentPayload, token: str, manifest: str) -> None:
        """Install the registration, then store the token. Undo both or neither.

        The push has already succeeded, so there is a working credential on the
        workstation either way; what must not survive a failure here is a
        half-enrolled core — a manifest folder with no token beside it, or a
        secret owned by a plugin that is not installed. Both are silent, and
        both make the next attempt fail for a reason that has nothing to do with
        the attempt.
        """
        package = build_package(payload, manifest)
        try:
            await asyncio.to_thread(
                install_package,
                self.layout,
                package,
                replace=False,
                limits=self.package_limits,
                secrets=self.secrets,
            )
        except PackageRejected as exc:
            raise EnrolmentRefused(409, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - the volume, not the caller
            logger.error(
                "workstation_enrol_install_failed",
                plugin=payload.plugin_name,
                error=repr(exc),
            )
            raise EnrolmentRefused(
                500,
                "The core could not write the workstation's registration. Nothing "
                "was installed. Check the appdata volume is mounted and writable.",
            ) from None

        try:
            await asyncio.to_thread(
                self.secrets.set, payload.secret_name, token, payload.plugin_name
            )
        except (SecretError, OSError) as exc:
            await self._undo(payload)
            logger.error(
                "workstation_enrol_secret_failed",
                plugin=payload.plugin_name,
                error=str(exc),
            )
            raise EnrolmentRefused(
                500,
                "The core installed the workstation's registration but could not "
                "store its credential, so it removed the registration again. "
                "Nothing is half-installed. Get a fresh code and press Join again.",
            ) from None

    async def _undo(self, payload: EnrolmentPayload) -> None:
        """Take back everything :meth:`_persist` had written. Never raises."""
        for step, action in (
            ("uninstall", lambda: uninstall_package(self.layout, payload.plugin_name)),
            ("secrets", lambda: self.secrets.delete_namespace(payload.plugin_name)),
        ):
            try:
                await asyncio.to_thread(action)
            except Exception as exc:  # noqa: BLE001 - already failing; say so and move on
                logger.error(
                    "workstation_enrol_rollback_failed",
                    plugin=payload.plugin_name,
                    step=step,
                    error=repr(exc),
                )

    async def _start(self, payload: EnrolmentPayload) -> str:
        """Rescan and switch it on, through the paths the admin surface uses.

        A failure to *start* is not a failure to enrol: the registration and the
        credential are both on disk, the row exists, and the supervisor will
        pick it up at the next reload or restart. Reporting the state honestly —
        ``unknown`` rather than ``ok`` — is the whole reason the health enum has
        three values.
        """
        # The recorded choice first, the running core second, the listing third
        # -- the same order `admin/api_plugins.py` toggles a plugin in, so a
        # crash between the halves leaves a core that comes back switched on.
        # A stale entry in the disabled list can outlive the plugin that put it
        # there, and without this a freshly enrolled workstation would install
        # correctly and never start.
        try:
            await asyncio.to_thread(
                set_plugin_enabled, self.layout, payload.plugin_name, enabled=True
            )
        except Exception as exc:  # noqa: BLE001 - it is installed either way
            logger.error(
                "workstation_enrol_enable_failed",
                plugin=payload.plugin_name,
                error=repr(exc),
            )
        if self.set_enabled is not None:
            try:
                await self.set_enabled(payload.plugin_name, True)
            except Exception as exc:  # noqa: BLE001 - one plugin, spec section 5.1
                logger.error(
                    "workstation_enrol_start_failed",
                    plugin=payload.plugin_name,
                    error=repr(exc),
                )
        try:
            listing = await self.reload()
        except Exception as exc:  # noqa: BLE001 - it is installed either way
            logger.error(
                "workstation_enrol_reload_failed",
                plugin=payload.plugin_name,
                error=repr(exc),
            )
            return "unknown"
        for view in getattr(listing, "plugins", ()):
            if getattr(view, "name", None) == payload.plugin_name:
                return str(getattr(view, "state", "unknown"))
        return "unknown"


def mint_token() -> str:
    """A new bearer token for one workstation. The only place one is created."""
    return secrets_module.token_urlsafe(TOKEN_BYTES)


def _collision_message(payload: EnrolmentPayload) -> str:
    return (
        f"A workstation is already enrolled as {payload.plugin_name!r}, so "
        f"{payload.display_name} cannot join under that name. Remove the old one "
        "from the Plugins screen first, or rename this machine and try again."
    )


__all__ = [
    "ACCEPTED_RISK",
    "AGENT_TOKEN_PATH",
    "CODE_REFUSED",
    "CREDENTIAL_IN_REQUEST",
    "ENROL_PATH",
    "MAX_BODY_BYTES",
    "PLUGIN_NAME_PREFIX",
    "RATE_LIMITED",
    "REQUEST_FIELDS",
    "Enrolled",
    "EnrolmentPayload",
    "EnrolmentRefused",
    "EnrolmentService",
    "auth_secret_name",
    "build_manifest_document",
    "build_package",
    "decode_body",
    "derive_plugin_name",
    "mint_token",
    "normalise_display_name",
    "normalise_fingerprint",
    "push_token",
    "read_code",
    "read_payload",
    "render_manifest",
    "risk_refused_message",
    "validate_tools",
    "validate_url",
    "validate_version",
]
