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

    prompt_prefix: str = ""
    """Placed in front of the text of every picture request, with one space
    between. Empty adds nothing. Some generators want a fixed lead-in before
    the description proper; this is where it goes, and it never appears in
    the transcript."""

    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=600.0, gt=0)
    """How long to wait for the picture. **This is the whole answer, not a
    chunk of one.**

    A generator sends nothing at all until the image is finished, so this
    single read has to outlast the entire render — unlike a chat reply, where
    a token arriving every second keeps resetting the clock. That difference
    is what makes a value borrowed from ``[llm.*]`` wrong here.

    **Measured, 2026-09-03**, against the configured generator:
    a 2048x2048 render answered in 185 seconds end to end over HTTP. The
    default was 120 and every such request would have failed on a timeout
    while the server was still working — the picture completes and is thrown
    away by the caller.

    Six hundred, so a slower prompt, a busier card, or a larger size has room
    without anybody editing a file. A generator that has genuinely stopped is
    caught by ``total_timeout_seconds`` below, which is the clock that does
    not reset; this one only needs to be longer than a real render."""

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
