"""Enrolling a workstation from a pairing code — the one unauthenticated call.

What the owner sees is in ``docs/wiki``: a code on the Plugins screen, a Join
button on the workstation, and a named machine on the workstation plugin's own
settings screen a moment later. What this module is responsible for is
everything between those two, and the property it exists to hold is this one:

**The workstation's bearer token never travels over the plaintext wire.**

The core's own surface is plain HTTP on a household LAN. The enrolment request
therefore carries no credential at all — a pairing code, addresses, certificate
fingerprints and a tool list, nothing more. The core *mints* the token and
pushes it back to the workstation over the workstation's own TLS, pinned to the
fingerprint the request just supplied, with the pairing code alongside it so the
Agent can tell the push came from the console its owner is standing at rather
than from a stranger. Only if that push succeeds is anything written to disk.

The residual risk is stated where it belongs, in the ADR: an attacker already
present on the LAN and actively interfering can substitute their own address and
fingerprint and receive a token, enrolling a counterfeit workstation. They gain
nothing on the real one, and the real one visibly fails to join. Passive
observation of the wire yields nothing usable. That was weighed and accepted.

**A machine is not a plugin.** Every enrolled machine is a row inside the one
``workstation`` plugin — owner decision, 2026-09-09 — so this module derives no
plugin name, mints no per-machine plugin and builds no package per enrolment.
:mod:`personacore.enrolment.registry` owns where a machine lives and how it
dies; enrolment adds one to it. What this module still owns is the manifest that
tells the plugin host how to *reach* those machines, and that manifest's
endpoint set is derived from the registry rather than kept beside it.

**Order is the design.** Redeem, validate, mint, push, *then* persist.
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
import os
import re
import secrets as secrets_module
import ssl
import tomllib
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx2
import tomli_w

from personacore.audit import get_logger
from personacore.config.appdata import AppdataLayout
from personacore.config.secrets import SecretStore
from personacore.contracts.manifest import PluginManifest, RiskLevel
from personacore.enrolment.registry import (
    MAX_ADDRESSES,
    PLUGIN_NAME,
    Machine,
    MachineAddress,
    MachineRegistry,
    MachineRejected,
)
from personacore.plugins.mcp_client import PluginTransportError, build_pinned_client_kwargs
from personacore.plugins.packages import (
    DEFAULT_PACKAGE_LIMITS,
    PackageLimits,
    PackageRejected,
    clear_switched_off_when_empty,
    install_package,
    mark_switched_off_when_empty,
    read_disabled_plugins,
    set_plugin_enabled,
    switched_off_when_empty,
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

MANIFEST_FILENAME = "manifest.toml"
"""The plugin document this module writes, beside the registry's own file."""

WORKSTATION_DESCRIPTION = (
    "Workstations enrolled with this core. Each one acts on that machine and "
    "the devices plugged into it. Add and remove them on this page."
)
"""The one plugin's description. **It names no machine.**

The description is rendered in the plugin list, and machines do not appear there
(reshape plan decision 0.1) — they are rows on this plugin's own settings page.
A description that grew a machine's name would put one in the single place the
owner said they do not go.
"""

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

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        redeemed: bool = False,
        reason: str | None = None,
    ) -> None:
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

        self.reason = reason
        """A short stable code for the log line and the audit row, or ``None``.

        **The message is for the caller; this is for the store.** A refusal
        sentence is free text and may name an already-enrolled machine — the
        collision refusal has to, to be worth reading — and both the log file
        and the audit store are core state that outlives the plugin. So the
        sentence is answered over the wire and a code is what is recorded.

        ``None`` means this refusal predates the registry's vocabulary and the
        route falls back to deriving one from the status
        (:func:`personacore.admin.api_enrol._refusal_code`). Every refusal
        raised from a :class:`~personacore.enrolment.registry.MachineRejected`
        carries the registry's own code instead, which is the more precise one.
        """


def _refused_from(exc: MachineRejected) -> EnrolmentRefused:
    """One registry refusal, as an enrolment refusal, with both halves kept.

    The sentence and the code travel together the whole way: the registry
    decides what to say and what to record, this carries both to the route, and
    the route answers with one and stores the other. Rewriting the sentence here
    would put a second author on a message the registry has already written for
    the person who hit it.
    """
    return EnrolmentRefused(exc.status_code, exc.message, reason=exc.reason)


# ---------------------------------------------------------------------------
# Validation — everything here arrives from an unauthenticated caller
# ---------------------------------------------------------------------------

_DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
"""What a machine may call itself.

A hostname, in other words, with room for a space. Deliberately narrow rather
than escaped-on-render: this string ends up in an audit-free registry row, a
refusal sentence and a settings page, and the cheapest way to be sure it is
harmless in all three is for it never to contain anything that needs thinking
about. A name outside this set is refused with a sentence saying so, not
silently mangled.
"""

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_FINGERPRINT_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


def normalise_display_name(raw: object) -> str:
    """The name the owner is shown, normalised rather than applied silently.

    Windows hostnames are commonly uppercase and may contain underscores, so
    what a machine calls itself and what reads well in a list are not the same
    string. The normalisation here is only whitespace — case is left alone,
    because the uppercase form is what is printed on the machine and quietly
    lowercasing it makes the row harder to recognise, not easier.

    This is the single answer to what a workstation may be called:
    :func:`personacore.enrolment.registry.normalise_name` imports it rather than
    restating the rule.
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
            "This machine offers no tools, so enrolling it would add a workstation "
            "that can do nothing.",
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

    addresses: tuple[tuple[str, str], ...]
    """The machine's whole listening set as ``(url, tls_fingerprint)`` pairs, in
    the machine's own order with the preferred address first.

    **N addresses, not two slots** (reshape plan decision 0.4). Windows hands a
    machine an IPv4 and an IPv6 address per interface and the owner may say
    which machine he means by either, so the set is the record and there is no
    "the" address.
    """

    push_origin: str
    """The origin the minted token is pushed to — the preferred address's."""

    push_fingerprint: str
    """The pin that push is made against. The preferred address's own, because
    each address carries its own certificate."""

    agent_version: str
    contract_version: str
    tools: dict[str, dict[str, str]]

    hosts: tuple[str, ...]
    """Every host in :attr:`addresses`, for ``permissions.network``."""


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


ENTRY_FIELDS = frozenset({"url", "tls_fingerprint"})
"""Every field one ``urls`` entry may carry.

The same two words the singular form uses, deliberately: an Agent author who has
written ``url`` and ``tls_fingerprint`` at the top level should not have to learn
that an entry spells the pin differently. The manifest calls it ``pin``; that is
the core's own document and the translation happens here, once.
"""

FINGERPRINT_WITHOUT_ADDRESS = (
    "This machine sent a certificate fingerprint with no address to go with it. "
    "Every address carries its own fingerprint: send 'url' and "
    "'tls_fingerprint' together, or list them as entries in 'urls'."
)

NO_ADDRESS = (
    "This machine did not send an address to reach it on. Send its https "
    "address as 'url' with the fingerprint of the certificate it serves there, "
    "and any further addresses it listens on as entries in 'urls'."
)


def read_addresses(document: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The machine's listening set, validated, de-duplicated, order preserved.

    ``url`` and ``tls_fingerprint`` are the **preferred** address and stay first:
    it is the one the token is pushed to, and an Agent that has one address sends
    exactly what it always sent. ``urls`` carries the rest, each entry complete —
    a whole URL and its own pin, never a bare host and a port to be reassembled.
    Rebuilding an address from parts is where IPv6 bracket notation goes wrong,
    and the machine already knows how to spell its own address.

    Exact repeats are dropped rather than refused. A machine listing its
    preferred address again inside ``urls`` is being thorough, not wrong, and
    two records for one address would be two connections to one machine.
    """
    entries: list[tuple[object, object]] = []
    raw_url = document.get("url")
    raw_pin = document.get("tls_fingerprint")
    if raw_url is not None:
        entries.append((raw_url, raw_pin))
    elif raw_pin is not None:
        raise EnrolmentRefused(400, FINGERPRINT_WITHOUT_ADDRESS)

    listed = document.get("urls")
    if listed is not None:
        if not isinstance(listed, list):
            raise EnrolmentRefused(
                400,
                "'urls' must be a list of addresses, each one an object with a "
                "'url' and the 'tls_fingerprint' of the certificate served there.",
            )
        for entry in listed:
            if not isinstance(entry, Mapping):
                raise EnrolmentRefused(
                    400,
                    "Each entry in 'urls' must be an object with a 'url' and a "
                    "'tls_fingerprint'.",
                )
            unknown = sorted(str(key) for key in entry if key not in ENTRY_FIELDS)
            if unknown:
                raise EnrolmentRefused(
                    400,
                    f"An entry in 'urls' carries fields the core does not take: "
                    f"{', '.join(repr(key) for key in unknown[:5])}. An entry takes "
                    f"{', '.join(sorted(ENTRY_FIELDS))} and nothing else.",
                )
            entries.append((entry.get("url"), entry.get("tls_fingerprint")))

    if not entries:
        raise EnrolmentRefused(400, NO_ADDRESS)

    resolved: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_entry_url, raw_entry_pin in entries:
        url, _origin = validate_url(raw_entry_url)
        pin = normalise_fingerprint(raw_entry_pin)
        if url in seen:
            continue
        seen.add(url)
        resolved.append((url, pin))

    if len(resolved) > MAX_ADDRESSES:
        # Refused here as well as in the registry, and for the reason every
        # other check runs before the mint: a machine that is going to be turned
        # away must not first be handed a live credential.
        raise EnrolmentRefused(
            409,
            f"This machine sent {len(resolved)} addresses, and this core keeps at "
            f"most {MAX_ADDRESSES} for one workstation. Have it listen on fewer, "
            "or send the ones it is actually reached at.",
            reason="too_many_addresses",
        )
    return tuple(resolved)


def read_payload(document: object) -> EnrolmentPayload:
    """Everything but the code, checked and normalised.

    Only ever called once the code has been redeemed, so its refusals may say
    what is actually wrong (spec section 9) without telling a stranger anything.
    """
    if not isinstance(document, Mapping):
        raise EnrolmentRefused(400, BODY_REFUSED)
    _refuse_unknown_fields(document)

    display_name = normalise_display_name(document.get("display_name"))
    addresses = read_addresses(document)
    preferred_url, preferred_pin = addresses[0]
    _url, origin = validate_url(preferred_url)
    return EnrolmentPayload(
        display_name=display_name,
        addresses=addresses,
        push_origin=origin,
        push_fingerprint=preferred_pin,
        agent_version=validate_version(document.get("agent_version"), what="The Agent version"),
        contract_version=validate_version(
            document.get("contract_version"), what="The contract version"
        ),
        tools=validate_tools(document.get("tools")),
        hosts=tuple(
            dict.fromkeys(urlparse(url).hostname or "" for url, _pin in addresses)
        ),
    )


REQUEST_FIELDS = frozenset(
    {
        "code",
        "display_name",
        "url",
        "urls",
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
    "Send the pairing code, the machine's name, its https addresses, their "
    "certificate fingerprints, its version and its tools, and nothing else."
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
# The manifest this writes — one plugin, N endpoints
# ---------------------------------------------------------------------------


def build_manifest_document(
    machines: Sequence[Machine],
    *,
    version: str,
    contract: str,
    tools: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """The ``workstation`` plugin's registration, **derived from the registry**.

    One plugin, one entry in ``plugin.urls`` per address of every enrolled
    machine — ADR-0048's additive endpoint set, which each machine's record maps
    onto with nothing to translate: the address, the pin for the certificate
    served there, and the *name* of the token that machine will accept.

    The registry is the source of truth and this document is derived from it, so
    the two cannot drift: rewriting it is how a machine that was added or
    removed becomes a connection that is opened or closed.

    ``plugin.url`` is **not** declared. An http plugin reaches its addresses
    through ``url`` or through ``urls``, and declaring both here would name an
    address the endpoint-set supervisor never dials — a row in the manifest that
    nothing connects to. ``plugin.auth_secret`` is not declared either: there is
    no credential shared across these machines, because each one issues its own.
    """
    endpoints: list[dict[str, str]] = []
    hosts: list[str] = []
    for machine in machines:
        for address in machine.addresses:
            entry = {"url": address.url, "pin": address.tls_fingerprint}
            if machine.token_secret:
                # Omitted rather than blank when there is none. A blank
                # ``auth_secret`` is refused by the contract, correctly — it is
                # the *name* of a secret and an empty name is a defect. A
                # machine the registry has not given an id to yet has no name
                # for its token, and an entry without the field is the honest
                # spelling of "no bearer token here"; the real one is written a
                # moment later by ``EnrolmentService._publish``.
                entry["auth_secret"] = machine.token_secret
            endpoints.append(entry)
            host = urlparse(address.url).hostname
            if host:
                hosts.append(host)
    return {
        "plugin": {
            "name": PLUGIN_NAME,
            "version": version,
            "contract": contract,
            "transport": "http",
            "urls": endpoints,
            "description": WORKSTATION_DESCRIPTION,
        },
        # A declaration rather than a gate (ADR-0012), written down because a
        # plugin's own page shows what the manifest asked for.
        "permissions": {
            "network": sorted(dict.fromkeys(hosts)),
            "secrets": [],
            "paths": [],
        },
        "tools": {name: dict(tool) for name, tool in tools.items()},
        "events": {"publishes": [], "subscribes": []},
    }


def render_manifest(document: Mapping[str, Any]) -> str:
    """Validate the document, then serialise it with ``tomli_w``.

    Never string-formatted by hand. The contract's own section 2 carries a
    correction because an earlier hand-written form put ``[tools.x]`` on one
    line as an inline table, which is not valid TOML in that position; a writer
    cannot make that mistake, and it cannot be made to emit a value a caller
    smuggled quotes or newlines into either.
    """
    try:
        PluginManifest.model_validate(dict(document))
    except Exception as exc:  # noqa: BLE001 - pydantic's own message is the useful one
        raise EnrolmentRefused(
            400,
            "The details this machine sent do not make a valid plugin registration: "
            f"{_first_problem(exc)}",
        ) from None
    return _MANIFEST_HEADER + tomli_w.dumps(dict(document))


_MANIFEST_HEADER = (
    "# The workstation plugin. Written by the core — every address below is an\n"
    "# enrolled machine, and machines are added and removed on this plugin's\n"
    "# own settings screen rather than by editing this file.\n"
    "#\n"
    "# auth_secret is the NAME a machine's token is stored under. The value is\n"
    "# in the secret store, was minted by this core, and is never in this file.\n\n"
)


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


def _bootstrap_package(manifest: str) -> bytes:
    """The ``workstation`` folder, as bytes ``install_package`` accepts.

    **Once, for the first machine that joins** — not once per enrolment. There
    is one workstation plugin and every machine after the first is a row inside
    the folder this creates.

    An archive built in memory and handed straight to the installer, rather than
    a folder written into ``plugins-http/`` from here. That is the point of the
    function: :func:`install_package` stages inside appdata, validates with the
    real scanner *before* anything moves into place, checks the name against the
    manifest's rule on the line that joins it to a directory, refuses a
    collision, and clears an orphaned secret namespace left behind by a previous
    plugin of the same name. Writing the folder here would be a second opinion
    about which directory is safe to write, which is exactly what spec section 7
    wants none of.

    The zip is not a *format* this feature has: nothing is uploaded, exported or
    kept. It is the argument type the one safe installer takes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{PLUGIN_NAME}/{MANIFEST_FILENAME}", manifest)
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
    "Nothing was enrolled. Check the workstation is running and reachable, then "
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

    **One address, not the set.** The push goes to the machine's preferred
    address, because it is one machine and one token: succeeding on any of its
    addresses means the machine has the credential, and trying the rest would
    hand the same token to whatever else answered at an address that turned out
    not to be it.

    Raises :class:`EnrolmentRefused` on every failure, with the reason in it.
    Nothing has been written when this raises, which is why it runs before the
    writes rather than after them.
    """
    factory = client_factory or _pinned_client
    target = f"{payload.push_origin}{AGENT_TOKEN_PATH}"
    try:
        async with factory(payload.push_origin, payload.push_fingerprint) as client:
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
                            origin=payload.push_origin,
                            reason=f"it answered {status}",
                        ),
                        reason="machine_unreachable",
                    )
                await _read_capped(response)
    except EnrolmentRefused:
        raise
    except PluginTransportError as exc:
        # The pin. Its message names both prints, which is the one line an
        # operator's incident turns on, so it is passed through unchanged.
        raise EnrolmentRefused(
            502,
            PUSH_FAILED.format(
                name=payload.display_name, origin=payload.push_origin, reason=str(exc)
            ),
            reason="machine_unreachable",
        ) from None
    except (httpx2.HTTPError, ssl.SSLError, OSError) as exc:
        raise EnrolmentRefused(
            502,
            PUSH_FAILED.format(
                name=payload.display_name,
                origin=payload.push_origin,
                reason=f"the connection failed ({type(exc).__name__})",
            ),
            reason="machine_unreachable",
        ) from None

    logger.info(
        "workstation_token_pushed",
        plugin=PLUGIN_NAME,
        origin=payload.push_origin,
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
                "Nothing was enrolled.",
                reason="machine_unreachable",
            )


# ---------------------------------------------------------------------------
# The whole call
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Enrolled:
    """What the Agent is told when it worked. Never the token, never the code."""

    plugin: str
    """Always ``workstation``. A machine joins that plugin; it never becomes
    one. Kept in the answer because the Agent's own log says where it landed,
    and because an Agent built against the per-machine naming rule that was
    withdrawn on 2026-09-09 reads this field and finds the constant."""

    display_name: str
    state: str
    message: str


TOOLS_DIFFER = (
    "{name} offers a different set of tools than the workstations already "
    "enrolled on this core, so it was not added. Every machine in the "
    "workstation plugin has to offer the same tools: the core holds one tool "
    "list for the plugin and checks each machine against it, and a machine that "
    "does not match would join and then refuse everything. {difference} Update "
    "the Agent on whichever machine is behind, then press Join again."
)

CONTRACT_DIFFERS = (
    "{name} speaks contract version {theirs}, and the workstations already "
    "enrolled on this core speak {ours}, so it was not added. The core holds "
    "one contract version for the workstation plugin. Update the Agent on "
    "whichever machine is behind, then press Join again."
)

PLUGIN_NOT_OURS = (
    "There is already a plugin called 'workstation' on this core that this core "
    "did not create, so no machine was added to it. Remove that plugin from the "
    "Plugins screen, then press Join again."
)

PLUGIN_WRITE_FAILED = (
    "The core enrolled {name} and then could not write the workstation plugin's "
    "registration, so it removed the machine again. Nothing is half-enrolled. "
    "Check the appdata volume is mounted and writable, then get a fresh code and "
    "press Join again."
)

SWITCHED_OFF = (
    "{name} joined this core. The workstation plugin is switched off, so nothing "
    "is connected to it yet — switch it on from the Plugins screen."
)

JOINED = "{name} joined this core as a workstation."


@dataclass
class EnrolmentService:
    """Everything the route needs, so the route itself is twenty lines.

    Every collaborator is passed in rather than built here. ``redeem`` and
    ``claim`` in particular are seams: the pairing store is a sibling module,
    and taking its two calls as callables is what lets this half be built,
    tested and reviewed without reaching into the other half's file.
    """

    layout: AppdataLayout
    secrets: SecretStore
    redeem: Callable[[str], bool]
    reload: Callable[[], Awaitable[Any]]
    set_enabled: Callable[[str, bool], Awaitable[None]] | None = None
    package_limits: PackageLimits = DEFAULT_PACKAGE_LIMITS
    client_factory: ClientFactory | None = None
    #: ``default_factory`` rather than ``default``, and it matters: a plain
    #: function as a dataclass ``default`` becomes a *class* attribute, and
    #: ``self.mint`` would then bind as a method and be called with ``self``.
    #: A factory puts it on the instance, where it stays a plain callable.
    mint: Callable[[], str] = field(default_factory=lambda: mint_token, repr=False)
    claim: Callable[[str], None] = field(default_factory=lambda: _default_claim, repr=False)
    """Tell the pairing screen which machine redeemed the live code.

    Memory-only and bounded by the code's own TTL, which is the whole reason it
    is allowed to hold a machine's name at all: it is the one place a name
    enters core state, and it leaves again when the code expires.
    """

    refuse: Callable[[str | None, str], None] = field(
        default_factory=lambda: _default_refuse, repr=False
    )
    """Tell the pairing screen that a redeemed code did not become a machine.

    The same seam as :attr:`claim` and the same store, for the other outcome.
    Without it a refusal past the code is invisible on the admin side — the
    refusal sentence goes back over the wire to the Agent, and the screen is
    left holding a spent code with no name, which is indistinguishable from one
    redeemed a split second ago.
    """

    registry_factory: Callable[[], MachineRegistry] | None = None

    def registry(self) -> MachineRegistry:
        """The machine registry for the one ``workstation`` plugin."""
        if self.registry_factory is not None:
            return self.registry_factory()
        return MachineRegistry(layout=self.layout, secrets=self.secrets)

    async def enrol(self, body: bytes) -> Enrolled:
        """Redeem, validate, mint, push, add, publish.

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
        # The name, as soon as there is one, so a refusal past this point can
        # tell the pairing screen *which* machine was turned away. `None` until
        # the body has been read: a request refused before it said what it is
        # called leaves the dialog a refusal with no name, which is honest.
        display_name: str | None = None
        try:
            payload = read_payload(document)
            display_name = payload.display_name
            registry = self.registry()
            # Everything this core will refuse is refused **before** a
            # credential is minted for it. The registry checks the collision
            # again under its own lock, which is the check that actually guards
            # the file; this one exists so a machine that is going to be turned
            # away is never first handed a live token.
            await asyncio.to_thread(self._refuse_mismatch, registry, payload)

            token = self.mint()
            await push_token(
                payload, code=code, token=token, client_factory=self.client_factory
            )
            added = await asyncio.to_thread(self._add, registry, payload, token)
        except EnrolmentRefused as refused:
            refused.redeemed = True
            # The code was burnt and no machine joined. Told to the pairing
            # screen so the dialog can close saying so, rather than sitting on
            # a spent code that looks exactly like one being redeemed right
            # now. The *code* for the store, never the sentence — the sentence
            # is already going back to the Agent that caused it.
            self._refuse(display_name, refused.reason or GENERIC_REFUSAL)
            raise

        # The name reaches the pairing screen only once the machine is really
        # enrolled, so the screen can never name a machine that did not join.
        self._claim(payload.display_name)
        state = await self._start(
            created=added.created_plugin, reawakened=added.reawakened
        )
        return Enrolled(
            plugin=PLUGIN_NAME,
            display_name=payload.display_name,
            state=state,
            message=(
                JOINED.format(name=payload.display_name)
                if state != "disabled"
                else SWITCHED_OFF.format(name=payload.display_name)
            ),
        )

    # -- removing one, which is the mirror of adding one -------------------

    async def remove_machine(self, machine_id: str) -> MachineRemoved:
        """Forget one machine **and** rewrite the endpoint set. One operation.

        The public counterpart of :meth:`enrol`, and it exists because the two
        halves must not be reachable separately. Calling
        :meth:`~personacore.enrolment.registry.MachineRegistry.remove` on its
        own takes the row and the token out and leaves ``manifest.toml`` still
        declaring that machine's addresses: the plugin host goes on dialling a
        machine the owner deleted and the plugin row goes on counting it, with
        nothing on any screen to say why.

        Args:
            machine_id: the opaque id from
                :attr:`~personacore.enrolment.registry.Machine.id`, not the
                machine's name. A name is the owner's to change; the id is what
                the token is stored under.

        Returns:
            :class:`MachineRemoved` — whether there was a machine of that id,
            how many are left, and whether the endpoint set is now stale.

        Raises:
            EnrolmentRefused: the plugin is not installed, is not this core's,
                or the endpoint set could not be written. The machine is
                already out of the registry when the write fails, so the
                refusal says the list is short of one rewrite rather than
                pretending nothing happened.

        **The last machine switches the plugin off and leaves it in place** —
        owner decision, 2026-09-09. Not uninstalled: the folder, the settings
        and the row on the Plugins screen all survive, and the next machine to
        join starts it again on its own. Two writes, in the order every other
        toggle in this core uses — the recorded choice first, the running host
        second — so a crash between them leaves a core that comes back off
        rather than one running a plugin it has nothing for.

        **manifest.toml is left exactly as it was**, and that is a decision
        rather than an omission. Checked against the contract rather than
        assumed: an http plugin is refused at load unless it declares ``url``
        or a **non-empty** ``urls`` — see
        :meth:`~personacore.contracts.manifest.PluginManifest` and
        :data:`~personacore.contracts.manifest.ENDPOINT_SET_MUST_NOT_BE_EMPTY`
        — so there is no document that means "no machines", and writing one
        would leave the owner a plugin that will not load. That is the outcome
        he did not pick. The last registration that was true therefore stands,
        untouched, and :meth:`_publish` replaces it in full when a machine
        joins. Nothing dials it in the meantime and nothing can: the plugin is
        off, and each entry's bearer token went with the machine it belonged
        to, so the client fails closed before a request is built.
        """
        result = await asyncio.to_thread(self._remove, machine_id)
        if result.switched_off and self.set_enabled is not None:
            try:
                await self.set_enabled(PLUGIN_NAME, False)
            except Exception as exc:  # noqa: BLE001 - the record is what persists
                logger.error(
                    "workstation_stop_failed", plugin=PLUGIN_NAME, error=repr(exc)
                )
        return result

    def _remove(self, machine_id: str) -> MachineRemoved:
        """The blocking half of :meth:`remove_machine`, on a worker thread."""
        self._refuse_a_plugin_that_is_not_ours()
        registry = self.registry()
        try:
            removed = registry.remove(machine_id)
            left = registry.list()
        except MachineRejected as exc:
            raise _refused_from(exc) from None

        if not removed:
            # No such machine, so nothing changed and the endpoint set is
            # already right. Reported rather than raised: a screen removing a
            # row twice is a double click, not an error worth a red box.
            return MachineRemoved(
                removed=False, machines_left=len(left), switched_off=False
            )
        if not left:
            self._park()
            return MachineRemoved(removed=True, machines_left=0, switched_off=True)
        self._publish(registry)
        return MachineRemoved(
            removed=True, machines_left=len(left), switched_off=False
        )

    def _park(self) -> None:
        """Switch the plugin off because it has nothing left, and record who did it.

        The marker goes down **before** the switch. A crash between the two then
        leaves a plugin that is still on and merely marked, which the next
        enrolment clears harmlessly — rather than one that is off with nothing
        saying the core is what turned it off. That second state is the trap:
        it is indistinguishable from a plugin the owner switched off himself,
        and enrolment would then correctly refuse to turn it back on, leaving a
        machine that joined sitting behind a plugin nobody knowingly disabled.
        """
        directory = self._directory()
        if directory is not None:
            mark_switched_off_when_empty(directory)
        try:
            set_plugin_enabled(self.layout, PLUGIN_NAME, enabled=False)
        except Exception as exc:  # noqa: BLE001 - the machine is out either way
            logger.error("workstation_park_failed", plugin=PLUGIN_NAME, error=repr(exc))

    # -- the checks that run before a token exists -------------------------

    def _refuse_mismatch(
        self, registry: MachineRegistry, payload: EnrolmentPayload
    ) -> None:
        """Refuse a machine this core cannot hold, before it is minted a token.

        Three refusals, and each one is a thing the core would otherwise
        discover after the machine was already carrying a credential:

        * **a name or an address already taken** — the registry's rule, checked
          here early and enforced there authoritatively;
        * **a different tool list** — the plugin holds one tool table and
          :func:`personacore.plugins.supervisor.reconcile_tools` checks every
          endpoint against it in both directions, so a machine offering a
          different set would connect and then be refused for not matching its
          own manifest;
        * **a different contract version** — the plugin holds one, for the same
          reason.

        The last two are refusals this core did not need while a machine was its
        own plugin with its own manifest. They are the honest cost of one plugin
        holding N machines, and they are made at the door rather than at the
        connection, because a machine that joins and then never runs is a
        failure landing a long way from its cause.
        """
        # Ownership first. Everything below this line reads the installed
        # manifest as though this core wrote it, and a plugin of that name it
        # did not write is neither a comparison to make nor a document to edit.
        self._refuse_a_plugin_that_is_not_ours()
        try:
            existing = registry.list()
        except MachineRejected as exc:
            if exc.reason == "not_installed":
                # Nothing is enrolled because there is no plugin yet. That is
                # the ordinary first Join, not a refusal.
                return
            raise _refused_from(exc) from None

        clash = registry.find(payload.display_name)
        if clash is not None:
            raise EnrolmentRefused(
                409,
                f"A workstation called {clash.name!r} is already on this core, so "
                f"{payload.display_name!r} cannot join under that name. Remove the "
                "one that is there, or rename this machine and try again.",
                reason="name_taken",
            )
        taken = {
            address.url for machine in existing for address in machine.addresses
        }
        for url, _pin in payload.addresses:
            if url in taken:
                raise EnrolmentRefused(
                    409,
                    "Another workstation on this core is already reached at that "
                    "address, so this one did not join. Remove the one that is "
                    "there first, or give this machine an address of its own.",
                    reason="address_taken",
                )

        installed = self._read_manifest()
        if installed is None:
            return
        plugin = installed.get("plugin")
        declared = installed.get("tools")
        if isinstance(declared, Mapping):
            theirs = set(payload.tools)
            ours = set(declared)
            if theirs != ours:
                raise EnrolmentRefused(
                    409,
                    TOOLS_DIFFER.format(
                        name=payload.display_name,
                        difference=_tool_difference(ours, theirs),
                    ),
                    reason="tools_differ",
                )
        if isinstance(plugin, Mapping):
            contract = plugin.get("contract")
            if isinstance(contract, str) and contract != payload.contract_version:
                raise EnrolmentRefused(
                    409,
                    CONTRACT_DIFFERS.format(
                        name=payload.display_name,
                        theirs=payload.contract_version,
                        ours=contract,
                    ),
                    reason="contract_differs",
                )

    # -- the writes --------------------------------------------------------

    def _add(
        self, registry: MachineRegistry, payload: EnrolmentPayload, token: str
    ) -> _Added:
        """Add the machine, then rewrite the plugin's endpoint set from the registry.

        Runs on a worker thread: every call under it is blocking file work, and
        doing it in one hop keeps the registry's own lock covering the whole
        read-modify-write rather than being taken and released around an await.
        """
        created = self._ensure_plugin(payload)
        # Read before anything is written, because the answer stops being true
        # the moment this machine lands: "the core parked it" and "it has no
        # machines" are the same thing right up until one arrives, and inferring
        # it afterwards is the trap the marker exists to close.
        directory = self._directory()
        parked = directory is not None and switched_off_when_empty(directory)
        try:
            machine = registry.add(
                name=payload.display_name,
                addresses=payload.addresses,
                token=token,
            )
        except MachineRejected as exc:
            if created:
                self._undo_plugin()
            raise _refused_from(exc) from None

        try:
            self._publish(registry, version=payload.agent_version)
        except EnrolmentRefused:
            # The endpoint set is how a machine is *reached*. A record the host
            # will never dial is a machine that looks enrolled and does nothing,
            # so it goes back out rather than being left on the screen.
            registry.remove(machine.id)
            if created:
                self._undo_plugin()
            raise EnrolmentRefused(
                500,
                PLUGIN_WRITE_FAILED.format(name=payload.display_name),
                reason="storage",
            ) from None
        if parked and directory is not None:
            # It has something to do again, so the core's own note about why it
            # parked this plugin has stopped being true. Cleared on the path
            # that already succeeded rather than beside the switch-on: the
            # marker describes what the plugin holds, not whether a later step
            # worked.
            clear_switched_off_when_empty(directory)
        return _Added(machine=machine, created_plugin=created, reawakened=parked)

    def _publish(
        self, registry: MachineRegistry, *, version: str | None = None
    ) -> None:
        """Write ``manifest.toml`` from the registry. The endpoint set, derived.

        Called after **every** change to the machine list — :meth:`_add` and
        :meth:`remove_machine` both end here. The registry is the source of
        truth; this document is how the plugin host learns to open a connection
        per machine, and rewriting it is the whole of "the plugin's endpoints
        follow the registry". A change that skipped it would leave the core
        dialling a machine the owner deleted and counting it on the plugin row.

        ``version`` is the joining Agent's when a machine is joining, and the
        installed document's otherwise — a removal does not change what version
        the plugin is. ``contract`` and ``tools`` are carried over from the
        installed document, which is the only place they live once the request
        that supplied them is gone — and every machine agrees on both, because
        :meth:`_refuse_mismatch` refuses one that does not.

        **Never called with nothing left.** An http plugin declaring an empty
        endpoint set is refused at load (``ENDPOINT_SET_MUST_NOT_BE_EMPTY``), so
        there is no document that means "no machines" and this method has none
        to write. :meth:`remove_machine` is what notices, and it reports the
        fact rather than deciding what to do about it.
        """
        machines = registry.list()
        if not machines:
            raise EnrolmentRefused(
                500,
                "The workstation plugin has no machines left, so there is no "
                "registration to write for it.",
                reason="no_address",
            )
        installed = self._read_manifest() or {}
        plugin = installed.get("plugin")
        plugin = plugin if isinstance(plugin, Mapping) else {}
        tools = installed.get("tools")
        tools = tools if isinstance(tools, Mapping) else {}
        document = build_manifest_document(
            machines,
            version=version or str(plugin.get("version") or "0.0.0"),
            contract=str(plugin.get("contract") or ""),
            tools=tools,
        )
        text = render_manifest(document)
        self._write_manifest(text)

    # -- the plugin folder -------------------------------------------------

    def _directory(self) -> Path | None:
        """Where the ``workstation`` plugin is installed, or ``None``."""
        for root in (self.layout.plugins, self.layout.plugins_http):
            candidate = root / PLUGIN_NAME
            if candidate.is_dir():
                return candidate
        return None

    def _read_manifest(self) -> dict[str, Any] | None:
        """The installed plugin document, or ``None`` if there is not one."""
        directory = self._directory()
        if directory is None:
            return None
        try:
            return tomllib.loads(
                (directory / MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            return None

    def _refuse_a_plugin_that_is_not_ours(self) -> None:
        """Refuse a ``workstation`` plugin this core did not write.

        Discovering somebody else's plugin folder and rewriting its manifest is
        the kind of helpfulness that destroys work, and the signature is cheap
        to check: the core's own workstation plugin is an http plugin declaring
        an endpoint set. A folder with no readable manifest counts as not ours
        for the same reason — nothing in it can be trusted to say otherwise.
        """
        directory = self._directory()
        if directory is None:
            return
        installed = self._read_manifest()
        if installed is None:
            raise EnrolmentRefused(409, PLUGIN_NOT_OURS, reason="name_taken")
        plugin = installed.get("plugin")
        if not isinstance(plugin, Mapping) or not plugin.get("urls"):
            raise EnrolmentRefused(409, PLUGIN_NOT_OURS, reason="name_taken")

    def _ensure_plugin(self, payload: EnrolmentPayload) -> bool:
        """Create the one ``workstation`` plugin if it is not there. Once, ever.

        Returns whether this call created it, so a later failure in the same
        call can take it back out again and leave the core exactly as it found
        it.

        **A plugin of that name this core did not write is refused, not
        overwritten.** Discovering somebody else's plugin folder and rewriting
        its manifest is the kind of helpfulness that destroys work, and the
        signature is cheap to check: the core's own workstation plugin is an
        http plugin declaring an endpoint set.
        """
        self._refuse_a_plugin_that_is_not_ours()
        if self._directory() is not None:
            return False

        # The document the folder is created with names this machine's own
        # addresses and no credential: the machine has no id until the registry
        # gives it one, and the id is what its token is stored under. The full
        # document — every machine, every address, every secret name — is written
        # by ``_publish`` a moment later, before anything reloads.
        document = build_manifest_document(
            [_provisional(payload)],
            version=payload.agent_version,
            contract=payload.contract_version,
            tools=payload.tools,
        )
        package = _bootstrap_package(render_manifest(document))
        try:
            install_package(
                self.layout,
                package,
                replace=False,
                limits=self.package_limits,
                secrets=self.secrets,
            )
        except PackageRejected as exc:
            raise EnrolmentRefused(409, str(exc), reason="name_taken") from None
        except Exception as exc:  # noqa: BLE001 - the volume, not the caller
            logger.error(
                "workstation_plugin_install_failed", plugin=PLUGIN_NAME, error=repr(exc)
            )
            raise EnrolmentRefused(
                500,
                "The core could not write the workstation plugin. Nothing was "
                "enrolled. Check the appdata volume is mounted and writable.",
                reason="storage",
            ) from None
        return True

    def _write_manifest(self, text: str) -> None:
        """Replace ``manifest.toml`` atomically, or leave the old one alone.

        A temporary file **in the same directory** — a rename is only atomic
        within one filesystem and appdata is a mounted volume — flushed, fsynced
        and moved with :func:`os.replace`. A crash leaves either the old
        endpoint set or the new one, never half of either. The same reasoning as
        ``registry.MachineRegistry._write``, on the sibling file.
        """
        directory = self._directory()
        if directory is None:  # pragma: no cover - _ensure_plugin ran first
            raise EnrolmentRefused(
                500, PLUGIN_NOT_OURS, reason="storage"
            )
        path = directory / MANIFEST_FILENAME
        temporary = path.with_name(path.name + ".new")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - the real failure is being raised
                pass
            logger.error(
                "workstation_manifest_write_failed", plugin=PLUGIN_NAME, error=str(exc)
            )
            raise EnrolmentRefused(
                500,
                "The workstation plugin's registration could not be saved. Check "
                "the appdata volume is mounted and writable.",
                reason="storage",
            ) from None

    def _undo_plugin(self) -> None:
        """Take back a plugin **this call** created. Never raises.

        Only ever called when ``_ensure_plugin`` returned ``True``, so there is
        nothing of the owner's in the folder: it was made moments ago and the
        machine that was going to be in it was refused.
        """
        for step, action in (
            ("uninstall", lambda: uninstall_package(self.layout, PLUGIN_NAME)),
            ("secrets", lambda: self.secrets.delete_namespace(PLUGIN_NAME)),
        ):
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - already failing; say so and move on
                logger.error(
                    "workstation_plugin_rollback_failed",
                    plugin=PLUGIN_NAME,
                    step=step,
                    error=repr(exc),
                )

    # -- telling the rest of the core -------------------------------------

    def _claim(self, display_name: str) -> None:
        """Name the machine on the pairing screen. Never fails the enrolment."""
        try:
            self.claim(display_name)
        except Exception as exc:  # noqa: BLE001 - the machine did join
            logger.warning("workstation_claim_failed", error=repr(exc))

    def _refuse(self, display_name: str | None, reason: str) -> None:
        """Close the pairing dialog on a refusal. Never replaces the refusal.

        Swallowing a failure here is deliberate and is the same judgement
        :meth:`_claim` makes: the caller already has an answer to give, and a
        pairing store that would not take the news must not turn one refusal
        into a different one.
        """
        try:
            self.refuse(display_name, reason)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("workstation_refusal_not_recorded", error=repr(exc))

    async def _start(self, *, created: bool, reawakened: bool = False) -> str:
        """Rescan, and switch the plugin on when it is this core's to switch on.

        A failure to *start* is not a failure to enrol: the record, the
        credential and the endpoint set are all on disk, and the supervisor will
        pick the machine up at the next reload or restart. Reporting the state
        honestly — ``unknown`` rather than ``ok`` — is the whole reason the
        health enum has three values.

        **Off is two different facts, and only one of them is the owner's.**

        * ``created`` or ``reawakened`` — the core made this plugin, or the core
          parked it when its last machine was removed. Switching it on undoes
          the core's own housekeeping and overrides nobody.
        * anything else — the owner switched it off by clicking. It stays off.
          The machine still joins, the state is reported as ``disabled``, and
          the answer says the plugin is off and where to switch it on.

        The difference is **read from the marker in the plugin's own folder**
        (:data:`~personacore.plugins.packages.EMPTY_MARKER`), not inferred from
        the plugin being empty. Inferring it would be the same trap in another
        coat: a plugin the owner switched off and then enrolled into stops being
        empty at exactly the moment the question is asked, so "it has no
        machines" cannot answer "who turned it off".
        """
        if created or reawakened:
            # The recorded choice first, the running core second -- the same
            # order `admin/api_plugins.py` toggles a plugin in, so a crash
            # between the halves leaves a core that comes back switched on. A
            # stale entry in the disabled list can outlive the plugin that put
            # it there, and without this a freshly created workstation plugin --
            # or one the core parked when it emptied -- would be correct on disk
            # and never start.
            try:
                await asyncio.to_thread(
                    set_plugin_enabled, self.layout, PLUGIN_NAME, enabled=True
                )
            except Exception as exc:  # noqa: BLE001 - it is enrolled either way
                logger.error(
                    "workstation_enable_failed", plugin=PLUGIN_NAME, error=repr(exc)
                )
        elif await asyncio.to_thread(self._switched_off):
            return "disabled"

        if self.set_enabled is not None:
            try:
                await self.set_enabled(PLUGIN_NAME, True)
            except Exception as exc:  # noqa: BLE001 - one plugin, spec section 5.1
                logger.error(
                    "workstation_start_failed", plugin=PLUGIN_NAME, error=repr(exc)
                )
        try:
            listing = await self.reload()
        except Exception as exc:  # noqa: BLE001 - it is enrolled either way
            logger.error(
                "workstation_reload_failed", plugin=PLUGIN_NAME, error=repr(exc)
            )
            return "unknown"
        for view in getattr(listing, "plugins", ()):
            if getattr(view, "name", None) == PLUGIN_NAME:
                return str(getattr(view, "state", "unknown"))
        return "unknown"

    def _switched_off(self) -> bool:
        try:
            return PLUGIN_NAME in read_disabled_plugins(self.layout)
        except Exception as exc:  # noqa: BLE001 - an unreadable list is not off
            logger.warning(
                "workstation_disabled_list_unreadable", plugin=PLUGIN_NAME, error=repr(exc)
            )
            return False


@dataclass(frozen=True, slots=True)
class MachineRemoved:
    """What :meth:`EnrolmentService.remove_machine` did.

    Three facts, because the caller has a different decision to make on each.
    """

    removed: bool
    """Whether there was a machine of that id. ``False`` is a screen removing a
    row that had already gone, which is a double click rather than a fault."""

    machines_left: int
    """How many are enrolled now. What the plugin's own page counts, and what
    an audit row may carry — it identifies no machine."""

    switched_off: bool
    """**True when that was the last machine and the core parked the plugin.**

    Owner decision, 2026-09-09: the last machine leaving switches the plugin
    off and leaves it in place. Not uninstalled — the folder, its settings and
    its row on the Plugins screen all survive, and a machine joining starts it
    again.

    This replaces the ``endpoints_stale`` alarm that stood here for one round.
    That field existed because the hook noticed a consequence it was not
    allowed to act on and had to hand it to the caller; the hook acts on it
    now, so what is left is a fact about what happened rather than something
    the caller must go and finish. The manifest is indeed left as it was — see
    :meth:`EnrolmentService.remove_machine` — but nothing dials it and nothing
    has to be done about it.
    """


@dataclass(frozen=True, slots=True)
class _Added:
    """What :meth:`EnrolmentService._add` did, for the steps that follow it."""

    machine: Machine
    created_plugin: bool

    reawakened: bool = False
    """Whether the plugin had been parked by the core for having no machines.

    The one fact that separates "switch it back on" from "leave the owner's
    switch alone", read before the write that makes it stop being true.
    """


def _provisional(payload: EnrolmentPayload) -> Machine:
    """The joining machine as a record, before the registry has given it an id.

    Only ever handed to :func:`build_manifest_document` to create the plugin
    folder, and only for as long as it takes the registry to write the real row
    a moment later. ``token_secret`` is empty because there is no id yet and so
    no name for the token to be stored under: an entry with no ``auth_secret``
    is a valid endpoint declaration and simply sends no bearer token, which is
    the honest description of a machine that is not enrolled yet.
    """
    return Machine(
        id="",
        name=payload.display_name,
        addresses=tuple(
            MachineAddress(url=url, tls_fingerprint=pin) for url, pin in payload.addresses
        ),
        token_secret="",
        enrolled_at=datetime.now(UTC),
    )


def _tool_difference(ours: set[str], theirs: set[str]) -> str:
    """Which tools differ, in a sentence. Names only, both directions."""
    parts: list[str] = []
    extra = sorted(theirs - ours)
    absent = sorted(ours - theirs)
    if extra:
        parts.append("It offers " + ", ".join(extra) + ", which they do not.")
    if absent:
        parts.append("They offer " + ", ".join(absent) + ", which it does not.")
    return " ".join(parts)


GENERIC_REFUSAL = "refused"
"""What a refusal with no code of its own is recorded as on the pairing screen.

Deliberately uninformative rather than guessed at. A refusal that predates the
registry's vocabulary is still a refusal and still has to close the dialog; the
useful sentence went to the Agent that caused it.
"""


def _default_refuse(display_name: str | None, reason: str) -> None:
    """Record a refusal on the pairing screen, through the module that owns it.

    Imported inside the function for the reason :func:`_default_claim` gives.
    """
    from personacore.enrolment.pairing import mark_refused  # noqa: PLC0415 - see docstring

    mark_refused(display_name, reason)


def _default_claim(display_name: str) -> None:
    """Name the machine on the pairing screen, through the module that owns it.

    Imported inside the function rather than at module scope on purpose: the
    pairing store and this half were built in parallel against a written
    signature, and a module-level import would make the order they land in
    matter. It also keeps :class:`EnrolmentService` testable with a stub without
    the store existing at all.
    """
    from personacore.enrolment.pairing import mark_claimed  # noqa: PLC0415 - see docstring

    mark_claimed(display_name)


def mint_token() -> str:
    """A new bearer token for one workstation. The only place one is created."""
    return secrets_module.token_urlsafe(TOKEN_BYTES)


__all__ = [
    "ACCEPTED_RISK",
    "AGENT_TOKEN_PATH",
    "CODE_REFUSED",
    "CONTRACT_DIFFERS",
    "CREDENTIAL_IN_REQUEST",
    "ENROL_PATH",
    "ENTRY_FIELDS",
    "GENERIC_REFUSAL",
    "MANIFEST_FILENAME",
    "MAX_BODY_BYTES",
    "PLUGIN_NAME",
    "PLUGIN_NOT_OURS",
    "RATE_LIMITED",
    "REQUEST_FIELDS",
    "TOOLS_DIFFER",
    "WORKSTATION_DESCRIPTION",
    "Enrolled",
    "EnrolmentPayload",
    "EnrolmentRefused",
    "EnrolmentService",
    "MachineRemoved",
    "build_manifest_document",
    "decode_body",
    "mint_token",
    "normalise_display_name",
    "normalise_fingerprint",
    "push_token",
    "read_addresses",
    "read_code",
    "read_payload",
    "render_manifest",
    "risk_refused_message",
    "validate_tools",
    "validate_url",
    "validate_version",
]
