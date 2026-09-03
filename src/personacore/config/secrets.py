"""Secret store — spec section 7, section 5.1's least-privilege rule, ADR-0025.

Secrets are files on the appdata volume, one secret per file, **inside a
namespace that says who owns it**::

    /appdata/secrets/core/<name>              the core's own
    /appdata/secrets/plugins/<plugin>/<name>  one plugin's own

The namespace is the security control, not a tidiness measure. Until ADR-0025
every secret lived in one flat directory and a plugin was limited to the names
*its own manifest* declared — a list its author wrote. A hostile plugin
declaring ``["openweather_key", "ha_token", "api_key"]`` was handed whatever
existed. Namespacing removes the attack outright: **plugin B cannot name plugin
A's secret, because the name is not global**, and neither can name the core's.
There is nothing to enumerate and nothing to guess. That is what Kubernetes does
with namespaced secrets, what systemd does with ``LoadCredential``, and what
Docker does per service.

**Nothing here reads a value back to an operator.** :class:`SecretStore` is the
management face — list names, create, replace, delete, delete a whole namespace,
migrate — and it has no method that returns a secret value at all. A value
leaves this module through exactly two doors, both of which have to be opened by
name: :meth:`SecretStore.core_secrets` for the core's own credentials and
:meth:`SecretStore.scoped` for one plugin's. Both return ``SecretStr``, so an
accidental log line prints a mask, and both are trivially greppable in review.
The admin surface holds a :class:`SecretStore` and therefore *cannot* read one.

There is no encryption at rest, deliberately (ADR-0025 section 6): in a
container that restarts unattended the key would have to sit beside the
ciphertext, which is a format change dressed as security. These are files on a
disk; protect the disk.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from secrets import token_urlsafe

from pydantic import SecretStr

from personacore.config.appdata import AppdataError, AppdataLayout

CORE_NAMESPACE_DIRNAME = "core"
"""Where the core's own credentials live — the LLM key, the broker password,
anything internal. **No plugin can reach into it under any name.**"""

PLUGINS_NAMESPACE_DIRNAME = "plugins"
"""Parent of one directory per plugin. A plugin's namespace is its own name, so
ownership is a fact about the path rather than a record that could drift."""

MAX_SECRET_BYTES = 8192
"""Ceiling on one stored value.

A credential is a token, a key or a passphrase — a PEM block is the largest
thing anyone legitimately pastes. Anything past this is a file being put in the
wrong place, and refusing it keeps a paste-happy operator from filling the
volume one secret at a time."""

GENERATED_BYTES = 32
"""Entropy for a secret the core mints itself (ADR-0025 section 3) — 256 bits,
which ``token_urlsafe`` renders as 43 URL-safe characters."""

# Secret names become filenames, so they are kept boring for the same reason
# plugin names are: no escaping rules in three different places.
#
# **Matched with `fullmatch`, never `match`** — see the note below, which is the
# whole reason these two lines have a comment about regex flavours on them.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

# A plugin's namespace is its directory, so the name is held to the manifest's
# own rule (contracts/manifest.py). Restated here rather than imported, because
# `config` sits below `plugins` and must not depend on it; the two patterns are
# asserted equal in the tests so they cannot drift apart in silence.
_OWNER_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# Why `fullmatch` rather than `\Z` (the 2026-08 security review):
#
# Python's `$` also matches immediately *before* a trailing newline, so
# `_NAME_RE.match("CON\n")` succeeded and then `"CON\n".split(".", 1)[0]` is
# `"CON\n"`, which is not in the reserved-device set — a newline walked a name
# straight past the guard below and on into a directory name, an audit field and
# a log line, where a newline is its own problem.
#
# The obvious fix is `\Z`, and it is not available here. This exact pattern is
# re-exported as `plugins.packages.PLUGIN_NAME_PATTERN` and handed to pydantic
# as a path-parameter constraint, which compiles it with the Rust `regex` crate:
# that engine has no `\Z` at all and refuses the whole pattern, so changing the
# anchor would stop the admin routes importing. `\A…\z` compiles in Rust but
# `\z` is not valid in Python's `re` before 3.14, and CI runs 3.12 and 3.13.
#
# `fullmatch` needs no anchor either engine has to agree about: it requires the
# pattern to consume the entire string, so the trailing newline has nowhere to
# go. It is used at **every** call site in this module for that reason, and a
# plain `match` on either of these is a defect.

# Windows resolves these names to console/printer/serial devices no matter what
# directory they sit in, so "writing a file" called CON opens a device instead
# — it can hang rather than fail cleanly. The stack deploys to Linux containers,
# but plugin authors develop on workstations, so the allowlist rejects them
# everywhere and the name rules stay the same on every host.
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(1, 10)}
    | {f"LPT{digit}" for digit in range(1, 10)}
)


class SecretError(RuntimeError):
    """A secret could not be read, written or removed.

    Message text is safe to show an operator: it names the secret and its owner,
    and never its value (ADR-0025 section 5)."""


def check_secret_name(name: str) -> str:
    """Validate a secret name before it is ever used as a filename.

    Spec section 9: the caller gets a plain-English, actionable message for
    every rejection — including the wrong *type*, which used to escape as a
    bare ``TypeError`` out of the regex engine.
    """
    if not isinstance(name, str):
        raise SecretError(
            f"Secret name must be a string, not {type(name).__name__}. Pass the "
            "secret's name as written in the manifest, e.g. 'API_KEY', not a "
            "path or any other object."
        )
    if not _NAME_RE.fullmatch(name):
        raise SecretError(
            f"Secret name {name!r} is not usable. Names must start with a letter "
            "and contain only letters, digits, dots, hyphens or underscores — "
            "they become filenames in the secrets directory."
        )
    # Windows applies the device rule to the part before the first dot, so
    # 'CON' and 'CON.txt' are equally dangerous; the comparison is
    # case-insensitive for the same reason.
    stem = name.split(".", 1)[0]
    if stem.upper() in _RESERVED_DEVICE_NAMES:
        raise SecretError(
            f"Secret name {name!r} is not usable: {stem!r} is a reserved device "
            "name on Windows (CON, PRN, AUX, NUL, COM1-COM9, LPT1-LPT9), with "
            "or without an extension. Give the secret a different name, such as "
            f"{'MY_' + name!r}."
        )
    return name


def check_owner(plugin: str) -> str:
    """Validate a plugin name before it is used as a namespace directory.

    Held to the manifest's rule rather than sanitised into something plausible:
    a name that could not have come from a manifest is not a plugin, and a
    namespace built from it would belong to nobody (spec section 7).
    """
    if not isinstance(plugin, str):
        raise SecretError(
            f"A plugin name must be a string, not {type(plugin).__name__}. "
            "Secrets are stored under the plugin's own name."
        )
    if not _OWNER_RE.fullmatch(plugin):
        raise SecretError(
            f"{plugin!r} is not a plugin name, so it cannot own a secret. Plugin "
            "names are 2-64 characters of lowercase letters, digits and hyphens, "
            "starting with a letter."
        )
    return plugin


def _describe(plugin: str | None) -> str:
    """How an error names the owner. ``None`` is the core's own namespace."""
    return "the core" if plugin is None else f"plugin {plugin!r}"


class SecretStore:
    """The management face: names in, values in, nothing out.

    Deliberately has **no method that returns a secret value**. Everything the
    admin surface needs — does this exist, what is missing, store this, delete
    this, delete everything this plugin owned — is here, and none of it can leak
    a credential into a page, a response, a log or an audit record.

    Reading is a separate, differently-named step through :meth:`core_secrets`
    or :meth:`scoped`.
    """

    def __init__(self, layout: AppdataLayout) -> None:
        self._layout = layout

    # -- where things live -------------------------------------------------

    @property
    def directory(self) -> Path:
        """The secrets root. Holds namespaces, not secrets (see :meth:`migrate`)."""
        return self._layout.secrets

    @property
    def core_directory(self) -> Path:
        return self.directory / CORE_NAMESPACE_DIRNAME

    @property
    def plugins_directory(self) -> Path:
        return self.directory / PLUGINS_NAMESPACE_DIRNAME

    def namespace(self, plugin: str | None = None) -> Path:
        """The directory one owner's secrets live in.

        ``None`` is the core's own namespace. A plugin name is checked before it
        is joined onto a path, so nothing a manifest says can walk out of the
        secrets directory.
        """
        if plugin is None:
            return self.core_directory
        return self.plugins_directory / check_owner(plugin)

    # -- enumeration: names only, never values -----------------------------

    def available(self, plugin: str | None = None) -> list[str]:
        """Names present in one namespace. Names only — this never reads a value.

        The entries the kernel returned, which is also what :meth:`_require_entry`
        compares against, so "what exists" has one answer on every filesystem.
        """
        directory = self.namespace(plugin)
        if not directory.is_dir():
            return []
        return sorted(entry.name for entry in directory.iterdir() if entry.is_file())

    def owners(self) -> list[str]:
        """Every plugin that has a namespace on disk, whether or not it is installed.

        Uninstall asks this to answer "is there anything of this plugin's left".
        Nothing shows it to a plugin: a plugin is never told which other
        namespaces exist.
        """
        if not self.plugins_directory.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.plugins_directory.iterdir()
            if entry.is_dir() and not entry.is_symlink() and _OWNER_RE.fullmatch(entry.name)
        )

    def has(self, name: str, plugin: str | None = None) -> bool:
        """Whether one namespace holds that exact name, byte for byte."""
        check_secret_name(name)
        return name in self.available(plugin)

    def missing(self, names: Iterable[str], plugin: str | None = None) -> list[str]:
        """Which of ``names`` have not been supplied yet, in the order asked.

        ADR-0025 section 4: a manifest naming a secret is a **request**, not an
        entitlement. This is the list a plugin is still waiting for, which is
        what its page shows and what the install screen asks for. An unusable
        name counts as missing rather than raising — the operator cannot supply
        it either, and a settings page must still render.
        """
        present = set(self.available(plugin))
        absent: list[str] = []
        for name in names:
            try:
                check_secret_name(name)
            except SecretError:
                absent.append(name if isinstance(name, str) else str(name))
                continue
            if name not in present:
                absent.append(name)
        return absent

    # -- the write path ----------------------------------------------------

    def set(self, name: str, value: str | SecretStr, plugin: str | None = None) -> None:
        """Create or replace one secret. Atomic — a crash leaves the old value.

        Written to a fresh file in the same directory and then renamed over the
        target, so there is no window in which the file exists holding half a
        credential. A half-written key authenticates against nothing and is
        miserable to diagnose from a 401, which is the whole reason this is not
        a plain ``write_text``.

        Surrounding whitespace goes: a trailing newline is what every editor and
        every paste adds and is almost never part of the value — the same strip
        the read side has always applied, moved to the moment it can still be
        got right.
        """
        check_secret_name(name)
        directory = self._require_namespace(plugin, create=True)
        raw = self._clean(name, plugin, value)
        target = self._entry_path(directory, name, plugin)
        temporary = directory / f".{name}.{token_urlsafe(8)}.tmp"
        try:
            # O_EXCL: never write through something already there, and never
            # follow a link somebody else put in the way.
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            os.replace(temporary, target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise SecretError(
                f"Secret {name!r} for {_describe(plugin)} could not be stored: "
                f"{exc.strerror or exc}. Check the appdata volume is mounted and "
                "writable by the user the core runs as."
            ) from exc
        # Best effort, and only where it means anything: a container runs as one
        # user, and a chmod that fails must not lose a credential that is
        # already safely on disk.
        try:
            os.chmod(target, 0o600)
        except OSError:  # pragma: no cover - Windows and odd filesystems
            pass

    def generate(self, name: str, plugin: str | None = None, *, replace: bool = False) -> bool:
        """Mint a secret the core can produce itself (ADR-0025 section 3).

        No field is shown and no operator is involved: anything whose value the
        core can invent — an HTTP plugin's identity credential, a password for a
        service the core manages, any internal token — is created at install and
        delivered from the namespace like any other.

        Idempotent by default, so it is safe on every start: returns ``False``
        and touches nothing when the secret already exists. ``replace=True``
        rotates it. **The value is not returned**, here or anywhere.
        """
        check_secret_name(name)
        if not replace and self.has(name, plugin):
            return False
        self.set(name, token_urlsafe(GENERATED_BYTES), plugin)
        return True

    def delete(self, name: str, plugin: str | None = None) -> bool:
        """Remove one secret. ``False`` if it was not there.

        Not an error: the caller that matters is uninstall, which is removing
        whatever happens to exist rather than a list it was promised.
        """
        check_secret_name(name)
        directory = self.namespace(plugin)
        if not directory.is_dir() or name not in self.available(plugin):
            return False
        path = self._entry_path(directory, name, plugin)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SecretError(
                f"Secret {name!r} for {_describe(plugin)} could not be removed: "
                f"{exc.strerror or exc}."
            ) from exc
        return True

    def delete_namespace(self, plugin: str) -> int:
        """Remove everything one plugin owns, and the namespace itself.

        This is the half of uninstall that could not be expressed before
        ADR-0025: with secrets in one flat pool and no owner recorded, "this
        plugin's secrets" was not a question the store could answer, and
        deleting by name would have deleted somebody else's credential. It is
        owned by the path now, so it is one call.

        Returns how many secrets went. Refuses to remove anything that is not a
        real directory directly inside the plugins namespace — the containment
        check is :meth:`AppdataLayout.require_inside`, the same one the plugin
        installer uses, and a namespace that is a symlink is refused outright
        rather than followed.
        """
        check_owner(plugin)
        directory = self.namespace(plugin)
        if not directory.exists():
            return 0
        resolved = self._require_removable(directory, plugin)
        count = sum(1 for entry in resolved.rglob("*") if entry.is_file())
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise SecretError(
                f"The secrets belonging to plugin {plugin!r} could not be removed "
                f"from {resolved.as_posix()}: {exc.strerror or exc}. Check the "
                "appdata volume is writable and try again."
            ) from exc
        return count

    # -- delivery: the only two doors a value leaves by ---------------------

    def core_secrets(self) -> CoreSecrets:
        """The core's own credentials, readable by the core and by nothing else.

        Named rather than implicit so that every place a core credential is read
        is one grep away, and so that holding a :class:`SecretStore` — which the
        admin surface does — is not itself the ability to read one.
        """
        return CoreSecrets(self)

    def scoped(self, plugin: str, declared: Iterable[str]) -> ScopedSecrets:
        """One plugin's namespace, limited further to what its manifest asked for.

        A **lookup inside a namespace**, not a filter over a shared pool. The old
        shape was the vulnerability (ADR-0025) and is not kept as a fallback:
        there is no argument to this method that reaches another owner's
        secrets, because the owner is part of the path.

        ``declared`` is still applied on top, so a plugin later edited to read a
        name its manifest never mentioned is refused — but the outer boundary no
        longer depends on that list being honest.
        """
        check_owner(plugin)
        names = list(declared)
        for name in names:
            check_secret_name(name)
        return ScopedSecrets(self, plugin, frozenset(names))

    # -- migration (ADR-0025 consequences) ---------------------------------

    def migrate(self) -> list[str]:
        """Move pre-ADR-0025 flat secrets into the core namespace.

        Every secret that existed before namespacing was put there by hand by
        somebody with shell access to the container, for the core's own use — so
        the core namespace is where they belong. Returns the names moved,
        sorted.

        Deterministic and idempotent, so it is safe on every start: it moves
        only *files* sitting directly in the secrets root, never a namespace
        directory, and it never writes over a secret that already exists in the
        core namespace — a collision leaves the flat file exactly where it is and
        reports it through :meth:`unmigrated`, because silently replacing a
        working credential is worse than leaving one for a human to look at.

        **A plugin that named one of these before this stops receiving it, and
        that is the point** (ADR-0025). The plugin's page has to say so, naming
        the secret, so the operator can supply it again in the plugin's own
        namespace rather than facing something that mysteriously stopped
        working.
        """
        flat = self._flat_entries()
        if not flat:
            return []
        self._require_namespace(None, create=True)
        moved: list[str] = []
        for name in flat:
            target = self.core_directory / name
            if target.exists():
                continue
            try:
                os.replace(self.directory / name, target)
            except OSError:
                # One unmovable file must not strand the rest; `unmigrated`
                # reports whatever is still lying in the root afterwards.
                continue
            moved.append(name)
        return sorted(moved)

    def unmigrated(self) -> list[str]:
        """Flat secrets :meth:`migrate` could not move, sorted.

        Non-empty means a name exists both flat and in the core namespace. The
        operator is the only one who can say which is current, so the store
        reports rather than guesses.
        """
        return sorted(self._flat_entries())

    # -- internals ---------------------------------------------------------

    def _flat_entries(self) -> list[str]:
        """Files sitting directly in the secrets root — the pre-ADR-0025 shape.

        Namespace directories are skipped by the ``is_file`` test, and a name
        that could never have been a secret is skipped too rather than being
        dragged into the core namespace.
        """
        if not self.directory.is_dir():
            return []
        return [
            entry.name
            for entry in self.directory.iterdir()
            if entry.is_file() and not entry.is_symlink() and _NAME_RE.fullmatch(entry.name)
        ]

    def _clean(self, name: str, plugin: str | None, value: str | SecretStr) -> str:
        """The value as it will be stored, or why it cannot be.

        Every refusal names the secret and never quotes the value, including the
        one about length — "that is 40000 characters" says enough.
        """
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not isinstance(value, str):
            raise SecretError(
                f"Secret {name!r} for {_describe(plugin)} was not given a value: "
                f"a secret is text, not {type(value).__name__}."
            )
        cleaned = value.strip()
        if not cleaned:
            raise SecretError(
                f"Secret {name!r} for {_describe(plugin)} was left empty. Paste "
                "the value, or remove the secret instead of storing a blank one."
            )
        if "\x00" in cleaned:
            raise SecretError(
                f"Secret {name!r} for {_describe(plugin)} contains a NUL byte, so "
                "it is not text that could have been copied from anywhere. Paste "
                "the value again."
            )
        size = len(cleaned.encode("utf-8"))
        if size > MAX_SECRET_BYTES:
            raise SecretError(
                f"Secret {name!r} for {_describe(plugin)} is {size} bytes, past "
                f"the {MAX_SECRET_BYTES}-byte ceiling on one credential. That is a "
                "file rather than a key; store the file where the plugin can read "
                "it and give it the path in the plugin's settings."
            )
        return cleaned

    def _require_namespace(self, plugin: str | None, *, create: bool = False) -> Path:
        """The namespace directory, created on demand, checked to be inside appdata."""
        directory = self.namespace(plugin)
        try:
            self._layout.require_inside(
                directory, what=f"The secrets namespace for {_describe(plugin)}"
            )
        except AppdataError as exc:
            raise SecretError(
                f"The secrets namespace for {_describe(plugin)} is not inside the "
                f"appdata volume: {exc}"
            ) from exc
        if create:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SecretError(
                    f"The secrets namespace for {_describe(plugin)} could not be "
                    f"created at {directory}: {exc.strerror or exc}. Check the "
                    "appdata volume is mounted and writable."
                ) from exc
        return directory

    def _require_removable(self, directory: Path, plugin: str) -> Path:
        """Refuse to delete anything that is not this plugin's own namespace.

        The same three checks the plugin installer makes before removing a
        plugin folder, in the same order: not a symlink, resolves inside
        appdata, and its resolved parent is the directory the core put it in.
        Deletion can only escape if all three are wrong at once.
        """
        if directory.is_symlink():
            raise SecretError(
                f"The secrets namespace for plugin {plugin!r} is a symbolic link "
                "rather than a real folder. Removing it would delete whatever it "
                "points at, so the core refuses. Remove the link by hand if that "
                "is really what you want."
            )
        try:
            resolved = self._layout.require_inside(
                directory, what=f"The secrets namespace for plugin {plugin!r}"
            )
        except AppdataError as exc:
            raise SecretError(str(exc)) from exc
        if not resolved.is_dir():
            raise SecretError(
                f"The secrets namespace for plugin {plugin!r} at "
                f"{resolved.as_posix()!r} is not a folder, so the core will not "
                "remove it."
            )
        if resolved.parent != self.plugins_directory.resolve():
            raise SecretError(
                f"The secrets namespace for plugin {plugin!r} resolves to "
                f"{resolved.as_posix()!r}, which is not directly inside "
                f"{self.plugins_directory.as_posix()!r}. The core only removes "
                "namespaces it created."
            )
        return resolved

    def _entry_path(self, directory: Path, name: str, plugin: str | None) -> Path:
        """``<namespace>/<name>``, checked to still be inside appdata.

        Belt and braces: the name regex already forbids separators, but the
        store is security-critical enough to check the resolved location too.
        """
        path = directory / name
        if not self._layout.contains(path):
            raise SecretError(
                f"Secret {name!r} for {_describe(plugin)} resolves outside the "
                "secrets directory."
            )
        return path

    def _require_entry(self, name: str, plugin: str | None) -> Path:
        """The path of a secret that really is there, byte for byte.

        Least privilege is enforced by comparing names as *strings*, while
        reading a secret opens ``<namespace>/<name>`` as a *path*. On a
        case-insensitive filesystem (NTFS, default APFS) those two steps
        disagree: a manifest declaring ``LLM_KEY`` passes the string check and
        the filesystem then hands it the file ``llm_key``, a secret the plugin
        never declared.

        Closing that means never trusting the path we constructed. The real
        directory entries are the only authority on what a name refers to, so
        the requested name must appear among them exactly — same bytes, same
        case. That comparison is a string comparison against data the kernel
        returned, so it holds identically on a case-sensitive filesystem, where
        it simply never rejects anything a plain open would have accepted. It is
        deliberately unconditional: a least-privilege boundary that is only
        present on some hosts is not a boundary.
        """
        check_secret_name(name)
        directory = self.namespace(plugin)
        path = self._entry_path(directory, name, plugin)
        try:
            entries = os.listdir(directory)
        except FileNotFoundError:
            raise SecretError(self._not_supplied(name, plugin)) from None
        except OSError as exc:
            raise SecretError(
                f"Secret {name!r} could not be read: the secrets namespace for "
                f"{_describe(plugin)} could not be listed ({exc.strerror or exc})."
            ) from exc
        if name in entries:
            return path
        variants = sorted(entry for entry in entries if entry.lower() == name.lower())
        if variants:
            raise SecretError(
                f"Secret {name!r} is not present under that exact name for "
                f"{_describe(plugin)}. The namespace holds {variants[0]!r}, which "
                "differs only in case. Secret names are matched exactly, so store "
                "it under the name that was asked for — nothing may be read under "
                "a name that was not declared."
            )
        raise SecretError(self._not_supplied(name, plugin))

    @staticmethod
    def _not_supplied(name: str, plugin: str | None) -> str:
        """The one sentence for "it was asked for and nobody has given it yet".

        Spec section 9: it says what to do next, and where, rather than naming a
        directory nobody can reach without a shell (ADR-0025 section 4).
        """
        if plugin is None:
            return (
                f"Secret {name!r} has not been supplied for the core. Store it "
                "from the settings screen that asks for it."
            )
        return (
            f"Secret {name!r} has not been supplied for plugin {plugin!r}. Open "
            "the plugin's settings and paste the value into the field asking for "
            "it; the plugin is waiting for it."
        )

    def _read(self, name: str, plugin: str | None) -> SecretStr:
        """The one place a value is read off the disk.

        Private, and reached only through :class:`CoreSecrets` or
        :class:`ScopedSecrets`, so that "who is allowed to read this" is decided
        by which view the caller was handed rather than by an argument it
        passes.
        """
        path = self._require_entry(name, plugin)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SecretError(self._not_supplied(name, plugin)) from None
        except OSError as exc:
            raise SecretError(
                f"Secret {name!r} could not be read: {exc.strerror or exc}."
            ) from exc
        # A trailing newline is what every editor and `echo` adds, and is almost
        # never part of the secret. Stripped here as well as on the way in, so
        # that a value placed on the volume by hand behaves like a stored one.
        return SecretStr(raw.strip("\r\n"))

    def __repr__(self) -> str:
        return f"SecretStore(root={str(self.directory)!r})"


class CoreSecrets:
    """The core's own credentials. One method, and it is the reading one.

    Handed out by :meth:`SecretStore.core_secrets` and never constructed by the
    admin surface, so "the core can read its LLM key" and "a page can read a
    secret" stay different sentences. A plugin never receives one of these under
    any circumstances.
    """

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    def get(self, name: str) -> SecretStr:
        """One core-owned secret.

        Returns ``SecretStr`` rather than ``str`` so that logging the value by
        accident prints a mask. Callers must go through ``.get_secret_value()``
        deliberately, which makes the leak-prone moment visible in review.
        """
        return self._store._read(name, None)

    def available(self) -> list[str]:
        """Core-owned names. Names only, and never a plugin's."""
        return self._store.available(None)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._store.available(None)

    def __repr__(self) -> str:
        return "CoreSecrets()"


class ScopedSecrets:
    """What one plugin gets: its own namespace, and only what it declared.

    It cannot name its way out of this in either direction — not to another
    plugin or to the core, because the namespace is part of the path and the
    plugin did not choose the path; and not to a name outside its manifest,
    because the declared set is applied on top. It offers no way to enumerate
    anything beyond itself.

    Declaring a secret is a **request**. :meth:`missing` is the half of that the
    product has to show: a plugin may ask for a hundred and receives what was
    actually supplied.
    """

    def __init__(self, store: SecretStore, plugin: str, allowed: frozenset[str]) -> None:
        self._store = store
        self._plugin = plugin
        self._allowed = allowed

    @property
    def plugin(self) -> str:
        return self._plugin

    @property
    def allowed(self) -> frozenset[str]:
        """What the manifest asked for. Not what it was given — see :meth:`missing`."""
        return self._allowed

    def get(self, name: str) -> SecretStr:
        """One of this plugin's secrets, if it declared it and it was supplied."""
        if not isinstance(name, str) or name not in self._allowed:
            raise SecretError(
                f"Plugin {self._plugin!r} did not declare secret {name!r} in its "
                "manifest, so it cannot read it. Add it to the manifest's "
                "permissions.secrets list if the plugin genuinely needs it."
            )
        return self._store._read(name, self._plugin)

    def available(self) -> list[str]:
        """Declared *and* supplied, sorted — exactly what :meth:`get` will answer."""
        return sorted(self._allowed.intersection(self._store.available(self._plugin)))

    def missing(self) -> list[str]:
        """Declared and not supplied, sorted.

        A plugin waiting on one of these is waiting for a credential, which is a
        state its page states plainly (ADR-0025 section 4) rather than a crash
        loop.
        """
        return sorted(self._allowed.difference(self._store.available(self._plugin)))

    def __contains__(self, name: object) -> bool:
        """Whether the manifest declared it. Presence is :meth:`available`."""
        return name in self._allowed

    def __repr__(self) -> str:
        # Names, never values, and sorted so the repr is stable in a log.
        return f"ScopedSecrets(plugin={self._plugin!r}, allowed={sorted(self._allowed)!r})"


__all__ = [
    "CORE_NAMESPACE_DIRNAME",
    "GENERATED_BYTES",
    "MAX_SECRET_BYTES",
    "PLUGINS_NAMESPACE_DIRNAME",
    "CoreSecrets",
    "ScopedSecrets",
    "SecretError",
    "SecretStore",
    "check_owner",
    "check_secret_name",
]
