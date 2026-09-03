"""What the core does when an event arrives — spec section 5.2.

The bus is the push channel: the world telling the assistant something happened.
Most of what arrives does not deserve the assistant's attention. A motion sensor
that fires two hundred times a day must not wake the agent two hundred times, and
deciding that in the core — rather than in each publisher — is what keeps
publishers dumb and the policy in one reviewable place.

Three outcomes, per spec section 5.2: ignore it, log it silently, or wake the
agent. Rules are configuration, not code: the core never learns the vocabulary of
any particular plugin (spec section 13.5 — if the core contains the string
"spotify", something has gone wrong).
"""

from __future__ import annotations

import fnmatch
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from personacore.contracts.events import EventEnvelope


class EventAction(StrEnum):
    IGNORE = "ignore"
    """Dropped without a trace. For genuinely high-volume noise."""

    LOG = "log"
    """Recorded and visible in the trace view, but the agent never sees it."""

    WAKE = "wake"
    """Handed to the agent, which decides whether to act or announce."""


class EventRule(BaseModel):
    """One rule. First match wins, so order is meaningful."""

    model_config = ConfigDict(extra="forbid")

    match: str
    """Glob against "<source>/<type>" — e.g. `doorbell/*`, `*/person-detected`."""

    action: EventAction
    note: str = ""
    """Why this rule exists. Shown in the admin UI beside the rule, because a
    rule nobody remembers the reason for is one nobody dares delete."""


class EventRules(BaseModel):
    """The ordered rule list plus the fallback.

    The default is LOG, deliberately. IGNORE would make an unrecognised event
    vanish silently, which is how a camera stops working for a fortnight before
    anyone notices; WAKE would let any publisher interrupt the household by
    inventing a new event type. Logging keeps it visible and harmless.
    """

    model_config = ConfigDict(extra="forbid")

    rules: list[EventRule] = Field(default_factory=list)
    default: EventAction = EventAction.LOG

    def decide(self, envelope: EventEnvelope) -> EventAction:
        subject = f"{envelope.source}/{envelope.type}"
        for rule in self.rules:
            if fnmatch.fnmatchcase(subject, rule.match):
                return rule.action
        return self.default
