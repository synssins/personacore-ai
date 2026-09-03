"""Voice packs on disk: install one from a zip, edit its metadata, export it back.

ADR-0029 §4 and PC-337: a voice arrives as **a zip uploaded through the admin
UI**, never as files copied into a directory over a shell. ``docs/voice-pack-
format.md`` is the format this module reads and writes.

Three things happen here, and they are one round trip:

**In.** The upload is unpacked and nothing about it is repackaged (PC-331). A
stock Piper voice — an ``.onnx`` and its JSON config, exactly as its author
published them — is a valid voice, and ``voice.toml`` is optional metadata
rather than a wrapper somebody has to write first.

**Edited.** The metadata a bare pair of files does not carry is filled in on the
manage screen and written back as ``voice.toml`` beside them.

**Out.** :func:`export_voice` zips the voice's own directory and generates the
``voice.toml`` the format specifies from that metadata. So a voice that arrived
as two loose files leaves as a properly formed pack, which is the whole point:
the pack is built *by* this process rather than required in front of it.

**The protections are the plugin installer's, reached rather than restated.**
``packages.py`` already refuses traversal in every spelling, symlinks,
non-regular members, absurd entry counts and both size ceilings, and it is
imported here — :func:`~personacore.plugins.packages._extract_safely` and the
member check inside it do the same work for a voice archive that they do for a
plugin package. A second copy of those rules is a second copy to keep correct,
and the one that drifts is the one nobody is reading. What is added here is the
rule the plugin format does not have: **a voice pack contains data and never
code**, so a pickle is refused by name (PC-259) and so is anything executable.

The name-before-validation bug is not reintroduced. A voice id is checked by
:func:`require_voice_id` — which reuses ``discovery``'s own unsafe-path
predicates — *before* it is joined onto anything, for the same reason
:func:`~personacore.plugins.packages._staging_folder_name` exists: ``Path.
__truediv__`` does not join an absolute path, it replaces everything to its
left with it.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import tomllib
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from secrets import token_hex
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from personacore.audit import get_logger
from personacore.config.appdata import AppdataError, AppdataLayout
from personacore.plugins.discovery import (
    _has_raw_drive_prefix,
    _has_raw_traversal_segment,
    _is_absolute_path_string,
)
from personacore.plugins.packages import (
    _IGNORED_TOP_LEVEL,
    _RESERVED_DEVICE_NAMES,
    PackageConflict,
    PackageInstallFailed,
    PackageInvalid,
    PackageLimits,
    PackageNotInstalled,
    PackageTooLarge,
    PackageUnsafe,
    PackageWords,
    _extract_safely,
    _human_bytes,
    _make_staging,
    _stage_archive,
)
from personacore.voice.engine import (
    ENGINE_ID_PATTERN as _ENGINE_ID_RE,
)
from personacore.voice.engine import (
    SYNTHESIS_FIELDS,
    VOICE_ID_RULE,
    model_refusal,
    synthesis_refusal,
)
from personacore.voice.engine import VOICE_ID_PATTERN as ENGINE_VOICE_ID_PATTERN
from personacore.voice.pacing import (
    CLAUSE_RATIO,
    DEFAULT_CLAUSE_MARKS,
    DEFAULT_SENTENCE_GAP_MS,
    DEFAULT_SENTENCE_MARKS,
    PACING_FIELDS,
    PACING_MARK_FIELDS,
    PACING_TABLE,
    PARAGRAPH_RATIO,
    marks_refusal,
    pacing_refusal,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# The format's own names (docs/voice-pack-format.md)
# ---------------------------------------------------------------------------

PACK_FORMAT_VERSION = 1
"""The format version this module reads and the one it writes."""

VOICE_METADATA_FILENAME = "voice.toml"
PRONUNCIATION_FILENAME = "pronunciation.json"
LICENCE_FILENAME = "LICENSE"
ATTRIBUTION_FILENAME = "ATTRIBUTION.md"
SAMPLE_FILENAME = "sample.wav"

PRONUNCIATION_FORMAT_VERSION = 1
DEFAULT_NOTATION = "ipa"

VOICE_LIMITS = PackageLimits(
    max_archive_bytes=256 * 1024 * 1024,
    max_uncompressed_bytes=512 * 1024 * 1024,
    max_entries=200,
)
"""The plugin installer's own ceilings, with a voice's numbers in them.

The type is reused so the checks are reused; only the values differ, because a
voice *is* a model — ADR-0029 puts one at roughly 60 MB — where a plugin is a
folder of code. The entry count goes the other way: a pack is a handful of
files, so 200 is generous and a thousand of them is not a voice.
"""

_VOICE_ID_RE = ENGINE_VOICE_ID_PATTERN
"""What a voice id may be: a directory name and a stable reference.

**Imported, not declared.** It used to be a second pattern here, and the two
disagreed about ``A-Z``: the engine discovered, listed and spoke a folder
called ``GLaDOS`` that this module could not see, so the voice was on the
Voices screen's engine and not on the screen — unexportable and unremovable
through the UI it was installed with. One rule now lives in
:data:`personacore.voice.engine.VOICE_ID_RULE`, which is the module both sides
already depend on, and this is that rule.

Lower case, because ``docs/voice-pack-format.md`` says so and because a name
that means two things on two filesystems is not a name. Dots, dashes and
underscores are in because real voices are punctuated like
``en_US-libritts_r-medium`` and a rule that refused the stock shape would be a
rule nobody could package against — the case is folded on the way in by
:func:`suggest_voice_id`, so such a voice installs as
``en_us-libritts_r-medium`` and keeps its own filenames untouched inside.
"""

VOICE_ID_PATTERN = VOICE_ID_RULE
"""The rule as a bare pattern string, unanchored, for a form field to use."""

ENGINE_ID_PATTERN = _ENGINE_ID_RE.pattern
"""ADR-0029's engine id: ``"vits-onnx"``. Stable, lowercase, never a voice name.

**Imported, not declared**, for the same reason as the voice id above. This
module carried its own copy allowing 64 characters while
:mod:`personacore.voice.engine` allowed 32 — one rule enforced in two places,
and when they drift it is the laxer half that decides what gets in while the
stricter half decides what is usable afterwards. That is precisely how a voice
called ``GLaDOS`` came to be speakable by the engine and invisible on the
screen. The engine's bound wins: the id is a directory name under
``appdata/voices/``, so the module that has to live with it owns the rule.
"""


# ---------------------------------------------------------------------------
# The rule the plugin format does not have: data, never code
# ---------------------------------------------------------------------------

PICKLE_SUFFIXES = frozenset(
    {".pkl", ".pickle", ".pck", ".pcl", ".dill", ".joblib", ".pt", ".pth", ".ckpt", ".npy", ".npz"}
)
"""File types that carry a pickle, and are refused by name (PC-259).

Unpickling runs whatever the file says to run, so a format that allowed one
would make installing a voice equivalent to running a stranger's program. The
torch checkpoint suffixes are here for exactly that reason — a ``.pth`` *is* a
pickle — and so are ``.npy``/``.npz``, which carry pickled objects whenever
they were written with ``allow_pickle``, a fact that cannot be established
without parsing the file.
"""

EXECUTABLE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyc",
        ".pyo",
        ".pyd",
        ".pyw",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".com",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".msi",
        ".scr",
        ".vbs",
        ".vbe",
        ".wsf",
        ".js",
        ".jse",
        ".jar",
        ".wasm",
        ".appimage",
    }
)
"""Code, in the shapes it arrives in. A voice does not need to run."""

NESTED_ARCHIVE_SUFFIXES = frozenset(
    {
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".tbz2",
        ".xz",
        ".txz",
        ".7z",
        ".rar",
        ".zst",
        ".lz4",
        ".whl",
        ".egg",
        ".apk",
        ".iso",
        ".cab",
    }
)
"""Nothing legitimate in a voice needs one, and they hide everything above."""

PICKLE_REFUSED = (
    "That voice was not installed: it contains {name}, a pickle — loading one "
    "runs arbitrary code. Convert it to JSON and zip it again."
)
"""ADR-0029's addendum, in the sentence an operator reads.

Names the file, because "the archive was rejected" is a refusal nobody can act
on. Says what to do about it in one clause and stops."""

EXECUTABLE_REFUSED = (
    "That voice was not installed: {name} is executable. A voice pack holds no executables."
)

NESTED_ARCHIVE_REFUSED = (
    "That voice was not installed: {name} is another archive. Zip the "
    "voice's files directly instead."
)

EXECUTABLE_BIT_REFUSED = (
    "That voice was not installed: {name} is marked executable in the archive. "
    "Clear the executable bit on the voice's files and zip it again."
)

SHEBANG_REFUSED = (
    "That voice was not installed: {name} begins with “#!” — a script, "
    "whatever it's named. A voice pack holds no code."
)


# ---------------------------------------------------------------------------
# Refusals — every message is shown to an operator verbatim
# ---------------------------------------------------------------------------


class VoiceContentRefused(PackageUnsafe):
    """The archive carried something a voice pack may not contain.

    A subclass of the installer's own unsafe-archive error rather than a new
    hierarchy beside it, so a caller that already catches
    :class:`~personacore.plugins.packages.PackageRejected` catches this too and
    nothing has to learn a second family of exceptions.
    """


VOICE_ARCHIVE_UNREADABLE = (
    "That file is not a readable zip archive. A voice pack is the voice's files, zipped."
)

VOICE_WORDS = PackageWords(
    noun="voice",
    package="voice pack",
    oversize_advice=(
        "A voice pack is a model, its configuration and a little text. Something "
        "this large is more than one voice."
    ),
    entry_count_advice=(
        "A voice pack is a handful of files: a model, its configuration and a little text."
    ),
    unreadable_advice="A voice pack is the voice's files, zipped.",
)
"""The installer's shared refusals, in a voice's words rather than a plugin's.

The checks themselves are the plugin installer's and are reached rather than
copied — that is the whole arrangement this module is built on. What travels
with the call is the noun, so an operator installing a voice is never told to
"zip the plugin folder" on a screen where no plugin exists (spec §9)."""

VOICE_ARCHIVE_EMPTY = (
    "That archive holds no files. Zip the voice's model and its configuration and try again."
)

VOICE_ID_UNUSABLE = (
    "“{raw}” cannot be a voice id. An id is 1–64 characters of lowercase "
    "letters, digits, dots, dashes and underscores, starting with a letter or "
    "a digit — no folder separators, drive letters or “..” segments."
)

VOICE_ID_RESERVED = (
    "“{raw}” is a name Windows reserves for a device, not a folder — give it a different id."
)

VOICE_ID_MISSING = (
    "That archive does not say what the voice is called, and no name was "
    "given. Type a name in the box beside the file and install it again."
)

ENGINE_MISMATCH = (
    "That pack says it is a voice for the {declared} engine, but it was being "
    "installed for {chosen}. Nothing was installed. Choose {declared} and "
    "install it again — or if this really is a {chosen} voice, edit the "
    "pack's voice.toml to say so."
)
"""The refusal that closes the round trip (PC-261).

A pack that declares ``[engine] id`` means it. Filing it under whatever the
upload form happened to be showing puts a working voice in a folder where it
will never be spoken, with no sentence anywhere saying why — which is exactly
what exporting a voice and reinstalling it used to do.
"""

ENGINE_UNSTATED = (
    "That pack does not say which engine speaks it, and no engine was "
    "chosen. Choose the engine beside the file and install it again."
)

ENGINE_ID_UNUSABLE = (
    "“{raw}” is not an engine id. An engine id is lowercase letters, digits and "
    "hyphens — the id the engine itself declares, such as “vits-onnx”."
)

VOICE_EXISTS = (
    "There is already a voice called “{voice}” on the {engine} engine. Tick "
    "“Replace” to overwrite it, or give this one a different id."
)

VOICE_NOT_INSTALLED = "There is no voice called “{voice}” on the {engine} engine."

EXPORT_SYMLINK_REFUSED = (
    "That voice cannot be exported: {name} is a symbolic link, which could "
    "package whatever it points at. Replace it with the real file."
)

EXPORT_ESCAPES = (
    "That voice cannot be exported: {name} resolves to somewhere outside the voice's own folder."
)

EXPORT_TOO_LARGE = (
    "That voice is larger than {limit} packaged — more than this core will "
    "build in memory. Nothing was written."
)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class InstalledVoice(BaseModel):
    """What one successful voice install put on disk."""

    model_config = ConfigDict(extra="forbid")

    engine: str
    voice: str
    directory: Path
    replaced: bool
    files: int
    bytes_written: int


class ExportedVoice(BaseModel):
    """One voice, packaged. ``data`` is the zip itself."""

    model_config = ConfigDict(extra="forbid")

    engine: str
    voice: str
    filename: str
    data: bytes
    #: Every name inside the zip, in the order it was written. The generated
    #: ``voice.toml`` is always among them; everything else came off disk.
    members: list[str]


class VoiceMetadata(BaseModel):
    """``voice.toml``'s fields, all of them optional except what identifies it.

    Optional because PC-331 says so: a stock Piper voice carries none of this
    and must install and speak anyway.

    ``None`` here and an empty string in the file are **the same state**, and
    every reader below is written so they stay the same: an exported pack
    always carries the complete set of fields, and the ones nobody filled in
    carry ``""``. So ``description = ""`` is a voice with no description — it
    installs, it works, and nothing renders an empty quoted string as though it
    were a name.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    engine: str

    name: str | None = None
    description: str | None = None
    language: str | None = None
    version: str | None = None
    engine_min_version: str | None = None

    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)

    length_scale: float | None = None
    noise_scale: float | None = None
    noise_w: float | None = None

    #: PC-342, the three gaps in milliseconds. ``None`` is the same "not set"
    #: every other field here uses: for the sentence gap that means the
    #: default, and for the other two it means "follow the ratio". **Zero is a
    #: value, not an empty box** -- it means no gap, which is the
    #: running-together the pacing was added to fix, kept reachable on purpose.
    sentence_gap_ms: int | None = None
    clause_gap_ms: int | None = None
    paragraph_gap_ms: int | None = None

    #: PC-342's marks: which characters end a sentence and which end a clause,
    #: as one string of characters each. ``None`` is "not set" and means the
    #: default set -- **not** "never break", which is what a zero gap is for.
    #: They are metadata rather than code so that a voice tuned by ear stays
    #: tuned when the pack is handed to somebody else.
    sentence_marks: str | None = None
    clause_marks: str | None = None

    licence_spdx: str | None = None
    licence_source: str | None = None
    author_name: str | None = None
    author_contact: str | None = None

    #: The sentence from :func:`~personacore.voice.engine.model_refusal`, when
    #: this voice's weights file is plainly not a model, or ``None``. Filled in
    #: by :func:`read_voice_metadata` so a pack that got past an older install
    #: (or was dropped in some other way) shows as dead on the list without
    #: anybody pressing play (spec §9).
    unsupported: str | None = None

    @property
    def display(self) -> str:
        """What a person sees. The id when nobody has named it — never blank,
        because a row with no name in it is a row nobody can click."""
        return self.name or self.id


# ---------------------------------------------------------------------------
# Names — checked before anything is joined onto anything
# ---------------------------------------------------------------------------


def _refuse_path_shaped(raw: str, message: str) -> None:
    """Refuse a string that is a path rather than a name.

    The predicates are ``discovery``'s own — the same three
    :mod:`~personacore.plugins.packages` uses — so "unsafe path string" has one
    definition in this package rather than two that agree until one is edited.
    """
    if (
        not raw
        or "/" in raw
        or "\\" in raw
        or "\x00" in raw
        or raw in (".", "..")
        or _is_absolute_path_string(raw)
        or _has_raw_drive_prefix(raw)
        or _has_raw_traversal_segment(raw)
    ):
        raise VoiceContentRefused(message)


def require_voice_id(raw: str) -> str:
    """A voice id, refused rather than repaired.

    Called **before** the id reaches a join. That ordering is the whole point:
    the plugin installer once built a path out of a name the archive supplied
    and validated it afterwards, and an absolute value walked out of the join
    taking the extraction with it. Nothing here is fixed up into something
    plausible — a name that is not a voice id is a sentence, not a guess.
    """
    value = (raw or "").strip()
    _refuse_path_shaped(value, VOICE_ID_UNUSABLE.format(raw=raw))
    if value.split(".")[0].lower() in _RESERVED_DEVICE_NAMES:
        raise PackageInvalid(VOICE_ID_RESERVED.format(raw=raw))
    if not _VOICE_ID_RE.fullmatch(value):
        raise VoiceContentRefused(VOICE_ID_UNUSABLE.format(raw=raw))
    return value


def require_engine_id(raw: str) -> str:
    """An engine id, on the same terms and for the same reason."""
    value = (raw or "").strip()
    _refuse_path_shaped(value, ENGINE_ID_UNUSABLE.format(raw=raw))
    if not _ENGINE_ID_RE.fullmatch(value):
        raise VoiceContentRefused(ENGINE_ID_UNUSABLE.format(raw=raw))
    return value


def suggest_voice_id(raw: str) -> str:
    """A *likely* voice id out of a filename or a folder name, or ``""``.

    Only ever used to fill a box the operator can see and correct, and its
    output goes through :func:`require_voice_id` like anything else — this
    function makes a plausible name, that one makes a safe one, and the order
    is what matters (the same split :func:`~personacore.web.screens.
    personas.persona_slug` makes for a persona).
    """
    stem = PurePosixPath((raw or "").replace("\\", "/")).name
    for suffix in (".zip", ".onnx", ".json"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    lowered = re.sub(r"[^a-z0-9._-]+", "-", stem.strip().lower())
    return lowered.strip("-._")[:64]


# ---------------------------------------------------------------------------
# Content refusals
# ---------------------------------------------------------------------------


def _basename(raw: str) -> str:
    return PurePosixPath(raw.replace("\\", "/")).name


def refuse_forbidden_names(names: Iterable[str]) -> None:
    """Refuse a pack that carries code, a pickle or another archive.

    One function, called twice: over the archive's own member names *before*
    anything is written, so a hostile pack never reaches disk, and again over
    what actually landed. Two call sites, one rule — which is the same reason
    the traversal and symlink checks are imported rather than repeated.
    """
    for raw in names:
        base = _basename(raw)
        if not base:
            continue
        suffix = PurePosixPath(base.lower()).suffix
        if suffix in PICKLE_SUFFIXES:
            raise VoiceContentRefused(PICKLE_REFUSED.format(name=base))
        if suffix in EXECUTABLE_SUFFIXES:
            raise VoiceContentRefused(EXECUTABLE_REFUSED.format(name=base))
        if suffix in NESTED_ARCHIVE_SUFFIXES:
            raise VoiceContentRefused(NESTED_ARCHIVE_REFUSED.format(name=base))


def _refuse_executable_bits(members: Iterable[zipfile.ZipInfo]) -> None:
    """Refuse a file the archive recorded as executable.

    ``docs/voice-pack-format.md`` lists "or any executable bit" beside the
    suffixes, and it is the half a rename defeats the suffix list with. Only
    regular files are examined and only when the archive stored a unix mode at
    all — a directory is executable by definition, and a zip written on Windows
    stores FAT attributes and no mode, which must not read as a refusal.
    """
    for member in members:
        if member.is_dir():
            continue
        mode = member.external_attr >> 16
        if not mode:
            continue
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            continue
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise VoiceContentRefused(
                EXECUTABLE_BIT_REFUSED.format(name=_basename(member.filename))
            )


def _refuse_shebangs(root: Path) -> None:
    """Refuse an extracted file that begins ``#!``.

    The suffix list cannot see a script somebody called ``phonemes`` with no
    extension at all. Two bytes per file answers that, and it runs on what
    landed rather than on what the archive claimed.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as handle:
                if handle.read(2) == b"#!":
                    raise VoiceContentRefused(SHEBANG_REFUSED.format(name=path.name))
        except OSError:  # pragma: no cover - the file was written moments ago
            continue


def _unsupported_reason(directory: Path) -> str | None:
    """:func:`~personacore.voice.engine.model_refusal` over one voice's weights.

    Shared by the installer, which raises on the answer, and
    :func:`read_voice_metadata`, which shows it. The weights file is
    identified the same way :func:`model_paths` identifies it for export — a
    second way of finding which file is the weights would be a second thing
    to keep in step. A pack with no identifiable weights file at all answers
    ``None`` here: that is the PC-331 shape (a voice as two loose files
    nobody has named yet), and it is not this function's business.
    """
    weights = model_paths(directory).get("weights")
    if weights is None:
        return None
    return model_refusal(directory / weights)


def _refuse_unsupported_model(payload: Path) -> None:
    """Refuse a pack whose weights file is plainly not a model.

    The installer's half of :func:`_unsupported_reason` — a web page saved
    from a Hugging Face ``/blob/`` link instead of the model it links to
    (PC-259's sibling problem) unpacks fine and is not unsafe, so it is
    refused here rather than by :class:`VoiceContentRefused`.
    """
    refusal = _unsupported_reason(payload)
    if refusal is not None:
        raise PackageInvalid(refusal)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_voice(
    layout: AppdataLayout,
    package: bytes | bytearray | memoryview | str | Path,
    *,
    engine: str | None = None,
    voice_id: str | None = None,
    replace: bool = False,
    limits: PackageLimits = VOICE_LIMITS,
) -> InstalledVoice:
    """Install one uploaded voice pack into ``voices/<engine>/<voice-id>/``.

    **Which engine is the pack's answer when it gives one.** ``[engine] id`` is
    a required field of the format and it names the only engine that can speak
    these files, so a pack that declares one is installed as what it is:
    ``engine`` may be left out entirely and an exported pack still reinstalls
    where it belongs, which is the round trip PC-337 asks for. A pack that
    declares nothing is installed as whatever the form chose — a stock Piper
    download carries no ``voice.toml`` at all and that must keep working
    (PC-331). The two disagreeing is a refusal naming both, never a silent move:
    a voice filed under the wrong engine is a voice that will not speak, and
    the operator would have no way to learn why.

    The order is the plugin installer's, because the reasons are the same:

    1. Check both ids **before** either becomes part of a path.
    2. Stage the upload inside appdata, never a shared temp location.
    3. Refuse a pack carrying code, a pickle or a nested archive — before a
       byte is written.
    4. Extract through :func:`~personacore.plugins.packages._extract_safely`,
       which refuses traversal in every spelling, symlinks, non-regular
       members, and both size ceilings.
    5. Check what landed, then move it into place.

    Nothing in the archive is opened as code at any step, and the contents are
    not repackaged: what came out of the zip is what lands in the voice's
    folder (PC-331).

    Raises:
        PackageRejected: with a sentence naming the problem. Nothing was
            installed and the staging directory is gone.
        PackageInstallFailed: the volume could not be written.
    """
    chosen_engine = require_engine_id(engine) if engine else None
    wanted = require_voice_id(voice_id) if voice_id else None

    voices_root = layout.voices
    try:
        voices_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PackageInstallFailed(
            f"The voices directory {voices_root} could not be created: "
            f"{exc.strerror or exc}. Check the appdata volume is mounted and writable."
        ) from exc

    staging = _make_staging(voices_root)
    try:
        archive = _stage_archive(staging, package, limits, VOICE_WORDS)
        _screen_archive(archive)

        extract_root = staging / "extract"
        extract_root.mkdir()
        extraction = _extract_safely(archive, extract_root, limits, VOICE_WORDS)

        payload = _payload_root(extract_root)
        landed = [path for path in sorted(payload.rglob("*")) if path.is_file()]
        if not landed:
            raise PackageInvalid(VOICE_ARCHIVE_EMPTY)
        refuse_forbidden_names(path.name for path in landed)
        _refuse_shebangs(payload)
        _refuse_unsupported_model(payload)
        # The voice's own files, which is what the folder will hold. Never the
        # archive's tally: the extraction root can also hold packaging debris
        # beside the pack (``__MACOSX``), and a number that counts files the
        # voice folder does not have is a claim about something that is not
        # there (D1).
        installed_files = len(landed)

        # The pack's own answers, read once, before either becomes a path.
        document = _pack_metadata(payload / VOICE_METADATA_FILENAME)
        engine_name = _engine_for(document, chosen_engine)
        chosen = wanted or _derived_voice_id(document, payload, extract_root)
        target = _voice_directory(layout, engine_name, chosen)
        replaced = _move_into_place(target, payload, replace=replace, engine=engine_name)
    finally:
        # Every path, including failure: a staging directory left behind is an
        # unvalidated archive sitting inside appdata.
        shutil.rmtree(staging, ignore_errors=True)

    logger.info(
        "voice_installed",
        engine=engine_name,
        voice=chosen,
        files=installed_files,
        replaced=replaced,
    )
    return InstalledVoice(
        engine=engine_name,
        voice=chosen,
        directory=target,
        replaced=replaced,
        files=installed_files,
        bytes_written=extraction.bytes_written,
    )


def _screen_archive(archive: Path) -> None:
    """The content rules, read off the archive's own listing.

    Deliberately a pass of its own in front of the extraction rather than a
    branch inside it: a pack carrying a pickle is refused with **nothing
    written anywhere**, which is a stronger promise than deleting it
    afterwards, and it leaves ``_extract_safely`` exactly as the plugin
    installer has it.
    """
    try:
        with zipfile.ZipFile(archive) as zipped:
            members = zipped.infolist()
    except zipfile.BadZipFile as exc:
        raise PackageInvalid(VOICE_ARCHIVE_UNREADABLE) from exc
    except OSError as exc:  # pragma: no cover - written moments ago
        raise PackageInstallFailed(
            f"The uploaded archive could not be read: {exc.strerror or exc}."
        ) from exc
    refuse_forbidden_names(member.filename for member in members)
    _refuse_executable_bits(members)


def _payload_root(extract_root: Path) -> Path:
    """Where the voice's files are: the extraction root, or the one folder in it.

    Both shapes are ordinary. A voice downloaded as two loose files and zipped
    has them at the top; one zipped from its folder has them one level down.
    Requiring either would be requiring the operator to repackage, which is the
    thing PC-331 exists to prevent.
    """
    entries = [
        path
        for path in extract_root.iterdir()
        if path.name not in _IGNORED_TOP_LEVEL and not path.name.startswith("._")
    ]
    directories = [path for path in entries if path.is_dir()]
    files = [path for path in entries if path.is_file()]
    if len(directories) == 1 and not files:
        return directories[0]
    return extract_root


def _pack_metadata(metadata: Path) -> dict[str, Any]:
    """The pack's ``voice.toml``, parsed, or ``{}``.

    Read **once** per install, before any of its strings is joined onto a path,
    and never treated as an error: the file is optional (PC-331), so a broken
    one must not be the thing that stops a voice installing. What is wrong with
    it shows on the manage screen, where it can be fixed.
    """
    if not metadata.is_file():
        return {}
    try:
        with metadata.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _declared(document: dict[str, Any], table: str, key: str) -> str | None:
    """One declared string out of the pack, or ``None`` when it says nothing."""
    block = document.get(table)
    value = block.get(key) if isinstance(block, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _engine_for(document: dict[str, Any], chosen: str | None) -> str:
    """Which engine this pack is filed under: what it declares, or what was chosen.

    The format makes ``[engine] id`` required and calls it "which engine plugin
    can speak this voice", so a pack that states one is stating a fact about
    its own files rather than expressing a preference. It therefore wins, and
    ``chosen`` only decides when the pack is silent — which is the ordinary
    case of a stock Piper download with no ``voice.toml`` in it at all.

    The two disagreeing is refused rather than resolved. Either answer would be
    a guess, and the wrong guess files a voice where its engine will never look
    for it — the operator sees an installed voice that cannot speak and nothing
    anywhere says why. The declared id goes through
    :func:`require_engine_id` like every other untrusted name, before it is
    joined onto anything.
    """
    declared = _declared(document, "engine", "id")
    if declared is None:
        if chosen is None:
            raise PackageInvalid(ENGINE_UNSTATED)
        return chosen
    engine = require_engine_id(declared)
    if chosen is not None and chosen != engine:
        raise PackageConflict(ENGINE_MISMATCH.format(declared=engine, chosen=chosen))
    return engine


def _derived_voice_id(document: dict[str, Any], payload: Path, extract_root: Path) -> str:
    """The voice's id when the operator did not type one.

    ``voice.toml`` first, because a pack that states its id means it; then the
    folder the archive wrapped it in. Both are untrusted strings and both go
    through :func:`require_voice_id` before anything is joined onto them.
    Nothing is guessed from the model filename: an id is a persona's stored
    reference, and one invented out of ``en_US-x-medium.onnx`` would be a
    reference nobody chose.
    """
    declared = _declared(document, "voice", "id")
    if declared:
        return require_voice_id(declared)
    if payload != extract_root:
        suggested = suggest_voice_id(payload.name)
        if suggested:
            return require_voice_id(suggested)
    raise PackageInvalid(VOICE_ID_MISSING)


def _voice_directory(layout: AppdataLayout, engine: str, voice: str) -> Path:
    """The folder one voice owns, with containment proved rather than assumed.

    Both ids are already through their own check, so the join below cannot be
    re-anchored by either. The containment check after it is the belt: it is
    what catches an appdata root that is itself a link into somewhere else, and
    it costs one ``resolve``.
    """
    engine_root = layout.voices / engine
    candidate = engine_root / voice
    try:
        layout.require_inside(candidate.parent, what=f"The folder for engine {engine!r}")
    except AppdataError as exc:
        raise VoiceContentRefused(str(exc)) from exc
    return candidate


def _move_into_place(target: Path, payload: Path, *, replace: bool, engine: str) -> bool:
    """Put the validated folder where it belongs, keeping the old one until it
    is certain the new one arrived.

    The replaced folder is set aside as a sibling with a leading dot rather
    than deleted, and only removed once the move has succeeded — an upgrade
    that half-finished must not be the thing that loses a 60 MB voice somebody
    downloaded once (spec §7).
    """
    exists = target.exists()
    if exists and not replace:
        raise PackageConflict(VOICE_EXISTS.format(voice=target.name, engine=engine))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PackageInstallFailed(
            f"The folder for engine {engine!r} could not be created: {exc.strerror or exc}."
        ) from exc

    backup = target.parent / f".replaced-{token_hex(4)}"
    try:
        if exists:
            os.replace(target, backup)
        shutil.move(str(payload), str(target))
    except OSError as exc:
        if exists and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise PackageInstallFailed(
            f"The voice could not be moved into {target}: {exc.strerror or exc}. "
            "Check there is free space on the appdata volume."
        ) from exc
    finally:
        if backup.exists() and target.exists():
            shutil.rmtree(backup, ignore_errors=True)
    return exists


# ---------------------------------------------------------------------------
# What is installed
# ---------------------------------------------------------------------------


def voice_directory(layout: AppdataLayout, engine: str, voice: str) -> Path:
    """One installed voice's folder, checked, or a refusal.

    The public door onto a voice's files. Every screen that names a voice comes
    through here, so "which folder does this id mean" has one answer.
    """
    directory = _voice_directory(layout, require_engine_id(engine), require_voice_id(voice))
    if not directory.is_dir():
        raise PackageNotInstalled(VOICE_NOT_INSTALLED.format(voice=voice, engine=engine))
    if directory.is_symlink():
        raise VoiceContentRefused(
            f"The folder for “{voice}” is a symbolic link rather than a real "
            "folder, so the core will not read through it."
        )
    return directory


def installed_voices(layout: AppdataLayout) -> list[VoiceMetadata]:
    """Every voice on disk, across every engine, in one list.

    ADR-0029 §5: the operator sees one list and the engine is a label on it.
    Sorted by display name so the ordering is the one a person reads by.

    **A folder that cannot be read is skipped, never fatal.** One broken voice
    must not cost an operator the other nine, and must never be what stops the
    core starting — the lockout class this project has produced three times.
    """
    root = layout.voices
    found: list[VoiceMetadata] = []
    if not root.is_dir():
        return found
    for engine_dir in sorted(root.iterdir()):
        if not engine_dir.is_dir() or engine_dir.name.startswith((".", "_")):
            continue
        if not _ENGINE_ID_RE.fullmatch(engine_dir.name):
            continue
        for voice_dir in sorted(engine_dir.iterdir()):
            if not voice_dir.is_dir() or voice_dir.name.startswith((".", "_")):
                continue
            if not _VOICE_ID_RE.fullmatch(voice_dir.name):
                continue
            found.append(
                read_voice_metadata(voice_dir, engine=engine_dir.name, voice=voice_dir.name)
            )
    return sorted(found, key=lambda item: (item.display.lower(), item.engine))


def voice_files(directory: Path) -> list[str]:
    """The voice's own files, as relative posix paths, for a listing."""
    return [
        path.relative_to(directory).as_posix()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


# ---------------------------------------------------------------------------
# Metadata — read tolerantly, written in the documented shape
# ---------------------------------------------------------------------------


def _string(table: Any, key: str) -> str | None:
    """One text field, where **an empty string and an absent key are the same**.

    That equivalence is the format's, not a convenience here: an exported pack
    carries every field the format defines and writes ``""`` for the ones
    nobody filled in, so a reader that told the two apart would make a complete
    template behave differently from a sparse one. Whitespace goes the same
    way — a field holding a space was not filled in either.
    """
    if not isinstance(table, dict):
        return None
    value = table.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(table: Any, key: str) -> float | None:
    """One numeric field, on the same terms.

    A number written as ``""`` in a template nobody completed is not a number,
    and is read as "not set" rather than as a zero or a refusal.
    """
    if not isinstance(table, dict):
        return None
    value = table.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _positive_int(table: Any, key: str) -> int | None:
    value = _number(table, key)
    if value is None or value <= 0:
        return None
    return int(value)


def _gap(table: Any, key: str) -> int | None:
    """One pacing gap in milliseconds, where **zero is a value** (PC-342).

    Not :func:`_positive_int`: zero means no gap and is legal in all three, so
    a reader that folded it into "not set" would make the one way of switching
    pacing off silently mean the opposite. Blank and absent stay the same state
    as everywhere else in this file, and a negative figure is not a length of
    silence, so it is read as not set and the form's refusal is what an
    operator sees.
    """
    value = _number(table, key)
    if value is None or value < 0:
        return None
    return int(value)


def _model_sample_rate(directory: Path) -> int | None:
    """The rate the model itself declares, from its own JSON config.

    The screen shows this as a fact rather than a setting, with a note saying it
    is "the model's own figure, from its configuration". It was reading
    ``voice.toml`` instead, which a voice installed as bare files does not have
    -- so the field said "-" forever under a note promising otherwise, and being
    read-only, nothing could ever fill it in. A displayed fact that is never
    populated is worse than no field.

    Piper puts it under ``audio.sample_rate``. Never raises: an unreadable or
    surprising config leaves the field absent, which is the state it was in
    anyway.
    """
    try:
        for candidate in sorted(directory.glob("*.json")):
            if candidate.name == VOICE_METADATA_FILENAME:
                continue
            with candidate.open("rb") as handle:
                document = json.load(handle)
            if not isinstance(document, dict):
                continue
            rate = _positive_int(document.get("audio"), "sample_rate")
            if rate is None:
                rate = _positive_int(document, "sample_rate")
            if rate is not None:
                return rate
    except (OSError, ValueError, TypeError):
        return None
    return None


def read_voice_metadata(directory: Path, *, engine: str, voice: str) -> VoiceMetadata:
    """One voice's ``voice.toml``, or the empty metadata a bare voice has.

    Never raises. The file is optional (PC-331) and a broken one is a thing to
    show and fix rather than a thing that hides a voice: every field that
    cannot be read is simply absent, which is the same state as never having
    been filled in.
    """
    unsupported = _unsupported_reason(directory)
    empty = VoiceMetadata(
        id=voice,
        engine=engine,
        sample_rate=_model_sample_rate(directory),
        unsupported=unsupported,
    )
    path = directory / VOICE_METADATA_FILENAME
    if not path.is_file():
        return empty
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return empty

    block = document.get("voice")
    audio = document.get("audio")
    synthesis = document.get("synthesis")
    pacing = document.get(PACING_TABLE)
    licence = document.get("licence") or document.get("license")
    author = document.get("author")
    return VoiceMetadata(
        # The folder is the identity, never the file: a ``voice.toml`` that
        # names a different id is describing a voice that is not in this
        # folder, and following it would move the operator's reference.
        id=voice,
        engine=engine,
        name=_string(block, "name"),
        description=_string(block, "description"),
        language=_string(block, "language"),
        version=_string(block, "version"),
        engine_min_version=_string(document.get("engine"), "min_version"),
        sample_rate=_positive_int(audio, "sample_rate") or _model_sample_rate(directory),
        channels=_positive_int(audio, "channels"),
        length_scale=_number(synthesis, "length_scale"),
        noise_scale=_number(synthesis, "noise_scale"),
        noise_w=_number(synthesis, "noise_w"),
        sentence_gap_ms=_gap(pacing, "sentence_gap_ms"),
        clause_gap_ms=_gap(pacing, "clause_gap_ms"),
        paragraph_gap_ms=_gap(pacing, "paragraph_gap_ms"),
        # Read as written, not as cleaned: this is the record of what the pack
        # says, and narrowing an odd character set is
        # :func:`personacore.voice.pacing.clean_marks`'s job at the moment the
        # splitter is built. Cleaning here would silently rewrite an operator's
        # box the next time they saved the page.
        sentence_marks=_string(pacing, "sentence_marks"),
        clause_marks=_string(pacing, "clause_marks"),
        licence_spdx=_string(licence, "spdx"),
        licence_source=_string(licence, "source"),
        author_name=_string(author, "name"),
        author_contact=_string(author, "contact"),
        unsupported=unsupported,
    )


MODEL_WEIGHT_SUFFIXES = (".onnx", ".safetensors", ".model", ".pb", ".tflite")
"""What ``[model] weights`` is likely to be, for a pack that never stated it.

Only used to *describe* files that are already in the folder — the core never
loads a model, and an engine that disagrees reads its own directory anyway
(ADR-0029's interface). A voice whose weights cannot be identified simply gets
no ``[model]`` table, because an invented path is worse than a missing one.
"""

_METADATA_FILENAMES = frozenset(
    {
        VOICE_METADATA_FILENAME,
        PRONUNCIATION_FILENAME,
        LICENCE_FILENAME,
        ATTRIBUTION_FILENAME,
        SAMPLE_FILENAME,
    }
)


def model_paths(directory: Path) -> dict[str, str]:
    """``[model]``'s paths for one voice, from the files actually present.

    Relative to the pack root and inside it, which is what the format requires
    of them. **The layout is not rearranged**: a voice that arrived as two
    loose files is exported as two loose files with paths that point at them,
    because moving them into a ``model/`` folder would be repackaging somebody
    else's voice on its way out.
    """
    names = [name for name in voice_files(directory) if name not in _METADATA_FILENAMES]
    weights = next(
        (name for name in names if PurePosixPath(name.lower()).suffix in MODEL_WEIGHT_SUFFIXES),
        None,
    )
    paths: dict[str, str] = {}
    if weights is None:
        return paths
    paths["weights"] = weights
    config = next(
        (name for name in names if name.lower().startswith(weights.lower()) and name != weights),
        None,
    ) or next((name for name in names if name.lower().endswith(".json")), None)
    if config is not None:
        paths["config"] = config
    return paths


DOCUMENT_HEADER = (
    "# A PersonaCore voice pack, format version 1 — docs/voice-pack-format.md.\n"
    "#\n"
    "# Every field the format defines is here, whether or not it holds anything.\n"
    "# An empty string means it was not set: the pack is still valid, still\n"
    "# installs and still speaks. Fill in what you know and leave the rest."
)
"""Why the file looks like this, at the top of every one the core writes.

A template that only carried the fields somebody happened to fill in would
teach the next packager exactly those fields and hide the others behind a
document they would have to go and find. A full one describes the format
wherever it travels."""


def _toml_value(value: object) -> str:
    """One value, as TOML. ``None`` is the empty string — the format's "not set".

    Written here rather than through ``tomli_w`` because this document carries
    comments and ``tomli_w`` does not emit them, and a template with a bare
    ``language = ""`` in it is a template that gets filled in wrongly.
    """
    if value is None or value == "":
        return '""'
    if isinstance(value, bool):  # pragma: no cover - no boolean field today
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_table(name: str, rows: list[tuple[str, object, str]], *, note: str = "") -> str:
    """One table, keys aligned, with the short comment each row earns.

    The comment is on the rows that need one — what a tag looks like, what the
    number does — and absent on the rows that do not, because a comment beside
    every line is a file people stop reading.
    """
    width = max((len(key) for key, _value, _hint in rows), default=0)
    lines = [f"[{name}]"]
    if note:
        lines.extend(f"# {line}" for line in note.splitlines())
    for key, value, hint in rows:
        rendered = f"{key.ljust(width)} = {_toml_value(value)}"
        lines.append(f"{rendered}  # {hint}" if hint else rendered)
    return "\n".join(lines)


def metadata_document(metadata: VoiceMetadata, *, directory: Path | None = None) -> str:
    """``voice.toml`` as ``docs/voice-pack-format.md`` specifies it, in full.

    **Every field is always present.** One nobody filled in carries ``""``,
    which the readers above treat as identical to the key being absent — so a
    voice that arrived as two bare files exports as a complete, self-describing
    template with its id, its engine and its file paths in it and empty strings
    everywhere else. That template is the thing a person edits to turn a
    scratch voice into a distributable one, which is the round trip this module
    exists for.

    The id is the one exception. It is required, it is a directory name, and it
    cannot be empty.
    """
    paths = model_paths(directory) if directory is not None else {}
    has_licence_file = directory is not None and (directory / LICENCE_FILENAME).is_file()
    has_attribution = directory is not None and (directory / ATTRIBUTION_FILENAME).is_file()

    blocks = [
        DOCUMENT_HEADER,
        f"format_version = {PACK_FORMAT_VERSION}",
        _toml_table(
            "voice",
            [
                (
                    "id",
                    metadata.id,
                    "a directory name and a stable reference: it never changes",
                ),
                ("name", metadata.name, "what a person sees in the voice list"),
                ("description", metadata.description, ""),
                ("language", metadata.language, 'BCP 47 — "en-US", "de-DE"'),
                ("version", metadata.version, "semver, this pack's version"),
            ],
        ),
        _toml_table(
            "engine",
            [
                (
                    "id",
                    metadata.engine,
                    "which engine speaks this voice",
                ),
                (
                    "min_version",
                    metadata.engine_min_version,
                    "oldest engine release this pack works with",
                ),
            ],
        ),
        _toml_table(
            "audio",
            [
                (
                    "sample_rate",
                    metadata.sample_rate,
                    "Hz, as the model produces. Wrong here is a chipmunk.",
                ),
                ("channels", metadata.channels, ""),
            ],
        ),
        _toml_table(
            "model",
            [
                ("weights", paths.get("weights"), ""),
                ("config", paths.get("config"), ""),
            ],
            note=(
                "Paths are relative to this file and stay inside the pack.\n"
                "The key names are the engine's business."
            ),
        ),
        _toml_table(
            "synthesis",
            [
                ("length_scale", metadata.length_scale, "speed — higher is slower"),
                ("noise_scale", metadata.noise_scale, "expressiveness"),
                ("noise_w", metadata.noise_w, "pitch variance"),
            ],
            note="Defaults an operator may override; the engine validates ranges.",
        ),
        _toml_table(
            PACING_TABLE,
            [
                (
                    "sentence_gap_ms",
                    metadata.sentence_gap_ms,
                    f"silence after a full stop. Empty is {DEFAULT_SENTENCE_GAP_MS}; 0 is none",
                ),
                (
                    "clause_gap_ms",
                    metadata.clause_gap_ms,
                    "at a clause mark below. Empty follows the sentence gap",
                ),
                (
                    "paragraph_gap_ms",
                    metadata.paragraph_gap_ms,
                    "between paragraphs. Empty follows the sentence gap",
                ),
                (
                    "sentence_marks",
                    metadata.sentence_marks,
                    f"characters that end a sentence. Empty is {DEFAULT_SENTENCE_MARKS}",
                ),
                (
                    "clause_marks",
                    metadata.clause_marks,
                    f"characters that end a clause. Empty is {DEFAULT_CLAUSE_MARKS}",
                ),
            ],
            note="\n".join(
                (
                    "How this voice is paced (PC-342). The core splits the text and puts",
                    "the silence in; an engine never does. Clause is "
                    f"{CLAUSE_RATIO:g}x the sentence gap and paragraph is",
                    f"{PARAGRAPH_RATIO:g}x unless a figure is written here, so tuning "
                    "the one number keeps",
                    "the shape. 0 in any of them is no gap.",
                    "",
                    "The two mark lists REPLACE the defaults above, so removing a mark is",
                    'writing the ones you want: clause_marks = "—–;" is this voice without',
                    "the comma break. Empty is the default set, never no breaks at all --",
                    "that is a 0 gap. An empty line is always a paragraph and never a mark.",
                )
            ),
        ),
        _toml_table(
            "licence",
            [
                ("spdx", metadata.licence_spdx, 'SPDX identifier — "CC-BY-4.0", "MIT"'),
                (
                    "file",
                    LICENCE_FILENAME if has_licence_file else None,
                    "a file in this pack",
                ),
                (
                    "attribution",
                    ATTRIBUTION_FILENAME if has_attribution else None,
                    "a file in this pack, when the licence needs one",
                ),
                ("source", metadata.licence_source, "where this voice came from"),
            ],
            note="A voice model is someone's work. Say what may be done with it.",
        ),
        _toml_table(
            "author",
            [
                ("name", metadata.author_name, ""),
                ("contact", metadata.author_contact, ""),
            ],
        ),
    ]
    return "\n\n".join(blocks) + "\n"


def refuse_unusable_synthesis(metadata: VoiceMetadata) -> None:
    """Refuse a synthesis default no engine could speak with, naming the field.

    ``docs/voice-pack-format.md`` says the ranges are the engine's, and they
    are: the numbers come from
    :data:`personacore.voice.engine.SYNTHESIS_LIMITS`, which is the table the
    ``vits-onnx`` engine enforces. What happens *here* is only when the
    operator is told — and that has to be while the form is still on screen,
    because the alternative is a value written to disk, clamped hours later in
    a log line nobody is reading, and a voice that does not sound like the
    number in the box.

    An empty box is not a value: leaving a field blank means "use the model's
    own default" and is always allowed.
    """
    for field_name in SYNTHESIS_FIELDS:
        refusal = synthesis_refusal(field_name, getattr(metadata, field_name, None))
        if refusal is not None:
            raise PackageInvalid(refusal)


def refuse_unusable_pacing(metadata: VoiceMetadata) -> None:
    """Refuse a gap no voice could be paced by, naming the field (PC-342).

    The synthesis defaults' own rule, applied to the three numbers beside them:
    refused at save with a sentence while the form is still on screen, and
    clamped with a note when somebody hand-edits the file
    (:func:`personacore.voice.pacing.read_pacing`). Matching that rather than
    inventing a second one is the point -- these boxes sit on the same form.

    An empty box is not a value. Zero **is** one, it is legal, and it means no
    gap.

    The two mark lists are checked here too, by their own validator: an empty
    one is the default set and is always fine, and a letter in one is the
    mistake it looks like rather than a voice that stops mid-word.
    """
    for field_name in PACING_FIELDS:
        refusal = pacing_refusal(field_name, getattr(metadata, field_name, None))
        if refusal is not None:
            raise PackageInvalid(refusal)
    for field_name in PACING_MARK_FIELDS:
        refusal = marks_refusal(field_name, getattr(metadata, field_name, None))
        if refusal is not None:
            raise PackageInvalid(refusal)


def write_voice_metadata(directory: Path, metadata: VoiceMetadata) -> Path:
    """Write ``voice.toml`` into an installed voice's folder.

    This is the manage screen's half of the round trip: the metadata a stock
    voice does not carry is typed once and lives beside the files it describes,
    so the next export produces a complete pack.

    The synthesis defaults are checked before anything is written, so a voice
    keeps the settings it had rather than half-taking a value that cannot be
    spoken with.
    """
    refuse_unusable_synthesis(metadata)
    refuse_unusable_pacing(metadata)
    path = directory / VOICE_METADATA_FILENAME
    try:
        path.write_text(metadata_document(metadata, directory=directory), encoding="utf-8")
    except OSError as exc:
        raise PackageInstallFailed(
            f"The voice's details could not be written to {path}: {exc.strerror or exc}. "
            "Check the appdata volume is writable."
        ) from exc
    return path


def write_text_file(directory: Path, filename: str, text: str) -> bool:
    """Write one of the pack's plain-text files, or remove it when emptied.

    ``LICENSE`` and ``ATTRIBUTION.md`` are files in the format and boxes on the
    screen, which is what lets a pack be completed without shell access.
    Clearing the box removes the file rather than leaving an empty one, because
    an empty ``LICENSE`` reads as a licence.
    """
    path = directory / filename
    body = text.strip()
    try:
        if not body:
            path.unlink(missing_ok=True)
            return False
        path.write_text(body + "\n", encoding="utf-8")
    except OSError as exc:
        raise PackageInstallFailed(
            f"{filename} could not be written: {exc.strerror or exc}."
        ) from exc
    return True


def read_text_file(directory: Path, filename: str) -> str:
    """One of the pack's plain-text files, or ``""``."""
    path = directory / filename
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable in the moment between calls
        return ""


# ---------------------------------------------------------------------------
# Pronunciation — the pack's, not the core's
# ---------------------------------------------------------------------------

PRONUNCIATION_LINE_REFUSED = (
    "Line {number} of the pronunciations has no “=” in it. Each line is one "
    "word, an equals sign, then how it is said: glados = ɡlˈɑːdɑːs"
)

MAX_PRONUNCIATION_ENTRIES = 2000
"""A lexicon is corrections, not a dictionary. Something arriving from outside
gets a bound before it gets a use (spec §7)."""

PRONUNCIATION_TOO_MANY = (
    f"That is more than {MAX_PRONUNCIATION_ENTRIES} pronunciations. Nothing was written."
)


def read_pronunciation(directory: Path) -> tuple[str, str]:
    """The lexicon as ``word = sound`` lines, and the notation it is written in.

    Lines rather than JSON on the screen because the operator is correcting how
    a word sounds, not editing a document, and a misplaced brace should not be
    able to cost them the file.
    """
    path = directory / PRONUNCIATION_FILENAME
    if not path.is_file():
        return "", DEFAULT_NOTATION
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", DEFAULT_NOTATION
    if not isinstance(document, dict):
        return "", DEFAULT_NOTATION
    notation = document.get("notation")
    entries = document.get("entries")
    lines = (
        [
            f"{word} = {sound}"
            for word, sound in entries.items()
            if isinstance(word, str) and isinstance(sound, str)
        ]
        if isinstance(entries, dict)
        else []
    )
    return "\n".join(lines), (
        notation.strip() if isinstance(notation, str) and notation.strip() else DEFAULT_NOTATION
    )


def write_pronunciation(directory: Path, text: str, *, notation: str) -> bool:
    """Write ``pronunciation.json``, or remove it when the box is emptied.

    Refuses a line it cannot read rather than dropping it: a pronunciation that
    silently did not save is a fault the operator would go hunting in the
    engine.
    """
    path = directory / PRONUNCIATION_FILENAME
    entries: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PackageInvalid(PRONUNCIATION_LINE_REFUSED.format(number=number))
        word, _, sound = line.partition("=")
        if word.strip() and sound.strip():
            entries[word.strip()] = sound.strip()
    if len(entries) > MAX_PRONUNCIATION_ENTRIES:
        raise PackageInvalid(PRONUNCIATION_TOO_MANY)
    try:
        if not entries:
            path.unlink(missing_ok=True)
            return False
        path.write_text(
            json.dumps(
                {
                    "format_version": PRONUNCIATION_FORMAT_VERSION,
                    "notation": notation.strip() or DEFAULT_NOTATION,
                    "entries": entries,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise PackageInstallFailed(
            f"{PRONUNCIATION_FILENAME} could not be written: {exc.strerror or exc}."
        ) from exc
    return True


# ---------------------------------------------------------------------------
# Export — the same containment reasoning, in the other direction
# ---------------------------------------------------------------------------


def _exportable(directory: Path) -> Iterator[tuple[Path, str]]:
    """Each file that belongs to this voice, with the name it takes in the zip.

    **This is the archive protections pointing outwards.** An install refuses
    an entry that would land outside the folder; an export refuses a file that
    resolves outside it. A symlink is refused rather than followed for exactly
    the reason one is refused on the way in — following it packages somebody
    else's file — and the resolved path of every member is checked against the
    resolved voice directory, so a link planted by hand cannot turn a download
    of one voice into a read of the appdata volume.
    """
    root = directory.resolve()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise VoiceContentRefused(EXPORT_SYMLINK_REFUSED.format(name=relative))
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise VoiceContentRefused(EXPORT_ESCAPES.format(name=relative))
        yield path, relative


def export_voice(
    layout: AppdataLayout,
    *,
    engine: str,
    voice: str,
    limits: PackageLimits = VOICE_LIMITS,
) -> ExportedVoice:
    """Package one installed voice as a pack, with its ``voice.toml`` generated.

    What goes in: every file in the voice's own folder — the model files it
    arrived with, ``pronunciation.json``, ``LICENSE``, ``ATTRIBUTION.md``,
    ``sample.wav``, whatever is there — plus a ``voice.toml`` written from the
    metadata on the manage screen. The stored ``voice.toml`` is replaced by the
    generated one rather than shipped twice.

    **A voice with nothing filled in still exports.** Missing fields are absent
    from the generated file, not invented and not blockers: the pack is being
    built by this round trip, so refusing until every field is complete would
    defeat the thing it exists for.
    """
    engine_name = require_engine_id(engine)
    voice_name = require_voice_id(voice)
    directory = voice_directory(layout, engine_name, voice_name)
    metadata = read_voice_metadata(directory, engine=engine_name, voice=voice_name)

    buffer = io.BytesIO()
    members: list[str] = []
    total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        generated = metadata_document(metadata, directory=directory).encode("utf-8")
        bundle.writestr(VOICE_METADATA_FILENAME, generated)
        members.append(VOICE_METADATA_FILENAME)
        total += len(generated)
        for path, relative in _exportable(directory):
            if relative == VOICE_METADATA_FILENAME:
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:  # pragma: no cover - vanished mid-export
                raise PackageInstallFailed(
                    f"{relative} could not be read: {exc.strerror or exc}."
                ) from exc
            total += size
            if total > limits.max_uncompressed_bytes:
                raise PackageTooLarge(
                    EXPORT_TOO_LARGE.format(limit=_human_bytes(limits.max_uncompressed_bytes))
                )
            bundle.write(path, arcname=relative)
            members.append(relative)

    logger.info("voice_exported", engine=engine_name, voice=voice_name, files=len(members))
    return ExportedVoice(
        engine=engine_name,
        voice=voice_name,
        filename=f"{voice_name}.zip",
        data=buffer.getvalue(),
        members=members,
    )


def remove_voice(layout: AppdataLayout, *, engine: str, voice: str) -> Path:
    """Delete one installed voice's folder, and nothing else.

    The folder is resolved and its parent checked against the engine's own
    directory before anything is removed, which is the same containment the
    plugin uninstall does: the core only deletes folders it put there.
    """
    directory = voice_directory(layout, engine, voice)
    resolved = directory.resolve()
    if resolved.parent != (layout.voices / engine).resolve():
        raise VoiceContentRefused(
            f"The folder for “{voice}” resolves to {resolved.as_posix()!r}, which "
            "is not directly inside the engine's own voices folder. The core only "
            "removes voice folders it put there."
        )
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        raise PackageInstallFailed(
            f"The voice folder {resolved} could not be removed: {exc.strerror or exc}. "
            "Check the appdata volume is writable by the user the core runs as."
        ) from exc
    logger.info("voice_removed", engine=engine, voice=voice)
    return resolved


__all__ = [
    "ATTRIBUTION_FILENAME",
    "DEFAULT_NOTATION",
    "ENGINE_ID_PATTERN",
    "EXECUTABLE_SUFFIXES",
    "ExportedVoice",
    "InstalledVoice",
    "LICENCE_FILENAME",
    "MODEL_WEIGHT_SUFFIXES",
    "NESTED_ARCHIVE_SUFFIXES",
    "PACK_FORMAT_VERSION",
    "PICKLE_SUFFIXES",
    "PRONUNCIATION_FILENAME",
    "VOICE_ID_PATTERN",
    "VOICE_LIMITS",
    "VOICE_METADATA_FILENAME",
    "VOICE_WORDS",
    "VoiceContentRefused",
    "VoiceMetadata",
    "export_voice",
    "install_voice",
    "installed_voices",
    "metadata_document",
    "model_paths",
    "read_pronunciation",
    "read_text_file",
    "read_voice_metadata",
    "refuse_forbidden_names",
    "refuse_unusable_pacing",
    "refuse_unusable_synthesis",
    "remove_voice",
    "require_engine_id",
    "require_voice_id",
    "suggest_voice_id",
    "voice_directory",
    "voice_files",
    "write_pronunciation",
    "write_text_file",
    "write_voice_metadata",
]
