"""dango/web/routes/initial_sync.py

Initial data sync state machine, API endpoints, and background task.

Runs all configured sources sequentially after first deployment, broadcasting
progress via WebSocket for the dashboard banner.  State is persisted to
``.dango/state/initial_sync.json`` so it survives server restarts.

Authentication: The ``/start`` endpoint accepts a one-time deploy token
(generated during ``dango deploy``) via ``Authorization: Bearer <token>``.
Other endpoints require a valid session or API key (handled by auth middleware).
"""

from __future__ import annotations

import hmac
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from dango.logging import get_logger
from dango.web.helpers import get_project_root
from dango.web.routes.websocket import ws_manager

logger = get_logger(__name__)

router = APIRouter(prefix="/api/initial-sync", tags=["initial-sync"])

# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


class SyncPhase(str, Enum):
    """Phases of the initial sync workflow."""

    IDLE = "idle"
    SYNCING = "syncing"
    DBT_DOCS = "dbt_docs"
    METABASE = "metabase"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class InitialSyncState:
    """Mutable state for the initial sync workflow."""

    phase: SyncPhase = SyncPhase.IDLE
    total_sources: int = 0
    current_source_index: int = 0
    current_source_name: str = ""
    completed_sources: list[str] = field(default_factory=list)
    failed_sources: list[dict[str, str]] = field(default_factory=list)
    skipped_sources: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    skip_current_requested: bool = False
    started_at: str = ""
    completed_at: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict."""
        d = asdict(self)
        d["phase"] = self.phase.value
        return d


# Module-level singleton
_sync_state = InitialSyncState()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    """Return path to the state JSON file."""
    return get_project_root() / ".dango" / "state" / "initial_sync.json"


def _save_state() -> None:
    """Persist current state to disk."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_sync_state.to_dict(), indent=2))


def _load_state() -> None:
    """Load state from disk if it exists. Mark interrupted syncs as failed."""
    global _sync_state  # noqa: PLW0603
    path = _state_path()
    if not path.exists():
        return

    try:
        data: dict[str, Any] = json.loads(path.read_text())
        _sync_state = InitialSyncState(
            phase=SyncPhase(data.get("phase", "idle")),
            total_sources=data.get("total_sources", 0),
            current_source_index=data.get("current_source_index", 0),
            current_source_name=data.get("current_source_name", ""),
            completed_sources=data.get("completed_sources", []),
            failed_sources=data.get("failed_sources", []),
            skipped_sources=data.get("skipped_sources", []),
            cancel_requested=data.get("cancel_requested", False),
            skip_current_requested=data.get("skip_current_requested", False),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            error=data.get("error"),
        )
        # If server restarted mid-sync, mark as failed
        if _sync_state.phase in (SyncPhase.SYNCING, SyncPhase.DBT_DOCS, SyncPhase.METABASE):
            _sync_state.phase = SyncPhase.FAILED
            _sync_state.error = "Sync interrupted by server restart."
            _save_state()
    except Exception:
        logger.warning("initial_sync_state_load_failed", exc_info=True)


async def _save_and_broadcast() -> None:
    """Persist state and broadcast update to WebSocket clients."""
    _save_state()
    await ws_manager.broadcast(
        {
            "event": "initial_sync_progress",
            "data": _sync_state.to_dict(),
            "timestamp": datetime.now().isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Deploy token validation
# ---------------------------------------------------------------------------


def _validate_deploy_token(token: str) -> bool:
    """Check and consume the one-time deploy token. Returns True if valid."""
    token_path = get_project_root() / ".dango" / "state" / "deploy_token"
    if not token_path.exists():
        return False

    stored = token_path.read_text().strip()
    if not hmac.compare_digest(token, stored):
        return False

    # One-time use — delete after validation
    try:
        token_path.unlink()
    except OSError:
        pass
    return True


def _get_admin_user_from_session(request: Request) -> Any:
    """Validate session cookie and return user if admin, else None.

    This endpoint is in ``_PUBLIC_EXACT`` so the auth middleware skips it.
    We validate the session explicitly to allow admins to re-trigger sync
    after the one-time deploy token has been consumed.
    """
    from dango.auth.admin import get_auth_db_path, is_auth_enabled
    from dango.auth.models import Role
    from dango.auth.sessions import validate_session
    from dango.web.middleware.auth import COOKIE_NAME

    project_root = get_project_root()
    if not is_auth_enabled(project_root):
        return None

    db_path = get_auth_db_path(project_root)
    if not db_path.exists():
        return None

    # Extract session cookie from request
    cookie_value = request.cookies.get(COOKIE_NAME)
    if not cookie_value:
        return None

    user = validate_session(db_path, cookie_value)
    if user is None:
        return None

    if user.role != Role.ADMIN:
        return None

    return user


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_initial_sync(project_root: Path, sources: list[dict[str, str]]) -> None:
    """Run the initial sync for all sources sequentially."""
    global _sync_state  # noqa: PLW0603

    _sync_state.phase = SyncPhase.SYNCING
    _sync_state.total_sources = len(sources)
    _sync_state.started_at = datetime.now().isoformat()
    await _save_and_broadcast()

    for i, source in enumerate(sources):
        # Check cancel
        if _sync_state.cancel_requested:
            _sync_state.phase = SyncPhase.CANCELLED
            _sync_state.completed_at = datetime.now().isoformat()
            await _save_and_broadcast()
            return

        # Disk check
        disk_warning = _check_disk_usage()
        if disk_warning == "abort":
            _sync_state.phase = SyncPhase.FAILED
            disk_pct = _get_disk_usage_pct()
            _sync_state.error = f"Disk at {disk_pct:.0f}%. Free space or resize the server."
            _sync_state.completed_at = datetime.now().isoformat()
            await _save_and_broadcast()
            return

        source_name = source["name"]
        _sync_state.current_source_index = i + 1
        _sync_state.current_source_name = source_name
        _sync_state.skip_current_requested = False
        await _save_and_broadcast()

        try:
            await _sync_single_source(project_root, source_name)

            if _sync_state.skip_current_requested:
                _sync_state.skipped_sources.append(source_name)
            else:
                _sync_state.completed_sources.append(source_name)
        except Exception as exc:
            logger.error(
                "initial_sync_source_failed",
                source=source_name,
                error=str(exc),
                exc_info=True,
            )
            _sync_state.failed_sources.append({"name": source_name, "error": str(exc)})

        await _save_and_broadcast()

    # Post-sync phases
    if _sync_state.completed_sources and not _sync_state.cancel_requested:
        # dbt docs
        _sync_state.phase = SyncPhase.DBT_DOCS
        await _save_and_broadcast()
        _generate_dbt_docs(project_root)

        # Metabase refresh
        _sync_state.phase = SyncPhase.METABASE
        await _save_and_broadcast()
        _refresh_metabase(project_root)

    _sync_state.phase = SyncPhase.COMPLETE
    _sync_state.completed_at = datetime.now().isoformat()
    await _save_and_broadcast()


async def _sync_single_source(project_root: Path, source_name: str) -> None:
    """Sync a single source using the existing run_sync pipeline."""
    import os

    from dango.config.helpers import load_config
    from dango.ingestion import run_sync
    from dango.utils import DbtLock

    # Stop Metabase on cloud to release DuckDB read lock (same as sync_trigger)
    cloud_mode = os.environ.get("DANGO_CLOUD_MODE") == "true"
    if cloud_mode:
        try:
            import subprocess

            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(project_root / "docker-compose.yml"),
                    "stop",
                    "metabase",
                ],
                capture_output=True,
                timeout=60,
            )
        except Exception:
            logger.debug("metabase_stop_before_initial_sync_failed", exc_info=True)

    lock = DbtLock(
        project_root=project_root,
        source="initial-sync",
        operation=f"sync {source_name}",
    )
    lock.acquire()
    try:
        config = load_config(project_root)
        source_config = config.sources.get_source(source_name)
        if source_config is None:
            raise ValueError(f"Source '{source_name}' not found in config")
        run_sync(project_root=project_root, sources=[source_config])
    finally:
        lock.release()
        if cloud_mode:
            try:
                import subprocess

                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(project_root / "docker-compose.yml"),
                        "start",
                        "metabase",
                    ],
                    capture_output=True,
                    timeout=120,
                )
            except Exception:
                logger.debug("metabase_start_after_initial_sync_failed", exc_info=True)


def _generate_dbt_docs(project_root: Path) -> None:
    """Generate dbt documentation after sync."""
    try:
        from dango.transformation import generate_dbt_docs

        generate_dbt_docs(project_root)
    except Exception:
        logger.warning("initial_sync_dbt_docs_failed", exc_info=True)


def _refresh_metabase(project_root: Path) -> None:
    """Trigger Metabase schema refresh."""
    try:
        from dango.visualization.metabase import sync_metabase_schema

        sync_metabase_schema(project_root)
    except Exception:
        logger.warning("initial_sync_metabase_refresh_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Disk usage helpers
# ---------------------------------------------------------------------------


def _get_disk_usage_pct() -> float:
    """Return disk usage percentage for /srv/dango (or / if not present)."""
    try:
        path = "/srv/dango" if Path("/srv/dango").exists() else "/"
        usage = shutil.disk_usage(path)
        return (usage.used / usage.total) * 100
    except Exception:
        return 0.0


def _check_disk_usage() -> str | None:
    """Check disk usage. Returns 'abort' if >=90%, 'warn' if >=80%, else None."""
    pct = _get_disk_usage_pct()
    if pct >= 90:
        return "abort"
    if pct >= 80:
        return "warn"
    return None


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@router.post("/start")
async def start_initial_sync(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Start the initial data sync.

    Accepts either:
    - A one-time deploy token (``Authorization: Bearer <token>``) — used by
      ``dango deploy`` immediately after provisioning.
    - An admin session cookie — used for re-triggering from the dashboard
      after the deploy token has been consumed.
    """
    global _sync_state  # noqa: PLW0603

    # Auth: check deploy token first, then fall back to session user
    auth_header = request.headers.get("authorization", "")

    token_valid = False
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        token_valid = _validate_deploy_token(token)

    if not token_valid:
        # Fall back to session-based auth (this endpoint is in _PUBLIC_EXACT
        # so the middleware sets user=None; validate the session explicitly)
        user = _get_admin_user_from_session(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Unauthorized")

    if _sync_state.phase == SyncPhase.SYNCING:
        raise HTTPException(status_code=409, detail="Sync already in progress")

    # Load sources
    project_root = get_project_root()
    try:
        from dango.config.helpers import load_config

        config = load_config(project_root)
        sources = [{"name": src.name, "type": src.type.value} for src in config.sources.sources]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load sources: {exc}") from exc

    if not sources:
        return {"status": "skipped", "message": "No sources configured"}

    # Reset state and start
    _sync_state = InitialSyncState()

    background_tasks.add_task(_run_initial_sync, project_root, sources)
    return {"status": "started", "message": f"Syncing {len(sources)} source(s)"}


@router.get("/status")
async def get_sync_status(request: Request) -> dict[str, Any]:
    """Return current initial sync state."""
    # Lazy load state from disk on first call
    if _sync_state.phase == SyncPhase.IDLE and _sync_state.total_sources == 0:
        _load_state()
    return _sync_state.to_dict()


@router.post("/skip-source")
async def skip_current_source(request: Request) -> dict[str, str]:
    """Skip the currently syncing source."""
    user = getattr(request.state, "user", None) if hasattr(request, "state") else None
    if user is not None and getattr(user, "role", None) is not None:
        from dango.auth.models import Role

        if user.role != Role.ADMIN:
            raise HTTPException(status_code=403, detail="Admin access required")

    if _sync_state.phase != SyncPhase.SYNCING:
        raise HTTPException(status_code=409, detail="No sync in progress")

    _sync_state.skip_current_requested = True
    return {"status": "ok", "message": f"Skipping {_sync_state.current_source_name}"}


@router.post("/cancel")
async def cancel_sync(request: Request) -> dict[str, str]:
    """Cancel the entire initial sync."""
    user = getattr(request.state, "user", None) if hasattr(request, "state") else None
    if user is not None and getattr(user, "role", None) is not None:
        from dango.auth.models import Role

        if user.role != Role.ADMIN:
            raise HTTPException(status_code=403, detail="Admin access required")

    if _sync_state.phase not in (SyncPhase.SYNCING, SyncPhase.DBT_DOCS, SyncPhase.METABASE):
        raise HTTPException(status_code=409, detail="No sync in progress")

    _sync_state.cancel_requested = True
    return {"status": "ok", "message": "Cancellation requested"}
