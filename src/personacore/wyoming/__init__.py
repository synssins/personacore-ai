"""PersonaCore as a Wyoming provider — Home Assistant's speech, served here.

Wyoming is the peer-to-peer protocol Home Assistant's voice pipeline speaks to
its speech services. **We are the provider and Home Assistant is the client:**
it connects to this port, asks what we are, and then uses this core as its
speech-to-text and its text-to-speech. One listener carries both halves,
because one ``info`` describing both is what makes Home Assistant create two
entities from one connection.

Nothing in this package decides anything about speech or hearing. It converts
between the wire and the two contracts the core already has, and takes both of
them as arguments (:mod:`personacore.wyoming.seams`).

One module here points the other way. :mod:`personacore.wyoming.client` is a
Wyoming *client*, used by the admin UI to speak a chat reply through this
core's own listener on loopback — the only thing in the project that drives the
streaming synthesis the way Home Assistant does, and therefore the only thing
that can find out whether it works. It lives beside the handler because what it
knows is the protocol, and the two have to agree exactly. **The host it dials
is a constant** and nothing above it can supply one; see that module.

Security — read this before turning it on
-----------------------------------------

**The protocol has no authentication, no authorisation and no encryption, by
design.** The upstream project's own SECURITY.md states the position plainly:
anything that can reach a Wyoming service can use it. There is no credential to
present, none to check, and no proxy that could usefully stand in front of it,
because there is nothing in the conversation for a proxy to authenticate.

In practice: **anyone who can reach this port can transcribe audio through
PersonaCore and make it speak, with no credential of any kind.** That is why
``[wyoming] enabled`` defaults to ``false`` and ``host`` defaults to
``127.0.0.1``. Put it on the container network Home Assistant is on. Never
publish it, and never put it on a network you do not own.

The one thing this package does about that: failures go back as a plain,
detail-free sentence, and the detail goes to the log. Whoever is on an
unauthenticated port is not owed a file path or a model name.
"""

from personacore.wyoming.describe import build_info
from personacore.wyoming.handler import PersonaCoreEventHandler
from personacore.wyoming.seams import HearingSource, VoiceSource
from personacore.wyoming.service import WyomingService

__all__ = [
    "HearingSource",
    "PersonaCoreEventHandler",
    "VoiceSource",
    "WyomingService",
    "build_info",
]
