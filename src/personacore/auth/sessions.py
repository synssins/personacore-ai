"""Sessions for the core's own sign-in — PC-286, PC-287, PC-288, PC-289.

A session, not HTTP Basic. Basic re-sends the password on every single request;
a session sends it once, at sign-in, and everything after that carries a token
that can be revoked. On a household LAN without TLS (PC-286) that difference is
the whole game: one credential crossing the wire once is recoverable, the same
credential crossing it a thousand times a day is not.

**Several sessions per user, at once** (PC-287). A phone, a laptop and a
kitchen display are one person. Nothing here is keyed by user alone, and
creating a session never touches the ones that already exist.

**Server-side.** The cookie carries a random token and nothing else — no user
name, no claims, no signature to get wrong. Everything about a session is in
this file, which is what makes PC-288 and PC-289 possible at all: a stateless
token cannot be ended before it expires.

**Only the digest is stored.** Like the API keys next to it, the file holds a
SHA-256 of the token. A token is 256 bits from ``secrets.token_urlsafe`` with
no dictionary behind it, so a password KDF would buy nothing and would cost a
scrypt run on every request.

**What a session shows** (PC-288's note): when it started, when it was last
used, and whether it is the one you are looking at. Deliberately not: an IP
address, a user-agent string, a location. Those turn "the control that matters
after losing a device" into a record of where each household member has been.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from personacore.audit.logging import get_logger
from personacore.auth._files import read_json, write_json
from personacore.config.appdata import AppdataLayout

logger = get_logger(__name__)

SESSIONS_FILENAME = "sessions.json"
STORE_VERSION = 1

SESSION_COOKIE = "personacore_session"
"""Name of the cookie the token rides in. HttpOnly always, ``Secure`` when the
request arrived over HTTPS, ``SameSite=Lax`` always — see
:func:`cookie_settings`."""

SESSION_LIFETIME = timedelta(days=7)
"""How long a session lasts, from when it was created.

**Absolute, not sliding.** An expired session is refused, never renewed on the
way past: a sliding window means a stolen token stays alive for as long as the
thief keeps using it, which is exactly backwards. Seven days rather than a few
hours because a kitchen display that signs itself out every morning is a
display somebody unplugs, and because PC-288 and PC-289 give a real revocation
control that does not have to wait for an expiry."""

LAST_SEEN_RESOLUTION = timedelta(minutes=1)
"""How finely ``last_seen_at`` is kept.

Two reasons, and they point the same way. Writing a JSON file on every request
would make the session store the slowest thing in the stack; and a
second-accurate record of when each household member touched the interface is
the tracking record PC-288's note rules out. A minute is enough to recognise a
session by and too coarse to follow anybody around with."""

TOKEN_BYTES = 32


class SessionRecord(BaseModel):
    """One signed-in device."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1, max_length=64)
    """Public identifier, safe to render. Not derived from the token."""

    user: str = Field(min_length=1, max_length=64)
    token_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


def cookie_settings(*, secure: bool) -> dict[str, object]:
    """The flags every session cookie is set with.

    One dict so the sign-in page and the JSON API cannot set different ones.

    ``httponly`` always: a session token readable from JavaScript is a session
    token any injected script can take, and the admin surface renders plugin
    text (spec section 7 treats all of it as untrusted).

    ``secure`` only when the request arrived over HTTPS. Hard-coding it would
    silently break the plain-HTTP LAN deployment PC-286 exists to be honest
    about — the browser would drop the cookie and sign-in would appear to do
    nothing at all.

    ``samesite="lax"``: the admin surface has state-changing POSTs on it, and
    Lax is the floor that stops another site posting to them with the cookie
    attached while still letting an ordinary link into the UI work.
    """
    return {"httponly": True, "secure": secure, "samesite": "lax", "path": "/"}


def request_is_secure(scheme: str | None, forwarded_proto: str | None) -> bool:
    """Whether this request arrived over HTTPS.

    ``forwarded_proto`` is ``X-Forwarded-Proto`` and is only consulted because
    the supported deployment terminates TLS at a proxy in front (spec section
    7), where the core itself always sees ``http``. It is a header, so it is
    untrusted input: believing it can only ever *add* the ``Secure`` flag,
    which is the safe direction to be wrong in — a cookie marked Secure on a
    connection that is not simply stops working, rather than leaking.
    """
    if (scheme or "").lower() == "https":
        return True
    return (forwarded_proto or "").split(",")[0].strip().lower() == "https"


class SessionStore:
    """Reads and writes the session file.

    Held in memory and re-read on change, like the account store beside it, so
    a session ended on one surface is gone on the next request to the other.
    """

    def __init__(self, layout: AppdataLayout, *, filename: str = SESSIONS_FILENAME) -> None:
        self._path = layout.users / filename
        self._records: dict[str, SessionRecord] = {}
        self._stamp: tuple[int, int] | None = None
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    # -- the request path --------------------------------------------------

    def lookup(self, token: str | None, *, now: datetime | None = None) -> SessionRecord | None:
        """The session this token names, or ``None``.

        ``None`` covers absent, unknown and expired alike — the caller renders
        all three as "sign in", because telling them apart tells a prober which
        tokens once existed.

        **An expired session is refused, not renewed.** It is also deleted on
        the way past, so the file does not accumulate every device the
        household has ever used.
        """
        if not token:
            return None
        moment = now or datetime.now(UTC)
        self._refresh()
        digest = _digest(token)
        # No early exit, for the same reason the API-key store has none: the
        # time taken must not say how far down the file a near-miss matched.
        found: SessionRecord | None = None
        for record in self._records.values():
            if hmac.compare_digest(record.token_hash, digest):
                found = record
        if found is None:
            return None
        if found.is_expired(now=moment):
            self._records.pop(found.session_id, None)
            self._save()
            logger.info("session_expired", session_id=found.session_id, user=found.user)
            return None
        self._touch(found, moment)
        return found

    def _touch(self, record: SessionRecord, now: datetime) -> None:
        """Advance ``last_seen_at``, at :data:`LAST_SEEN_RESOLUTION` at best.

        Never moves ``expires_at``: see :data:`SESSION_LIFETIME`.
        """
        if now - record.last_seen_at < LAST_SEEN_RESOLUTION:
            return
        self._records[record.session_id] = record.model_copy(update={"last_seen_at": now})
        self._save()

    # -- sign in and out ---------------------------------------------------

    def create(
        self, user: str, *, lifetime: timedelta = SESSION_LIFETIME
    ) -> tuple[str, SessionRecord]:
        """Mint a session for ``user``, returning ``(token, record)``.

        The token is returned exactly once and is never recoverable from the
        file. Existing sessions for the same user are left alone (PC-287).
        """
        self._refresh()
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        session_id = secrets.token_hex(8)
        while session_id in self._records:  # pragma: no cover - 2**64 collision
            session_id = secrets.token_hex(8)
        record = SessionRecord(
            session_id=session_id,
            user=user,
            token_hash=_digest(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + lifetime,
        )
        self._records[session_id] = record
        self._purge_expired(now)
        self._save()
        logger.info("session_created", session_id=session_id, user=user)
        return token, record

    def end(self, session_id: str) -> bool:
        """End one session. Returns whether there was one to end."""
        self._refresh()
        if self._records.pop(session_id, None) is None:
            return False
        self._save()
        logger.info("session_ended", session_id=session_id)
        return True

    def end_by_token(self, token: str | None) -> bool:
        """End whichever session this token names — sign-out.

        Takes the token rather than an id so signing out needs nothing from the
        page but the cookie the browser already sends, and so a token whose
        session has already been ended elsewhere is a quiet no-op.
        """
        if not token:
            return False
        self._refresh()
        digest = _digest(token)
        for record in list(self._records.values()):
            if hmac.compare_digest(record.token_hash, digest):
                return self.end(record.session_id)
        return False

    def end_all_for(self, user: str) -> int:
        """End every session this user holds — PC-288 and PC-289 in one call.

        The same operation whether a user is ending their own after losing a
        phone, or an admin is revoking somebody's access: who is allowed to ask
        is decided at the route, and there is one implementation so the two
        cannot end up meaning different things.
        """
        self._refresh()
        doomed = [record.session_id for record in self._records.values() if record.user == user]
        for session_id in doomed:
            del self._records[session_id]
        if doomed:
            self._save()
            logger.info("sessions_ended_for_user", user=user, count=len(doomed))
        return len(doomed)

    # -- listing -----------------------------------------------------------

    def for_user(self, user: str, *, now: datetime | None = None) -> list[SessionRecord]:
        """This user's live sessions, newest first (PC-288)."""
        moment = now or datetime.now(UTC)
        self._refresh()
        return sorted(
            (
                record
                for record in self._records.values()
                if record.user == user and not record.is_expired(now=moment)
            ),
            key=lambda record: record.created_at,
            reverse=True,
        )

    def count_for(self, user: str, *, now: datetime | None = None) -> int:
        return len(self.for_user(user, now=now))

    def session_id_for_token(self, token: str | None) -> str | None:
        """Which listed session is the one on this screen. Never the token."""
        if not token:
            return None
        self._refresh()
        digest = _digest(token)
        for record in self._records.values():
            if hmac.compare_digest(record.token_hash, digest):
                return record.session_id
        return None

    def reload(self) -> None:
        self._stamp = None
        self._loaded = False

    # -- persistence -------------------------------------------------------

    def _purge_expired(self, now: datetime) -> None:
        for session_id, record in list(self._records.items()):
            if record.is_expired(now=now):
                del self._records[session_id]

    def _refresh(self) -> None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            self._records = {}
            self._stamp = None
            self._loaded = True
            return
        except OSError as exc:
            logger.error("session_file_unreadable", error=str(exc), path=str(self._path))
            self._records = {}
            self._loaded = True
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._loaded and stamp == self._stamp:
            return
        self._records = self._read()
        self._stamp = stamp
        self._loaded = True

    def _read(self) -> dict[str, SessionRecord]:
        raw = read_json(self._path, kind="session")
        if raw is None or not isinstance(raw.get("sessions"), list):
            if raw is not None:
                logger.error("session_file_invalid", error="unexpected shape")
            return {}
        records: dict[str, SessionRecord] = {}
        for entry in raw["sessions"]:
            try:
                record = SessionRecord.model_validate(entry)
            except ValueError as exc:
                logger.error("session_entry_invalid", error=str(exc))
                continue
            records[record.session_id] = record
        return records

    def _save(self) -> None:
        write_json(
            self._path,
            {
                "version": STORE_VERSION,
                "sessions": [
                    record.model_dump(mode="json") for record in self._records.values()
                ],
            },
            kind="session",
        )
        self._stamp = None


def _digest(token: str) -> str:
    """SHA-256 of a token. The module docstring says why this is not a KDF."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "LAST_SEEN_RESOLUTION",
    "SESSIONS_FILENAME",
    "SESSION_COOKIE",
    "SESSION_LIFETIME",
    "SessionRecord",
    "SessionStore",
    "cookie_settings",
    "request_is_secure",
]
