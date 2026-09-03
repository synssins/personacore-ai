"""Who is calling, and the seam back to the agent.

Two definitions the rest of this surface needs and that neither the wire format
nor the router owns: what a caller is — an issued key, or nobody — and the one
method the router consumes from the agent loop.

They live apart from both so the router can import the translation and the turn
paths without any of those importing the router back. Nothing here decides
anything: a caller carries the profile that does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from personacore.agent.loop import AgentEvent, TurnRequest
from personacore.api.keys import ApiKeyRecord
from personacore.contracts.policy import PolicyProfile

# ---------------------------------------------------------------------------
# Who is calling — a key, or nobody (ADR-0018)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeylessCaller:
    """A request admitted with no valid key, carrying the anonymous profile.

    A separate type from :class:`~personacore.api.keys.ApiKeyRecord` because
    that model *refuses* an anonymous profile, deliberately and correctly: an
    issued key must never be able to launder its traffic into the anonymous
    tier. Keyless is the other direction — no key at all — and it is honest
    about it here rather than by relaxing what a key may hold.

    It carries the same two attributes the rest of this module reads off a key,
    so nothing downstream has to ask which kind of caller it has: the profile
    decides, exactly as it does for a key, and ``key_id`` names the caller in
    the audit trail.
    """

    profile: PolicyProfile
    key_id: str = "keyless"


Caller = ApiKeyRecord | KeylessCaller
"""Everything this router will answer for. Both halves expose ``profile`` and
``key_id``; nothing else about them is consulted."""


# ---------------------------------------------------------------------------
# The seam back to the agent
# ---------------------------------------------------------------------------


class TurnRunner(Protocol):
    """The one method this router needs from the agent loop.

    A Protocol rather than a concrete import for the same reason
    ``personacore.agent.protocols`` uses them: it names exactly the capability
    consumed — run a turn, get events — so the router can be exercised without
    a persona store, an LLM or a database, and so nothing here can quietly
    start calling something further down the stack.
    """

    def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        ...
