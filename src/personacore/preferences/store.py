"""Per-user settings, stored in SQLite under appdata (ADR-0030).

**This module owns the durable-state database file, not just the preferences
table.** ADR-0030 created ``<appdata>/state/personacore.db`` for the first
thing that had to outlive the retention purge; API keys are the second
(migration 0003). One file has exactly one ``schema_version`` and therefore
exactly one ordered ladder, so the ladder lives here, beside the store that
opened the file first, and :func:`open_state_database` is how anything else
gets a connection to it. A second ladder over the same file would be two
components each believing they knew what version 3 meant.

The preference *table* is described below; the ladder and the connection are
general to the file.

One table, keyed by **which door the operator came through**, their
``AdminUser.id``, and a setting name. The key is deliberately the *identity*
and not an account row: three doors produce a signed-in operator — the core's
own sign-in, a trusted proxy header, and the development bypass — and only the
first has a record in ``accounts.json``. Keying to accounts would work on a
machine using built-in sign-in and silently save nothing behind a login proxy.

The door is part of the key because the id on its own is not a person. The
three doors mint it under incompatible rules (see ``AdminUser.door``), so a
proxy-supplied ``alice``, the account ``alice`` and
``PERSONACORE_ADMIN_DEV_USER=alice`` are three operators wearing one string,
and the last of those is an always-admin break-glass. Sharing a row between
them is harmless for a boolean about speech and is not harmless for the child
lock, which ADR-0030 says inherits this shape.

Its own database file, not the audit one. ``audit.db`` holds what the retention
purge trims on a timer; settings must outlive that, and "clear the audit log"
must never mean "lose everyone's settings".

Reads are served from memory. The table holds one small row per person per
setting, every page render needs it, and the core is asyncio — so rather than
hand every lookup to a worker thread, the whole table is read once at open and
kept in a dict that writes update in place. This is why nothing here is a
coroutine.

**Writes block, and an async caller must offload them.** A write is an
``INSERT`` and a ``commit()``, and a WAL commit at SQLite's default
``synchronous=FULL`` fsyncs — hundreds of microseconds to milliseconds of the
single event loop, spent on behalf of one authenticated request while
``/health`` and every in-flight chat turn wait. So :meth:`PreferenceStore.set_bool`
stays an ordinary blocking function and every ``async`` caller reaches it
through ``asyncio.to_thread``, as the admin API's other SQLite callers do. The
connection is opened with ``check_same_thread=False`` and every write holds
the lock, so calling it from a worker thread is safe. Reads deliberately do
*not* offload: they are a dict lookup, and a worker-thread hop would cost more
than it saves.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from personacore.auth.method import AuthMethod

_logger = structlog.get_logger(__name__)

STATE_DATABASE_FILENAME = "personacore.db"
"""The durable-state database inside ``<appdata>/state/`` (ADR-0030)."""

PREFERENCES_FILENAME = STATE_DATABASE_FILENAME
"""The same file, under the name it was given when preferences were the only
thing in it. Kept because callers across the tree import it; new code should
prefer :data:`STATE_DATABASE_FILENAME`, which does not imply the file holds
one component's rows."""

Migration = Callable[[sqlite3.Connection, Path], None]
"""One rung of the ladder: the connection, and the path the database was opened
from. The path is passed because migration 0003 has to find a file *beside* the
database — see :func:`_import_legacy_api_keys`."""


class SchemaDowngradeError(RuntimeError):
    """The database was last opened by a build that knew more migrations."""


class StateMigrationError(RuntimeError):
    """A migration found something it must not paper over.

    Raised instead of continuing with an empty table. The message reaches an
    operator at start-up — it is the only thing they will see — so it names the
    file, says what is wrong with it, and says what to do about it.
    """


class Override(StrEnum):
    """What an administrator has decided about a setting for everybody.

    ``UNSET`` is not "off". It means the administrator has expressed no opinion
    and each person's own choice stands — which is a different state from
    forcing the setting off for everyone, and the two must not collapse into
    one another.
    """

    UNSET = "unset"
    ON = "on"
    OFF = "off"


def _migration_0001_user_preferences(conn: sqlite3.Connection, _path: Path) -> None:
    conn.execute(
        """
        CREATE TABLE user_preferences (
            user_id TEXT NOT NULL,
            name    TEXT NOT NULL,
            value   TEXT NOT NULL,
            PRIMARY KEY (user_id, name)
        )
        """
    )


_DOORS_AT_MIGRATION_0002 = ("builtin", "proxy", "bypass")
"""The three doors as they stood when migration 0002 was written.

Spelled out rather than read from :class:`~personacore.auth.method.AuthMethod`
on purpose: a migration is a historical record of what happened to a database,
and one that consulted a live enum would quietly do something different the day
a fourth door is added — to databases that were migrated years earlier.
"""


def _migration_0002_key_by_door(conn: sqlite3.Connection, _path: Path) -> None:
    """Put the door in the primary key, keeping every row already written.

    Version 0001 is deployed, so this cannot start from an empty table. Which
    door wrote those rows is not knowable here — the store is built from a path
    and nothing else, and the answer lives in an ``AuthDecision`` it has never
    seen — so rather than guess one and orphan the rows if the guess is wrong,
    each existing row is carried forward under **all three** doors.

    Only one door is ever in force in a running core (``AuthMethod``: "never two
    at once"), so exactly one of the three copies is the live one and the other
    two are inert — the same kind of inert row ADR-0030 §2 already accepts for
    proxy-supplied names that were never created here. The accepted cost is
    that a *pre-existing* row still reads the same under all three doors until
    somebody saves, which is precisely the behaviour these rows already had.
    Nothing written after this migration can share a key space again, which is
    what the child lock needs and is why this is being done now rather than
    later.

    A column rather than a composite ``door:id`` key string: the id is
    unconstrained printable text up to 256 characters and may itself contain
    any separator that could be chosen, so a flattened key would be ambiguous
    exactly where a proxy controls the input. SQLite cannot alter a primary
    key, hence the copy-and-rename; it runs inside the migration step's
    transaction, so a crash leaves the old table intact.
    """
    conn.execute(
        """
        CREATE TABLE user_preferences_v2 (
            door    TEXT NOT NULL,
            user_id TEXT NOT NULL,
            name    TEXT NOT NULL,
            value   TEXT NOT NULL,
            PRIMARY KEY (door, user_id, name)
        )
        """
    )
    for door in _DOORS_AT_MIGRATION_0002:
        conn.execute(
            "INSERT INTO user_preferences_v2 (door, user_id, name, value) "
            "SELECT ?, user_id, name, value FROM user_preferences",
            (door,),
        )
    conn.execute("DROP TABLE user_preferences")
    conn.execute("ALTER TABLE user_preferences_v2 RENAME TO user_preferences")


_LEGACY_API_KEY_JSON_AT_MIGRATION_0003 = ("users", "api-keys.json")
"""Where the API-key file sat when migration 0003 was written, relative to the
appdata root — ``<appdata>/users/api-keys.json``, with the database itself at
``<appdata>/state/personacore.db``.

Spelled out rather than asked of :class:`~personacore.config.appdata.AppdataLayout`,
for the reason :data:`_DOORS_AT_MIGRATION_0002` gives: a migration is a
historical record of what happened to a database, and the layout is live code
that may move. The path is derived from the database's own path (``..``, then
``users``) because the ladder runs for whichever component opens the file
first, and :class:`PreferenceStore` is built from a path and knows nothing
about an appdata root.
"""


def _migration_0003_api_keys(conn: sqlite3.Connection, database_path: Path) -> None:
    """Give API keys a table, and carry ``users/api-keys.json`` into it.

    Keys move here because ``audit.db`` is the wrong file — the retention purge
    trims that one on a timer — and JSON was the wrong format: the old store
    read a corrupt file as "no keys" and the next issue wrote the whole file
    back over it, destroying every credential without a word. There is no such
    path here. A row is an ``INSERT``, never a rewrite of the set, and a
    database that will not open raises instead of presenting an empty one.

    The profile is one JSON column rather than fourteen typed ones.
    :class:`~personacore.contracts.policy.PolicyProfile` is a nested pydantic
    model that owns its own shape and gains fields as policy grows; exploding
    it into columns would mean a migration here every time it does, and two
    definitions of what a policy is.

    **The import, and the three cases it has to tell apart:**

    * *No file* — a fresh appdata, or a deployment that never issued a key.
      Nothing to do; the table starts empty and that is the truth.
    * *A file that parses* — every entry is inserted, inside this migration's
      own transaction along with the ``CREATE TABLE``, so a crash halfway
      leaves the database at version 2 with no table rather than at version 3
      with half the keys.
    * *A file that does not parse, or an entry that does not validate* — raise
      :class:`StateMigrationError`. Starting with an empty table would be the
      old bug wearing a new hat: the keys would be invisible, the operator
      would issue a replacement, and the evidence of what went wrong would
      still be sitting in a file nothing reads any more.

      Raising here does **not** stop the core. :func:`open_state_database`
      catches it at the boundary, rolls this rung back and carries the sentence
      out as a problem for a screen to show, because a container that will not
      start has no admin UI — and the admin UI is where the operator would go
      to find out what is wrong. The key store then reports itself *unusable*
      rather than empty, refuses to issue or revoke, and every ``/v1`` request
      401s. The import is still owed: fix the file, restart, and it runs.

    **The JSON file is left on disk, and ignored from here on.** Deleting the
    only copy of a credential store during its own migration would leave
    nothing to roll back to; pinning the previous image tag has to be a real
    option. Nothing reads it after this runs, so editing it changes nothing.

    Validation borrows the live :class:`~personacore.api.keys.ApiKeyRecord`,
    imported inside the function to keep the import one-directional
    (``api.keys`` reaches in here for :func:`open_state_database`, never the
    reverse). That is a deliberate exception to the frozen-migration rule above
    and the one place it could bite: if ``ApiKeyRecord`` ever gains a required
    field or loses one, this migration stops accepting the very files it exists
    to read. Whoever changes that model — which means adding a migration to
    this ladder for the table too — should freeze a copy of the shape here at
    the same time.
    """
    conn.execute(
        """
        CREATE TABLE api_keys (
            key_id     TEXT NOT NULL PRIMARY KEY,
            key_hash   TEXT NOT NULL,
            created_at TEXT NOT NULL,
            note       TEXT NOT NULL,
            profile    TEXT NOT NULL
        )
        """
    )
    _import_legacy_api_keys(conn, database_path)


def _import_legacy_api_keys(conn: sqlite3.Connection, database_path: Path) -> None:
    """Read ``users/api-keys.json`` into the ``api_keys`` table, or raise."""
    from personacore.api.keys import ApiKeyRecord  # local: see the docstring above

    legacy = database_path.parent.parent.joinpath(*_LEGACY_API_KEY_JSON_AT_MIGRATION_0003)
    fix_it = (
        "Correct the file, or move it aside if the keys in it are no longer "
        "wanted, and start the core again. Nothing has been changed on disk."
    )
    try:
        text = legacy.read_text(encoding="utf-8")
    except FileNotFoundError:
        _logger.info("api_key_import_skipped", reason="no legacy file", path=str(legacy))
        return
    except OSError as exc:
        raise StateMigrationError(
            f"The API-key file at {legacy} could not be read: "
            f"{exc.strerror or exc}. Its keys cannot be moved into the state "
            f"database until it can be. {fix_it}"
        ) from exc

    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise StateMigrationError(
            f"The API-key file at {legacy} is not valid JSON: {exc}. Every key "
            "in it would be lost if the core started without it, so it is "
            f"refusing to start instead. {fix_it}"
        ) from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("keys"), list):
        raise StateMigrationError(
            f"The API-key file at {legacy} is JSON but not the expected shape — "
            'an object with a "keys" list. Every key in it would be lost if the '
            f"core started without it. {fix_it}"
        )

    imported = 0
    for position, entry in enumerate(raw["keys"]):
        try:
            record = ApiKeyRecord.model_validate(entry)
        except ValueError as exc:
            named = entry.get("key_id") if isinstance(entry, dict) else None
            which = f"key {named!r}" if named else f"the entry at position {position}"
            raise StateMigrationError(
                f"The API-key file at {legacy} holds an entry that cannot be "
                f"read — {which}: {exc}. It is not being skipped, because a key "
                "that silently vanishes is a client that stops working for no "
                f"visible reason. {fix_it}"
            ) from exc
        dumped: dict[str, Any] = record.model_dump(mode="json")
        conn.execute(
            "INSERT INTO api_keys (key_id, key_hash, created_at, note, profile) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                record.key_id,
                record.key_hash,
                dumped["created_at"],
                record.note,
                json.dumps(dumped["profile"], sort_keys=True),
            ),
        )
        imported += 1

    _logger.info("api_key_import_completed", keys=imported, path=str(legacy))


_MIGRATIONS: tuple[Migration, ...] = (
    _migration_0001_user_preferences,
    _migration_0002_key_by_door,
    _migration_0003_api_keys,
)


@dataclass(frozen=True, slots=True)
class StateDatabase:
    """An open state database, and anything the ladder could not finish.

    ``problem`` is a sentence for an operator, or ``None``. It is set when a
    migration could not complete because something *outside* the database is
    damaged — the file it was told to import, not the database itself. The
    connection is real and usable either way; a component whose own table came
    from a rung that did complete carries on as normal, and a component whose
    table is the one that did not must report itself unusable and refuse to
    write. See :func:`open_state_database`.
    """

    connection: sqlite3.Connection
    schema_version: int
    problem: str | None


def open_state_database(database_path: Path | str) -> StateDatabase:
    """Open ``<appdata>/state/personacore.db`` with its schema up to date.

    The one way into this file. Every component keeping durable state here
    calls this and then minds its own tables: the ladder is the file's, not any
    one component's, and running it from two places would be two ideas of what
    version 3 means.

    ``check_same_thread=False`` because the core is asyncio and a blocking
    write belongs on a worker thread; the caller owns a lock around its own use
    of the connection.

    **A broken database raises. Broken input data does not.** The two failures
    look similar and want opposite handling:

    * The database will not open, or its schema came from a newer build. There
      is nothing to serve and nothing a screen could offer, so this raises and
      the caller deals with it.
    * A migration was asked to import a file — ``users/api-keys.json``, for
      migration 0003 — and that file is damaged. Raising here would run inside
      ``create_app`` and produce the lockout this project has already shipped
      three times, and it would bite hardest in exactly the case where the
      operator most needs the admin UI: the UI is where they would go to see
      what is wrong. So the rung is rolled back, the ladder **stops there**,
      and the sentence the migration wrote comes back as ``problem`` for
      whoever can put it on a screen. This is the shape
      :func:`~personacore.hearing.registry.builtin_engines` uses.

    Stopping is not skipping. The version stays at the last completed rung, so
    the migration is still owed: correct the file, restart, and the import runs
    exactly as it would have. Nothing is deleted or written over in the
    meantime.

    Two consequences worth knowing. Rungs *after* the halted one do not run
    either — a later migration may depend on the one that stopped, and running
    it would be inventing an order the ladder does not have. And a component
    whose table lives at the halted rung has no table at all, which is why
    :class:`~personacore.api.keys.ApiKeyStore` checks ``problem`` before it
    touches SQL rather than letting "no such table" stand in for an
    explanation.
    """
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        version, problem = _migrate(conn, path)
    except Exception:
        conn.close()
        raise
    _restrict(path)
    return StateDatabase(connection=conn, schema_version=version, problem=problem)


def _restrict(path: Path) -> None:
    """Best-effort ``0o600`` on the database and its WAL sidecars.

    The file holds per-person settings and the SHA-256 of every API key. A hash
    is not a credential, but spec section 7 keeps credential material under
    least privilege even inside a volume only the core should be reading, which
    is the rule the JSON key file followed before this one replaced it. POSIX is
    the deployment target; on a Windows dev machine ``chmod`` is close to a
    no-op and its failure is not worth refusing to start over.
    """
    for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except OSError as exc:  # pragma: no cover - platform dependent
            _logger.warning("state_database_permissions", error=str(exc), path=str(candidate))


def _migrate(conn: sqlite3.Connection, database_path: Path) -> tuple[int, str | None]:
    """Run the ladder as far as it will go. Returns the version reached, and
    the sentence explaining why it stopped short if it did."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    conn.commit()
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        conn.commit()
        current = 0
    else:
        current = int(row["version"])

    target = len(_MIGRATIONS)
    if current > target:
        raise SchemaDowngradeError(
            f"The state database schema_version is {current}, but this "
            f"PersonaCore build only knows migrations up to version {target}. "
            "This database was last opened by a newer build. Run that newer "
            "build again, or restore an appdata backup matching this build."
        )

    reached = current
    problem: str | None = None
    for step_index in range(current, target):
        try:
            _run_migration_step(conn, database_path, step_index, _MIGRATIONS[step_index])
        except StateMigrationError as exc:
            # Damaged input, not a damaged database. The rung rolled itself
            # back, so the version below is the last one that completed and
            # this migration is still owed. Stop here rather than skipping on:
            # a later rung may depend on this one.
            problem = str(exc)
            _logger.error(
                "state_database_migration_halted",
                schema_version=reached,
                halted_at=step_index + 1,
                detail=problem,
            )
            break
        reached = step_index + 1

    if reached > current:
        _logger.info(
            "state_database_migrated",
            schema_version_from=current,
            schema_version_to=reached,
        )
    return reached, problem


def _run_migration_step(
    conn: sqlite3.Connection, database_path: Path, step_index: int, migration: Migration
) -> None:
    """Apply one migration and its version bump as one atomic unit.

    Same shape as the audit store's, and for the same reason: the default
    isolation mode implicitly commits before DDL, which would split the step in
    two and let a crash leave half of it applied forever. Autocommit plus an
    explicit BEGIN is the sqlite3 idiom that makes DDL genuinely transactional
    — and it is what lets migration 0003 create a table and fill it from a file
    as one thing that either happened or did not.
    """
    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration(conn, database_path)
            conn.execute("UPDATE schema_version SET version = ?", (step_index + 1,))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation_level


def _key(door: AuthMethod | str, user_id: str, name: str) -> tuple[str, str, str]:
    """The cache key: door, identity, setting. ``str`` on the door because
    :class:`AuthMethod` is a ``StrEnum`` and a key that is sometimes a member
    and sometimes its value is a key that is hard to reason about."""
    return (str(door), user_id, name)


class PreferenceStore:
    """Every person's own settings, and the rule for resolving one."""

    def __init__(self, database_path: Path | str) -> None:
        self._path = Path(database_path)
        # Held for the whole of a write — the INSERT, the commit, and the cache
        # update — so two writers cannot interleave and so the cache can never
        # be left claiming something the database does not hold.
        #
        # **It is not held for a read, and that is safe rather than sloppy.**
        # A read is a single `dict.get` and a write's cache update is a single
        # `dict[key] = value`; each is one atomic bytecode step under CPython,
        # so a reader sees either the value before the write or the value after
        # it and never a half-built dict. There is no compound
        # read-modify-write anywhere in this class — nothing reads the cache and
        # then writes back a value derived from what it read.
        #
        # The next person to add a multi-key operation here (writing two
        # settings that must agree, or reading one to compute another) does not
        # inherit that safety and must take the lock: the atomicity of the
        # individual operations says nothing about a *sequence* of them.
        self._lock = threading.Lock()

        # `problem` is not this store's to report: it is set when a rung the
        # ladder stopped at could not import somebody else's file, and the
        # preference table comes from a rung that completed long before it.
        self._conn = open_state_database(self._path).connection

        with self._lock:
            self._cache: dict[tuple[str, str, str], str] = {
                _key(str(row["door"]), str(row["user_id"]), str(row["name"])): str(
                    row["value"]
                )
                for row in self._conn.execute(
                    "SELECT door, user_id, name, value FROM user_preferences"
                )
            }

    @property
    def path(self) -> Path:
        return self._path

    # -- one person's own choice ------------------------------------------

    def get_bool(self, door: AuthMethod | str, user_id: str, name: str) -> bool | None:
        """What this person chose, or ``None`` if they never said.

        ``None`` is the whole point of the return type: "never chose" has to be
        distinguishable from "chose off", or the built-in default could never
        be applied to a new person.

        Served from the cache, without the lock and without a thread hop — see
        the module docstring and the note beside the lock for why both are
        safe.
        """
        raw = self._cache.get(_key(door, user_id, name))
        if raw is None:
            return None
        return raw == "1"

    def set_bool(
        self, door: AuthMethod | str, user_id: str, name: str, value: bool
    ) -> None:
        """Store one person's choice. **Blocking — an ``async`` caller must
        reach this through ``asyncio.to_thread``** (module docstring)."""
        stored = "1" if value else "0"
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_preferences (door, user_id, name, value) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (door, user_id, name) DO UPDATE SET value = excluded.value",
                (str(door), user_id, name, stored),
            )
            self._conn.commit()
            self._cache[_key(door, user_id, name)] = stored

    # -- the resolved answer ----------------------------------------------

    def resolve_bool(
        self,
        door: AuthMethod | str,
        user_id: str,
        name: str,
        *,
        override: Override,
        default: bool,
    ) -> bool:
        """Administrator's rule first, then this person's, then the default.

        An override deliberately does not consult — or erase — what the person
        chose. Their choice stays in the table and is what they go back to if
        the override is ever lifted.
        """
        if override is Override.ON:
            return True
        if override is Override.OFF:
            return False
        chosen = self.get_bool(door, user_id, name)
        return default if chosen is None else chosen

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "PREFERENCES_FILENAME",
    "STATE_DATABASE_FILENAME",
    "Migration",
    "Override",
    "PreferenceStore",
    "SchemaDowngradeError",
    "StateDatabase",
    "StateMigrationError",
    "open_state_database",
]
