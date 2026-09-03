"""The listener — starting and stopping one Wyoming TCP server.

``start()`` and never ``run()``. Two reasons, both learned the hard way rather
than chosen for taste:

* ``run()`` installs its own ``SIGTERM`` handler and would then be fighting
  uvicorn for signal ownership inside the container.
* ``run()`` blocks, so it has to live on a task, and a task that dies takes the
  failure with it. ``start()`` returns once the socket is listening, which
  means a port already in use surfaces where the operator is looking instead of
  as a silence where speech used to be.
"""

from __future__ import annotations

import asyncio
from functools import partial

import structlog
from wyoming.info import Info
from wyoming.server import AsyncTcpServer

from personacore.config.wyoming import WyomingSettings
from personacore.wyoming.describe import build_info
from personacore.wyoming.handler import PersonaCoreEventHandler
from personacore.wyoming.seams import HearingSource, VoiceSource

log = structlog.get_logger(__name__)


class WyomingService:
    """PersonaCore as a Wyoming provider, on one port, for both halves.

    Home Assistant creates two entities — a speech-to-text one and a
    text-to-speech one — from a single ``info``, so a second listener would be
    two of everything for no gain. 10300 by convention; the pairing with 10200
    is a convention of separate services, not a rule of the protocol.

    Neither half is imported here. ``hearing`` and ``voices`` arrive as
    arguments so the tests can drive the real socket with fakes on the far
    side, and so this package cannot take a decision that belongs to speech or
    to hearing.
    """

    def __init__(
        self,
        settings: WyomingSettings,
        *,
        hearing: HearingSource | None = None,
        voices: VoiceSource | None = None,
        version: str | None = None,
    ) -> None:
        self._settings = settings
        self._hearing = hearing
        self._voices = voices
        self._version = version
        self._server: AsyncTcpServer | None = None
        # One pair of locks for the whole service, not one per connection:
        # connections are unlimited and concurrent, and the models beneath are
        # not promised to be re-entrant.
        self._hearing_lock = asyncio.Lock()
        self._speech_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> int | None:
        """The port actually listening, which is not always the one asked for.

        Port 0 means "any free port", which is how the tests get one that is
        certainly free. Reaching into the library's socket is the only way to
        learn which one that turned out to be.
        """
        server = getattr(self._server, "_server", None)
        sockets = getattr(server, "sockets", None) or ()
        for socket in sockets:
            return int(socket.getsockname()[1])
        return None

    def info(self) -> Info:
        """The ``info`` this service would answer ``describe`` with, now."""
        return build_info(hearing=self._hearing, voices=self._voices, version=self._version)

    async def start(self) -> bool:
        """Listen, and say whether anything is now listening.

        ``False`` when the setting is off — which is the common case, and not a
        failure. A bind that fails **raises**: the operator asked for a port
        and did not get it, and reporting that as a log line nobody reads is
        how a service ends up quietly absent from Home Assistant.
        """
        if not self._settings.enabled:
            return False
        if self._server is not None:
            return True

        server = AsyncTcpServer(self._settings.host, self._settings.port)
        factory = partial(
            PersonaCoreEventHandler,
            info_factory=self.info,
            hearing=self._hearing,
            voices=self._voices,
            hearing_lock=self._hearing_lock,
            speech_lock=self._speech_lock,
        )
        # `start`, not `run` — see the module docstring.
        await server.start(factory)
        self._server = server
        log.info(
            "wyoming_started",
            host=self._settings.host,
            port=self.bound_port,
            # Said at startup because the protocol has no authentication of any
            # kind: whoever reaches this port can transcribe through this core
            # and make it speak, with no credential.
            unauthenticated=True,
            asr=bool(self._hearing),
            tts=bool(self._voices),
        )
        return True

    async def stop(self) -> None:
        """Close the listener and every connection on it. Never raises."""
        server, self._server = self._server, None
        if server is None:
            return
        try:
            await server.stop()
        except Exception as exc:
            # Shutdown reporting a failure it cannot act on helps nobody, and
            # speech may never be the reason the core will not stop.
            log.warning("wyoming_stop_failed", error=repr(exc))
        else:
            log.info("wyoming_stopped")
