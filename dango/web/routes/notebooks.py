"""dango/web/routes/notebooks.py

Notebook management API endpoints and page route.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import dango
import dango.notebooks.templates
from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import has_permission, require_permission
from dango.logging import get_logger
from dango.notebooks.locking import (
    acquire_lock,
    copy_locked_notebook,
    expire_stale_locks,
    force_release_lock,
    get_lock_info,
    refresh_lock,
    release_lock,
)
from dango.notebooks.manager import get_marimo_status, start_idle_checker, start_marimo, stop_marimo
from dango.notebooks.snapshot import create_snapshot
from dango.utils.dango_db import connect
from dango.validation import validate_identifier
from dango.web.helpers import get_project_root
from dango.web.routes.ui import _render_template
from dango.web.routes.websocket import ws_manager

router = APIRouter(tags=["notebooks"])
logger = get_logger(__name__)

_VALID_TEMPLATES = frozenset({"blank", "explore", "quality"})
_DEFAULT_MARIMO_PORT = 7805


def _validate_name(name: str) -> str | JSONResponse:
    """Return the normalized name on success, or a 400 response on failure."""
    try:
        return validate_identifier(name)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-NB001",
                "message": "Invalid notebook name. Use only letters, numbers, and underscores.",
            },
        )


async def _start_marimo_background(project_root: Path, user_email: str) -> None:
    """Create snapshot and start Marimo in the background (non-blocking).

    Launched via ``asyncio.create_task()`` so the lock endpoint can return
    immediately while the snapshot copy and Marimo startup complete.
    """
    snapshot_path = None
    try:
        snapshot_path = await asyncio.to_thread(create_snapshot, project_root, user_email)
    except FileNotFoundError:  # noqa: BLE001
        pass  # no warehouse yet
    try:
        await asyncio.to_thread(start_marimo, project_root, snapshot_path=snapshot_path)
    except RuntimeError:
        pass  # already running (race condition)


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


def _list_notebooks_blocking(project_root: Path) -> list[dict[str, Any]]:
    """Query notebook metadata, scan filesystem, and merge lock info."""
    notebooks_dir = project_root / "notebooks"

    # Query metadata table
    metadata: dict[str, dict[str, Any]] = {}
    with connect(project_root) as conn:
        rows = conn.execute(
            "SELECT id, name, description, created_by, created_at, updated_at "
            "FROM notebook_metadata ORDER BY created_at DESC"
        ).fetchall()
        for row in rows:
            metadata[row["name"]] = {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    # Scan filesystem for .py files
    fs_names: set[str] = set()
    if notebooks_dir.exists():
        for f in notebooks_dir.glob("*.py"):
            if f.name == "__init__.py":
                continue
            fs_names.add(f.stem)

    # Merge: metadata entries + unregistered files
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for name, meta in metadata.items():
        entry: dict[str, Any] = {**meta}
        entry["file_exists"] = name in fs_names
        lock = get_lock_info(project_root, name)
        entry["lock"] = lock
        result.append(entry)
        seen.add(name)

    for name in sorted(fs_names - seen):
        result.append(
            {
                "id": None,
                "name": name,
                "description": None,
                "created_by": None,
                "created_at": None,
                "updated_at": None,
                "file_exists": True,
                "lock": get_lock_info(project_root, name),
            }
        )

    return result


def _get_notebook_by_name(project_root: Path, name: str) -> dict[str, Any] | None:
    """Look up a notebook in the metadata table by name."""
    with connect(project_root) as conn:
        row = conn.execute(
            "SELECT id, name, description, created_by, created_at, updated_at "
            "FROM notebook_metadata WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def _create_notebook_blocking(
    project_root: Path,
    name: str,
    template: str,
    user_email: str,
) -> dict[str, Any]:
    """Copy template file and insert metadata row."""
    notebooks_dir = project_root / "notebooks"
    notebooks_dir.mkdir(parents=True, exist_ok=True)

    target = notebooks_dir / f"{name}.py"
    if target.exists():
        raise FileExistsError(f"Notebook file already exists: {name}.py")

    templates_dir = Path(dango.notebooks.templates.__file__).parent
    template_file = templates_dir / f"{template}.py"
    shutil.copy2(str(template_file), str(target))

    new_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()

    with connect(project_root) as conn:
        conn.execute(
            "INSERT INTO notebook_metadata (id, name, description, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, name, f"Created from {template} template", user_email, now, now),
        )
        conn.commit()

    return {"id": new_id, "name": name, "template": template}


def _delete_notebook_blocking(project_root: Path, name: str) -> None:
    """Delete notebook file, metadata row, and any lock."""
    notebooks_dir = project_root / "notebooks"
    target = notebooks_dir / f"{name}.py"
    if target.exists():
        target.unlink()

    with connect(project_root) as conn:
        conn.execute("DELETE FROM notebook_metadata WHERE name = ?", (name,))
        conn.execute("DELETE FROM notebook_locks WHERE notebook_id = ?", (name,))
        conn.commit()


@router.get("/notebooks")
async def notebooks_page(
    request: Request,
    user: User = Depends(require_permission("notebooks.view")),
) -> HTMLResponse:
    """Render the notebook management UI page."""
    return _render_template(
        request,
        "notebooks.html",
        {
            "version": dango.__version__,
            "current_page": "notebooks",
            "subtitle": "Notebooks",
        },
    )


@router.get("/api/notebooks")
async def list_notebooks(
    user: User = Depends(require_permission("notebooks.view")),
) -> JSONResponse:
    """List all notebooks with metadata and lock status.

    NOTE: Lock state is always fresh on each request (queried from the
    ``notebook_locks`` table).  CLI-acquired locks are immediately visible
    after a page refresh.  Real-time push notification of lock changes
    (e.g., via WebSocket) is a post-v1 enhancement.
    """
    project_root = get_project_root()
    notebooks = await asyncio.to_thread(_list_notebooks_blocking, project_root)
    return JSONResponse(content=notebooks)


@router.post("/api/notebooks")
async def create_notebook(
    request: Request,
    user: User = Depends(require_permission("notebooks.execute")),
) -> JSONResponse:
    """Create a new notebook from a template."""
    project_root = get_project_root()

    body: dict[str, Any] = await request.json()
    name = body.get("name", "")
    template = body.get("template", "blank")

    validated = _validate_name(name)
    if isinstance(validated, JSONResponse):
        return validated
    name = validated
    if template not in _VALID_TEMPLATES:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-NB002",
                "message": f"Invalid template. Must be one of: {', '.join(sorted(_VALID_TEMPLATES))}.",
            },
        )

    try:
        result = await asyncio.to_thread(
            _create_notebook_blocking, project_root, name, template, user.email
        )
    except FileExistsError:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-NB003",
                "message": f"Notebook {name!r} already exists.",
            },
        )

    _audit(
        AuditEvent.NOTEBOOK_CREATED,
        user,
        request,
        project_root,
        notebook_name=name,
        template=template,
    )

    return JSONResponse(status_code=201, content=result)


@router.delete("/api/notebooks/{name}")
async def delete_notebook(
    name: str,
    request: Request,
    user: User = Depends(require_permission("notebooks.execute")),
) -> JSONResponse:
    """Delete a notebook file and metadata."""
    result = _validate_name(name)
    if isinstance(result, JSONResponse):
        return result
    name = result

    project_root = get_project_root()

    notebook = await asyncio.to_thread(_get_notebook_by_name, project_root, name)

    notebooks_dir = project_root / "notebooks"
    file_exists = (notebooks_dir / f"{name}.py").exists()

    if notebook is None and not file_exists:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-NB004",
                "message": f"Notebook {name!r} not found.",
            },
        )

    # Refuse delete if locked by another user
    lock = await asyncio.to_thread(get_lock_info, project_root, name)
    if lock and lock["locked_by"] != user.email:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "DANGO-NB011",
                "message": f"Notebook is locked by {lock['locked_by']}. Release the lock first.",
            },
        )

    # Check ownership: if user lacks notebooks.manage, they can only delete their own
    if notebook is not None and notebook.get("created_by") != user.email:
        if not has_permission(user, "notebooks.manage"):
            return JSONResponse(
                status_code=403,
                content={
                    "error_code": "DANGO-NB005",
                    "message": "You can only delete notebooks you created.",
                },
            )

    await asyncio.to_thread(_delete_notebook_blocking, project_root, name)

    _audit(
        AuditEvent.NOTEBOOK_DELETED,
        user,
        request,
        project_root,
        notebook_name=name,
    )

    return JSONResponse(content={"status": "deleted", "name": name})


@router.post("/api/notebooks/{name}/lock")
async def lock_notebook(
    name: str,
    request: Request,
    user: User = Depends(require_permission("notebooks.execute")),
) -> JSONResponse:
    """Acquire editing lock and start Marimo if needed."""
    result = _validate_name(name)
    if isinstance(result, JSONResponse):
        return result
    name = result

    project_root = get_project_root()

    notebooks_dir = project_root / "notebooks"
    if not (notebooks_dir / f"{name}.py").exists():
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-NB004",
                "message": f"Notebook {name!r} not found.",
            },
        )

    acquired = await asyncio.to_thread(acquire_lock, project_root, name, user.email)
    if not acquired:
        lock = await asyncio.to_thread(get_lock_info, project_root, name)
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "DANGO-NB006",
                "message": f"Notebook is locked by {lock['locked_by'] if lock else 'another user'}.",
                "lock": lock,
            },
        )

    status = await asyncio.to_thread(get_marimo_status, project_root)
    already_running = status["running"]
    if not already_running:
        # Launch snapshot creation + Marimo startup as a background task
        # so the API returns immediately (non-blocking).
        asyncio.create_task(_start_marimo_background(project_root, user.email))
        start_idle_checker(project_root)
        return JSONResponse(
            content={"locked": True, "ready": False, "status": "starting", "marimo_url": None}
        )

    logger.debug("Marimo already running — skipping snapshot creation")

    port = status.get("port") or _DEFAULT_MARIMO_PORT  # type: ignore[assignment]

    # BUG-241: Cloud mode routes through FastAPI notebook proxy (auth-protected).
    # --base-url /notebooks/marimo is set in manager.py start_marimo() when
    # is_running_on_cloud(). TODO(R12-M): Verify on cloud VM.
    from dango.config.helpers import is_running_on_cloud

    if is_running_on_cloud():
        marimo_url = f"/notebooks/marimo/?file={name}.py"
    else:
        marimo_url = f"http://localhost:{port}/?file={name}.py"

    start_idle_checker(project_root)

    return JSONResponse(content={"locked": True, "marimo_url": marimo_url, "ready": True})


@router.get("/api/notebooks/status")
async def notebook_status(
    request: Request,
    file: str | None = None,
) -> JSONResponse:
    """Poll notebook server readiness. Used by frontend after non-blocking open."""
    project_root = get_project_root()
    status = await asyncio.to_thread(get_marimo_status, project_root)
    if not status["running"]:
        return JSONResponse(content={"ready": False, "marimo_url": None})

    port = status.get("port") or _DEFAULT_MARIMO_PORT  # type: ignore[assignment]
    from dango.config.helpers import is_running_on_cloud

    if is_running_on_cloud():
        base = "/notebooks/marimo/"
        marimo_url = f"{base}?file={file}" if file else base
    else:
        marimo_url = (
            f"http://localhost:{port}/?file={file}" if file else f"http://localhost:{port}/"
        )

    return JSONResponse(content={"ready": True, "marimo_url": marimo_url})


@router.post("/api/notebooks/marimo/restart")
async def restart_marimo(
    request: Request,
    user: User = Depends(require_permission("notebooks.manage")),
) -> JSONResponse:
    """Admin/editor: restart Marimo server, releasing all locks.

    Returns the list of affected lock holders so the frontend can warn users.
    The next notebook open will create a fresh DuckDB snapshot.
    """
    project_root = get_project_root()

    # Collect active (non-expired) lock holders for the warning
    active_locks: list[dict[str, str]] = []
    with connect(project_root) as conn:
        conn.execute("DELETE FROM notebook_locks WHERE expires_at < datetime('now')")
        rows = conn.execute("SELECT notebook_id, locked_by FROM notebook_locks").fetchall()
        active_locks = [{"notebook": r["notebook_id"], "user": r["locked_by"]} for r in rows]

    # Stop Marimo
    stop_marimo(project_root)

    # Release all locks (inlined from manager._release_all_locks to avoid
    # importing a private function across modules)
    with connect(project_root) as conn:
        conn.execute("DELETE FROM notebook_locks")
        conn.commit()

    # Audit
    log_auth_event(
        AuditEvent.NOTEBOOK_MARIMO_RESTARTED,
        user_id=user.id,
        email=user.email,
        ip=request.client.host if request.client else None,
        details={"active_locks": len(active_locks)},
    )

    return JSONResponse(
        content={
            "restarted": True,
            "affected_users": active_locks,
            "message": "Marimo restarted. Next notebook open will create a fresh snapshot.",
        }
    )


@router.post("/api/notebooks/{name}/heartbeat")
async def heartbeat_notebook(
    name: str,
    user: User = Depends(require_permission("notebooks.execute")),
) -> JSONResponse:
    """Refresh lock expiry for an active editing session."""
    project_root = get_project_root()

    await asyncio.to_thread(expire_stale_locks, project_root)

    refreshed = await asyncio.to_thread(refresh_lock, project_root, name, user.email)
    if not refreshed:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "DANGO-NB007",
                "message": "Lock not held by you or has expired.",
            },
        )

    return JSONResponse(content={"refreshed": True})


@router.post("/api/notebooks/{name}/release")
async def release_notebook_lock(
    name: str,
    user: User = Depends(require_permission("notebooks.execute")),
) -> JSONResponse:
    """Release editing lock on a notebook."""
    project_root = get_project_root()

    released = await asyncio.to_thread(release_lock, project_root, name, user.email)
    if not released:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "DANGO-NB008",
                "message": "Lock not held by you or already released.",
            },
        )

    return JSONResponse(content={"released": True})


@router.delete("/api/notebooks/{name}/lock")
async def force_release_notebook_lock(
    name: str,
    request: Request,
    user: User = Depends(require_permission("notebooks.manage")),
) -> JSONResponse:
    """Force-release a lock regardless of owner (admin/editor action)."""
    project_root = get_project_root()

    released = await asyncio.to_thread(force_release_lock, project_root, name)
    if not released:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-NB009",
                "message": f"No active lock found on notebook {name!r}.",
            },
        )

    _audit(
        AuditEvent.NOTEBOOK_LOCK_FORCE_RELEASED,
        user,
        request,
        project_root,
        notebook_name=name,
    )

    await ws_manager.broadcast(
        {
            "event": "notebook_lock_revoked",
            "notebook": name,
            "message": f"Lock on notebook '{name}' was force-released by an administrator.",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )

    return JSONResponse(content={"force_released": True})


@router.post("/api/notebooks/{name}/copy")
async def copy_notebook(
    name: str,
    user: User = Depends(require_permission("notebooks.execute")),
) -> JSONResponse:
    """Copy a locked notebook for the current user."""
    result = _validate_name(name)
    if isinstance(result, JSONResponse):
        return result
    name = result

    project_root = get_project_root()

    try:
        copy_name = await asyncio.to_thread(copy_locked_notebook, project_root, name, user.email)
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-NB010",
                "message": f"Notebook file {name!r} not found.",
            },
        )

    return JSONResponse(
        status_code=201,
        content={"copy_name": copy_name},
    )


# ---------------------------------------------------------------------------
# Notebook proxy routes — BUG-241: Cloud mode proxies Marimo through FastAPI
# WebSocket proxy for Marimo is handled by Caddy directly (not FastAPI).
# Caddy routes /notebooks/marimo/ws to Marimo's port, bypassing dango-web.
# This avoids FastAPI/Starlette WebSocket routing issues while keeping
# HTTP requests auth-protected through dango-web.
# ---------------------------------------------------------------------------


@router.api_route(
    "/notebooks/marimo/",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
@router.api_route(
    "/notebooks/marimo/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def notebook_marimo_proxy(
    request: Request,
    path: str = "",
    _user: User = Depends(require_permission("notebooks.execute")),
) -> Any:
    """Proxy HTTP requests to the local Marimo server (cloud mode)."""
    from dango.notebooks.proxy import proxy_to_marimo

    project_root = get_project_root()
    status = await asyncio.to_thread(get_marimo_status, project_root)
    if not status.get("running"):
        return JSONResponse(
            status_code=503,
            content={"error": "Notebook server not running"},
        )
    port = int(str(status["port"]))
    # Marimo runs with --base-url /notebooks/marimo in cloud mode.
    # Forward the full prefixed path so Marimo recognizes the request.
    target_path = f"/notebooks/marimo/{path}" if path else "/notebooks/marimo/"
    return await proxy_to_marimo(request, target_path, port)
