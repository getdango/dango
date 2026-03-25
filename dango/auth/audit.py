"""dango/auth/audit.py

Security audit logging for authentication events.

Records auth events to ``.dango/logs/audit.jsonl`` (append-only JSONL)
and emits structured log entries via the TASK-007 logging infrastructure.
Write failures are swallowed (warning only) — audit logging must never
block the operation it instruments.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from dango.logging import get_logger

_logger = get_logger("dango.auth.audit")


# -- Event taxonomy --------------------------------------------------------


class AuditEvent(str, Enum):
    """Security-relevant authentication events."""

    # fmt: off
    LOGIN_SUCCESS      = "login_success"
    LOGIN_FAILURE      = "login_failure"
    LOGOUT             = "logout"
    SESSION_EXPIRED    = "session_expired"
    PASSWORD_CHANGE    = "password_change"
    PASSWORD_RESET     = "password_reset"
    USER_CREATED       = "user_created"
    USER_DEACTIVATED   = "user_deactivated"
    USER_REACTIVATED   = "user_reactivated"
    USER_DELETED       = "user_deleted"
    ROLE_CHANGED       = "role_changed"
    PERMISSION_DENIED  = "permission_denied"
    RATE_LIMIT_HIT     = "rate_limit_hit"
    ACCOUNT_LOCKED     = "account_locked"
    ACCOUNT_UNLOCKED   = "account_unlocked"
    TWO_FA_ENABLED     = "two_fa_enabled"
    TWO_FA_DISABLED    = "two_fa_disabled"
    API_KEY_CREATED    = "api_key_created"
    API_KEY_REVOKED    = "api_key_revoked"
    RECOVERY_CODE_USED = "recovery_code_used"
    INVITE_ACCEPTED    = "invite_accepted"
    INVITE_RESENT      = "invite_resent"
    SECRET_SET         = "secret_set"
    SECRET_DELETED     = "secret_deleted"
    OAUTH_SOURCE_CONNECTED    = "oauth_source_connected"
    OAUTH_SOURCE_DISCONNECTED = "oauth_source_disconnected"
    SCHEDULE_CREATED          = "schedule_created"
    SCHEDULE_UPDATED          = "schedule_updated"
    SCHEDULE_DELETED          = "schedule_deleted"
    SCHEDULE_TRIGGERED        = "schedule_triggered"
    SCHEDULES_RELOADED        = "schedules_reloaded"
    JOB_CANCELLED             = "job_cancelled"
    SYNC_TRIGGERED            = "sync_triggered"
    NOTEBOOK_CREATED          = "notebook_created"
    NOTEBOOK_DELETED          = "notebook_deleted"
    NOTEBOOK_LOCK_FORCE_RELEASED = "notebook_lock_force_released"
    GOVERNANCE_DRIFT_VIEWED      = "governance_drift_viewed"
    GOVERNANCE_PII_VIEWED        = "governance_pii_viewed"
    CATALOG_VIEWED               = "catalog_viewed"
    INSIGHTS_VIEWED              = "insights_viewed"
    AI_CATALOG_VIEWED            = "ai_catalog_viewed"
    # fmt: on


# -- File path helper ------------------------------------------------------


def get_audit_log_path(log_dir: Path | None = None) -> Path:
    """Return the path to the audit JSONL file, creating the directory.

    Args:
        log_dir: Override for the log directory.  Defaults to
            ``.dango/logs`` relative to the current working directory.
    """
    if log_dir is None:
        log_dir = Path.cwd() / ".dango" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "audit.jsonl"


# -- Write -----------------------------------------------------------------


def log_auth_event(
    event_type: AuditEvent,
    *,
    user_id: str | None = None,
    email: str | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
    log_dir: Path | None = None,
) -> None:
    """Record an authentication event to the audit log.

    Appends a single JSON line to ``audit.jsonl`` and emits a structured
    log entry at INFO level.  If the file write fails the error is logged
    as a warning — callers are never interrupted.

    Args:
        event_type: The kind of event (see :class:`AuditEvent`).
        user_id: UUID of the acting or affected user, if known.
        email: Email address associated with the event.
        ip: Client IP address.
        details: Arbitrary extra context (role changes, key names, etc.).
        log_dir: Override for the log directory (useful in tests).
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type.value,
    }
    if user_id is not None:
        entry["user_id"] = user_id
    if email is not None:
        entry["email"] = email
    if ip is not None:
        entry["ip"] = ip
    if details is not None:
        entry["details"] = details

    # Structured log (always, even if file write fails)
    _logger.info(
        "audit_event",
        audit_event=event_type.value,
        **{k: v for k, v in entry.items() if k not in ("event", "timestamp")},
    )

    # JSONL file write (never-fail)
    try:
        log_path = get_audit_log_path(log_dir)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        _logger.warning("audit_log_write_failed", audit_event=event_type.value, exc_info=True)


# -- Query -----------------------------------------------------------------


def query_audit_log(
    *,
    since: str | None = None,
    event_type: AuditEvent | None = None,
    user_id: str | None = None,
    limit: int = 100,
    log_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Read and filter entries from the audit log.

    Returns entries newest-first, truncated to *limit* after filtering.
    Missing or unreadable files return an empty list.  Corrupt JSON lines
    are skipped with a warning.

    Args:
        since: ISO-8601 timestamp lower bound (inclusive, UTC string
            comparison — safe because all timestamps are UTC ISO-8601).
        event_type: Keep only entries matching this event type.
        user_id: Keep only entries for this user.
        limit: Maximum entries to return (applied after filtering).
        log_dir: Override for the log directory (useful in tests).
    """
    if log_dir is None:
        log_dir = Path.cwd() / ".dango" / "logs"
    log_path = log_dir / "audit.jsonl"

    if not log_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line_num, raw_line in enumerate(fh, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record: dict[str, Any] = json.loads(raw_line)
                except json.JSONDecodeError:
                    _logger.warning("audit_log_corrupt_line", line=line_num, path=str(log_path))
                    continue

                # Apply filters
                if since is not None and record.get("timestamp", "") < since:
                    continue
                if event_type is not None and record.get("event") != event_type.value:
                    continue
                if user_id is not None and record.get("user_id") != user_id:
                    continue

                entries.append(record)
    except OSError:
        _logger.warning("audit_log_read_failed", path=str(log_path), exc_info=True)
        return []

    # Newest first, then apply limit
    entries.reverse()
    return entries[:limit]
