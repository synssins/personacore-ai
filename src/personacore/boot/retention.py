"""``[retention]`` as the audit store wants it — ADR-0004, moved by ADR-0040.

Moved out of ``personacore.server``. One conversion, from the string-keyed
document an operator edits into the ``Surface``-keyed shape the store takes,
and one rule about what to do with a surface name nobody recognises.

Refusing an unknown surface is the policy this module exists to hold: dropping
it would leave an operator believing a retention window is set when it silently
is not. That is a decision about behaviour, so it does not belong in the wiring.
"""

from __future__ import annotations

from personacore.audit.models import RetentionConfig, Surface
from personacore.config import ConfigError
from personacore.config.settings import RetentionSettings


def _resolve_retention(settings: RetentionSettings) -> RetentionConfig:
    """core.toml's ``[retention]`` (str-keyed) into the store's shape
    (``personacore.audit.models.RetentionConfig``, ``Surface``-keyed) -- ADR-0004.

    An unknown surface name is refused, not dropped. Dropping it would leave
    an operator believing that surface's retention window is set when it
    silently is not.

    This is now the SECOND line of that defence, not the first:
    :class:`RetentionSettings` refuses an unknown surface at the boundary the
    admin API writes through, so a bad save is rejected while the operator is
    still looking at it rather than at the next restart. This check stays
    because config also arrives by hand-editing core.toml, and because a
    conversion that assumes its input is clean is one refactor away from being
    wrong. A row that turns up IN THE DATABASE under a surface that is not a
    current ``Surface`` member is a different case, already handled by
    ``AuditStore._purge_older_than``'s own default-window fallback for schema
    drift; that behaviour is untouched here.
    """
    per_surface: dict[Surface, int] = {}
    for key, days in settings.per_surface_days.items():
        try:
            surface = Surface(key)
        except ValueError:
            valid = ", ".join(sorted(item.value for item in Surface))
            raise ConfigError(
                f"core.toml's [retention] section names a surface {key!r} "
                f"that does not exist. Valid surfaces are: {valid}."
            ) from None
        per_surface[surface] = days
    return RetentionConfig(default_days=settings.default_days, per_surface_days=per_surface)
