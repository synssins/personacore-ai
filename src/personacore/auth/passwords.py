"""Password hashing for the core's own sign-in — PC-285.

``hashlib.scrypt`` from the standard library. PC-285's note is explicit about
why: it "is in the standard library and adequate, so this costs no new
dependency", and CLAUDE.md forbids adding one without approval.

Three properties this module exists to hold:

1. **The stored value is never reversible.** A stolen appdata backup (spec
   section 10 copies the volume) must not hand the thief a working password
   people have reused elsewhere.
2. **The parameters travel with the hash.** The encoded string carries ``n``,
   ``r`` and ``p``, so raising the work factor later does not invalidate the
   accounts already stored — :func:`needs_rehash` says which ones are behind,
   and they are upgraded the next time their owner signs in.
3. **An unknown user costs the same as a known one.** :func:`dummy_verify`
   burns the same work, so the time a sign-in takes does not say whether the
   account exists. Enumeration through timing is the failure PC-290 is about,
   arrived at from the other side.

The encoding is one line of printable ASCII::

    scrypt$16384$8$1$<base64 salt>$<base64 derived key>

Chosen over JSON-per-field so the whole credential is one opaque string in the
account file — nothing downstream can accidentally render half of it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCHEME = "scrypt"

DEFAULT_N = 2**14
"""CPU/memory cost. 16384 needs ~16 MiB and ~50 ms on the CPU-only hardware
this project targets (CLAUDE.md's hard constraint), which is slow enough to
make offline guessing expensive and fast enough that a kitchen display signing
in does not appear to hang."""

DEFAULT_R = 8
DEFAULT_P = 1
DKLEN = 32
SALT_BYTES = 16

MAXMEM = 256 * 1024 * 1024
"""Ceiling handed to ``hashlib.scrypt``. Four times what the defaults need, so
raising :data:`DEFAULT_N` twice does not also require editing this line — a
parameter bump that fails with ``memory limit exceeded`` at sign-in time would
lock every account out at once."""

MIN_PASSWORD_CHARS = 12
"""Long enough that the scrypt cost above is the attacker's real problem.
Stated to the operator in plain English on the setup page rather than enforced
silently."""

MAX_PASSWORD_CHARS = 1024
"""A ceiling, not a policy: scrypt hashes whatever it is given, and without a
limit a megabyte of "password" is a free way to make the server do work."""


class PasswordRejected(ValueError):
    """The proposed password cannot be used. The message is shown to whoever
    typed it, so it says what to do about it."""


def check_password_quality(password: str) -> None:
    """Raise :class:`PasswordRejected` if this password may not be set.

    Deliberately short: length, and nothing else. Composition rules ("one
    capital, one symbol") shrink the search space people actually use and are
    the reason passwords get written on the monitor.
    """
    if len(password) < MIN_PASSWORD_CHARS:
        raise PasswordRejected(
            f"That password is too short. Use at least {MIN_PASSWORD_CHARS} characters "
            "— a few unrelated words is easier to remember and harder to guess than "
            "a short one with symbols in it."
        )
    if len(password) > MAX_PASSWORD_CHARS:
        raise PasswordRejected(
            f"That password is longer than {MAX_PASSWORD_CHARS} characters, which is "
            "more than the core will hash. Shorten it."
        )


def hash_password(
    password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> str:
    """Hash a password with a fresh random salt, returning the encoded string."""
    salt = secrets.token_bytes(SALT_BYTES)
    derived = _derive(password, salt, n=n, r=r, p=p)
    return "$".join(
        (
            SCHEME,
            str(n),
            str(r),
            str(p),
            _b64(salt),
            _b64(derived),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Whether ``password`` produces ``encoded``.

    Never raises for a malformed stored value: a corrupted account line is
    "this password does not match", which fails closed, rather than a 500 that
    tells an unauthenticated caller the account exists.
    """
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        derived = _derive(password, salt, n=n, r=r, p=p)
    except (ValueError, OverflowError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


def dummy_verify(password: str) -> bool:
    """Do a verification's worth of work and return ``False``.

    Called when no such account exists, so an unknown name and a wrong password
    take the same time. Always ``False`` — there is nothing here to match.
    """
    verify_password(password, hash_password("", n=DEFAULT_N, r=DEFAULT_R, p=DEFAULT_P))
    return False


def needs_rehash(
    encoded: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> bool:
    """Whether a stored hash was made with weaker parameters than today's.

    The reason the parameters are stored beside the hash. A stored value that
    cannot be parsed at all counts as needing a rehash: it can never match, so
    the account is unusable until its owner sets a password again.
    """
    parsed = _parse(encoded)
    if parsed is None:
        return True
    stored_n, stored_r, stored_p, _, _ = parsed
    return (stored_n, stored_r, stored_p) < (n, r, p)


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DKLEN,
        maxmem=MAXMEM,
    )


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except (ValueError, TypeError):
        return None
    if n < 2 or r < 1 or p < 1 or not salt or not expected:
        return None
    return n, r, p, salt, expected


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


__all__ = [
    "DEFAULT_N",
    "DEFAULT_P",
    "DEFAULT_R",
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_CHARS",
    "SCHEME",
    "PasswordRejected",
    "check_password_quality",
    "dummy_verify",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
