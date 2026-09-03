"""Settings that belong to a person rather than to the core (ADR-0030)."""

from personacore.preferences.store import (
    PREFERENCES_FILENAME,
    STATE_DATABASE_FILENAME,
    Override,
    PreferenceStore,
    SchemaDowngradeError,
    StateDatabase,
    StateMigrationError,
    open_state_database,
)

__all__ = [
    "PREFERENCES_FILENAME",
    "STATE_DATABASE_FILENAME",
    "Override",
    "PreferenceStore",
    "SchemaDowngradeError",
    "StateDatabase",
    "StateMigrationError",
    "open_state_database",
]
