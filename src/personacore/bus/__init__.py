"""The event bus — spec section 5.2.

MCP tool calls are the pull channel. This is the push channel: cameras,
schedules and door sensors telling the assistant something happened, rather than
the assistant having to ask.
"""

from personacore.bus.client import SUBSCRIPTION, BusHealth, EventBus, EventHandler
from personacore.bus.rules import EventAction, EventRule, EventRules

__all__ = [
    "SUBSCRIPTION",
    "BusHealth",
    "EventAction",
    "EventBus",
    "EventHandler",
    "EventRule",
    "EventRules",
]
