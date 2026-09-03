"""Structured logging plus the audit and transcript store.

Spec sections 7 (security, audit log), 8 (multi-user), 9 (trace view), 10
(logging), 13.3 (schemas before features); ADR-0004 (conversation audit and
retention). See the module docstrings in `models.py`, `store.py`, and
`logging.py` for the detail each spec/ADR reference actually drives.
"""

from __future__ import annotations

from personacore.audit.logging import (
    LoggingConfig,
    bind_correlation_id,
    clear_correlation_id,
    configure_logging,
    get_correlation_id,
    get_logger,
    redact_processor,
)
from personacore.audit.models import (
    AuditCategory,
    AuditOutcome,
    AuditRecord,
    AuditStoreConfig,
    Author,
    AuthorKind,
    MessageRole,
    Owner,
    OwnerKind,
    PurgeResult,
    RetentionConfig,
    Surface,
    TranscriptRecord,
)
from personacore.audit.store import AuditStore

__all__ = [
    "AuditCategory",
    "AuditOutcome",
    "AuditRecord",
    "AuditStore",
    "AuditStoreConfig",
    "Author",
    "AuthorKind",
    "LoggingConfig",
    "MessageRole",
    "Owner",
    "OwnerKind",
    "PurgeResult",
    "RetentionConfig",
    "Surface",
    "TranscriptRecord",
    "bind_correlation_id",
    "clear_correlation_id",
    "configure_logging",
    "get_correlation_id",
    "get_logger",
    "redact_processor",
]
