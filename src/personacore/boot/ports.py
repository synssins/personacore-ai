"""Which port the listener binds, and what a bad one is told — ADR-0040.

Moved out of ``personacore.server``. Three sources name a port — an explicit
argument, ``PERSONACORE_PORT``, then ``[server]`` in ``core.toml`` — and only
the last of them was ever bounded. This module holds the ordering and the
bounds together, so that a port nobody can use is a plain-English config error
(spec §9) rather than a bare ``ValueError`` or a container nobody can reach.

It does not bind anything and knows nothing about uvicorn.
"""

from __future__ import annotations

from personacore.config import ConfigError


def _override_port(explicit: int | None, env_value: str | None) -> int | None:
    """The port from the first source that names one, or ``None`` for neither.

    ``[server].port`` is bounded 1-65535 by :class:`ServerSettings`; the two
    sources that outrank it were not bounded at all, so an out-of-range value
    reached uvicorn and a non-numeric one escaped as a bare ``ValueError`` from
    ``int()`` instead of the plain English every other config failure gets
    (spec section 9).

    Port 0 is refused rather than treated as "not set". To the OS it means
    "pick any free port", which for a container whose port is published and
    health-checked is an assistant nobody can reach; someone who writes 0 has
    made a mistake, and falling quietly through to the next source would hide
    it from them.
    """
    if explicit is not None:
        return _checked_port(explicit, "The port given on the command line")
    if env_value is not None and env_value.strip():
        try:
            parsed = int(env_value.strip())
        except ValueError:
            raise ConfigError(
                f"PERSONACORE_PORT is set to '{env_value}', which is not a whole number. "
                "Set it to a port between 1 and 65535, or leave it unset to use the "
                "port from core.toml's [server] section."
            ) from None
        return _checked_port(parsed, "PERSONACORE_PORT")
    return None


def _checked_port(port: int, source: str) -> int:
    if not 1 <= port <= 65535:
        raise ConfigError(
            f"{source} is {port}, which is not a port that can be used. "
            "Ports run from 1 to 65535."
        )
    return port
