"""PersonaCore — a small core with stable contracts; everything else is a plugin."""

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    __version__ = _package_version("personacore")
except PackageNotFoundError:  # pragma: no cover - only when running from a tree
    # Not installed (a source checkout with no editable install). Say so rather
    # than inventing a number: a wrong version is worse than an obvious one.
    __version__ = "0.0.0+unknown"

#: The exact build, when the image was built with one. `__version__` alone
#: cannot tell two builds of the same tag apart, and this image is pulled from
#: a floating `latest`, so "which build am I actually running" needs an answer
#: that a rebuild changes. Empty outside a container.
BUILD_COMMIT = os.environ.get("PERSONACORE_BUILD_COMMIT", "")
BUILD_DATE = os.environ.get("PERSONACORE_BUILD_DATE", "")

# The plugin contract version this core implements. Semver, per spec section 4.5:
# minor bumps are additive and never break an existing plugin; a major bump is
# breaking, and this one is.
#
# 2.0 (ADR-0026): `permissions.secrets` became a list of tables — a name, a
# required description, and an optional `required` flag — and the bare-string
# form was removed rather than deprecated. A 1.x manifest is refused, naming the
# field and saying what to write instead
# (`contracts.manifest.CONTRACT_2_0_CHANGE`). Taken deliberately while nothing
# was public and every manifest in existence was in reach: the same change once
# a stranger's plugin exists costs a migration, a deprecation window and a
# compatibility path.
#
# 2.1: `[plugin] provides` — an optional list naming what kind of service the
# plugin *is* ("tts", "stt"). Purely additive, so this is a minor and not a
# major: a manifest written for 2.0 declares nothing there and loads on this
# core untouched, and `_contract_compatible` says the same thing
# (`contract = "2.x"` and `contract = "2.0"` both load here). A manifest that
# pins `contract = "2.1"` is refused by a 2.0 core, naming the version it needs
# — which is the only reason to pin one.
CONTRACT_VERSION = "2.1"
