"""A conversation is a room, and these are its controls.

Everything the chat-room contract's §5 and §6 put *around* the messages: what
the thread is called and what it is filed under, who else is in it, who answers
in it, whether it reads itself aloud, folding the list of threads away, saving
it as a zip, and taking it off the owner's screen.

They are together because they are one errand — somebody tidying, staffing or
clearing a conversation — and because they share the same three habits, which
are easier to keep in one file than to remember in six:

* **A control a store cannot honour is absent, not disabled.** A button that
  silently does nothing is worse than one that is not there, so a store that
  cannot name, file, hide or staff a conversation says so in a sentence.
* **Nothing reports an outcome it did not check.** The delete used to say
  "Conversation deleted." either way; see :data:`CONVERSATION_NOT_DELETED`.
* **Hiding tells the owner one line and no more** (§6). A hidden conversation is
  off the rail, out of the messages, out of the download, and its URL answers
  exactly as an address that never named anything.

**It deliberately does not run a turn.** Saying something is
:mod:`personacore.web.screens.chat_exchange`'s and
:mod:`personacore.web.screens.chat_streaming`'s; the zip itself is written
by :mod:`personacore.web.screens.chat_save`, which this only calls. The
whole page comes back from the controls that change who answers, because
redrawing one field would hide the size of what just happened.

Split out of ``chat.py`` unchanged (ADR-0040). The screen still registers these
routes, and every name below is still importable from that module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from personacore.admin.models import AdminUser
from personacore.agent.errors import PersonaError
from personacore.agent.personas import PersonaStore
from personacore.audit.models import Owner
from personacore.conversations.models import (
    MAX_ROSTER,
    MAX_TITLE_LENGTH,
    UNTITLED,
    Conversation,
)
from personacore.web.screens import chat_streaming as streaming
from personacore.web.screens import chat_voices as voices
from personacore.web.screens.chat_audio import speaker
from personacore.web.screens.chat_save import (
    conversation_zip,
    save_filename,
    saved_messages,
)
from personacore.web.screens.chat_thread import (
    ASSISTANT_UNATTRIBUTED,
    _my_name,
    author_label,
    conversation_start,
    participants,
    thread_records,
    wanted_conversation,
)
from personacore.web.screens.personas import persona_voice_label
from personacore.web.shared import (
    RAIL_COLLAPSED_PREFERENCE,
    group_collapsed_preference,
    safe_admin_next,
    wants_collapsed,
)

if TYPE_CHECKING:  # pragma: no cover - the screen builds this and hands it over
    from personacore.web.screens.chat import ChatView


PERSONA_UNCHANGED = "That is already the persona in this conversation."

PERSONA_SCOPE_NOTE = "Who answers in this conversation. It stays with the thread."
"""Said next to the picker, because the old surprise has been removed and the
new promise is worth stating.

The picker used to change ``[core] default_persona``: there was no
per-conversation persona to select, so choosing a persona here made the core
answer as that persona for the OpenAI-compatible API, for an event waking the
agent, and for everybody else at the same time. The owner asked for the other
thing — a mild default for the house, and a distinct one for whoever is
actually talking to it — so the choice now belongs to the conversation
(``Conversation.persona``) and this handler writes no configuration at all.
"""

PERSONA_SAVED = "Answering as {name}."
"""Confirmation for a recorded choice, and nothing else.

It used to add "The core's default is unchanged", on the reasoning that the
control had the opposite scope until recently. That is a note about our
implementation history, said to somebody who picked a name from a list and
wants to know it took. The owner cut it. A confirmation that explains what it
did *not* do is defending itself."""

PERSONA_NOT_REMEMBERED = (
    "This core's log store cannot remember a persona per conversation, so the "
    "choice was not saved. The assistant answers as the core's default persona."
)
"""A store that knows about conversations but not about their persona — an
older database, or a core assembled with a different transcript store. Said
plainly rather than silently accepted: a picker that appears to save and then
answers as somebody else is worse than one that says it cannot."""

ROSTER_NOTE = "Everyone in the room reads and answers the others."
"""One sentence beside the roster, and it says the only thing the control
itself cannot: what changes when a second character is in the conversation.

Deliberately not "who else is in this room" — the chips already say that, and a
label repeating what is next to it is a line nobody reads."""

ROSTER_ADDED = "{name} joined this conversation."
ROSTER_REMOVED = "{name} left this conversation."
ROSTER_ALREADY = "{name} is already in this conversation."
ROSTER_FULL = (
    "This conversation already has as many personas as it can hold. Take one out first."
)
"""A room at :data:`~personacore.conversations.models.MAX_ROSTER`. The limit is
what the open-floor ask costs, not what the store can hold — see that constant
— and saying "take one out first" is the whole of what somebody needs to do."""

ROSTER_NOT_HELD = (
    "This core's log store cannot put more than one persona in a conversation, "
    "so that control is off."
)
"""A store that knows about conversations and personas but not about rosters —
an older database. The same treatment, for the same reason, as
:data:`PERSONA_NOT_REMEMBERED`: a control that appears to add somebody to the
room and then forgets is worse than one that is not there."""

STOP_LABEL = "Stop"
STOP_TITLE = "Stop the conversation at the end of this reply"
"""§5. Near the composer, **lit only while the room is active** — a button that
is always there and usually does nothing teaches people to ignore it — so the
markup carries it hidden and ``chat.js`` reveals it for as long as an exchange
with more than one persona in it is running.

It ends the exchange at the current turn's boundary: the reply being written
finishes and is recorded, nobody else is asked, and **the audio is cut** rather
than allowed to finish. That is the implementer's call §5 leaves open, taken
the way a stop button is expected to behave — pressing stop and then listening
to another twenty seconds of speech is not stopping. Partial replies already on
screen stay: they were said.
"""

SPEECH_ON_HERE = "Replies read themselves aloud in this conversation."
SPEECH_OFF_HERE = "Replies stay quiet in this conversation."
"""§6.2's switch, in the two states it can be in.

The owner asked for it in the same breath as the audio queue, as a way to
silence a conversation that has become distracting to have read aloud. It
silences **this conversation** and touches nothing else
— not the person's own autoplay setting (ADR-0030) and not any other thread.
The audio is still there to press and still there to download; it simply does
not start on its own.

Where the two disagree the narrower one wins: autoplay off for the person means
silence everywhere, whatever a conversation says, which is why nothing here can
turn speech back *on*.
"""

SPEECH_NOT_KEPT = (
    "This core cannot remember settings, so the voice cannot be switched off "
    "for one conversation."
)
"""No preference store — the same core that cannot remember autoplay either."""

CONVERSATION_DELETED = "Conversation deleted."
"""The whole of what the owner is told, and it is deliberately not the whole
truth (the chat-room contract, §6).

The button hides; nothing is destroyed, and an administrator can still read the
conversation until the retention purge takes it. The owner decided what this
says and why: a user does not need to know that an admin can still see a
deleted conversation for a retention period, the same way an ordinary
workplace audit log is not disclosed to the people it logs. So this line
carries no qualifier, no undo, and no hint that anything was kept — and
nothing else on the way out may add one, which is why a hidden conversation's
URL answers exactly as one that never existed.
"""

CONVERSATION_NOT_DELETED = "Nothing was deleted."
"""Said when the hide did not happen, and it is said instead of the line above.

**This exists because the screen used to say "Conversation deleted." either
way.** The owner reported this defect on v0.11.1: delete claimed to have
worked but the conversation still showed in the rail and could still be
participated in. The handler never looked at what it got back, so a
marker naming nothing, a conversation already hidden and a store that refused
all reported success. A screen must not report an outcome it did not check —
and a delete that reports success is the one failure a person cannot catch by
looking.

**One sentence for all three, with no reason attached**, and that is the §6
requirement rather than terseness for its own sake: an answer that told
"already hidden" apart from "never existed" would say that a conversation the
owner cleared is still in there, which is precisely what hiding is for.
"""

ROOM_NOT_MANAGED = (
    "This core's log store cannot name or file conversations, so those controls are off."
)
"""Said in place of the rename and group controls for a store that has the
conversations but not the room operations — the same treatment, for the same
reason, as :data:`PERSONA_NOT_REMEMBERED`: a control that appears to save and
then forgets is worse than one that is not there."""

NOTHING_TO_NAME = "There is nothing to name yet. Say something first."
"""A rename posted against a conversation that does not exist — the screen was
opened and nothing has been said in it. Nothing is created to hold the name:
minting a conversation to title it would leave a row nobody spoke in."""

NAME_REFUSED = "A conversation needs a name, so that one was not saved."
"""An empty title. The service refuses it too (a blank row is one nobody can
click on with any confidence); this is the sentence that goes with the
refusal."""

NAME_SAVED = "Renamed."
GROUP_SAVED = "Filed under {group}."
GROUP_CLEARED = "Taken out of its group."


# ---------------------------------------------------------------------------
# Personas, and the voice that rides on them
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaChoice:
    """One option in the picker: who answers, and what they sound like."""

    name: str
    label: str
    voice: str
    selected: bool
    description: str = ""
    """The persona's own one-line description, or ``""`` when it has none.

    In ``persona.toml`` since the beginning and shown nowhere until the
    composer's picker became a list of rows (``composer.md`` §5): a bold
    ``display_name`` with this underneath it in grey. Defaulted rather than
    required so nothing that builds one of these has to learn about it.
    """


def persona_choices(personas: PersonaStore, *, default: str) -> list[PersonaChoice]:
    """Every persona on disk, each with the voice it suggests.

    The voice is spelled by :func:`persona_voice_label`, imported from the
    Personas screen rather than re-implemented — ADR-0002 requires one spelling
    of a voice everywhere in this interface, and two functions producing
    ``GLaDOS (vits-onnx)`` is exactly how a surface ends up with two.

    A persona whose files cannot be read is skipped rather than crashing the
    screen: a picker missing one broken entry is usable, and the Personas
    screen is where a broken persona gets diagnosed.
    """
    choices: list[PersonaChoice] = []
    for name in personas.available():
        try:
            persona = personas.load(name)
        except Exception:  # noqa: BLE001, S112 - one bad persona must not take the page
            continue
        choices.append(
            PersonaChoice(
                name=name,
                label=persona.display_name or name,
                voice=persona_voice_label(persona.voice_engine, persona.voice_name),
                selected=name == default,
                # ``getattr`` rather than ``persona.description``: the tests in
                # this suite hand ``persona_choices`` a stand-in object with
                # only the fields it used to read, and a picker that raises on
                # a persona missing an optional line would be a worse control
                # than one that shows no line.
                description=str(getattr(persona, "description", "") or ""),
            )
        )
    return choices


# ---------------------------------------------------------------------------
# The parts that never needed a request
# ---------------------------------------------------------------------------


def _chosen_persona(conversation: Conversation | None) -> str | None:
    """Who this thread is being held with, or ``None`` for the core's
    default.

    Read from the conversation row and from nowhere else — never from the
    request, so a hand-edited form cannot name a persona that was not
    chosen through the picker, and never from ``core.toml``, so a thread
    that chose somebody keeps them when the default moves.
    """
    chosen = getattr(conversation, "persona", None)
    return str(chosen) if chosen else None


def _thinking_here(
    conversation: Conversation | None, answering: str, personas: PersonaStore
) -> bool:
    """Whether the Thinking checkbox reads checked — thinking contract §13 D.

    The conversation's own override wins when it has one; otherwise the
    answering persona's own switch. Own copy of
    :mod:`personacore.web.screens.chat`'s own version of this — the same
    "a screen module is meant to be read without following an import into
    another one" reasoning :func:`personacore.web.screens.chat_workspace.
    _workspace_ceilings` gives for its own small, duplicated helper.

    ``getattr`` both ways: ``thinking`` on
    :class:`~personacore.conversations.models.Conversation` and
    ``thinking_enabled`` on a loaded persona are the same core joint
    (``working/contracts/workspace.md`` §13) this screen builds against
    before the other half of the contract has necessarily landed either one
    — absent means "no override" for the first and "on" for the second,
    which is also what each already means once landed.
    """
    override = getattr(conversation, "thinking", None)
    if override is not None:
        return bool(override)
    try:
        loaded = personas.load(answering)
    except Exception:  # noqa: BLE001 - an unreadable persona still gets a checkbox
        return True
    return getattr(loaded, "thinking_enabled", True)


#: What a submitted ``thinking`` field spells "on" and "off" as — a plain
#: checkbox's own value (``value="1"``, per ``chat_thinking_switch.html``)
#: plus the words a hand-built request might reasonably send instead.
_THINKING_ON = frozenset({"1", "true", "on"})
_THINKING_OFF = frozenset({"0", "false", "off"})

THINKING_VALUE_INVALID = (
    "That is not a thinking value this screen understands. Nothing was changed."
)


def parse_thinking_field(raw: object) -> bool:
    """Whether the Thinking switch's POST said on or off, refused otherwise.

    A field entirely **absent** follows the checkbox convention every native
    checkbox on this form already uses (see ``memory``/``workspace`` on the
    persona edit screen): the browser only submits a ticked box's value at
    all, so no field means unticked, which means off. Anything present has
    to be one of :data:`_THINKING_ON` or :data:`_THINKING_OFF` — a shaped-
    wrong value is refused rather than silently read as off, which is what
    :func:`personacore.web.shared.wants_collapsed` would have done here and
    is exactly wrong for a control this contract treats as a real switch.
    """
    if raw is None:
        return False
    text = str(raw).strip().lower()
    if text in _THINKING_ON:
        return True
    if text in _THINKING_OFF:
        return False
    raise ValueError(THINKING_VALUE_INVALID)


# -- folding the rail away ---------------------------------------------
#
# Two toggles, both ordinary forms that save a preference and redirect back
# to the conversation that is open. No `next` field and nothing to validate
# as a destination: the only place either of them can land is Chat, and
# which thread is named by the same `conversation` marker every other
# control on this screen carries.
#
# Neither is audited, for the reason the sidebar's is not (`profile.py`):
# this changes nothing about the household.


def _back_to(request: Request, marker: str | None) -> str:
    """The conversation this toggle came from, as an address.

    The marker is echoed only if it parses as a conversation instant, so
    the redirect cannot be steered by a hand-edited field into carrying
    somebody's text back onto the screen.

    **A marker that does not parse used to answer ``/admin/chat``, and that is
    the worst outcome available**: bare ``/admin/chat`` is the "New
    conversation" address, so pressing a toggle would open an empty thread and
    look exactly like the application having lost the one you were in. Nothing
    about folding a list away is a request to start again.

    So the fallback is the page the button was pressed from — the surface's own
    rule for where a toggle comes back to (:func:`safe_admin_next`), which the
    sidebar's toggle already uses, checked the same way and refusing anywhere
    off this surface. It lands on Chat with nothing named only when there is
    genuinely nothing to go back to, which is the one case where a new
    conversation is the honest answer.
    """
    started = conversation_start(wanted_conversation(marker))
    if started is None:
        return safe_admin_next(request, None)
    return f"/admin/chat?c={quote(started.isoformat())}"


def register(router: APIRouter, view: ChatView) -> None:
    """Register the controls that name, staff, silence, save and clear a room."""
    ctx = view.ctx
    templates = ctx.templates
    personas = ctx.personas
    require_user = ctx.require_user
    conversations = view.conversations
    _screen = view.screen
    _visible = view.visible
    _looked_at = view.looked_at
    _labels = view.labels
    _members = view.members

    @router.get("/chat/save", summary="Download this conversation")
    async def chat_save(request: Request, c: str | None = None) -> Any:
        """One conversation as a zip: the transcript, and every spoken reply
        as audio, synthesised now (§5.2).

        Read through exactly the same path the screen is: this operator's own
        transcript, with hidden conversations already gone from it, cut to the
        thread ``?c=`` names. So a conversation the owner deleted downloads as
        an empty one, the same as an address that never named anything — a
        download is a route, and §6 says every route answers the same way.

        Streamed rather than assembled: see
        :mod:`personacore.web.screens.chat_save`.
        """
        user = require_user(request)
        started = conversation_start(wanted_conversation(c))
        opened = started or datetime.now(UTC)
        records, known = await _visible(user)
        rows = thread_records(records, opened)
        mine = _my_name(records, user)
        # Which persona each reply gets spoken in. Display name to persona
        # name, because the transcript records who answered by display name and
        # the voice is resolved from the persona's own file.
        named = {
            choice.label: choice.name
            for choice in persona_choices(personas, default=personas.default_persona)
        }
        thread = known.get(rows[0].conversation_id or "") if rows else None
        title = thread.title if thread and thread.title else UNTITLED
        messages = saved_messages(
            rows,
            names=named,
            persona=getattr(thread, "persona", None),
            author_of=author_label,
            human=mine,
            assistant=ASSISTANT_UNATTRIBUTED,
        )
        return StreamingResponse(
            conversation_zip(
                messages,
                title=title,
                people=participants(rows, human=mine),
                started=rows[0].timestamp if rows else opened,
                made=speaker(request),
            ),
            media_type="application/zip",
            headers={
                # The filename is built from characters `save_filename` chose,
                # so a conversation named with a quote or a newline cannot
                # split this header.
                "Content-Disposition": (
                    f'attachment; filename="{save_filename(title, opened)}"'
                ),
                "Cache-Control": "no-store",
            },
        )

    @router.post("/chat/hide", summary="Delete this conversation from your list")
    async def chat_hide(request: Request) -> RedirectResponse:
        """Take one conversation off this operator's screen. Nothing is
        destroyed (§6).

        **The screen's delete button calls this and no longer calls
        ``delete()``.** The owner's reasoning: chats are logged for
        administrator review and are only removed from the ordinary user's
        view, so that a household member's conversation history stays
        auditable. The service's ``delete()`` still exists and still
        destroys; it is an administrator's path and is not reachable from here.

        What the owner is told when it worked is :data:`CONVERSATION_DELETED`
        and nothing more — no qualifier, no undo, no mention of review or
        retention. Everything else on this screen keeps the same promise: the
        hidden conversation leaves the rail, its messages stop being read back,
        and its URL answers exactly as an address that never named anything.

        **When it did not work, it says so.** It used to say the same sentence
        either way, and the owner found the consequence on v0.11.1: delete
        claimed to have worked but the conversation still showed in the rail
        and could still be participated in. The hide had never run — the
        button was carrying the conversation that was open when the page
        loaded rather than the one the operator had switched to — and nothing
        here checked, so a pointing bug came out as a lie. A screen must not
        report an outcome it did not check.

        A marker naming nothing, one already hidden and a store that refused
        all land on :data:`CONVERSATION_NOT_DELETED`, which is one sentence for
        three outcomes on purpose: telling them apart would say that a
        conversation the owner cleared is still there, and that is the
        disclosure §6 exists to prevent.

        Nothing is created on the way (``create=False``): minting a
        conversation in order to hide it would be an absurd way to answer
        "there is nothing there".

        ``303`` so the browser follows with a ``GET`` and a refresh does not
        replay it.
        """
        user = require_user(request)
        form = await request.form()
        marker = str(form.get("conversation") or "") or None
        started = conversation_start(wanted_conversation(marker))
        owner = Owner.profile(user.id)
        found = None if started is None else await conversations.at(owner, started, create=False)
        hidden = found is not None and await conversations.hide(owner, found.conversation_id)
        return RedirectResponse(
            f"/admin/chat?deleted={'1' if hidden else '0'}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post(
        "/chat/room", response_class=HTMLResponse, summary="Name and file this conversation"
    )
    async def chat_room(request: Request) -> HTMLResponse:
        """Rename this conversation, and file it under a group (§5.5).

        One form and one button for the two, because they are one errand:
        somebody tidying a list does both in the same breath, and two buttons
        side by side would ask them which of the two things they meant.

        Both refusals are sentences rather than silences. A blank name is
        refused outright — a row nobody can click on with any confidence is the
        whole reason ``UNTITLED`` exists — and a conversation nobody has spoken
        in yet has nothing to name, so nothing is created to hold the name.

        A group typed as nothing clears it, which is what an emptied field
        means everywhere else on this surface.

        The whole page comes back, like the persona picker's: renaming changes
        the heading, the rail row and where the thread sits under its group, and
        redrawing one field would hide most of what just happened.
        """
        user = require_user(request)
        form = await request.form()
        marker = str(form.get("conversation") or "") or None
        opened = conversation_start(wanted_conversation(marker)) or datetime.now(UTC)
        title = str(form.get("title") or "")
        group = str(form.get("group") or "")

        note = await _rename_and_file(user, opened, title, group)
        return templates.TemplateResponse(
            request=request,
            name="chat.html",
            context=await _screen(request, wanted=opened.isoformat(), note=note),
        )

    async def _rename_and_file(
        user: AdminUser, opened: datetime, title: str, group: str
    ) -> dict[str, str]:
        """Do both writes and say what happened, in one sentence."""
        if not conversations.manages_rooms:
            return {"kind": "invalid", "message": ROOM_NOT_MANAGED}
        owner = Owner.profile(user.id)
        found = await conversations.at(owner, opened, create=False)
        if found is None:
            return {"kind": "invalid", "message": NOTHING_TO_NAME}
        if not title.strip():
            return {"kind": "invalid", "message": NAME_REFUSED}
        if not await conversations.rename(owner, found.conversation_id, title):
            return {"kind": "invalid", "message": NAME_REFUSED}

        wanted = group.strip()
        if (wanted or None) == (found.group_name or None):
            # The group did not change, so nothing is claimed about it. Saying
            # "filed under Home" to somebody who only retitled it would be the
            # screen narrating a write that did not happen.
            return {"kind": "saved", "message": NAME_SAVED}
        if not await conversations.regroup(owner, found.conversation_id, wanted or None):
            return {"kind": "saved", "message": NAME_SAVED}
        return {
            "kind": "saved",
            "message": GROUP_SAVED.format(group=wanted) if wanted else GROUP_CLEARED,
        }

    @router.post("/chat/rail", summary="Fold the conversation list away, or open it")
    async def chat_rail(request: Request) -> RedirectResponse:
        """Save whether the list of earlier conversations is a bare bar.

        Collapsed it carries **no icons** — the owner's instruction, and the
        right one: a conversation has no emblem, and one invented per row would be a
        column of identical marks that said nothing. So there is a bar and the
        button that opens it again, and nothing else.
        """
        user = require_user(request)
        form = await request.form()
        await asyncio.to_thread(
            ctx.preferences.set_bool,
            user.door,
            user.id,
            RAIL_COLLAPSED_PREFERENCE,
            wants_collapsed(form.get("collapsed")),
        )
        return RedirectResponse(
            _back_to(request, str(form.get("conversation") or "") or None),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post("/chat/group-view", summary="Fold one group of conversations away")
    async def chat_group_view(request: Request) -> RedirectResponse:
        """Save whether one group in the rail shows its conversations.

        The group is named by the label somebody typed, trimmed the same way
        ``ConversationService.regroup`` trims it before storing — so the name
        pressed here and the name stored there are the same string, and the
        preference key stays as bounded as the group is. An empty name is the
        ungrouped bucket, which has a key of its own.
        """
        user = require_user(request)
        form = await request.form()
        raw = str(form.get("group") or "").strip()[:MAX_TITLE_LENGTH].strip()
        await asyncio.to_thread(
            ctx.preferences.set_bool,
            user.door,
            user.id,
            group_collapsed_preference(raw or None),
            wants_collapsed(form.get("collapsed")),
        )
        return RedirectResponse(
            _back_to(request, str(form.get("conversation") or "") or None),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # -- who else is in the room -------------------------------------------

    @router.post(
        "/chat/roster", response_class=HTMLResponse, summary="Add or remove a persona"
    )
    async def chat_roster(request: Request) -> HTMLResponse:
        """Put another persona in this conversation, or take one out (§2).

        A conversation-level action from the composer bar, because that is
        where somebody talking is looking and where the picker beside it
        already lives. The personas in a room read each other's messages and
        answer each other — the owner's framing is the one built to: they deal
        with each other through the chat, as if they were also unique users in
        the chat room.

        **Taking somebody out does not take back what they said.** Their
        messages stay in the transcript — they said them — and they simply stop
        being asked to speak. There is nothing to undo and nothing to hide.

        The persona that *answers first* is not removed here: it is changed
        with the picker. That is why this control is "who else", and why the
        one-persona conversation is untouched by all of it — an empty roster is
        the state every conversation was already in.

        Three refusals, each a sentence:

        * **a persona that will not load** — checked at the click, through the
          same store the picker checks against, so a name that is not installed
          never reaches the room;
        * **a room that is already full** — see ``MAX_ROSTER``, which is a limit
          on what the open floor costs rather than on what the store can hold;
        * **a store that cannot remember a roster** — said plainly rather than
          appearing to add somebody who is gone on reload.

        The whole page comes back, like the picker's: adding a character
        changes who answers everything from here on, and redrawing one control
        would hide the size of what just happened.
        """
        user = require_user(request)
        form = await request.form()
        marker = str(form.get("conversation") or "") or None
        opened = conversation_start(wanted_conversation(marker)) or datetime.now(UTC)
        wanted = str(form.get("persona") or "").strip()
        leaving = str(form.get("action") or "").strip().lower() == "remove"

        note = await _change_roster(user, opened, wanted, leaving=leaving)
        return templates.TemplateResponse(
            request=request,
            name="chat.html",
            context=await _screen(request, wanted=opened.isoformat(), note=note),
        )

    async def _change_roster(
        user: AdminUser, opened: datetime, wanted: str, *, leaving: bool
    ) -> dict[str, str]:
        """Add ``wanted`` to this room or take it out, and say what happened."""
        if not conversations.holds_a_roster:
            return {"kind": "invalid", "message": ROSTER_NOT_HELD}
        if not wanted:
            return {"kind": "invalid", "message": "Pick a persona first."}

        labels = _labels()
        # ``create=True`` when somebody is joining, for the reason the picker
        # creates: choosing who is in the room before saying anything is
        # ordinary, and the conversation minted here starts at the instant the
        # composer already carries, so the first message lands in it. Nothing
        # is created to remove somebody from a room that does not exist.
        conversation = await conversations.at(
            Owner.profile(user.id), opened, create=not leaving
        )
        if conversation is None:
            return {"kind": "invalid", "message": NOTHING_TO_NAME}
        here = [member.name for member in _members(conversation, labels)]

        if leaving:
            if wanted not in here[1:]:
                # Not in the room, or the persona the picker names — which is
                # changed rather than removed. Either way nothing moves, and
                # saying so beats a save note over a write that did not happen.
                return {"kind": "none", "message": ROSTER_REMOVED.format(name=wanted)}
            rest = [name for name in here[1:] if name != wanted]
            if not await conversations.set_roster(conversation, rest):
                return {"kind": "invalid", "message": ROSTER_NOT_HELD}
            return {
                "kind": "saved",
                "message": ROSTER_REMOVED.format(name=labels.get(wanted, wanted)),
            }

        if wanted in here:
            return {
                "kind": "none",
                "message": ROSTER_ALREADY.format(name=labels.get(wanted, wanted)),
            }
        if len(here) >= MAX_ROSTER:
            return {"kind": "invalid", "message": ROSTER_FULL}
        try:
            persona = personas.load(wanted)
        except PersonaError as exc:
            # The store's own sentence, which already names the persona and the
            # reason (spec §9). Nothing is recorded and nobody is substituted.
            return {"kind": "invalid", "message": f"Not added: {exc.spoken_message}"}
        except Exception:  # noqa: BLE001 - an unreadable persona is not a dead screen
            return {
                "kind": "invalid",
                "message": f"Not added: I couldn't read the persona {wanted!r}.",
            }
        if not await conversations.set_roster(conversation, [*here[1:], wanted]):
            return {"kind": "invalid", "message": ROSTER_NOT_HELD}
        return {
            "kind": "saved",
            "message": ROSTER_ADDED.format(name=persona.display_name or wanted),
        }

    @router.post("/chat/stop", summary="Stop the personas talking")
    async def chat_stop(request: Request) -> Response:
        """Both stops, because there are two and they are not the same thing.

        ``token`` is **§5's stop the room**: end the exchange at the current
        turn's boundary. The reply being written finishes and is recorded —
        cutting it off mid-sentence would leave a half-sentence in the
        transcript that nobody said — and nobody else is asked. It exists only
        for a room, because it is a decision about who speaks *next*. **The
        audio is cut**, immediately, by the browser: that is the choice §5
        leaves to the implementer, taken the way a stop button is expected to
        behave, because pressing stop and then listening to another twenty
        seconds of speech is not stopping.

        ``conversation`` is **§4a's stop this reply**: halt the answer being
        written. That did not exist, and the reasoning that left it out has
        been overtaken — it said "a single persona answers once and there is no
        second turn for a stop to prevent", which was true when a reply took
        seconds. Replies now run as long as twenty minutes, the conversation
        that most needs a stop is the solo one with no button at all, and since a
        turn began outliving the browser that started it
        (``chat_streaming``, detached-turns contract §3) stopping is the *only*
        way to end one somebody has walked away from.

        Both may arrive together and both are honoured: pressing stop while a
        room is talking should end the sentence being written **and** not ask
        the next character. Neither is required — a solo conversation sends no
        token, and a browser too old to have been told about this sends no
        conversation.

        Answers with no content, always, whatever happened. An unknown token,
        somebody else's conversation and a turn that has already ended are one
        answer: which it was belongs in the log, not in the response, for the
        same reason a reply's audio does not distinguish them.

        There is no page to come back to because the page never left: the
        button is a fetch from ``chat.js``, and the stream it is stopping is
        still open and about to carry the last frame.
        """
        user = require_user(request)
        form = await request.form()
        voices.stop(request, str(form.get("token") or ""), user.id)
        streaming.stop_turn(request, str(form.get("conversation") or "") or None, user.id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/chat/speech", summary="Silence this conversation, or let it speak")
    async def chat_speech(request: Request) -> RedirectResponse:
        """§6.2 — stop replies reading themselves aloud in **this** thread.

        The owner asked for it in the same breath as the audio queue, as a way
        to silence a conversation that has become distracting to have read
        aloud. It touches nothing else — not this
        person's own autoplay setting (ADR-0030), not any other conversation —
        and the audio is still there to press and still there to download. It
        simply does not start on its own.

        Where the two disagree the narrower wins, so this can only ever
        silence: with autoplay off for the person there is nothing here to turn
        back on.

        An ordinary form that saves a preference and redirects back to the
        conversation, exactly like the rail's fold, and not audited for the
        same reason: it changes nothing about the household.
        """
        user = require_user(request)
        form = await request.form()
        marker = str(form.get("conversation") or "") or None
        started = conversation_start(wanted_conversation(marker))
        found = await _looked_at(user, started)
        key = voices.muted_preference(
            str(getattr(found, "conversation_id", "") or "")
        )
        if key is not None:
            await asyncio.to_thread(
                ctx.preferences.set_bool,
                user.door,
                user.id,
                key,
                wants_collapsed(form.get("muted")),
            )
        return RedirectResponse(
            _back_to(request, marker), status_code=status.HTTP_303_SEE_OTHER
        )

    _thinking_template = templates.get_template("fragments/chat_thinking_switch.html")

    @router.post(
        "/chat/thinking",
        response_class=HTMLResponse,
        summary="Whether this conversation thinks first",
    )
    async def chat_thinking(request: Request) -> Response:
        """Thinking contract §13 D — the header's own per-conversation override.

        Modelled on :func:`chat_speech` just above: an ordinary form that
        saves a preference and comes back to the conversation. It differs in
        one way — htmx's own request gets back just the refreshed switch
        (the same ``#chat-thinking`` id its ``hx-target`` names), not the
        whole page, because a checkbox tapped in a sheet that is still open
        should not redraw everything around it.

        Calls :meth:`personacore.conversations.service.ConversationService.
        set_thinking` — the core joint this screen builds against
        (``working/contracts/workspace.md`` §13) — which may not exist on a
        core that has not landed the other half of the contract yet; that is
        a plain ``AttributeError`` today, and this needs no change the day
        it lands.

        "Applies from the next reply": this turn's own request already left,
        if one is running, and the loop reads the setting fresh per round
        (contract §13's own turn rule), so there is nothing here to stop or
        restart.
        """
        user = require_user(request)
        form = await request.form()
        marker = str(form.get("conversation") or "") or None
        started = conversation_start(wanted_conversation(marker))
        owner = Owner.profile(user.id)
        found = await _looked_at(user, started)
        try:
            wanted = parse_thinking_field(form.get("thinking"))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if found is not None:
            await conversations.set_thinking(owner, found.conversation_id, wanted)
        if request.headers.get("hx-request", "").lower() == "true":
            answering = _chosen_persona(found) or personas.default_persona
            # `wanted` is what was just written, not a stale read of `found`
            # (fetched *before* the call above) — reading `found.thinking`
            # here would answer with the override this same request just
            # replaced, one post behind what is actually on disk.
            thinking_here = (
                wanted if found is not None else _thinking_here(found, answering, personas)
            )
            return HTMLResponse(
                _thinking_template.render(
                    conversation=marker or "",
                    thinking_switchable=found is not None,
                    thinking_here=thinking_here,
                )
            )
        return RedirectResponse(
            _back_to(request, marker), status_code=status.HTTP_303_SEE_OTHER
        )

    # -- who is answering --------------------------------------------------

    @router.post("/chat/persona", response_class=HTMLResponse, summary="Choose who answers")
    async def chat_persona(request: Request) -> HTMLResponse:
        """Choose who answers **in this conversation**, and nowhere else.

        This used to post to the JSON API's ``select_persona``, which writes
        ``default_persona`` into ``core.toml``: picking a persona to talk to made
        the core answer as that persona for the OpenAI-compatible API, for an
        event waking the agent, and for every other consumer at once. The
        owner asked for the opposite — a mild default the house keeps using,
        and the personalised ones for whoever is actually in a conversation —
        so the choice is
        recorded on the conversation and **no configuration is written here at
        all**.

        Three refusals, each a sentence and none of them a silent substitution:

        * **Nothing picked** — say so.
        * **A persona that will not load** — never installed, deleted, or a
          name that is not a plain persona name. The store's own plain-English
          message is shown and nothing is recorded. Checked here, at the click,
          so the answer arrives while the operator is looking at the control;
          the turn checks again anyway, through the loop's own resolution, for
          a persona that disappears in between.
        * **A store that cannot remember it** — :data:`PERSONA_NOT_REMEMBERED`,
          rather than a save note over a choice that will be gone on reload.

        The persona is validated but never *resolved* here: what reaches the
        turn is the name, loaded by the agent loop through the same call the
        default goes through, so the safety block and the persona's guardrails
        (ADR-0005) are composed identically whichever persona answers.

        The whole page comes back rather than a fragment: the persona decides
        the voice and the name under every future reply in this thread, and a
        picker that changed all of that while quietly redrawing one dropdown
        would be hiding the size of what just happened.
        """
        user = require_user(request)
        form = await request.form()
        wanted = str(form.get("persona") or "").strip()
        marker = str(form.get("conversation") or "") or None
        # One instant for the whole handler. A page that posted no marker at
        # all still has to come back showing the thread its choice was
        # recorded against, so "now" is decided once here rather than twice —
        # recording against one instant and then rendering another would show
        # the operator a picker that had forgotten what they just picked.
        opened = conversation_start(wanted_conversation(marker)) or datetime.now(UTC)
        current = _chosen_persona(await _looked_at(user, opened)) or personas.default_persona

        note: dict[str, str]
        if not wanted:
            note = {"kind": "invalid", "message": "Pick a persona first."}
        elif wanted == current:
            note = {"kind": "none", "message": PERSONA_UNCHANGED}
        else:
            note = await _choose(user, opened, wanted)
        return templates.TemplateResponse(
            request=request,
            name="chat.html",
            context=await _screen(request, wanted=opened.isoformat(), note=note),
        )

    async def _choose(user: AdminUser, opened: datetime, wanted: str) -> dict[str, str]:
        """Record ``wanted`` on this operator's conversation, or say why not."""
        try:
            persona = personas.load(wanted)
        except PersonaError as exc:
            # The store's own sentence, which already names the persona and the
            # reason (spec §9). Nothing is recorded and nothing is substituted.
            return {"kind": "invalid", "message": f"Not changed: {exc.spoken_message}"}
        except Exception:  # noqa: BLE001 - an unreadable persona is not a dead screen
            return {
                "kind": "invalid",
                "message": f"Not changed: I couldn't read the persona {wanted!r}.",
            }

        if not conversations.remembers_persona:
            return {"kind": "invalid", "message": PERSONA_NOT_REMEMBERED}

        # ``create=True``: choosing who answers before saying anything is
        # ordinary, and the conversation minted here starts at the instant the
        # composer is already carrying, so the first message lands in it. An
        # empty conversation is not shown in the rail, so a pick nobody spoke
        # into leaves nothing behind.
        conversation = await conversations.at(Owner.profile(user.id), opened)
        if not await conversations.set_persona(conversation, wanted):
            return {"kind": "invalid", "message": PERSONA_NOT_REMEMBERED}
        return {
            "kind": "saved",
            "message": PERSONA_SAVED.format(name=persona.display_name or wanted),
        }
