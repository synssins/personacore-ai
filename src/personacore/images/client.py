"""The image generation service's client — docs/contracts/image-conversations.md.

An image conversation's responder (contract §4): a prompt goes in over HTTP,
a picture comes back. This is the core's only path to that service, the same
role ``personacore.llm.client.LLMClient`` plays for chat completions — a
thin, OpenAI-shaped HTTP client with nothing backend-specific in it, so
config alone decides which server answers (``personacore.config.image.
ImageSettings``).

**Verified against the upstream project, not written from memory** — this
contract's own instruction, because two contracts written the same day as
this one asserted things about the codebase that were not true and both were
caught only by an implementer reading the real thing. Checked:
``leejet/stable-diffusion.cpp`` (the project ``sd-server`` is built from),
``examples/server/api.md``, the "OpenAI API" section, on the ``master``
branch, fetched 2026-09-02. That document is explicit about ``POST
/v1/images/generations``:

* Request fields it lists: ``prompt`` (string, required), ``n`` (integer,
  optional), ``size`` (``WIDTHxHEIGHT``, optional), ``output_format``
  (``png``/``jpeg``/``webp``, optional), ``output_compression`` (integer
  0-100, optional). No ``model`` field is listed for *this* endpoint — this
  client sends one anyway, additively, only when ``ImageSettings.model`` is
  set, on the OpenAI-API convention that an unrecognised field is ignored
  rather than rejected; it is never required.
* Response fields it lists: ``created`` (integer), ``output_format``
  (string), ``data`` (array of ``{b64_json: string}``). **Only base64, never
  a ``url`` response format** — so this client does not offer
  ``response_format`` and always reads ``data[0].b64_json``.

What that document does **not** say: the HTTP status codes or error body
shape this endpoint uses on failure (the native, asynchronous ``sdcpp``
job API a few sections later documents its own error object, but that is a
different mechanism this project does not use here). Failure handling below
is therefore deliberately conservative rather than parsed against a
documented shape: any non-2xx response is read as a refusal, and whatever
words it sent — an OpenAI-style ``{"error": {"message": ...}}``, a bare
string, or nothing parseable at all — are folded into the sentence
docs/contracts/image-conversations.md §9 asks for, on a best-effort basis.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx

from personacore.config.image import ImageSettings

DEFAULT_OUTPUT_FORMAT = "png"
"""Asked for explicitly rather than left to the server's own default, so this
client always knows which :data:`_MEDIA_TYPES_BY_FORMAT` entry applies to
what comes back."""

_MEDIA_TYPES_BY_FORMAT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
"""The three ``output_format`` values ``api.md`` documents, mapped to the
media type :func:`personacore.attachments.put` needs — all three are already
in that module's own storage allowlist."""

_ERROR_BODY_CHARS = 300
"""How much of an error response's own wording is folded into the sentence a
person sees. Long enough for an actual message, short enough that a server
returning an HTML error page does not become the whole of what the thread
shows."""

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
"""How much of a response this client will hold before refusing it.

**Nothing upstream of this bounds it.** The picture's own ceiling is
:data:`personacore.attachments.MAX_ATTACHMENT_BYTES` (100 MB), and it applies
at :func:`~personacore.attachments.put` — which is three steps after the whole
body has already been buffered, parsed as JSON and base64-decoded. A hostile,
compromised or simply broken generator answering with a gigabyte would spend
all three of those in memory before anything said no.

**32 MiB, and the number is this module's own choice** — the contract does not
give one. The response is JSON around base64, so it is four thirds of the
picture plus a little: this admits roughly a 24 MB picture, where a 1024x1024
PNG is one or two megabytes and a 4K one is twenty. CPU-only (CLAUDE.md's hard
constraint) is what makes that generous rather than tight — nothing this stack
can reach generates a larger picture than that in practice. Raising it costs
memory per concurrent request twice over, once buffered and once decoded,
which is why it is not simply set to the storage ceiling.
"""

TOO_LARGE = f"the image generator sent more than {MAX_RESPONSE_BYTES // (1024 * 1024)} MB"
"""Contract §9 asks for a specific cause, not a tidy one. This is a refusal
and not an unavailability: the generator was reached and it answered — with
something this core will not hold."""


class ImageUnavailable(RuntimeError):
    """The image service could not be reached at all — dead host, wrong
    address, DNS failure, connection refused. Contract §9: this is a
    different fact from :class:`ImageRefused` and gets a different sentence
    — "we can't reach the image generator" is the specified wording for it.
    """


class ImageRefused(RuntimeError):
    """The image service was reached and declined — a 4xx/5xx response, or a
    200 whose body did not actually contain a picture. Contract §9's other
    sentence: "the image generator refused that request." Carries whatever
    the server's own response said, when it said anything parseable.
    """


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """One picture, ready to be handed to :func:`personacore.attachments.put`
    exactly as any uploaded file is (contract §8 rule 4)."""

    data: bytes
    media_type: str


class ImageClient:
    """One image service, reached the way any ``[llm.*]`` role's endpoint is
    — settings in, an HTTP call out, nothing cached between calls."""

    def __init__(self, settings: ImageSettings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return self._settings.is_configured()

    async def generate(self, prompt: str) -> GeneratedImage:
        """Ask for one picture and return it, or raise :class:`ImageUnavailable`
        / :class:`ImageRefused` — never anything else, so a caller never has
        to guess which of the two plain-English sentences applies.
        """
        if self._settings.base_url is None:
            raise ImageUnavailable("no [image] base_url is configured")

        body: dict[str, Any] = {
            "prompt": prompt,
            "n": 1,
            "output_format": DEFAULT_OUTPUT_FORMAT,
        }
        if self._settings.model:
            body["model"] = self._settings.model

        timeout = httpx.Timeout(
            connect=self._settings.connect_timeout_seconds,
            read=self._settings.read_timeout_seconds,
            write=self._settings.connect_timeout_seconds,
            pool=self._settings.connect_timeout_seconds,
        )
        url = f"{self._settings.base_url}/v1/images/generations"
        deadline = self._settings.total_timeout_seconds
        try:
            # Two clocks, and both are needed. ``timeout`` is httpx's own and
            # it is per-operation: a server sending one byte inside every read
            # window satisfies it indefinitely. ``asyncio.timeout`` is the one
            # that does not reset, so a dribble ends rather than holding this
            # worker for as long as the other end feels like holding it.
            #
            # Streamed rather than ``client.post``: the body is read in pieces
            # against a ceiling (:func:`_read_bounded`) instead of buffered
            # whole and asked about afterwards, which is the only order in
            # which a ceiling means anything.
            async with asyncio.timeout(deadline):
                async with (
                    httpx.AsyncClient(timeout=timeout) as client,
                    client.stream("POST", url, json=body) as response,
                ):
                    status_code = response.status_code
                    raw_body = await _read_bounded(response)
        except TimeoutError as exc:
            # Reads as "PersonaCore can't reach the image generator: it did
            # not answer within N seconds" once `chat_image._unavailable_
            # sentence` has put its half in front — which is why this half does
            # not name the generator a second time.
            raise ImageUnavailable(f"it did not answer within {deadline:g} seconds") from exc
        except httpx.HTTPError as exc:
            raise ImageUnavailable(_readable(exc)) from exc

        if status_code >= 400:
            raise ImageRefused(_error_detail(status_code, raw_body))

        try:
            payload = json.loads(raw_body)
            items = payload["data"]
            b64 = items[0]["b64_json"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ImageRefused(
                "the image generator's response did not contain a picture"
            ) from exc
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ImageRefused(
                "the image generator's response could not be decoded"
            ) from exc
        if not raw:
            raise ImageRefused("the image generator returned an empty picture")

        output_format = str(payload.get("output_format") or DEFAULT_OUTPUT_FORMAT).lower()
        media_type = _MEDIA_TYPES_BY_FORMAT.get(output_format, "image/png")
        # What the *bytes* are, never what the service called them. Everything
        # downstream trusts the media type stored beside a picture: it becomes
        # the served response's own ``Content-Type``, and an image is served
        # inline so it can be a thumbnail's ``src``. A declared format is a
        # claim and a magic number is a fact, and this is where the two have to
        # agree or nothing is stored at all.
        actual = _sniffed(raw)
        if actual is None:
            raise ImageRefused(
                "the image generator's response was not a PNG, JPEG or WebP picture"
            )
        if actual != media_type:
            raise ImageRefused(
                f"the image generator called its picture {media_type} and sent {actual}"
            )
        return GeneratedImage(data=raw, media_type=media_type)


def _readable(exc: BaseException) -> str:
    """One line describing a connection failure, with no traceback in it —
    the same treatment ``personacore.web.shared._readable`` gives an
    exception, kept as a small copy here rather than an import: that name is
    this project's per-surface convention, not a shared utility, and this
    package does not otherwise depend on the admin UI."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _read_bounded(response: httpx.Response) -> bytes:
    """This response's body, or :class:`ImageRefused` the moment it goes over
    :data:`MAX_RESPONSE_BYTES`.

    Checked *as it arrives*, not after: the ceiling exists so that a body this
    core will not hold is never held, and a check after ``await
    response.aread()`` would already have held it. The connection is closed on
    the way out by the ``client.stream`` block this runs inside.

    Applies to a refusal's body as much as to a picture's — a generator
    answering ``500`` with a gigabyte of HTML is the same denial of service as
    one answering ``200`` with it.
    """
    buffered = bytearray()
    async for chunk in response.aiter_bytes():
        buffered.extend(chunk)
        if len(buffered) > MAX_RESPONSE_BYTES:
            raise ImageRefused(TOO_LARGE)
    return bytes(buffered)


_MAGIC_NUMBERS = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
"""The two formats a leading signature settles outright. WebP needs its second
half checked as well, so it is done separately in :func:`_sniffed`."""


def _sniffed(data: bytes) -> str | None:
    """What these bytes actually are, or ``None`` for anything that is not one
    of the three formats this client accepts.

    No new dependency, and none needed: PNG, JPEG and WebP each open with an
    unambiguous signature. Deliberately not a full decode — it does not prove
    the picture is well-formed, only that it is not something else wearing an
    image's name, which is the claim everything downstream of here relies on.
    """
    for signature, media_type in _MAGIC_NUMBERS:
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _error_detail(status_code: int, body: bytes) -> str:
    """Whatever the server said about why it refused, trimmed to a sentence.

    Conservative on purpose — see the module docstring for why this endpoint's
    error shape is not documented upstream. An OpenAI-style ``{"error":
    {"message": ...}}`` is read if present; anything else falls back to
    whatever text the response carried, or failing that, the bare status.

    Takes the bytes :func:`_read_bounded` already holds rather than the
    response object: ``response.text`` would read the body a second time and
    without a ceiling, which is the whole thing this pair exists to stop.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        text = body.decode("utf-8", "replace").strip()
        return text[:_ERROR_BODY_CHARS] if text else f"HTTP {status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:_ERROR_BODY_CHARS]
        if isinstance(error, str) and error:
            return error[:_ERROR_BODY_CHARS]
    return f"HTTP {status_code}"


__all__ = [
    "DEFAULT_OUTPUT_FORMAT",
    "MAX_RESPONSE_BYTES",
    "TOO_LARGE",
    "GeneratedImage",
    "ImageClient",
    "ImageRefused",
    "ImageUnavailable",
]
