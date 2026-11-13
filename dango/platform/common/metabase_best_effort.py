"""dango/platform/common/metabase_best_effort.py

Logging for best-effort Metabase credential steps whose failure must not change the outcome.
"""

from __future__ import annotations

from typing import Any

from dango.logging import get_logger

_logger = get_logger(__name__)


def log_best_effort_failure(event: str, exc: BaseException) -> None:
    """Log ``event`` with the exception type only (messages may carry credential material)."""
    _logger.warning(event, error_type=type(exc).__name__)


def discard_pending_best_effort(store: Any) -> None:
    """Drop the staged candidate credential; a failure is logged and otherwise ignored."""
    try:
        store.discard_pending()
    except Exception as exc:
        log_best_effort_failure("pending_credential_discard_failed", exc)
