"""MQTT event bus client — spec section 5.2.

The core holds one subscription, to everything beneath the event root, and
decides per rule (see ``rules.py``) what each message deserves. Plugins publish;
the core listens. No plugin ever addresses the core directly, which is what lets
a new publisher appear without the core knowing anything about it.

Two things this module refuses to do, both from spec section 7:

- **It never trusts a payload.** Anything on the bus came from outside. It is
  parsed into the versioned envelope and handed on as data; a malformed message
  is dropped and counted, never allowed to raise into the core's main loop.
- **It never lets the broker take the core down.** Spec section 10 requires
  graceful degradation: with the broker unreachable the assistant loses its push
  channel and keeps doing everything else, reconnecting in the background.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

import aiomqtt
import structlog
from pydantic import SecretStr, ValidationError

from personacore.bus.rules import EventAction, EventRules
from personacore.contracts.events import TOPIC_ROOT, EventEnvelope

if TYPE_CHECKING:
    from personacore.config.settings import BusSettings

log = structlog.get_logger(__name__)

SUBSCRIPTION = f"{TOPIC_ROOT}/#"
"""One subscription covers every event. Spec section 5.2's topic convention is
what makes that possible."""

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


SPACE_MARK = "␣"
"""OPEN BOX (U+2423), the conventional printed space, and the fallback for any
other whitespace character with no mark of its own."""

_WHITESPACE_MARKS = {"\t": "⇥", "\n": "⏎", "\r": "⏎"}
"""Visible stand-ins for the characters that make a wrong address look right.

A trailing space in a host name is invisible on a screen and produces a DNS
failure on an IP literal — which is nonsense on its face, and therefore an hour
of looking everywhere except at the value.
"""


def visible(value: str) -> str:
    """One configured value, quoted, with any whitespace in it drawn.

    Used for every address-ish thing the health screen prints. The quotes bound
    the value so an empty string is not a gap on the page, and the marks make a
    stray space something an operator *sees* rather than something they have to
    suspect.
    """
    marked = "".join(
        _WHITESPACE_MARKS.get(char, SPACE_MARK if char.isspace() else char)
        for char in value
    )
    return f'"{marked}"'


def has_whitespace(value: str) -> bool:
    """Whether a value carries whitespace no host name or IP address ever has."""
    return any(char.isspace() for char in value)


def bus_address(facts: Mapping[str, object]) -> str:
    """``"broker":1883`` — the pair, as the MQTT client will use them.

    Takes the health facts rather than two arguments so that every screen
    naming the broker names the same two keys out of the same dictionary. A
    caller that pulled ``host`` and forgot ``port`` is how a health line comes
    to be confidently half-right.
    """
    return f"{visible(str(facts.get('host', '')))}:{facts.get('port', '')}"


class BusHealth:
    """What the section 9 health dashboard needs to know about the bus.

    Counters and connection state, **and what the bus is pointed at**. The
    second half is the more useful one: "not connected: Name or service not
    known" tells an operator that something failed, and nothing whatever about
    whether the address they think they configured is the address this process
    is dialling. Between a typo in the file, a save that never reached the live
    object and a container still running yesterday's config, those are three
    different problems with one error message, and only the running value
    separates them.
    """

    def __init__(self, describe: Callable[[], dict[str, object]] | None = None) -> None:
        self.connected: bool = False
        self.last_error: str | None = None
        self.received: int = 0
        self.malformed: int = 0
        self.published: int = 0
        self.reconnects: int = 0
        self._describe = describe
        """Called at read time, not copied at construction.

        The whole value of reporting the address is that it is the *running*
        one, so it is fetched from the live :class:`EventBus` whenever somebody
        asks. A snapshot taken here would go stale at the next ``reconfigure``
        and would then be a confident, wrong answer — worse than none.
        """

    def target(self) -> dict[str, object]:
        """The broker this bus would dial right now, or ``{}`` if unattached."""
        return dict(self._describe()) if self._describe is not None else {}

    def as_dict(self) -> dict[str, object]:
        """Address first: it is the thing an operator has to check before the
        counters mean anything."""
        return {
            **self.target(),
            "connected": self.connected,
            "last_error": self.last_error,
            "received": self.received,
            "malformed": self.malformed,
            "published": self.published,
            "reconnects": self.reconnects,
        }


class EventBus:
    """Publishes and receives events, and survives the broker going away.

    Construction takes settings and secrets already resolved — this module never
    reads a config file or a secret store itself, so it stays testable against a
    fake broker and cannot become a second place that knows the appdata layout.
    """

    def __init__(
        self,
        settings: BusSettings,
        *,
        rules: EventRules | None = None,
        password: SecretStr | None = None,
        on_wake: EventHandler | None = None,
        client_factory: Callable[[], aiomqtt.Client] | None = None,
        reconnect_seconds: float = 5.0,
    ) -> None:
        self._settings = settings
        self._rules = rules or EventRules()
        self._password = password
        """Kept wrapped, and unwrapped only in ``_default_client``, the same way
        the LLM client keeps its API key. The redaction in
        ``personacore.audit.logging`` matches on key NAMES, so a bare plaintext
        string carried around in an attribute is the one shape it cannot catch
        if it ever reaches a log line or a repr."""
        self._on_wake = on_wake
        # Injectable so tests never need a broker. Production passes nothing.
        self._client_factory = client_factory or self._default_client
        self._reconnect_seconds = reconnect_seconds

        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        """Serialises start, stop and reconfigure against each other.

        Without it, a settings save arriving while a previous one is still
        swapping brokers can interleave the two halves of a reconnect and leave
        a listener running against settings nobody asked for — or, worse, race
        shutdown and resurrect the bus after the app has stopped it.
        """
        self.health = BusHealth(self._target)

    def _target(self) -> dict[str, object]:
        """What the next connection will use, read live off this object.

        Deliberately not read back out of ``core.toml``: the point of putting
        this on the health screen is to expose the case where the file and the
        process disagree, and a reader that consulted the file could not show
        that difference by construction.

        ``password_set`` and nothing else about the password. Not its length,
        not a masked form of it, not the name of the secret it came from — spec
        section 7, and "a password is set" is the entire fact an operator needs
        to tell an authentication failure from an address failure.
        """
        settings = self._settings
        return {
            "host": settings.host,
            "port": settings.port,
            "client_id": settings.client_id,
            "username": settings.username,
            "password_set": self._password is not None,
        }

    def _default_client(self) -> aiomqtt.Client:
        return aiomqtt.Client(
            hostname=self._settings.host,
            port=self._settings.port,
            username=self._settings.username,
            password=self._password.get_secret_value() if self._password else None,
            identifier=self._settings.client_id,
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin listening. Returns immediately; connection happens in the
        background so a missing broker never blocks core startup."""
        async with self._lifecycle:
            self._start()

    async def stop(self) -> None:
        async with self._lifecycle:
            await self._stop()

    async def reconfigure(
        self, settings: BusSettings, *, password: SecretStr | None = None
    ) -> bool:
        """Point the bus at a different broker, live. Returns whether it did.

        ADR-0010: a setting saved in the admin UI takes effect now. The broker
        address was the one component a save never reached — ``_default_client``
        reads these values at connect time, but nothing ever gave the bus new
        ones, so a re-pointed broker stayed re-pointed only in the config file.

        **It reconnects only when the configuration genuinely differs.** Saving
        an unrelated setting must not drop a healthy subscription, so sameness
        is the whole :class:`BusSettings` value *and* the resolved password —
        the latter because a secret's name can stay put while its contents
        change, and because a secret that was missing at boot and has since
        been created is a real change with no visible one in the config.

        Degradation is unchanged (spec section 10): the new broker is contacted
        by the background task exactly as at startup, so an address that does
        not answer costs the push channel and nothing else, and this never
        raises on the caller's behalf.
        """
        async with self._lifecycle:
            if settings == self._settings and password == self._password:
                return False
            # `_task is not None` is the whole test for "was listening": if
            # shutdown got here first the task is already gone, and this must
            # not bring the bus back up behind it. The lock is what makes that
            # check trustworthy rather than a guess about ordering.
            running = self._task is not None
            if running:
                await self._stop()
            self._settings = settings
            self._password = password
            # The old address's failure is not news about the new one. Health
            # would otherwise show an error naming a broker nobody is trying to
            # reach any more until the next connect attempt overwrote it.
            self.health.last_error = None
            log.info("bus_reconfigured", host=settings.host, port=settings.port)
            if running:
                self._start()
            return True

    def _start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="event-bus")

    async def _stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._client = None
        self.health.connected = False

    async def _run(self) -> None:
        first = True
        while not self._stopping.is_set():
            try:
                if not first:
                    self.health.reconnects += 1
                first = False
                async with self._client_factory() as client:
                    self._client = client
                    self.health.connected = True
                    self.health.last_error = None
                    await client.subscribe(SUBSCRIPTION)
                    log.info("bus_connected", host=self._settings.host, topic=SUBSCRIPTION)
                    async for message in client.messages:
                        await self._handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Deliberately broad: the bus is a degradable dependency, and an
                # unexpected client error must not escape into the core's main
                # loop. Spec section 10 — the assistant keeps working without it.
                self._client = None
                self.health.connected = False
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("bus_disconnected", error_type=type(exc).__name__)
            if not self._stopping.is_set():
                await asyncio.sleep(self._reconnect_seconds)

    # -- receiving ---------------------------------------------------------

    async def _handle(self, message: aiomqtt.Message) -> None:
        self.health.received += 1
        envelope = self._parse(message)
        if envelope is None:
            return

        action = self._rules.decide(envelope)
        if action is EventAction.IGNORE:
            return

        # Note what is logged: source, type and id — never `data`. A payload can
        # carry anything a camera or a chat bridge put in it, and spec section 7
        # treats it as untrusted content, not as something to splash into logs.
        log.info(
            "bus_event",
            source=envelope.source,
            event_type=envelope.type,
            event_id=str(envelope.event_id),
            action=action.value,
        )
        if action is EventAction.WAKE and self._on_wake is not None:
            try:
                await self._on_wake(envelope)
            except Exception as exc:
                # A handler that throws must not kill the subscription loop and
                # lose every subsequent event.
                log.warning(
                    "bus_handler_failed",
                    source=envelope.source,
                    event_type=envelope.type,
                    error_type=type(exc).__name__,
                )

    def _parse(self, message: aiomqtt.Message) -> EventEnvelope | None:
        """Turn a raw message into an envelope, or drop it.

        Every failure path here is a drop-and-count rather than a raise. A
        publisher with a bug, or something hostile putting rubbish on the topic,
        must not be able to stop the core receiving everything else.
        """
        payload = message.payload
        if isinstance(payload, (bytes, bytearray)):
            try:
                text = bytes(payload).decode("utf-8")
            except UnicodeDecodeError:
                self._count_malformed(message, "payload is not valid UTF-8")
                return None
        elif isinstance(payload, str):
            text = payload
        else:
            self._count_malformed(message, "payload is not text")
            return None

        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            self._count_malformed(message, "payload is not valid JSON")
            return None
        if not isinstance(raw, dict):
            self._count_malformed(message, "payload is not a JSON object")
            return None

        try:
            return EventEnvelope(**raw)
        except ValidationError:
            self._count_malformed(message, "payload is not a valid event envelope")
            return None

    def _count_malformed(self, message: aiomqtt.Message, reason: str) -> None:
        self.health.malformed += 1
        # The topic is safe to log; the payload is not.
        log.warning("bus_malformed_event", topic=str(message.topic), reason=reason)

    # -- publishing --------------------------------------------------------

    async def publish(self, envelope: EventEnvelope) -> bool:
        """Publish an event. Returns whether it went out.

        Returns False rather than raising when the broker is away: a caller
        publishing an event should not have to defend against the bus being
        down, and the health record already carries the reason.
        """
        client = self._client
        if client is None or not self.health.connected:
            log.warning(
                "bus_publish_skipped",
                source=envelope.source,
                event_type=envelope.type,
                reason="not connected to the broker",
            )
            return False
        try:
            await client.publish(envelope.topic, envelope.model_dump_json().encode("utf-8"))
        except Exception as exc:
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("bus_publish_failed", error_type=type(exc).__name__)
            return False
        self.health.published += 1
        return True
