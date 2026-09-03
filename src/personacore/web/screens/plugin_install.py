"""Installing a plugin from the browser: review first, then install.

Two posts, deliberately. The first reads the package and shows what it declares
- what it wants, where it runs, whether anything about it is enforced - and the
second is the one that installs. The words below are that disclosure, and they
are constants rather than template text so the review page and the tests read
the same sentences.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from personacore.config.secrets import SecretStore
from personacore.contracts.manifest import ServiceKind, Transport
from personacore.plugins.disclosure import PackageDisclosure, inspect_package
from personacore.plugins.packages import (
    PackageRejected,
    require_plugin_name,
)
from personacore.web.screens.plugin_common import (
    SECRET_NO_DESCRIPTION,
    SECRET_OPTIONAL_NOTE,
    SECRET_OPTIONAL_WORD,
    can,
    declared_secret_requests,
    secret_fields_from,
    store_secrets,
)
from personacore.web.screens.plugins import plugins_context
from personacore.web.shared import (
    NO_PLUGIN_OPERATIONS,
    UIContext,
    api_handler,
    refusal,
)

INSTALL_NO_FILE = (
    "Nothing was installed: no file was chosen. Click “Choose a .zip…”, pick the "
    "package, then Install."
)
"""The one refusal this screen owns.

Every other reason an install can fail is the installer's own sentence, passed
through unchanged. This one never reaches the installer at all — an empty form
has no bytes to hand it — so it is worded here, and it names the two clicks
rather than saying "archive is required"."""

INSTALL_UNREADABLE = (
    "Nothing was installed: the upload did not arrive in one piece. Try the same "
    "file again."
)
"""A malformed or truncated multipart body. Not the operator's mistake and not a
traceback: a sentence saying what to do next (spec section 9)."""

REVIEW_UNREADABLE_ARCHIVE = (
    "that .zip is truncated or malformed, and could not be read."
)
"""The catch-all for an archive that breaks the zip reader in some way other
than "not a zip at all".

``zipfile`` raises more than ``BadZipFile`` on a deliberately corrupt central
directory, and each of those would otherwise arrive as a 500 with a traceback —
which is the one thing a hostile upload must never be able to produce here."""


class _UploadRefused(Exception):
    """The multipart envelope never yielded a package, and this is why.

    Carries the finished operator-facing sentence, prefix and all. Callers
    render ``str(exc)`` and add nothing to it."""

INSTALL_NEXT_STEPS = (
    "It is in the list below. Open its settings if it needs an API key or an address."
)
"""Appended to the installer's own success sentence.

"Installed" is not the end of the errand. The package is on disk and the core
has reloaded, but a plugin with required settings sits there failing until
somebody fills them in, and the screen that installed it is the right place to
say so."""


# -- the install-time disclosure (PC-127, ADR-0012, ADR-0013) ---------------
#
# A stranger's zip was installed. Told that the manifest declares what the
# plugin asks for, the owner's objection was that most people will not read
# the instructions before installing a stranger's zip file — correctly, and
# that is the whole reason the four sentences below exist. A manifest read
# only by whoever unzipped the archive is not a disclosure, it is a document
# nobody opens.
#
# They are constants rather than template prose because **the honesty of this
# screen is the feature**, and a sentence that can drift is a sentence that will.
# ADR-0012 records that `permissions.network` and `permissions.paths` are
# declared and enforced by nothing for a stdio plugin, and says in as many words
# that "a control that appears enforced and is not is worse than no control,
# because it is trusted." So none of this screen calls them permissions.

REVIEW_TITLE = "Install {name} {version}?"

REVIEW_LEDE = (
    "Installing this runs its author's code on this machine."
)
"""The frame the rest of the dialog is read through.

It says "declares" before anything is listed, because the operator reads the
lists in whatever light the first sentence gave them."""

REVIEW_AUTHORS_WORDS = (
    "The author's own words. Nothing has checked they are true."
)

SERVICE_CLAUSES: dict[ServiceKind, str] = {
    ServiceKind.TTS: "a speech engine (the voice this assistant speaks in)",
    ServiceKind.STT: "a listener (what turns what you say into words)",
}
"""One clause per service a package can register as, contract 2.1's ``provides``.

Written as "what it becomes for you", not as the manifest's word for it: an
operator reading this screen has no reason to know what "tts" expands to, and
the expansion on its own ("text to speech") is not the fact they need either.
"""

REGISTERS_AS_ONE = (
    "It registers as {services}."
)

REGISTERS_AS_MANY = (
    "It registers as {services}."
)
"""What ``provides`` means, in the two shortest sentences that say it.

Two forms because a plugin can be both a speaker and a microphone and "that is
a job" would then be wrong. Nothing else differs between them.

**Two sentences, not a paragraph.** This screen is already long, and a package
declaring a service is the uncommon case — but it is a
real capability grant rather than decoration, so the second sentence exists to
stop the first being read as another line of the author's blurb. A package that
declares nothing prints neither.
"""

RISK_IS_ENFORCED = (
    "Enforced. Each tool is held to the level shown here. “safe” runs without "
    "asking; “confirm” asks first, every time; “restricted” needs permission for "
    "the person asking, and then still asks."
)
"""One of the two real guarantees, said plainly because it is real."""

SECRETS_ARE_ENFORCED = (
    "Enforced. It is handed the credentials named here and no others. It cannot "
    "reach another plugin's, or this core's."
)
"""The other real guarantee. The question at the moment of installing a
stranger's package is what of the owner's this gets to see, and this is the
one answer on the screen that is a boundary rather than a description.

Reworded for ADR-0025: the boundary is no longer "we filter a shared pool by
the names in your manifest" — which was the vulnerability, because the names
were global and guessable — but "your namespace is the only one you have". The
sentence says the stronger thing because the stronger thing is now true."""

SECRETS_OPTIONAL = (
    "Fill in what you have. An empty box installs anyway — the plugin waits for "
    "that credential, and its own page carries the same field. An empty box never "
    "clears a value already stored, and nothing pasted here is shown again."
)
"""Said under the credential boxes on the install disclosure (ADR-0025 §4).

Four facts an operator needs before deciding whether to hunt for an API key
right now: skipping is allowed, skipping is not a broken install, skipping does
not throw away a value they gave a previous install, and there is somewhere else
to do it later. Without them, a required-looking box in the middle of a security
disclosure is a reason to abandon the install.

**The third one is a rule, not a lookup.** It is true of every package on this
screen — :func:`~personacore.web.screens.plugin_common.store_secrets`
skips an empty box rather than deleting, and only an *uninstall* removes a
plugin's namespace — so it can be said without the screen asking the store
anything about a namespace the uploaded archive named. See
:func:`_review_secrets` for why that distinction is the whole point."""

REPLACE_KEEPS_CREDENTIALS = (
    "This .zip will be handed the credentials already stored for {plugin}. Nothing "
    "has checked it is that plugin: the name matches, and any package can declare "
    "a name. If it did not come from whoever wrote the {plugin} you are running, "
    "cancel."
)
"""What a replace actually authorises, said before the operator authorises it.

:data:`SECRETS_OPTIONAL` says an empty box keeps a value already supplied, which
is true of every package and is why it may be said at all. It is not the same
statement as this one. "Your value is kept" reads as a convenience; the fact
underneath is that **the archive in front of you inherits a namespace it named
itself**, and an operator replacing a plugin has to be told that before clicking
Install rather than discovering it afterwards.

Shown only when the operator chose replace *and* a plugin of that name is
installed *and* this package asks for credentials — three things the screen may
say without becoming an oracle. That a plugin named X is installed is on the
page behind this dialog already, and the installer itself refuses a colliding
name in those words; what the screen still never reveals is whether the
namespace holds anything, which is the question ADR-0025 section 1 makes
unanswerable. See :func:`_review_secrets` for the other half of that rule, and
``packages._clear_orphaned_secrets`` for what happens in the case this sentence
is *not* shown: the plugin is not installed, the core cannot tell a reinstall
from a stranger, and the namespace is cleared instead — silently, because a
sentence about it would be the oracle by another door."""

INSTALL_SECRETS_STORED = (
    "{names} stored for {plugin}, and it was restarted so it picks {them} up."
)

INSTALL_SECRETS_WAITING = (
    "It is waiting for {credential}: {names}. Open its settings to fill in what is "
    "missing."
)
"""Appended when a credential was requested and skipped.

Never "failed" and never a red row: the operator declined a box on purpose, or
did not have the key to hand, and telling them their install went wrong would
be telling them something untrue about a choice they just made."""

DECLARED_NOT_ENFORCED = (
    "Not enforced. This is what the author says the plugin needs, not a limit this "
    "core imposes. A host listed here can also be an example — check its settings "
    "for the address it will actually use."
)
"""Said under the hosts and the paths, and nowhere near the word "permission".

"Says it will" and "declares" throughout, never "may only": the manifest field
is called ``permissions`` and this screen deliberately does not repeat that
word, because the operator would believe it."""

NOT_A_SANDBOX = (
    "This is a disclosure, not a sandbox. A plugin this core starts is a program on "
    "this machine and can reach whatever this machine can reach, whatever its "
    "manifest says. If you did not get this .zip from someone you trust, cancel."
)
"""The sentence the whole screen exists to make unavoidable.

A false sense of safety at the exact moment someone runs a stranger's code is
the worst possible place for one, so this sits with the buttons rather than
above the fold, where it is the last thing read before Install is clicked."""

WHERE_IT_RUNS_STDIO = (
    "As a program this core starts on this machine, in its own folder under "
    "appdata. It shares this core's network access and filesystem view."
)

WHERE_IT_RUNS_HTTP = (
    "As a separate service, reached over the network. Its code does not run on this "
    "machine — but everything it is sent goes to that address."
)

REVIEW_REFUSED_TITLE = "That package was not read"

REVIEW_REFUSED_NOTE = (
    "Nothing was installed. Fix the package or choose a different file."
)

MULTIPART_SLACK_BYTES = 64 * 1024
"""How much bigger than the .zip the multipart body around it is allowed to be.

The archive-size cap belongs to the installer and is enforced there, on the
actual bytes. This surface checks the declared ``Content-Length`` first only so
a wildly oversized upload is refused before Starlette spools it to disk — and
because that length also counts the part headers, the boundary and the replace
checkbox, it is compared against the cap **plus** this slack. Refusing a package
that is legally at the limit because its form envelope added a few hundred bytes
would be this surface inventing a stricter rule than the one it is enforcing."""


def _registers_as(disclosure: PackageDisclosure | None) -> str | None:
    """:data:`REGISTERS_AS`, filled in — or nothing at all.

    Nothing is the ordinary answer. A plugin that only offers tools declares no
    services, and the section it would print into is already the longest thing
    an operator reads before clicking Install; a line saying "it registers as
    nothing" would be a sentence per package for the benefit of no package.

    Module-level and taking only the disclosure, so a test can read the exact
    sentence for a given set of services without standing up the application.
    """
    if disclosure is None or not disclosure.provides:
        return None
    clauses = [SERVICE_CLAUSES[service] for service in disclosure.provides]
    if len(clauses) == 1:
        return REGISTERS_AS_ONE.format(services=clauses[0])
    services = f"{', '.join(clauses[:-1])} and as {clauses[-1]}"
    return REGISTERS_AS_MANY.format(services=services)


def register(router: APIRouter, ctx: UIContext) -> None:
    """Register the review and install posts the plugins page's form makes."""
    # Imported inside `register`: see the note in the factory.
    from personacore.admin.routes import UPLOAD_TOO_LARGE

    templates = ctx.templates
    package_limits = ctx.package_limits
    layout = ctx.layout
    _plugins_context = partial(plugins_context, ctx)
    # Borrowed from `plugin_common`, under the names they had while they were
    # closures on the factory - the code below is unchanged from when it was.
    _can = can
    _refusal = refusal


    def _zip_request(request: Request, data: bytes) -> Request:
        """This request, presented to the JSON handler as the raw .zip body it reads.

        ``POST /admin/api/plugins/install`` takes the package as the request
        body and **refuses multipart outright** (see ``MULTIPART_NOT_ACCEPTED``
        — parsing it is Starlette's job, not that endpoint's). A browser file
        input can only post multipart. So the envelope is unwrapped here and the
        bytes are handed over in the shape that handler documents.

        Everything that decides *who is asking* is carried across untouched: the
        same scope, so ``require_user`` sees the same headers, the same cookies
        and the same client address, and re-derives the same operator it would
        have for a JSON caller. Only the content headers are rewritten, and only
        to describe the body that is now attached. In particular the ceiling is
        not applied here: the handler's own ``_read_upload`` streams this body
        and enforces the cap on it, so the limit has one implementation and this
        is not it (the caller's ``Content-Length`` glance is a guard against
        spooling, not a second check).
        """
        headers = [
            (name, value)
            for name, value in request.scope["headers"]
            if name not in (b"content-type", b"content-length", b"transfer-encoding")
        ]
        headers.append((b"content-type", b"application/zip"))
        headers.append((b"content-length", str(len(data)).encode("latin-1")))
        delivered = False

        async def receive() -> Any:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": data, "more_body": False}

        return Request({**request.scope, "headers": headers}, receive)

    async def _uploaded_package(request: Request) -> tuple[bytes, bool, str]:
        """The .zip out of the multipart envelope, or a refusal in a sentence.

        Both halves of the two-step install parse the same form — the review
        that shows the disclosure, and the install that follows it — so the
        envelope is unwrapped in one place. Raising rather than returning a
        union keeps each caller's happy path readable, and the message on the
        exception is already final: no caller re-words it.
        """
        # The declared length, before the body is touched. Starlette spools a
        # file part to a temporary file with no ceiling of its own, so a
        # gigabyte would be written to disk on its way to being refused.
        declared = request.headers.get("content-length")
        if (
            declared
            and declared.isdigit()
            and int(declared) > package_limits.max_archive_bytes + MULTIPART_SLACK_BYTES
        ):
            raise _UploadRefused(f"Not installed: {UPLOAD_TOO_LARGE}")

        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - a malformed envelope is a sentence, not a 500
            raise _UploadRefused(INSTALL_UNREADABLE) from exc

        try:
            upload = form.get("archive")
            if not isinstance(upload, StarletteUploadFile) or not upload.filename:
                raise _UploadRefused(INSTALL_NO_FILE)
            data = await upload.read()
            if not data:
                raise _UploadRefused(INSTALL_NO_FILE)
            # An unticked checkbox is simply absent, which is how HTML says no.
            return data, bool(form.get("replace")), upload.filename
        finally:
            await form.close()

    async def _uploaded_secrets(request: Request) -> dict[str, str]:
        """The credential boxes out of the same multipart envelope.

        Read in a second pass rather than returned alongside the archive,
        because the two are wanted at different moments: the bytes go to the
        installer, and these are not looked at until the installer has said
        which plugin actually arrived and what its manifest asked for. A
        malformed envelope is already the installer's refusal by then, so this
        answers with nothing rather than raising a second time.
        """
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001 - already refused on the archive's pass
            return {}
        try:
            return secret_fields_from(form)
        finally:
            await form.close()

    async def _apply_secrets(request: Request, plugin: str) -> str:
        """Store what was pasted, restart the plugin, and say what happened.

        Runs **after** the install, deliberately. The names are gated on
        ``declared_secrets`` read from the manifest that is now on disk — the
        one the installer validated — rather than on the disclosure the
        operator was shown, so nothing can be written under a name the
        installed plugin did not itself ask for. A package that failed to
        install never reaches here, so a refused upload cannot leave a
        credential behind for a plugin that does not exist.

        The restart is the JSON API's own switch off and on, which is what
        ``_reload_after_config`` does for a saved setting: a plugin reads its
        environment when it starts, so a credential supplied a moment after it
        gave up is not in use until it starts again.
        """
        requests = declared_secret_requests(layout, plugin)
        declared = [request["name"] for request in requests]
        required = {
            request["name"] for request in requests if request.get("required", True)
        }
        if not declared:
            return ""
        submitted = await _uploaded_secrets(request)
        stored, refusals = store_secrets(layout, plugin, submitted, declared)
        parts: list[str] = []
        if stored:
            one = len(stored) == 1
            parts.append(
                INSTALL_SECRETS_STORED.format(
                    names=", ".join(stored),
                    plugin=plugin,
                    them="it" if one else "them",
                )
            )
            await _restart(request, plugin)
        parts.extend(refusals)
        # Only a *required* credential leaves the plugin waiting (ADR-0026).
        # An optional one nobody pasted is a plugin that started, and telling
        # the operator to go and find a value for it would be this screen
        # asking for an errand the plugin does not need run.
        outstanding = [
            name for name in declared if name in required and name not in set(stored)
        ]
        # Re-read rather than assumed: a replace-install keeps a credential
        # supplied last time, and telling somebody to paste one they already
        # gave is the interface being wrong about its own state.
        try:
            outstanding = SecretStore(layout).missing(outstanding, plugin=plugin)
        except Exception:  # noqa: BLE001, S110 - an unreadable store is not a dead page
            outstanding = list(outstanding)
        if outstanding:
            parts.append(
                INSTALL_SECRETS_WAITING.format(
                    credential="a credential" if len(outstanding) == 1 else "credentials",
                    names=", ".join(outstanding),
                )
            )
        return " ".join(parts)

    async def _restart(request: Request, plugin: str) -> None:
        """Off, then on, through the JSON API's own handlers.

        The same two calls ``_reload_after_config`` makes, reached the same way
        every other operation on this surface is: nothing here stops or starts a
        subprocess itself, so the audit records and the ordering are the API's.
        A core assembled without the plugin API simply does not restart, and
        the credential is still stored and in use at the next start.
        """
        for operation in ("disable", "enable"):
            handler = api_handler(request.app, operation)
            if handler is None:  # pragma: no cover - no plugin API at all
                return
            try:
                await handler(name=plugin, request=request)
            except HTTPException:  # pragma: no cover - reported by the page's own row
                return

    def _installed_now(name: str) -> bool:
        """Whether a plugin folder of that name is on disk, either transport.

        The name is held to the manifest's own rule before it is joined onto
        anything: it arrives from an uploaded manifest, and this surface does
        not get to be the one place that trusts one (spec section 7). It is
        already pattern-checked by the manifest model that produced the
        disclosure, so a refusal here means something upstream changed — and
        "not installed" is the answer that discloses least.
        """
        try:
            require_plugin_name(name)
        except PackageRejected:  # pragma: no cover - the manifest model checks first
            return False
        return (layout.plugins / name).exists() or (layout.plugins_http / name).exists()

    def _replace_disclosure(
        disclosure: PackageDisclosure | None, *, replace: bool
    ) -> str | None:
        """The sentence in :data:`REPLACE_KEEPS_CREDENTIALS`, or nothing.

        Nothing is the answer for every case in which the credentials are not
        about to change hands: no package read, replace not chosen, a package
        that asks for no credentials at all, or no plugin of that name installed
        — which is the case the installer handles by clearing the namespace
        rather than by saying anything.
        """
        if disclosure is None or not replace or not disclosure.secret_requests:
            return None
        if not _installed_now(disclosure.name):
            return None
        return REPLACE_KEEPS_CREDENTIALS.format(plugin=disclosure.name)

    def _review_secrets(disclosure: PackageDisclosure | None) -> list[dict[str, Any]]:
        """One request per secret the package declares, for the boxes.

        **The store is not consulted here, and that is the fix.** This used to
        call ``missing(disclosure.secrets, plugin=disclosure.name)`` — and
        *both* arguments come out of the uploaded manifest. An archive that
        simply claimed ``name = "alpha"`` and declared ``alpha_key`` got back
        "A credential is stored.", which is the answer to "does the secret
        ``alpha_key`` exist in the ``alpha`` namespace". Existence only,
        admin-only, and Cancel installed nothing — and still an oracle, because
        **the archive chose the namespace it asked about**. ADR-0025 §1 exists
        to make that question unanswerable ("there is nothing to enumerate and
        nothing to guess"), and a namespace a stranger's zip can name is not a
        namespace.

        **Could a genuine re-install be told apart from a stranger's zip
        claiming the same name? No.** Nothing about an archive at review time
        binds it to the plugin already on disk: there is no signature, the
        manifest is the author's own words (``REVIEW_AUTHORS_WORDS`` says so on
        this very screen), and the package has deliberately not been unpacked.
        "Is a plugin called alpha installed" and "is this zip that plugin" are
        different questions and only the first is answerable. Gating the lookup
        on the installed plugin's own manifest would answer the first one and
        still let the archive pick which namespace to point it at. So the screen
        says nothing about stored state — the honest answer when the two cases
        cannot be separated.

        The re-install case keeps what was useful in it, without a lookup:
        :data:`SECRETS_OPTIONAL` now says outright that an empty box keeps a
        value already supplied, which is true of every package rather than a
        report on one namespace. And after the install — where the plugin's
        identity comes from the manifest the *installer* validated on disk and
        not from an upload — :func:`_apply_secrets` re-reads the store and only
        says "waiting for" what is genuinely absent. That is the one place
        entitled to know.

        The rendered rows carry no state word at all: ``secret_state`` is false
        in the review's context, so the template draws the boxes and not the
        sentence. "None yet." for every row would be the same oracle running the
        other way — an assertion about the namespace, made to whoever chose it.

        **The description is now the author's own** (ADR-0026). It comes out of
        ``permissions.secrets`` in the manifest, which is the one member this
        screen already decompresses — so it costs no extra read and undoes
        nothing about Cancel. It is a stranger's text on a page the operator
        trusts, so it is rendered escaped and never as markup, exactly like the
        plugin's description and its tool descriptions beside it.

        ``required`` travels with it so an optional request is drawn as one.
        Telling somebody a box is optional is not an assertion about their
        store — it is what the manifest in front of them says.
        """
        if disclosure is None or not disclosure.secret_requests:
            return []
        return [
            {
                "name": request.name,
                "description": request.description.strip() or SECRET_NO_DESCRIPTION,
                "required": request.required,
                "stored": False,
            }
            for request in disclosure.secret_requests
        ]

    async def _reviewed(
        request: Request,
    ) -> tuple[PackageDisclosure | None, str | None, bool]:
        """Read what the uploaded package declares, without installing it.

        Exactly one of the first pair comes back; the third is the replace
        checkbox as the form posted it, which the review needs because
        replacing is the one case where an installed plugin's credentials
        change hands (:func:`_replace_disclosure`). Nothing is written anywhere
        on this path: :func:`~personacore.plugins.disclosure.inspect_package`
        opens the archive from the bytes in memory and decompresses one member,
        the manifest. So Cancel has nothing to undo, and a hostile archive is
        refused here — before the operator is asked anything — rather than at
        the end of an install they already agreed to.
        """
        try:
            data, replace, _filename = await _uploaded_package(request)
        except _UploadRefused as refused:
            return None, str(refused), False
        try:
            disclosure = await asyncio.to_thread(
                inspect_package, data, limits=package_limits
            )
        except PackageRejected as exc:
            return None, f"Nothing was installed: {exc}", replace
        except Exception:  # noqa: BLE001 - a broken archive is a sentence, not a 500
            # `zipfile` raises more than `BadZipFile` on a truncated or
            # deliberately malformed central directory, and every one of them
            # would otherwise reach the operator as a traceback (spec §9).
            return None, f"Nothing was installed: {REVIEW_UNREADABLE_ARCHIVE}", replace
        return disclosure, None, replace

    async def _install_uploaded(request: Request) -> dict[str, str]:
        """Install the uploaded package, and say what happened in a sentence.

        Never raises and never answers a bare status code. Every way this can
        fail — no file chosen, a truncated upload, an oversized one, something
        that is not a zip, no manifest, a manifest whose name does not match its
        folder, a name already installed without ``replace``, an entry that
        escapes its folder — comes back as ``{"kind": ..., "message": ...}`` for
        the template to render. The wording of all but the first two is the
        installer's own, passed through unchanged: it was written for an
        operator already (spec section 9), and rewording it here would be a
        second description of the same refusal, ready to drift.
        """
        handler = api_handler(request.app, "install")
        if handler is None:  # pragma: no cover - the control is disabled first
            return {"kind": "refused", "message": NO_PLUGIN_OPERATIONS}

        try:
            data, replace, filename = await _uploaded_package(request)
        except _UploadRefused as refused:
            return {"kind": "refused", "message": str(refused)}

        try:
            result = await handler(
                request=_zip_request(request, data), replace=replace, filename=filename
            )
        except HTTPException as exc:
            return {"kind": "refused", "message": f"Not installed: {_refusal(exc)}"}
        # The plugin's real name, from the manifest the installer validated —
        # never the disclosure's, and never the uploaded filename.
        credentials = await _apply_secrets(request, result.installed.name)
        message = f"{result.message} {INSTALL_NEXT_STEPS}"
        return {"kind": "ok", "message": f"{message} {credentials}".strip()}

    @router.post(
        "/plugins/install/review",
        response_class=HTMLResponse,
        summary="Show what an uploaded .zip would be able to reach and do",
    )
    async def plugin_install_review(request: Request) -> HTMLResponse:
        """The step between choosing a stranger's .zip and running their code.

        This is the whole point of the two-step install. The manifest already
        declared what the plugin exposes and what it wants; until now that
        declaration was visible only to somebody who unzipped the archive and
        read a TOML file, which is not a disclosure. So the archive is **read
        and not unpacked** — see
        :func:`~personacore.plugins.disclosure.inspect_package`, which opens it
        from the bytes in memory and decompresses one member — and what it says
        about itself is put in front of the operator before anything is written
        anywhere.

        **Cancel therefore costs nothing to undo**: there is no staging
        directory, no temporary folder and no half-installed plugin, because
        nothing was created. The bytes are posted again by the dialog's Install
        button (htmx re-sends the form's own file input), which is deliberate:
        the alternative is holding an untrusted upload on the server between two
        clicks, keyed by something a caller could guess.

        Answers 200 either way, into ``#modal``. A package that cannot be read
        gets the same dialog carrying the refusal and one button, because a
        malformed archive must reach the operator as a sentence rather than a
        traceback (spec section 9).
        """
        if not _can(request, "install"):  # pragma: no cover - control disabled first
            disclosure, refusal, replace = None, NO_PLUGIN_OPERATIONS, False
        else:
            disclosure, refusal, replace = await _reviewed(request)
        return templates.TemplateResponse(
            request=request,
            name="fragments/plugin_review.html",
            context={
                "disclosure": disclosure,
                "refusal": refusal,
                "title": (
                    REVIEW_TITLE.format(name=disclosure.name, version=disclosure.version)
                    if disclosure
                    else REVIEW_REFUSED_TITLE
                ),
                "lede": REVIEW_LEDE,
                # Contract 2.1: what kind of service the package says it *is*,
                # beside the name rather than down with the tools. None when it
                # declares none, which is the ordinary case.
                "provides_note": _registers_as(disclosure),
                "authors_words": REVIEW_AUTHORS_WORDS,
                "risk_enforced": RISK_IS_ENFORCED,
                "secrets_enforced": SECRETS_ARE_ENFORCED,
                "declared_not_enforced": DECLARED_NOT_ENFORCED,
                "not_a_sandbox": NOT_A_SANDBOX,
                # ADR-0025 §4: the request becomes a field, here, rather than a
                # name the operator has to go and satisfy with shell access.
                "secret_requests": _review_secrets(disclosure),
                "secrets_optional": SECRETS_OPTIONAL,
                # The one thing this screen says about stored credentials, and
                # it is about the *installed* plugin rather than about a
                # namespace the upload named (see `_replace_disclosure`). None
                # on every other path, so the template has nothing to print.
                "replace_disclosure": _replace_disclosure(disclosure, replace=replace),
                # A request the manifest marked `required = false` is labelled
                # as one (ADR-0026). This says what the manifest says, not what
                # the store holds, so it is a sentence the review may print.
                "optional_word": SECRET_OPTIONAL_WORD,
                "optional_note": SECRET_OPTIONAL_NOTE,
                # No state word on this screen, in either direction: neither
                # "A credential is stored." nor "None yet." is a sentence the
                # review is entitled to say, because the namespace it would be
                # about was named by the upload (see `_review_secrets`). The
                # words are not passed at all, so the template has nothing to
                # print even if the guard below it were lost.
                "secret_state": False,
                "where_it_runs": (
                    WHERE_IT_RUNS_HTTP
                    if disclosure and disclosure.transport is Transport.HTTP
                    else WHERE_IT_RUNS_STDIO
                ),
                "refused_note": REVIEW_REFUSED_NOTE,
            },
        )

    @router.post(
        "/plugins/install",
        response_class=HTMLResponse,
        summary="Install a plugin from an uploaded .zip",
    )
    async def plugin_install(request: Request) -> HTMLResponse:
        """ADR-0013's upload, from the page rather than from curl (PC-127).

        **The second of the two clicks.** From the page this is reached only
        from the dialog ``plugin_install_review`` returned, so by the time it
        runs the operator has been shown what the package declares. The route
        itself is unchanged by that and deliberately so: it takes an archive and
        installs it, exactly as it did when the button posted here directly, and
        a caller who posts straight to it is not treated differently. The
        disclosure is a step in front of this door, not a lock on it — the locks
        are the installer's own checks, which run either way.

        The form is multipart because that is what a file input posts. **This
        route parses the envelope and nothing else**: the bytes inside go
        straight to the JSON API's own ``install_plugin``, so a package
        installed here and one installed with ``POST
        /admin/api/plugins/install`` go through the same staging, the same
        traversal and symlink checks, the same size ceilings, the same reload
        and the same audit record. Nothing in the archive is opened as code on
        either path — that guarantee is ADR-0013's and lives in the installer,
        and this surface has no branch that could step around it.

        Answers 200 whether the install succeeded or was refused, because the
        response *is* the refreshed screen: the plugin list htmx asked for, and
        the reason swapped in beside the form out of band. A status code is not
        a sentence an operator can act on (spec section 9).
        """
        notice = await _install_uploaded(request)
        return templates.TemplateResponse(
            request=request,
            name="fragments/plugin_installed.html",
            context=await _plugins_context(request, install_result=notice, shell=False),
        )
