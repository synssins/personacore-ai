"""Workspace files on the Review screen — workspace contract §7, the
administrator's half.

:mod:`personacore.web.screens.chat_workspace` is the owner's own download
route, scoped to the operator making the request. An administrator reviewing
somebody else's conversation is not that operator, so it needs its own route
— the same reason :mod:`personacore.web.screens.review_delete` is a separate
module from :mod:`personacore.web.screens.review` rather than a branch inside
it, and the same shape: the reviewed account comes from the ``?user=`` query
string the review screen already resolves, never from whoever is signed in
doing the reviewing.

**Admin-only by the router, not by a check written here.** ``/admin/review``
is deliberately absent from ``MEMBER_PATHS``/``MEMBER_PREFIXES``
(``web/routes.py``, ADR-0032) and so is this route — the default-deny in
:func:`personacore.web.routes.create_admin_ui_router` is the whole of this
module's authorisation, the same reason :mod:`personacore.web.screens.review`
gives for having none of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from personacore import workspaces as workspaces_module
from personacore.audit.logging import get_logger
from personacore.audit.models import Owner, Surface
from personacore.auth.accounts import AccountRejected, normalise_name
from personacore.conversations.service import ConversationService
from personacore.web.screens.chat_workspace import (
    NOT_FOUND,
    WorkspaceChip,
    kind_label_for,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from personacore.config.appdata import AppdataLayout
    from personacore.web.shared import UIContext

logger = get_logger(__name__)


def _mime_type(name: str) -> str:
    """Own copy of :func:`personacore.web.screens.chat_workspace._mime_type`
    — see :func:`_workspace_ceilings` beside it for why this module keeps its
    own small helpers rather than reaching into another screen's private
    names."""
    lowered = name.lower()
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith(".json"):
        return "application/json"
    return "text/plain"


_DEFAULT_MAX_FILE_BYTES = 2_000_000
_DEFAULT_MAX_WORKSPACE_BYTES = 50_000_000
"""Used only to list — and, on the download route, read — a workspace's
files. See :func:`personacore.web.screens.review_delete._workspace_ceilings`
for why either value would do when nothing here enforces a ceiling."""


def _workspace_ceilings(request: Request) -> tuple[int, int]:
    """Own copy of :func:`personacore.web.screens.chat_workspace.
    _workspace_ceilings` — four lines, kept local for the same reason that
    one is: a screen module is meant to be read without following an import
    into another one."""
    settings = getattr(request.app.state, "settings", None)
    workspace = getattr(settings, "workspace", None)
    max_file = getattr(workspace, "max_file_bytes", None)
    max_total = getattr(workspace, "max_workspace_bytes", None)
    return (
        max_file if isinstance(max_file, int) and max_file > 0 else _DEFAULT_MAX_FILE_BYTES,
        max_total if isinstance(max_total, int) and max_total > 0 else _DEFAULT_MAX_WORKSPACE_BYTES,
    )


def workspace_files_for(
    request: Request, layout: AppdataLayout, conversation_id: str, user: str
) -> list[WorkspaceChip]:
    """One reviewed conversation's workspace files, as cards for the review
    screen — the same shape :func:`personacore.web.screens.chat_workspace.
    chips_for_names` builds, with a link into *this* module's own route
    rather than the owner's.

    A workspace that cannot be built (a bad conversation id, one outside
    appdata) or one that is simply empty both answer with an empty list —
    nothing here is a page-breaking failure, the same rule
    :func:`personacore.web.screens.review_delete._file_count` follows for
    the same read.
    """
    max_file, max_total = _workspace_ceilings(request)
    try:
        workspace = workspaces_module.Workspace(
            layout,
            conversation_id,
            max_file_bytes=max_file,
            max_workspace_bytes=max_total,
        )
    except workspaces_module.WorkspaceError:
        return []
    query = f"?user={quote(user)}"
    return [
        WorkspaceChip(
            name=entry.name,
            url=f"/admin/review/{conversation_id}/workspace/{entry.name}{query}",
            kind_label=kind_label_for(entry.name),
        )
        for entry in workspace.list()
    ]


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the administrator's own workspace-file download route."""
    require_user = ctx.require_user
    layout = ctx.layout
    store = ctx.audit
    conversations = ConversationService(store, surface=Surface.ADMIN_UI)

    def _owner_or_404(user: str) -> Owner:
        try:
            name = normalise_name(user)
        except AccountRejected as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from exc
        return Owner.profile(name)

    @router.get(
        "/review/{conversation_id}/workspace/{name}",
        summary="One reviewed conversation's workspace file, admin only",
    )
    async def review_workspace_file(
        request: Request, conversation_id: str, name: str, user: str = ""
    ) -> Response:
        """Serve one workspace file to the administrator reviewing it.

        Owner-checked against the *reviewed* account, not the administrator
        making the request — see the module docstring. A conversation that
        is not that account's, one that does not exist, and a file that is
        not in it all answer the same :data:`NOT_FOUND`, for the same reason
        :mod:`chat_workspace`'s own route gives.
        """
        require_user(request)
        owner = _owner_or_404(user)
        conversation = await conversations.resolve(
            owner, conversation_id=conversation_id, include_hidden=True
        )
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        max_file, max_total = _workspace_ceilings(request)
        try:
            workspace = workspaces_module.Workspace(
                layout,
                conversation_id,
                max_file_bytes=max_file,
                max_workspace_bytes=max_total,
            )
            text = workspace.read(name)
        except (workspaces_module.WorkspaceError, OSError, UnicodeDecodeError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from exc
        return Response(
            content=text,
            media_type=_mime_type(name),
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )


__all__ = ["register", "workspace_files_for"]
