"""User accounts for the core's own sign-in — PC-283, PC-285, PC-290, PC-291.

One file, ``<appdata>/users/accounts.json``, beside the API keys that already
live there: spec Appendix B gives ``users/`` as "profiles, permissions, speaker
enrolment", and an account is a profile with a credential attached.

An account is four facts and no more — a name, a password hash, whether the
holder is an admin, and whether an administrator has marked them a minor.
PC-288's note draws the line this module stays behind: enough to recognise,
never a tracking record. There is no last-login field, no email, no display
name; none of them is needed to sign somebody in, and each one is a thing a
household member could read about another if a listing ever leaked (PC-290).

**Only an admin ever sees the list** (PC-290). That is enforced at the routes,
not here — a store that refuses to enumerate is a store that cannot be
administered — but every method that returns more than one record is named so
the call site reads as a decision.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from personacore.audit.logging import get_logger
from personacore.auth._files import AuthStoreError, read_json, write_json
from personacore.auth.passwords import (
    check_password_quality,
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)
from personacore.config.appdata import AppdataLayout

logger = get_logger(__name__)

ACCOUNTS_FILENAME = "accounts.json"
STORE_VERSION = 1

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
"""What a name may be, after lowercasing.

Narrow on purpose. The name is rendered into HTML, written into audit records
and used as a key in a JSON file, and every one of those is safer with a
character set that contains no separator, no whitespace and no control
character. It is not a display name — nothing here has one."""

NAME_REFUSED = (
    "That name cannot be used. Use letters, digits, dots, dashes or underscores, "
    "starting with a letter or digit, up to 64 characters - for example "
    "'kitchen' or 'alice.p'."
)


class AccountRejected(ValueError):
    """The account cannot be created or changed as asked. The message is shown
    to whoever asked, so it says what to do about it."""


class UserRecord(BaseModel):
    """One account. ``password_hash`` never leaves this process."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    """``ignore`` rather than ``forbid``, and the reason is rollback.

    ``_read`` drops any row that fails validation. Under ``forbid``, that made
    every field ever added to this file a one-way door: pull the previous
    container image — which CLAUDE.md names as the standard way to roll a
    release back — and every account carrying a key that build has never heard
    of fails validation and vanishes from the store. Not stripped of the field.
    Gone, including the last administrator, leaving a house nobody can sign
    into and a fix that needs a text editor on the appdata volume.

    ``is_minor`` is the first field to make that reachable, but it was true of
    this file from the day it was written. An unknown key is now ignored on the
    way in and lost on the next write, which is what rolling back should cost.
    """

    name: str = Field(min_length=1, max_length=64)
    password_hash: str = Field(min_length=1)
    is_admin: bool
    created_at: datetime

    is_minor: bool = False
    """Whether an administrator has marked this account a minor.

    **It grants nothing and denies nothing.** It is a review signal: the reason
    for hiding conversations rather than deleting them is so that a minor's
    activity can be reviewed, and the person reviewing needs to know which
    accounts that applies to. Anything that gates a capability on
    this field is a different decision and is not made here.

    Defaulted rather than required, which is the whole of its migration: an
    ``accounts.json`` written before this field existed has no key for it, and
    every account in it is correctly not a minor until somebody says otherwise.
    :data:`STORE_VERSION` is unchanged for the same reason — nothing on disk
    became unreadable, so there is no version to step.
    """


def normalise_name(raw: str) -> str:
    """Fold a submitted name to the one stored form, or refuse it.

    Case-insensitive, because "Alice" and "alice" being two accounts is a
    household support call, not a feature.
    """
    name = (raw or "").strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise AccountRejected(NAME_REFUSED)
    return name


class UserStore:
    """Reads and writes the account file.

    Re-reads whenever the file's mtime or size changes, exactly as
    :class:`personacore.api.keys.ApiKeyStore` does, so an account created
    through one surface is usable on the very next request without a restart.
    """

    def __init__(self, layout: AppdataLayout, *, filename: str = ACCOUNTS_FILENAME) -> None:
        self._path = layout.users / filename
        self._records: dict[str, UserRecord] = {}
        self._stamp: tuple[int, int] | None = None
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    # -- reading -----------------------------------------------------------

    def count(self) -> int:
        """How many accounts exist. The one fact first-run setup needs, and the
        only one it is safe to answer without knowing who is asking (PC-291)."""
        self._refresh()
        return len(self._records)

    def get(self, name: str) -> UserRecord | None:
        self._refresh()
        try:
            key = normalise_name(name)
        except AccountRejected:
            return None
        return self._records.get(key)

    def records(self) -> list[UserRecord]:
        """Every account, by name. **Admin-only at the call site** (PC-290)."""
        self._refresh()
        return sorted(self._records.values(), key=lambda record: record.name)

    def admin_count(self) -> int:
        """How many admins there are — the check that stops the last one being
        removed or demoted into a system nobody can administer."""
        self._refresh()
        return sum(1 for record in self._records.values() if record.is_admin)

    # -- verification ------------------------------------------------------

    def verify(self, name: str, password: str) -> UserRecord | None:
        """The account for this name and password, or ``None``.

        ``None`` covers unknown name and wrong password alike, because the
        caller must render both identically: a sign-in page that says "no such
        user" hands an attacker the household's member list, which is the
        failure PC-290 names.

        An unknown name still pays the full hashing cost
        (:func:`~personacore.auth.passwords.dummy_verify`), so the two cases
        cannot be told apart by how long they took either.
        """
        record = self.get(name)
        if record is None:
            dummy_verify(password)
            return None
        if not verify_password(password, record.password_hash):
            return None
        if needs_rehash(record.password_hash):
            # The reason the parameters are stored with the hash: the upgrade
            # happens on the one occasion the plaintext is legitimately in
            # hand, and never blocks the sign-in it rides along with.
            try:
                self.set_password(record.name, password, check_quality=False)
            except (AuthStoreError, AccountRejected) as exc:  # pragma: no cover - disk
                logger.warning("account_rehash_failed", user=record.name, error=str(exc))
        return record

    # -- writing -----------------------------------------------------------

    def create(
        self, name: str, password: str, *, is_admin: bool, is_minor: bool = False
    ) -> UserRecord:
        """Add an account. Refuses a name that is already taken.

        The caller decides ``is_admin``; PC-291's "the first account created is
        an admin" is a rule about the *first* one, enforced where first-run
        setup happens rather than here — a store that silently promoted
        whichever account happened to be first would also promote one restored
        from a backup into an empty volume.
        """
        key = normalise_name(name)
        check_password_quality(password)
        self._refresh()
        if key in self._records:
            raise AccountRejected(
                f"There is already an account called '{key}'. Pick another name, "
                "or sign in as that one."
            )
        record = UserRecord(
            name=key,
            password_hash=hash_password(password),
            is_admin=is_admin,
            is_minor=is_minor,
            created_at=datetime.now(UTC),
        )
        self._records[key] = record
        self._save()
        # The name and the flags, never the password and never the hash.
        logger.info("account_created", user=key, is_admin=is_admin, is_minor=is_minor)
        return record

    def set_password(self, name: str, password: str, *, check_quality: bool = True) -> bool:
        """Replace an account's password. Returns whether the account existed."""
        key = normalise_name(name)
        if check_quality:
            check_password_quality(password)
        self._refresh()
        record = self._records.get(key)
        if record is None:
            return False
        self._records[key] = record.model_copy(
            update={"password_hash": hash_password(password)}
        )
        self._save()
        logger.info("account_password_changed", user=key)
        return True

    def set_admin(self, name: str, is_admin: bool) -> bool:
        """Promote or demote. Refuses to demote the last admin."""
        key = normalise_name(name)
        self._refresh()
        record = self._records.get(key)
        if record is None:
            return False
        if record.is_admin and not is_admin and self.admin_count() <= 1:
            raise AccountRejected(
                "That is the only admin account. Make somebody else an admin first, "
                "or there will be nobody who can administer this assistant."
            )
        self._records[key] = record.model_copy(update={"is_admin": is_admin})
        self._save()
        logger.info("account_admin_changed", user=key, is_admin=is_admin)
        return True

    def set_minor(self, name: str, is_minor: bool) -> bool:
        """Mark an account a minor, or stop. Returns whether it existed.

        No sibling of :meth:`set_admin`'s last-admin rule, because there is
        nothing here to lock anybody out of: this flag decides nothing. It is
        read by the review screen and by nothing else, and an account with it
        set can do exactly what it could do before.
        """
        key = normalise_name(name)
        self._refresh()
        record = self._records.get(key)
        if record is None:
            return False
        self._records[key] = record.model_copy(update={"is_minor": is_minor})
        self._save()
        logger.info("account_minor_changed", user=key, is_minor=is_minor)
        return True

    def delete(self, name: str) -> bool:
        """Remove an account. Refuses to remove the last admin."""
        key = normalise_name(name)
        self._refresh()
        record = self._records.get(key)
        if record is None:
            return False
        if record.is_admin and self.admin_count() <= 1:
            raise AccountRejected(
                "That is the only admin account. Removing it would leave nobody who "
                "can administer this assistant. Make somebody else an admin first."
            )
        del self._records[key]
        self._save()
        logger.info("account_deleted", user=key)
        return True

    def reload(self) -> None:
        self._stamp = None
        self._loaded = False

    # -- persistence -------------------------------------------------------

    def _refresh(self) -> None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            # A fresh volume has no accounts. That is first run (PC-291), not
            # an error, and it is what makes the setup page appear.
            self._records = {}
            self._stamp = None
            self._loaded = True
            return
        except OSError as exc:
            logger.error("account_file_unreadable", error=str(exc), path=str(self._path))
            self._records = {}
            self._loaded = True
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._loaded and stamp == self._stamp:
            return
        self._records = self._read()
        self._stamp = stamp
        self._loaded = True

    def _read(self) -> dict[str, UserRecord]:
        raw = read_json(self._path, kind="account")
        if raw is None or not isinstance(raw.get("users"), list):
            if raw is not None:
                logger.error("account_file_invalid", error="unexpected shape")
            return {}
        records: dict[str, UserRecord] = {}
        for entry in raw["users"]:
            try:
                record = UserRecord.model_validate(entry)
            except ValueError as exc:
                # One unusable row never disables the others, and is never
                # silently skipped: somebody has an account they cannot use.
                logger.error("account_entry_invalid", error=str(exc))
                continue
            records[record.name] = record
        return records

    def _save(self) -> None:
        write_json(
            self._path,
            {
                "version": STORE_VERSION,
                "users": [record.model_dump(mode="json") for record in self._records.values()],
            },
            kind="account",
        )
        self._stamp = None


__all__ = [
    "ACCOUNTS_FILENAME",
    "NAME_PATTERN",
    "NAME_REFUSED",
    "AccountRejected",
    "UserRecord",
    "UserStore",
    "normalise_name",
]
