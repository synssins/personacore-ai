"""Storage and lifecycle for one attachment — docs/contracts/attachments.md.

This is the whole of the contract's "your half": where the bytes go, the link
to the message that carries them, and what happens to both when the message
they belong to is gone. **No UI, no composer, no prompt assembly** — those are
somebody else's screen and somebody else's prompt-builder, built against the
:class:`~personacore.audit.models.AttachmentRecord` fields the contract fixes
and this module returns.

Three operations, and each is small on purpose:

* :func:`put` — stream bytes to disk under a random id, refusing the ceiling
  and the media-type allowlist before anything is kept, then record the row.
* :func:`get` — this owner's attachment, or ``None`` for "not theirs" and
  "not there" alike (contract §3).
* :func:`delete` and :func:`purge_orphaned` — row and file together, in the
  order the module docstring below argues for.

**The id is random, never a content hash.** A hash is an equality oracle: two
household members who attach the same file would land on the same id, and one
could learn what the other had sent by guessing (contract §3). It is minted
here with :func:`secrets.token_hex`, the same family of call the rest of this
project uses for a value that must not be guessable
(:func:`personacore.plugins.packages._make_staging`,
:mod:`personacore.web.screens.chat_audio`'s reply handles).

**The id is validated before it is ever joined to a path.** Contract §3 names
the prior art directly: :func:`personacore.plugins.voice_packages.
require_voice_id` and the ``_refuse_path_shaped`` check behind it, because
``Path.__truediv__`` does not join an absolute path — it replaces everything
to its left with it, and this project has been bitten by exactly that once
already. The rule is not re-imported wholesale, because that function raises
a voice-specific exception for a voice-specific charset; what is reused is its
source of truth, :mod:`personacore.plugins.discovery`'s own raw-path
predicates, so "unsafe path string" still has exactly one definition across
the whole codebase rather than a second copy quietly drifting beside it.

**Row and file, and which order.** Contract §7: "a purge that fails to delete
a file must not leave the row" — decided here as *the file is attempted
first, and the row is removed regardless of whether that succeeded*. An
orphaned row (pointing at a file this store no longer has) is a broken link:
:meth:`get` would hand back metadata for bytes that do not exist, breaking the
"read back byte-identical" promise for every future request. An orphaned file
(one this store failed to remove but no longer has a row for) is inert and
unreachable through this module — nothing joins an id to a path unless a row
says that id exists — and it is logged, so it is not invisible, only
undeleted. Between a promise this module cannot keep and a mess it can at
least point at, the mess wins.

**Ageing out.** Contract §7, and the owner's rule (2026-09-02): an attachment
is deleted when its conversation is deleted, and hidden until then, aged out,
or removed by an admin. Hiding a
conversation (:meth:`~personacore.audit.store_rooms.RoomsMixin.
hide_conversation`) only sets a timestamp and touches no transcript row, so an
attachment's message is still there and :func:`purge_orphaned` finds nothing
to remove. Only once the message itself is gone — aged out by
:meth:`~personacore.audit.store_retention.RetentionMixin.purge_older_than`, or
removed outright by :meth:`~personacore.audit.store_conversations.
ConversationsMixin.delete_conversation` — does the attachment become an
orphan. See :meth:`~personacore.audit.store_attachments.AttachmentsMixin.
orphaned_attachments` for why that is read off the message rather than tracked
a second time on this table.
"""

from __future__ import annotations

import asyncio
import io
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import IO, TYPE_CHECKING, Protocol

from personacore.audit.logging import get_logger
from personacore.audit.models import AttachmentRecord, Owner
from personacore.config.appdata import AppdataError, AppdataLayout
from personacore.plugins.discovery import (
    _has_raw_drive_prefix,
    _has_raw_traversal_segment,
    _is_absolute_path_string,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Limits and the allowlist — contract §6, §9
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
"""Contract §9, the owner's number: the *storage* ceiling. Not the send ceiling —
that is a different, smaller limit that depends on what a model host actually
accepts and is explicitly not this module's to invent (§9 says to ask)."""

MEDIA_TYPE_ALLOWLIST = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
    }
)
"""What this core will store, chosen by the core — never taken from what an
uploader claims (contract §6). Images and plain text are what the first
version needs (contract §1); **never ``text/html``** — contract §6 names the
reason and it is a real attack on this surface: a stored file served back as
HTML on the admin origin is stored XSS against a screen that shows one
household member's private conversations to another.
"""

assert "text/html" not in MEDIA_TYPE_ALLOWLIST  # noqa: S101 - the one rule this module must never regress

_PAYLOAD_FILENAME = "content"
"""The file's name inside its own directory — chosen by the core, not derived
from what the upload was called. Contract §6: the served filename and
``Content-Disposition`` are built from the validated id, never from
``original_name``, so nothing about the name a person typed ever reaches a
path or a header."""

_CHUNK_BYTES = 64 * 1024
"""Read size while streaming to disk — the same figure
:func:`personacore.plugins.packages._extract_safely` reads a zip member with,
kept here for the same reason: small enough that the ceiling check below runs
often, large enough that it is not the bottleneck."""

ATTACHMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
"""What :func:`secrets.token_hex(16)` produces, and nothing else — the same
"an allowlist of exactly what we mint" approach
:data:`personacore.web.screens.chat_audio.HANDLE_PATTERN` takes for a
reply handle. No slash, backslash, drive letter, NUL or ``..`` segment can
appear in 32 lowercase hex characters, so this alone already refuses every
path-shaped string; the checks in :func:`_require_attachment_id` below are
belt on top of braces, reusing discovery's predicates rather than trusting the
regex alone (contract §3)."""


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class AttachmentError(RuntimeError):
    """Base for everything that can go wrong storing or retrieving one."""


class AttachmentRejected(AttachmentError):
    """The attachment itself is refused. A sentence naming the problem;
    nothing was stored."""


class AttachmentTooLarge(AttachmentRejected):
    """Over :data:`MAX_ATTACHMENT_BYTES`. Raised as soon as the ceiling is
    crossed while streaming, never after the whole upload has landed."""


class AttachmentTypeRefused(AttachmentRejected):
    """Not in :data:`MEDIA_TYPE_ALLOWLIST`."""


class AttachmentInvalid(AttachmentRejected):
    """An id that is not shaped like one this core minted."""


class AttachmentStoreFailed(AttachmentError):
    """The attachment was fine; the filesystem or the database was not."""


NOT_FOUND = "That is not an attachment this core can find."
"""One sentence for every reason :func:`get` or :func:`delete` might have
nothing to show — a malformed id, somebody else's id, or one that was already
removed. Deliberately one sentence for all of them, the same rule
:data:`personacore.web.screens.chat_audio.REPLAY_GONE` keeps: telling
"not yours" apart from "not there" from outside is a way to go looking for
somebody else's attachments by id."""


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - the loop always returns


def _require_attachment_id(raw: str) -> str:
    """An attachment id, refused rather than repaired — checked before it is
    ever joined onto a path (contract §3).

    The prior art the contract names is
    :func:`personacore.plugins.voice_packages.require_voice_id` and
    ``_refuse_path_shaped`` behind it. That function is not called directly:
    it raises a voice-specific exception for a voice-specific charset, and
    importing it here would leave an attachment refused with a sentence about
    voice packs. What is reused instead is *its* source of truth —
    :mod:`personacore.plugins.discovery`'s three raw-path predicates — so
    "unsafe path string" still has exactly one definition in this codebase.

    :data:`ATTACHMENT_ID_PATTERN` alone already refuses every one of these
    shapes; the explicit checks are kept anyway; a regex that could be
    quietly loosened one day should not become the last line of defence
    against a repeat of the exact failure this project has already had.
    """
    value = (raw or "").strip()
    if (
        not value
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or value in (".", "..")
        or _is_absolute_path_string(value)
        or _has_raw_drive_prefix(value)
        or _has_raw_traversal_segment(value)
        or not ATTACHMENT_ID_PATTERN.fullmatch(value)
    ):
        raise AttachmentInvalid(NOT_FOUND)
    return value


def _attachment_directory(layout: AppdataLayout, attachment_id: str) -> Path:
    """The folder one attachment owns, with containment proved rather than
    assumed — the same shape
    :func:`personacore.plugins.voice_packages._voice_directory` checks its
    own join with, and for the same reason: the id is already validated, so
    this is the belt that catches an appdata root that is itself a link into
    somewhere else.
    """
    checked = _require_attachment_id(attachment_id)
    candidate = layout.attachments / checked
    try:
        layout.require_inside(candidate.parent, what="The attachments folder")
    except AppdataError as exc:
        raise AttachmentInvalid(str(exc)) from exc
    return candidate


def attachment_path(layout: AppdataLayout, record: AttachmentRecord) -> Path:
    """Where this attachment's bytes live on disk.

    For a caller that streams a response from a path (a ``FileResponse``, or
    anything else that does not want a 100 MB image ever wholly in memory).
    ``record`` is assumed already fetched through :func:`get`, so
    ``record.attachment_id`` came out of a row this store wrote rather than
    off a URL — the join-time check in :func:`_attachment_directory` still
    runs, because it is cheap and a single choke point is the whole point of
    having one.
    """
    return _attachment_directory(layout, record.attachment_id) / _PAYLOAD_FILENAME


def read_attachment_bytes(layout: AppdataLayout, record: AttachmentRecord) -> bytes:
    """The whole file, for a caller that has already decided the size is fine
    to hold in memory — a small text attachment, or a test proving a
    round-trip. A caller serving whatever the ceiling allows through (up to
    :data:`MAX_ATTACHMENT_BYTES`) should prefer :func:`attachment_path` and
    stream it instead.
    """
    return attachment_path(layout, record).read_bytes()


# ---------------------------------------------------------------------------
# The store side of this module's world — a narrow protocol, not a class
# ---------------------------------------------------------------------------


class AttachmentGateway(Protocol):
    """What this module needs of the audit store, and nothing else.

    A structural type rather than importing
    :class:`personacore.audit.store.AuditStore` directly, so a caller can hand
    this module a fake in a test without constructing a real sqlite database,
    and so this module's own dependency stays what it actually uses.
    :class:`~personacore.audit.store.AuditStore` satisfies this once
    :class:`~personacore.audit.store_attachments.AttachmentsMixin` is mixed
    in, which it is.
    """

    async def insert_attachment(self, record: AttachmentRecord) -> AttachmentRecord: ...

    async def get_attachment(
        self, attachment_id: str, *, owner: Owner
    ) -> AttachmentRecord | None: ...

    async def delete_attachment_row(self, attachment_id: str) -> AttachmentRecord | None: ...

    async def orphaned_attachments(self) -> list[AttachmentRecord]: ...


# ---------------------------------------------------------------------------
# Writing to disk — streamed, and refused before the ceiling is crossed
# ---------------------------------------------------------------------------


def _iter_chunks(source: IO[bytes] | Iterable[bytes]) -> Iterator[bytes]:
    """Bytes off ``source``, in pieces, whichever shape it arrived in.

    A file-like object (anything with ``.read``) is read in
    :data:`_CHUNK_BYTES` pieces, which is what keeps this module from ever
    holding a 100 MB upload whole before it has decided whether to keep it.
    An already-chunked iterable (bytes off a network stream, or a test's own
    generator) is passed through as given — re-chunking it would not make it
    any smaller, only slower to read.
    """
    reader = getattr(source, "read", None)
    if reader is not None:
        while True:
            chunk = reader(_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk
    else:
        yield from source  # type: ignore[misc]


def _write_stream_to_disk(
    source: IO[bytes] | Iterable[bytes], destination: Path, limit: int
) -> int:
    """Write ``source`` to ``destination``, refusing as soon as ``limit`` is
    passed rather than after the whole thing has landed (contract §9).

    **Never holds the upload whole in memory to measure it.** Bytes are
    counted as they arrive and written as they are counted, the same shape
    :func:`personacore.plugins.packages._extract_safely` polices its own
    uncompressed-size ceiling with — read a piece, add its length, check,
    write, repeat. A source that never runs out (an attacker's endless
    stream) is stopped after the first piece over the limit, not after it
    has exhausted itself.

    The partial file is removed on every failure path, refusal or I/O error
    alike: a refused upload must not leave bytes on disk for something that
    was never accepted.
    """
    written = 0
    try:
        with destination.open("wb") as handle:
            for chunk in _iter_chunks(source):
                written += len(chunk)
                if written > limit:
                    raise AttachmentTooLarge(
                        f"That file is over the {_human_bytes(limit)} limit for an "
                        "attachment. Nothing was stored."
                    )
                handle.write(chunk)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise AttachmentStoreFailed(
            f"The attachment could not be written to {destination}: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and writable."
        ) from exc
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return written


# ---------------------------------------------------------------------------
# Put
# ---------------------------------------------------------------------------


async def put(
    layout: AppdataLayout,
    store: AttachmentGateway,
    data: bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes],
    *,
    original_name: str,
    media_type: str,
    owner: Owner,
    correlation_id: str,
    conversation_id: str | None = None,
    now: datetime | None = None,
) -> AttachmentRecord:
    """Store one attachment and record it. Returns the row.

    Order matters and mirrors :func:`personacore.plugins.voice_packages.
    install_voice`'s own reasoning: the type is checked before a byte is
    written, the bytes are streamed to a fresh id-named directory with the
    ceiling enforced as they land, and only once that succeeds does a row get
    written — a file with no row is unreachable through this module (nothing
    here joins an id to a path unless a row says that id exists) and is
    cleaned up rather than left behind on any failure past that point.
    """
    if media_type not in MEDIA_TYPE_ALLOWLIST:
        raise AttachmentTypeRefused(
            f"{media_type!r} is not a type this core stores as an attachment. "
            f"Allowed: {', '.join(sorted(MEDIA_TYPE_ALLOWLIST))}."
        )
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    moment = (now or datetime.now(UTC)).astimezone(UTC)

    attachment_id = token_hex(16)
    directory = _attachment_directory(layout, attachment_id)
    try:
        directory.mkdir(parents=True)
    except OSError as exc:
        raise AttachmentStoreFailed(
            f"The attachment folder {directory} could not be created: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and writable."
        ) from exc

    destination = directory / _PAYLOAD_FILENAME
    source: IO[bytes] | Iterable[bytes]
    if isinstance(data, bytes | bytearray | memoryview):
        source = io.BytesIO(bytes(data))
    else:
        source = data

    try:
        byte_size = await asyncio.to_thread(
            _write_stream_to_disk, source, destination, MAX_ATTACHMENT_BYTES
        )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    record = AttachmentRecord(
        attachment_id=attachment_id,
        correlation_id=correlation_id,
        conversation_id=conversation_id,
        owner=owner,
        media_type=media_type,
        byte_size=byte_size,
        original_name=original_name,
        created_at=moment,
    )
    try:
        stored = await store.insert_attachment(record)
    except BaseException:
        # Contract §3: "a file no row references is... garbage." A file that
        # landed on disk but never got a row is exactly that, and it is
        # removed rather than left as something nothing will ever find or
        # clean up again.
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return stored


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def get(
    store: AttachmentGateway, attachment_id: str, *, owner: Owner
) -> AttachmentRecord | None:
    """This owner's attachment, or ``None`` for "not theirs" and "not there"
    alike (contract §3).

    A malformed id answers ``None`` here too, without raising — the same
    treatment :data:`personacore.web.screens.chat_audio.HANDLE_PATTERN`
    gives a handle that does not match its shape, so a caller does not have
    to tell "shaped wrong" apart from "not found" either. Validated through
    :func:`_require_attachment_id`, the same check the path join uses, so a
    lookup and a fetch can never disagree about what counts as an id.
    """
    try:
        checked = _require_attachment_id(attachment_id)
    except AttachmentInvalid:
        return None
    return await store.get_attachment(checked, owner=owner)


# ---------------------------------------------------------------------------
# Delete — row and file together
# ---------------------------------------------------------------------------


def _remove_attachment_file(layout: AppdataLayout, record: AttachmentRecord) -> bool:
    """Best-effort removal of one attachment's directory. ``True`` if it is
    gone by the time this returns (including "was never there").

    Never raises. Contract §7: a purge that fails to delete a file must not
    leave the row, which only makes sense if this always returns rather than
    aborting the caller's attempt to remove the row afterwards. A failure is
    logged with the id, never the file's content or its ``original_name``.
    """
    directory = _attachment_directory(layout, record.attachment_id)
    try:
        if directory.exists():
            shutil.rmtree(directory)
        return not directory.exists()
    except OSError as exc:
        _logger.error(
            "attachment_file_delete_failed",
            attachment_id=record.attachment_id,
            error=str(exc),
        )
        return False


async def delete(
    layout: AppdataLayout, store: AttachmentGateway, attachment_id: str, *, owner: Owner
) -> bool:
    """Delete one of this owner's attachments — row and file together.

    Returns ``False`` for "not theirs" and "not there" alike, the same
    non-distinction :func:`get` makes, checked here first so nothing is
    removed on behalf of an id that was never this owner's to remove.

    **Order:** the file is attempted first; the row is removed after,
    regardless of whether the file actually went — see the module docstring
    for why an orphaned file is preferred over an orphaned row. A failed file
    removal is logged by :func:`_remove_attachment_file` and does not stop
    the row from going.
    """
    record = await get(store, attachment_id, owner=owner)
    if record is None:
        return False
    _remove_attachment_file(layout, record)
    await store.delete_attachment_row(record.attachment_id)
    return True


# ---------------------------------------------------------------------------
# The retention sweep — orphans found by the store, removed by this module
# ---------------------------------------------------------------------------


class AttachmentPurgeResult:
    """What one sweep removed, for the caller's own log line."""

    __slots__ = ("removed", "file_failures")

    def __init__(self, *, removed: int = 0, file_failures: int = 0) -> None:
        self.removed = removed
        self.file_failures = file_failures


async def purge_orphaned(layout: AppdataLayout, store: AttachmentGateway) -> AttachmentPurgeResult:
    """Remove every attachment whose message is already gone — row and file.

    Contract §7: "the purge that removes transcript rows removes the files
    those rows referenced." This is the piece that does that, and it is
    deliberately a second call rather than something
    :meth:`~personacore.audit.store_retention.RetentionMixin.
    purge_older_than` does on its own — that method knows a database path and
    nothing about the rest of appdata, so it can find an orphaned row
    (:meth:`~personacore.audit.store_attachments.AttachmentsMixin.
    orphaned_attachments`) but cannot reach the file that goes with it. This
    function is what has both.

    **Whoever schedules the retention purge must call this alongside it.**
    It is not wired into that timer by this change — see the module's own
    report for exactly where the one line belongs.

    Each orphan's file is attempted before its row, and the row is removed
    whether or not the file actually went, for the reason argued in the
    module docstring: an orphaned row would silently break the next attempt
    to read something this store no longer has.
    """
    orphans = await store.orphaned_attachments()
    removed = 0
    file_failures = 0
    for record in orphans:
        if not _remove_attachment_file(layout, record):
            file_failures += 1
        await store.delete_attachment_row(record.attachment_id)
        removed += 1
    if removed:
        _logger.info("attachments_purged", removed=removed, file_failures=file_failures)
    return AttachmentPurgeResult(removed=removed, file_failures=file_failures)


__all__ = [
    "ATTACHMENT_ID_PATTERN",
    "MAX_ATTACHMENT_BYTES",
    "MEDIA_TYPE_ALLOWLIST",
    "NOT_FOUND",
    "AttachmentError",
    "AttachmentGateway",
    "AttachmentInvalid",
    "AttachmentPurgeResult",
    "AttachmentRejected",
    "AttachmentStoreFailed",
    "AttachmentTooLarge",
    "AttachmentTypeRefused",
    "attachment_path",
    "delete",
    "get",
    "put",
    "purge_orphaned",
    "read_attachment_bytes",
]
