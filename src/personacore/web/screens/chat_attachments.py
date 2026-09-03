"""Attachments on the Chat screen — docs/contracts/attachments.md.

This is the composer/display half of the feature. The storage half shipped in
v0.13.5 (PC-371) and is not this file's to change: :mod:`personacore.attachments`
owns ``put``/``get``/``delete``, the 100 MB ceiling and the media-type allowlist,
and :mod:`personacore.audit.store_attachments` owns the table and the
owner-scoped query. What lives here is everything between a person choosing a
file and a chip appearing on screen.

**Two phases, because of where the id comes from.** An attachment's id is
minted by :func:`personacore.attachments.put`, and ``put`` needs the turn's
own ``correlation_id`` before it can be called (contract §3: the row is the
authority on who may fetch the file, so the file has to be filed under the id
the row actually gets — a mismatch is exactly how PC-371's orphan sweep would
find nothing and remove a live attachment on the next pass). That id is not
known until the agent loop has written the person's message row, which is
*after* the turn has already been asked — so this is not one function:

* :func:`read_pending` runs **before** the turn. It reads every text/plain
  part into memory (bounded by the same ceiling ``put`` enforces, so a part
  that lies about being small cannot be read whole first) and leaves an image
  part untouched on disk-or-memory wherever Starlette put it — nothing here
  ever holds a 100 MB photo just to look at it.
* :func:`compose_model_message` builds what the model is actually asked —
  contract §4.1, the person's own words followed by every text attachment's,
  in order. Nothing about ids is in it, because none exist yet.
* :func:`store_pending` runs **after** the turn, once the row it belongs to
  has been read back (exactly the way :mod:`chat_streaming`'s
  ``_record_turn_metrics`` already reads a row back to learn what only the
  loop knows). It calls ``put`` for each part, and records which ids landed.

**Where the ids go, so a reload can find them again.** ``TranscriptRecord``'s
fields are the chat-room contract's (ADR-0004) and are not this file's to
extend, so the ids are not a new column — they are an
:class:`~personacore.audit.models.AuditRecord` sharing the message's own
correlation id, exactly the way ``chat_reply.TURN_METRICS_ACTION`` already
files a turn's timing beside it. Ageing out, and never inventing a second
retention clock, both fall out of reusing that table rather than adding one.

**What a person typed is not what the model was asked.** Contract §6: a text
attachment renders as a chip, "not as forty screens inlined into the
transcript" — but the chat runner protocol
(:class:`personacore.admin.protocols.ChatRunner`) records exactly the string
it is given, and there is no second channel for "what the model read" apart
from "what the person said" (see :func:`compose_model_message`'s own
docstring). So the row's own ``content`` genuinely does carry the attached
text, once, for this turn — and ``typed_len`` in the same audit record above
is how the screen tells the two apart again on a reload
(:mod:`personacore.web.screens.chat_thread`'s ``_fill_reply`` truncates
back to it before drawing the bubble). This is a design decision made because
the alternative — teaching the chat runner protocol two channels, one that is
recorded and one that is not — is a contract change to a seam this file does
not own, and CLAUDE.md is explicit that a contract change is an owner
checkpoint, not something an implementer routes around quietly. Recorded here
in the open rather than done silently.

**An image is sent to the model, and the core does not gate it.** An earlier
draft of this module built a refusal for sending an image to a model that had
not been probed as able to see it. The owner rejected that: the probe was
never meant to gate sending, a truthful refusal was not wanted, and the plan
is to use a model that supports images. So there is no gate here, on whether
a model was probed as able to see or on anything else — an attached image is
stored, shown, *and* sent, exactly the way a text attachment's words are.

**It reaches the model as an OpenAI-format content array** — a ``text`` part
and one ``image_url`` part per image, each carrying a ``data:`` URI (contract
§4.2) — through an **additive** contract change to the agent loop:
:attr:`personacore.agent.loop.TurnRequest.image_data_urls`, a new field that
defaults to empty. :func:`read_pending` builds the URIs (bounded by
:data:`MAX_SEND_IMAGE_BYTES`, contract §9 — a separate, smaller ceiling than
storage's, because a 100 MB image is roughly 133 MB of base64 in a request
body no backend here will take), :func:`image_data_urls` hands them to
whichever screen is running the turn, and that screen passes them to the
:class:`~personacore.admin.protocols.ChatRunner` it already holds. Every
caller of the loop that predates this field — the OpenAI surface, the
Wyoming path, every plugin, every test built around ``TurnRequest
(user_message=...)`` alone — keeps sending a plain string, because nothing
about ``user_message`` itself changed and nothing makes this field
non-empty except a caller choosing to fill it in. An image too large to fit
under :data:`MAX_SEND_IMAGE_BYTES` is still stored and shown; it is left out
of what the model is asked, and :func:`send_refusals` carries a sentence
saying so — a refusal about size, which the owner accepted, never one
about capability, which the owner rejected.

Never logs attachment content or the name a person chose (contract
§6/§7/§11) — only what :mod:`personacore.audit.store_attachments` already
logs (ids, correlation ids, media types, byte counts).
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from personacore.attachments import (
    NOT_FOUND,
    AttachmentRejected,
    AttachmentStoreFailed,
    attachment_path,
)
from personacore.attachments import get as get_attachment
from personacore.attachments import put as put_attachment
from personacore.audit.models import (
    AttachmentRecord,
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    MessageRole,
    Owner,
    Surface,
    TranscriptRecord,
)
from personacore.conversations.service import ConversationService

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from personacore.admin.models import AdminUser
    from personacore.conversations.models import Conversation
    from personacore.web.shared import UIContext

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# The composer's field, and where a stored attachment is served from
# ---------------------------------------------------------------------------

ATTACHMENTS_FIELD = "attachments"
"""The multipart field name every part arrives under — a real choice off the
file input and a pasted blob ``chat.js`` appends alike. The server does not
need to tell them apart: both are bytes with a filename and a declared type,
and both go through :func:`read_pending` the same way."""

ATTACHMENT_URL_PREFIX = "/admin/chat/attachments/"
"""Where one attachment's bytes may be fetched, and nowhere else — the same
discipline :data:`personacore.web.screens.chat_reply.AUDIO_URL_PREFIX`
keeps for a reply's audio, so a template never has to trust a URL it is
handed."""

_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "text/plain": ".txt",
}
"""What the served filename ends in, keyed by the stored media type. Never
derived from ``original_name`` (contract §6): the extension names what this
core actually stored, not what an uploader's filename happened to claim."""

_KIND_LABELS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
    "text/plain": "TXT",
}
"""The badge over a non-image tile, and the card's kind subtitle (contract
§6a: "Document · MD" is the shape; this is this core's own vocabulary for
it, drawn from the type it actually stored)."""

MAX_ATTACHMENT_LABEL_CHARS = 120
"""Same ceiling :data:`personacore.web.screens.chat_reply.
MAX_TOOL_NAME_LENGTH` uses for the same reason: ``original_name`` is
operator-chosen text (contract §6/§11 — display only, never core-controlled),
and the template escapes it — this exists only so one absurdly long name
cannot make a chip, a card or a rendered exchange unreadable."""

_READ_CHUNK = 64 * 1024
"""Same figure :data:`personacore.attachments._CHUNK_BYTES` streams a store
with, kept here for the same reason: a part declared ``text/plain`` is read
in pieces so a ceiling can be enforced without ever holding the whole of a
dishonestly-labelled upload in memory first."""

MAX_SEND_IMAGE_BYTES = 72 * 1024 * 1024
"""Ceiling on an image *sent* to the model — contract §9.

**Measured against the owner's own host on 2026-09-02, not guessed.** An
earlier draft carried 8 MB as a placeholder with a note that the number was
the owner's to give. The owner pointed out it was measurable directly against
the running inference server instead.

Probed with a valid PNG padded to size, as a ``data:`` URI in an
``image_url`` part, at ascending sizes. The host accepted the request body up
to **96 MB of base64** and answered ``413`` at **107 MB** — a 100 MB body cap.
Below the cap it answered 500 with "image input is not supported", which is a
*content* refusal from a model with no vision projector, not a transport one:
the request had already been read whole.

So the transport limit is about 100 MB of base64, and base64 is four thirds of
the source. 72 MB of source bytes is 96 MB encoded — the largest size actually
observed to be accepted, rather than the largest that might be. The owner's
stated range is 5 to 30 MB per image, which is nowhere near it.

Re-measure if the host changes: a different server, a proxy in front, or a
raised body cap all move this, and a number carried over from another machine
would be a guess wearing a measurement's clothes.

Deliberately separate from :data:`personacore.attachments.MAX_ATTACHMENT_BYTES`
(the 100 MB storage ceiling): an image over this number is still stored and
shown, and only left out of what the model is asked (see
:func:`send_refusals`)."""

# ---------------------------------------------------------------------------
# One attachment, in the one shape a chip, a thumbnail and a card all draw
# from — contract §6a: one shape for every type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttachmentChip:
    """Everything a template needs to draw one attachment, whichever of the
    three places it is drawing it: the composer's pending tile, a thumbnail
    row above a sent message, or a card. The contract is explicit that these
    are one shape with different contents, not three shapes — so this is the
    one dataclass all three read from."""

    attachment_id: str
    is_image: bool
    url: str
    name: str
    """Truncated ``original_name``, display only."""
    kind_label: str
    size_label: str


def _kind_label(media_type: str) -> str:
    return _KIND_LABELS.get(media_type, "FILE")


def _size_label(count: int) -> str:
    """A byte count as an operator reads it — the same rounding
    :func:`personacore.attachments._human_bytes` uses, kept as a small copy
    here rather than an import: that name is not part of the shipped
    module's public surface (see its own ``__all__``), and this file must
    not reach past it."""
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - the loop always returns


def chip_for(record: AttachmentRecord) -> AttachmentChip:
    """One stored attachment, as the templates draw it."""
    return AttachmentChip(
        attachment_id=record.attachment_id,
        is_image=record.media_type in _IMAGE_TYPES,
        url=f"{ATTACHMENT_URL_PREFIX}{record.attachment_id}",
        name=(record.original_name or "attachment")[:MAX_ATTACHMENT_LABEL_CHARS],
        kind_label=_kind_label(record.media_type),
        size_label=_size_label(record.byte_size),
    )


# ---------------------------------------------------------------------------
# Phase A — reading what arrived, before the turn is asked anything
# ---------------------------------------------------------------------------


def gathered_uploads(form: Any) -> list[StarletteUploadFile]:
    """Every real file the composer's field carries, in the order added.

    A browser never sends anything under this name but a file part; the
    ``isinstance`` check is defensive against a hand-crafted request naming
    a plain field ``attachments`` instead, which ``form.getlist`` would
    otherwise hand back as a bare string.
    """
    if not hasattr(form, "getlist"):
        return []
    found = form.getlist(ATTACHMENTS_FIELD)
    return [item for item in found if isinstance(item, StarletteUploadFile) and item.filename]


@dataclass(slots=True)
class PendingAttachment:
    """One upload, read just far enough to know what to do with it.

    ``text`` is the decoded content for a part declared ``text/plain``, and
    ``None`` for everything else.

    ``image_data_url`` and ``send_refusal`` are the image half of the same
    idea: at most one of them is set, and only for a part whose media type is
    one of :data:`_IMAGE_TYPES`. Building the URI here, in this same read
    pass, means there is exactly one place an upload's bytes are read before
    the turn — text and image alike — rather than a second reader
    re-opening what this one just closed.
    """

    upload: StarletteUploadFile
    media_type: str
    text: str | None
    image_data_url: str | None = None
    """A ``data:`` URI ready for the model's content array (contract §4.2),
    or ``None`` for anything that is not a small-enough image — see
    :data:`MAX_SEND_IMAGE_BYTES` and :attr:`send_refusal`."""

    send_refusal: str | None = None
    """A plain sentence, when an image was too large to *send* — contract
    §9. The image is still stored and shown (:func:`store_pending` does not
    consult this field at all); it is only left out of what the model is
    asked. ``None`` for everything else, including every non-image part."""


async def read_pending(uploads: Sequence[StarletteUploadFile]) -> list[PendingAttachment]:
    """Read every text part in full, and every image part far enough to know
    whether it can be sent. Must run before the turn — see the module
    docstring for why this cannot be one function with :func:`store_pending`.

    Bounded either way: a text part stops at
    :data:`~personacore.attachments.MAX_ATTACHMENT_BYTES` (the storage
    ceiling — a part that lies about being small text is never held whole in
    memory), and an image part stops at the much smaller
    :data:`MAX_SEND_IMAGE_BYTES` (the send ceiling, contract §9) — reading
    further than that would hold most of a 100 MB photo in memory only to
    throw the read away as too large to send. Either read seeks the upload
    back to the start afterwards, which is what lets :func:`store_pending`
    read the same upload again, in full, for the store.
    """
    pending: list[PendingAttachment] = []
    for upload in uploads:
        media_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
        text: str | None = None
        image_data_url: str | None = None
        send_refusal: str | None = None
        if media_type == "text/plain":
            # Enforced by `personacore.attachments.MAX_ATTACHMENT_BYTES` at
            # store time regardless; this is a defensive read cap so a part
            # that *claims* text/plain and is actually huge is never held
            # whole in memory just to build the model's prompt from it.
            from personacore.attachments import MAX_ATTACHMENT_BYTES

            raw = bytearray()
            while len(raw) <= MAX_ATTACHMENT_BYTES:
                chunk = await upload.read(_READ_CHUNK)
                if not chunk:
                    break
                raw.extend(chunk)
            await upload.seek(0)
            text = raw[:MAX_ATTACHMENT_BYTES].decode("utf-8", errors="replace")
        elif media_type in _IMAGE_TYPES:
            raw = bytearray()
            while len(raw) <= MAX_SEND_IMAGE_BYTES:
                chunk = await upload.read(_READ_CHUNK)
                if not chunk:
                    break
                raw.extend(chunk)
            await upload.seek(0)
            if len(raw) > MAX_SEND_IMAGE_BYTES:
                # Too large to *send* — not a store refusal (`put_attachment`
                # has not even run yet) and not the capability refusal the
                # owner rejected outright: a sentence about size, naming the size,
                # the way contract §9 asks for.
                name = (upload.filename or "That image")[:MAX_ATTACHMENT_LABEL_CHARS]
                send_refusal = (
                    f"{name} is too large to send to the model "
                    f"({_size_label(len(raw))}, over the "
                    f"{_size_label(MAX_SEND_IMAGE_BYTES)} limit for sending an image) — "
                    "it's stored and shown, but the model won't see it."
                )
            else:
                image_data_url = (
                    f"data:{media_type};base64,{base64.b64encode(bytes(raw)).decode('ascii')}"
                )
        pending.append(
            PendingAttachment(
                upload=upload,
                media_type=media_type,
                text=text,
                image_data_url=image_data_url,
                send_refusal=send_refusal,
            )
        )
    return pending


def image_data_urls(pending: Sequence[PendingAttachment]) -> list[str]:
    """The ``data:`` URIs this turn should hand the agent loop — contract
    §4.2, in the order the images were added. Empty when there is nothing to
    send, which is what keeps a turn with no image attached, or a core still
    running an agent loop from before this field existed, composing exactly
    the request it always has (see ``TurnRequest.image_data_urls``)."""
    return [item.image_data_url for item in pending if item.image_data_url is not None]


def send_refusals(pending: Sequence[PendingAttachment]) -> list[str]:
    """Sentences for an image read here but too large to send — contract §9.

    Read *before* the turn runs (see :func:`read_pending`), so this is known
    at the same moment :func:`image_data_urls` is — unlike
    :class:`StoredAttachments.refusals`, which cannot exist until
    :func:`store_pending` has run *after* the turn. A caller shows both
    together in the one notice a person sees under their message.
    """
    return [item.send_refusal for item in pending if item.send_refusal is not None]


TEXT_ATTACHMENT_BLOCK = "\n\n[Attached: {name}]\n{content}"


def compose_model_message(typed: str, pending: Sequence[PendingAttachment]) -> str:
    """What the model is asked — contract §4.1.

    The person's own words, then every text attachment's, in the order they
    were added. An image contributes nothing to *this string* — it travels
    to the model through a separate channel, :func:`image_data_urls`, because
    the OpenAI content-array shape contract §4.2 asks for is a ``text`` part
    plus an ``image_url`` part, not one string with an image folded into it.
    This function stays the text half of that shape.

    See the module docstring for why this string is also, unavoidably, what
    ends up in the transcript row's own ``content`` — and why that is a
    stated decision rather than an oversight.
    """
    blocks = [
        TEXT_ATTACHMENT_BLOCK.format(
            name=(item.upload.filename or "attachment")[:MAX_ATTACHMENT_LABEL_CHARS],
            content=item.text,
        )
        for item in pending
        if item.text is not None
    ]
    return typed + "".join(blocks)


# ---------------------------------------------------------------------------
# Phase B — storing, once the message's own correlation id is known
# ---------------------------------------------------------------------------

ATTACHMENTS_CATEGORY = AuditCategory.EVENT
ATTACHMENTS_ACTION = "chat.attachments"
"""Where one message's attachment ids are filed — beside its own turn's
timing (``chat_reply.TURN_METRICS_ACTION``) and for the same reason: neither
is one of :class:`~personacore.audit.models.AuditCategory`'s named shelves,
and that enum's own docstring says the list is a floor, not a ceiling.
``detail`` carries ``ids`` (the stored attachment ids, in order — never a
filename, never bytes) and ``typed_len`` (see the module docstring)."""


@dataclass(frozen=True, slots=True)
class StoredAttachments:
    chips: tuple[AttachmentChip, ...]
    refusals: tuple[str, ...]
    """A sentence per part that could not be kept — too large, wrong type,
    or a filesystem failure. Contract §6a/§10: a refusal is a sentence where
    the control is, never a silently dropped file."""


async def store_pending(
    ctx: UIContext,
    *,
    owner: Owner,
    correlation_id: str,
    conversation_id: str | None,
    timestamp: datetime,
    typed_len: int,
    pending: Sequence[PendingAttachment],
) -> StoredAttachments:
    """Persist every pending upload against the message that just landed.

    Called only once that message's own correlation id is known (see the
    module docstring). A caller with nothing pending never reaches this.
    """
    if not pending:
        return StoredAttachments(chips=(), refusals=())
    chips: list[AttachmentChip] = []
    refusals: list[str] = []
    ids: list[str] = []
    for item in pending:
        try:
            record = await put_attachment(
                ctx.layout,
                ctx.audit,
                item.upload.file,
                original_name=item.upload.filename or "attachment",
                media_type=item.media_type,
                owner=owner,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
            )
        except (AttachmentRejected, AttachmentStoreFailed) as exc:
            # Both exceptions already carry a finished, plain-English
            # sentence (contract §6a/§10) — nothing here rewords them.
            refusals.append(str(exc))
            continue
        chips.append(chip_for(record))
        ids.append(record.attachment_id)
    if ids:
        try:
            await ctx.audit.record_audit(
                AuditRecord(
                    correlation_id=correlation_id,
                    timestamp=timestamp,
                    surface=Surface.ADMIN_UI,
                    owner=owner,
                    category=ATTACHMENTS_CATEGORY,
                    action=ATTACHMENTS_ACTION,
                    outcome=AuditOutcome.SUCCESS,
                    detail={"ids": ids, "typed_len": typed_len},
                )
            )
        except Exception as exc:  # noqa: BLE001 - a lost replay linkage beats a dead turn
            log.warning(
                "chat_attachments_link_failed", correlation_id=correlation_id, error=repr(exc)
            )
    return StoredAttachments(chips=tuple(chips), refusals=tuple(refusals))


async def written_message_row(
    thread_rows: Callable[[AdminUser, datetime | None], Awaitable[Sequence[TranscriptRecord]]],
    user: AdminUser,
    opened: datetime,
    since: datetime,
) -> TranscriptRecord | None:
    """The person's own message row a just-run turn wrote, or ``None``.

    Shared by :mod:`chat_exchange` and :mod:`chat_streaming` — both run a
    turn the same way and both need the same answer to "which row does this
    attachment belong to", read back rather than carried from the turn's own
    result for the reason ``chat_streaming._turn_reply_row`` already reads
    the *assistant* row back: the correlation id (contract §3) is the agent
    loop's own, bound once per request inside its own ``self._run()`` and
    never handed to whoever started the turn.

    ``thread_rows`` is the screen's own read (``ChatView.thread_rows``),
    passed in rather than imported so this stays independent of which
    caller's closures it is reading through. ``opened`` must be the concrete
    instant naming the conversation, never the possibly-``None`` ``started``
    a fresh conversation arrives with — that function treats ``None`` as
    "nothing said yet" and would find nothing. ``since`` is a moment
    strictly before the loop could have written anything; only the *first*
    persona's turn in an exchange ever writes this row
    (``record_user_message``), so there is at most one to find.
    """
    rows = await thread_rows(user, opened)
    return next(
        (row for row in rows if row.role is MessageRole.USER and row.timestamp >= since),
        None,
    )


async def finish_pending(
    ctx: UIContext,
    thread_rows: Callable[[AdminUser, datetime | None], Awaitable[Sequence[TranscriptRecord]]],
    *,
    user: AdminUser,
    conversation: Conversation | None,
    opened: datetime,
    since: datetime,
    typed: str,
    pending: Sequence[PendingAttachment],
) -> tuple[tuple[AttachmentChip, ...], str]:
    """Store one turn's pending uploads and say what needs saying about them.

    The one call both ``chat_exchange._turn`` and ``chat_streaming.
    _turn_frames`` make once the first reply's row exists — see
    :func:`written_message_row` for why not any earlier. The only notice this
    returns is a part that could not be *stored* (too large for
    :data:`~personacore.attachments.MAX_ATTACHMENT_BYTES`, wrong type —
    contract §6a/§10). A part too large to *send* is a different notice,
    known before this function is even reached (:func:`send_refusals`, read
    off :func:`read_pending`'s own output) — the caller is responsible for
    combining the two into what a person sees under their message.
    """
    row = await written_message_row(thread_rows, user, opened, since)
    if row is None:
        # Nothing was recorded to file these under — a store too old to
        # write transcript rows, or a turn that raised before the loop wrote
        # anything at all. Contract §10: nothing is silently discarded, so
        # this is said rather than swallowed.
        return (
            (),
            "What was attached could not be saved: the message it belonged to was never recorded.",
        )
    result = await store_pending(
        ctx,
        owner=Owner.profile(user.id),
        correlation_id=row.correlation_id,
        conversation_id=conversation.conversation_id if conversation else None,
        timestamp=row.timestamp,
        typed_len=len(typed),
        pending=pending,
    )
    return result.chips, " ".join(result.refusals)


def attachments_from_detail(detail: Any) -> tuple[list[str], int | None]:
    """The ids and ``typed_len`` back out of one :data:`ATTACHMENTS_ACTION`
    record's ``detail`` — the read side of :func:`store_pending`'s write,
    kept here so the shape of ``detail`` has exactly one definition.
    """
    if not isinstance(detail, dict):
        return [], None
    raw_ids = detail.get("ids")
    ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
    typed_len = detail.get("typed_len")
    return ids, (int(typed_len) if isinstance(typed_len, int) else None)


async def chips_for_ids(
    ctx: UIContext, *, owner: Owner, ids: Sequence[str]
) -> list[AttachmentChip]:
    """Hydrate a replayed message's attachment ids back into chips.

    One owner-scoped :func:`personacore.attachments.get` per id — there is
    no query that lists a correlation id's attachments (PC-371 did not ship
    one, and :mod:`personacore.audit` is not this file's to extend), so this
    is the shape a small, known list of ids is turned into records. A vanished
    or already-purged id is left out rather than drawn as a broken tile.
    """
    chips: list[AttachmentChip] = []
    for attachment_id in ids:
        record = await get_attachment(ctx.audit, attachment_id, owner=owner)
        if record is not None:
            chips.append(chip_for(record))
    return chips


# ---------------------------------------------------------------------------
# Serving one attachment's bytes — contract §6
# ---------------------------------------------------------------------------


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the one route this file owns: fetching an attachment back.

    Registered from :func:`personacore.web.routes.create_admin_ui_router`
    beside ``chat_audio_screen.register`` — a socket under the Chat screen
    rather than the screen's own rendering, the same reason that module gives
    for being separate. Already covered by ``MEMBER_PREFIXES``'s
    ``"/admin/chat/"`` entry, so a household member can fetch their own
    attachments without becoming an administrator to do it.
    """
    require_user = ctx.require_user
    layout = ctx.layout
    store = ctx.audit
    templates = ctx.templates
    # Built locally, the same way `chat.py`'s own `register` does (its own
    # `conversations = ConversationService(audit, surface=Surface.ADMIN_UI)`)
    # — a screen module's own `UIContext` carries no conversation service of
    # its own, so each file that needs one builds it over the same store.
    conversations = ConversationService(store, surface=Surface.ADMIN_UI)

    @router.get(
        "/chat/attachments/{attachment_id}",
        summary="One attachment's bytes, to its owner only",
    )
    async def chat_attachment(request: Request, attachment_id: str) -> Any:
        """Serve one attachment.

        **Owner-checked** the same way a reply's audio handle is
        (``chat_audio.py``): :func:`personacore.attachments.get` filters by
        owner in the query's own ``WHERE`` (contract §3), so an id that
        exists and is not this operator's answers exactly like an id that
        does not exist at all — :data:`personacore.attachments.NOT_FOUND`
        either way, because telling the two apart from outside is how
        somebody goes looking for somebody else's attachments by id.

        **The content type is this core's own**, read off the stored record
        — never whatever a browser once claimed on upload — and it can only
        ever be one of :data:`personacore.attachments.MEDIA_TYPE_ALLOWLIST`,
        which excludes ``text/html`` by construction (see the assertion
        beside it in that module). ``Content-Disposition`` and the served
        filename are built from the validated id, never from
        ``original_name`` (contract §6).
        """
        user = require_user(request)
        record = await get_attachment(store, attachment_id, owner=Owner.profile(user.id))
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        path = attachment_path(layout, record)
        is_image = record.media_type in _IMAGE_TYPES
        extension = _EXTENSIONS.get(record.media_type) or (
            mimetypes.guess_extension(record.media_type) or ".bin"
        )
        return FileResponse(
            path,
            media_type=record.media_type,
            filename=f"{record.attachment_id}{extension}",
            # Inline for an image, so it can be used as a thumbnail's `src`
            # rather than forcing a download; a non-image "opens" (contract
            # §6a), which for a browser navigating this URL directly means
            # downloading it.
            content_disposition_type="inline" if is_image else "attachment",
            headers={
                # An attachment's bytes never change once stored (there is no
                # edit), and the id is unguessable — safe to cache, and
                # `private` because this is one owner's file, not a shared
                # asset a proxy in front of this core may cache for anybody.
                "Cache-Control": "private, max-age=86400",
                # The layer under the allowlist. `media_type` above is this
                # core's own and can only be one of
                # `personacore.attachments.MEDIA_TYPE_ALLOWLIST` — but a
                # browser is free to ignore a declared type and guess from the
                # bytes, and these bytes are served inline on the admin
                # origin. This is what makes "it is served as what it was
                # stored as" a promise the browser keeps rather than one it is
                # merely offered. Nothing about the stored bytes changes; this
                # is the header that stops them being re-interpreted.
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get(
        "/chat/attachments/{attachment_id}/view",
        response_class=HTMLResponse,
        summary="One picture, full size, with a way back and a way to keep it",
    )
    async def chat_attachment_view(request: Request, attachment_id: str) -> HTMLResponse:
        """The viewer page — image-viewer.md contract §2.

        **Owner-checked exactly as** :func:`chat_attachment` **is**, for the
        same reason (contract §2): somebody else's id must answer exactly like
        a missing one, or an id becomes a way to probe who has what.

        **Not an image** (a document's own id, fetched here anyway) is the
        same 404 — this route is for pictures; a document still opens through
        :func:`chat_attachment` as it always has.

        **``back_url`` is never built from the record's own**
        ``conversation_id`` **directly** — image-viewer.md §2 (corrected
        2026-09-03): that id is a UUID4 (`Conversation.conversation_id`),
        and ``/admin/chat``'s own ``?c=`` is not that at all — it is an ISO
        instant (``chat_thread.conversation_start`` parses it with
        ``datetime.fromisoformat``), the same value ``chat.py``'s
        ``chat_new_image`` puts in its own redirect
        (``quote(conversation.started_at.isoformat())``). A link built from
        the UUID would not error; it would silently open a fresh, empty
        conversation instead of this one. So the id is resolved back to the
        real :class:`~personacore.conversations.models.Conversation` first
        (:meth:`ConversationService.resolve`, owner-checked the same way
        this route's own attachment lookup is — a conversation that is not
        this owner's, or hidden, or gone, resolves to ``None`` exactly like
        one that never existed), and ``back_url`` is built from *its*
        ``started_at`` — never from anything a caller sends either way, so a
        query parameter here can never choose where "Back" goes.
        """
        user = require_user(request)
        owner = Owner.profile(user.id)
        record = await get_attachment(store, attachment_id, owner=owner)
        if record is None or record.media_type not in _IMAGE_TYPES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        chip = chip_for(record)
        back_url = "/admin/chat"
        if record.conversation_id:
            found = await conversations.resolve(owner, conversation_id=record.conversation_id)
            if found is not None:
                back_url = f"/admin/chat?c={quote(found.started_at.isoformat())}"
        return templates.TemplateResponse(
            request=request,
            name="attachment_view.html",
            context={
                **await ctx.shell(request, "chat"),
                "image_url": chip.url,
                "download_url": chip.url,
                "name": chip.name,
                "back_url": back_url,
            },
        )


__all__ = [
    "ATTACHMENTS_ACTION",
    "ATTACHMENTS_CATEGORY",
    "ATTACHMENTS_FIELD",
    "ATTACHMENT_URL_PREFIX",
    "MAX_ATTACHMENT_LABEL_CHARS",
    "MAX_SEND_IMAGE_BYTES",
    "TEXT_ATTACHMENT_BLOCK",
    "AttachmentChip",
    "PendingAttachment",
    "StoredAttachments",
    "attachments_from_detail",
    "chip_for",
    "chips_for_ids",
    "compose_model_message",
    "finish_pending",
    "gathered_uploads",
    "image_data_urls",
    "read_pending",
    "register",
    "send_refusals",
    "store_pending",
    "written_message_row",
]
