"""Agent-loop exceptions — spec section 10 (graceful degradation).

Every failure the agent loop can hit ends up spoken aloud by TTS, so these
mirror ``personacore.llm.errors``: a ``spoken_message`` fit to be read out
verbatim, and a ``detail`` that carries the operator-facing specifics into the
structured log and never into the room.

The loop itself catches these — nothing in this module should escape
``AgentLoop.run_turn``. They exist so the persona loader and the risk gate can
fail with a sentence instead of a traceback.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base for every error raised inside the agent package."""

    def __init__(self, spoken_message: str, *, detail: str | None = None) -> None:
        super().__init__(spoken_message)
        self.spoken_message = spoken_message
        self.detail = detail


class PersonaError(AgentError):
    """Something is wrong with a persona on disk, spec section 5.5."""


class PersonaNotFoundError(PersonaError):
    """No persona directory of that name inside ``<appdata>/personas/``.

    Also raised for a name that would escape the personas directory — a
    persona name arrives from config, a policy profile, or a voice command,
    which spec section 7 classes as untrusted input, so "not found" is the
    right answer to ``../../etc`` rather than a different, more interesting
    error.
    """


class PersonaInvalidError(PersonaError):
    """The persona directory exists but cannot be read as a persona — no
    prompt file, unreadable metadata, or an empty system prompt."""


__all__ = [
    "AgentError",
    "PersonaError",
    "PersonaInvalidError",
    "PersonaNotFoundError",
]
