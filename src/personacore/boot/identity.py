"""Who is allowed to say who they are — spec §7, moved by ADR-0040.

Moved out of ``personacore.server``. Two things that must be decided in exactly
one place, and were: which peers may assert an identity header at all, and which
correlation id every record written while answering one request is filed under.

Both are enforced as ASGI middleware, so they apply before any route is reached
and no surface can opt out of them. The environment variable names live here too
— they are read by the assembly, but what they *mean* is this module's subject,
and :mod:`personacore.auth.method` already documents them by this name.

Putting a saved ``[auth] method`` into force is the third. What a method
*means* is still :mod:`personacore.auth.method`'s to decide; this module only
adopts the answer, and never re-reads either source afterwards — so what
``/health`` reports is what the running process is actually doing.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI

from personacore.admin.authn import LiveAuth
from personacore.audit.logging import bind_correlation_id, clear_correlation_id
from personacore.config.settings import AuthSettings

log = structlog.get_logger(__name__)

TRUSTED_PROXY_ENV = "PERSONACORE_TRUSTED_PROXY_IPS"
"""Addresses whose identity header is believed. Defaults to loopback only.

The admin API's whole auth model is "the proxy already authenticated this and
set a header" (spec §7). That holds only if nothing else can set the header. A
proxy is supposed to strip inbound copies, but the core is not entitled to assume
one is in front: with host networking and no proxy, anyone on the LAN could send
the header themselves and be an admin.

So the header is honoured only from an allowlisted peer, and the default is
loopback — which covers a proxy on the same host and nothing else. Set this to the
proxy's address when it runs elsewhere, e.g. on a Compose network.
"""

DEV_ADMIN_USER_ENV = "PERSONACORE_ADMIN_DEV_USER"
"""Escape hatch for running without a login proxy in front.

When set, requests arriving without the trusted header are treated as coming
from that user. It exists because the admin surface is unusable until a proxy is
wired in, and "unusable" is how a security control gets ripped out rather than
configured.

It is off unless explicitly set, it is announced loudly at startup, and it is
reported by the health endpoint so it cannot quietly persist into a deployment
that has a proxy. With it on, ANYONE who can reach this port is an admin.
"""

DEFAULT_TRUSTED_USER_HEADER = "Remote-User"
"""Set by the reverse proxy after it has authenticated the request (spec §7).

Safe only because the proxy terminates OIDC, strips any inbound copy of this
header, and this listener is never published directly. With no proxy in front,
the admin API fails closed rather than trusting whatever arrives.
"""



def _setup_required(app: FastAPI) -> bool:
    """Whether the core's own sign-in has no account yet (PC-291).

    Reported by ``/health`` because "the setup page is open" is a posture an
    operator needs to be able to see from outside the container - a core that
    sat on an unclaimed setup page would hand the first caller an admin
    account. It is a boolean and nothing more: no names, no counts.
    """
    context = getattr(app.state, "auth_context", None)
    return bool(context is not None and context.setup_required())


def _parse_trusted_proxies(raw: str | None) -> set[str]:
    if raw is None or not raw.strip():
        return {"127.0.0.1", "::1", "localhost"}
    return {item.strip() for item in raw.split(",") if item.strip()}


class CorrelationIdMiddleware:
    """Bind one correlation id per HTTP request — spec sections 7 and 9.

    PC-012 asks that "everything one request did" can be pulled out of the
    trace as a single group. Before this, only ``AgentLoop.run_turn`` bound an
    id, so any surface that wrote a record without going through a turn — the
    whole admin API — fell back to minting a fresh id *per record*. One admin
    request that writes two records (a settings-page lookup writes a
    ``tool_call`` and an ``admin_change``) produced two rows nothing could
    group, and a filter on either id returned half the story.

    Plain ASGI rather than ``BaseHTTPMiddleware`` on purpose: the id is a
    ``contextvar``, and this way the endpoint, its background work and a
    streaming response body all run inside the context it was bound in — the
    streaming API turn (spec section 5.4) yields its last chunks long after the
    endpoint returned.

    The id is generated here, never read from a request header. Letting a
    caller choose it would let one caller file its records under another's
    request, and the trace view is an accountability record (spec section 7).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        bind_correlation_id()
        try:
            await self.app(scope, receive, send)
        finally:
            clear_correlation_id()


def _install_identity_guard(
    app: FastAPI,
    header_name: str,
    trusted_proxies: set[str],
    dev_user: str | None,
) -> None:
    """Decide, in one place, who is allowed to assert an identity.

    Two rules, applied to every request before any route sees it:

    1. The identity header is stripped unless the peer is an allowlisted proxy.
       Without this the admin API's auth model is "whoever asks nicely", because
       nothing stops a caller sending the header a proxy would have set.
    2. If the development bypass is on, the header is then set to that user —
       overwriting rather than honouring anything inbound, so a convenience for
       testing never becomes a way to choose which user you are.
    """
    encoded = header_name.lower().encode("latin-1")
    dev_value = dev_user.encode("latin-1") if dev_user else None

    @app.middleware("http")
    async def _identity_guard(request, call_next):  # type: ignore[no-untyped-def]
        peer = request.client.host if request.client else None
        headers = request.scope["headers"]
        if peer not in trusted_proxies:
            headers = [(k, v) for k, v in headers if k.lower() != encoded]
        if dev_value is not None:
            headers = [(k, v) for k, v in headers if k.lower() != encoded]
            headers = [*headers, (encoded, dev_value)]
        request.scope["headers"] = headers
        return await call_next(request)

    if dev_user:
        log.warning(
            "admin_auth_bypass_enabled",
            header=header_name,
            user=dev_user,
            detail=(
                "Every request to the admin API is treated as this user. Anyone "
                "who can reach this port is an admin. Set this only on a network "
                "you control, and unset it once a login proxy is in front."
            ),
        )
    else:
        log.info("identity_guard_enabled", header=header_name, trusted=sorted(trusted_proxies))


def apply_auth_settings(
    app: FastAPI,
    live_auth: LiveAuth,
    new_auth: AuthSettings,
    *,
    trusted_header: str,
    trusted_proxies: set[str],
) -> None:
    """Swap the door to whatever ``[auth] method`` now says (ADR-0010).

    **Synchronous, and deliberately so.** Everything else here can settle a
    moment after the response that saved it; the door cannot. The very next
    request is likely to be the operator's own browser following the save,
    and a door that changed a tick later would answer that request through
    the old one -- which is the same "saved but not applied" confusion the
    restart notice used to be honest about, wearing a new face.

    Nothing here decides whether the swap is safe. That was settled before
    the setting reached disk, by ``LiveAuth.refusal_for`` in the admin API's
    save path: if the new door would not admit the operator asking for it,
    this is never reached and ``core.toml`` still says what it said.
    """
    decision = live_auth.adopt(new_auth.method)
    app.state.auth_decision = decision
    app.state.auth_health = decision.as_health(
        trusted_header=trusted_header, trusted_proxies=sorted(trusted_proxies)
    )
