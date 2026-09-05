"""Plugins as things you install, switch on and remove — ADR-0013.

Split out of :mod:`personacore.admin.routes` (ADR-0040). What is *listed* and
how each plugin is getting on belongs to
:mod:`personacore.admin.api_plugin_listing`; what is here changes the plugins
on disk and in the running core.

Two rules survive the move unchanged and are worth reading before editing
anything below. An uploaded filename is **never** joined onto a path — the
installed folder's name comes from the validated manifest (spec section 7). And
"uninstalled" means the folder, the config, the secrets namespace and what the
running host remembers, because the name is reusable and anything left behind
is inherited by the next plugin installed under it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi import Path as PathParam

from personacore.admin.api_shared import AdminApiContext, _fail, _record_change
from personacore.admin.models import (
    AdminUser,
    InstallResult,
    PluginInstalled,
    PluginListing,
    PluginToggled,
    PluginUninstalled,
    ReloadResult,
)
from personacore.audit import AuditOutcome, get_logger
from personacore.plugins.packages import (
    PLUGIN_NAME_PATTERN,
    InstalledPackage,
    PackageConflict,
    PackageInstallFailed,
    PackageNotInstalled,
    PackageRejected,
    PackageTooLarge,
    PluginStateError,
    install_package,
    set_plugin_enabled,
    uninstall_package,
)

logger = get_logger(__name__)


UPLOAD_TOO_LARGE = (
    "That file is larger than this core accepts as a plugin package. A plugin is "
    "a folder of code and a manifest; nothing was installed."
)

MULTIPART_NOT_ACCEPTED = (
    "Send the plugin package as the body of the request, not as a form upload. "
    "This core reads the .zip bytes directly (Content-Type: application/zip); it "
    "does not parse multipart form data. Nothing was installed."
)
"""Refused rather than parsed.

``multipart/form-data`` needs ``python-multipart``, which is **not** on
``docs/p0-dependencies.md`` and therefore not in the runtime image (CLAUDE.md:
no new runtime dependency without approval). Hand-rolling a multipart parser for
untrusted input would be the worst of both worlds, so the endpoint takes the raw
bytes and says so in words when a browser form arrives instead.
"""


async def _read_upload(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything over ``limit``.

    Streamed with a running total rather than ``await request.body()``: the
    point of a size limit is to not hold the oversized thing in memory, and a
    limit checked after buffering is a limit that has already been exceeded. The
    declared ``Content-Length`` is checked first as a courtesy — it fails fast
    for an honest client — but it is never trusted as the only check, because it
    is a header from outside (spec section 7).
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _fail(status.HTTP_413_CONTENT_TOO_LARGE, UPLOAD_TOO_LARGE)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _fail(status.HTTP_413_CONTENT_TOO_LARGE, UPLOAD_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def _upload_label(filename: str | None) -> str:
    """The uploaded filename, made safe to record and never used as a path.

    Spec section 7: the filename is attacker-controlled. It is *never* joined
    onto a directory — the installed folder's name comes from the validated
    manifest — and what survives here is only ever written into an audit record
    and rendered through an escaping template. Control characters and anything
    long enough to be a payload rather than a name are dropped.
    """
    if not filename:
        return ""
    cleaned = "".join(character for character in filename if character.isprintable())
    return cleaned[:120]


def _install_failure(exc: PackageRejected) -> HTTPException:
    """One refusal, as the right status code with the reason unchanged.

    The message comes from ``packages.py`` already written for a human (spec
    section 9), so it is passed through rather than reworded here — two places
    describing the same refusal is how one of them ends up wrong.
    """
    if isinstance(exc, PackageConflict):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, PackageTooLarge):
        code = status.HTTP_413_CONTENT_TOO_LARGE
    elif isinstance(exc, PackageNotInstalled):
        code = status.HTTP_404_NOT_FOUND
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return _fail(code, str(exc))


def _installed_view(installed: InstalledPackage) -> PluginInstalled:
    """The installer's result, narrowed to what may leave the core."""
    return PluginInstalled(
        name=installed.name,
        version=installed.version,
        transport=installed.transport,
        directory=installed.directory.as_posix(),
        replaced=installed.replaced,
        config_preserved=installed.config_preserved,
        files=installed.files,
        bytes_written=installed.bytes_written,
    )


def register(router: APIRouter, ctx: AdminApiContext) -> None:
    """Register the plugin listing, reload, install and toggle routes.

    Every one of them lands on the router this is handed, which is the guarded
    one (ADR-0032). Installing a plugin is the single most powerful thing this
    surface can do; it must never be reachable without the door.
    """
    api = router
    layout = ctx.layout
    audit = ctx.audit
    personas = ctx.personas
    scans = ctx.scans
    require_user = ctx.require_user
    package_limits = ctx.package_limits
    plugin_toggle = ctx.plugin_toggle
    plugin_health = ctx.plugin_health
    secrets = ctx.secrets
    live_toggle = ctx.live_toggle
    runbooks = ctx.runbooks

    @api.get("/plugins", response_model=PluginListing, summary="Installed plugins")
    async def list_plugins() -> PluginListing:
        """Spec section 5.1 — every plugin found, loaded or broken, each
        failure with the plain-English reason it failed."""
        return await scans.current()

    @api.post("/plugins/reload", response_model=ReloadResult, summary="Rescan plugins")
    async def reload_plugins(request: Request) -> ReloadResult:
        """Spec section 5.1's reload action: "adding a plugin = copy a folder,
        hit reload. No container rebuilds, no stack restarts."

        Also drops the persona cache, so the same button covers "I edited a
        persona and something is stale" without a second control for it.
        """
        # The router-level dependency has already authenticated this request;
        # calling it again is how the handler gets the identity without putting
        # `Depends(...)` in a default argument or an un-resolvable local alias.
        user = require_user(request)
        listing = await scans.reload()
        personas.invalidate()
        await _record_change(
            audit,
            user,
            action="plugins.reload",
            outcome=AuditOutcome.SUCCESS,
            detail={"loaded": listing.loaded_count, "failed": listing.failed_count},
        )
        if listing.failed_count:
            message = (
                f"Reloaded: {listing.loaded_count} plugin(s) available, "
                f"{listing.failed_count} could not be loaded — see the reasons below."
            )
        else:
            message = f"Reloaded: {listing.loaded_count} plugin(s) available."
        return ReloadResult(reloaded=True, listing=listing, message=message)

    # -- plugin packages (ADR-0013) ----------------------------------------

    async def _install_bundled_runbooks(name: str, directory: Path) -> None:
        """Copy a freshly-installed plugin's ``runbooks/`` in, if it has any.

        ``working/contracts/runbook.md`` §6: "A plugin's bundled runbooks are
        validated on plugin install the same way [as an upload]; a failure is
        a warning, not a refusal." :meth:`RunbookStore.install_bundled`
        already never raises for a bad runbook file; this wrapper only
        guards against there being no store at all (an assembly built with
        ``runbooks=None``) and against a filesystem surprise, neither of
        which may be allowed to turn a plugin install that has already
        succeeded into a failure response.
        """
        if runbooks is None:
            return
        try:
            records = await asyncio.to_thread(runbooks.install_bundled, name, directory)
        except Exception as exc:  # noqa: BLE001 - the plugin is already installed
            logger.warning("runbooks_bundled_install_failed", plugin=name, error=repr(exc))
            return
        if records:
            logger.info(
                "runbooks_bundled_installed",
                plugin=name,
                runbooks=[record.id for record in records],
                invalid=[record.id for record in records if not record.valid],
            )

    async def _install_plugin(
        data: bytes, user: AdminUser, *, replace: bool, label: str
    ) -> InstallResult:
        """Install one uploaded package, reload, audit. Shared with the UI.

        The reload is not decoration: ADR-0013's flow ends "moves the validated
        directory into place, **then reloads**", and an install that leaves the
        plugin listed but not running looks to the operator like a broken
        plugin rather than an incomplete install.
        """
        try:
            installed = await asyncio.to_thread(
                install_package, layout, data, replace=replace, limits=package_limits
            )
        except PackageRejected as exc:
            await _record_change(
                audit,
                user,
                action="plugins.install",
                outcome=AuditOutcome.REFUSED,
                # The filename is recorded (an investigator wants it) and the
                # reason with it. Neither is used to build a path, ever.
                detail={
                    "filename": label,
                    "bytes": len(data),
                    "replace": replace,
                    "reason": str(exc),
                },
            )
            raise _install_failure(exc) from exc
        except PackageInstallFailed as exc:
            await _record_change(
                audit,
                user,
                action="plugins.install",
                outcome=AuditOutcome.FAILURE,
                detail={"filename": label, "bytes": len(data), "replace": replace},
            )
            raise _fail(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

        await _install_bundled_runbooks(installed.name, installed.directory)

        listing = await scans.reload()
        personas.invalidate()
        await _record_change(
            audit,
            user,
            action="plugins.install",
            outcome=AuditOutcome.SUCCESS,
            detail={
                "plugin": installed.name,
                "version": installed.version,
                "transport": installed.transport,
                "replaced": installed.replaced,
                "config_preserved": installed.config_preserved,
                "files": installed.files,
                "filename": label,
                "bytes": len(data),
            },
        )
        if installed.replaced:
            message = (
                f"{installed.name} {installed.version} installed, replacing the "
                "version that was there"
                + (
                    ". Its existing config.toml settings were kept."
                    if installed.config_preserved
                    else "."
                )
            )
        else:
            message = f"{installed.name} {installed.version} installed and started."
        return InstallResult(
            installed=_installed_view(installed), listing=listing, message=message
        )

    async def _toggle_plugin(name: str, enabled: bool, user: AdminUser) -> PluginToggled:
        """Switch one plugin on or off, and make it stick (ADR-0013).

        Order matters and is not arbitrary: the choice is written to appdata
        first, so a crash between the two halves leaves a core that comes back
        in the state the operator asked for rather than the one they replaced.
        The live toggle then stops or starts the process, and the reload brings
        the listing into line with both.
        """
        action = "plugins.enable" if enabled else "plugins.disable"
        installed = {view.name for view in (await scans.current()).plugins}
        if name not in installed and not any(
            (root / name).is_dir() for root in (layout.plugins, layout.plugins_http)
        ):
            await _record_change(
                audit,
                user,
                action=action,
                outcome=AuditOutcome.REFUSED,
                detail={"plugin": name, "reason": "not installed"},
            )
            raise _fail(
                status.HTTP_404_NOT_FOUND,
                f"No plugin named {name!r} is installed, so it cannot be switched "
                f"{'on' if enabled else 'off'}.",
            )

        try:
            changed = await asyncio.to_thread(
                set_plugin_enabled, layout, name, enabled=enabled
            )
        except PackageRejected as exc:
            raise _install_failure(exc) from exc
        except PluginStateError as exc:
            await _record_change(
                audit,
                user,
                action=action,
                outcome=AuditOutcome.FAILURE,
                detail={"plugin": name},
            )
            raise _fail(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

        applied_live = False
        if live_toggle is not None:
            try:
                await live_toggle(name, enabled)
                applied_live = True
            except Exception as exc:  # noqa: BLE001 - one plugin, spec section 5.1
                logger.error(
                    "plugin_toggle_failed", plugin=name, enabled=enabled, error=repr(exc)
                )

        listing = await scans.reload()
        await _record_change(
            audit,
            user,
            action=action,
            outcome=AuditOutcome.SUCCESS,
            detail={"plugin": name, "changed": changed, "applied_live": applied_live},
        )
        if not changed:
            message = f"{name} was already switched {'on' if enabled else 'off'}."
        elif enabled:
            message = f"{name} is switched on and running."
        else:
            message = (
                f"{name} is switched off. Its folder and its config.toml are still "
                "here; it is not running and its tools are not available."
            )
        if not applied_live and live_toggle is None:
            message += " It takes effect when the core next starts."
        return PluginToggled(
            name=name, enabled=enabled, changed=changed, listing=listing, message=message
        )

    async def _forget_plugin_state(name: str) -> bool:
        """Drop what the running host remembers about a plugin that is gone.

        Returns whether anything was asked to forget, which is what decides
        whether the confirmation may promise a clean reinstall. An assembly with
        no host — or a host too old to have ``forget`` — degrades to the pre-fix
        behaviour and simply does not make the promise, rather than making one
        it cannot keep.
        """
        forget = getattr(plugin_toggle, "forget", None) or getattr(
            plugin_health, "forget", None
        )
        if forget is None:
            return False
        try:
            await forget(name)
        except Exception as exc:  # noqa: BLE001 - the folder is already gone
            logger.warning("plugin_forget_failed", plugin=name, error=repr(exc))
            return False
        return True

    async def _forget_plugin_secrets(name: str) -> int | None:
        """Delete the plugin's whole secret namespace (ADR-0025).

        ``None`` means nothing was deleted because nothing could be: no store
        was wired in, or the one that was cannot delete. An integer is how many
        secrets went, and ``0`` is a real answer — a plugin that never held one.

        The plugin's *name* is passed and no path is built here. Containment is
        the store's, checked with the same rule the installer uses, and a second
        opinion about which directory is safe to remove is exactly the kind of
        duplicate path logic spec section 7 wants none of.
        """
        remove = getattr(secrets, "delete_namespace", None)
        if remove is None:
            return None
        try:
            return int(await asyncio.to_thread(remove, name))
        except Exception as exc:  # noqa: BLE001 - the folder is already gone
            # `str(exc)` on purpose and reviewed as such: every refusal this
            # store raises names the secret or the namespace and never quotes a
            # value (ADR-0025 section 5).
            logger.warning("plugin_secrets_remove_failed", plugin=name, error=str(exc))
            return None

    async def _remove_plugin(name: str, user: AdminUser) -> PluginUninstalled:
        """Stop the plugin, delete its folder and everything else it owns, then
        reload (ADR-0013, ADR-0025).

        Stopping first is not tidiness. A running stdio plugin is a live
        subprocess holding its own files open; deleting them underneath it is
        wrong on any platform and Windows simply refuses, which turned uninstall
        into an error message about a folder that "could not be removed".

        A plugin owns more than its folder, and "uninstalled" has to mean all of
        it, because the name is reusable:

        * its **secrets**, in its own namespace (ADR-0025). Left behind, they
          are inherited whole by the next plugin installed under the same name
          — a leak, and a surprise, in exactly the direction namespacing exists
          to close.
        * its **switched-off state and captured log**, which live in the running
          host's memory. Uninstall stops the plugin first, which puts the name
          in that switched-off set, and nothing used to take it out again:
          **install, uninstall, install installed a plugin that never started**,
          with nothing on screen saying why. ``forget`` is the fix.

        Both are optional-structural (see ``protocols.py``) and both are
        best-effort: the folder is already gone by the time they run, so a
        failure in either is reported rather than turned into an uninstall that
        appears to have failed after it succeeded.
        """
        if plugin_toggle is not None:
            try:
                await plugin_toggle.set_enabled(name, False)
            except Exception as exc:  # noqa: BLE001 - a stop that fails must not block removal
                logger.warning("plugin_stop_before_uninstall_failed", plugin=name, error=repr(exc))
        try:
            removed = await asyncio.to_thread(uninstall_package, layout, name)
        except PackageRejected as exc:
            await _record_change(
                audit,
                user,
                action="plugins.uninstall",
                outcome=AuditOutcome.REFUSED,
                detail={"plugin": name, "reason": str(exc)},
            )
            raise _install_failure(exc) from exc
        except PackageInstallFailed as exc:
            await _record_change(
                audit,
                user,
                action="plugins.uninstall",
                outcome=AuditOutcome.FAILURE,
                detail={"plugin": name},
            )
            raise _fail(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

        forgotten = await _forget_plugin_state(removed.name)
        secrets_removed = await _forget_plugin_secrets(removed.name)

        listing = await scans.reload()
        await _record_change(
            audit,
            user,
            action="plugins.uninstall",
            outcome=AuditOutcome.SUCCESS,
            detail={
                "plugin": removed.name,
                "directory": removed.directory.as_posix(),
                "config_removed": removed.config_removed,
                "files_removed": removed.files_removed,
                # How many, never which and never what. A count says the
                # namespace went; a name in an audit record is one more place a
                # credential's existence is written down (ADR-0025 section 5).
                "secrets_removed": secrets_removed,
                "runtime_state_cleared": forgotten,
            },
        )
        message = f"{removed.name} was uninstalled: {removed.directory.as_posix()} is gone"
        message += (
            ", including its config.toml settings."
            if removed.config_removed
            else ". It had no config.toml."
        )
        # Say the rest out loud. An operator who is told only about a folder and
        # a config.toml has been told the truth and not the whole of it, and the
        # parts left out are the ones that decide what a reinstall does.
        if secrets_removed:
            message += (
                f" Any secrets it held went with it ({secrets_removed} removed) "
                "and would have to be supplied again."
            )
        elif secrets_removed == 0:
            message += " It held no secrets."
        if forgotten:
            message += (
                " Its captured log and its switched-off state are cleared, so "
                "installing it again starts it fresh."
            )
        return PluginUninstalled(
            name=removed.name,
            directory=removed.directory.as_posix(),
            config_removed=removed.config_removed,
            files_removed=removed.files_removed,
            listing=listing,
            message=message,
        )

    @api.post(
        "/plugins/install",
        response_model=InstallResult,
        status_code=status.HTTP_201_CREATED,
        summary="Install a plugin from an uploaded zip package",
    )
    async def install_plugin(
        request: Request,
        replace: Annotated[bool, Query()] = False,
        filename: Annotated[str | None, Query(max_length=200)] = None,
    ) -> InstallResult:
        """ADR-0013: "a plugin is a zip file, uploaded through the web UI".

        **The body is the .zip itself**, not a form upload — see
        :data:`MULTIPART_NOT_ACCEPTED` for why. ``filename`` is accepted only so
        the audit record can say which file an operator chose; it is never used
        to build a path and never appears in a response (spec section 7).

        Nothing in the package is executed, imported or installed. Every
        refusal — traversal, symlink, zip bomb, invalid manifest, name
        collision — comes back with the reason in plain English and nothing on
        disk (spec section 9).
        """
        user = require_user(request)
        content_type = request.headers.get("content-type", "")
        if content_type.split(";")[0].strip().lower() == "multipart/form-data":
            raise _fail(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, MULTIPART_NOT_ACCEPTED)
        data = await _read_upload(request, package_limits.max_archive_bytes)
        return await _install_plugin(
            data, user, replace=replace, label=_upload_label(filename)
        )

    @api.post(
        "/plugins/{name}/enable",
        response_model=PluginToggled,
        summary="Switch a plugin on",
    )
    async def enable_plugin(
        name: Annotated[str, PathParam(pattern=PLUGIN_NAME_PATTERN)], request: Request
    ) -> PluginToggled:
        """ADR-0013's toggle, on. Idempotent: switching on something already on
        succeeds and says nothing changed."""
        return await _toggle_plugin(name, True, require_user(request))

    @api.post(
        "/plugins/{name}/disable",
        response_model=PluginToggled,
        summary="Switch a plugin off without uninstalling it",
    )
    async def disable_plugin(
        name: Annotated[str, PathParam(pattern=PLUGIN_NAME_PATTERN)], request: Request
    ) -> PluginToggled:
        """ADR-0013: "a plugin can be disabled without being uninstalled — it
        stays on disk with its config, and the supervisor does not start it".
        Its tools stop being offered to the model and stop being callable."""
        return await _toggle_plugin(name, False, require_user(request))

    @api.delete(
        "/plugins/{name}",
        response_model=PluginUninstalled,
        summary="Uninstall a plugin, deleting its folder",
    )
    async def uninstall_plugin(
        name: Annotated[str, PathParam(pattern=PLUGIN_NAME_PATTERN)], request: Request
    ) -> PluginUninstalled:
        """Delete the plugin's folder and everything in it.

        Unlike ``DELETE /keys/{key_id}``, this **does** answer ``404`` for
        something that is not installed. The reasoning that makes key
        revocation idempotent runs the other way here: this destroys data, and
        an operator who deletes the wrong name has to find out, not be told
        "done" by an interface that quietly did nothing.

        The response says exactly what was removed, including whether the
        plugin's ``config.toml`` went with it (ADR-0013).
        """
        return await _remove_plugin(name, require_user(request))


__all__ = [
    "MULTIPART_NOT_ACCEPTED",
    "UPLOAD_TOO_LARGE",
    "register",
]
