"""dango/web/routes/scripts_api.py

Script API endpoints: list, run, cancel, history, single detail.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from dango.auth.audit import AuditEvent
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.web.helpers import append_log_entry, get_project_root
from dango.web.routes.scripts_helpers import (
    _MAX_STDERR_SIZE,
    _MAX_STDOUT_SIZE,
    _append_history,
    _audit,
    _cancelling,
    _discover_scripts,
    _get_log_dir,
    _get_script_timeout,
    _load_history,
    _running_processes,
    _validate_script_path,
    logger,
)
from dango.web.routes.websocket import ws_manager

api_router = APIRouter(tags=["scripts"])


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@api_router.get("/api/scripts")
async def list_scripts(
    user: User = Depends(require_permission("source.view")),
) -> JSONResponse:
    """List discovered scripts with last run and running status."""
    project_root = get_project_root()
    scripts = _discover_scripts(project_root)

    result: list[dict[str, Any]] = []
    for script in scripts:
        name = script["name"]
        history = _load_history(project_root, name, limit=1)
        last_run = history[0] if history else None

        entry: dict[str, Any] = {
            "name": name,
            "path": script["path"],
            "last_run": last_run,
            "running": name in _running_processes,
            "timeout_seconds": script["timeout_seconds"],
        }
        result.append(entry)

    return JSONResponse(content=result)


@api_router.post("/api/scripts/{name:path}/run")
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
    timeout_seconds = _get_script_timeout(project_root, name)

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
        del _running_processes[name]

    run_id = str(uuid.uuid4())
    log_dir = _get_log_dir(project_root) / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)

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

    append_log_entry(
        {
            "timestamp": started_at.isoformat(),
            "level": "info",
            "source": f"script:{name}",
            "message": f"Script '{name}' started (run {run_id})",
        }
    )

    _audit(
        AuditEvent.SCRIPT_RUN,
        user,
        request,
        project_root,
        script_name=name,
        run_id=run_id,
    )

    loop = asyncio.get_running_loop()

    async def _wait_for_completion() -> None:
        stdout = ""
        stderr = ""
        try:
            stdout, stderr = await asyncio.wait_for(
                loop.run_in_executor(None, proc.communicate),
                timeout=timeout_seconds,
            )
            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()
            exit_code = proc.returncode if proc.returncode is not None else -1
            status = "success" if exit_code == 0 else "failed"
            error = None if exit_code == 0 else f"Exit code: {exit_code}"

        except asyncio.TimeoutError:
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
            error = f"Script timed out after {timeout_seconds}s"

        try:
            stdout_str = stdout or ""
            stderr_str = stderr or ""
            if len(stdout_str) > _MAX_STDOUT_SIZE:
                stdout_str = stdout_str[:_MAX_STDOUT_SIZE] + "\n\n[TRUNCATED at 1MB]"
            if len(stderr_str) > _MAX_STDERR_SIZE:
                stderr_str = stderr_str[:_MAX_STDERR_SIZE] + "\n\n[TRUNCATED at 1MB]"

            (log_dir / "stdout.txt").write_text(stdout_str, encoding="utf-8", errors="replace")
            (log_dir / "stderr.txt").write_text(stderr_str, encoding="utf-8", errors="replace")

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

            _append_history(project_root, name, meta_update)
            _running_processes.pop(name, None)

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


@api_router.post("/api/scripts/{name:path}/cancel")
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
        del _running_processes[name]
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-SC005",
                "message": f"No running process found for script {name!r}.",
            },
        )

    already_cancelling = name in _cancelling

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


@api_router.get("/api/scripts/{name:path}/history")
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


@api_router.get("/api/scripts/{name:path}")
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
