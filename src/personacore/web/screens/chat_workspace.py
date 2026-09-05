"""Workspace file cards on the Chat screen — workspace contract §7.

A file a tool fetched or the persona itself wrote during a turn is shown as a
card under the reply, the same ``attach-card`` tile
:mod:`personacore.web.screens.chat_attachments` draws for a document — never
an editor, never a preview, a card is a link. This module is that card's own
half of the feature: the shape a template draws (:class:`WorkspaceChip`), the
route that serves one file's bytes back (:func:`register`), and the read side
of the audit record :mod:`personacore.web.screens.chat_streaming` writes when
a turn's tool calls left files behind (:func:`workspace_files_from_detail`).

**Never the storage or the tools.** :mod:`personacore.workspaces` owns the
folder, the filename rule and the ceilings; this module only ever turns a
name already on disk into a link, and turns that link back into bytes for
the owner who may have them. Modelled on :mod:`personacore.web.screens.
chat_attachments` one level down: same shape, same owner check, same
"a mismatch and a missing file answer alike" rule (contract §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from personacore import workspaces as workspaces_module
from personacore.audit.models import AuditCategory, Owner, Surface
from personacore.conversations.service import ConversationService

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Collection, Sequence

    from personacore.config.appdata import AppdataLayout
    from personacore.web.shared import UIContext

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# One workspace file, in the shape a template draws
# ---------------------------------------------------------------------------

WORKSPACE_URL_PREFIX = "/admin/chat/workspace/"
"""Where one workspace file's bytes may be fetched from the Chat screen, and
nowhere else — the same discipline
:data:`personacore.web.screens.chat_attachments.ATTACHMENT_URL_PREFIX` keeps,
so a template never has to trust a URL it is handed."""

NOT_FOUND = "There is no workspace file there."
"""One sentence for a conversation that is not this operator's and a file
that is not in it — contract §7: telling the two apart from outside is a way
to go looking for somebody else's workspace by name."""

_DEFAULT_MAX_FILE_BYTES = 2_000_000
_DEFAULT_MAX_WORKSPACE_BYTES = 50_000_000
"""Used only to build a :class:`~personacore.workspaces.Workspace` for
reading a file back — see
:func:`personacore.web.screens.review_delete._workspace_ceilings`'s own copy
of this pair for the same reasoning: neither ceiling is enforced by a read,
only by a write, so a value nobody has touched is as good as the real one."""

_KIND_LABELS: dict[str, str] = {
    ".md": "Markdown",
    ".json": "JSON",
}
_DEFAULT_KIND_LABEL = "Text"
"""The card's sublabel, ``Workspace · {kind}`` — the approved design's own
three words, by extension: a workspace file is always one of these three
shapes (contract §3/§4 only ever write ``.md``, ``.json`` or a plain-text
``.txt``), so there is no "unknown" case to spell."""


def kind_label_for(name: str) -> str:
    """``Markdown``/``JSON``/``Text``, by ``name``'s extension."""
    lowered = name.lower()
    for suffix, label in _KIND_LABELS.items():
        if lowered.endswith(suffix):
            return label
    return _DEFAULT_KIND_LABEL


def _mime_type(name: str) -> str:
    """What this file is served as — never guessed from bytes, always from
    the same extension :func:`kind_label_for` reads, so the two never
    disagree about what kind of file this is."""
    lowered = name.lower()
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith(".json"):
        return "application/json"
    return "text/plain"


@dataclass(frozen=True, slots=True)
class WorkspaceChip:
    """Everything a template needs to draw one workspace file's card."""

    name: str
    url: str
    kind_label: str
    pinned: bool = False
    """Contract §13's own marker — read back through :func:`pinned_names_for`
    (a live card) or off ``FileEntry.pinned`` (the review screen's own
    listing). ``False`` by default, which is also what an old core that has
    not landed ``Workspace.pinned()`` yet always answers with — a card never
    claims a file is pinned when nothing on disk can say so."""
    pin_url: str = ""
    unpin_url: str = ""
    """Where the **Pin**/**Unpin** control on the owner's own card posts —
    empty on the review screen's own chips, which show the marker only and
    draw no control (contract §13: "the marker only, no control")."""


def chip_for(conversation_id: str, name: str, *, pinned: bool = False) -> WorkspaceChip:
    """One workspace file, as the templates draw it.

    Built from a name alone — no disk read here — because a card drawn live,
    the moment a tool result names the file, must not wait on a second read of
    something :mod:`personacore.workspaces` has only just written. A name that
    no longer exists by the time the link is followed is exactly what the
    download route's own 404 is for.

    ``pinned`` is the one exception to "no disk read": the caller has already
    done that read (:func:`pinned_names_for`), once for the whole card list
    rather than once per card, and hands the answer in here.
    """
    return WorkspaceChip(
        name=name,
        url=f"{WORKSPACE_URL_PREFIX}{conversation_id}/{name}",
        kind_label=kind_label_for(name),
        pinned=pinned,
        pin_url=f"{WORKSPACE_URL_PREFIX}{conversation_id}/{name}/pin",
        unpin_url=f"{WORKSPACE_URL_PREFIX}{conversation_id}/{name}/unpin",
    )


def chips_for_names(
    conversation_id: str, names: Sequence[str], *, pinned: Collection[str] = ()
) -> list[WorkspaceChip]:
    """Every name in ``names``, as cards — in order, nothing dropped.

    Unlike :func:`personacore.web.screens.chat_attachments.chips_for_ids`,
    which drops an id that no longer resolves, a workspace name that is gone
    still draws a card: the turn that produced it is still true history, and
    a card whose link now 404s says so plainly rather than making the file
    disappear from a conversation that really did produce it.

    ``pinned`` is every name in this workspace :func:`pinned_names_for` found
    pinned — read once by the caller for the whole list, not once per name.
    """
    pinned_set = pinned if isinstance(pinned, (set, frozenset)) else set(pinned)
    return [chip_for(conversation_id, name, pinned=name in pinned_set) for name in names]


def pinned_names_for(layout: AppdataLayout, conversation_id: str) -> frozenset[str]:
    """Every name pinned in this conversation's workspace — contract §13.

    Reads :meth:`personacore.workspaces.Workspace.pinned`, the core joint
    this screen builds against before the other half of the contract has
    necessarily landed it: ``getattr`` degrades to "nothing is pinned" on a
    :class:`~personacore.workspaces.Workspace` too old to have the method at
    all, the same tolerance every other reader of an evolving core call takes
    on this screen (see :func:`_workspace_ceilings`'s own comment on reading
    settings that may not exist yet). The ceilings passed to
    :class:`~personacore.workspaces.Workspace` are the defaults, never the
    configured ones: neither is enforced by a read, only by a write (see
    :func:`_workspace_ceilings`'s own reasoning), so this needs no request to
    read them off.
    """
    try:
        workspace = workspaces_module.Workspace(
            layout,
            conversation_id,
            max_file_bytes=_DEFAULT_MAX_FILE_BYTES,
            max_workspace_bytes=_DEFAULT_MAX_WORKSPACE_BYTES,
        )
    except workspaces_module.WorkspaceError:
        return frozenset()
    getter = getattr(workspace, "pinned", None)
    if getter is None:
        return frozenset()
    try:
        return frozenset(str(name) for name in getter())
    except Exception:  # noqa: BLE001 - a pin list must never break the card list
        return frozenset()


# ---------------------------------------------------------------------------
# The read side of the audit record chat_streaming writes
# ---------------------------------------------------------------------------

WORKSPACE_FILES_CATEGORY = AuditCategory.EVENT
WORKSPACE_FILES_ACTION = "chat.workspace_files"
"""Where one turn's own workspace files are filed — beside its attachments
(:data:`personacore.web.screens.chat_attachments.ATTACHMENTS_ACTION`) and its
timing (:data:`personacore.web.screens.chat_reply.TURN_METRICS_ACTION`), for
the same reason: neither of :class:`~personacore.audit.models.AuditCategory`'s
named shelves is this, and that enum's own docstring says the list is a
floor, not a ceiling. ``detail`` carries ``files`` — every name this turn's
own tool calls left behind, in the order they arrived, never a path and never
the file's own bytes."""


def workspace_files_from_detail(detail: Any) -> list[str]:
    """The names back out of one :data:`WORKSPACE_FILES_ACTION` record's
    ``detail`` — the read side of :mod:`chat_streaming`'s write, kept here so
    the shape of ``detail`` has exactly one definition, the same discipline
    :func:`personacore.web.screens.chat_attachments.attachments_from_detail`
    keeps for its own record.
    """
    if not isinstance(detail, dict):
        return []
    raw = detail.get("files")
    return [str(item) for item in raw] if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# Serving one workspace file's bytes — contract §7
# ---------------------------------------------------------------------------


def _workspace_ceilings(request: Request) -> tuple[int, int]:
    """The configured workspace ceilings, or the defaults nobody who never
    touched the setting would differ from.

    Read off ``app.state.settings`` per request rather than captured once —
    the same reasoning
    :func:`personacore.web.screens.review_delete._workspace_ceilings` gives
    for its own copy of this helper, kept separate rather than shared for the
    same reason that one is its own: a screen module is meant to be read
    without following an import into another one, and this is four lines.
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
    """Register the one route this file owns: fetching a workspace file back.

    Registered from :func:`personacore.web.routes.create_admin_ui_router`
    beside ``chat_attachments_screen.register`` — a socket under the Chat
    screen rather than the screen's own rendering, the same reason that
    module gives for being separate. Already covered by ``MEMBER_PREFIXES``'s
    ``"/admin/chat/"`` entry, so a household member can fetch their own
    workspace files without becoming an administrator to do it.
    """
    require_user = ctx.require_user
    layout = ctx.layout
    store = ctx.audit
    conversations = ConversationService(store, surface=Surface.ADMIN_UI)
    _card_template = ctx.templates.get_template("fragments/chat_workspace_card.html")

    def _wants_fragment(request: Request) -> bool:
        """Whether this request came from htmx (a Pin/Unpin tap with scripts
        on) rather than a plain form post — the same test
        :func:`personacore.web.screens.memory._wants_fragment` makes, kept
        as its own four lines for the same reason this module already keeps
        its own copy of :func:`_workspace_ceilings`."""
        return request.headers.get("hx-request", "").lower() == "true"

    async def _pin_or_unpin(
        request: Request, conversation_id: str, name: str, *, pin: bool
    ) -> Response:
        """**Pin**/**Unpin**, contract §13's own control beside the download
        link. Owner-checked exactly as :func:`chat_workspace_file` above is.

        Calls :meth:`personacore.workspaces.Workspace.pin`/``unpin`` — the
        core joint this screen builds against (``working/contracts/
        workspace.md`` §13) — which may not exist on a core that has not
        landed the other half of the contract yet; that is a plain
        ``AttributeError`` today; the day it lands, this needs no change.
        """
        user = require_user(request)
        owner = Owner.profile(user.id)
        conversation = await conversations.resolve(owner, conversation_id=conversation_id)
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
            if pin:
                workspace.pin(name)
            else:
                workspace.unpin(name)
        except (workspaces_module.WorkspaceError, OSError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from exc
        if _wants_fragment(request):
            chip = chip_for(conversation_id, name, pinned=pin)
            return HTMLResponse(_card_template.render(chip=chip))
        marker = quote(conversation.started_at.isoformat())
        return RedirectResponse(f"/admin/chat?c={marker}", status_code=status.HTTP_303_SEE_OTHER)

    @router.post(
        "/chat/workspace/{conversation_id}/{name}/pin",
        summary="Pin one workspace file, to its owner only",
    )
    async def chat_workspace_pin(request: Request, conversation_id: str, name: str) -> Response:
        return await _pin_or_unpin(request, conversation_id, name, pin=True)

    @router.post(
        "/chat/workspace/{conversation_id}/{name}/unpin",
        summary="Unpin one workspace file, to its owner only",
    )
    async def chat_workspace_unpin(request: Request, conversation_id: str, name: str) -> Response:
        return await _pin_or_unpin(request, conversation_id, name, pin=False)

    @router.get(
        "/chat/workspace/{conversation_id}/{name}",
        summary="One workspace file's bytes, to its owner only",
    )
    async def chat_workspace_file(request: Request, conversation_id: str, name: str) -> Response:
        """Serve one workspace file.

        **Owner-checked exactly as** :func:`personacore.web.screens.
        chat_attachments.chat_attachment` **is**: the conversation is
        resolved against this operator's own id first, and a conversation
        that exists but is not theirs answers exactly like one that does not
        exist at all — :data:`NOT_FOUND` either way. The file name is
        checked by :class:`~personacore.workspaces.Workspace` itself against
        the same rule every workspace file is written under
        (``workspaces.FILENAME_PATTERN``), so a name shaped wrong and a name
        that is not there both land on the one refusal
        :class:`~personacore.workspaces.WorkspaceError` carries — folded into
        the same 404 rather than given a sentence of their own, for the same
        "do not tell an id from a name from outside" reason the conversation
        check follows.
        """
        user = require_user(request)
        owner = Owner.profile(user.id)
        conversation = await conversations.resolve(owner, conversation_id=conversation_id)
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
                # The layer under the mime type above: this is served on the
                # admin origin and must not be re-interpreted by a browser
                # that prefers to guess from the bytes — the same header
                # `chat_attachments.chat_attachment` serves an attachment
                # with, for the same reason.
                "X-Content-Type-Options": "nosniff",
            },
        )


__all__ = [
    "NOT_FOUND",
    "WORKSPACE_FILES_ACTION",
    "WORKSPACE_FILES_CATEGORY",
    "WORKSPACE_URL_PREFIX",
    "WorkspaceChip",
    "chip_for",
    "chips_for_names",
    "kind_label_for",
    "pinned_names_for",
    "register",
    "workspace_files_from_detail",
]
