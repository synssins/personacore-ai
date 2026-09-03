"""Conversations: a person's transcript as separate, listable, resumable
threads rather than one rolling history.

The split is deliberate and load-bearing. :mod:`.models` is pure vocabulary
with no idea that a database exists, because
:mod:`personacore.audit.store` imports it *from inside a schema migration* and
a migration that reached for a service layer would be an import cycle waiting
for the first cold start to find it. :mod:`.service` is the other direction:
it knows nothing about SQL and everything about not raising at a surface.
"""

from __future__ import annotations

from personacore.conversations.addressing import (
    FLOOR_MAX_TOKENS,
    FLOOR_NO_THINKING,
    FLOOR_QUESTION,
    FloorAnswer,
    addressed,
    claims_floor,
    repeats,
)
from personacore.conversations.models import (
    MAX_ROSTER,
    MAX_TITLE_LENGTH,
    SESSION_GAP,
    UNTITLED,
    Author,
    AuthorKind,
    Conversation,
    ConversationKind,
    ConversationOrigin,
    derive_title,
    new_conversation_id,
    roster_of,
)
from personacore.conversations.service import (
    ConversationService,
    ConversationStore,
    PersonaAwareStore,
    RoomStore,
    RosterStore,
)

__all__ = [
    "FLOOR_MAX_TOKENS",
    "FLOOR_NO_THINKING",
    "FLOOR_QUESTION",
    "MAX_ROSTER",
    "MAX_TITLE_LENGTH",
    "SESSION_GAP",
    "UNTITLED",
    "Author",
    "AuthorKind",
    "Conversation",
    "ConversationKind",
    "ConversationOrigin",
    "ConversationService",
    "ConversationStore",
    "FloorAnswer",
    "PersonaAwareStore",
    "RoomStore",
    "RosterStore",
    "addressed",
    "claims_floor",
    "derive_title",
    "new_conversation_id",
    "repeats",
    "roster_of",
]
