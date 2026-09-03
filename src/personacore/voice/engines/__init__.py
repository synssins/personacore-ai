"""The speech engines built into the core image.

Each module here is one engine implementing the ``Engine`` protocol of
ADR-0029's interface addendum. An engine is a switch, not a container: the code
sits in the image doing nothing until the operator turns it on, and
:meth:`stop` puts it back to holding nothing.

Nothing in here decides *which* engine is on, which voice a persona uses, or
when to speak. That is the registry's, and it lives one directory up.

:func:`build_engines_and_problems` is what the registry calls to find out what
this image carries, **and what it could not carry**: an engine that will not
construct is absent from every screen, so it is named rather than logged and
forgotten. Constructing an engine must cost nothing — no model, no runtime, no
directory read — which is what lets this list grow without every operator
paying for engines they never enable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..engine import Engine

_log = logging.getLogger(__name__)


def _vits_onnx() -> Any:
    # Imported inside the builder so that a module which cannot import — a
    # missing runtime, a syntax error in somebody's new engine — costs that
    # engine alone and never the package.
    from .vits_onnx import VitsOnnxEngine

    return VitsOnnxEngine()


BUILDERS: tuple[tuple[str, Callable[[], Any]], ...] = (("vits-onnx", _vits_onnx),)
"""Every engine in this image, by id, and how to construct one."""


ENGINE_LOAD_FAILED = (
    "The {engine} speech engine is built into this image but could not be "
    "loaded: {error}. It is not offered until that is fixed; every other "
    "engine is unaffected."
)
"""What an operator is told when an engine vanishes out from under them.

Named rather than logged. An engine that fails to construct is *absent* — no
row, no switch, nothing on the screen — and an absent engine and an engine this
image never carried look identical to the person looking for it. The sentence
names the engine, says what went wrong and says what it costs, and it reaches
:meth:`~personacore.voice.registry.VoiceRegistry.add_problem`, so it lands on
``/health`` and on the engines screen beside every other voice problem.
"""


def build_engines_and_problems() -> tuple[list[Engine], tuple[str, ...]]:
    """Every engine this image carries, and every one it could not build.

    One engine failing to construct costs that engine and nothing else. The
    registry already refuses to let a bad engine stop the core starting; this
    is the same bargain one level down, so a future engine whose optional
    import is missing does not take the working ones with it — and, unlike the
    log line this used to settle for, the operator is told which one went and
    why.
    """
    engines: list[Any] = []
    problems: list[str] = []
    for engine_id, build in BUILDERS:
        try:
            engines.append(build())
        except Exception as exc:  # noqa: BLE001 - no engine may cost another
            _log.warning("speech engine %s could not be constructed: %s", engine_id, exc)
            problems.append(ENGINE_LOAD_FAILED.format(engine=engine_id, error=exc))
    return engines, tuple(problems)


def build_engines() -> list[Engine]:
    """Every engine this image carries, constructed but not started.

    The engines alone, for callers that only want the list.
    :func:`build_engines_and_problems` is what the registry calls, because a
    caller assembling the application also has to be able to say what is
    missing.
    """
    engines, _ = build_engines_and_problems()
    return engines
