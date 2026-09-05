"""The appdata layout — spec Appendix B, and sections 7 and 10.

Appdata is the assistant. The containers are disposable and rebuildable from the
Compose file; everything that matters — plugins, personas, voices, memory,
users, audit, config, secrets — lives here on a mounted volume.

Two rules this module exists to enforce:

1. **Nothing outside appdata.** Every path handed out is resolved and checked to
   be inside the root, so a config value or a plugin name can never walk out of
   the volume. Spec section 7 treats anything from outside as untrusted, and a
   directory name is from outside.
2. **One place that knows the layout.** The moment two modules each build
   ``root / "plugins"`` themselves, the layout is no longer changeable. Every
   consumer takes its directory from here.
"""

from __future__ import annotations

from pathlib import Path

PLUGINS_DIRNAME = "plugins"
PLUGINS_HTTP_DIRNAME = "plugins-http.d"
PERSONAS_DIRNAME = "personas"
VOICES_DIRNAME = "voices"
MEMORY_DIRNAME = "memory"
USERS_DIRNAME = "users"
SECRETS_DIRNAME = "secrets"
AUDIT_DIRNAME = "audit"
STATE_DIRNAME = "state"
CONFIG_DIRNAME = "config"
ATTACHMENTS_DIRNAME = "attachments"
WORKSPACES_DIRNAME = "workspaces"

CORE_CONFIG_FILENAME = "core.toml"


class AppdataError(RuntimeError):
    """Something is wrong with the appdata volume itself.

    Message text reaches an operator during first-run setup, so it says what to
    do, not merely what failed.
    """


class AppdataLayout:
    """Resolved paths for one appdata root."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    # -- the directories, spec Appendix B ---------------------------------

    @property
    def plugins(self) -> Path:
        return self.root / PLUGINS_DIRNAME

    @property
    def plugins_http(self) -> Path:
        return self.root / PLUGINS_HTTP_DIRNAME

    @property
    def personas(self) -> Path:
        return self.root / PERSONAS_DIRNAME

    @property
    def voices(self) -> Path:
        return self.root / VOICES_DIRNAME

    @property
    def memory(self) -> Path:
        return self.root / MEMORY_DIRNAME

    @property
    def users(self) -> Path:
        return self.root / USERS_DIRNAME

    @property
    def secrets(self) -> Path:
        return self.root / SECRETS_DIRNAME

    @property
    def audit(self) -> Path:
        return self.root / AUDIT_DIRNAME

    @property
    def state(self) -> Path:
        """Durable application state that must never be aged out.

        Separate from :attr:`audit` on purpose (ADR-0030): that directory holds
        the record that the retention purge trims on a timer, and "clear the
        audit log" must never mean "lose everyone's settings".
        """
        return self.root / STATE_DIRNAME

    @property
    def config(self) -> Path:
        return self.root / CONFIG_DIRNAME

    @property
    def core_config_file(self) -> Path:
        return self.config / CORE_CONFIG_FILENAME

    @property
    def attachments(self) -> Path:
        """One directory per attachment, holding the file under its own
        stored name plus nothing else (attachments contract, §3).

        The directory name is a random id minted by
        :mod:`personacore.attachments`, never a content hash — see that
        module's docstring for why a hash would let one household member
        learn what another had sent by guessing at it.
        """
        return self.root / ATTACHMENTS_DIRNAME

    @property
    def workspaces(self) -> Path:
        """One directory per conversation, holding whatever files that
        conversation's tools and persona have written (workspace contract,
        §1).

        Same shape as :attr:`attachments`: the folder name is validated
        before use by :mod:`personacore.workspaces`, and a per-conversation
        subdirectory is created lazily, on first write, never here and never
        merely because a conversation started.
        """
        return self.root / WORKSPACES_DIRNAME

    # -- containment ------------------------------------------------------

    def _resolve(self, candidate: Path | str) -> Path:
        """Resolve a candidate path, or raise ``AppdataError``.

        The single place either containment function touches the filesystem, so
        that both fail for the same reasons. ``ValueError`` is in the net
        because it is what ``os.path.realpath`` raises on Linux — the
        deployment target — for a path holding an embedded NUL byte, which is
        an illegal name, not a programming error.
        """
        try:
            return Path(candidate).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise AppdataError(
                f"The path {str(candidate)!r} could not be resolved: {exc}. It is "
                "either an illegal name or a broken link inside the appdata "
                f"volume at {self.root}; correct or remove it and retry."
            ) from exc

    def contains(self, candidate: Path | str) -> bool:
        """Whether a path resolves to somewhere inside the appdata root.

        Resolves symlinks first: a symlink inside appdata pointing at /etc is
        outside appdata, whatever its own location suggests.
        """
        try:
            resolved = self._resolve(candidate)
        except AppdataError:
            # A path that cannot even be resolved (a loop, an illegal name) is
            # not one we are going to hand out.
            return False
        return resolved == self.root or self.root in resolved.parents

    def require_inside(self, candidate: Path | str, *, what: str) -> Path:
        """Resolve a path and refuse it if it escapes appdata.

        Spec section 9 wants plain-English, actionable failures, so a path that
        cannot be resolved at all degrades to ``AppdataError`` here rather than
        letting a raw ``OSError``/``ValueError`` out — ``contains()`` already
        swallows exactly those, and two functions with one job must fail alike.
        """
        resolved = self._resolve(candidate)
        if not self.contains(resolved):
            raise AppdataError(
                f"{what} points outside the appdata volume. Everything the "
                f"assistant stores must live under {self.root}, so that backups "
                "and upgrades cover it."
            )
        return resolved

    # -- first run --------------------------------------------------------

    def ensure(self) -> None:
        """Create the layout if it is missing. Safe to call on every start.

        Deliberately does NOT create or touch any file inside those
        directories — spec section 7 says an upgrade must never touch appdata
        content, and the cheapest way to keep that promise is for this code to
        have no idea how to write one.
        """
        if self.root.exists() and not self.root.is_dir():
            raise AppdataError(
                f"The appdata path {self.root} exists but is not a directory. "
                "Point the volume at a directory, or move that file aside."
            )
        for directory in (
            self.root,
            self.plugins,
            self.plugins_http,
            self.personas,
            self.voices,
            self.memory,
            self.users,
            self.secrets,
            self.audit,
            self.state,
            self.config,
            self.attachments,
            self.workspaces,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AppdataError(
                    f"Could not create {directory}: {exc.strerror or exc}. "
                    "Check the volume is mounted and writable by the user the "
                    "container runs as."
                ) from exc

    def __repr__(self) -> str:
        return f"AppdataLayout(root={str(self.root)!r})"
