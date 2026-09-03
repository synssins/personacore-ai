"""Slowing repeated failed sign-ins.

Two mechanisms, because they cover different attacks:

1. **The hash itself.** Every attempt — including one for an account that does
   not exist, which pays :func:`~personacore.auth.passwords.dummy_verify` —
   runs scrypt at ~50 ms. That alone caps an unthrottled attacker at roughly
   twenty guesses a second per core, and it is the part that cannot be turned
   off or forgotten.
2. **A lockout with exponential backoff**, here. After
   :data:`FAILURE_THRESHOLD` failures the pair is refused outright for a period
   that doubles with each further failure, up to :data:`MAX_LOCKOUT`.

**Counted per (account name, client address), not per account.** Locking an
account by name alone lets anybody on the LAN lock the household out of its own
assistant by typing the wrong password five times — a denial of service handed
out for free. Pairing it with the address means the attacker locks only
themselves out, and somebody guessing many names from one address trips the
lockout on each of them independently while still paying the scrypt cost every
time.

**In memory, not in appdata.** A restart clears it, which is a real weakness
and a deliberate one: persisting it would mean an unauthenticated caller can
make the core write to disk, and would carry a lockout across the restart an
operator performs precisely because they are locked out.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

FAILURE_THRESHOLD = 5
"""Failures before a lockout starts. Generous enough that somebody mistyping a
long password is not punished for it."""

FAILURE_WINDOW = timedelta(minutes=15)
"""How long a failure counts for. A run of failures older than this is somebody
who forgot their password last week, not an attack in progress."""

BASE_LOCKOUT = timedelta(seconds=30)
MAX_LOCKOUT = timedelta(minutes=15)
"""Backoff floor and ceiling. Capped rather than unbounded so a household is
never permanently locked out of its own house by an attacker who keeps
knocking."""

LOCKED_OUT = (
    "Too many sign-in attempts. Wait {seconds} seconds and try again. "
    "If this was not you, somebody is guessing at this account."
)
"""What the person on the other end is told. It says how long, because a
refusal with no end date is indistinguishable from a broken sign-in page."""


@dataclass
class _Attempts:
    failures: int = 0
    first_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    locked_until: datetime | None = None


class SignInThrottle:
    """Failed-sign-in counters, keyed by ``(name, address)``.

    Thread-safe because the sign-in handlers run in FastAPI's threadpool as
    well as on the event loop, and a counter that undercounts under concurrency
    is a counter an attacker can outrun by opening more connections.
    """

    def __init__(
        self,
        *,
        threshold: int = FAILURE_THRESHOLD,
        window: timedelta = FAILURE_WINDOW,
        base_lockout: timedelta = BASE_LOCKOUT,
        max_lockout: timedelta = MAX_LOCKOUT,
    ) -> None:
        self._threshold = threshold
        self._window = window
        self._base = base_lockout
        self._max = max_lockout
        self._attempts: dict[tuple[str, str], _Attempts] = {}
        self._lock = threading.Lock()

    def retry_after(self, name: str, address: str, *, now: datetime | None = None) -> int:
        """Seconds this pair must wait, or ``0`` if it may try now.

        Checked *before* the password is looked at, so a locked-out caller does
        not even get the scrypt run — the lockout has to be cheaper than the
        thing it is protecting or it is a way to burn the CPU rather than save
        it.
        """
        moment = now or datetime.now(UTC)
        key = (name, address)
        with self._lock:
            record = self._attempts.get(key)
            if record is None or record.locked_until is None:
                return 0
            if moment >= record.locked_until:
                return 0
            return max(1, int((record.locked_until - moment).total_seconds() + 0.5))

    def record_failure(self, name: str, address: str, *, now: datetime | None = None) -> int:
        """Count one failure. Returns the seconds locked out, ``0`` if not yet."""
        moment = now or datetime.now(UTC)
        key = (name, address)
        with self._lock:
            record = self._attempts.get(key)
            if record is None or moment - record.first_at > self._window:
                record = _Attempts(first_at=moment)
            record.failures += 1
            if record.failures >= self._threshold:
                over = record.failures - self._threshold
                # Doubling, capped. `min` on the seconds rather than on the
                # timedelta so a large `over` cannot overflow the multiply.
                seconds = min(
                    self._base.total_seconds() * (2 ** min(over, 16)),
                    self._max.total_seconds(),
                )
                record.locked_until = moment + timedelta(seconds=seconds)
            self._attempts[key] = record
            self._sweep(moment)
            if record.locked_until is None:
                return 0
            return max(1, int((record.locked_until - moment).total_seconds() + 0.5))

    def clear(self, name: str, address: str) -> None:
        """Forget this pair's failures — called on a successful sign-in, so a
        person who eventually remembers their password is not still locked out
        by the attempts they made getting there."""
        with self._lock:
            self._attempts.pop((name, address), None)

    def _sweep(self, now: datetime) -> None:
        """Drop entries nobody is waiting on, so an attacker cycling names
        cannot grow this dict without bound. Called under the lock."""
        horizon = now - self._window - self._max
        for key, record in list(self._attempts.items()):
            latest = record.locked_until or record.first_at
            if latest < horizon:
                del self._attempts[key]


__all__ = [
    "BASE_LOCKOUT",
    "FAILURE_THRESHOLD",
    "FAILURE_WINDOW",
    "LOCKED_OUT",
    "MAX_LOCKOUT",
    "SignInThrottle",
]
