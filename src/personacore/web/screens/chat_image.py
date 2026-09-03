"""The image-conversation responder — docs/contracts/image-conversations.md.

Contract §4: `kind` selects *which service answers*, and the shape to avoid
is an `if kind == "image"` scattered through the request path — one place
maps a kind to a responder instead. That one place is
:data:`~personacore.web.screens.chat_exchange.KIND_RESPONDERS`, read by
:func:`~personacore.web.screens.chat_exchange.register`'s own
``_answered_by_kind`` — the single function *both* send paths ask before
anything else they do, the serial ``/chat/fragment`` and the streamed
``/chat/stream`` alike. It was a check inside the serial path alone for one
day, and every browser that can stream — which is every browser in use —
sent an image conversation's message to the language model instead. A second
copy of the check would have hidden the same hole for the next member the
contract names.

Everything past that hand-off is this kind's own, small world — contract §3
in full: no persona, no room, no floor-asking, no reasoning trace, no
compaction. An implementer who finds themselves teaching persona resolution
about `image` has taken a wrong turn; this module is where that turn was
avoided instead.

**The exchange is two ordinary transcript rows.** A USER row (the prompt) and
an ASSISTANT row (the picture), sharing one correlation id — exactly the
shape the agent loop already writes for a text turn, so the rail, the
download, retention and every other reader that walks the transcript need
nothing new to understand this kind. What *is* new, and confined to this
module: the assistant row's own `content` is, by convention, nothing but the
generated picture's own attachment-serve URL
(:data:`~personacore.web.screens.chat_attachments.ATTACHMENT_URL_PREFIX`)
and never a caption or any other words. That convention exists because
:mod:`personacore.web.markdown` deliberately renders no `<img>` tag at
all — a chat reply is untrusted model output, and that module's closed tag
set is what keeps one from restructuring the page it is shown on. A generated
picture is not that: it is bytes this core made and immediately serves back
to the person who asked for it, so it takes its own narrow path instead of
trying to fit through that one. :func:`hydrate_replayed_images` is the read
side of the same convention, called by ``chat.py`` only for a conversation
whose ``kind`` is already known to be :attr:`~personacore.conversations.
models.ConversationKind.IMAGE` — never for an ordinary reply, where a model
that happened to write that exact string is still just text and must print
as itself.

**And the kind alone is not enough to trust a row.** Where a row sits says
nothing about who wrote it, so what may become a URL is constrained to what
:func:`issued_here` recognises: this core's own serve prefix followed by
nothing but an attachment id of the shape the store itself validates. Any
other content in that same slot renders as ordinary, escaped text.

**The picture is stored the same way any attachment is** (contract §8 rule
4) — :func:`personacore.attachments.put`, the one function every uploaded
file already goes through. There is no second store for a generated image.

**A refusal is not persisted.** Contract §9 asks for the failure to appear
"in the thread, where the picture would have been" — and, exactly like a
refused text turn (`chat_exchange._unsendable`, and the exception branch in
`chat_exchange._turn`), nothing about *why* it failed is written to the
transcript. Only the person's own prompt is recorded either way; reopening
the conversation later shows the question and no answer, never a stale error
message a since-fixed server would leave stranded forever.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from markupsafe import Markup

from personacore.attachments import (
    ATTACHMENT_ID_PATTERN,
    AttachmentRejected,
    AttachmentStoreFailed,
)
from personacore.attachments import put as put_attachment
from personacore.audit.models import (
    Author,
    AuthorKind,
    MessageRole,
    Owner,
    Surface,
    TranscriptRecord,
)
from personacore.config.image import ImageSettings
from personacore.images.client import GeneratedImage, ImageClient, ImageRefused, ImageUnavailable
from personacore.web.screens.chat_attachments import ATTACHMENT_URL_PREFIX, chip_for
from personacore.web.screens.chat_reply import PERSONA_UNRECORDED, _refused
from personacore.web.shared import current_config

if TYPE_CHECKING:  # pragma: no cover - the screen builds these and hands them over
    from personacore.admin.models import AdminUser
    from personacore.conversations.models import Conversation
    from personacore.conversations.service import ConversationService
    from personacore.web.shared import UIContext

log = structlog.get_logger(__name__)


IMAGE_NOT_CONFIGURED = (
    "This core has no image generator configured, so this conversation can't "
    "be answered. Add an [image] section to core.toml naming the service's "
    "address."
)
"""Contract §9's third case, beside "can't reach" and "refused": nothing was
ever pointed at anything. Named plainly and specifically, per the build
brief's own instruction, rather than folded into one of the other two
sentences — a person reading this should not go looking for a dead server
that was never configured in the first place."""

IMAGE_RESPONDER_NAME = "Image generator"
"""Who the design names as having answered, over a generated picture's own
row — the same slot :data:`~personacore.web.shared.PERSONA_UNRECORDED`
and :data:`~personacore.web.screens.chat_thread.ASSISTANT_UNATTRIBUTED`
occupy for the cases *they* cover. Contract §3: an image conversation has no
persona, so this is a plain, honest label for the thing that spoke — a
service, not a character — decided here because the contract does not name
one; **this name is this module's own choice**, not the owner's."""


def _settings(ctx: UIContext) -> ImageSettings:
    """This core's own ``[image]`` settings, read fresh — the same per-request
    read every other setting this screen consults gets (`chat._cap`, `chat.
    _browser_dictation_enabled`), because an operator can repoint this at a
    different server without restarting (ADR-0010's reasoning, applied here).

    A ``core.toml`` this core cannot read at all degrades to "unconfigured"
    rather than raising — the same treatment `_browser_dictation_enabled`
    already gives a broken settings file, and for the same reason: a bad file
    must not be what stops an image conversation from saying so plainly.
    """
    current, _unreadable = current_config(ctx.layout)
    section = current.settings.get("image") if current is not None else None
    return ImageSettings.model_validate(section if isinstance(section, dict) else {})


async def _write_row(
    ctx: UIContext,
    *,
    correlation_id: str,
    owner: Owner,
    role: MessageRole,
    content: str,
    author: Author,
    timestamp: datetime,
) -> None:
    """One transcript row, tolerant of a store too old — or too broken right
    now — to take it. The same tolerance `chat_exchange._recorded_unanswered`
    already applies: a lost row costs the reload, never the live reply.
    """
    writer = getattr(ctx.audit, "record_transcript", None)
    if writer is None:
        return
    try:
        await writer(
            TranscriptRecord(
                correlation_id=correlation_id,
                timestamp=timestamp,
                surface=Surface.ADMIN_UI,
                owner=owner,
                role=role,
                content=content,
                author=author,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a lost row beats a dead turn
        log.error("chat_image_write_failed", role=role.value, error=repr(exc))


async def answer(
    ctx: UIContext,
    conversations: ConversationService,
    *,
    user: AdminUser,
    conversation: Conversation,
    since: datetime,
    message: str,
) -> list[dict[str, Any]]:
    """Run this kind's one exchange: record the prompt, ask the image
    service, store what comes back, record the reply. Returns the same shape
    ``fragments/chat_exchange.html`` already renders — a list because every
    other responder's own turn is (contract §7 draws this as an ordinary chat
    screen), even though this kind never answers with more than one.
    """
    owner = Owner.profile(user.id)
    correlation_id = str(uuid4())
    asked_at = datetime.now(UTC)
    await _write_row(
        ctx,
        correlation_id=correlation_id,
        owner=owner,
        role=MessageRole.USER,
        content=message,
        author=Author(name=user.id, kind=AuthorKind.HUMAN),
        timestamp=asked_at,
    )

    settings = _settings(ctx)
    if not settings.is_configured():
        await conversations.append(conversation, since=since)
        return [_refused(message, IMAGE_NOT_CONFIGURED)]

    client = ImageClient(settings)
    try:
        image: GeneratedImage = await client.generate(message)
    except ImageUnavailable as exc:
        await conversations.append(conversation, since=since)
        return [_refused(message, _unavailable_sentence(exc))]
    except ImageRefused as exc:
        await conversations.append(conversation, since=since)
        return [_refused(message, _refused_sentence(exc))]

    try:
        record = await put_attachment(
            ctx.layout,
            ctx.audit,
            image.data,
            original_name=f"generated{_extension_for(image.media_type)}",
            media_type=image.media_type,
            owner=owner,
            correlation_id=correlation_id,
            conversation_id=conversation.conversation_id,
            now=asked_at,
        )
    except (AttachmentRejected, AttachmentStoreFailed) as exc:
        await conversations.append(conversation, since=since)
        return [_refused(message, f"The picture could not be saved: {exc}")]

    chip = chip_for(record)
    answered_at = datetime.now(UTC)
    await _write_row(
        ctx,
        correlation_id=correlation_id,
        owner=owner,
        role=MessageRole.ASSISTANT,
        content=chip.url,
        author=Author(name=IMAGE_RESPONDER_NAME, kind=AuthorKind.PERSONA, model=settings.model),
        timestamp=answered_at,
    )
    await conversations.append(conversation, since=since)

    return [
        {
            "message": message,
            "attachments": [],
            "attachment_notice": "",
            # Filled in by `chat_exchange._attributed_all`, from the very rows
            # just written above — exactly how a text turn's own
            # `chat_reply.chat_exchange()` leaves these two for the same
            # function to fill, rather than naming them twice.
            "author": "",
            "reply_author": "",
            "reply_model": "",
            "ok": True,
            "reply": chip.url,
            "reply_html": Markup(""),
            "reply_image_url": chip.url,
            "reasoning": "",
            "error": None,
            "persona": PERSONA_UNRECORDED,
            "tools_offered": None,
            "tools_called": [],
            "tool_calls": [],
            "duration": "",
            "first_token": "",
            "token_rate": "",
            "first_audio": "",
            "token_usage": "",
            "audio_url": None,
            "audio_report_url": None,
            "voice_note": None,
            "replayed": False,
        }
    ]


def issued_here(content: str) -> str | None:
    """``content`` if it is an attachment URL **this core itself issued**, and
    ``None`` for anything else at all.

    The whole of what may become an ``<img src>`` and an ``<a href download>``
    on the chat screen (``fragments/chat_exchange_body.html``). The kind of
    the conversation a row sits in is not enough on its own: any assistant row
    in an image conversation would otherwise be a URL the browser fetches on
    load and a link that runs on click — and rows written by the language
    model, before the streamed path knew this kind existed at all, exist
    exactly like that.

    Two halves, both exact and neither a substring test: the serve prefix this
    core mints (:data:`~personacore.web.screens.chat_attachments.
    ATTACHMENT_URL_PREFIX`) and, after it, nothing but an attachment id of the
    shape :data:`personacore.attachments.ATTACHMENT_ID_PATTERN` fixes — the
    same pattern the store validates an id with before it will read a byte
    off disk. A scheme check would pass ``javascript:``-in-a-query and a
    substring check would pass ``https://elsewhere.invalid/admin/chat/
    attachments/x``; this passes neither, because it describes the only string
    this module ever writes rather than describing what is forbidden.
    """
    if not content.startswith(ATTACHMENT_URL_PREFIX):
        return None
    attachment_id = content[len(ATTACHMENT_URL_PREFIX) :]
    return content if ATTACHMENT_ID_PATTERN.fullmatch(attachment_id) else None


def hydrate_replayed_images(built: Sequence[dict[str, Any]]) -> None:
    """Turn a replayed image-conversation reply's own row content back into
    the tile a fresh reply renders — the read side of the convention
    :func:`answer` writes (see the module docstring): every assistant row in
    an image conversation holds nothing but its own picture's serve URL.

    Called by ``chat.py`` only once the conversation being rendered is
    already known to have ``kind == ConversationKind.IMAGE`` — and that is
    still not enough. Being in an image conversation says only where a row
    is, never who wrote it: :func:`issued_here` is what says the content is a
    URL this core minted. A row that is not one is left exactly as the
    ordinary replay built it and prints as text, escaped, through the same
    path every other reply takes.
    """
    for entry in built:
        if not entry.get("ok"):
            continue
        url = issued_here(str(entry.get("reply") or ""))
        if url is None:
            continue
        entry["reply_image_url"] = url
        entry["reply_html"] = Markup("")


def _unavailable_sentence(exc: ImageUnavailable) -> str:
    detail = str(exc).strip()
    return f"PersonaCore can't reach the image generator: {detail}" if detail else (
        "PersonaCore can't reach the image generator."
    )


def _refused_sentence(exc: ImageRefused) -> str:
    detail = str(exc).strip()
    return f"The image generator refused that request: {detail}" if detail else (
        "The image generator refused that request."
    )


_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _extension_for(media_type: str) -> str:
    return _EXTENSIONS.get(media_type, "")


__all__ = [
    "IMAGE_NOT_CONFIGURED",
    "IMAGE_RESPONDER_NAME",
    "answer",
    "hydrate_replayed_images",
    "issued_here",
]
