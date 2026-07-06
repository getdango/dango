"""dango/web/routes/scripts.py

Script management API endpoints and page route.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import dango
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.web.helpers import append_log_entry, get_project_root
from dango.web.routes.ui import _render_template
from dango.web.routes.websocket import ws_manager

router = APIRouter(tags=["scripts"])
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


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@router.get("/scripts")
async def scripts_page(
    request: Request,
    user: User = Depends(require_permission("source.view")),
) -> HTMLResponse:
    """Render the scripts management UI page."""
    return _render_template(
        request,
        "scripts.html",
        {
            "version": dango.__version__,
            "current_page": "scripts",
            "subtitle": "Scripts",
        },
    )


@router.get("/scripts/{name:path}/logs/{run_id}")
async def script_log_page(
    name: str,
    run_id: str,
    request: Request,
    user: User = Depends(require_permission("source.view")),
) -> HTMLResponse:
    """Render the script execution log viewer page."""
    project_root = get_project_root()

    validated = _validate_script_path(project_root, name)
    if isinstance(validated, JSONResponse):
        return _render_template(
            request,
            "script_log.html",
            {
                "version": dango.__version__,
                "current_page": "scripts",
                "subtitle": "Script Log",
                "error": f"Script {name!r} not found.",
                "script_name": name,
            },
            status_code=404,
        )

    log_dir = _get_log_dir(project_root) / run_id
    stdout_path = log_dir / "stdout.txt"
    stderr_path = log_dir / "stderr.txt"
    meta_path = log_dir / "meta.json"

    if not log_dir.exists():
        return _render_template(
            request,
            "script_log.html",
            {
                "version": dango.__version__,
                "current_page": "scripts",
                "subtitle": "Script Log",
                "error": f"Log for run {run_id!r} not found.",
                "script_name": name,
            },
            status_code=404,
        )

    stdout_content = ""
    stderr_content = ""
    meta: dict[str, Any] = {}

    if stdout_path.exists():
        stdout_content = stdout_path.read_text(encoding="utf-8", errors="replace")
    if stderr_path.exists():
        stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    return _render_template(
        request,
        "script_log.html",
        {
            "version": dango.__version__,
            "current_page": "scripts",
            "subtitle": "Script Log",
            "script_name": name,
            "run_id": run_id,
            "stdout": stdout_content,
            "stderr": stderr_content,
            "meta": meta,
        },
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@router.get("/api/scripts")
async def list_scripts(
    user: User = Depends(require_permission("source.view")),
) -> JSONResponse:
    """List discovered scripts with last run and running status."""
    project_root = get_project_root()
    scripts = _discover_scripts(project_root)

    result: list[dict[str, Any]] = []
    for script in scripts:
        name = script["name"]
        # Get last run from history
        history = _load_history(project_root, name, limit=1)
        last_run = history[0] if history else None

        entry: dict[str, Any] = {
            "name": name,
            "path": script["path"],
            "last_run": last_run,
            "running": name in _running_processes,
        }
        result.append(entry)

    return JSONResponse(content=result)


@router.post("/api/scripts/{name:path}/run")
async def run_script(
    name: str,
    request: Request,
    user: User = Depends(require_permission("source.sync")),
) -> JSONResponse:
    """Launch a script as a subprocess."""
    project_root = get_project_root()

    validated = _validate_script_path(project_root, name)
    if isinstance(validated, JSONResponse):
        return validated

    script_path = validated

    # Check not already running
    if name in _running_processes:
        proc = _running_processes[name]
        if proc.poll() is None:
            return JSONResponse(
                status_code=409,
                content={
                    "error_code": "DANGO-SC003",
                    "message": f"Script {name!r} is already running.",
                },
            )
        # Process finished but wasn't cleaned up — clean it now
        del _running_processes[name]

    run_id = str(uuid.uuid4())
    log_dir = _get_log_dir(project_root) / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)

    # Write initial meta
    meta: dict[str, Any] = {
        "run_id": run_id,
        "script_name": name,
        "started_at": started_at.isoformat(),
        "status": "running",
    }
    (log_dir / "meta.json").write_text(json.dumps(meta))

    # Launch subprocess
    env = os.environ.copy()
    env["DANGO_PROJECT_ROOT"] = str(project_root)
    env["DANGO_SCRIPT_RUN_ID"] = run_id

    try:
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(project_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as e:
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "DANGO-SC004",
                "message": f"Failed to launch script: {e}",
            },
        )

    _running_processes[name] = proc

    # Activity log entry
    append_log_entry(
        {
            "timestamp": started_at.isoformat(),
            "level": "info",
            "source": f"script:{name}",
            "message": f"Script '{name}' started (run {run_id})",
        }
    )

    # Audit
    _audit(
        AuditEvent.SCRIPT_RUN,
        user,
        request,
        project_root,
        script_name=name,
        run_id=run_id,
    )

    # Background task: wait for completion and record results
    loop = asyncio.get_running_loop()

    async def _wait_for_completion() -> None:
        stdout = ""
        stderr = ""
        try:
            stdout, stderr = await asyncio.wait_for(
                loop.run_in_executor(None, proc.communicate),
                timeout=_SCRIPT_TIMEOUT,
            )
            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()
            exit_code = proc.returncode if proc.returncode is not None else -1
            status = "success" if exit_code == 0 else "failed"
            error = None if exit_code == 0 else f"Exit code: {exit_code}"

        except asyncio.TimeoutError:
            # Timeout — terminate, then kill
            proc.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(
                    loop.run_in_executor(None, proc.communicate),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await loop.run_in_executor(None, proc.communicate)

            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()
            exit_code = proc.returncode if proc.returncode is not None else -1
            status = "timeout"
            error = f"Script timed out after {_SCRIPT_TIMEOUT}s"

        try:
            # Write stdout/stderr (truncated)
            stdout_str = stdout or ""
            stderr_str = stderr or ""
            if len(stdout_str) > _MAX_STDOUT_SIZE:
                stdout_str = stdout_str[:_MAX_STDOUT_SIZE] + "\n\n[TRUNCATED at 1MB]"
            if len(stderr_str) > _MAX_STDERR_SIZE:
                stderr_str = stderr_str[:_MAX_STDERR_SIZE] + "\n\n[TRUNCATED at 1MB]"

            (log_dir / "stdout.txt").write_text(stdout_str, encoding="utf-8", errors="replace")
            (log_dir / "stderr.txt").write_text(stderr_str, encoding="utf-8", errors="replace")

            # Update meta
            meta_update: dict[str, Any] = {
                "run_id": run_id,
                "script_name": name,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round(duration, 1),
                "status": status,
                "exit_code": exit_code,
                "error": error,
            }
            (log_dir / "meta.json").write_text(json.dumps(meta_update))

            # Append history entry
            _append_history(project_root, name, meta_update)

            # Remove from running processes
            _running_processes.pop(name, None)

            # Broadcast via WebSocket
            event_map = {
                "success": "script_completed",
                "failed": "script_failed",
                "timeout": "script_timed_out",
            }
            ws_event = event_map.get(status, "script_failed")
            try:
                await ws_manager.broadcast(
                    {
                        "event": ws_event,
                        "script": name,
                        "run_id": run_id,
                        "status": status,
                        "exit_code": exit_code,
                        "duration_seconds": round(duration, 1),
                        "message": f"Script '{name}' {status} (exit code: {exit_code})",
                        "timestamp": finished_at.isoformat(),
                    },
                    log=False,
                )
            except Exception:
                logger.debug("script_ws_broadcast_failed", script=name, exc_info=True)
        except Exception:
            logger.warning(
                "script_post_execution_failed",
                script=name,
                run_id=run_id,
                exc_info=True,
            )
            _running_processes.pop(name, None)

    asyncio.create_task(_wait_for_completion())

    return JSONResponse(
        content={
            "status": "started",
            "run_id": run_id,
            "script_name": name,
        }
    )


@router.post("/api/scripts/{name:path}/cancel")
async def cancel_script(
    name: str,
    request: Request,
    user: User = Depends(require_permission("source.sync")),
) -> JSONResponse:
    """Cancel a running script (SIGTERM → 5s → SIGKILL)."""
    project_root = get_project_root()

    validated = _validate_script_path(project_root, name)
    if isinstance(validated, JSONResponse):
        return validated

    if name not in _running_processes:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-SC005",
                "message": f"No running process found for script {name!r}.",
            },
        )

    proc = _running_processes[name]
    if proc.poll() is not None:
        # Already finished
        del _running_processes[name]
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-SC005",
                "message": f"No running process found for script {name!r}.",
            },
        )

    already_cancelling = name in _cancelling

    # SIGTERM
    proc.terminate()
    _cancelling.add(name)
    loop = asyncio.get_running_loop()

    async def _force_kill() -> None:
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, proc.wait),
                timeout=5,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await loop.run_in_executor(None, proc.wait)

        _running_processes.pop(name, None)
        _cancelling.discard(name)

        # Broadcast cancellation
        try:
            await ws_manager.broadcast(
                {
                    "event": "script_cancelled",
                    "script": name,
                    "message": f"Script '{name}' was cancelled.",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                },
                log=False,
            )
        except Exception:
            logger.debug("script_cancel_ws_broadcast_failed", script=name, exc_info=True)

    asyncio.create_task(_force_kill())

    # Activity log and audit — only on first cancel
    if not already_cancelling:
        append_log_entry(
            {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "level": "info",
                "source": f"script:{name}",
                "message": f"Script '{name}' cancellation requested",
            }
        )

        _audit(
            AuditEvent.SCRIPT_CANCELLED,
            user,
            request,
            project_root,
            script_name=name,
        )

    return JSONResponse(content={"status": "cancelling", "script_name": name})


@router.get("/api/scripts/{name:path}/history")
async def get_script_history(
    name: str,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_permission("source.view")),
) -> JSONResponse:
    """Get paginated execution history for a script."""
    project_root = get_project_root()

    validated = _validate_script_path(project_root, name)
    if isinstance(validated, JSONResponse):
        return validated

    # Load without limit first to know total, then paginate
    all_entries = _load_history(project_root, name, limit=0)  # 0 = no limit
    total = len(all_entries)
    page = all_entries[offset : offset + limit]

    return JSONResponse(
        content={
            "items": page,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/api/scripts/{name:path}")
async def get_script(
    name: str,
    user: User = Depends(require_permission("source.view")),
) -> JSONResponse:
    """Get details for a single script."""
    project_root = get_project_root()

    validated = _validate_script_path(project_root, name)
    if isinstance(validated, JSONResponse):
        return validated

    last_run = None
    history = _load_history(project_root, name, limit=1)
    if history:
        last_run = history[0]

    return JSONResponse(
        content={
            "name": name,
            "path": name,
            "last_run": last_run,
            "running": name in _running_processes,
        }
    )
