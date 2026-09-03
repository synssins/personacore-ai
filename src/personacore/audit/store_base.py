"""What every group of store operations borrows from the store itself.

The operations are split across modules by concern (ADR-0040) but they are one
class at runtime, sharing one sqlite connection and the one lock that keeps
that connection safe to use from the worker threads ``asyncio.to_thread`` hands
work to. This declares those four attributes so each group can be read on its
own and a type checker can follow it; :class:`personacore.audit.store.AuditStore`
is what actually assigns them, in its ``__init__``, and is the only thing that
should.

Annotations only, deliberately. A default here would be a second, wrong answer
to "is this store open yet" living in a class nobody constructs.
"""

from __future__ import annotations

import sqlite3
import threading

from personacore.audit.models import AuditStoreConfig


class StoreBase:
    """The connection, its lock, the config, and whether the store is shut."""

    #: Replaced wholesale by ``set_retention``; read fresh on every purge.
    _config: AuditStoreConfig
    #: One connection, ``check_same_thread=False``, shared by every worker.
    _conn: sqlite3.Connection
    #: The only thing serialising that connection. Held by every method that
    #: touches it, ``close`` included -- see ``AuditStore.close``.
    _lock: threading.Lock
    #: Set by ``close`` under the lock. Only the retention purge asks.
    _closed: bool
