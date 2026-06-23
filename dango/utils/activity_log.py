"""dango/utils/activity_log.py

Centralized activity logging for both CLI and Web UI.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import dango

LogLevel = Literal["info", "success", "warning", "error"]
LogCategory = Literal["core", "auxiliary"]


def get_activity_log_file(project_root: Path) -> Path:
    """Get path to persistent activity log file"""
    logs_dir = project_root / ".dango" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "activity.jsonl"


def log_activity(
    project_root: Path,
    level: LogLevel,
    source: str,
    message: str,
    timestamp: str | None = None,
    category: LogCategory = "core",
) -> None:
    """
    Write an activity log entry

    Args:
        project_root: Project root directory
        level: Log level (info, success, warning, error)
        source: Source name or system component
        message: Log message (will be trimmed of extra whitespace)
        timestamp: ISO timestamp (defaults to now)
        category: Event category — "core" (syncs, schedules, failures) or
                  "auxiliary" (queries, notebooks). Defaults to "core".
    """
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc).isoformat()

    log_entry = {
        "timestamp": timestamp,
        "level": level,
        "source": source,
        "message": message.strip(),  # Remove leading/trailing whitespace
        "category": category,
        "dango_version": dango.__version__,
    }

    log_file = get_activity_log_file(project_root)

    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception as e:
        # Don't fail if logging fails
        print(f"Warning: Failed to write activity log: {e}")
