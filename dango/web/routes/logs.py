"""dango/web/routes/logs.py

Log retrieval endpoints.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException

from dango.validation import validate_limit, validate_source_name
from dango.web.helpers import get_project_root, load_all_logs
from dango.web.models import LogEntry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["logs"])


@router.get("/api/sources/{source_name}/logs", response_model=list[LogEntry])
async def get_source_logs(source_name: str, limit: int = 100):
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
        logs = []
        with open(log_file, encoding="utf-8") as f:
            lines = f.readlines()

            # Get last N lines
            for line in lines[-limit:]:
                # Parse log line (assuming format: timestamp - level - message)
                parts = line.strip().split(" - ", 2)
                if len(parts) >= 3:
                    logs.append(LogEntry(timestamp=parts[0], level=parts[1], message=parts[2]))
                else:
                    # Fallback for unparseable lines
                    logs.append(
                        LogEntry(
                            timestamp=datetime.now().isoformat(), level="INFO", message=line.strip()
                        )
                    )

        return logs

    except Exception as e:
        logger.error(f"Error reading logs for {source_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading logs: {str(e)}") from e


@router.get("/api/logs")
async def get_all_logs(limit: int = 1000):
    """Get all activity logs.

    Args:
        limit: Maximum number of log entries to return (default 1000)

    Returns:
        List of all log entries
    """
    limit = validate_limit(limit)
    try:
        logs = load_all_logs(limit=limit)
        return logs
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching logs: {str(e)}") from e
