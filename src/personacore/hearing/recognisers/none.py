"""A recogniser that hears nothing, deliberately.

This is the frame's test article, not a stand-in for a real one. It exists so
that the registry, the settings, the Wyoming surface and the tests all have
something *real* to switch on, switch off and hand audio to before an actual
recogniser lands — a socket that has never had anything plugged into it is a
socket nobody has proved.

It is always available and it always succeeds. Handed audio it returns
``Transcript(text="")``, which is the same answer a real recogniser gives for
silence: **empty is a success**, not an error. A surface that treats an empty
transcript as a failure is broken against this recogniser and will be broken
against the real one on the first quiet room, which is the point of shipping
this one first.

It loads nothing on ``start`` and holds nothing between calls, so it is also
the only recogniser for which "off means off" is trivially true — which makes
it a poor test of that property and an excellent baseline for the ones that
are not.
"""

from __future__ import annotations

from typing import Any

from personacore.hearing.engine import Audio, Transcript


class NullRecogniser:
    """Always available, always silent."""

    id = "none"
    display = "Nothing (no recogniser)"
    available = True
    """Always. It needs no runtime, no model and no hardware, so there is no
    build and no machine on which it cannot run — which is what makes it the
    thing to point at when every other recogniser reports itself unavailable."""

    unavailable_reason: str | None = None

    def start(self) -> None:
        """Nothing to load."""

    def stop(self) -> None:
        """Nothing to release."""

    def transcribe(self, audio: Audio, **knobs: Any) -> Transcript:
        """No words, and that is an answer rather than a fault."""
        return Transcript(text="")
