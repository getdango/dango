"""dango/web/routes/scripts_helpers.py

Discovery, validation, history, and audit helpers for script management.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.logging import get_logger

logger = get_logger(__name__)

# Module-level state: tracks running subprocesses for cancel support.
# Keyed by relative script path (e.g., "marketing/report.py").
_running_processes: dict[str, subprocess.Popen] = {}

# Tracks scripts with a cancellation in flight to prevent duplicate audit
# events from rapid double-clicks on the Cancel button.
_cancelling: set[str] = set()

_MAX_STDOUT_SIZE = 1_048_576  # 1 MB
_MAX_STDERR_SIZE = 1_048_576  # 1 MB
_SCRIPT_TIMEOUT = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    event: AuditEvent,
    user: User,
    request: Request,
    project_root: Path,
    **extra: Any,
) -> None:
    """Log an audit event with standard fields."""
    log_auth_event(
        event,
        user_id=user.id,
        email=user.email,
        ip=request.client.host if request.client else None,
        details=extra,
        log_dir=project_root / ".dango" / "logs",
    )


def _get_scripts_dir(project_root: Path) -> Path:
    """Return the ``scripts/`` directory, creating it if missing."""
    scripts_dir = project_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir


def _get_history_dir(project_root: Path) -> Path:
    """Return the script history directory, creating it if missing."""
    history_dir = project_root / ".dango" / "logs" / "scripts"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def _get_log_dir(project_root: Path) -> Path:
    """Return the script run log directory, creating it if missing."""
    log_dir = _get_history_dir(project_root) / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _get_history_file(project_root: Path, script_name: str) -> Path:
    """Return the JSONL history file path for a script."""
    safe = _safe_filename(script_name)
    return _get_history_dir(project_root) / f"{safe}.jsonl"


def _safe_filename(script_name: str) -> str:
    """Replace ``/`` with ``__`` for filesystem-safe filenames."""
    return script_name.replace("/", "__").replace("\\", "__")


def _discover_scripts(project_root: Path) -> list[dict[str, Any]]:
    """Recursively walk ``scripts/`` and return sorted ``.py`` file entries.

    Skips ``__init__.py``, dotfiles, and ``_``-prefixed files and dirs.
    """
    scripts_dir = _get_scripts_dir(project_root)
    result: list[dict[str, Any]] = []

    if not scripts_dir.exists():
        return result

    for py_file in sorted(scripts_dir.rglob("*.py")):
        # Skip __init__.py
        if py_file.name == "__init__.py":
            continue
        # Skip dotfiles
        if py_file.name.startswith("."):
            continue
        # Skip _-prefixed files and directories
        if any(part.startswith("_") for part in py_file.parts[:-1]) or py_file.name.startswith("_"):
            continue

        rel_path = str(py_file.relative_to(scripts_dir))
        result.append({"name": rel_path, "path": rel_path})

    return result


def _validate_script_path(project_root: Path, script_name: str) -> Path | JSONResponse:
    """Resolve and validate a script path.

    Returns the absolute ``Path`` on success, or a ``JSONResponse`` on
    failure (404 if not found, 400 if path traversal detected).
    """
    scripts_dir = _get_scripts_dir(project_root)
    script_path = (scripts_dir / script_name).resolve()

    # Prevent path traversal — path must be inside scripts/
    if not str(script_path).startswith(str(scripts_dir.resolve())):
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-SC001",
                "message": "Invalid script path.",
            },
        )

    if not script_path.exists() or not script_path.is_file():
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-SC002",
                "message": f"Script {script_name!r} not found.",
            },
        )

    return script_path


def _load_history(
    project_root: Path,
    script_name: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read execution history from JSONL, newest first."""
    history_file = _get_history_file(project_root, script_name)
    if not history_file.exists():
        return []

    entries: list[dict[str, Any]] = []
    try:
        with open(history_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("script_history_corrupt_line", path=str(history_file))
                    continue
    except OSError:
        logger.warning("script_history_read_failed", path=str(history_file), exc_info=True)
        return []

    # Newest first
    entries.reverse()
    if limit > 0:
        entries = entries[:limit]
    return entries


def _append_history(project_root: Path, script_name: str, entry: dict[str, Any]) -> None:
    """Append one JSON line to the script's history file."""
    history_file = _get_history_file(project_root, script_name)
    try:
        with open(history_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        logger.warning("script_history_write_failed", path=str(history_file), exc_info=True)
