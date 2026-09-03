"""The image generation service's settings — ``[image]`` in ``core.toml``.

docs/contracts/image-conversations.md §4: an image conversation's `kind`
selects a *different responder*, not a mode of the LLM — a service that takes
a prompt and hands back a picture, reached over HTTP the same way any
``[llm.*]`` role is. It is not an LLM and does not belong under ``[llm.*]``:
the wire shape is different (``/v1/images/generations``, not chat
completions), the cost curve is different, and CLAUDE.md's CPU-only
constraint is exactly why this project reaches an image model this way at
all — a GPU-capable model runs elsewhere and is reached as a plugin-shaped
HTTP service, never in-process.

**Absent — the default — means unconfigured, not a guess.** Every
``[llm.*]`` role but the vision one falls back to ``[llm.interactive]``
(ADR-0011), and ``[llm.interactive]`` itself defaults to a guessable
``localhost:8080`` because conversation is this core's whole reason to exist.
Neither is true here: there is no other role for an image request to fall
back to, and no address this core could assume an image service is
listening on. ``base_url: None`` is therefore the honest starting state, and
the contract's own instruction (§9, and the build brief that cites it) is
explicit about what an unconfigured core does with an image conversation: say
so, in the thread, plainly — never crash, never answer with silence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ImageSettings(BaseModel):
    """``[image]`` — the image generation service."""

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    """The image service's root, e.g. ``http://localhost:7860`` — this
    module's own client (:mod:`personacore.images.client`) appends
    ``/v1/images/generations`` to it. ``None`` means the feature is not
    configured at all; see the module docstring for why that is the honest
    default rather than a guessed address."""

    model: str | None = None
    """A model name to pass through, for a server hosting more than one.
    ``None`` — the common case, one model loaded — omits the field from the
    request entirely rather than sending an empty string the server would
    have to reject or ignore."""

    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=120.0, gt=0)
    """Generous relative to a chat completion's own timeout
    (``[llm.*].read_timeout_seconds``): even on the CPU-only hardware this
    project requires (CLAUDE.md's hard constraint), one image is a slower,
    but boundable, answer — unlike a chat reply's open-ended token stream,
    there is one response to wait for and then it is over."""

    total_timeout_seconds: float = Field(default=900.0, gt=0)
    """The whole request's ceiling, from the first byte out to the last byte
    in.

    **Not the same promise as** ``read_timeout_seconds``, which is per-read:
    a server dribbling one byte inside every read window satisfies it forever
    and holds the connection — and the worker waiting on it — open with no
    end. This is the clock that does not reset, so a generator that is
    hostile, compromised or simply stuck costs one request and then stops
    costing anything.

    Fifteen minutes, because CPU-only is this project's hard constraint and a
    large picture generated without a GPU genuinely takes minutes. It is a
    ceiling on a pathological case, not a target: a working generator answers
    inside ``read_timeout_seconds`` and never comes near this.
    """

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, value: str | None) -> str | None:
        """Store the root with no trailing slash, or ``None`` for
        unconfigured — a field left as whitespace is still unconfigured, not
        a URL nobody meant to type."""
        if value is None:
            return None
        trimmed = value.strip().rstrip("/")
        return trimmed or None

    def is_configured(self) -> bool:
        """Whether an operator has actually pointed this at a service."""
        return self.base_url is not None


__all__ = ["ImageSettings"]
