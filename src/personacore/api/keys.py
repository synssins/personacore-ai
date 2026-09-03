"""Per-client API keys for the exposed OpenAI-compatible API — spec section 5.4.

Section 5.4 is unambiguous: "**Auth required:** per-client API keys, issued and
revoked in the admin UI. No anonymous access, even on the LAN." This module is
the storage and verification half of that sentence. The issuing half belongs to
the admin API (section 9) — see :class:`ApiKeyStore`.

Three decisions worth stating, because each one is a security property rather
than a preference:

1. **Only the hash is stored.** A stolen appdata backup (section 10 copies this
   file nightly) must not hand the thief a working key. The stored value is a
   SHA-256 digest of the whole presented key. A password KDF — bcrypt, argon2 —
   is deliberately *not* used and would not help: a key is 256 bits of
   ``secrets.token_urlsafe`` entropy, so there is no dictionary to run against
   it, and a slow KDF on the hot path of every request would spend section 10's
   latency budget for nothing. That reasoning holds only because this module is
   the *only* thing that mints keys; a human-chosen key would need a KDF.
2. **Comparison is constant time and does not stop early.** :meth:`verify`
   walks every record with :func:`hmac.compare_digest` and no ``break``, so
   neither the comparison itself nor the number of records examined varies with
   how close a guess was. This is why the hash is not looked up with a
   ``WHERE key_hash = ?``, which would be the obvious SQL and the wrong one.
3. **A key can never be anonymous.** Section 5.4 forbids anonymous access on
   this surface, and ADR-0003's anonymous tier is a different surface entirely,
   so :class:`ApiKeyRecord` refuses to hold a profile of that kind rather than
   trusting whoever fills in the admin form.

Where the keys live: the ``api_keys`` table in ``<appdata>/state/personacore.db``,
the durable-state database ADR-0030 created, reached through
:func:`~personacore.preferences.store.open_state_database` and created by
migration 0003 of that file's one ladder.

**Not ``audit.db``**, whose contents the retention purge trims on a timer. A key
must never age out, and "clear the audit log" must never mean "lose every
client's credential". Same lifetime argument ADR-0030 made for settings.

**And no longer ``<appdata>/users/api-keys.json``, which had a bug that ate
keys.** The JSON store read a corrupt file as "no keys" — swallowing the parse
error — and every write then serialised the whole in-memory set back over the
file. So one truncated write, one bad edit, one half-copied backup, and the
next key issued would silently destroy every other key in the house. That shape
is gone: a row is an ``INSERT`` and never a rewrite of the set, a commit is a
transaction, and a store that did not load reports itself **unusable** rather
than empty. The JSON file is imported once and then left on disk, ignored; see
migration 0003 for why it is not deleted.

**A damaged legacy file does not stop the core, and does not open the door
either.** The migration refuses it, the ladder halts with the import still
owed, and ``create_app`` completes carrying the reason — because a container
that will not start has no admin UI, and the admin UI is where the operator
would go to find out what is wrong. What the store then does with that reason
is the point: :meth:`ApiKeyStore.verify` fails closed so every ``/v1`` request
401s, :meth:`ApiKeyStore.records` raises the sentence instead of returning an
empty list, and issuing or revoking is refused outright. An operator shown "no
keys" issues a replacement, and under the JSON store that write was what
destroyed the file it had failed to read.

**Reads go to the table, deliberately, and are not cached.** The preference
store caches its whole table in a dict and says so; this one must not, because
a key is security state and a cache is a window during which a revoked key
still works. The admin surface and the ``/v1`` surface are separate
``ApiKeyStore`` objects over one file (see ``server.py``), so an in-process
cache would not even be one window but two, and neither would be invalidated by
the other's revoke.

The cost was measured rather than assumed: with 25 keys in the table, a
:meth:`ApiKeyStore.verify` that finds nothing takes ~37 µs and one that matches
~59 µs, on the machine this was written on. That is against a request about to
wait on an LLM for a second or more, so "revoked means revoked now" is bought
outright rather than approximately. If a deployment ever holds enough keys for
that to stop being true, the answer is not a cache — it is a cache *with*
invalidation between the two surfaces, which is a different piece of work and
should be measured before it is written.

**Writes block, and an async caller must offload them**, as with the preference
store: a WAL commit fsyncs, and a stall on the single event loop is a stall on
``/health`` and every in-flight chat turn. The admin routes already reach
:meth:`ApiKeyStore.issue` and :meth:`ApiKeyStore.revoke` through
``asyncio.to_thread``. The connection is opened ``check_same_thread=False`` and
every statement holds this object's lock, so a worker thread is safe.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from personacore.audit.logging import get_logger
from personacore.config.appdata import AppdataLayout
from personacore.contracts.policy import PolicyProfile, ProfileKind
from personacore.preferences.store import STATE_DATABASE_FILENAME, open_state_database

logger = get_logger(__name__)

KEY_FILENAME = "api-keys.json"
"""The retired JSON store inside ``<appdata>/users/``.

Kept as a name because operators, docs and backups know it. Nothing reads it
after migration 0003 has imported it once; editing it changes nothing.
"""

KEY_PREFIX = "personacore_"
"""Leading marker on every issued key. Not a security feature — it exists so a
key pasted into a bug report, a config file or a git diff is recognisable as a
PersonaCore credential, by a secret scanner and by eye.

**The word, not an abbreviation, and an underscore rather than a dash.** This
was ``pck-``, which did the job for anyone who had read this file and meant
nothing to anyone who had not — and the point of the marker is to be
recognisable to a stranger, or to a scanner somebody else wrote. It follows the
convention the ecosystem settled on: ``ghp_``, ``xoxb-``, ``sk_live_``. The
underscore also survives a double-click, which selects the whole key in most
terminals and editors where a dash would break it in two — and a credential is
something people paste.

Nothing validates the prefix; it is a label, not a check. Changed while no key
had been issued, so there is nothing in the world wearing the old one.
"""

KEY_ENTROPY_BYTES = 32
STORE_VERSION = 1
"""Version stamp of the retired JSON format. Not the schema version of the
table, which is the state database's ``schema_version`` and nothing to do with
this."""

_ID_ATTEMPTS = 8
"""How many times :meth:`ApiKeyStore.issue` will re-roll a colliding key id
before giving up. A collision needs two matching draws from 2**64, so more than
one attempt is already unreachable; the loop exists so that the *impossible*
case is a refusal with a sentence rather than an unhandled IntegrityError."""


class ApiKeyError(RuntimeError):
    """The key store itself is unusable. Message text reaches an operator, so it
    says what to do, not merely what failed.

    Deliberately *not* raised by :meth:`ApiKeyStore.verify`: a broken store must
    fail closed — every request 401s — rather than becoming a 500 that confirms
    to an unauthenticated prober that there is a real store back here. The admin
    surface, whose caller is already authenticated, gets the error instead of an
    empty list, because "no keys issued" and "the database would not open" must
    never look the same to the person deciding whether to issue another one.
    """


class ApiKeyRecord(BaseModel):
    """One issued key: the credential's fingerprint plus the policy it carries.

    The policy travelling *with* the key is the whole point of section 5.4's
    per-client profiles — "a dumb display widget should not be able to unlock
    doors". Authentication and authorisation are therefore one lookup, and no
    code path can authenticate a key and then go looking for its permissions
    somewhere else.
    """

    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(min_length=1)
    """Public identifier, safe to show in the admin UI, in logs and in audit
    records. Not derived from the key material."""

    key_hash: str = Field(min_length=64, max_length=64)
    """Hex SHA-256 of the whole key string, prefix included."""

    created_at: datetime
    profile: PolicyProfile
    """What this key may do. ``profile.enabled`` is the on/off switch: a
    disabled key is refused at the door with the same 401 an unknown key gets,
    rather than being let in to be refused later by the agent loop."""

    note: str = ""
    """Free text from whoever issued it — "kitchen display", "study lamp
    script". Operator convenience; never a place for the key itself."""

    @model_validator(mode="after")
    def _refuse_anonymous(self) -> ApiKeyRecord:
        # Spec section 5.4: no anonymous access on this surface, even on the
        # LAN. ADR-0003's anonymous tier belongs to the chat UI, not here, so a
        # key attributing its traffic to the anonymous profile would be a
        # laundering route between two access tiers meant to stay separate.
        if self.profile.kind is ProfileKind.ANONYMOUS:
            raise ValueError(
                "an API key cannot carry the anonymous profile — the exposed API "
                "has no anonymous tier (spec section 5.4)"
            )
        return self


class IssuedKey(BaseModel):
    """The one and only moment the plaintext key exists outside the client.

    ``SecretStr`` so a stray ``repr``, an f-string in a log call or a pydantic
    validation error carries ``**********`` instead of a live credential. The
    admin API calls ``.get_secret_value()`` exactly once, to put it on screen.
    """

    model_config = ConfigDict(extra="forbid")

    key: SecretStr
    record: ApiKeyRecord


_SELECT_ALL = "SELECT key_id, key_hash, created_at, note, profile FROM api_keys"
_SELECT_ONE = f"{_SELECT_ALL} WHERE key_id = ?"
_SELECT_ORDERED = f"{_SELECT_ALL} ORDER BY created_at, key_id"
_INSERT = (
    "INSERT INTO api_keys (key_id, key_hash, created_at, note, profile) "
    "VALUES (?, ?, ?, ?, ?)"
)


class ApiKeyStore:
    """Reads, verifies and (for the admin side) writes the API-key table.

    **Issuing and revoking are an admin-API concern, not this package's
    router.** ``personacore.api.openai`` only ever calls :meth:`verify`.
    :meth:`issue`, :meth:`revoke`, :meth:`set_enabled`, :meth:`replace_profile`
    and :meth:`records` exist for whoever builds spec section 9's "user/profile
    management and API-key issuance" screen: drive this class from there,
    behind that surface's own authentication.

    Every read goes to the table, so a key issued or revoked by the admin API
    takes effect on the very next request — the two surfaces hold different
    ``ApiKeyStore`` objects over one database and must not have to share an
    in-memory object to agree on what is valid. See the module docstring for
    why nothing here is cached.
    """

    def __init__(
        self, layout: AppdataLayout, *, database_path: Path | str | None = None
    ) -> None:
        self._layout = layout
        self._path = (
            Path(database_path)
            if database_path is not None
            else layout.state / STATE_DATABASE_FILENAME
        )
        # Every statement is issued under this. One sqlite3 connection is
        # shared between the event loop (reads) and worker threads (writes),
        # and a read-modify-write here — `set_enabled`, `replace_profile` —
        # is a SELECT and an UPDATE that must not have another writer between
        # them.
        self._lock = threading.Lock()
        opened = open_state_database(self._path)
        self._conn = opened.connection
        self._unusable = opened.problem
        if self._unusable is not None:
            # Said once, at the boundary, rather than on every request. The
            # sentence is kept so the admin surface can show it to the one
            # person who can act on it; a log line alone would be a fault
            # nobody sees until they go looking for it.
            logger.error(
                "api_key_store_unusable", detail=self._unusable, path=str(self._path)
            )

    @property
    def path(self) -> Path:
        """The state database. Exposed for the admin UI's health screen.

        This used to be the JSON file, and is a database now; anything printing
        it to an operator is naming a file they should not be editing either
        way.
        """
        return self._path

    @property
    def legacy_json_path(self) -> Path:
        """``<appdata>/users/api-keys.json`` — imported once by migration 0003,
        left on disk, and read by nothing since."""
        return self._layout.users / KEY_FILENAME

    # -- the read path ----------------------------------------------------

    def verify(self, presented: str | None) -> ApiKeyRecord | None:
        """Return the record for a presented key, or ``None``.

        ``None`` covers every failure there is — absent, malformed, unknown,
        disabled, and a database that will not answer — because the caller must
        render them identically (spec section 5.4: a caller learns nothing
        about which keys exist). Which one it was gets logged and audited on
        the caller's side, never returned here as a distinguishable value.

        Hits the table on every call. That is the point: a revoke must be true
        immediately, and a cache is exactly a window in which it is not.
        """
        if not presented:
            return None
        if self._unusable is not None:
            # The keys that exist have not been loaded, so there is nothing to
            # match against and no way to know whether this one is real. A
            # store that cannot answer must never admit anybody.
            return None
        digest = _digest(presented)
        try:
            rows = self._select(_SELECT_ALL)
        except sqlite3.Error as exc:
            # Fail closed, and loudly in the operator's log. Not an exception:
            # see ApiKeyError. Nothing is written, so nothing is lost.
            logger.error("api_key_database_unreadable", error=str(exc), path=str(self._path))
            return None

        # No early exit: every row is compared, so the time taken does not
        # reveal how far down the table a near-miss matched. Only the winner is
        # turned into a model, because parsing every profile on every request
        # would be work done to no one's benefit.
        found: sqlite3.Row | None = None
        for row in rows:
            if hmac.compare_digest(str(row["key_hash"]), digest):
                found = row
        if found is None:
            return None
        try:
            record = _decode(found)
        except ValueError as exc:
            # A row the current models refuse. Nobody can use that key, and
            # letting it through unvalidated would be worse than refusing it.
            logger.error(
                "api_key_row_invalid", key_id=str(found["key_id"]), error=str(exc)
            )
            return None
        if not record.profile.enabled:
            return None
        return record

    def reload(self) -> None:
        """Kept for callers that remember the file-backed store; does nothing.

        There is no cache to invalidate any more — every read is a query, so a
        write made through another object is already visible.
        """

    # -- the admin path ---------------------------------------------------

    def records(self) -> list[ApiKeyRecord]:
        """Every issued key. Hashes included: a hash is not a credential, and
        the admin UI needs something stable to key its rows on.

        Raises :class:`ApiKeyError` rather than returning ``[]`` if the store
        is unusable — the database will not answer, or the ladder halted with
        the legacy import still owed. The old store returned an empty list in
        both cases and that is the bug this move exists to kill: an operator
        seeing "no keys" issues another one, and under the JSON store that
        write destroyed the file it could not read.
        """
        rows = self._read_rows(_SELECT_ORDERED)
        return [self._decode_or_raise(row) for row in rows]

    def get(self, key_id: str) -> ApiKeyRecord | None:
        rows = self._read_rows(_SELECT_ONE, (key_id,))
        if not rows:
            return None
        return self._decode_or_raise(rows[0])

    def issue(self, *, profile: PolicyProfile, note: str = "") -> IssuedKey:
        """Mint a key for ``profile`` and persist its hash.

        The returned plaintext is not recoverable afterwards. That is the
        point: losing it costs one re-issue, whereas storing it costs the
        household its front door.

        The record is built — and therefore validated, which is where an
        anonymous profile is refused — before anything touches the database, so
        a refused profile writes nothing.
        """
        self._refuse_if_unusable(writing=True)
        secret = f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_ENTROPY_BYTES)}"
        created_at = datetime.now(UTC)
        for _attempt in range(_ID_ATTEMPTS):
            record = ApiKeyRecord(
                key_id=secrets.token_hex(8),
                key_hash=_digest(secret),
                created_at=created_at,
                profile=profile,
                note=note,
            )
            try:
                with self._lock:
                    self._conn.execute(_INSERT, _encode(record))
                    self._conn.commit()
            except sqlite3.IntegrityError:  # pragma: no cover - 2**64 collision
                self._rollback()
                continue
            except sqlite3.Error as exc:
                self._rollback()
                raise _write_failed(self._path, exc) from exc
            logger.info(
                "api_key_issued",
                key_id=record.key_id,
                profile_id=profile.id,
                profile_kind=profile.kind.value,
            )
            return IssuedKey(key=SecretStr(secret), record=record)
        raise ApiKeyError(  # pragma: no cover - 2**64 collision, eight times
            f"Could not find an unused key id after {_ID_ATTEMPTS} attempts. "
            "This should be impossible; check the api_keys table in "
            f"{self._path} has not been filled with rows by something else."
        )

    def revoke(self, key_id: str) -> bool:
        """Delete a key outright. Returns whether there was one to delete.

        Idempotent: revoking an already-revoked key is a no-op that returns
        ``False``, never an error. The admin API records both outcomes as a
        success, because the operator asked for a state and got it.

        Revocation removes the row rather than flagging it: a hash nobody can
        match is not worth keeping, and spec section 7's audit log already
        holds the history of what that key did.
        """
        self._refuse_if_unusable(writing=True)
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM api_keys WHERE key_id = ?", (key_id,)
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            self._rollback()
            raise _write_failed(self._path, exc) from exc
        if cursor.rowcount <= 0:
            return False
        logger.info("api_key_revoked", key_id=key_id)
        return True

    def set_enabled(self, key_id: str, enabled: bool) -> bool:
        """Turn a key off without destroying it — the "pause the widget I am
        debugging" case. A disabled key gets the same 401 as an unknown one."""
        self._refuse_if_unusable(writing=True)
        record = self.get(key_id)
        if record is None:
            return False
        updated = record.profile.model_copy(update={"enabled": enabled})
        self._write_profile(key_id, updated)
        logger.info("api_key_enabled_changed", key_id=key_id, enabled=enabled)
        return True

    def replace_profile(self, key_id: str, profile: PolicyProfile) -> bool:
        """Re-point an existing key at a different policy without re-issuing it
        — the admin UI's "this widget may now also read the calendar"."""
        self._refuse_if_unusable(writing=True)
        record = self.get(key_id)
        if record is None:
            return False
        # Re-validated through the model rather than `model_copy`, which does
        # not run validators: a key must not be able to acquire the anonymous
        # profile by the back door of an edit when it could not have been
        # issued with one. The old store's `model_copy` here let it.
        ApiKeyRecord.model_validate(
            {**record.model_dump(mode="json"), "profile": profile.model_dump(mode="json")}
        )
        self._write_profile(key_id, profile)
        logger.info("api_key_policy_changed", key_id=key_id, profile_id=profile.id)
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- persistence ------------------------------------------------------

    def _select(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """One query under the lock. ``sqlite3.Error`` is the caller's to catch:
        the read path swallows it into a 401 and the admin path turns it into an
        :class:`ApiKeyError`, and those must not be decided here."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _read_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        self._refuse_if_unusable()
        try:
            return self._select(sql, params)
        except sqlite3.Error as exc:
            raise ApiKeyError(
                f"Could not read the API keys from {self._path}: {exc}. The state "
                "database is unreadable — check the appdata volume is mounted and "
                "restore it from a backup if the file is damaged. No keys have "
                "been changed."
            ) from exc

    def _decode_or_raise(self, row: sqlite3.Row) -> ApiKeyRecord:
        try:
            return _decode(row)
        except ValueError as exc:
            raise ApiKeyError(
                f"The API key {str(row['key_id'])!r} in {self._path} cannot be "
                f"read: {exc}. Revoke it from the admin UI and issue a "
                "replacement; the rest of the keys are unaffected."
            ) from exc

    def _write_profile(self, key_id: str, profile: PolicyProfile) -> None:
        self._refuse_if_unusable(writing=True)
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE api_keys SET profile = ? WHERE key_id = ?",
                    (_dump_profile(profile), key_id),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            self._rollback()
            raise _write_failed(self._path, exc) from exc

    def _refuse_if_unusable(self, *, writing: bool = False) -> None:
        """Report an unusable store, in the sentence the migration wrote.

        **Unusable is not empty, and the difference is the whole point.** An
        operator shown an empty listing issues a replacement key — which, under
        the JSON store this replaced, was the very act that overwrote the file
        it had failed to read. So a store that did not load says so, in a
        sentence that names the file and what is wrong with it, and refuses to
        write until it is fixed.
        """
        if self._unusable is None:
            return
        if writing:
            raise ApiKeyError(
                f"{self._unusable} No key can be issued or revoked until then, "
                "because the keys that already exist have not been loaded."
            )
        raise ApiKeyError(self._unusable)

    def _rollback(self) -> None:
        """Put the connection back to a known state after a failed statement.

        sqlite3 opens a transaction implicitly on the first DML, and a failure
        leaves it open — so without this, the *next* write on this connection
        would inherit it."""
        try:
            self._conn.rollback()
        except sqlite3.Error:  # pragma: no cover - the connection is already lost
            pass


def _write_failed(path: Path, exc: sqlite3.Error) -> ApiKeyError:
    return ApiKeyError(
        f"Could not write to the API-key table in {path}: {exc}. Check the "
        "appdata volume is mounted and writable by the user the container runs "
        "as. Nothing was changed."
    )


def _dump_profile(profile: PolicyProfile) -> str:
    """The profile as one JSON column. ``sort_keys`` so an unchanged profile
    produces an unchanged string and a diff of two dumps means something."""
    return json.dumps(profile.model_dump(mode="json"), sort_keys=True)


def _encode(record: ApiKeyRecord) -> tuple[str, str, str, str, str]:
    """A record as its row. ``created_at`` goes in as the ISO-8601 string
    pydantic produces and comes back through the same model, so the timestamp
    that returns is the one that went in, timezone included."""
    return (
        record.key_id,
        record.key_hash,
        record.created_at.isoformat(),
        record.note,
        _dump_profile(record.profile),
    )


def _decode(row: sqlite3.Row) -> ApiKeyRecord:
    """A row back into a record, validated. Raises ``ValueError`` — pydantic's
    ``ValidationError`` is one — if the row is not something a key can be."""
    return ApiKeyRecord.model_validate(
        {
            "key_id": row["key_id"],
            "key_hash": row["key_hash"],
            "created_at": row["created_at"],
            "note": row["note"],
            "profile": json.loads(row["profile"]),
        }
    )


def _digest(key: str) -> str:
    """SHA-256 of a key. The module docstring says why this is not a KDF."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


__all__ = [
    "KEY_FILENAME",
    "KEY_PREFIX",
    "ApiKeyError",
    "ApiKeyRecord",
    "ApiKeyStore",
    "IssuedKey",
]
