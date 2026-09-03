"""The recognisers built into the core image.

Each module here is one recogniser implementing the ``Recogniser`` protocol of
:mod:`personacore.hearing.engine`. A recogniser is a switch, not a container:
the code sits in the image doing nothing until the operator turns it on, and
:meth:`stop` puts it back to holding nothing.

Nothing in here decides *which* recogniser is on, or what happens to the words
afterwards. That is the registry's, and it lives one directory up.

:func:`build_engines_and_problems` is what the registry calls to find out what
this image carries, **and what it could not carry**: a recogniser that will not
construct is absent from every screen, so it is named rather than logged and
forgotten. Constructing one must cost nothing — no model, no runtime, no
directory read — which is what lets this list grow without every operator
paying for recognisers they never enable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..engine import Recogniser

_log = logging.getLogger(__name__)


def _none() -> Any:
    # Imported inside the builder so that a module which cannot import — a
    # missing runtime, a syntax error in somebody's new recogniser — costs that
    # recogniser alone and never the package.
    from .none import NullRecogniser

    return NullRecogniser()


def _moonshine() -> Any:
    from .moonshine import MoonshineRecogniser

    return MoonshineRecogniser()


BUILDERS: tuple[tuple[str, Callable[[], Any]], ...] = (
    ("none", _none),
    ("moonshine", _moonshine),
)
"""Every recogniser in this image, by id, and how to construct one.

Moonshine joined this tuple and changed nothing else here, which is the whole
reason the indirection exists. Constructing it still costs nothing: it asks
whether three files exist and reports itself unavailable if they do not, so an
image built without the model weights carries the code and offers no switch.
"""


ENGINE_LOAD_FAILED = (
    "The {engine} recogniser is built into this image but could not be "
    "loaded: {error}. It is not offered until that is fixed; every other "
    "recogniser is unaffected."
)
"""What an operator is told when a recogniser vanishes out from under them.

Named rather than logged. One that fails to construct is *absent* — no row, no
switch, nothing on the screen — and an absent recogniser and one this image
never carried look identical to the person looking for it. The sentence names
it, says what went wrong and says what it costs, and it reaches
:meth:`~personacore.hearing.registry.HearingRegistry.add_problem`, so it lands
beside every other hearing problem.
"""


def build_engines_and_problems() -> tuple[list[Recogniser], tuple[str, ...]]:
    """Every recogniser this image carries, and every one it could not build.

    One failing to construct costs that one and nothing else. The registry
    already refuses to let a bad recogniser stop the core starting; this is the
    same bargain one level down, so a future recogniser whose optional import
    is missing does not take the working ones with it — and, unlike a log line,
    the operator is told which one went and why.
    """
    engines: list[Any] = []
    problems: list[str] = []
    for engine_id, build in BUILDERS:
        try:
            engines.append(build())
        except Exception as exc:  # noqa: BLE001 - no recogniser may cost another
            _log.warning("recogniser %s could not be constructed: %s", engine_id, exc)
            problems.append(ENGINE_LOAD_FAILED.format(engine=engine_id, error=exc))
    return engines, tuple(problems)


def build_engines() -> list[Recogniser]:
    """Every recogniser this image carries, constructed but not started.

    The recognisers alone, for callers that only want the list.
    :func:`build_engines_and_problems` is what the registry calls, because a
    caller assembling the application also has to be able to say what is
    missing.
    """
    engines, _ = build_engines_and_problems()
    return engines
