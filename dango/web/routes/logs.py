"""dango/web/routes/logs.py

Log retrieval endpoints.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.utils.redact import load_secret_values, redact_text
from dango.validation import validate_limit, validate_source_name
from dango.web.helpers import get_project_root, load_all_logs
from dango.web.models import LogEntry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["logs"])


def _redact_entry(value: Any, secrets: frozenset[str]) -> Any:
    """Redact every string in a log entry (nested values included)."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {k: _redact_entry(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_entry(v, secrets) for v in value]
    return value


@router.get("/api/sources/{source_name}/logs", response_model=list[LogEntry])
async def get_source_logs(
    source_name: str,
    limit: int = 100,
    user: User = Depends(require_permission("logs.view")),
) -> list[LogEntry]:
    """Get sync logs for a specific source.

    Args:
        source_name: Name of the source
        limit: Maximum number of log entries to return

    Returns:
        List of log entries
    """
    source_name = validate_source_name(source_name)
    limit = validate_limit(limit)
    log_file = get_project_root() / "logs" / f"{source_name}_sync.log"

    if not log_file.exists():
        return []

    try:
        secrets = load_secret_values(get_project_root())
        logs = []
        with open(log_file, encoding="utf-8") as f:
            lines = f.readlines()

            # Get last N lines
            for line in lines[-limit:]:
                # Parse log line (assuming format: timestamp - level - message)
                parts = line.strip().split(" - ", 2)
                if len(parts) >= 3:
                    logs.append(
                        LogEntry(
                            timestamp=redact_text(parts[0], secrets),
                            level=redact_text(parts[1], secrets),
                            message=redact_text(parts[2], secrets),
                        )
                    )
                else:
                    # Fallback for unparseable lines
                    logs.append(
                        LogEntry(
                            timestamp=datetime.now(tz=timezone.utc).isoformat(),
                            level="INFO",
                            message=redact_text(line.strip(), secrets),
                        )
                    )

        return logs

    except Exception as e:
        logger.error(f"Error reading logs for {source_name}: {e}")
        raise HTTPException(status_code=500, detail="Error reading logs") from e


@router.get("/api/logs")
async def get_all_logs(
    limit: int = 1000,
    category: str | None = None,
    user: User = Depends(require_permission("logs.view")),
) -> list[dict[str, object]]:
    """Get all activity logs.

    Args:
        limit: Maximum number of log entries to return (default 1000)
        category: Filter by category ("core" or "auxiliary"). None returns all.

    Returns:
        List of all log entries
    """
    limit = validate_limit(limit)
    # Validate category if provided
    if category is not None and category not in ("core", "auxiliary"):
        category = None
    try:
        secrets = load_secret_values(get_project_root())
        return [_redact_entry(entry, secrets) for entry in load_all_logs(limit, category)]
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        raise HTTPException(status_code=500, detail="Error fetching logs") from e
