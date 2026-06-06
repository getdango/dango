"""dango/web/routes/dbt.py

dbt model endpoints and dbt docs proxy.
"""

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response

from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.validation import validate_identifier
from dango.web.helpers import get_dbt_manifest, get_dbt_models, get_project_root
from dango.web.routes.websocket import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dbt"])


@router.get("/api/dbt/models")
async def list_dbt_models() -> dict[str, list]:
    """List all dbt models.

    Returns:
        List of dbt models with their metadata
    """
    try:
        models = get_dbt_models()
        return {"models": models}
    except Exception as e:
        logger.error(f"Error fetching dbt models: {e}")
        raise HTTPException(status_code=500, detail="Error fetching dbt models") from e


@router.post("/api/dbt/models/{model_name}/run")
async def run_dbt_model(
    model_name: str,
    background_tasks: BackgroundTasks,
    cascade: bool = True,
    user: User = Depends(require_permission("dbt.run")),
) -> dict[str, str | bool]:
    """Run a specific dbt model.

    Args:
        model_name: Name of the model to run
        cascade: Whether to cascade to downstream models (default True)

    Returns:
        Success message
    """
    model_name = validate_identifier(model_name)
    # Check if model exists (use manifest only, avoid DuckDB query which can block)
    manifest = get_dbt_manifest()
    if manifest:
        nodes = manifest.get("nodes", {})
        model_exists = any(
            node.get("resource_type") == "model" and node.get("name") == model_name
            for node in nodes.values()
        )
    else:
        model_exists = False

    if not model_exists:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

    # Broadcast start
    await ws_manager.broadcast(
        {
            "event": "dbt_run_started",
            "source": f"dbt:{model_name}",
            "message": f"Running dbt model: {model_name}",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )

    # Run in background
    background_tasks.add_task(run_dbt_model_task, model_name, cascade)

    return {
        "success": True,
        "message": f"dbt model '{model_name}' run started",
        "model_name": model_name,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
    }


async def run_dbt_model_task(model_name: str, cascade: bool) -> None:
    """Run dbt model in background."""
    from dango.utils import DbtLock, DbtLockError
    from dango.utils.dbt_status import update_model_status

    start_time = time.time()
    project_root = get_project_root()
    dbt_dir = project_root / "dbt"

    # Try to acquire lock before running dbt
    lock = None
    try:
        lock = DbtLock(
            project_root=project_root,
            source="ui",
            operation=f"dbt run {model_name}{'+ (cascade)' if cascade else ''}",
        )
        lock.acquire()
    except DbtLockError as e:
        # Lock is held by another process - broadcast error and return
        await ws_manager.broadcast(
            {
                "event": "dbt_run_failed",
                "source": f"dbt:{model_name}",
                "message": str(e).split("\n")[0],  # First line of error message
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
        from dango.utils.activity_log import log_activity

        log_activity(project_root, "warning", f"dbt:{model_name}", "dbt lock unavailable")
        logger.warning(f"Could not acquire dbt lock for {model_name}: {e}")
        return

    from dango.utils.activity_log import log_activity

    log_activity(project_root, "info", f"dbt:{model_name}", f"dbt run started: {model_name}")

    # Get dbt executable path (from venv or system PATH)
    python_bin_dir = Path(sys.executable).parent
    dbt_path = python_bin_dir / "dbt"
    dbt_cmd = str(dbt_path) if dbt_path.exists() else "dbt"

    try:
        # Build the dbt command
        if cascade:
            # Run model and all downstream models
            cmd = [
                dbt_cmd,
                "run",
                "--select",
                f"{model_name}+",
                "--project-dir",
                str(dbt_dir),
                "--profiles-dir",
                str(dbt_dir),
            ]
        else:
            # Run only this model
            cmd = [
                dbt_cmd,
                "run",
                "--select",
                model_name,
                "--project-dir",
                str(dbt_dir),
                "--profiles-dir",
                str(dbt_dir),
            ]

        # Broadcast progress
        await ws_manager.broadcast(
            {
                "event": "dbt_run_progress",
                "source": f"dbt:{model_name}",
                "message": f"Executing: {' '.join(cmd)}",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
        )

        # Run dbt
        result = subprocess.run(
            cmd,
            cwd=str(dbt_dir),
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        duration = time.time() - start_time

        if result.returncode == 0:
            # Update persistent model status
            update_model_status(project_root)

            # Success
            log_activity(
                project_root,
                "success",
                f"dbt:{model_name}",
                f"Model '{model_name}' completed in {duration:.1f}s",
            )
            await ws_manager.broadcast(
                {
                    "event": "dbt_run_completed",
                    "source": f"dbt:{model_name}",
                    "message": f"Model '{model_name}' ran successfully in {duration:.1f}s",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )

            # CRITICAL: Refresh Metabase connection to see new/updated tables
            from dango.visualization.metabase import refresh_metabase_connection

            project_root = get_project_root()

            mb_ok, _mb_err = refresh_metabase_connection(project_root)
            if mb_ok:
                await ws_manager.broadcast(
                    {
                        "event": "dbt_run_progress",
                        "source": f"dbt:{model_name}",
                        "message": "Metabase connection refreshed",
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    }
                )
        else:
            # Failed
            error_msg = result.stderr or result.stdout or "Unknown error"
            log_activity(
                project_root,
                "error",
                f"dbt:{model_name}",
                f"Model '{model_name}' failed: {error_msg[:200]}",
            )
            await ws_manager.broadcast(
                {
                    "event": "dbt_run_failed",
                    "source": f"dbt:{model_name}",
                    "message": f"Model '{model_name}' failed: {error_msg[:200]}",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )

    except subprocess.TimeoutExpired:
        log_activity(
            project_root,
            "error",
            f"dbt:{model_name}",
            f"Model '{model_name}' timed out after 5 minutes",
        )
        await ws_manager.broadcast(
            {
                "event": "dbt_run_failed",
                "source": f"dbt:{model_name}",
                "message": f"Model '{model_name}' timed out after 5 minutes",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
    except Exception as e:
        log_activity(
            project_root, "error", f"dbt:{model_name}", f"Model '{model_name}' failed: {e!s:.200}"
        )
        logger.error(f"Error running dbt model {model_name}: {e}")
        await ws_manager.broadcast(
            {
                "event": "dbt_run_failed",
                "source": f"dbt:{model_name}",
                "message": f"Model '{model_name}' failed: {str(e)}",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


# ==============================================================================
# dbt Docs Reverse Proxy
# ==============================================================================


@router.get("/manifest.json")
@router.get("/catalog.json")
async def dbt_docs_assets(request: Request) -> Response:
    """Proxy dbt docs JSON assets.

    dbt docs JavaScript loads these files using absolute paths,
    so we need to proxy them from the nginx container.
    """
    dbt_docs_url = "http://localhost:8081"
    target_url = f"{dbt_docs_url}{request.url.path}"

    if request.url.query:
        target_url += f"?{request.url.query}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            proxy_response = await client.get(target_url)

            # Return the JSON response
            return Response(
                content=proxy_response.content,
                status_code=proxy_response.status_code,
                headers=dict(proxy_response.headers),
            )
    except Exception as e:
        logger.error(f"dbt docs asset proxy error: {e}")
        return Response(content="Asset not found", status_code=404)


@router.api_route("/dbt-docs/{path:path}", methods=["GET"])
@router.api_route("/dbt-docs", methods=["GET"])
async def dbt_docs_proxy(request: Request, path: str = "") -> Response:
    """Reverse proxy for dbt docs with nav bar injection.

    Routes all requests to http://localhost:8081 and automatically
    injects Dango nav bar into HTML responses.
    """
    dbt_docs_url = "http://localhost:8081"

    # Build target URL
    if path:
        target_url = f"{dbt_docs_url}/{path}"
    else:
        target_url = dbt_docs_url

    if request.url.query:
        target_url += f"?{request.url.query}"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            # Proxy the request
            proxy_response = await client.get(
                target_url,
                headers={
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in ["host", "connection"]
                },
            )

            # Build response headers
            response_headers = {}
            for key, value in proxy_response.headers.items():
                # Skip headers that cause issues
                if key.lower() not in ["content-encoding", "transfer-encoding", "content-length"]:
                    response_headers[key] = value

            # Return proxy response without modification (no nav bar injection)
            content = proxy_response.content
            return Response(
                content=content, status_code=proxy_response.status_code, headers=response_headers
            )

    except Exception as e:
        logger.error(f"dbt docs proxy error: {e}")
        return Response(content="Proxy error", status_code=502)
