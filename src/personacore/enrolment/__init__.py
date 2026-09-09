"""Enrolment — how a machine joins this core without a credential being carried.

The owner opens the Plugins screen, clicks *Add a workstation*, and is shown a
short pairing code with a live expiry. On the workstation he pastes that code
and presses **Join**. Nothing else is typed, and no token is ever shown to him
or carried between the two machines by hand.

Two halves, deliberately separate:

* :mod:`personacore.enrolment.pairing` — issuing, expiring and burning the
  pairing code. Admin-authenticated, driven from the owner's browser.
* :mod:`personacore.enrolment.workstation` — the one **unauthenticated**,
  code-gated call that turns a redeemed code into a working HTTP plugin.

Neither module builds an HTTP route. The routes live on the admin surface's
routers, which is where this core's one authorisation decision is made.
"""

from __future__ import annotations

__all__: list[str] = []
