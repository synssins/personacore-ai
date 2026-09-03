"""The contracts. Spec section 4.1: these are the product, features are cargo.

Everything in this package is versioned and additive-only. Changing a field's
meaning, removing one, or tightening a constraint is a breaking change and needs
a contract major version plus an ADR — not a patch.
"""

from personacore.contracts.events import EventEnvelope, event_topic
from personacore.contracts.manifest import (
    PluginManifest,
    RiskLevel,
    SecretRequest,
    Transport,
)
from personacore.contracts.policy import MemoryScope, PolicyProfile, ProfileKind

__all__ = [
    "EventEnvelope",
    "MemoryScope",
    "PluginManifest",
    "PolicyProfile",
    "ProfileKind",
    "RiskLevel",
    "SecretRequest",
    "Transport",
    "event_topic",
]
