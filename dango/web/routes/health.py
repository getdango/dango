"""dango/web/routes/health.py

Health check and platform status endpoints.
"""

import asyncio
import logging
from datetime import datetime

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


@router.get("/api/status", response_model=ServiceHealth)
async def get_status():
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
async def get_watcher_status_api():
    """Get file watcher status.

    Returns information about the file watcher including:
    - Whether it's running
    - PID if running
    - Configuration (auto-sync, auto-dbt, debounce, patterns, directories)
    - Log file location
    """
    from dango.cli.utils import get_watcher_status
    from dango.config import ConfigLoader

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
async def get_platform_health():
    """Get comprehensive platform health status.

    Returns:
        Platform health including DB size, disk space, recent failures, and overall status
    """
    # Gather health data asynchronously
    data = await get_platform_health_data()

    db_health = data["db_health"]
    disk = data["disk"]
    sources_config = data["sources_config"]
    failed_syncs = data["failed_syncs"]
    failed_dbt = data["failed_dbt"]

    # Determine overall status
    critical_issues = []
    warnings = []

    if disk["status"] == "critical":
        critical_issues.append("Critical disk space")
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

    if critical_issues:
        overall_status = "critical"
    elif warnings:
        overall_status = "warning"
    else:
        overall_status = "healthy"

    return {
        "status": overall_status,
        "timestamp": datetime.now().isoformat(),
        "database": db_health,
        "disk": disk,
        "sync_failures": failed_syncs,
        "dbt_failures": failed_dbt,
        "total_sources": len(sources_config),
        "enabled_sources": len([s for s in sources_config if s.get("enabled", True)]),
        "critical_issues": critical_issues,
        "warnings": warnings,
    }
