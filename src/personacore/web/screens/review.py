"""Per-user conversation review — the chat-room contract, section 7.2.

The owner's requirement: chats are logged for admin review and simply hidden
from user view, so that a minor's activity remains reviewable; and per-user
audit access, so an admin can select a user and review that user's recent
chats up to the Purge window.

**Why this is its own screen and not part of the trace view.** The contract
offered ``/admin/logs`` as a possible home, and it is the wrong shape for this
question in three ways that would each have needed the log view rewritten:

* The log view's unit is an *exchange*, grouped by correlation id — one
  question and one answer. The unit here is a *conversation*, which is the
  thing that gets hidden, named and grouped, and the only thing ``hidden_at``
  is a property of. There is nowhere on a log row to put it.
* The log view drops every group with no transcript message, deliberately.
  A conversation somebody hid and never spoke in again would vanish from
  exactly the review it exists for.
* The log view has no notion of *whose* records these are; it filters by
  surface. Adding a person filter to it would make it two screens sharing one
  template, and the second one would be this one.

So the trace view stays what it is — everything that happened, in time order —
and this answers a different question: *what has this person been talking
about.* They read the same store and neither is a second implementation of the
other.

**The router is what makes it admin-only.** This module itself neither hides,
un-hides nor renames. It is registered on the admin UI router, which refuses
anybody who is not an admin on every path outside a written allowlist
(ADR-0032), and ``/admin/review`` is deliberately not on that list. There is no
second check in this module, for the reason ADR-0020 gives: a screen that
guards itself is a guard that can drift out of step with the real one.

**Delete lives beside this, not in it.**
:mod:`personacore.web.screens.review_delete` adds the one control on this
screen that changes anything — a full destroy of a conversation, its
attachments and its workspace files (workspace contract §2), never the
owner's own hide. It is a separate module for the same reason
:mod:`personacore.web.screens.persona_delete` is separate from
:mod:`personacore.web.screens.personas`, and it inherits this router's same
default-deny rather than adding a second one.

A non-admin is therefore refused *before* the handler runs, which is also what
makes a real account and an invented one indistinguishable to them: both are
the same 403, decided without the store being asked anything at all. For an
admin, a name with no conversations and a name with no account render the same
empty list — this screen never confirms or denies that an account exists,
because it does not have to in order to do its job.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from personacore.audit.logging import get_logger
from personacore.audit.models import DEFAULT_RETENTION_DAYS, MessageRole, Owner, Surface
from personacore.auth.accounts import AccountRejected, normalise_name
from personacore.web.screens import review_workspace
from personacore.web.shared import UIContext

logger = get_logger(__name__)

REVIEW_PATH = "/admin/review"
"""Where the screen lives, for links from the accounts screen."""

REVIEW_ROUTE = "/review"
"""The route as declared, relative to the router's own ``/admin`` prefix."""

CONVERSATIONS_SHOWN = 200
"""How many of one person's conversations the screen lists.

A ceiling on the query rather than a page size: the retention window already
bounds how far back this can go, and paginating a list that a purge keeps
short would be machinery with nothing to do.
"""

MESSAGES_SHOWN = 500
"""How many messages one conversation shows when it is opened.

Enough that a real thread is read whole rather than sampled. The read is
capped at all because a single unbounded ``SELECT`` on a store somebody else
is writing to is how one screen stalls the event loop for everybody.
"""

NO_ACCOUNTS = "There are no accounts on this core, so there is nobody to pick."
"""Said where the account store is empty, or where there is none at all.

Both are the same sentence because both are the same situation for the person
reading it. Under a login proxy the names live in the proxy, not here.
"""

NOBODY_PICKED = "Pick somebody to see their conversations."

NOTHING_TO_SHOW = "No conversations inside the retention window."
"""What an empty list says.

Deliberately not "this person has not used the assistant": records age out, so
an empty list means the window is empty and nothing more than that.
"""

HIDDEN_UNAVAILABLE = (
    "This store cannot report hidden conversations, so only visible ones are "
    "listed."
)
"""Said when the store predates hidden conversations.

The screen degrades rather than showing a list that silently omits exactly the
rows an administrator opened it to find. An omission nobody is told about is
the worst outcome available here.
"""

_SURFACE_WORD: dict[Surface, str] = {
    Surface.VOICE: "voice",
    Surface.API: "api",
    Surface.ADMIN_UI: "admin",
    Surface.ANONYMOUS: "anonymous",
    Surface.SYSTEM: "system",
}
"""The log view's own vocabulary, so one surface is called one thing across the
interface."""

_ROLE_WORD: dict[str, str] = {
    MessageRole.USER.value: "asked",
    MessageRole.ASSISTANT.value: "replied",
    MessageRole.SYSTEM.value: "system",
}
"""The log view's words for who spoke, for the same reason."""


def _when(moment: datetime | None) -> str:
    """One timestamp, as the log view writes them: UTC, as stored.

    ``""`` for a missing one rather than a placeholder — a blank cell reads as
    "not recorded", which is what it is.
    """
    return "" if moment is None else moment.strftime("%d %b %Y %H:%M")


def store_reports_hidden(store: object) -> bool:
    """Whether this store's conversation listing can include hidden rows.

    Asked of the object rather than assumed, the same way
    :class:`~personacore.conversations.service.ConversationService` asks
    whether a store knows about conversations at all. The admin UI is handed an
    ``AuditGateway`` — a structural protocol several test doubles also satisfy
    — and some of those know nothing about any of this. Calling with a keyword
    the object does not take would raise, be caught, and produce an empty list
    that looks exactly like "there is nothing to review".
    """
    listing = getattr(store, "list_conversations", None)
    if listing is None:
        return False
    try:
        return "include_hidden" in inspect.signature(listing).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False


def retention_days(request: Request) -> int:
    """The default retention window, right now.

    Read from ``app.state.settings`` per request rather than captured when the
    routes were registered, because saving core settings replaces that object
    — a captured copy would keep naming the window the container booted with.
    The *default* and not the per-surface overrides: this list spans surfaces,
    and one number that is named as the default is honest where a single number
    presented as the answer would not be.
    """
    settings = getattr(request.app.state, "settings", None)
    retention = getattr(settings, "retention", None)
    days = getattr(retention, "default_days", None)
    return days if isinstance(days, int) and days > 0 else DEFAULT_RETENTION_DAYS


def conversation_rows(
    conversations: list[Any], *, active: str | None = None
) -> list[dict[str, Any]]:
    """One person's conversations, as the template renders them.

    ``hidden_at``, ``hidden_by`` and ``group_name`` are read with ``getattr``
    because they arrive with the migration in the same batch as this screen and
    this module must not fall over on a store that has not had it yet — the
    same reason :data:`HIDDEN_UNAVAILABLE` exists. When they are there they are
    shown; when they are not, a conversation is simply not marked hidden, which
    is true of every row such a store can return.
    """
    rows: list[dict[str, Any]] = []
    for conversation in conversations:
        hidden_at = getattr(conversation, "hidden_at", None)
        rows.append(
            {
                "id": conversation.conversation_id,
                "title": conversation.title,
                "surface": _SURFACE_WORD.get(
                    conversation.surface, conversation.surface.value
                ),
                "started": _when(conversation.started_at),
                "last": _when(conversation.last_activity_at),
                "messages": conversation.message_count,
                "group": getattr(conversation, "group_name", None),
                "hidden": hidden_at is not None,
                "hidden_at": _when(hidden_at),
                "hidden_by": getattr(conversation, "hidden_by", None),
                "open": conversation.conversation_id == active,
            }
        )
    return rows


def message_rows(records: list[Any]) -> list[dict[str, Any]]:
    """One conversation's messages, oldest first — reading order.

    The store answers newest first, which is right for a rail of threads and
    wrong for reading one.
    """
    return [
        {
            "at": _when(record.timestamp),
            "who": _ROLE_WORD.get(record.role.value, record.role.value),
            "text": record.content,
        }
        for record in reversed(records)
    ]


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the review screen."""
    audit = ctx.audit
    shows_hidden = store_reports_hidden(audit)

    def _people() -> list[Any] | None:
        """Every account, or ``None`` where there is no account store at all.

        The template gives ``None`` and an empty list the same sentence, which
        is the opposite of what the accounts screen does with the same two
        values — deliberately. There, the reader may be a non-admin and an
        empty list would be a claim about the household. Here the reader is
        always an admin, and "there is nobody to pick" is true either way.
        """
        auth = ctx.auth_context
        return None if auth is None else auth.user_views()

    async def _conversations(owner: Owner) -> list[Any]:
        """This person's conversations, hidden ones included where the store
        can say so.

        Every surface, not just the admin UI's: the question is what somebody
        has been talking about, and a voice thread left out of that is the
        review missing the half a household actually speaks through.

        Failure is an empty list and a log line, following
        ``ConversationService``'s rule: an unreadable store must not take a
        screen down. Ids and counts only — never a title, never a message.
        """
        listing = getattr(audit, "list_conversations", None)
        if listing is None:
            return []
        extra = {"include_hidden": True} if shows_hidden else {}
        try:
            return await listing(
                owner=owner, surface=None, limit=CONVERSATIONS_SHOWN, **extra
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring above
            logger.error("review_list_failed", owner_id=owner.id, error=repr(exc))
            return []

    async def _messages(owner: Owner, conversation_id: str) -> list[Any]:
        """One conversation's messages. Same failure floor as above.

        The store's own owner check is what keeps this honest: the id arrives
        in a query string, so asking for it against the *picked* owner is what
        stops a guessed id reading somebody else's thread.
        """
        read = getattr(audit, "read_conversation", None)
        if read is None:
            return []
        try:
            return await read(conversation_id, owner=owner, limit=MESSAGES_SHOWN)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "review_read_failed",
                owner_id=owner.id,
                conversation_id=conversation_id,
                error=repr(exc),
            )
            return []

    @router.get(
        REVIEW_ROUTE, response_class=HTMLResponse, summary="Review one person's chats"
    )
    async def review_page(
        request: Request, user: str = "", conversation: str = ""
    ) -> HTMLResponse:
        """Pick somebody, see their conversations, open one.

        A name that is not a usable account name is treated as nobody picked,
        rather than refused: the picker cannot produce one, so anything else
        arrived by hand, and an error message would be the only difference
        between a real name and an invented one that this screen offers.
        """
        people = _people()
        try:
            chosen = normalise_name(user) if user else None
        except AccountRejected:
            chosen = None

        # Whether the person being reviewed is marked a minor. Read off the
        # listing already in hand rather than fetched again, and `None` where
        # there is no account store to ask — three states, because "not marked"
        # and "there is nobody here to have marked" are different facts.
        chosen_minor: bool | None = None
        if people is not None and chosen is not None:
            chosen_minor = any(
                person.username == chosen and person.is_minor for person in people
            )

        rows: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        opened: dict[str, Any] | None = None
        if chosen is not None:
            owner = Owner.profile(chosen)
            rows = conversation_rows(
                await _conversations(owner), active=conversation or None
            )
            opened = next((row for row in rows if row["open"]), None)
            if opened is not None:
                messages = message_rows(await _messages(owner, opened["id"]))
                # Workspace contract §7's admin half: the same cards the
                # owner's own Chat screen draws, linked into this screen's
                # own admin-only download route instead of theirs.
                opened["workspace_files"] = review_workspace.workspace_files_for(
                    request, ctx.layout, opened["id"], chosen
                )

        return ctx.templates.TemplateResponse(
            request=request,
            name="review.html",
            context={
                **await ctx.shell(request, "review"),
                "people": people,
                "chosen": chosen,
                "chosen_minor": chosen_minor,
                "rows": rows,
                "opened": opened,
                "messages": messages,
                "retention_days": retention_days(request),
                "shows_hidden": shows_hidden,
                "no_accounts": NO_ACCOUNTS,
                "nobody_picked": NOBODY_PICKED,
                "nothing_to_show": NOTHING_TO_SHOW,
                "hidden_unavailable": HIDDEN_UNAVAILABLE,
            },
        )


__all__ = [
    "CONVERSATIONS_SHOWN",
    "HIDDEN_UNAVAILABLE",
    "MESSAGES_SHOWN",
    "NOBODY_PICKED",
    "NOTHING_TO_SHOW",
    "NO_ACCOUNTS",
    "REVIEW_PATH",
    "REVIEW_ROUTE",
    "conversation_rows",
    "message_rows",
    "register",
    "retention_days",
    "store_reports_hidden",
]
