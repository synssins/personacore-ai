"""Deleting a conversation from the review screen — the workspace contract,
§2. Read-only, everywhere else: the owner's own delete on the chat screen is
a hide (:mod:`personacore.web.screens.chat_room`'s ``chat_hide``); this is
the administrator's, and it destroys.

Three routes, copied from key revoke's own shape
(:mod:`personacore.web.screens.keys`, ``key_revoke_confirm_page`` /
``key_revoke_confirm`` / ``key_revoke``): a confirmation page, the same
confirmation as a dialog fragment, and the post that does it. One helper
computes the two facts both confirmations say, so the page and the dialog
cannot drift apart — the same discipline
:mod:`personacore.web.screens.persona_delete` keeps for the same reason.

**Who owns what is being deleted.** The reviewed account, taken from the
``?user=`` query string the review screen already resolves — never the
administrator doing the deleting. ``ConversationService.delete`` and every
attachment/workspace lookup below is scoped to that owner, so a malformed or
absent ``user`` 404s before anything is touched.

**Admin-only by the router, not by a check written here.** ``/admin/review``
is deliberately absent from ``MEMBER_PATHS``/``MEMBER_PREFIXES``
(``web/routes.py``, ADR-0032) and so is every route this module adds under
it — the default-deny in :func:`personacore.web.routes.create_admin_ui_router`
is the whole of this screen's authorisation, the same reason
:mod:`personacore.web.screens.review` gives for having none of its own.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from personacore import attachments as attachments_module
from personacore import workspaces as workspaces_module
from personacore.audit.logging import get_logger
from personacore.audit.models import AuditOutcome, Owner, Surface
from personacore.auth.accounts import AccountRejected, normalise_name
from personacore.conversations.service import ConversationService
from personacore.web.screens.review import (
    CONVERSATIONS_SHOWN,
    NOBODY_PICKED,
    NOTHING_TO_SHOW,
    conversation_rows,
    store_reports_hidden,
)
from personacore.web.shared import UIContext

logger = get_logger(__name__)

DELETE_TITLE = "Delete this conversation?"
"""The confirmation's title, same on the page and in the dialog."""

DELETE_LABEL = "Delete"

NOT_FOUND = "There is no conversation to delete."
"""One sentence for a bad ``user``, an unknown conversation, and one that is
not this owner's — the same non-distinction
:data:`personacore.attachments.NOT_FOUND` makes, and for the same reason:
telling them apart from outside is a way to go looking for somebody else's
conversations by id."""

_DEFAULT_MAX_FILE_BYTES = 2_000_000
_DEFAULT_MAX_WORKSPACE_BYTES = 50_000_000
"""Used only to count a workspace's files before removing it — see
:func:`personacore.conversations.service` for why either value would do."""


def delete_body(*, title: str, messages: int, attachments: int, files: int) -> str:
    """The sentence both the dialog and the page say — the design canvas's
    wording (approved 2026-09-04), naming exactly what a delete destroys."""
    return (
        f"“{title}” — {messages} messages, {attachments} attachments, {files} "
        "workspace files. Everything is removed. This cannot be undone."
    )


def _workspace_ceilings(request: Request) -> tuple[int, int]:
    """The configured workspace ceilings, or the defaults nobody who never
    touched the setting would differ from.

    Read off ``app.state.settings`` per request, the same reasoning
    :func:`personacore.web.screens.review.retention_days` gives: a captured
    copy would keep naming whatever the container booted with. Used only to
    build a :class:`~personacore.workspaces.Workspace` for counting files —
    neither ceiling is enforced by that read.
    """
    settings = getattr(request.app.state, "settings", None)
    workspace = getattr(settings, "workspace", None)
    max_file = getattr(workspace, "max_file_bytes", None)
    max_total = getattr(workspace, "max_workspace_bytes", None)
    return (
        max_file if isinstance(max_file, int) and max_file > 0 else _DEFAULT_MAX_FILE_BYTES,
        max_total if isinstance(max_total, int) and max_total > 0 else _DEFAULT_MAX_WORKSPACE_BYTES,
    )


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the delete confirmation and the delete post."""
    # Imported inside `register`, the same way `persona_delete.py` imports
    # it: see the note in that module about the factory and import cycles.
    from personacore.admin.routes import _record_change

    templates = ctx.templates
    audit = ctx.audit
    layout = ctx.layout
    require_user = ctx.require_user
    # Built locally over `ctx.audit`, the same way `chat.py`'s own
    # `register` and `chat_attachments.py` each build one: a screen module's
    # `UIContext` carries no conversation service of its own, so every file
    # that needs one builds it over the same store. `layout` is passed so a
    # conversation this deletes takes its workspace with it, the same reason
    # `chat.py`'s own instance now carries one for `hide`.
    conversations = ConversationService(audit, surface=Surface.ADMIN_UI, layout=layout)

    def _owner_or_404(user: str) -> Owner:
        try:
            name = normalise_name(user)
        except AccountRejected as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from exc
        return Owner.profile(name)

    async def _conversation_or_404(request: Request, user: str, conversation_id: str) -> Any:
        owner = _owner_or_404(user)
        conversation = await conversations.resolve(
            owner, conversation_id=conversation_id, include_hidden=True
        )
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return owner, conversation

    async def _attachment_ids(owner: Owner, conversation_id: str) -> list[str]:
        lister = getattr(audit, "attachments_for_conversation", None)
        if lister is None:
            return []
        try:
            records = await lister(conversation_id, owner=owner)
        except Exception as exc:  # noqa: BLE001 - a count must not take the screen down
            logger.error("review_delete_attachments_failed", error=repr(exc))
            return []
        return [record.attachment_id for record in records]

    def _file_count(request: Request, conversation_id: str) -> int:
        max_file, max_total = _workspace_ceilings(request)
        try:
            workspace = workspaces_module.Workspace(
                layout,
                conversation_id,
                max_file_bytes=max_file,
                max_workspace_bytes=max_total,
            )
        except workspaces_module.WorkspaceError:
            return 0
        return len(workspace.list())

    async def _delete_confirm_context(
        request: Request, user: str, conversation_id: str
    ) -> dict[str, Any]:
        owner, conversation = await _conversation_or_404(request, user, conversation_id)
        attachment_count = len(await _attachment_ids(owner, conversation.conversation_id))
        files = _file_count(request, conversation.conversation_id)
        return {
            "title": DELETE_TITLE,
            "body": delete_body(
                title=conversation.title,
                messages=conversation.message_count,
                attachments=attachment_count,
                files=files,
            ),
            "confirm_label": DELETE_LABEL,
        }

    async def _list_fragment(request: Request, user: str) -> HTMLResponse:
        """The review list, redrawn after a delete — the swap boundary
        ``fragments/review_conversations.html`` names, same idea as key
        revoke's own ``#key-list``."""
        owner = _owner_or_404(user)
        shows_hidden = store_reports_hidden(audit)
        listing = getattr(audit, "list_conversations", None)
        found: list[Any] = []
        if listing is not None:
            extra = {"include_hidden": True} if shows_hidden else {}
            try:
                found = await listing(owner=owner, surface=None, limit=CONVERSATIONS_SHOWN, **extra)
            except Exception as exc:  # noqa: BLE001 - see review.py's own rule
                logger.error("review_delete_list_failed", error=repr(exc))
                found = []
        return templates.TemplateResponse(
            request=request,
            name="fragments/review_conversations.html",
            context={
                "chosen": user,
                "rows": conversation_rows(found, active=None),
                "opened": None,
                "messages": [],
                "nobody_picked": NOBODY_PICKED,
                "nothing_to_show": NOTHING_TO_SHOW,
            },
        )

    @router.get(
        "/review/{conversation_id}/delete/confirm",
        response_class=HTMLResponse,
        summary="Confirm deleting one conversation (page)",
    )
    async def review_delete_confirm_page(
        request: Request, conversation_id: str, user: str = ""
    ) -> HTMLResponse:
        """The no-script fallback (ADR-0020) for :func:`review_delete_confirm`
        below — the same facts, as a real page with a real form."""
        context = await _delete_confirm_context(request, user, conversation_id)
        return templates.TemplateResponse(
            request=request,
            name="confirm_page.html",
            context={
                **await ctx.shell(request, "review"),
                **context,
                "action": f"/admin/review/{conversation_id}/delete?user={quote(user)}",
                "back_href": (
                    f"/admin/review?user={quote(user)}&conversation={quote(conversation_id)}"
                ),
                "back_label": "← Review",
            },
        )

    @router.get(
        "/review/{conversation_id}/delete/confirm/fragment",
        response_class=HTMLResponse,
        summary="Confirm deleting one conversation",
    )
    async def review_delete_confirm(
        request: Request, conversation_id: str, user: str = ""
    ) -> HTMLResponse:
        """Names what it destroys — see :func:`delete_body`."""
        context = await _delete_confirm_context(request, user, conversation_id)
        return templates.TemplateResponse(
            request=request,
            name="fragments/confirm.html",
            context={
                **context,
                "action": f"/admin/review/{conversation_id}/delete?user={quote(user)}",
                "target": "#review-conversations",
            },
        )

    @router.post(
        "/review/{conversation_id}/delete",
        response_class=HTMLResponse,
        response_model=None,
        summary="Delete one conversation",
    )
    async def review_delete(
        request: Request, conversation_id: str, user: str = ""
    ) -> HTMLResponse | RedirectResponse:
        """Destroy the conversation, its attachments and its workspace.

        Order: the conversation row and its messages go first
        (``ConversationService.delete``), then its attachments — now, not
        left for the retention sweep to find as orphans later — then its
        workspace folder.

        **The audit record is written before any of that runs**, naming what
        is about to be destroyed — not after, which would leave a crash
        partway through destruction with no record at all that an admin
        delete was even attempted. If destruction raises, a second record
        with ``outcome=FAILURE`` is written and the exception re-raised
        unchanged, into whatever handles an unhandled exception here today.
        """
        owner, conversation = await _conversation_or_404(request, user, conversation_id)
        attachment_ids = await _attachment_ids(owner, conversation.conversation_id)
        files = _file_count(request, conversation.conversation_id)

        admin_user = require_user(request)
        detail = {
            "conversation_id": conversation.conversation_id,
            "messages": conversation.message_count,
            "attachments": len(attachment_ids),
            "files": files,
        }
        await _record_change(
            audit,
            admin_user,
            action="conversation.deleted",
            outcome=AuditOutcome.SUCCESS,
            detail=detail,
        )

        try:
            await conversations.delete(owner, conversation.conversation_id)

            for attachment_id in attachment_ids:
                await attachments_module.delete(layout, audit, attachment_id, owner=owner)

            workspaces_module.remove(layout, conversation.conversation_id)
        except Exception:
            await _record_change(
                audit,
                admin_user,
                action="conversation.deleted",
                outcome=AuditOutcome.FAILURE,
                detail=detail,
            )
            raise

        if request.headers.get("HX-Request"):
            return await _list_fragment(request, user)
        return RedirectResponse(
            f"/admin/review?user={quote(user)}", status_code=status.HTTP_303_SEE_OTHER
        )


__all__ = [
    "DELETE_LABEL",
    "DELETE_TITLE",
    "NOT_FOUND",
    "delete_body",
    "register",
]
