"""Structured logging — spec section 10.

Configures `structlog` for JSON output to stdout and to a file under appdata,
with a correlation id bound per request so the section 9 trace view can pull
"everything one request did" as a single group. The same correlation id is
what :class:`personacore.audit.models.AuditRecord` and
:class:`personacore.audit.models.TranscriptRecord` store as
``correlation_id``, so a log stream and a store query about the same request
line up without translation.

Redaction (mandatory, this module's other job): pattern- and key-based, not
shape-based. A bound field whose *key* is a known-sensitive name (api_key,
token, password, authorization, ...) is replaced wholesale, and any string
value -- bound field or free-text message -- that matches a labelled shape
like ``Bearer <value>``, ``api_key=<value>`` or ``Authorization: <value>`` has
the value half redacted. This is a belt-and-braces control — plugins and core
code should not be logging secrets in the first place (spec section 7) — but
a processor that runs on every record is cheaper than auditing every call
site by hand, and it catches the call site someone adds later without
reading this docstring.

What this does NOT do: catch a bare secret-shaped string with no recognisable
label (no key name in `_SENSITIVE_KEYS`, no `token=`/`Bearer `/etc. prefix in
the text) — e.g. a raw API key logged under an innocuous field name with no
surrounding words. Shape-based detection of arbitrary high-entropy strings
would flag ordinary conversation text constantly, so this module deliberately
does not attempt it. Callers must not log an unlabelled bare secret and rely
on this processor to catch it.

Conversation content is a separate, stricter rule enforced in
personacore/audit/store.py, not here: this module's redaction processor
would happily let ordinary message text through since it is not a secret.
The store's own logging calls simply never pass content as a field.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict

REDACTED = "***REDACTED***"

# Keys that are wholesale sensitive: if a log call passes password=... or
# api_key=... as a bound field, the whole value is replaced regardless of
# what it looks like.
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "client_secret",
    "password",
    "passwd",
    # The broker password's own two names: the form field it is typed into and
    # the dotted path it is stored at. Neither normalises to a bare "password",
    # so without them a log call binding either key would print the value.
    "bus_password",
    "bus.password",
    "authorization",
    "auth",
    "bearer",
    # The core's own sign-in (PC-285). `password` above catches the field the
    # form and the JSON body both use; these are the other spellings that exist
    # in this codebase, and none of them normalises to a bare "password", so
    # without naming them a log call binding one would print the value.
    #
    # `password_hash` and `token_hash` are not credentials that can be
    # replayed, but they are offline-guessable material out of a backup, and a
    # log line is the one place they have no business being at all.
    "confirm_password",
    "new_password",
    "current_password",
    "password_hash",
    "session_token",
    "token_hash",
}

# Patterns for secrets embedded inside free-text log messages, e.g. an
# exception string that happens to contain "Authorization: Bearer abc123" or
# "api_key=sk-...". Group 1 is the label, kept in the output so the shape of
# the message survives; group 2 (the value) is what gets redacted.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\b(bearer)\s+(\S+)"),
    re.compile(r"(?i)\b(api[_-]?key)\s*[:=]\s*(\S+)"),
    re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|token)\s*[:=]\s*(\S+)"),
    re.compile(r"(?i)\b(client[_-]?secret|secret)\s*[:=]\s*(\S+)"),
    # `\w*` in front so a prefixed spelling is caught too: "bus_password=x" has
    # no word boundary before "password", so the unprefixed pattern walked
    # straight past it and printed the value.
    re.compile(r"(?i)\b(\w*pass(?:word|wd))\s*[:=]\s*(\S+)"),
    re.compile(r"(?i)\b(authorization)\s*:\s*(\S.*)"),
]


def _redact_string(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", value)
    return value


def _normalise_key(key: object) -> str:
    """Canonical form of a bound-field or dict key for comparison against
    `_SENSITIVE_KEYS`. Strips incidental whitespace (e.g. a key like
    ``" token"`` produced by header-name parsing) in addition to the
    existing case and hyphen/underscore normalisation -- without stripping,
    such a key silently missed the sensitive-key set entirely."""
    return str(key).strip().lower().replace("-", "_")


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, BaseException):
        # `_redact_value` used to return an Exception instance unchanged,
        # so `logger.info(..., error=exc)` -- a very common pattern -- never
        # had its text scanned for secrets. Redact the exception's string
        # form and its args, and keep the type name so the log line stays
        # useful rather than just vanishing the error.
        return {
            "exception_type": type(value).__name__,
            "message": _redact_string(str(value)),
            "args": [_redact_value(arg) for arg in value.args],
        }
    if isinstance(value, dict):
        return {
            key: REDACTED if _normalise_key(key) in _SENSITIVE_KEYS else _redact_value(val)
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def redact_processor(
    logger: object, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """structlog processor: redact sensitive keys and embedded secret patterns.

    Mandatory in the processor chain (see :func:`configure_logging`) — this is
    not opt-in per call site.
    """
    for key in list(event_dict.keys()):
        if _normalise_key(key) in _SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


class LoggingConfig(BaseModel):
    """Constructor-argument config for :func:`configure_logging`.

    Deliberately local to this module rather than a shared config object —
    the task brief for this component reserves cross-cutting config for
    another agent's module.
    """

    model_config = ConfigDict(extra="forbid")

    log_dir: Path
    """Directory under appdata that JSON log files are written to."""

    level: str = "INFO"
    file_name: str = "personacore.jsonl"
    to_stdout: bool = True


def configure_logging(config: LoggingConfig) -> None:
    """Wire structlog + stdlib logging so every record is JSON, goes to
    stdout (if enabled) and to a file under appdata, and passes through
    redaction. Safe to call more than once (e.g. once per test) — it replaces
    the root logger's handlers rather than accumulating them.
    """
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.log_dir / config.file_name

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    structlog.configure(
        processors=shared_processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    root.setLevel(config.level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if config.to_stdout:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger. Call :func:`configure_logging` first."""
    return structlog.get_logger(name)


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation id to every log call made in this context (thread /
    async task) from here on, so the section 9 trace view can group them.
    Generates a fresh id if none is given, and returns whichever id ends up
    bound.
    """
    cid = correlation_id or str(uuid4())
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    return cid


def get_correlation_id() -> str | None:
    """The correlation id bound in the current context, if any."""
    return structlog.contextvars.get_contextvars().get("correlation_id")


def clear_correlation_id() -> None:
    """Unbind the correlation id — call at the end of a request/task."""
    structlog.contextvars.unbind_contextvars("correlation_id")


__all__ = [
    "REDACTED",
    "LoggingConfig",
    "bind_correlation_id",
    "clear_correlation_id",
    "configure_logging",
    "get_correlation_id",
    "get_logger",
    "redact_processor",
]
