"""Event bus envelope and topic conventions — spec section 5.2.

MCP tool calls are the *pull* channel. This is the *push* channel: the world
telling the assistant something happened. Everything on the bus carries a
versioned envelope so a subscriber written today still parses a message
published by a plugin written in three years.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOPIC_ROOT = "personacore/events"
"""All events live under this root. A single wildcard subscription beneath it is
the core's only subscription."""

ENVELOPE_VERSION = 1
"""Bumped only for a breaking envelope change. Additive fields do not bump it."""

_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _check_segment(value: str, what: str) -> str:
    # MQTT wildcards inside a topic segment would let one publisher subscribe to
    # or spoof another's traffic, so they are rejected rather than escaped.
    if "+" in value or "#" in value or "/" in value:
        raise ValueError(f"{what} {value!r} must not contain a slash, plus or hash")
    if not _SEGMENT_RE.match(value):
        raise ValueError(
            f"{what} {value!r} must be lowercase letters, digits, dots, hyphens "
            "or underscores, and start with a letter or digit"
        )
    return value


def event_topic(source: str, event_type: str) -> str:
    """Build the topic for an event: <root>/<source>/<type>."""
    _check_segment(source, "event source")
    _check_segment(event_type, "event type")
    return f"{TOPIC_ROOT}/{source}/{event_type}"


class EventEnvelope(BaseModel):
    """The versioned wrapper every event on the bus carries.

    `data` is deliberately untyped at this layer. Spec section 7 is explicit that
    anything arriving from outside is untrusted input and is data, never
    instructions — so the core treats the payload as opaque and hands it to the
    agent quoted, rather than parsing meaning out of it here.
    """

    model_config = ConfigDict(extra="forbid")

    envelope_version: int = ENVELOPE_VERSION
    event_id: UUID = Field(default_factory=uuid4)

    source: str
    """Who published it — usually a plugin name."""

    type: str
    """What happened, in the publisher's own vocabulary, e.g. person-detected."""

    timestamp: datetime
    """When it happened, not when it was received. Timezone-aware, always."""

    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        return _check_segment(v, "event source")

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return _check_segment(v, "event type")

    @field_validator("timestamp")
    @classmethod
    def _require_tz(cls, v: datetime) -> datetime:
        # A naive timestamp from a plugin in another container is ambiguous, and
        # the audit log (spec section 7) is only useful if its ordering is real.
        if v.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        return v

    @property
    def topic(self) -> str:
        return event_topic(self.source, self.type)
