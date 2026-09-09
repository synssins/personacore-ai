"""The machine registry — where a workstation lives, and how it dies.

One plugin called ``workstation`` holds every enrolled machine. A machine is a
row inside that plugin's own folder, never a plugin of its own and never a row
in core state. That is not a storage preference; it is the whole boundary:

    **Uninstall the plugin and every machine goes with it.**

Two calls are the teardown path — :func:`personacore.plugins.packages.uninstall_package`
removes the plugin's folder, and :meth:`personacore.config.secrets.SecretStore.delete_namespace`
removes everything that plugin owned in the secret store. **Anything about a
machine that those two do not destroy is a defect**, and this module is written
backwards from that sentence.

Where the two halves live, and nothing else does:

* ``<appdata>/plugins-http/workstation/machines.toml`` — the records. Inside the
  plugin's own folder, so ``uninstall_package`` takes it with the rest.
* ``<appdata>/secrets/plugins/workstation/machine_<id>_token`` — the per-machine
  bearer token. One namespace, N names, so ``delete_namespace`` takes all of
  them in one call.

Four rules this module keeps, each of which is a leak if it is broken later:

* **A record holds the *name* of its secret, never the value.** ``token_secret``
  is a lookup key; the token itself is only ever handed to
  :meth:`SecretStore.set`. Written text is checked for credential-shaped keys
  on the way in *and* on the way out, the same both-directions rule
  ``admin.plugin_config_io`` applies to a plugin's ``config.toml`` — with one
  addition, because the shared check descends into tables and not into arrays
  of tables, and every machine here is an entry in one.
* **Nothing in this module logs a machine's name, address or id.** The core's
  log file lives under appdata, is never rotated and is not touched by either
  half of teardown, so a machine named in a log line outlives the plugin
  exactly the way an audit row does. Log lines here carry the plugin name and
  counts.
* **Online is read, never polled.** It is the core's own connection state,
  supplied by whoever owns the connections, because a separate liveness check
  is a second source of truth that can disagree with the first.
* **A separate file from ``config.toml``, on purpose.** The plugin's
  ``config.toml`` is the operator's document — carried verbatim so its comments
  survive, and shown in the settings editor. These records are written by the
  core, hold no operator prose, and are added and removed by clicking. Keeping
  them apart means the machine-by-machine detail never reaches the generic
  config editor, and never reaches the ``plugins.config.update`` audit record
  that names a saved document's top-level keys.

Not here, deliberately: enrolment itself, the name-or-address resolver, any UI,
and the log read path. This module owns where a machine lives and how it dies.
"""

from __future__ import annotations

import os
import re
import secrets as secrets_module
import threading
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w

from personacore.audit import get_logger
from personacore.config.appdata import AppdataLayout
from personacore.config.secrets import SecretError, SecretStore, check_secret_name

logger = get_logger(__name__)

PLUGIN_NAME = "workstation"
"""The one plugin every machine belongs to. There is never a second one, and a
machine never becomes one — owner decision, 2026-09-09."""

MACHINES_FILENAME = "machines.toml"
"""Beside ``config.toml`` in the plugin's own folder. See the module docstring
for why it is not *in* ``config.toml``."""

SCHEMA_VERSION = 1
"""Written into every file and checked on every read. A file from a newer core
is refused rather than half-understood: a record this core cannot read whole is
a machine it would silently drop, and a dropped machine is a token left live on
somebody's desktop with nothing referring to it."""

SECRET_PREFIX = "machine_"  # noqa: S105 - a name fragment, not a credential
SECRET_SUFFIX = "_token"  # noqa: S105 - a name fragment, not a credential

MAX_MACHINES = 32
"""A household, not a fleet — the same judgement the pairing store is built on.
The cap exists so an unbounded write loop cannot fill the appdata volume, not
because anybody is expected to reach it."""

MAX_ADDRESSES = 8
"""How many addresses one machine may listen on. A machine with an IPv4 and an
IPv6 address on each of two interfaces is four; eight is room to be wrong about
that without becoming a place to store a list."""

ID_BYTES = 8
"""16 lowercase hex characters. Opaque on purpose: the secret's name is built
from the id and not from what the machine calls itself, so no machine name is
ever spelled as a filename in the secret store, and renaming one later cannot
orphan its token."""


class MachineRejected(Exception):
    """A registry call was refused, in words meant for the owner.

    Carries ``status_code`` so the admin surface can answer with the right one
    without re-deciding what a refusal means. It is a plain integer rather than
    an HTTP enum because this module builds no route and knows about no
    framework.

    **Two ways of saying why, for two different audiences.** ``message`` is the
    sentence, written for whoever hit the refusal, and it may name a machine —
    it goes back over the wire to the person standing at the keyboard and is not
    stored anywhere. ``reason`` is a short stable code, and it is what may be
    written into an audit row or a log line, both of which are core state that
    outlives the plugin. A collision has to say *what it collided with* to be
    worth reading; that sentence names an **already enrolled** machine, so the
    sentence is answered and the code is recorded.
    """

    def __init__(
        self, message: str, *, status_code: int = 400, reason: str = "refused"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason = reason
        """A short code — lowercase, digits and underscores — never a sentence.
        :func:`enrolment_audit_detail` enforces the shape rather than trusting
        it, because a caller passing a message here is exactly how a machine's
        name reaches a store that outlives it."""


@dataclass(frozen=True, slots=True)
class MachineAddress:
    """One address a machine listens on, and the pin for it.

    A pin per address rather than per machine: it is how the endpoint set on the
    manifest is shaped, so a record maps onto it one entry to one entry with
    nothing to translate. Two addresses on one machine normally carry the same
    fingerprint, and nothing here requires that — a machine that presents a
    different certificate per interface is unusual, not wrong.
    """

    url: str
    """``https://<host>[:port]/<path>``. An IPv6 host keeps its brackets."""

    tls_fingerprint: str
    """``sha256:`` and 64 lowercase hex, the spelling the client compares."""


@dataclass(frozen=True, slots=True)
class Machine:
    """One enrolled workstation, as it is stored.

    Everything a caller needs to address the machine and nothing that describes
    a moment: no health, no online flag, no last-seen. Those are read from the
    running core (:class:`MachineView`), because a stored copy of them is a
    second source of truth that goes stale the first time the core restarts.
    """

    id: str
    """16 lowercase hex characters. Stable for the life of the record."""

    name: str
    """What the owner calls it, normalised for whitespace only — the case a
    machine prints on its own screen is the case that makes the row
    recognisable. Unique across the registry, compared case-insensitively."""

    addresses: tuple[MachineAddress, ...]
    """At least one, at most :data:`MAX_ADDRESSES`. The full listening set, in
    the order the machine reported it — not two fixed slots."""

    token_secret: str
    """The *name* the machine's bearer token is stored under, in the one
    ``workstation`` namespace. Never the token."""

    enrolled_at: datetime
    """When it joined. Timezone-aware, UTC."""

    last_acted_at: datetime | None = None
    """When it last did something, or ``None`` if it never has. Written on the
    path that already writes an audit row for the act, so it costs one file
    replace and no new bookkeeping."""


@dataclass(frozen=True, slots=True)
class MachineView:
    """A machine plus what the running core knows about it right now."""

    machine: Machine
    online: bool | None
    """``True``/``False`` from the core's own connection state. ``None`` means
    nobody has told this registry what the connections are doing — reported as
    unknown rather than guessed as offline, for the reason the health enum has
    three values."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def machine_secret_name(machine_id: str) -> str:
    """``machine_<id>_token``. The only place a machine's secret is named.

    Put through :func:`personacore.config.secrets.check_secret_name` here rather
    than trusted, because it becomes a filename and the id arrives from a
    stored record on a later read.
    """
    return check_secret_name(f"{SECRET_PREFIX}{machine_id}{SECRET_SUFFIX}")


def normalise_name(raw: object) -> str:
    """The name the owner sees, normalised rather than applied silently.

    Whitespace only. Case is left exactly as the machine reported it: Windows
    hostnames are commonly uppercase, and lowercasing one makes the row harder
    to recognise, not easier. The rule itself is
    :func:`personacore.enrolment.workstation.normalise_display_name` — imported
    rather than restated, so there is one answer to what a workstation may be
    called.
    """
    from personacore.enrolment.workstation import (  # noqa: PLC0415 - see _refused
        normalise_display_name,
    )

    with _refused():
        return normalise_display_name(raw)


def normalise_address(raw: object, fingerprint: object) -> MachineAddress:
    """One address and its pin, through the enrolment module's own rules.

    ``validate_url`` refuses anything but https, an address carrying a
    credential, and the loopback / unspecified / link-local literals a
    workstation cannot be at; it rebuilds the authority from the parsed parts,
    which is what puts an IPv6 host back inside its brackets. ``normalise_
    fingerprint`` accepts a bare digest or the prefixed spelling and stores the
    prefixed one. Both are imported rather than re-implemented.
    """
    from personacore.enrolment.workstation import (  # noqa: PLC0415 - see _refused
        normalise_fingerprint,
        validate_url,
    )

    with _refused():
        url, _origin = validate_url(raw)
        pin = normalise_fingerprint(fingerprint)
    return MachineAddress(url=url, tls_fingerprint=pin)


class _refused:
    """Turn an :class:`EnrolmentRefused` into a :class:`MachineRejected`.

    The validators live in the enrolment module because that is where the rule
    was decided, and they raise its exception. The registry is not an HTTP
    route and does not want one, so the message — which is already written for
    the owner — is carried across and the type is not. Imported inside the
    functions above rather than at module scope: the admin surface imports
    enrolment, and a module-level edge back the other way is a cycle waiting
    for whoever adds the next import.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        from personacore.enrolment.workstation import (  # noqa: PLC0415
            EnrolmentRefused,
        )

        if isinstance(exc, EnrolmentRefused):
            raise MachineRejected(
                exc.message, status_code=exc.status_code, reason="invalid_field"
            ) from None
        return False


def credential_shaped_keys(node: Any, prefix: str = "") -> list[str]:
    """Dotted paths of keys whose *name* says they hold a live credential.

    :func:`personacore.admin.config_io.secret_value_keys` is the list of names
    and the walk over tables; it is called rather than copied, because a second
    list of credential-shaped key names drifts from the first within a release.
    What is added here is **descent into arrays**: that walk descends into
    ``dict`` values only, so a ``token = "…"`` sitting inside an array of
    tables is invisible to it — and every machine record in this file is an
    entry in exactly such an array.

    Imported inside the function for the reason :class:`_refused` gives: the
    admin package imports this one, so a module-level import back the other way
    is a cycle the next person to add an import would fall into.
    """
    from personacore.admin.config_io import secret_value_keys  # noqa: PLC0415

    found: list[str] = []
    if isinstance(node, Mapping):
        found.extend(
            f"{prefix}.{path}" if prefix else path
            for path in secret_value_keys(dict(node))
        )
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, list):
                found.extend(credential_shaped_keys(value, path))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(credential_shaped_keys(item, f"{prefix}[{index}]"))
    return sorted(set(found))


def _refuse_credential_values(document: Any, path: Path) -> None:
    """Refuse a machines file carrying what looks like a live credential.

    Applied on the way out as well as in, for the reason
    ``plugin_config_io._refuse_secret_values`` gives: a value that should never
    have reached the disk must not be handed onward to a browser, a screenshot
    or a support conversation either.
    """
    offenders = credential_shaped_keys(document)
    if not offenders:
        return
    names = ", ".join(offenders)
    raise MachineRejected(
        f"{path.name} holds a setting that looks like a live credential ({names}), "
        "so it will not be read or written. A machine record names its token; the "
        "value lives in the secret store and never in a file that is backed up.",
        status_code=409,
        reason="credential_in_file",
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass
class MachineRegistry:
    """Every machine the ``workstation`` plugin holds.

    One lock around every read-modify-write. There is one core process and a
    handful of machines, so a lock is the whole of the concurrency story — but
    it is not optional: ``record_last_acted`` runs on the tool-call path, and
    two calls landing together without it would read the same document and one
    would write the other's machine away.

    ``now`` is accepted by every method that needs the current time, the same
    testability seam :class:`personacore.enrolment.pairing.PairingStore` uses.
    """

    layout: AppdataLayout
    secrets: SecretStore
    plugin: str = PLUGIN_NAME
    connection_state: Callable[[Machine], bool | None] | None = None
    """Whether the core's own client is connected to this machine right now.

    A seam, not a poll: whoever holds the connections answers it, so the word
    "online" means the same thing on the settings screen as it does in the code
    that dials. ``None`` — the callable absent, or answering ``None`` — is
    reported as unknown."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- where the file is -------------------------------------------------

    @property
    def path(self) -> Path:
        """``<appdata>/plugins[-http]/<plugin>/machines.toml``, or refuse.

        The path is not built here. It is
        :func:`personacore.admin.plugin_config_io.plugin_config_path` — which
        checks the plugin name against the manifest's own rule, resolves the
        folder and the file through ``require_inside``, and refuses a file that
        does not sit directly inside the resolved plugin folder — and then the
        sibling name. A second opinion about which directory is safe to write
        is exactly the duplicate path logic spec section 7 wants none of.

        Imported inside the property: the admin package imports enrolment, so a
        module-level import the other way is a cycle.
        """
        from personacore.admin.plugin_config_io import (  # noqa: PLC0415
            ConfigRejected,
            plugin_config_path,
        )

        try:
            config = plugin_config_path(self.layout, self.plugin)
        except ConfigRejected as exc:
            raise MachineRejected(
                exc.message, status_code=404, reason="not_installed"
            ) from None
        return config.with_name(MACHINES_FILENAME)

    # -- reading -----------------------------------------------------------

    def list(self) -> tuple[Machine, ...]:
        """Every machine, in the order they were written."""
        with self._lock:
            return self._read()

    def get(self, machine_id: str) -> Machine | None:
        """One machine by id, or ``None``."""
        return next((m for m in self.list() if m.id == machine_id), None)

    def find(self, name: str) -> Machine | None:
        """One machine by exact name, compared case-insensitively.

        Exact only. Partial matching, ambiguity and the question that follows
        one are the resolver's, which is a separate piece of work — this is the
        lookup name uniqueness is enforced with.
        """
        wanted = _fold(name)
        return next((m for m in self.list() if _fold(m.name) == wanted), None)

    def views(self) -> tuple[MachineView, ...]:
        """Every machine plus whether the core is connected to it right now."""
        machines = self.list()
        return tuple(MachineView(machine=m, online=self._online(m)) for m in machines)

    def _online(self, machine: Machine) -> bool | None:
        if self.connection_state is None:
            return None
        try:
            state = self.connection_state(machine)
        except Exception as exc:  # noqa: BLE001 - a listing must not fail on it
            logger.warning(
                "machine_connection_state_failed", plugin=self.plugin, error=repr(exc)
            )
            return None
        return None if state is None else bool(state)

    # -- writing -----------------------------------------------------------

    def add(
        self,
        *,
        name: object,
        addresses: Iterable[tuple[object, object]],
        token: str,
        now: datetime | None = None,
    ) -> Machine:
        """Store one machine and its token. Both, or neither.

        ``addresses`` is ``(url, tls_fingerprint)`` pairs — the machine's whole
        listening set, in its own order.

        Everything that can refuse the machine is checked **before** the secret
        is written and before the record is: a refusal must leave nothing
        behind, and a name collision found after a token was stored is a live
        credential nothing refers to.

        The secret goes first and the record second. That order is chosen for
        which half is worse to be left holding: a record naming a secret that
        was never stored is a machine that looks enrolled and cannot connect,
        while a secret nothing refers to is inert and goes with the namespace at
        teardown either way. The record write failing takes the secret back out
        regardless, so neither is the normal outcome.
        """
        moment = now or datetime.now(UTC)
        machine_name = normalise_name(name)
        resolved = _resolve_addresses(addresses)
        if not isinstance(token, str) or not token:
            raise MachineRejected(
                "That machine has no credential to store, so it was not added. "
                "A machine is enrolled with a token minted by this core.",
                reason="no_token",
            )

        with self._lock:
            existing = self._read()
            self._refuse_collision(existing, machine_name, resolved)
            if len(existing) >= MAX_MACHINES:
                raise MachineRejected(
                    f"This core already holds {MAX_MACHINES} workstations, which is "
                    "as many as it keeps. Remove one you no longer use and add this "
                    "one again.",
                    status_code=409,
                    reason="too_many_machines",
                )

            machine_id = _mint_id({m.id for m in existing})
            machine = Machine(
                id=machine_id,
                name=machine_name,
                addresses=resolved,
                token_secret=machine_secret_name(machine_id),
                enrolled_at=_to_utc(moment),
            )

            try:
                self.secrets.set(machine.token_secret, token, self.plugin)
            except (SecretError, OSError) as exc:
                logger.error(
                    "machine_secret_write_failed", plugin=self.plugin, error=str(exc)
                )
                raise MachineRejected(
                    "That machine's credential could not be stored, so it was not "
                    "added. Nothing is half-written. Check the appdata volume is "
                    "mounted and writable, then add it again.",
                    status_code=500,
                    reason="secret_write_failed",
                ) from None

            try:
                self._write((*existing, machine))
            except Exception:
                self._forget_secret(machine.token_secret)
                raise

        logger.info("machine_added", plugin=self.plugin, machines=len(existing) + 1)
        return machine

    def remove(self, machine_id: str) -> bool:
        """Forget one machine and its token. ``False`` if there was no such row.

        The record goes first: while the secret is being deleted there must be
        nothing left that would send a caller to that machine, and a stored
        token with no record is inert.
        """
        with self._lock:
            existing = self._read()
            machine = next((m for m in existing if m.id == machine_id), None)
            if machine is None:
                return False
            self._write(tuple(m for m in existing if m.id != machine_id))
            self._forget_secret(machine.token_secret)
        logger.info("machine_removed", plugin=self.plugin, machines=len(existing) - 1)
        return True

    def forget_all(self) -> int:
        """Forget every machine and every machine token. Returns how many went.

        Not the teardown path — that is ``uninstall_package`` plus
        ``delete_namespace``, and this registry is deliberately not consulted by
        either, because a teardown that depends on the thing being torn down
        calling a method is a teardown with a way to be skipped. This exists for
        the owner clearing the list without uninstalling the plugin.
        """
        with self._lock:
            existing = self._read()
            if existing:
                self._write(())
            for machine in existing:
                self._forget_secret(machine.token_secret)
        logger.info("machines_forgotten", plugin=self.plugin, machines=len(existing))
        return len(existing)

    def record_last_acted(
        self, machine_id: str, *, now: datetime | None = None
    ) -> bool:
        """Stamp when a machine last did something. ``False`` if it is unknown.

        Called from wherever the act is already being written to the audit
        store, so it adds a file replace and no new bookkeeping. A write that
        already says the same second is skipped rather than repeated, which is
        the only thing standing between a burst of tool calls and a burst of
        identical file replaces.

        Never raises for a storage failure: a timestamp that could not be
        written — **or read** — is not a reason to fail the thing the machine
        actually did. The read was outside the guard in the first version of
        this method, so an unreadable or future-format ``machines.toml`` failed
        a tool call the machine had already completed, which is the opposite of
        what the sentence above promises. Both halves are guarded now.
        """
        moment = _to_utc(now or datetime.now(UTC))
        with self._lock:
            try:
                existing = self._read()
            except (MachineRejected, OSError) as exc:
                logger.warning(
                    "machine_last_acted_read_failed",
                    plugin=self.plugin,
                    error=str(exc),
                )
                return False
            updated: list[Machine] = []
            found = False
            for machine in existing:
                if machine.id != machine_id:
                    updated.append(machine)
                    continue
                found = True
                if _same_second(machine.last_acted_at, moment):
                    return True
                updated.append(
                    Machine(
                        id=machine.id,
                        name=machine.name,
                        addresses=machine.addresses,
                        token_secret=machine.token_secret,
                        enrolled_at=machine.enrolled_at,
                        last_acted_at=moment,
                    )
                )
            if not found:
                return False
            try:
                self._write(tuple(updated))
            except (MachineRejected, OSError) as exc:
                logger.warning(
                    "machine_last_acted_write_failed",
                    plugin=self.plugin,
                    error=str(exc),
                )
                return False
        return True

    # -- the file ----------------------------------------------------------

    def _refuse_collision(
        self,
        existing: Sequence[Machine],
        name: str,
        addresses: Sequence[MachineAddress],
    ) -> None:
        """Refuse a name or an address already taken. Never auto-suffixed.

        A collision is told to the owner and nothing is written. The alternative
        — appending a number — turns a wiped and re-enrolled machine into a
        silent second row beside a dead one, which is worse than being told.

        The name is compared case-insensitively because the two spellings name
        the same machine to everyone except a byte comparison, and Windows
        agrees. An address is refused for the same reason: two rows pointing at
        one machine is one machine that can be removed and still answer.

        **The message names the machine it collided with; the reason code does
        not.** Being told "there is already one called that" without being told
        which one is a refusal nobody can act on — the owner has to know whether
        the row in his way is the machine he wiped last week. But that name is
        an *enrolled* machine, so it may go to the caller and it may not go into
        an audit row or a log line. ``reason="name_taken"`` is what is
        recordable, and :func:`enrolment_audit_detail` enforces that rather than
        trusting the caller to pass the right one.
        """
        wanted = _fold(name)
        clash = next((m for m in existing if _fold(m.name) == wanted), None)
        if clash is not None:
            raise MachineRejected(
                f"A workstation called {clash.name!r} is already on this core, so "
                f"{name!r} cannot be added under that name. Remove the one that is "
                "there, or rename this machine and add it again.",
                status_code=409,
                reason="name_taken",
            )
        taken = {address.url for machine in existing for address in machine.addresses}
        for address in addresses:
            if address.url in taken:
                raise MachineRejected(
                    "Another workstation on this core is already reached at that "
                    "address, so this one was not added. Remove the one that is "
                    "there first, or give this machine an address of its own.",
                    status_code=409,
                    reason="address_taken",
                )

    def _read(self) -> tuple[Machine, ...]:
        """Parse the file, or refuse it. Missing means no machines yet.

        A file that does not parse, or that this core does not understand, is a
        refusal and not an empty list. Reporting no machines when there are some
        would take a working workstation off the screen with nothing to say why,
        and leave its token live in the store with nothing referring to it.
        """
        path = self.path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise MachineRejected(
                f"The list of workstations could not be read from {path.as_posix()}: "
                f"{exc.strerror or exc}. Check the appdata volume is mounted.",
                status_code=500,
                reason="storage",
            ) from None

        try:
            document = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise MachineRejected(
                f"The list of workstations in {path.as_posix()} could not be read: "
                f"{exc}. It is written by this core, so an edited or damaged file is "
                "the likely cause; move it aside and add the machines again.",
                status_code=500,
                reason="unreadable",
            ) from None

        _refuse_credential_values(document, path)
        version = document.get("schema", SCHEMA_VERSION)
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise MachineRejected(
                f"The list of workstations in {path.as_posix()} was written by a "
                f"newer version of this core (format {version!r}, this core reads "
                f"{SCHEMA_VERSION}). Upgrade the core, or move the file aside and "
                "add the machines again.",
                status_code=409,
                reason="future_format",
            )
        entries = document.get("machine", [])
        if not isinstance(entries, list):
            raise MachineRejected(
                f"The list of workstations in {path.as_posix()} is not a list of "
                "machines. Move it aside and add the machines again.",
                status_code=500,
                reason="unreadable",
            )
        return tuple(_machine_from(entry, path) for entry in entries)

    def _write(self, machines: Sequence[Machine]) -> None:
        """Replace the file atomically, or leave the old one exactly as it was.

        The bytes are built and checked first, so a refusal never opens
        anything; then a temporary file **in the same directory** — a rename is
        only atomic within one filesystem and appdata is a mounted volume — is
        flushed, fsynced, and moved over the real file with :func:`os.replace`,
        which is atomic on POSIX and on Windows. A crash leaves either the old
        list or the new one, never half of either. The reasoning is
        ``admin.plugin_config_io.write_plugin_config``'s; the file is a
        different one, so the few lines are here rather than that function being
        bent to take a filename.
        """
        path = self.path
        document: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "machine": [_machine_document(machine) for machine in machines],
        }
        _refuse_credential_values(document, path)
        text = _FILE_HEADER + tomli_w.dumps(document)

        temporary = path.with_name(path.name + ".new")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - the real failure is being raised
                pass
            raise MachineRejected(
                f"The list of workstations could not be saved to {path.as_posix()}: "
                f"{exc.strerror or exc}. Check the appdata volume is mounted and "
                "writable; nothing was changed.",
                status_code=500,
                reason="storage",
            ) from None

    def _forget_secret(self, name: str) -> None:
        """Delete one machine's token. Never raises; the namespace is the backstop.

        A token that survives this call still goes at teardown, because
        ``delete_namespace`` removes the whole namespace by owner and not by
        name. Failing the owner's *remove* over it would leave the record on
        screen, which is the worse of the two.
        """
        try:
            self.secrets.delete(name, self.plugin)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning(
                "machine_secret_remove_failed", plugin=self.plugin, error=str(exc)
            )


_FILE_HEADER = (
    "# Workstations enrolled with this core. Written by the core — add and\n"
    "# remove them on the workstation plugin's own settings screen.\n"
    "#\n"
    "# Every machine here belongs to this plugin. Uninstalling the plugin\n"
    "# removes this file with the rest of the folder, and the tokens named\n"
    "# below go with the plugin's secret namespace at the same time.\n"
    "#\n"
    "# token_secret is the NAME a credential is stored under. The value is in\n"
    "# the secret store and never in this file.\n\n"
)


# ---------------------------------------------------------------------------
# Record <-> document
# ---------------------------------------------------------------------------


def _machine_document(machine: Machine) -> dict[str, Any]:
    """One record as it is written. Field order is the order it reads best in."""
    document: dict[str, Any] = {
        "id": machine.id,
        "name": machine.name,
        "addresses": [
            {"url": address.url, "tls_fingerprint": address.tls_fingerprint}
            for address in machine.addresses
        ],
        "token_secret": machine.token_secret,
        "enrolled_at": _to_utc(machine.enrolled_at),
    }
    if machine.last_acted_at is not None:
        document["last_acted_at"] = _to_utc(machine.last_acted_at)
    return document


def _machine_from(entry: Any, path: Path) -> Machine:
    """One record as it is read back. Unknown keys are ignored, not refused.

    Ignored so a file written by a later core with a field this one does not
    know is still a readable list of machines rather than an unreadable one —
    the schema number is what refuses a format this core genuinely cannot
    handle. Missing or malformed *known* fields are refused, because a machine
    with no address or no token name is not a machine.
    """
    if not isinstance(entry, Mapping):
        raise MachineRejected(
            f"An entry in {path.as_posix()} is not a workstation record. Move the "
            "file aside and add the machines again.",
            status_code=500,
            reason="unreadable",
        )

    def _text(key: str) -> str:
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            raise MachineRejected(
                f"A workstation in {path.as_posix()} has no {key}, so the list "
                "cannot be read. Move the file aside and add the machines again.",
                status_code=500,
                reason="unreadable",
            )
        return value

    raw_addresses = entry.get("addresses")
    if not isinstance(raw_addresses, list) or not raw_addresses:
        raise MachineRejected(
            f"A workstation in {path.as_posix()} has no addresses to reach it at, "
            "so the list cannot be read. Move the file aside and add the machines "
            "again.",
            status_code=500,
            reason="unreadable",
        )
    addresses: list[MachineAddress] = []
    for item in raw_addresses:
        if not isinstance(item, Mapping):
            raise MachineRejected(
                f"An address in {path.as_posix()} is not readable. Move the file "
                "aside and add the machines again.",
                status_code=500,
                reason="unreadable",
            )
        url = item.get("url")
        pin = item.get("tls_fingerprint")
        if not isinstance(url, str) or not isinstance(pin, str) or not url or not pin:
            raise MachineRejected(
                f"An address in {path.as_posix()} is missing its url or its "
                "certificate fingerprint. Move the file aside and add the machines "
                "again.",
                status_code=500,
                reason="unreadable",
            )
        addresses.append(MachineAddress(url=url, tls_fingerprint=pin))

    return Machine(
        id=_text("id"),
        name=_text("name"),
        addresses=tuple(addresses),
        token_secret=_text("token_secret"),
        enrolled_at=_moment(entry.get("enrolled_at"), path, "enrolled_at"),
        last_acted_at=(
            None
            if entry.get("last_acted_at") is None
            else _moment(entry.get("last_acted_at"), path, "last_acted_at")
        ),
    )


def _moment(value: Any, path: Path, key: str) -> datetime:
    """A stored timestamp, as an aware UTC ``datetime``.

    TOML has a real date-time type and ``tomli_w`` writes one, so the normal
    path is already a ``datetime``. A string is accepted too — a file somebody
    edited by hand is exactly where one appears — and anything else is refused.
    """
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, str):
        try:
            return _to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    raise MachineRejected(
        f"A workstation in {path.as_posix()} has an unreadable {key}. Move the file "
        "aside and add the machines again.",
        status_code=500,
        reason="unreadable",
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _resolve_addresses(
    addresses: Iterable[tuple[object, object]],
) -> tuple[MachineAddress, ...]:
    """Validate the listening set and drop exact repeats, keeping the order."""
    resolved: list[MachineAddress] = []
    seen: set[str] = set()
    for raw_url, raw_pin in addresses:
        address = normalise_address(raw_url, raw_pin)
        if address.url in seen:
            continue
        seen.add(address.url)
        resolved.append(address)
    if not resolved:
        raise MachineRejected(
            "That machine sent no address to reach it at, so it was not added. A "
            "workstation is added with the addresses it listens on.",
            reason="no_address",
        )
    if len(resolved) > MAX_ADDRESSES:
        raise MachineRejected(
            f"That machine sent {len(resolved)} addresses, and this core keeps at "
            f"most {MAX_ADDRESSES}. Have it listen on fewer, or give it the ones it "
            "is actually reached at.",
            status_code=409,
            reason="too_many_addresses",
        )
    return tuple(resolved)


def _mint_id(taken: set[str]) -> str:
    """A fresh opaque id. Loops rather than trusting 64 bits blindly."""
    while True:
        candidate = secrets_module.token_hex(ID_BYTES)
        if candidate not in taken:
            return candidate


def _fold(name: str) -> str:
    """How two machine names are compared: case-insensitively, whitespace-collapsed."""
    return " ".join(name.split()).casefold()


def _to_utc(moment: datetime) -> datetime:
    """Aware UTC. A naive timestamp is read as UTC rather than as local time —
    everything this core stores is UTC, and guessing a zone is how a timestamp
    ends up an hour wrong twice a year."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _same_second(stored: datetime | None, moment: datetime) -> bool:
    if stored is None:
        return False
    return _to_utc(stored).replace(microsecond=0) == moment.replace(microsecond=0)


# ---------------------------------------------------------------------------
# What an enrolment audit record may contain
# ---------------------------------------------------------------------------


def enrolment_audit_detail(
    *,
    plugin: str | None = None,
    machines: int | None = None,
    state: str | None = None,
    address: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The detail of an enrolment audit row, with no machine in it.

    **The audit store is core state and outlives the plugin.** A row saying
    which machine joined, under what name, at what address is a second copy of
    the registry in a place uninstalling the plugin does not reach — so the
    boundary the rest of this module keeps would be broken by the one row that
    records a machine arriving.

    What a row keeps, and why each earns its place:

    * ``plugin`` — which plugin changed, when one did. Core state either way,
      and with one workstation plugin it is a constant that identifies no
      machine. Omitted on a refusal, where nothing was installed and naming a
      plugin would be a claim about a change that did not happen.
    * ``machines`` — how many are enrolled afterwards. This is what makes the
      row *useful*: "a workstation joined at 18:22 and there are now three"
      answers the question an audit trail exists for — did something get added
      that I did not add — without being a list of what they are.
    * ``state`` — the plugin's health a moment later. About the plugin.
    * ``address`` — **on a refusal only.** A refused attempt is not a machine,
      and the source of a failed write on this core's one unauthenticated route
      is exactly what an audit trail is for. On success the same string is the
      enrolled machine's address, which is the registry again, so it is dropped.
    * ``reason`` — a **short code**, never a refusal sentence.
      :data:`REASON_CODE_RE` is applied here rather than assumed, and anything
      that does not match is replaced by ``"refused"``. The first version of
      this function documented that a reason "names no machine" and then took
      whatever it was handed: a collision sentence has to say what it collided
      with to be worth reading, and what it collides with is an **already
      enrolled** machine. The sentence is the answer to the caller; the code is
      what is recorded. Checking is the fix, because the docstring saying so
      was the defect.

    **Not here, and not to be added:** the machine's name, its id, its
    addresses, its token or the name its token is stored under. Nothing in this
    dictionary identifies a machine, which is the property that has to survive
    the next person who wants one more field for an investigation.
    """
    detail: dict[str, Any] = {}
    if plugin is not None:
        detail["plugin"] = plugin
    if machines is not None:
        detail["machines"] = machines
    if state is not None:
        detail["state"] = state
    if address is not None:
        detail["address"] = address
    if reason is not None:
        detail["reason"] = safe_reason(reason)
    return detail


REASON_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""What a recordable reason looks like: a short lowercase code, no spaces, so a
sentence cannot be one. The length cap is part of the check — a code is a label
and anything longer is prose wearing a label's clothes."""

GENERIC_REASON = "refused"
"""What a reason that is not a code becomes. Deliberately uninformative: the
useful text went to the caller in the response, and the alternative to dropping
it here is storing whatever it happened to say."""


def safe_reason(reason: str) -> str:
    """One reason code, or :data:`GENERIC_REASON`.

    The rejected text is **not** logged. Logging "this reason was too detailed
    to store" together with the reason would be the same leak with a warning
    attached; the event name and the plugin are enough for whoever is looking
    for the call site.
    """
    if isinstance(reason, str) and REASON_CODE_RE.fullmatch(reason):
        return reason
    logger.warning("audit_reason_not_a_code", plugin=PLUGIN_NAME)
    return GENERIC_REASON


__all__ = [
    "GENERIC_REASON",
    "ID_BYTES",
    "MACHINES_FILENAME",
    "MAX_ADDRESSES",
    "MAX_MACHINES",
    "PLUGIN_NAME",
    "REASON_CODE_RE",
    "SCHEMA_VERSION",
    "Machine",
    "MachineAddress",
    "MachineRegistry",
    "MachineRejected",
    "MachineView",
    "credential_shaped_keys",
    "enrolment_audit_detail",
    "machine_secret_name",
    "normalise_address",
    "normalise_name",
    "safe_reason",
]
