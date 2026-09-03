"""The LLM roster: one client per endpoint, one handle per role — ADR-0011.

Moved out of ``personacore.server`` by ADR-0040. This module owns which client
answers for a role, which answers for a persona (ADR-0036), how an endpoint is
identified, and what is said about a role whose API key was never supplied.

It deliberately owns none of the assembly. Nothing here reads ``core.toml``,
touches ``app.state``, or knows that there is an HTTP listener: the roster is
built from a settings object and a layout, and would make the same sense to a
caller with no FastAPI in the process at all.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import structlog
from pydantic import SecretStr

from personacore.agent.personas import Persona
from personacore.config import AppdataLayout, SecretStore
from personacore.config.secrets import SecretError
from personacore.config.settings import (
    CoreSettings,
    LLMRole,
    LLMSettings,
    llm_role,
)
from personacore.llm.client import HealthStatus, LLMClient, LLMClientConfig
from personacore.llm.errors import LLMClientError

log = structlog.get_logger(__name__)


class LiveLLM:
    """A stable handle to whichever LLM client one role currently resolves to.

    ADR-0010 puts the LLM host in the admin UI, and a setting that needs a
    container restart to take effect has only moved the friction. The agent loop
    and the health dashboard are handed this object once; saving new settings
    rebinds the client inside it. They keep working against the same reference
    and never learn that it changed.

    One handle per role (ADR-0011), but **not** necessarily one client per
    handle: roles that resolve to the same endpoint share a client, and
    therefore share its connection pool and its circuit breaker. The
    :class:`LLMRoster` owns that decision and owns the clients' lifetimes —
    this handle never closes what it points at, because what it points at may
    still be another role's client.

    It also satisfies the admin API's per-role health view
    (``personacore.admin.protocols.LLMRoleView``), which is why ``role``,
    ``falls_back_to``, ``endpoint`` and ``facts`` live here rather than in a
    parallel wrapper that could drift from the client actually in use.
    """

    def __init__(
        self,
        role: LLMRole,
        client: LLMClient,
        *,
        endpoint: str,
        falls_back_to: str | None,
        unusable: str | None = None,
    ) -> None:
        self.role: str = role.value
        self.falls_back_to = falls_back_to
        self.endpoint = endpoint
        self._client = client
        self._unusable = unusable

    @property
    def current(self) -> LLMClient:
        return self._client

    @property
    def unusable(self) -> str | None:
        """Why this role cannot work, or ``None``.

        Set when the API key secret ``core.toml`` names has not been supplied.
        The core still boots — ADR-0025's addendum, "a missing secret is a
        state, not a crash" — and this is the state, carried on the handle so
        the dashboard reports it against the role rather than the operator
        finding out from a 401.
        """
        return self._unusable

    def rebind(
        self,
        client: LLMClient,
        *,
        endpoint: str,
        falls_back_to: str | None,
        unusable: str | None = None,
    ) -> None:
        """Point this role at a different client. Closing is the roster's job."""
        self._client = client
        self.endpoint = endpoint
        self.falls_back_to = falls_back_to
        self._unusable = unusable

    @property
    def facts(self) -> dict[str, Any]:
        """What the section 9 dashboard shows for this role.

        Deliberately does NOT include the base URL. A hand-edited ``core.toml``
        can carry credentials in one (``http://user:secret@host``), and the
        dashboard is rendered, logged and screenshotted. The endpoint digest
        below is enough to see which roles share a host — and therefore share a
        breaker — without publishing the address itself.
        """
        snapshot = self._client.breaker_snapshot
        facts: dict[str, Any] = {
            "model": self._client.config.model,
            "endpoint": self.endpoint,
            "breaker": snapshot.state.value,
        }
        if self.falls_back_to is not None:
            facts["falls_back_to"] = self.falls_back_to
        if self._unusable is not None:
            # The sentence, not the secret's value — there is no value here to
            # leak, because none was ever read (ADR-0025 §5).
            facts["unusable"] = self._unusable
        return facts

    def stream_chat_completion(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.stream_chat_completion(*args, **kwargs)

    async def chat_completion(self, *args: Any, **kwargs: Any) -> Any:
        return await self._client.chat_completion(*args, **kwargs)

    async def list_models(self, *args: Any, **kwargs: Any) -> Any:
        return await self._client.list_models(*args, **kwargs)

    async def context_length(self) -> int | None:
        return await self._client.context_length()

    async def health_check(self) -> Any:
        """The endpoint's probe, or the reason there is no point probing it.

        A role whose key is missing is reported unhealthy **without** a request
        going out: an unauthenticated call to a host that wants a key comes back
        as a 401, and "the host rejected our credentials" is the wrong sentence
        for "we were never given any". Spec section 10 asks for a plain-English
        explanation of what is down; this is the one that names the fix.
        """
        if self._unusable is not None:
            return HealthStatus(
                healthy=False, detail=self._unusable, breaker=self._client.breaker_snapshot
            )
        return await self._client.health_check()

    @property
    def breaker_snapshot(self) -> Any:
        return self._client.breaker_snapshot


MAX_PERSONA_ENDPOINTS = 32
"""How many distinct persona connections this core expects to hold clients for.

Not a refusal — see :meth:`LLMRoster.client_for`. It is the number past which
something is worth saying in the log, because the only way to reach it is a
persona being re-pointed over and over, and the clients left behind by that are
released on the next settings save rather than immediately."""


class LLMRoster:
    """One client per distinct endpoint, one handle per role — ADR-0011.

    Two rules, and the whole class exists to hold them together:

    * **A role that falls back to ``interactive`` shares its client.** Opening a
      second connection pool to the same host, with a second breaker that can
      disagree with the first about whether that host is up, would be a bug
      dressed as isolation.
    * **Distinct endpoints get distinct breakers.** ADR-0011 is explicit: a dead
      vision host must not open the breaker conversation depends on. One failing
      endpoint degrades one capability (spec section 10), and since the breaker
      lives inside :class:`~personacore.llm.client.LLMClient`, "distinct
      breakers" and "distinct clients" are the same sentence.

    Sameness is the whole resolved :class:`LLMSettings` value, not just the base
    URL — the model name, timeouts and breaker thresholds are all part of what
    a client *is*, so two roles pointed at one host with different models are
    two endpoints and get two breakers. The digest of that value is the
    endpoint's identity, and it is a digest rather than the value itself so it
    can be shown on the dashboard without risking a credential in a base URL.

    ``transport`` is injectable so tests can drive every endpoint through an
    ``httpx.MockTransport`` without a network; nothing in production passes it,
    and every client is still built through the same settings mapping the
    product uses.
    """

    def __init__(
        self,
        layout: AppdataLayout,
        settings: CoreSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        def build(config: LLMSettings) -> tuple[LLMClient, str | None]:
            return _build_llm_client(layout, config, transport=transport)

        self._build_client: Callable[[LLMSettings], tuple[LLMClient, str | None]] = build
        self._clients: dict[str, LLMClient] = {}
        # Endpoint digest -> why that endpoint cannot be used, or None. Keyed by
        # endpoint rather than by role because the missing key belongs to the
        # endpoint's configuration: two roles sharing an endpoint share its
        # credential, and would otherwise be able to disagree about whether it
        # is there.
        self._unusable: dict[str, str | None] = {}
        self._retired: list[LLMClient] = []
        # Endpoints only a persona asks for (ADR-0036). Kept apart from the
        # roles' map so that re-resolving the roles cannot evict one, and so
        # `_resolve`'s "everything not built this time is retired" rule stays
        # true of the thing it is actually about.
        self._persona_clients: dict[str, LLMClient] = {}
        self._persona_unusable: dict[str, str | None] = {}
        self._persona_used: set[str] = set()
        self._handles: dict[LLMRole, LiveLLM] = {}
        for role, client, endpoint, fallback in self._resolve(settings):
            self._handles[role] = LiveLLM(
                role,
                client,
                endpoint=endpoint,
                falls_back_to=fallback,
                unusable=self._unusable.get(endpoint),
            )

    def _resolve(
        self, settings: CoreSettings
    ) -> list[tuple[LLMRole, LLMClient, str, str | None]]:
        """Work out every role's client, reusing what already exists.

        Reuse is what makes a save cheap and safe: an untouched role keeps the
        exact client object it had, so its pooled connections stay open and its
        breaker keeps whatever state it had learned. Only a role whose resolved
        settings actually changed gets a new one.
        """
        resolved: list[tuple[LLMRole, LLMClient, str, str | None]] = []
        built: dict[str, LLMClient] = {}
        unusable: dict[str, str | None] = {}
        for role in LLMRole:
            config = settings.llm_for(role)
            endpoint = _endpoint_digest(config)
            client = built.get(endpoint) or self._clients.get(endpoint)
            if client is None:
                client, why = self._build_client(config)
                unusable[endpoint] = why
            else:
                # A reused client keeps whatever the store said when it was
                # built. Re-reading here would make an untouched role's status
                # depend on when somebody last loaded a page.
                unusable.setdefault(
                    endpoint, unusable.get(endpoint, self._unusable.get(endpoint))
                )
            built[endpoint] = client
            fallback = settings.llm.falls_back_to(role)
            resolved.append((role, client, endpoint, fallback.value if fallback else None))
        retired: list[LLMClient] = []
        for endpoint, client in self._clients.items():
            if endpoint in built:
                continue
            if endpoint in self._persona_used:
                # A persona is pinned to exactly this connection and is sharing
                # this client. Moving a *role* off the endpoint must not close a
                # client another character is talking through, so it is handed
                # over to the persona side rather than retired. `_unusable` is
                # still the old map here, which is the one that describes this
                # client.
                self._persona_clients.setdefault(endpoint, client)
                self._persona_unusable.setdefault(endpoint, self._unusable.get(endpoint))
                continue
            retired.append(client)
        self._retired = retired
        self._clients = built
        self._unusable = unusable
        return resolved

    def for_role(self, role: LLMRole | str) -> LiveLLM:
        """The handle a consumer holds. Callers ask for a role, never a URL."""
        return self._handles[llm_role(role)]

    def client_for(self, config: LLMSettings) -> tuple[LLMClient, str | None]:
        """A client for a connection that is nobody's role — a persona's own.

        Same two rules as the roles, for the same reasons. **Sameness is the
        whole resolved settings value**, so a persona pinned to exactly what
        ``interactive`` uses shares that client, its connection pool and its
        circuit breaker — one host, one opinion about whether it is up. A
        persona pointed somewhere else gets its own breaker, so a dead model
        behind one character cannot open the breaker every other character is
        talking through.

        Returns the client and, when the connection names an API key secret this
        core was never given, the reason it cannot be used — the caller turns
        that into a sentence in *its* own words, because "check the Models
        screen" is the wrong advice for a persona.
        """
        endpoint = _endpoint_digest(config)
        self._persona_used.add(endpoint)
        client = self._clients.get(endpoint)
        if client is not None:
            return client, self._unusable.get(endpoint)
        client = self._persona_clients.get(endpoint)
        if client is None:
            client, why = self._build_client(config)
            self._persona_clients[endpoint] = client
            self._persona_unusable[endpoint] = why
            if len(self._persona_clients) > MAX_PERSONA_ENDPOINTS:
                # Not refused — a housekeeping limit must never take a
                # character off the air. Said out loud instead, because the
                # only way to reach this number is a lot of editing, and the
                # clients left behind by edits are freed by the next settings
                # save (see `apply`) or a restart.
                log.warning(
                    "persona_llm_endpoints_accumulating",
                    endpoints=len(self._persona_clients),
                    limit=MAX_PERSONA_ENDPOINTS,
                    detail=(
                        "Personas have been pointed at more distinct model "
                        "connections than expected since this core started. "
                        "Nothing is broken; the unused ones are released on the "
                        "next settings save or restart."
                    ),
                )
        return client, self._persona_unusable.get(endpoint)

    def role_views(self) -> list[LiveLLM]:
        """Every role, for the section 9 dashboard's one-row-per-role listing."""
        return [self._handles[role] for role in LLMRole]

    async def health_check(self) -> Any:
        """Interactive's health, so this also satisfies the single-endpoint
        ``LLMHealthSource`` for anything that has not learned about roles."""
        return await self._handles[LLMRole.INTERACTIVE].health_check()

    async def apply(self, settings: CoreSettings) -> None:
        """Re-resolve every role and swap only what changed (ADR-0010)."""
        for role, client, endpoint, fallback in self._resolve(settings):
            self._handles[role].rebind(
                client,
                endpoint=endpoint,
                falls_back_to=fallback,
                unusable=self._unusable.get(endpoint),
            )
        # A persona's connection is edited on a different screen and on its own
        # schedule, so nothing here re-resolves one. What this does is let go of
        # the ones nothing has asked for since the last save: a persona pointed
        # somewhere new leaves its old client behind, and without this the only
        # thing that ever frees it is a restart.
        stale = [
            endpoint
            for endpoint in self._persona_clients
            if endpoint not in self._persona_used
        ]
        for endpoint in stale:
            self._retired.append(self._persona_clients.pop(endpoint))
            self._persona_unusable.pop(endpoint, None)
        self._persona_used.clear()
        # Closed AFTER every handle has been rebound, so an in-flight turn
        # finishes against the client it started with rather than losing its
        # connection mid-answer.
        for retired in self._retired:
            await retired.aclose()
        self._retired = []

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        for client in self._persona_clients.values():
            await client.aclose()


PERSONA_LLM_KEY_MISSING = (
    "I'm set up to answer as {persona} using a model connection of my own, but "
    "the API key it needs hasn't been given to this core, so I can't reach it."
)
"""What the assistant says when a persona's own connection has no usable key.

Spoken aloud, so it is a sentence and not a diagnosis: the secret's NAME is in
the log line beside it, where an operator will look, and its value is nowhere —
nothing ever read one (ADR-0025 §5). Deliberately not the role wording from
:func:`llm_key_unavailable`, which sends somebody to the Models screen; this
one is fixed on the persona's own screen."""


class PersonaLLMRouter:
    """Which client answers for one persona — ``PersonaLLMSource``, ADR-0036.

    Two lines of policy and no more: a persona with no connection of its own
    uses the system's, and a persona that has one uses the roster's client for
    it. The roster is asked rather than a client being built here, so a
    persona's endpoint gets the same pooling, the same circuit breaker and the
    same lifetime as a role's — building one here would be a second way to
    reach an LLM host, with its own idea of when to give up.

    A connection whose API key secret is missing raises, and raises the same
    typed error the client itself would: the agent loop already turns an
    ``LLMClientError`` into a spoken sentence (spec §10), so this degrades the
    way an unreachable host does and costs one character rather than the turn,
    the screen, or anybody else's persona.
    """

    def __init__(self, roster: LLMRoster, fallback: LiveLLM) -> None:
        self._roster = roster
        self._fallback = fallback

    def stream_for(self, persona: Persona) -> Any:
        connection = persona.connection
        if connection is None:
            return self._fallback
        client, unusable = self._roster.client_for(connection)
        if unusable is not None:
            log.warning(
                "persona_llm_key_unavailable",
                persona=persona.name,
                # The name of the secret, never a value — there is no value
                # here, because none was ever read.
                secret=connection.api_key_secret,
            )
            raise LLMClientError(
                PERSONA_LLM_KEY_MISSING.format(persona=persona.display_name or persona.name),
                detail=unusable,
            )
        return client


def _endpoint_digest(config: LLMSettings) -> str:
    """A stable, non-revealing identity for one endpoint configuration.

    Not a hash for security's sake — it is a dictionary key. It is a hash
    rather than the settings themselves so that showing "these two roles share
    an endpoint" on the dashboard cannot show a base URL that a hand-edited
    config put credentials into.
    """
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def llm_key_unavailable(secret: str) -> str:
    """The sentence the dashboard shows for a core secret nobody has supplied.

    Written here rather than passed through from
    :meth:`~personacore.config.secrets.SecretStore.core_secrets`, whose own
    message for a core secret still points at "the settings screen that asks
    for it" — a screen ADR-0025 §4 gives to *plugin* secrets only. Telling an
    operator to use something that does not exist is worse than telling them
    nothing, so this says what is actually true today: the value goes on the
    appdata volume, and the role runs without it until it does.

    The secret's NAME, never its value (ADR-0025 §5). A name is what
    ``core.toml`` already carries and what the config screen already shows.
    """
    return (
        f"This role is not usable: it is configured to use the API key stored "
        f"under the secret name {secret!r}, and this core has not been given a "
        f"secret by that name. Everything else is running. Put the value in "
        f"appdata/secrets/core/{secret} and restart, or clear this role's API "
        f"key setting on the Models screen to run without a key."
    )


def _resolve_llm_api_key(
    layout: AppdataLayout, config: LLMSettings
) -> tuple[SecretStr | None, str | None]:
    """The core-owned API key named by one role, or a sentence saying why not.

    The twin of :func:`_resolve_bus_password`, and for the same reason: ADR-0025's
    addendum says **a missing secret is a state, not a crash**, and that rule is
    about the core's own credentials as much as a plugin's. Letting
    ``SecretError`` out of here stopped ``create_app`` dead, so a ``core.toml``
    naming a secret the volume had lost took the whole container down on the
    next unattended restart — including the admin UI, which is the only place
    the credential could ever have been supplied. An operator locked out of the
    UI can fix nothing; one LLM role that says it is unusable can be fixed on a
    screen.

    The message names the secret and never its value.
    """
    if not config.api_key_secret:
        return None, None
    try:
        # Core-owned (ADR-0025): the LLM key belongs to the assistant, not to
        # any plugin, and no plugin's namespace can reach it.
        return SecretStore(layout).core_secrets().get(config.api_key_secret), None
    except SecretError as exc:
        log.warning(
            "llm_api_key_secret_unavailable",
            secret=config.api_key_secret,
            error=str(exc),
        )
        return None, llm_key_unavailable(config.api_key_secret)


def _build_llm_client(
    layout: AppdataLayout,
    config: LLMSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[LLMClient, str | None]:
    """One client for one endpoint, and why that endpoint is unusable if it is.

    Returns the pair rather than raising, so that a role whose key is missing
    still has a client object in the graph — everything downstream holds a
    :class:`LiveLLM` handle and none of it has to learn about a hole — while the
    dashboard gets the one sentence that explains the hole.
    """
    api_key, unusable = _resolve_llm_api_key(layout, config)
    client = LLMClient(
        LLMClientConfig(
            base_url=config.base_url,
            model=config.model,
            api_key=api_key,
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.read_timeout_seconds,
            failure_threshold=config.failure_threshold,
            cooldown_seconds=config.cooldown_seconds,
        ),
        transport=transport,
    )
    return client, unusable
