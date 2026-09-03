"""Voices: what is installed, uploading one, filling it in, and getting it back.

This is the round trip ADR-0029 §4 and PC-337 describe, and it is one screen
plus one form because that is what it is:

1. **Upload the bare files.** A stock Piper voice — an ``.onnx`` and its JSON
   config, zipped exactly as its author published them — installs and works. No
   wrapper file, no ``voice.toml``, nothing edited inside the archive (PC-331).
2. **Fill it in.** Name, description, language, licence, attribution,
   pronunciation and the synthesis defaults, on the manage screen.
3. **Export.** The voice comes back as a properly formed pack with its
   ``voice.toml`` generated from what was typed.

The point of step 3 is step 1's cost: a stock voice carries no metadata at all,
so the pack is *built by* this process rather than required in front of it. An
export that refused until every field was filled would defeat what it is for,
which is why a voice with nothing filled in still exports.

Nothing here unpacks or validates an archive itself. Every refusal comes from
:mod:`personacore.plugins.voice_packages`, which reaches the plugin installer's
own traversal, symlink, member and size checks rather than restating them.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from personacore.audit.models import AuditOutcome
from personacore.plugins.packages import PackageRejected
from personacore.plugins.voice_packages import (
    ATTRIBUTION_FILENAME,
    DEFAULT_NOTATION,
    LICENCE_FILENAME,
    VOICE_LIMITS,
    VoiceMetadata,
    export_voice,
    install_voice,
    installed_voices,
    read_pronunciation,
    read_text_file,
    read_voice_metadata,
    remove_voice,
    suggest_voice_id,
    voice_directory,
    voice_files,
    write_pronunciation,
    write_text_file,
    write_voice_metadata,
)
from personacore.voice.engine import wav_bytes
from personacore.voice.pacing import (
    DEFAULT_CLAUSE_MARKS,
    DEFAULT_SENTENCE_GAP_MS,
    DEFAULT_SENTENCE_MARKS,
    PACING_FIELDS,
    PACING_MARK_FIELDS,
    PACING_MEANING,
    PACING_NOT_A_NUMBER,
    Pacing,
    marks_refusal,
    pacing_refusal,
)
from personacore.web.screens.voice_common import (
    CARRIED_INTO_A_SENTENCE,
    NO_VOICE_SUBSYSTEM,
    SPEAK_HINT,
    SPEAK_MAX_CHARACTERS,
    SPEAK_TOO_LONG,
    TEST_SENTENCE,
    TEST_SPEAK_UNAVAILABLE,
    engine_rows,
    speak_blocker,
    voice_library,
    voice_registry,
    voice_rows,
)
from personacore.web.shared import UIContext

INSTALL_NO_FILE = (
    "Nothing was installed: no file was chosen. Click “Choose a .zip…”, pick the "
    "voice, then click Install."
)

INSTALL_NO_ENGINE = "Nothing was installed: pick the engine that speaks this voice."

INSTALL_UNREADABLE = "Nothing was installed: that upload could not be read. Choose the .zip again."

INSTALL_TOO_LARGE = (
    "Nothing was installed: that file is larger than {limit}, the limit for one voice."
)

INSTALL_OK = "{label} installed. It is in the list below and in every voice picker."

INSTALL_REPLACED = "{label} installed, replacing the voice that was there."

INSTALL_DISCLOSURE = (
    "A voice pack holds data — model files, configuration and text. Anything "
    "executable, any pickle and any nested archive is refused, naming the file."
)
"""What the upload form says about what it accepts.

Three facts and a stop. The first draft of this explained why a pickle is
dangerous, which is the installer's job at the moment it refuses one, not the
form's job in advance."""

SKIPPED_LEDE = (
    "These folders are in the voices directory and are not in the list below. "
    "Each line says which and why."
)
"""The heading over the voice folders that could not be read.

The interface contract says a bad voice folder is "skipped and named, never
fatal", and the naming is the half that matters: an operator who uploaded a
voice and cannot find it in the list is owed the reason, not a shorter list.
:meth:`~personacore.voice.library.VoiceLibrary.listing` has computed those
reasons since the day it was written and no running application asked for one —
so the reason existed, was tested, and reached nobody.

This screen is also where the walk that feeds ``/health``'s ``skipped_voices``
happens, which is why it is here and not on the engines screen: install and
remove both re-render this page, so the snapshot is current the moment anything
about the voices directory changes through the UI.
"""

SKIPPED_LABEL = "{engine}/{voice}"

MULTIPART_SLACK_BYTES = 64 * 1024
"""Room for the multipart envelope around the file itself, so a package right on
the limit is not refused for the boundary lines around it."""

SAVED = "Saved. {label} is written into voice.toml beside its files."
SAVE_NOTHING = "Nothing changed."

ID_IS_FIXED = (
    "The id is “{voice}” and it does not change — it is the folder name on "
    "disk. Install it under a new id if you need a different one."
)
"""Said plainly on the screen rather than left as a disabled box with no reason.

The same decision, and the same wording problem, as
:data:`~personacore.web.screens.persona_edit.PERSONA_IDENTIFIER_FIXED`: a
field that is simply greyed out sends somebody hunting for the permission to
edit it."""

EXPORT_NOTE = (
    "Downloads this voice as a pack: its files, plus a voice.toml built from "
    "this page. Empty fields export empty."
)

REMOVE_TITLE = "Remove {label}?"

REMOVE_BODY = (
    "This deletes the folder for “{voice}” and everything in it — model, "
    "configuration, pronunciations, licence and everything typed on this "
    "page. This cannot be undone. Download the pack first if you may want it "
    "again."
)

REMOVE_KEEP = "Keep {label}"

REMOVED = "{label} removed."

VOICE_FIELDS = (
    "name",
    "description",
    "language",
    "version",
    "engine_min_version",
    "licence_spdx",
    "licence_source",
    "author_name",
    "author_contact",
)
"""The text fields the manage form writes, by the metadata attribute each sets.

Named once so the form, the reader and the writer cannot disagree about which
boxes exist."""

NUMERIC_FIELDS = ("length_scale", "noise_scale", "noise_w")

NUMBER_REFUSED = "“{value}” is not a number, so {field} was not saved."

GAP_FIELDS = PACING_FIELDS
"""The three pacing gaps (PC-342), in whole milliseconds.

Separate from :data:`NUMERIC_FIELDS` because they are counted rather than
scaled: 450 is a length of silence, and "450.5 ms" is a number somebody typed
by accident. They are refused and clamped by exactly the rules the synthesis
defaults already use -- :func:`personacore.voice.pacing.pacing_refusal` is
:func:`personacore.voice.engine.synthesis_refusal`'s shape on purpose."""

PACING_HELP = (
    "The silence between sentences. A clause gap is half this value and a "
    "paragraph gap is double, so tuning it keeps the shape. 0 runs sentences "
    "together."
)

PACING_ADVANCED_HELP = (
    "Leave both empty and they follow the sentence gap. A number here "
    "overrides that ratio. 0 is a value and means no gap."
)

MARK_FIELDS = PACING_MARK_FIELDS
"""The two boxes that say which characters this voice breaks at (PC-342).

Beside the gaps because they are the other half of the same question -- the
gaps are how long a break lasts and these are where the breaks are -- and
per voice for the reason the gaps are: the right answer was found by ear on one
voice, and it has to travel with that voice's pack."""

PACING_MARKS_HELP = (
    "Which characters break the speech. Each box replaces the usual list, so "
    "removing a mark means typing the ones you want to keep — drop the comma "
    "and this voice runs its commas together again. Empty is the usual list, "
    "not “never break”; that is a gap of 0."
)

DESCRIBED_FIELDS = ("sample_rate", "channels")
"""Two figures on this screen that are facts about the voice, not settings.

They used to be editable boxes, and neither could ever be honoured. The sample
rate is whatever the model emits, stated in the model's own config, and this
build has no resampler — a different number would not change the voice, it
would mislabel the audio. Nothing anywhere reads a channel count; the engine
always produces mono.

So they are shown and not offered. An editable box that silently does nothing
is the defect ``tests/server/test_no_dead_controls.py`` exists for, and it does
not become acceptable because the number beside it happens to be true.
"""

SAMPLE_RATE_NOTE = (
    "The model's own figure, from its configuration. There is no resampler, "
    "so changing this number would mislabel the audio, not change the "
    "voice's speed."
)

CHANNELS_NOTE = "This engine produces mono. Nothing reads a channel count."

DESCRIBED_IGNORED = "{field} is not a setting on this screen. The value sent was ignored. {why}"
"""What a submitted value for one of those two is answered with.

Answered rather than dropped, for the same reason the engines screen answers a
value for a switch it does not offer: a value that arrives and vanishes with
nothing said is a control that appears to work and does not, seen from the
other side. It is not refused either — the rest of the form is the operator's
work and is saved.
"""


def _text(form: Any, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _optional(text: str) -> str | None:
    """A box's contents, where **empty means not set**.

    The same equivalence the pack format has: an exported ``voice.toml``
    carries every field and writes ``""`` for the ones nobody filled in, so a
    box cleared on this screen and a field never filled in have to be the same
    state or the round trip would not be one.
    """
    return text or None


def _gap_or(text: str, fallback: int) -> int:
    """A pacing box read for display only, never for saving.

    Used to work out what the two advanced boxes follow while they are empty,
    so a bad value in the sentence box shows the default's ratio rather than
    crashing the page the refusal has to be rendered on.
    """
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


def _requested_text(text: str) -> tuple[str | None, str]:
    """What to speak, and a note if it is not literally what was asked for.

    Three answers, and the middle one is the interesting one:

    * Nothing typed -> the measured sentence, which is what the box is
      pre-filled with anyway.
    * **One word -> that word inside a carrier sentence**, with a note. This
      voice mispronounces almost any word given alone (PC-203) -- "about"
      becomes "bout", "put" becomes "putt" -- and says the same phonemes
      correctly in a sentence. Speaking a bare word would answer the operator's
      question wrongly in whichever direction they read it: a correct fix would
      sound broken, and a broken one can sound fine. Refusing would be worse
      still, since asking to hear one word is the natural thing to do straight
      after editing that word's pronunciation.
    * Too long -> ``None``, refused before any synthesis starts.

    A sentence is passed through untouched. The operator's text is the point:
    it is also the only way anybody can tell this control synthesises rather
    than replaying a recording.
    """
    wanted = " ".join(text.split())
    if not wanted:
        return TEST_SENTENCE, ""
    if len(wanted) > SPEAK_MAX_CHARACTERS:
        return None, ""
    if len(wanted.split()) == 1:
        bare = wanted.strip(".,;:!?\"'")
        return f"Say {bare} again.", CARRIED_INTO_A_SENTENCE.format(word=bare)
    return wanted, ""


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the voices list, the upload, the manage screen and the export."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import _record_change

    templates = ctx.templates
    layout = ctx.layout

    def _found(engine: str, voice: str):
        """One installed voice's folder, or a 404 in a sentence.

        Both ids go through the pack module's own checks before either becomes
        part of a path — the ordering the plugin installer's traversal bug was
        about. From outside, "that is not a voice id" and "there is no such
        voice" are the same answer, so both are 404.
        """
        try:
            return voice_directory(layout, engine, voice)
        except PackageRejected as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    async def _list_context(
        request: Request, *, install_result: dict[str, str] | None = None
    ) -> dict[str, Any]:
        registry = voice_registry(request)
        library = voice_library(request)
        voices = await asyncio.to_thread(installed_voices, layout)
        engines = engine_rows(registry, voices)
        # Ask the engines what they could NOT read, which is the only place
        # that answer exists. Threaded because it walks the voices directory,
        # and it is walked here on purpose: this page is re-rendered by install
        # and by remove, so the snapshot `/health` reports is refreshed by the
        # same act that can change what is installed. `refresh` never raises —
        # a voice folder nobody can read must not cost the screen that exists
        # to tell you about it.
        listing = await asyncio.to_thread(library.refresh) if library is not None else None
        skipped = [
            {
                "label": SKIPPED_LABEL.format(engine=item.engine_id or "?", voice=item.id),
                "reason": item.reason,
            }
            for item in (listing.skipped if listing is not None else ())
        ]
        return {
            **await ctx.shell(request, "voices"),
            "voices": voice_rows(voices, engines),
            "engines": [row for row in engines if row["available"]],
            "install_result": install_result,
            "disclosure": INSTALL_DISCLOSURE,
            "max_bytes": VOICE_LIMITS.max_archive_bytes,
            "engines_note": NO_VOICE_SUBSYSTEM if registry is None else "",
            # Named, never counted, and never merely absent from the list.
            "skipped": skipped,
            "skipped_lede": SKIPPED_LEDE,
        }

    # -- the list, and the upload on it ------------------------------------

    @router.get("/voice/voices", response_class=HTMLResponse, summary="Installed voices")
    async def voices_page(request: Request) -> HTMLResponse:
        """Every installed voice, its engine, and the control that adds one."""
        return templates.TemplateResponse(
            request=request, name="voices.html", context=await _list_context(request)
        )

    async def _uploaded(request: Request) -> tuple[bytes, str, str, bool]:
        """The archive and the three answers beside it, or a refusal in a sentence.

        The declared length is glanced at before the body is touched, because
        Starlette spools a file part to a temporary file with no ceiling of its
        own — a gigabyte would be written to disk on its way to being refused.
        It is a guard against spooling rather than a second limit: the
        installer enforces the ceiling on the same
        :data:`~personacore.plugins.voice_packages.VOICE_LIMITS` object.
        """
        declared = request.headers.get("content-length")
        if (
            declared
            and declared.isdigit()
            and int(declared) > VOICE_LIMITS.max_archive_bytes + MULTIPART_SLACK_BYTES
        ):
            raise _Refused(
                INSTALL_TOO_LARGE.format(limit=_megabytes(VOICE_LIMITS.max_archive_bytes))
            )
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - a malformed envelope is a sentence
            raise _Refused(INSTALL_UNREADABLE) from exc
        try:
            upload = form.get("archive")
            if not isinstance(upload, StarletteUploadFile) or not upload.filename:
                raise _Refused(INSTALL_NO_FILE)
            data = await upload.read()
            if not data:
                raise _Refused(INSTALL_NO_FILE)
            engine = _text(form, "engine")
            if not engine:
                raise _Refused(INSTALL_NO_ENGINE)
            # The typed id when there is one, otherwise a suggestion from the
            # filename — and either way the installer checks it before it is
            # joined onto anything.
            wanted = _text(form, "voice") or suggest_voice_id(upload.filename)
            return data, engine, wanted, bool(form.get("replace"))
        finally:
            await form.close()

    @router.post(
        "/voice/voices/install",
        response_class=HTMLResponse,
        summary="Install a voice from an uploaded .zip",
    )
    async def voice_install(request: Request) -> HTMLResponse:
        """PC-337's upload, from the page rather than from a shell.

        A plain multipart form post answered with the whole page. This route
        parses the envelope and nothing else: the bytes go straight to
        :func:`~personacore.plugins.voice_packages.install_voice`, which stages
        them inside appdata and runs the plugin installer's own refusals over
        them. There is no branch here that could step around one.

        Answers 200 whether the install succeeded or was refused, because the
        response *is* the refreshed screen. A status code is not a sentence an
        operator can act on (spec §9).
        """
        user = ctx.require_user(request)
        try:
            data, engine, wanted, replace = await _uploaded(request)
        except _Refused as refused:
            return await _answer(request, {"kind": "refused", "message": str(refused)})

        try:
            installed = await asyncio.to_thread(
                lambda: install_voice(
                    layout, data, engine=engine, voice_id=wanted or None, replace=replace
                )
            )
        except PackageRejected as exc:
            return await _answer(request, {"kind": "refused", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a full volume is a sentence, not a 500
            return await _answer(
                request, {"kind": "refused", "message": f"Nothing was installed: {exc}"}
            )

        await _record_change(
            ctx.audit,
            user,
            action="voice.install",
            outcome=AuditOutcome.SUCCESS,
            detail={"engine": installed.engine, "voice": installed.voice},
        )
        label = f"{installed.voice} ({installed.engine})"
        message = (INSTALL_REPLACED if installed.replaced else INSTALL_OK).format(label=label)
        return await _answer(request, {"kind": "ok", "message": message})

    async def _answer(request: Request, result: dict[str, str]) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="voices.html",
            context=await _list_context(request, install_result=result),
        )

    # -- one voice: what it is, and what it may be told ---------------------

    async def _edit_context(
        request: Request,
        engine: str,
        voice: str,
        *,
        save_result: dict[str, str] | None = None,
        typed: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        directory = _found(engine, voice)
        metadata = await asyncio.to_thread(
            read_voice_metadata, directory, engine=engine, voice=voice
        )
        lexicon, notation = await asyncio.to_thread(read_pronunciation, directory)
        registry = voice_registry(request)
        library = voice_library(request)
        engines = {row["id"]: row for row in engine_rows(registry, [])}
        row = engines.get(engine, {})
        # Whether the control is drawn at all is the library's answer, not this
        # screen's guess: a running engine that has not got this voice cannot
        # speak it either, and it says so in a sentence.
        resolution = library.resolve(engine, voice) if library is not None else None
        fields = {name: getattr(metadata, name) or "" for name in VOICE_FIELDS}
        fields |= {
            name: ("" if getattr(metadata, name) is None else str(getattr(metadata, name)))
            for name in (*NUMERIC_FIELDS, *GAP_FIELDS, "sample_rate", "channels")
        }
        fields |= {name: getattr(metadata, name) or "" for name in MARK_FIELDS}
        fields["licence_text"] = read_text_file(directory, LICENCE_FILENAME)
        fields["attribution_text"] = read_text_file(directory, ATTRIBUTION_FILENAME)
        fields["pronunciation"] = lexicon
        fields["notation"] = notation or DEFAULT_NOTATION
        if typed:
            fields |= typed
        # What the two advanced boxes will do while they are empty, shown as
        # their placeholder. "Follows the ratio" is a rule; 225 is an answer,
        # and an operator who has just changed the sentence gap can see what
        # the other two became without saving and coming back.
        paced = Pacing(sentence_ms=_gap_or(fields["sentence_gap_ms"], DEFAULT_SENTENCE_GAP_MS))
        label = f"{metadata.display} ({engine})"
        return {
            **await ctx.shell(request, "voices"),
            "engine": engine,
            "voice": voice,
            "label": label,
            "fields": fields,
            "files": voice_files(directory),
            "id_note": ID_IS_FIXED.format(voice=voice),
            # Said on the screen rather than implied by a box that will not
            # take a value: where the number came from, and why it is not a
            # setting.
            "sample_rate_note": SAMPLE_RATE_NOTE,
            "channels_note": CHANNELS_NOTE,
            "export_note": EXPORT_NOTE,
            # The refusal `read_voice_metadata` already worked out, said in
            # full on the voice's own page. The list can only carry the word
            # -- its chips are two words each -- and the sentence there is a
            # `title`, which does not exist on a touch screen. This surface is
            # read on a tablet, so a mark with no visible reason for it
            # only tells the reader something is wrong.
            "unsupported": metadata.unsupported or "",
            "engine_known": bool(row),
            # Whether the control is drawn at all is the library's answer, not
            # this screen's guess: a running engine that has not got this voice
            # cannot speak it either.
            "engine_running": bool(resolution is not None and resolution.can_speak),
            "engine_display": row.get("display") or engine,
            "pacing_help": PACING_HELP,
            "pacing_advanced_help": PACING_ADVANCED_HELP,
            "pacing_marks_help": PACING_MARKS_HELP,
            "pacing_default": DEFAULT_SENTENCE_GAP_MS,
            "sentence_marks_default": DEFAULT_SENTENCE_MARKS,
            "clause_marks_default": DEFAULT_CLAUSE_MARKS,
            "clause_follows": paced.clause,
            "paragraph_follows": paced.paragraph,
            # Open when anything inside it has been set, marks included: a
            # voice carrying its own clause marks would otherwise hide the one
            # setting that explains how it sounds.
            "pacing_open": any(fields[name] for name in (*GAP_FIELDS[1:], *MARK_FIELDS)),
            "test_sentence": TEST_SENTENCE,
            "speak_max": SPEAK_MAX_CHARACTERS,
            "speak_hint": SPEAK_HINT,
            "cannot_speak": (
                NO_VOICE_SUBSYSTEM if library is None else speak_blocker(engine, row, resolution)
            ),
            "save_result": save_result,
        }

    @router.get(
        "/voice/voices/{engine}/{voice}",
        response_class=HTMLResponse,
        summary="One voice's details",
    )
    async def voice_page(request: Request, engine: str, voice: str) -> HTMLResponse:
        """Everything about one voice that a person may change, and its id, which
        they may not."""
        return templates.TemplateResponse(
            request=request,
            name="voice_edit.html",
            context=await _edit_context(request, engine, voice),
        )

    @router.post(
        "/voice/voices/{engine}/{voice}",
        response_class=HTMLResponse,
        summary="Save one voice's details",
    )
    async def voice_save(request: Request, engine: str, voice: str) -> HTMLResponse:
        """Write ``voice.toml`` — and ``LICENSE``, ``ATTRIBUTION.md`` and
        ``pronunciation.json`` — beside the voice's own files.

        Every box may be empty, and an empty one means the field is not set.
        Nothing is required except the id, which is the folder name and is not
        on this form at all.
        """
        user = ctx.require_user(request)
        directory = _found(engine, voice)
        form = await request.form()
        try:
            typed = {name: _text(form, name) for name in VOICE_FIELDS}
            typed |= {name: _text(form, name) for name in NUMERIC_FIELDS}
            typed |= {name: _text(form, name) for name in GAP_FIELDS}
            typed |= {name: _text(form, name) for name in MARK_FIELDS}
            sent = {name: _text(form, name) for name in DESCRIBED_FIELDS}
            typed["licence_text"] = str(form.get("licence_text") or "")
            typed["attribution_text"] = str(form.get("attribution_text") or "")
            typed["pronunciation"] = str(form.get("pronunciation") or "")
            typed["notation"] = _text(form, "notation")
        finally:
            await form.close()

        numbers: dict[str, float | None] = {}
        for name in NUMERIC_FIELDS:
            raw = typed[name]
            if not raw:
                numbers[name] = None
                continue
            try:
                numbers[name] = float(raw)
            except ValueError:
                return await _refuse_save(
                    request,
                    engine,
                    voice,
                    typed,
                    NUMBER_REFUSED.format(value=raw, field=name.replace("_", " ")),
                )
        # PC-342's three, on the same terms and by the same validator the
        # synthesis defaults use: an empty box is "not set", zero is a value
        # and means no gap, and anything unusable is refused here -- while the
        # form is still on screen -- rather than clamped in a log line hours
        # later. A whole number, because 450 is a count of milliseconds.
        gaps: dict[str, int | None] = {}
        for name in GAP_FIELDS:
            raw = typed[name]
            if not raw:
                gaps[name] = None
                continue
            try:
                gaps[name] = int(raw)
            except ValueError:
                return await _refuse_save(
                    request,
                    engine,
                    voice,
                    typed,
                    PACING_NOT_A_NUMBER.format(
                        field=name.removesuffix("_ms").replace("_", " ").capitalize(),
                        value=raw,
                        meaning=PACING_MEANING[name],
                    ),
                )
            refusal = pacing_refusal(name, gaps[name])
            if refusal is not None:
                return await _refuse_save(request, engine, voice, typed, refusal)

        # PC-342's two character sets, refused on the same terms as the gaps
        # above and by the same shape of validator. An empty box is the usual
        # marks; a letter in one is refused here rather than cleaned away
        # quietly, because "period" typed into the box is a mistake to answer.
        marks: dict[str, str | None] = {}
        for name in MARK_FIELDS:
            refusal = marks_refusal(name, typed[name] or None)
            if refusal is not None:
                return await _refuse_save(request, engine, voice, typed, refusal)
            marks[name] = _optional(typed[name])

        # The two described figures are carried through from what the voice
        # already records rather than read off the form. Read off the form they
        # would be erased by every save the moment the boxes came off the page,
        # which is how a control that stops being editable quietly becomes a
        # control that deletes.
        existing = await asyncio.to_thread(
            read_voice_metadata, directory, engine=engine, voice=voice
        )
        counts: dict[str, int | None] = {name: getattr(existing, name) for name in DESCRIBED_FIELDS}
        ignored = [
            DESCRIBED_IGNORED.format(
                field="The sample rate" if name == "sample_rate" else "Channels",
                why=SAMPLE_RATE_NOTE if name == "sample_rate" else CHANNELS_NOTE,
            )
            for name in DESCRIBED_FIELDS
            if sent[name] and sent[name] != str(counts[name] or "")
        ]

        metadata = VoiceMetadata(
            id=voice,
            engine=engine,
            **{name: _optional(typed[name]) for name in VOICE_FIELDS},
            **numbers,
            **gaps,
            **marks,
            **counts,
        )
        try:
            await asyncio.to_thread(
                write_text_file, directory, LICENCE_FILENAME, typed["licence_text"]
            )
            await asyncio.to_thread(
                write_text_file, directory, ATTRIBUTION_FILENAME, typed["attribution_text"]
            )
            await asyncio.to_thread(
                lambda: write_pronunciation(
                    directory, typed["pronunciation"], notation=typed["notation"]
                )
            )
            # Last, because it records which of those files now exist.
            await asyncio.to_thread(write_voice_metadata, directory, metadata)
        except PackageRejected as exc:
            return await _refuse_save(request, engine, voice, typed, str(exc))
        except Exception as exc:  # noqa: BLE001 - a full volume is a sentence
            return await _refuse_save(request, engine, voice, typed, str(exc))

        await _record_change(
            ctx.audit,
            user,
            action="voice.update",
            outcome=AuditOutcome.SUCCESS,
            detail={"engine": engine, "voice": voice},
        )
        label = f"{metadata.display} ({engine})"
        # A value sent for one of the two described figures is answered here,
        # in the same breath as the save that did work. Not a refusal: the rest
        # of the form is the operator's work and it is written.
        message = " ".join([SAVED.format(label=label), *ignored])
        return templates.TemplateResponse(
            request=request,
            name="voice_edit.html",
            context=await _edit_context(
                request,
                engine,
                voice,
                save_result={
                    "kind": "invalid" if ignored else "saved",
                    "message": message,
                },
            ),
        )

    async def _refuse_save(
        request: Request, engine: str, voice: str, typed: dict[str, str], message: str
    ) -> HTMLResponse:
        """Re-render the form with what was typed still in it.

        A refused save that reverted the boxes would cost the operator
        everything they had written to tell them one number was wrong.
        """
        return templates.TemplateResponse(
            request=request,
            name="voice_edit.html",
            context=await _edit_context(
                request,
                engine,
                voice,
                save_result={"kind": "invalid", "message": message},
                typed=typed,
            ),
        )

    # -- export ------------------------------------------------------------

    @router.get(
        "/voice/voices/{engine}/{voice}/export",
        summary="Download one voice as a pack",
    )
    async def voice_export(request: Request, engine: str, voice: str) -> Response:
        """The voice's own directory, zipped, with its ``voice.toml`` generated.

        **One voice's folder and nothing else.** Every file is resolved and
        checked against the resolved voice directory before it is written into
        the zip, and a symlink is refused rather than followed — the same
        reasoning as the install-side refusals, pointing the other way: an
        install must not write outside the folder, and an export must not read
        outside it.

        A voice with nothing filled in exports anyway. The generated
        ``voice.toml`` carries every field the format defines with empty
        strings in the ones nobody has set, which is the template somebody
        edits to turn loose files into a distributable pack.
        """
        _found(engine, voice)
        try:
            pack = await asyncio.to_thread(lambda: export_voice(layout, engine=engine, voice=voice))
        except PackageRejected as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return Response(
            content=pack.data,
            media_type="application/zip",
            headers={
                # The filename is the validated voice id, so it holds nothing a
                # header could be split on.
                "Content-Disposition": f'attachment; filename="{pack.filename}"',
                "Content-Length": str(len(pack.data)),
            },
        )

    # -- test speak --------------------------------------------------------

    @router.get(
        "/voice/voices/{engine}/{voice}/speak",
        response_class=HTMLResponse,
        summary="Play a test line in one voice",
    )
    async def voice_speak_page(
        request: Request, engine: str, voice: str, text: str = ""
    ) -> HTMLResponse:
        """A page with a player on it, rather than the audio itself.

        **The button used to navigate straight at the ``.wav``.** Pressing
        it on a tablet produced a screenful of binary rendered as text: handed a
        media file by navigation, a browser may play it, download it, or print
        the bytes, and which one is not ours to decide. It had been that way
        since before the text box — nobody had pressed it.

        So the control lands on HTML, which works with no JavaScript, lets the
        page say what was actually spoken — which matters when a bare word was
        carried into a sentence — and gives an operator a play button and a
        scrub bar instead of one shot at hearing it.

        **This route synthesises, once, and embeds the result.** It used to
        render an ``<audio src>`` pointing at :func:`voice_speak_audio` and let
        the browser fetch that second request. A voice whose model cannot be
        loaded raises there, and the failure is written correctly — the
        engine's own exception message, in a 409 — into a response an
        ``<audio>`` element cannot render as anything: not text, not a player,
        nothing an operator can read. `can_speak` was true throughout, because
        nothing loads the model until synthesis is attempted. So this page
        proves the audio before it ever draws a player: the WAV is built here,
        inline as a ``data:`` URI, and a synthesis failure becomes the same
        sentence-in-a-paragraph every other refusal on this page already is,
        with no second request left that could fail where nobody would see it.

        The cost is one embedding: base64 is a third again the size of the raw
        WAV, all of it in the page's own response rather than a separate
        request the browser could cache. For :data:`SPEAK_MAX_CHARACTERS` of
        text that is still a page measured in single-digit megabytes at worst,
        which is what an operator's own browser, on the same network as this
        core, is asked to hold for one test line.

        :func:`voice_speak_audio` is unchanged and still answers a direct
        ``GET`` — nothing else in this build links to it, but deleting a route
        somebody may be depending on for a reason this screen cannot see is a
        worse mistake than leaving one running unused.
        """
        _found(engine, voice)
        wanted, carried = _requested_text(text)
        if wanted is None:
            return HTMLResponse(
                f"<p>{SPEAK_TOO_LONG.format(limit=SPEAK_MAX_CHARACTERS)}</p>",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        library = voice_library(request)
        if library is None:
            return HTMLResponse(
                f"<p>{NO_VOICE_SUBSYSTEM}</p>", status_code=status.HTTP_409_CONFLICT
            )
        resolution = await asyncio.to_thread(library.resolve, engine, voice)
        if not resolution.can_speak:
            rows = {item["id"]: item for item in engine_rows(voice_registry(request), [])}
            refusal = speak_blocker(engine, rows.get(engine, {}), resolution)
            return HTMLResponse(
                f"<p>{refusal or TEST_SPEAK_UNAVAILABLE}</p>",
                status_code=status.HTTP_409_CONFLICT,
            )
        # `can_speak` only means the engine and voice exist — nothing has
        # opened the model file yet. Synthesis is the first point that can
        # fail on a voice that is not what it claims to be, and it happens
        # here, in a response that can still say so in a sentence, not in the
        # one response type on this page that cannot.
        try:
            spoken = await asyncio.to_thread(resolution.speak, wanted)
        except Exception as exc:  # noqa: BLE001 - an engine failure is a sentence
            return HTMLResponse(f"<p>{exc}</p>", status_code=status.HTTP_409_CONFLICT)
        data_url = f"data:audio/wav;base64,{base64.b64encode(_wav(spoken)).decode('ascii')}"
        return templates.TemplateResponse(
            request=request,
            name="fragments/voice_speak.html",
            context={
                "spoken": wanted,
                "carried": carried,
                "audio_url": data_url,
            },
        )

    @router.get(
        "/voice/voices/{engine}/{voice}/speak.wav",
        summary="The audio for one test line",
    )
    async def voice_speak_audio(
        request: Request, engine: str, voice: str, text: str = ""
    ) -> Response:
        """PC-203: **a sentence, never a bare word.**

        Measured rather than preferred. This voice says "about" as "bout" and
        "put" as "putt" when either is given alone, and says both correctly
        inside a sentence — so a one-word test reports faults that do not exist
        and hides real ones, which turns the one tool for checking a
        pronunciation fix into a thing that lies in both directions. The line
        is :data:`~personacore.web.screens.voice_common.TEST_SENTENCE` and
        it carries the four words that were measured.

        The library decides whether this can speak, and when it cannot the
        refusal is written by :func:`~personacore.web.screens.voice_common.speak_blocker`
        — **the same call that puts the sentence on the page this button is
        on**. It used to answer with ``resolution.reason``, which is written for
        a persona and says "this persona replies in text" on a screen where no
        persona exists: one condition, one page, two different sentences, which
        is how an operator stops trusting what the screen says.

        **The page's player no longer points here.** It used to, and a voice
        whose model would not load raised past that check into a WAV response
        that an ``<audio src>`` cannot render as anything readable — the
        failure was written correctly and delivered nowhere an operator could
        see it. :func:`voice_speak_page` now synthesises itself and embeds the
        result, so this route answers no request the player makes. It is kept
        because nothing here justifies deleting a route another caller may
        still be using directly, and every refusal it gives is still correct —
        it was only ever wrong to *link to* it from an ``<audio>`` element.
        """
        _found(engine, voice)
        library = voice_library(request)
        if library is None:
            return HTMLResponse(
                f"<p>{NO_VOICE_SUBSYSTEM}</p>", status_code=status.HTTP_409_CONFLICT
            )
        resolution = await asyncio.to_thread(library.resolve, engine, voice)
        if not resolution.can_speak:
            rows = {item["id"]: item for item in engine_rows(voice_registry(request), [])}
            refusal = speak_blocker(engine, rows.get(engine, {}), resolution)
            return HTMLResponse(
                f"<p>{refusal or TEST_SPEAK_UNAVAILABLE}</p>",
                status_code=status.HTTP_409_CONFLICT,
            )
        wanted, carried = _requested_text(text)
        if wanted is None:
            return HTMLResponse(
                f"<p>{SPEAK_TOO_LONG.format(limit=SPEAK_MAX_CHARACTERS)}</p>",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            spoken = await asyncio.to_thread(resolution.speak, wanted)
        except Exception as exc:  # noqa: BLE001 - an engine failure is a sentence
            return HTMLResponse(f"<p>{exc}</p>", status_code=status.HTTP_409_CONFLICT)
        headers = {"Content-Disposition": f'inline; filename="{voice}-test.wav"'}
        if carried:
            # The operator asked for one word and heard a sentence. Saying so
            # in a header rather than only in the log, because a listener who
            # is not told will hear words they did not type and distrust the
            # control -- which is the opposite of what it is for.
            #
            # Percent-encoded, not written straight in: an HTTP header is
            # Latin-1 by the ASGI server's own rule, and
            # :data:`~personacore.web.screens.voice_common.CARRIED_INTO_A_SENTENCE`
            # carries curly quotes and an em dash that are not in it — a bare
            # word given directly to this route, with no page in between,
            # crashed here with a 500 before this line quoted it. The page
            # never hit this: it renders `carried` as HTML, where none of that
            # is a problem, which is exactly how a route only reachable
            # directly kept a fault the page's own path never exercised.
            headers["X-Spoken-As"] = quote(carried)
        return Response(content=_wav(spoken), media_type="audio/wav", headers=headers)

    # -- remove ------------------------------------------------------------

    @router.get(
        "/voice/voices/{engine}/{voice}/remove",
        response_class=HTMLResponse,
        summary="Confirm removing one voice",
    )
    async def voice_remove_confirm(request: Request, engine: str, voice: str) -> HTMLResponse:
        """The confirmation, on a page of its own, with a plain form on it.

        The same shape the plugin uninstall was rewritten into and for the same
        reason: a destructive action an operator has already decided on must
        not need a script to have loaded before it does anything.
        """
        directory = _found(engine, voice)
        metadata = read_voice_metadata(directory, engine=engine, voice=voice)
        label = f"{metadata.display} ({engine})"
        return templates.TemplateResponse(
            request=request,
            name="voice_remove.html",
            context={
                **await ctx.shell(request, "voices"),
                "engine": engine,
                "voice": voice,
                "title": REMOVE_TITLE.format(label=label),
                "body": REMOVE_BODY.format(voice=voice),
                "confirm_label": f"Remove {voice}",
                "keep_label": REMOVE_KEEP.format(label=metadata.display),
            },
        )

    @router.post(
        "/voice/voices/{engine}/{voice}/remove",
        response_class=HTMLResponse,
        summary="Remove one voice, deleting its folder",
    )
    async def voice_remove(request: Request, engine: str, voice: str) -> HTMLResponse:
        """Delete the folder, then show the list it is no longer in."""
        user = ctx.require_user(request)
        _found(engine, voice)
        try:
            await asyncio.to_thread(lambda: remove_voice(layout, engine=engine, voice=voice))
        except PackageRejected as exc:
            return await _answer(request, {"kind": "refused", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a locked file is a sentence
            return await _answer(request, {"kind": "refused", "message": str(exc)})
        await _record_change(
            ctx.audit,
            user,
            action="voice.remove",
            outcome=AuditOutcome.SUCCESS,
            detail={"engine": engine, "voice": voice},
        )
        return await _answer(
            request, {"kind": "ok", "message": REMOVED.format(label=f"{voice} ({engine})")}
        )


class _Refused(Exception):
    """One upload refusal, carrying the sentence the screen shows."""


def _megabytes(count: int) -> str:
    return f"{count / (1024 * 1024):.0f} MB"


def _wav(spoken: Any) -> bytes:
    """The engine's raw samples in a WAV container, for a browser to play.

    One line, because the header itself belongs in the core voice package
    (:func:`personacore.voice.engine.wav_bytes`): the reply path speaks
    through the same writer, and a second implementation here is how the two
    surfaces end up disagreeing about what a WAV is. ADR-0029 makes the
    framing decision core's rather than the engine's, and this is where core
    makes it — once.
    """
    return wav_bytes(spoken)
