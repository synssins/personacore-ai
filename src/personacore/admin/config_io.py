"""Reading and writing ``core.toml`` for the admin API — spec sections 7 and 9.

Spec section 9 asks for "config editing with validation and plain-English
errors". That sentence is the whole of this module: validation is
:class:`personacore.config.settings.CoreSettings` (which already forbids
unknown keys and bounds every number), and the plain English is the translation
layer below, which turns a pydantic error location into a key an operator
recognises and a sentence they can act on.

Two rules the module enforces on top of validation:

* **A secret value is never accepted and never returned** (spec section 7).
  ``core.toml`` gets read into backups and pasted into support conversations,
  so it holds the *name* of a secret and nothing else. A write carrying
  something that looks like a live credential is refused by name before
  validation even runs, with an explanation of what to do instead.

  One field is exempt and it is the reason :data:`WRITE_ONLY_PATHS` exists:
  ``[bus].password`` holds the broker password itself, because asking an
  operator to hand-create a secret file before they can type an MQTT password
  is exporting an implementation detail into their face. It is exempt from the
  refusal and never from the protection — it is redacted on every read, and a
  document carrying the redaction marker back in leaves the stored value alone.
* **The file is replaced atomically.** A half-written ``core.toml`` is a core
  that will not start, and the admin UI is exactly the wrong place to learn
  that. The new content is written beside the real file and moved over it, so a
  crash mid-write leaves the previous config intact.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from personacore.admin.models import ConfigProblem, ConfigResponse
from personacore.config.appdata import AppdataLayout
from personacore.config.secrets import SecretError, SecretStore
from personacore.config.settings import (
    REDACTED_VALUE,
    REVEAL_SECRETS,
    ConfigError,
    CoreSettings,
    load_core_settings,
)

SECRET_REFERENCE_SUFFIX = "_secret"  # noqa: S105 - a field-name suffix, not a credential
"""Fields naming a secret end in this. ``llm.api_key_secret`` holds the *name*
``llm_key``; the value lives in the secret store (spec section 7)."""

_VALUE_BEARING_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "bearer_token",
        "credential",
        "credentials",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "secret_key",
        "token",
    }
)
"""Key names that would carry a live credential rather than a reference.

``CoreSettings`` forbids extra keys, so a write containing one of these would
be rejected anyway — but with "Extra inputs are not permitted", which tells an
operator nothing about *why* the field they expected is not there. This check
runs first purely so the answer is "secrets are referenced by name, put the
value in the secret store" (spec section 7).
"""

WRITE_ONLY_PATHS: tuple[tuple[str, ...], ...] = (("bus", "password"),)
"""Dotted paths whose value may live in ``core.toml`` but must never leave it.

Exactly one entry, and it is an exception to the rule above rather than a
loosening of it: ``[bus].password`` is a value, so ``_value_bearing_secret_keys``
would otherwise refuse both to return the document and to write it. The
exemption is paid for by two guarantees enforced here — the value is replaced by
:data:`~personacore.config.settings.REDACTED_VALUE` on every read
(:func:`read_config`), and a document arriving with the marker still in it
leaves the stored password untouched (:func:`restore_write_only_values`).

Nothing else is on this list on purpose. The LLM API key and every plugin
secret keep the reference-by-name design; whether they should follow the bus is
a decision for whoever owns spec section 7, not a side effect of this file.
"""

_WRITE_ONLY_KEYS = frozenset(".".join(path) for path in WRITE_ONLY_PATHS)


class ConfigRejected(Exception):
    """A config read or write was refused, with one problem per offending key.

    Carries a list rather than a single message so an operator fixing three
    typos finds out about all three at once.
    """

    def __init__(self, message: str, problems: list[ConfigProblem]) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def secret_references(settings: CoreSettings) -> dict[str, str]:
    """Every ``*_secret`` field that is set, as ``dotted.path -> secret name``.

    Surfaced separately from ``settings`` so the UI can render "API key: comes
    from the secret 'llm_key'" rather than showing an operator a field that
    looks blank and inviting them to paste a credential into it.
    """
    return secret_reference_names(settings.model_dump(mode="json"))


def secret_reference_names(document: dict[str, Any]) -> dict[str, str]:
    """:func:`secret_references` over any parsed document, not just ``CoreSettings``.

    Public because a plugin's own ``config.toml`` gets the same treatment
    (spec section 5.1 puts plugin settings in the plugin's folder, spec section
    7 keeps secret *values* out of every file that is backed up). Two walks
    over two documents is how one of them ends up not knowing about a suffix
    the other one does.
    """
    found: dict[str, str] = {}

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                walk(value, path)
            elif str(key).endswith(SECRET_REFERENCE_SUFFIX) and isinstance(value, str) and value:
                found[path] = value

    walk(document, "")
    return found


def secret_value_keys(document: dict[str, Any]) -> list[str]:
    """Dotted paths of keys whose *name* says they hold a live credential.

    Public for the same reason as :func:`secret_reference_names`: the plugin
    config editor has to make the identical refusal, and a second list of
    credential-shaped key names would drift from this one within a release.
    """
    return sorted(_value_bearing_secret_keys(document))


def read_config(layout: AppdataLayout) -> ConfigResponse:
    """Load ``core.toml`` and render it for the admin API.

    Raises :class:`ConfigRejected` when the file on disk exists but cannot be
    read — the message from ``load_core_settings`` already names the file and
    the offending key, so it is passed through rather than re-worded.
    """
    path = layout.core_config_file
    try:
        settings = load_core_settings(layout)
    except ConfigError as exc:
        raise ConfigRejected(
            str(exc),
            [
                ConfigProblem(
                    key="(file)",
                    problem=str(exc),
                    hint=(
                        f"Fix {path} on disk, or move it aside to start again from "
                        "the built-in defaults."
                    ),
                )
            ],
        ) from exc

    dumped = settings.model_dump(mode="json")
    _assert_no_secret_values(dumped)
    return ConfigResponse(
        settings=dumped,
        secret_references=secret_references(settings),
        source=str(path),
        exists=path.exists(),
    )


def _assert_no_secret_values(dumped: dict[str, Any]) -> None:
    """Belt and braces on spec section 7's "never return a secret value".

    One field can now hold one — ``[bus].password`` — so this no longer only
    guards a hypothetical. It checks the two halves separately: any *other*
    credential-named key is an outright refusal, and a write-only path that is
    anything but the redaction marker means the serialiser did not run and the
    real password is about to be returned. Either way the endpoint fails loudly
    rather than leaking quietly, which is the failure mode nobody notices in
    review.
    """
    leaked = [
        ".".join(path)
        for path in WRITE_ONLY_PATHS
        if _dig(dumped, path) not in (None, REDACTED_VALUE)
    ]
    if leaked:
        raise ConfigRejected(
            "Refusing to return the configuration: a write-only value was not "
            "redacted on the way out.",
            [
                ConfigProblem(
                    key=key,
                    problem="This field would expose a live credential through the admin API.",
                    hint=(
                        "This is a bug, not a configuration problem. The value is "
                        "write-only and must serialise as the redaction marker."
                    ),
                )
                for key in leaked
            ],
        )
    offenders = sorted(_value_bearing_secret_keys(dumped))
    if offenders:
        raise ConfigRejected(
            "Refusing to return the configuration: it contains a field that "
            "looks like a live secret value rather than a secret name.",
            [
                ConfigProblem(
                    key=key,
                    problem="This field would expose a secret value through the admin API.",
                    hint=(
                        "Secrets are stored in the secret store and referenced by name "
                        f"from a '{SECRET_REFERENCE_SUFFIX}' field (spec section 7)."
                    ),
                )
                for key in offenders
            ],
        )


def _value_bearing_secret_keys(node: Any, prefix: str = "") -> list[str]:
    """Dotted paths of keys whose *name* says they hold a credential.

    A path on :data:`WRITE_ONLY_PATHS` is skipped: it is allowed to hold a value
    and is guarded by redaction instead. The exemption is by full dotted path,
    not by key name, so ``bus.password`` is permitted while a ``password``
    anywhere else in the document is still refused.
    """
    found: list[str] = []
    if not isinstance(node, dict):
        return found
    for key, value in node.items():
        name = str(key)
        path = f"{prefix}.{name}" if prefix else name
        if name.lower() in _VALUE_BEARING_SECRET_KEYS and path not in _WRITE_ONLY_KEYS:
            found.append(path)
        if isinstance(value, dict):
            found.extend(_value_bearing_secret_keys(value, path))
    return found


def _dig(document: Any, path: tuple[str, ...]) -> Any:
    """The value at a dotted path, or ``None`` if any step is missing."""
    node = document
    for step in path:
        if not isinstance(node, dict) or step not in node:
            return None
        node = node[step]
    return node


def restore_write_only_values(
    layout: AppdataLayout, payload: dict[str, Any]
) -> dict[str, Any]:
    """Turn a redaction marker back into the password already on disk.

    This is the whole safety of keeping the broker password in ``core.toml``.
    Every read of the config renders it as
    :data:`~personacore.config.settings.REDACTED_VALUE`, so any caller that
    reads the document and saves it back — the raw editor tab, a script doing
    ``GET`` then ``PUT``, the Core form carrying forward a field the operator
    did not touch — posts the marker where the password was. Written through,
    that would replace a working credential with three asterisks and break the
    broker connection on a save about something else entirely.

    So the marker means **leave it as it is**, and it is resolved here, before
    validation, from the file rather than from anything the caller sent. A key
    that is absent still means unset: that is what the explicit "remove the
    password" control produces, and it keeps whole-document writes meaning what
    they say everywhere else in this API.

    The document is copied down each path it touches; the caller's dictionary is
    never mutated.
    """
    try:
        stored = load_core_settings(layout)
    except ConfigError:
        # An unreadable file has no password to preserve. The save is about to
        # replace it wholesale anyway, and raising here would turn "your config
        # is broken" into "your save failed for no stated reason".
        return payload

    document = stored.model_dump(mode="json", context={REVEAL_SECRETS: True})
    result = payload
    for path in WRITE_ONLY_PATHS:
        if _dig(result, path) != REDACTED_VALUE:
            continue
        kept = _dig(document, path)
        result = _replace(result, path, kept)
    return result


def _replace(document: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """``document`` with ``path`` set to ``value``, copying every node on the way.

    Dropped rather than set to ``None``: TOML has no null, and ``write_config``
    excludes ``None`` for exactly that reason — an explicit null here would be a
    second spelling of "absent" for the settings model to disagree about.
    """
    head, rest = path[0], path[1:]
    copied = dict(document)
    if not rest:
        if value is None:
            copied.pop(head, None)
        else:
            copied[head] = value
        return copied
    child = copied.get(head)
    copied[head] = _replace(child if isinstance(child, dict) else {}, rest, value)
    return copied


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def validate_settings(payload: dict[str, Any]) -> CoreSettings:
    """Validate a submitted settings document, or raise :class:`ConfigRejected`.

    The whole document is validated at once (not merged over what is on disk):
    a partial write whose meaning depends on the current file content is
    impossible to review and impossible to roll back by hand.
    """
    offenders = sorted(_value_bearing_secret_keys(payload))
    if offenders:
        raise ConfigRejected(
            "That configuration contains a secret value. Secrets are never stored "
            "in core.toml.",
            [
                ConfigProblem(
                    key=key,
                    problem=(
                        f"'{key}' looks like a live secret. The configuration file is "
                        "included in backups, so it only ever holds the name of a secret."
                    ),
                    hint=(
                        "Put the value in the secret store, then set the matching "
                        f"'{SECRET_REFERENCE_SUFFIX}' field to that secret's name — for "
                        "example llm.api_key_secret = \"llm_key\"."
                    ),
                )
                for key in offenders
            ],
        )

    try:
        return CoreSettings.model_validate(payload)
    except ValidationError as exc:
        problems = [_describe_error(err) for err in exc.errors()]
        raise ConfigRejected(
            "That configuration was not saved because "
            f"{'one setting is' if len(problems) == 1 else f'{len(problems)} settings are'} "
            "not valid.",
            problems,
        ) from exc


def check_secret_references(layout: AppdataLayout, settings: CoreSettings) -> None:
    """Refuse a save that names a core secret this core has not been given.

    Spec section 9 asks for validation with plain-English errors, and this is
    the half that was missing: :func:`validate_settings` checks the *shape* of a
    ``_secret`` field and never asked the store whether anything answers to that
    name. So ``api_key_secret = "no_such_secret"`` saved with a 200, the running
    process carried on unaffected, and the consequence landed on the **next**
    restart — which for a container is unattended.

    It is the same lesson as the retention crashloop earlier in this project:
    **validation belongs where the write happens, not at the next startup.** A
    refusal here costs the operator one corrected field on the screen they are
    already on. The same mistake found at boot costs them a shell, because the
    admin UI is the only place a setting can be corrected and the admin UI is
    what stops coming up.

    Every ``*_secret`` field is checked, not only the LLM key: they all name the
    **core's** namespace (ADR-0025 §1 — a plugin's secrets are named in its own
    manifest and never here), so one walk answers for all of them and a field
    added later is covered without anybody remembering to come back.

    **Only a reference this save introduces or changes is checked**, and that
    limit is not a softening — it is the same rule one level up. A whole-document
    write carries every field forward, including ones the operator never looked
    at, so checking all of them would mean a ``core.toml`` that already names a
    missing secret refuses *every* save until that field is corrected — a second
    way to wedge somebody out of their own settings screen, invented while
    closing the first. This also covers the ordinary case of an operator with a
    leftover ``[bus].password_secret`` pointing at nothing, fixed by typing the
    password into the field beside it. That save must go through.

    A reference already on disk is already the state of the world, and startup
    now degrades on it and says so on the dashboard. What has no excuse is a
    *new* name that nothing answers to, typed a moment ago on the screen that is
    still open.

    Names only, in and out. Nothing in this function can read a value.
    """
    references = secret_references(settings)
    if not references:
        return
    try:
        previous = secret_references(load_core_settings(layout))
    except ConfigError:
        # An unreadable file has no references to compare against, and this save
        # is probably the one fixing it. Refusing here would turn "your config
        # is broken" into "your save failed for no stated reason" — the same
        # reasoning `restore_write_only_values` gives.
        return
    references = {
        key: name for key, name in references.items() if previous.get(key) != name
    }
    if not references:
        return
    store = SecretStore(layout)
    problems: list[ConfigProblem] = []
    for key, name in sorted(references.items()):
        try:
            present = store.has(name)
        except SecretError as exc:
            # An unusable name — too long, a path separator, a reserved
            # filename. The store's own sentence says which, and it names the
            # secret and never a value.
            problems.append(
                ConfigProblem(
                    key=key,
                    problem=f"'{key}' cannot name a secret: {exc}",
                    hint=(
                        "A secret name is a plain file name: letters, digits, "
                        "dashes and underscores."
                    ),
                )
            )
            continue
        if present:
            continue
        problems.append(
            ConfigProblem(
                key=key,
                problem=(
                    f"'{key}' names the secret '{name}', and this core has not "
                    "been given a secret by that name."
                ),
                hint=_supply_core_secret(name),
            )
        )
    if problems:
        raise ConfigRejected(
            "That configuration was not saved because "
            f"{'it names a secret' if len(problems) == 1 else f'it names {len(problems)} secrets'} "
            "this core does not have.",
            problems,
        )


def _supply_core_secret(name: str) -> str:
    """What to do about a core secret nobody has supplied — said honestly.

    ADR-0025 §4 gives an operator a field to paste a credential into on the
    **install and settings screens of a plugin**. It gives the core's own
    secrets no such field, and the store's own message for a missing core secret
    points at "the settings screen that asks for it", which does not exist. That
    sentence is not repeated here: a hint that sends somebody looking for a
    screen that was never built is worse than no hint. This says the two things
    that are actually true and actionable today.
    """
    return (
        f"Nothing was saved. Either clear this field — the setting works without "
        f"a key — or put the value on the appdata volume as the file "
        f"secrets/core/{name} first and save again. There is no screen for "
        f"creating a core secret yet; the fields ADR-0025 added are for a "
        f"plugin's own credentials."
    )


def _describe_error(err: dict[str, Any]) -> ConfigProblem:
    """Turn one pydantic error into a sentence a non-programmer can act on.

    Spec section 9's requirement is that a rejected write says *which key* and
    *why*, in words. Pydantic's own ``msg`` is usually already readable ("Input
    should be greater than 0"); what it never supplies is what to do next, so
    the hints below are keyed off the error type rather than the message text.
    """
    key = ".".join(str(part) for part in err.get("loc", ())) or "(top level)"
    kind = str(err.get("type", ""))
    message = str(err.get("msg", "is not valid"))
    prefix = "Value error, "
    if message.startswith(prefix):
        message = message[len(prefix) :]

    hints = {
        "extra_forbidden": (
            f"PersonaCore has no setting called '{key}'. Check the spelling, or remove "
            "it — a setting that is not recognised is usually a typo for one that is."
        ),
        "missing": f"Add '{key}' to the configuration.",
        "int_parsing": f"'{key}' must be a whole number, written without quotes.",
        "float_parsing": f"'{key}' must be a number, written without quotes.",
        "bool_parsing": f"'{key}' must be true or false, written without quotes.",
        "string_type": f"'{key}' must be text, written in quotes.",
    }
    hint = hints.get(kind)
    if hint is None and kind.startswith(("greater_than", "less_than")):
        hint = f"Choose a value for '{key}' inside the allowed range and save again."

    return ConfigProblem(key=key, problem=f"'{key}': {message}.", hint=hint)


def write_config(layout: AppdataLayout, settings: CoreSettings) -> None:
    """Write validated settings to ``core.toml``, replacing it atomically.

    ``exclude_none`` matters and is not cosmetic: TOML has no null, so an unset
    optional (``bus.username``, ``llm.api_key_secret``) must be *absent* rather
    than empty — and an empty string would validate as a real, wrong value.
    """
    path = layout.core_config_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigRejected(
            f"The configuration directory {path.parent} could not be created: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and writable.",
            [],
        ) from exc

    # The one call in the tree that asks for the real broker password: this is
    # the file it belongs in, and writing the redaction marker into it would be
    # writing a config that authenticates with three asterisks.
    document = settings.model_dump(
        mode="json", exclude_none=True, context={REVEAL_SECRETS: True}
    )
    temporary = path.with_name(path.name + ".new")
    try:
        with temporary.open("wb") as handle:
            tomli_w.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        _discard(temporary)
        raise ConfigRejected(
            f"The configuration could not be saved to {path}: {exc.strerror or exc}. "
            "Check the appdata volume is mounted and writable.",
            [],
        ) from exc


def _discard(path: Path) -> None:
    """Remove a half-written temporary file; never mask the original error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - the real failure is already being raised
        pass


__all__ = [
    "SECRET_REFERENCE_SUFFIX",
    "WRITE_ONLY_PATHS",
    "ConfigRejected",
    "check_secret_references",
    "read_config",
    "restore_write_only_values",
    "secret_reference_names",
    "secret_references",
    "secret_value_keys",
    "validate_settings",
    "write_config",
]
