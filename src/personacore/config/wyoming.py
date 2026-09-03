"""The Wyoming server's settings — ``[wyoming]`` in ``core.toml``.

Wyoming is the peer-to-peer protocol Home Assistant's voice pipeline speaks to
its speech services. PersonaCore is the **provider**: Home Assistant connects
to us and we answer as its speech-to-text and text-to-speech.

**Off by default, and the default bind is loopback, deliberately.** The
protocol has no authentication, no authorisation and no encryption — the
upstream project says so in its own SECURITY.md, and its position is that
anything which can reach a Wyoming service can use it. Anyone who reaches this
port can transcribe audio through PersonaCore and make it speak, with no
credential of any kind. So an operator turns it on knowingly — **and that
switch is the whole decision.**

``host`` used to default to loopback so that widening it was a second,
deliberate act. In practice that protected nothing and cost an afternoon: the
listener bound inside the container, ``/health`` reported ``wyoming`` among its
surfaces, and every connection from the machine that needed it was refused.
Loopback is unreachable through a published Docker port and, under host
networking, reachable only from the host itself. Somebody who has switched this
on has already said they want Home Assistant to reach it; a second invisible
setting between them and that is a puzzle, not a safeguard.

Keep it on a network you control. There is nothing to put in front of it — the
protocol carries no credential for a proxy to check — so the network *is* the
boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PORT = 10300
"""The convention is 10300 for speech-to-text and 10200 for text-to-speech.

One port carries both here. A single Wyoming ``info`` may describe an ``asr``
program and a ``tts`` program at once, and Home Assistant creates two entities
from it — so two listeners would be two of everything for no gain.
"""


class WyomingSettings(BaseModel):
    """``[wyoming]`` — whether Home Assistant may use this core for speech."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    """Off unless somebody turned it on. See the module docstring for why."""

    host: str = "0.0.0.0"  # noqa: S104 - deliberate; see below
    """Every interface, because the switch is the decision and this is not.

    **This default was `127.0.0.1` and it was wrong in practice.** The reasoning
    was sound in the abstract — nothing stands in front of this port, and
    nothing can, since the protocol carries no credential for a proxy to check —
    but it produced a service that reported itself running and could not be
    reached. Docker forwards a published port to the container's interface, and
    an application bound to the container's loopback never sees it; under host
    networking the same bind is reachable only from the host itself. That
    produced exactly this: `/health` listing `wyoming` among its surfaces while
    the port refused every connection from the machine that needed it.

    Loopback here protected nothing that `enabled` does not already protect.
    The server is **off until somebody turns it on**, and turning it on is the
    decision — an operator who has done that has said they want Home Assistant
    to reach this core, and making them then discover a second, invisible
    setting is a puzzle rather than a safeguard.

    The box stays, for the case that actually bites: a port or interface clash
    on a host-networked container, where something else already holds 10300.
    """

    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
