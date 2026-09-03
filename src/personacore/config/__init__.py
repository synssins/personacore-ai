"""Configuration and appdata.

One module owns the appdata layout, one owns secrets, one owns core settings.
Component modules take what they need as constructor arguments rather than
reaching for a global, so each stays testable on its own and nothing acquires a
hidden dependency on process-wide state.
"""

from personacore.config.appdata import AppdataError, AppdataLayout
from personacore.config.secrets import (
    CoreSecrets,
    ScopedSecrets,
    SecretError,
    SecretStore,
    check_owner,
    check_secret_name,
)
from personacore.config.settings import (
    AuthSettings,
    BusSettings,
    ConfigError,
    CoreSettings,
    LLMRole,
    LLMRoles,
    LLMSettings,
    RetentionSettings,
    ServerSettings,
    load_core_settings,
)
from personacore.config.voice import VoiceEngineSettings, VoiceSettings

__all__ = [
    "AppdataError",
    "AppdataLayout",
    "AuthSettings",
    "BusSettings",
    "ConfigError",
    "CoreSecrets",
    "CoreSettings",
    "LLMRole",
    "LLMRoles",
    "LLMSettings",
    "RetentionSettings",
    "ScopedSecrets",
    "SecretError",
    "SecretStore",
    "ServerSettings",
    "VoiceEngineSettings",
    "VoiceSettings",
    "check_owner",
    "check_secret_name",
    "load_core_settings",
]
