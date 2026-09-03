"""What a piece that would not load costs, and who is told — ADR-0040 §3.

Once the pieces are separate, an **optional** one that fails to load can be
logged and skipped rather than taking the container with it. A broken speech
engine costs speech. A broken plugin host costs plugins. Neither costs the
assistant.

What is **not** optional, and is deliberately not wrapped by anything in here:
settings, the audit store, the admin surface, and authentication. A core that
boots without its own front door, or without a record of what it did, is worse
than one that refuses to boot, because it looks fine. The assembly states that
by calling those directly — a piece that is fatal is one nothing here touches.

Every skip is recorded rather than only logged, because *"a degradation nobody
can see is an outage with better manners."* The record is read by ``/health``
and by the Health screen, and it carries the exception that stopped the piece:
"speech is off" without the reason sends somebody to a log they do not have.

This module holds no pieces and builds nothing. It is a list and two guards.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEGRADED_ATTRIBUTE = "degraded_pieces"
"""Where the register lives on the application.

Named rather than spelled out at each use, because ``/health``, the Health
screen and the assembly all read it and a typo in any of them would report a
healthy core."""


@dataclass(frozen=True)
class SkippedPiece:
    """One optional piece that did not load, and what that costs."""

    piece: str
    """The piece, in the words a screen would use for it."""

    costs: str
    """What is missing now, said as a consequence rather than a fault. The
    operator needs to know what stopped working, not which constructor
    raised."""

    error: str
    """The exception that stopped it, as ``repr``. Kept because the Health
    screen is the one place somebody looks before they look at a log, and
    "speech is off" without a reason is not something anybody can act on."""

    def as_dict(self) -> dict[str, str]:
        return {"piece": self.piece, "costs": self.costs, "error": self.error}


class DegradedPieces:
    """Every optional piece this core tried to load and could not.

    Built before the application object is, because the first pieces the
    assembly loads — the engines — are loaded before there is an ``app`` to
    hang anything on. The assembly publishes it on ``app.state`` as soon as
    there is one.

    Append-only for the life of the process. A piece that would not load at
    boot is not retried, so a skip that stopped being true is a lie this class
    cannot tell.
    """

    def __init__(self) -> None:
        self._skipped: list[SkippedPiece] = []

    def skip(self, piece: str, *, costs: str, error: BaseException) -> SkippedPiece:
        """Record one skipped piece and say so in the log."""
        skipped = SkippedPiece(piece=piece, costs=costs, error=repr(error))
        self._skipped.append(skipped)
        log.error(
            "optional_piece_skipped",
            piece=piece,
            costs=costs,
            error=skipped.error,
        )
        return skipped

    def as_health(self) -> list[dict[str, str]]:
        """What ``/health`` reports. Empty on a core with nothing missing."""
        return [item.as_dict() for item in self._skipped]

    def __iter__(self) -> Iterator[SkippedPiece]:
        return iter(tuple(self._skipped))

    def __len__(self) -> int:
        return len(self._skipped)

    def __bool__(self) -> bool:
        return bool(self._skipped)


def load_optional[T](
    pieces: DegradedPieces,
    piece: str,
    build: Callable[[], T],
    *,
    costs: str,
    fallback: T,
) -> T:
    """Build one optional piece, or record why there is none and carry on.

    ``fallback`` is what the rest of the graph holds instead, and it is a value
    rather than a factory on purpose: if a fallback could fail too there would
    be no bottom to this, and the assembly would be back to a boot that can die
    anywhere.
    """
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - the whole point: optional never fatal
        pieces.skip(piece, costs=costs, error=exc)
        return fallback


@contextlib.contextmanager
def optional(pieces: DegradedPieces, piece: str, *, costs: str) -> Iterator[None]:
    """Run a step whose failure means only that the step did not happen.

    For the things that have no value to fall back to — starting the bus,
    starting the plugin host, opening the Wyoming port. They either happened or
    they did not, and the assembly carries on either way.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - the whole point: optional never fatal
        pieces.skip(piece, costs=costs, error=exc)


def degraded(app: Any) -> DegradedPieces:
    """The register on an application, or an empty one.

    A default rather than an attribute error, because ``/health`` and the
    Health screen are served by assemblies in the tests that never set it, and
    a dashboard that raises about the absence of bad news is its own defect.

    ``is None`` and not ``or``: an empty register is falsy, so ``or`` handed
    back a fresh throwaway and every skip recorded through it went nowhere. A
    core whose ``/v1`` surface would not mount logged the skip and then reported
    itself undegraded, which is exactly the invisible outage this module exists
    to prevent.
    """
    register = getattr(app.state, DEGRADED_ATTRIBUTE, None)
    return register if register is not None else DegradedPieces()
