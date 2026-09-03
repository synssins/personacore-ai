"""The core's own sign-in — PC-283 to PC-294.

Four pieces, deliberately separate:

* :mod:`~personacore.auth.method` — **which single way in is open**. The
  precedence rule lives there and nowhere else (PC-294).
* :mod:`~personacore.auth.passwords` — ``hashlib.scrypt`` hashing, with the
  parameters stored beside every hash so they can be raised later (PC-285).
* :mod:`~personacore.auth.accounts` — the account file (PC-283, PC-290,
  PC-291).
* :mod:`~personacore.auth.sessions` — server-side sessions, several per user
  at once (PC-286 to PC-289).
* :mod:`~personacore.auth.throttle` — slowing repeated failed sign-ins.

Nothing here knows about HTTP. The seam that turns any of it into an
authenticated request is :mod:`personacore.admin.authn`, and there is exactly
one of those.
"""

from personacore.auth.accounts import AccountRejected, UserRecord, UserStore
from personacore.auth.method import AuthDecision, AuthMethod, resolve_auth
from personacore.auth.sessions import SessionRecord, SessionStore
from personacore.auth.throttle import SignInThrottle

__all__ = [
    "AccountRejected",
    "AuthDecision",
    "AuthMethod",
    "SessionRecord",
    "SessionStore",
    "SignInThrottle",
    "UserRecord",
    "UserStore",
    "resolve_auth",
]
