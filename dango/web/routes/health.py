"""dango/web/routes/health.py

Health check and platform status endpoints.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from dango.web.helpers import (
    check_service_status_async,
    get_duckdb_path,
    get_platform_health_data,
    get_project_root,
)
from dango.web.models import ServiceHealth, WatcherStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_BACKUP_STALENESS_HOURS = 36


@router.get("/api/status", response_model=ServiceHealth)
async def get_status() -> ServiceHealth:
    """Get service health status.

    Returns health check for Dango API and related services (DuckDB, Metabase, dbt-docs)
    """
    get_project_root()
    duckdb_path = get_duckdb_path()

    # Check DuckDB
    duckdb_status = "healthy" if duckdb_path.exists() else "not_initialized"

    # Check Docker services asynchronously (run in parallel)
    metabase_status_task = asyncio.create_task(check_service_status_async("metabase"))
    dbt_docs_status_task = asyncio.create_task(check_service_status_async("dbt-docs"))

    metabase_status = await metabase_status_task
    dbt_docs_status = await dbt_docs_status_task

    return ServiceHealth(
        status="healthy",
        dango_version="0.1.0",
        services={
            "api": "running",
            "duckdb": duckdb_status,
            "metabase": metabase_status,
            "dbt_docs": dbt_docs_status,
        },
        uptime="N/A",  # TODO: Track actual uptime
    )


@router.get("/api/watcher/status", response_model=WatcherStatus)
async def get_watcher_status_api() -> WatcherStatus:
    """Get file watcher status.

    Returns information about the file watcher including:
    - Whether it's running
    - PID if running
    - Configuration (auto-sync, auto-dbt, debounce, patterns, directories)
    - Log file location
    """
    from dango.config import ConfigLoader
    from dango.platform.watcher_lifecycle import get_watcher_status

    project_root = get_project_root()

    # Get watcher process status
    watcher_status = get_watcher_status(project_root)

    # Load config for settings
    try:
        loader = ConfigLoader(project_root)
        config = loader.load_config()
        platform = config.platform

        return WatcherStatus(
            running=watcher_status["running"],
            pid=watcher_status.get("pid"),
            auto_sync_enabled=platform.auto_sync,
            auto_dbt_enabled=platform.auto_dbt,
            debounce_seconds=platform.debounce_seconds,
            watch_patterns=platform.watch_patterns,
            watch_directories=platform.watch_directories,
            log_file=str(watcher_status["log_file"]) if watcher_status["log_file"] else None,
        )
    except Exception as e:
        logger.error(f"Failed to load watcher config: {e}")
        # Return minimal status if config fails
        return WatcherStatus(
            running=watcher_status["running"],
            pid=watcher_status.get("pid"),
            auto_sync_enabled=False,
            auto_dbt_enabled=False,
            debounce_seconds=600,
            watch_patterns=["*.csv"],
            watch_directories=["data/uploads"],
            log_file=str(watcher_status["log_file"]) if watcher_status["log_file"] else None,
        )


@router.get("/api/health/platform")
async def get_platform_health() -> dict[str, Any]:
    """Get comprehensive platform health status.

    Returns:
        Platform health including DB size, disk space, recent failures, and overall status.
        On cloud deployments, also includes CPU/RAM/disk resource metrics and backup health.
    """
    # Gather health data asynchronously
    data = await get_platform_health_data()

    db_health = data["db_health"]
    disk = data["disk"]
    sources_config = data["sources_config"]
    failed_syncs = data["failed_syncs"]
    failed_dbt = data["failed_dbt"]

    # Determine overall status
    critical_issues: list[str] = []
    warnings: list[str] = []

    project_root = Path(get_project_root())
    is_cloud = (project_root / ".dango" / "cloud.yml").exists()

    if disk["status"] == "critical":
        critical_issues.append("Critical disk space")
    elif disk.get("used_pct", 0) > 80:
        # Prefer the actionable 80% message over generic "Low disk space"
        if is_cloud:
            warnings.append("Disk usage above 80% \u2014 consider resizing or running cleanup")
        else:
            warnings.append("Disk usage above 80% \u2014 run `dango cleanup` to free space")
    elif disk["status"] == "warning":
        warnings.append("Low disk space")

    if db_health["status"] == "critical":
        warnings.append("Very large database")
    elif db_health["status"] == "large":
        warnings.append("Large database")

    if failed_syncs:
        warnings.append(f"{len(failed_syncs)} source(s) with recent failures")

    if failed_dbt:
        warnings.append("dbt run failures")

    result: dict[str, Any] = {
        "status": "healthy",  # set below
        "timestamp": datetime.now().isoformat(),
        "database": db_health,
        "disk": disk,
        "disk_breakdown": data.get("disk_breakdown", {}),
        "sync_failures": failed_syncs,
        "dbt_failures": failed_dbt,
        "total_sources": len(sources_config),
        "enabled_sources": len([s for s in sources_config if s.get("enabled", True)]),
        "critical_issues": critical_issues,
        "warnings": warnings,
    }

    # Scheduler health
    scheduler_status = _get_scheduler_status()
    result["scheduler"] = scheduler_status
    if not scheduler_status.get("running"):
        warnings.append("Scheduler not running")

    # Cloud-specific: add resource metrics and backup health
    cloud_data = await _get_cloud_health_data()
    if cloud_data:
        result["resources"] = cloud_data["resources"]
        result["backup_health"] = cloud_data["backup_health"]
        if cloud_data["backup_health"]["status"] == "stale":
            warnings.append(f"Backup is stale (>{_BACKUP_STALENESS_HOURS}h)")
        elif cloud_data["backup_health"]["status"] == "none":
            warnings.append("No backups configured")

    if critical_issues:
        result["status"] = "critical"
    elif warnings:
        result["status"] = "warning"
    else:
        result["status"] = "healthy"

    return result


_SCHEDULER_FALLBACK: dict[str, Any] = {"running": False, "job_count": 0, "next_run_time": None}


def _get_scheduler_status() -> dict[str, Any]:
    """Return scheduler status from app state, with a safe fallback."""
    try:
        from dango.web.app import app

        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            status: dict[str, Any] = scheduler.get_status()
            return status
    except Exception:  # noqa: BLE001
        logger.debug("scheduler_status_check_failed", exc_info=True)
    return dict(_SCHEDULER_FALLBACK)


async def _get_cloud_health_data() -> dict[str, Any] | None:
    """Collect cloud-specific health data if running on a cloud server.

    Returns None if not a cloud deployment.
    """
    project_root = get_project_root()
    cloud_yml = Path(project_root) / ".dango" / "cloud.yml"

    if not cloud_yml.exists():
        return None

    # Check if it has a droplet_id (actual deployment vs empty config)
    try:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root)
        cloud_cfg = loader.load_cloud_config()
        if cloud_cfg is None or cloud_cfg.droplet_id is None:
            return None
    except Exception:  # noqa: BLE001
        return None

    # Collect local resource usage in a thread to avoid blocking the event loop
    from dango.platform.cloud.server_status import get_local_resource_usage

    resources = await asyncio.to_thread(get_local_resource_usage)

    # Check backup health
    backup_health = await asyncio.to_thread(_check_backup_health)

    return {
        "resources": resources,
        "backup_health": backup_health,
    }


def _check_backup_health() -> dict[str, Any]:
    """Check backup staleness by scanning the backup directory.

    Returns a dict with ``status`` ("healthy", "stale", "none") and
    ``last_backup`` timestamp string (or None).
    """
    import os

    backup_dir = Path("/srv/dango/backups")
    if not backup_dir.exists():
        return {"status": "none", "last_backup": None}

    # Find the most recent file across all backup subdirs
    latest_mtime: float = 0
    latest_name: str | None = None

    try:
        for dirpath, _dirnames, filenames in os.walk(backup_dir):
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    mtime = fpath.stat().st_mtime
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                        latest_name = fname
                except OSError:
                    continue
    except OSError:
        return {"status": "none", "last_backup": None}

    if latest_mtime == 0 or latest_name is None:
        return {"status": "none", "last_backup": None}

    last_backup_dt = datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
    age_hours = (datetime.now(tz=timezone.utc) - last_backup_dt).total_seconds() / 3600

    status = "healthy" if age_hours <= _BACKUP_STALENESS_HOURS else "stale"

    return {
        "status": status,
        "last_backup": last_backup_dt.isoformat(),
        "age_hours": round(age_hours, 1),
        "file": latest_name,
    }
