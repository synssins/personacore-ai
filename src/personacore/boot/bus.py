"""The bus's own two decisions — ADR-0040.

Which password the broker is offered, and what a saved ``[bus]`` does to a bus
that is already running. Both moved out of ``personacore.server``; the assembly
now builds a bus and asks this module to point it somewhere.

The password half is the bus's side of the rule the LLM roster keeps on its
own: a secret is referenced by NAME in ``core.toml`` (spec §7), resolved here,
and never carried in config.

It returns a failure rather than raising one, because the bus is a degradable
dependency. Deciding that is policy, which is why it is here and not in the
assembly — the assembly only asks, and passes the answer on to ``/health``.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI
from pydantic import SecretStr

from personacore.bus.client import EventBus
from personacore.config import AppdataLayout, SecretStore
from personacore.config.secrets import SecretError
from personacore.config.settings import BusSettings

log = structlog.get_logger(__name__)


def _resolve_bus_password(
    layout: AppdataLayout, settings: BusSettings
) -> tuple[SecretStr | None, str | None]:
    """The MQTT password named in ``[bus]``, or a sentence saying why not.

    Mirrors ``_build_llm_client``'s ``api_key_secret`` resolution: a secret is
    referenced by NAME in core.toml (spec §7) and resolved here, never carried
    in config. It stays a :class:`~pydantic.SecretStr` the whole way, as the LLM
    path does, so nothing holds the plaintext for the life of the process.

    It returns the failure rather than raising it because the bus is a
    DEGRADABLE dependency (spec §10, and :class:`EventBus`'s own docstring):
    with no broker reachable the assistant loses its push channel and nothing
    else, and a password that cannot be read is that same failure one step
    earlier. The message names the secret, never its value.

    One function rather than two copies because both the assembly path and the
    live-apply path need it, and a second copy is how the two would come to
    disagree about what "degraded" means.

    ``[bus].password`` wins over ``[bus].password_secret`` when both are set.
    The plain field is what the Core settings screen writes, so an operator who
    types a password there has said something more recent and more deliberate
    than a secret name left over in the file — and a save that appeared to do
    nothing because an old ``password_secret`` still took precedence would be
    the same class of silent-wrong-value this whole change exists to end.
    """
    if settings.password is not None:
        return settings.password, None
    if not settings.password_secret:
        return None, None
    try:
        # The broker password is the CORE's, not a plugin's, so it is read
        # through the core's own door (ADR-0025): `SecretStore` itself has no
        # method that returns a value, and `core_secrets()` is the named,
        # greppable exception for the core's own credentials.
        return SecretStore(layout).core_secrets().get(settings.password_secret), None
    except SecretError as exc:
        log.warning(
            "bus_password_secret_unavailable",
            secret=settings.password_secret,
            error=str(exc),
        )
        return None, str(exc)


async def apply_bus_settings(
    app: FastAPI, bus: EventBus, layout: AppdataLayout, new_bus: BusSettings
) -> None:
    """Point the running bus at whatever ``[bus]`` now says (ADR-0010).

    The secret is re-resolved on every save rather than carried over from
    boot. That is not just so a rotated password lands: ``bus_password_
    degraded`` is what /health reports, and a "degraded" left standing after
    the operator created the missing secret is its own defect — it says the
    push channel is unauthenticated when it is not.

    Nothing in here is allowed to turn a settings save into an error. The
    bus decides for itself whether anything changed, contacts the new
    broker in the background exactly as it does at startup, and an address
    that does not answer costs the push channel and nothing else (spec §10).
    """
    password, error = _resolve_bus_password(layout, new_bus)
    try:
        changed = await bus.reconfigure(new_bus, password=password)
    except Exception as exc:  # noqa: BLE001 - degradable: never fail the save
        log.warning("bus_reconfigure_failed", error=repr(exc))
        return
    # Set only once the new configuration is actually in force, so the flag
    # always describes the bus that is running rather than one that failed
    # to be adopted.
    app.state.bus_password_degraded = error
    if changed:
        log.info("bus_settings_applied", host=new_bus.host, port=new_bus.port)
