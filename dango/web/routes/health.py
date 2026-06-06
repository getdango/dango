"""dango/web/routes/health.py

Health check and platform status endpoints.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from dango.oauth.storage import OAuthStorage
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
    duckdb_capacity = data.get("duckdb_capacity", {})
    sources_config = data["sources_config"]
    failed_syncs = data["failed_syncs"]
    failed_dbt = data["failed_dbt"]

    # Determine overall status
    critical_issues: list[str] = []
    warnings: list[str] = []

    project_root = Path(get_project_root())
    from dango.config.helpers import is_running_on_cloud

    is_cloud = is_running_on_cloud()

    if disk["status"] == "critical":
        critical_issues.append("Critical disk space")
    elif is_cloud and disk.get("used_pct", 0) > 80:
        warnings.append("Disk usage above 80% \u2014 consider resizing or running cleanup")
    elif not is_cloud and disk.get("free_gb", 999) < 10:
        warnings.append(
            f"Low disk space ({disk.get('free_gb', 0):.0f} GB free) \u2014 run `dango cleanup` to free space"
        )
    elif disk["status"] == "warning":
        warnings.append("Low disk space")

    if db_health["status"] == "critical":
        warnings.append("Very large database")
    elif db_health["status"] == "large":
        warnings.append("Large database")

    if duckdb_capacity.get("duckdb_capacity_warning"):
        warnings.append("DuckDB using >75% of recommended capacity — consider archiving data")

    if failed_syncs:
        warnings.append(f"{len(failed_syncs)} source(s) with recent failures")

    # Check for sources that have never synced
    enabled_sources = [s for s in sources_config if s.get("enabled", True)]
    if enabled_sources and not failed_syncs:
        from dango.web.helpers import load_sync_history as _load_sync_history

        never_synced = [
            s["name"] for s in enabled_sources if not _load_sync_history(s["name"], limit=1)
        ]
        if never_synced:
            warnings.append(f"{len(never_synced)} source(s) never synced")

    # Check for orphaned raw schemas (no matching source config)
    try:
        db_path = project_root / "data" / "warehouse.duckdb"
        if db_path.exists():
            configured_sources = {s.get("name") for s in sources_config if s.get("name")}

            def _check_orphaned_schemas() -> list[str]:
                import duckdb as _duckdb

                conn = _duckdb.connect(str(db_path), config={"access_mode": "read_only"})
                try:
                    schemas = conn.execute(
                        "SELECT DISTINCT schema_name FROM information_schema.schemata "
                        "WHERE schema_name LIKE 'raw_%' "
                        "AND schema_name NOT LIKE '%_staging'"
                    ).fetchall()
                finally:
                    conn.close()
                return [s[0][4:] for s in schemas if s[0][4:] not in configured_sources]

            orphaned = await asyncio.to_thread(_check_orphaned_schemas)
            if orphaned:
                warnings.append(
                    f"Orphaned tables found ({', '.join(orphaned[:3])}) \u2014 run `dango db clean`"
                )
    except Exception:
        pass  # Non-critical check

    if failed_dbt:
        warnings.append("dbt run failures")

    result: dict[str, Any] = {
        "status": "healthy",  # set below
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "database": db_health,
        "disk": disk,
        "disk_breakdown": data.get("disk_breakdown", {}),
        "duckdb_capacity": duckdb_capacity,
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

    # Deployment info (cloud only — journal written on the cloud server)
    if is_cloud:
        deploy_info = _get_local_deployment_info(project_root)
        if deploy_info:
            result["deployment"] = deploy_info

    # OAuth token health
    oauth_health: list[dict[str, Any]] = []
    try:
        oauth_storage = OAuthStorage(project_root)
        for cred in oauth_storage.list():
            token_info: dict[str, Any] = {
                "source_type": cred.source_type,
                "provider": cred.provider,
                "is_expired": cred.is_expired(),
                "days_until_expiry": cred.days_until_expiry(),
            }
            oauth_health.append(token_info)
            if cred.is_expired():
                critical_issues.append(
                    f"OAuth token expired for {cred.source_type}"
                    " \u2014 reconnect at /settings/secrets"
                )
            elif cred.is_expiring_soon(days=7):
                days = cred.days_until_expiry()
                warnings.append(
                    f"OAuth token for {cred.source_type} expires in {days} day(s)"
                    " \u2014 reconnect at /settings/secrets"
                )
    except Exception:  # noqa: BLE001
        logger.warning("Failed to check OAuth token health", exc_info=True)
    result["oauth_health"] = oauth_health

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

    Returns None if not running on a cloud server (i.e. DANGO_CLOUD_MODE != true).
    """
    from dango.config.helpers import is_running_on_cloud

    if not is_running_on_cloud():
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


def _get_local_deployment_info(project_root: Path) -> dict[str, Any] | None:
    """Read latest deployment from local journal (runs ON the cloud server)."""
    from dango.platform.cloud.deploy_journal import read_local_journal

    entries = read_local_journal(project_root, limit=1)
    if not entries:
        return None
    entry = entries[0]
    return {
        "git_commit": entry.get("git_commit"),
        "git_branch": entry.get("git_branch"),
        "deployed_by": entry.get("deployer"),
        "deployed_at": entry.get("timestamp"),
        "success": entry.get("success"),
    }


_DEPLOYMENT_ALLOWED_FIELDS = {
    "timestamp",
    "deployer",
    "success",
    "git_commit",
    "git_branch",
    "git_clean",
    "dango_version",
    "files_synced",
    "models_changed",
    "models_added",
    "models_removed",
    "duration_seconds",
    "dry_run",
    "error",
}


@router.get("/api/deployments/history")
async def get_deployment_history(
    request: Request,
    limit: int = 20,
) -> JSONResponse:
    """Return deployment history from local journal (admin-only)."""
    from dango.auth.audit import AuditEvent, log_auth_event
    from dango.auth.permissions import require_permission
    from dango.platform.cloud.deploy_journal import read_local_journal

    perm_dep = require_permission("platform.manage")
    user = await perm_dep(request)

    log_auth_event(
        AuditEvent.DEPLOYMENT_HISTORY_VIEWED,
        user_id=user.id,
        email=user.email,
    )

    project_root = Path(get_project_root())
    limit = max(1, min(limit, 100))
    entries = read_local_journal(project_root, limit=limit)

    filtered = [{k: v for k, v in e.items() if k in _DEPLOYMENT_ALLOWED_FIELDS} for e in entries]

    return JSONResponse(content={"deployments": filtered})
