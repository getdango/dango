"""dango/web/routes/scripts.py

Script page routes. API endpoints live in ``scripts_api.py``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import dango
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.web.helpers import get_project_root
from dango.web.routes.scripts_api import api_router
from dango.web.routes.scripts_helpers import (
    _get_log_dir,
    _validate_script_path,
)
from dango.web.routes.ui import _render_template

router = APIRouter(tags=["scripts"])
router.include_router(api_router)


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
