"""dango/web/helpers.py

Shared helper functions for web routes. Consolidates DuckDB queries, config loading,
service health checks, and log management.
"""

import asyncio
import json
import logging
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import yaml

from dango.web.models import SourceStatus, TableInfo

logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    """Get project root from app state."""
    from dango.web.app import app

    return app.state.project_root


def is_cloud_deployment(project_root: Path) -> bool:
    """Check if this is a cloud deployment.

    Returns ``True`` when the ``DANGO_CLOUD_MODE`` environment variable is
    ``"true"`` (set in the systemd unit on the server) **or** when
    ``.dango/cloud.yml`` exists locally.
    """
    import os

    if os.environ.get("DANGO_CLOUD_MODE") == "true":
        return True
    return (project_root / ".dango" / "cloud.yml").exists()


def load_sources_config() -> list[dict[str, Any]]:
    """Load sources configuration from .dango/sources.yml."""
    sources_file = get_project_root() / ".dango" / "sources.yml"

    if not sources_file.exists():
        return []

    try:
        with open(sources_file, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
            return config.get("sources", [])
    except Exception as e:
        logger.error(f"Error loading sources config: {e}")
        return []


def get_duckdb_path() -> Path:
    """Get path to DuckDB database."""
    return get_project_root() / "data" / "warehouse.duckdb"


def get_dbt_manifest() -> dict[str, Any] | None:
    """Load dbt manifest.json."""
    manifest_path = get_project_root() / "dbt" / "target" / "manifest.json"

    if not manifest_path.exists():
        return None

    try:
        with open(manifest_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading dbt manifest: {e}")
        return None


def _get_all_model_row_counts() -> dict[str, int]:
    """Get row counts for ALL models in a single DuckDB query.

    Returns a dict mapping ``schema.table_name`` to row count.
    Much faster than per-model queries (1 connection vs N).
    """
    db_path = get_duckdb_path()
    if not db_path.exists():
        return {}

    try:
        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        try:
            rows = conn.execute(
                "SELECT schema_name, table_name, estimated_size "
                "FROM duckdb_tables() "
                "WHERE schema_name IN ('staging', 'intermediate', 'marts')"
            ).fetchall()
            return {f"{r[0]}.{r[1]}": r[2] for r in rows}
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting model row counts: {e}")
        return {}


def get_dbt_model_last_run() -> str | None:
    """Get last dbt run timestamp from run_results.json."""
    project_root = get_project_root()
    run_results_path = project_root / "dbt" / "target" / "run_results.json"

    if not run_results_path.exists():
        return None

    try:
        with open(run_results_path, encoding="utf-8") as f:
            run_results = json.load(f)

        # Get the generated_at time (when dbt command completed)
        metadata = run_results.get("metadata", {})
        generated_at = metadata.get("generated_at")

        if generated_at:
            return generated_at

        return None

    except Exception as e:
        logger.error(f"Error reading run_results.json: {e}")
        return None


def get_dbt_model_statuses() -> dict[str, dict[str, Any]]:
    """Get status and timing for each dbt model from persistent status file.

    Returns:
        Dictionary mapping unique_id to {"status": str, "last_run": Optional[str]}
    """
    from dango.utils.dbt_status import get_model_statuses

    project_root = get_project_root()
    return get_model_statuses(project_root)


def get_dbt_models() -> list[dict[str, Any]]:
    """Get list of dbt models from manifest."""
    manifest = get_dbt_manifest()

    if not manifest:
        return []

    # Get statuses and per-model timing from run_results.json
    model_statuses = get_dbt_model_statuses()

    # Batch fetch all row counts in one query (instead of N individual queries)
    row_counts = _get_all_model_row_counts()

    models = []
    nodes = manifest.get("nodes", {})

    for node_id, node in nodes.items():
        # Only include models (not tests, seeds, etc.)
        if node.get("resource_type") == "model":
            schema = node.get("schema")
            model_name = node.get("name")

            # Look up row count from batch result
            row_count = row_counts.get(f"{schema}.{model_name}")

            # Get status and per-model last_run from run_results
            # If not in run_results, default to None (never run)
            model_info = model_statuses.get(node_id, {})
            status = model_info.get("status")
            last_run = model_info.get("last_run")

            models.append(
                {
                    "name": model_name,
                    "unique_id": node_id,
                    "path": node.get("path"),
                    "materialization": node.get("config", {}).get("materialized", "view"),
                    "schema": schema,
                    "database": node.get("database"),
                    "depends_on": node.get("depends_on", {}).get("nodes", []),
                    "description": node.get("description", ""),
                    "tags": node.get("tags", []),
                    "row_count": row_count,
                    "last_run": last_run,  # Per-model timing, not global
                    "status": status,  # success/error/skipped/None
                }
            )

    # Sort models by schema first, then by name within each schema for consistent ordering
    models.sort(key=lambda m: (m.get("schema", "").lower(), m.get("name", "").lower()))

    return models


def get_source_row_count(source_name: str) -> int | None:
    """Get row count for a source from DuckDB (with timeout to prevent blocking)."""
    db_path = get_duckdb_path()

    if not db_path.exists():
        return None

    @contextmanager
    def timeout_context(seconds):
        """Context manager for timeout."""

        def timeout_handler(signum, frame):
            """Raise TimeoutError when SIGALRM fires."""
            raise TimeoutError(f"Query timed out after {seconds} seconds")

        # Set the signal handler and alarm (Unix only)
        if hasattr(signal, "SIGALRM"):
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(seconds)
            try:
                yield
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        else:
            # Windows doesn't support SIGALRM - just skip timeout
            yield

    try:
        with timeout_context(2):  # 2 second timeout
            conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
            try:
                # Check for multi-resource schema first (raw_{source_name})
                multi_schema = f"raw_{source_name}"
                result = conn.execute(f"""
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = '{multi_schema}'
                      AND table_name NOT LIKE '_dlt_%'
                      AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
                """).fetchone()

                if result and result[0] > 0:
                    # Multi-resource source: sum rows across all tables in schema
                    tables = conn.execute(f"""
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = '{multi_schema}'
                          AND table_name NOT LIKE '_dlt_%'
                          AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
                    """).fetchall()

                    total_rows = 0
                    for (table_name,) in tables:
                        count_result = conn.execute(
                            f'SELECT COUNT(*) FROM "{multi_schema}"."{table_name}"'
                        ).fetchone()
                        if count_result:
                            total_rows += count_result[0]

                    return total_rows

                # Single-resource source: check raw.{source_name} table
                result = conn.execute(f"""
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = 'raw' AND table_name = '{source_name}'
                """).fetchone()

                if result and result[0] > 0:
                    count_result = conn.execute(
                        f'SELECT COUNT(*) FROM "raw"."{source_name}"'
                    ).fetchone()
                    return count_result[0] if count_result else 0

                # Fall back to staging table
                result = conn.execute(f"""
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = 'staging' AND table_name = 'stg_{source_name}'
                """).fetchone()

                if result and result[0] > 0:
                    count_result = conn.execute(
                        f'SELECT COUNT(*) FROM "staging"."stg_{source_name}"'
                    ).fetchone()
                    return count_result[0] if count_result else 0

                return None
            finally:
                conn.close()

    except TimeoutError:
        logger.warning(
            f"Row count query timed out for {source_name} (database likely busy with sync)"
        )
        return None
    except Exception as e:
        logger.error(f"Error getting row count for {source_name}: {e}")
        return None


def get_source_tables_info(source_name: str) -> dict[str, Any] | None:
    """Get detailed table information for a source, including per-table breakdown.

    All sources use raw_{source_name} schema pattern (industry best practice).

    Returns:
        Dictionary with:
        - total_rows: Total row count across all tables
        - tables: List of {name, row_count, schema} for each table
        - has_multiple_tables: Whether source has multiple tables

    Returns None if source not found or database unavailable.
    """
    db_path = get_duckdb_path()

    if not db_path.exists():
        return None

    try:
        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        try:
            # All sources use raw_{source_name} schema
            schema_name = f"raw_{source_name}"
            result = conn.execute(f"""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = '{schema_name}'
                  AND table_name NOT LIKE '_dlt_%'
                  AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
            """).fetchone()

            if result and result[0] > 0:
                # Get per-table breakdown
                tables_result = conn.execute(f"""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = '{schema_name}'
                      AND table_name NOT LIKE '_dlt_%'
                      AND table_name NOT IN ('spreadsheet', 'spreadsheet_info')
                    ORDER BY table_name
                """).fetchall()

                tables = []
                total_rows = 0
                for (table_name,) in tables_result:
                    count_result = conn.execute(
                        f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"'
                    ).fetchone()
                    if count_result:
                        row_count = count_result[0]
                        total_rows += row_count
                        tables.append(
                            {"name": table_name, "row_count": row_count, "schema": schema_name}
                        )

                return {
                    "total_rows": total_rows,
                    "tables": tables,
                    "has_multiple_tables": len(tables) > 1,
                }

            # Fall back to staging table (for backward compatibility)
            result = conn.execute(f"""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = 'staging' AND table_name = 'stg_{source_name}'
            """).fetchone()

            if result and result[0] > 0:
                count_result = conn.execute(
                    f'SELECT COUNT(*) FROM "staging"."stg_{source_name}"'
                ).fetchone()
                row_count = count_result[0] if count_result else 0
                return {
                    "total_rows": row_count,
                    "tables": [
                        {
                            "name": f"stg_{source_name}",
                            "row_count": row_count,
                            "schema": "staging",
                        }
                    ],
                    "has_multiple_tables": False,
                }

            return None
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Error getting table info for {source_name}: {e}")
        return None


def get_last_sync_time(source_name: str) -> str | None:
    """Get last sync time from sync history."""
    history = load_sync_history(source_name, limit=1)
    if history and len(history) > 0:
        return history[0].get("timestamp")
    return None


def get_last_sync_status(source_name: str) -> str | None:
    """Get last sync status from sync history (success/failed)."""
    history = load_sync_history(source_name, limit=1)
    if history and len(history) > 0:
        return history[0].get("status")  # 'success' or 'failed'
    return None


def mask_sensitive_config(config: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive fields in configuration."""
    masked_config = config.copy()

    # List of sensitive field names to mask
    sensitive_fields = {
        "password",
        "api_key",
        "secret",
        "token",
        "credentials",
        "access_token",
        "refresh_token",
        "private_key",
        "client_secret",
    }

    def mask_dict(d: dict[str, Any]) -> dict[str, Any]:
        """Recursively mask sensitive fields in dict."""
        result = {}
        for key, value in d.items():
            key_lower = key.lower()
            if any(sensitive in key_lower for sensitive in sensitive_fields):
                # Mask the value
                if isinstance(value, str) and len(value) > 4:
                    result[key] = value[:2] + "*" * (len(value) - 4) + value[-2:]
                else:
                    result[key] = "****"
            elif isinstance(value, dict):
                result[key] = mask_dict(value)
            elif isinstance(value, list):
                result[key] = [
                    mask_dict(item) if isinstance(item, dict) else item for item in value
                ]
            else:
                result[key] = value
        return result

    return mask_dict(masked_config)


def load_sync_history(source_name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Load sync history for a source."""
    from dango.utils.sync_history import load_sync_history as load_history

    return load_history(get_project_root(), source_name, limit)


def save_sync_history_entry(source_name: str, entry: dict[str, Any]):
    """Save a sync history entry for a source."""
    from dango.utils.sync_history import save_sync_history_entry as save_entry

    save_entry(get_project_root(), source_name, entry)


def get_source_freshness(source_name: str) -> dict[str, Any]:
    """Calculate data freshness for a source.

    Returns freshness status based on time since last successful sync:
    - synced: successful sync
    - never_synced: no sync history
    - failed: last sync failed

    Args:
        source_name: Name of the source

    Returns:
        Dictionary with freshness information:
        {
            "status": "synced" | "never_synced" | "failed",
            "hours_since_sync": float | None,
            "last_sync_time": str | None,
            "last_sync_status": str | None
        }
    """
    history = load_sync_history(source_name, limit=1)

    if not history:
        return {
            "status": "never_synced",
            "hours_since_sync": None,
            "last_sync_time": None,
            "last_sync_status": None,
        }

    last_sync = history[0]
    last_sync_status = last_sync.get("status")
    last_sync_time = last_sync.get("timestamp")

    # If last sync failed, mark as failed regardless of time
    # "partial" still loaded some data — treat as having fresh data
    if last_sync_status not in ("success", "partial"):
        return {
            "status": "failed",
            "hours_since_sync": None,
            "last_sync_time": last_sync_time,
            "last_sync_status": last_sync_status,
        }

    # Calculate time since last successful sync
    try:
        # Timestamps are stored as naive UTC. Treat them as UTC for comparison.
        ts_str = last_sync_time.replace("Z", "+00:00")
        if "+" not in ts_str and ts_str.count("-") <= 2:
            # Naive timestamp — assume UTC
            ts_str += "+00:00"
        timestamp = datetime.fromisoformat(ts_str)
        now_utc = datetime.now(tz=timestamp.tzinfo)
        hours_ago = (now_utc - timestamp).total_seconds() / 3600

        # All successful syncs show as "synced"
        return {
            "status": "synced",
            "hours_since_sync": round(hours_ago, 1),
            "last_sync_time": last_sync_time,
            "last_sync_status": last_sync_status,
        }

    except Exception as e:
        logger.error(f"Error calculating freshness for {source_name}: {e}")
        return {
            "status": "unknown",
            "hours_since_sync": None,
            "last_sync_time": last_sync_time,
            "last_sync_status": last_sync_status,
        }


def get_logs_file() -> Path:
    """Get path to persistent logs file."""
    logs_dir = get_project_root() / ".dango" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "activity.jsonl"


def append_log_entry(log_entry: dict[str, Any]):
    """Append a log entry to the persistent logs file."""
    from dango.utils.activity_log import log_activity

    try:
        log_activity(
            project_root=get_project_root(),
            level=log_entry.get("level", "info"),
            source=log_entry.get("source", "system"),
            message=log_entry.get("message", ""),
            timestamp=log_entry.get("timestamp"),
        )
    except Exception as e:
        logger.error(f"Error appending log entry: {e}")


def load_all_logs(limit: int = 1000) -> list[dict[str, Any]]:
    """Load all logs from the persistent file."""
    logs_file = get_logs_file()

    if not logs_file.exists():
        return []

    try:
        logs = []
        with open(logs_file, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # Return most recent logs first, limited to 'limit'
        return logs[-limit:][::-1]

    except Exception as e:
        logger.error(f"Error loading logs: {e}")
        return []


def check_service_via_http(service_name: str) -> str:
    """Check service via HTTP health endpoint (faster on Windows)."""
    import httpx

    # Map service names to their health check URLs
    health_urls = {
        "metabase": "http://localhost:3000/api/health",
        "dbt-docs": "http://localhost:8081",
    }

    url = health_urls.get(service_name)
    if not url:
        return "unknown"

    try:
        response = httpx.get(url, timeout=5.0, follow_redirects=False)
        if response.status_code in [200, 302]:  # 302 for dbt-docs redirect
            return "running"
        else:
            return "stopped"
    except Exception:
        return "not_found"


def check_service_via_docker(service_name: str) -> str:
    """Check service via Docker command (fast on Mac/Linux)."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={service_name}", "--format", "{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0 and result.stdout.strip():
            if "Up" in result.stdout:
                return "running"
            else:
                return "stopped"
        else:
            return "not_found"
    except Exception as e:
        logger.error(f"Error checking service {service_name}: {e}")
        return "unknown"


async def check_service_status_async(service_name: str) -> str:
    """Check if a service is running.

    Windows: Uses HTTP health checks (Docker Desktop too slow)
    Mac/Linux: Uses Docker commands (fast and reliable)
    """
    if sys.platform == "win32":
        # Windows: HTTP checks are much faster
        return await asyncio.to_thread(check_service_via_http, service_name)
    else:
        # Mac/Linux: Docker commands work well
        return await asyncio.to_thread(check_service_via_docker, service_name)


async def get_platform_health_data():
    """Gather platform health data (runs blocking operations in thread pool)."""
    from dango.utils.db_health import (
        check_duckdb_health,
        get_component_disk_usage,
        get_disk_usage_summary,
        get_duckdb_capacity,
    )

    project_root = get_project_root()
    duckdb_path = get_duckdb_path()

    # Run all blocking operations concurrently in thread pool
    db_health_task = asyncio.create_task(
        asyncio.to_thread(
            lambda: (
                check_duckdb_health(duckdb_path)
                if duckdb_path.exists()
                else {
                    "size_gb": 0,
                    "size_mb": 0,
                    "tables": 0,
                    "status": "new",
                    "raw_tables": 0,
                    "staging_tables": 0,
                    "marts_tables": 0,
                }
            )
        )
    )

    disk_task = asyncio.create_task(asyncio.to_thread(get_disk_usage_summary, project_root))
    breakdown_task = asyncio.create_task(asyncio.to_thread(get_component_disk_usage, project_root))
    capacity_task = asyncio.create_task(
        asyncio.to_thread(get_duckdb_capacity, duckdb_path, project_root)
    )
    sources_task = asyncio.create_task(asyncio.to_thread(load_sources_config))

    # Wait for all tasks
    try:
        db_health = await db_health_task
    except Exception as e:
        logger.error(f"Error checking DB health: {e}")
        db_health = {
            "size_gb": 0,
            "size_mb": 0,
            "tables": 0,
            "status": "error",
            "raw_tables": 0,
            "staging_tables": 0,
            "marts_tables": 0,
        }

    disk = await disk_task
    sources_config = await sources_task

    try:
        disk_breakdown = await breakdown_task
    except Exception as e:
        logger.error(f"Error getting disk breakdown: {e}")
        disk_breakdown: dict[str, Any] = {}

    try:
        duckdb_capacity = await capacity_task
    except Exception as e:
        logger.error(f"Error getting DuckDB capacity: {e}")
        duckdb_capacity: dict[str, Any] = {}

    # Check for failed syncs
    failed_syncs = []
    for source in sources_config:
        source_name = source.get("name", "unknown")
        history = await asyncio.to_thread(load_sync_history, source_name, 5)

        if history and len(history) > 0:
            most_recent = history[0]
            if most_recent.get("status") == "failed":
                try:
                    timestamp = datetime.fromisoformat(
                        most_recent.get("timestamp", "").replace("Z", "+00:00")
                    )
                    # Ensure both sides are tz-aware UTC for correct comparison
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    hours_ago = (datetime.now(tz=timezone.utc) - timestamp).total_seconds() / 3600

                    if hours_ago < 24:
                        failed_syncs.append(
                            {
                                "source": source_name,
                                "count": 1,
                                "last_error": most_recent.get("error_message", "Unknown error"),
                            }
                        )
                except Exception:
                    pass

    # Check for failed dbt runs
    failed_dbt = []
    run_results_path = project_root / "dbt" / "target" / "run_results.json"

    if run_results_path.exists():
        try:

            def read_dbt_results():
                """Read and parse the dbt run_results.json file."""
                with open(run_results_path, encoding="utf-8") as f:
                    return json.load(f)

            run_results = await asyncio.to_thread(read_dbt_results)

            results = run_results.get("results", [])
            failed_models = [r for r in results if r.get("status") == "error"]

            if failed_models:
                failed_dbt.append(
                    {
                        "run_time": run_results.get("metadata", {}).get("generated_at"),
                        "failed_models": len(failed_models),
                        "models": [r.get("unique_id", "unknown") for r in failed_models[:5]],
                    }
                )
        except Exception as e:
            logger.error(f"Error reading dbt run results: {e}")

    return {
        "db_health": db_health,
        "disk": disk,
        "disk_breakdown": disk_breakdown,
        "duckdb_capacity": duckdb_capacity,
        "sources_config": sources_config,
        "failed_syncs": failed_syncs,
        "failed_dbt": failed_dbt,
    }


async def get_source_status_data(source: dict) -> SourceStatus:
    """Get status data for a single source (runs blocking operations in thread pool)."""
    source_name = source.get("name", "unknown")
    source_type = source.get("type", "unknown")
    enabled = source.get("enabled", True)

    # Run blocking operations in thread pool
    tables_info = await asyncio.to_thread(get_source_tables_info, source_name)
    last_sync = await asyncio.to_thread(get_last_sync_time, source_name)
    last_sync_status = await asyncio.to_thread(get_last_sync_status, source_name)
    history = await asyncio.to_thread(load_sync_history, source_name, 5)
    freshness = await asyncio.to_thread(get_source_freshness, source_name)

    # Extract row count and tables list
    if tables_info:
        row_count = tables_info["total_rows"]
        # Show tables breakdown if source has multiple tables
        tables = (
            [TableInfo(**t) for t in tables_info["tables"]]
            if tables_info.get("has_multiple_tables")
            else None
        )
    else:
        row_count = None
        tables = None

    rows_processed = history[0].get("rows_processed", 0) if history else None
    last_sync_duration = history[0].get("duration_seconds") if history else None

    # Determine status (priority: failed > partial > synced > empty > not_synced)
    if last_sync_status == "failed":
        status = "failed"
    elif last_sync_status == "partial":
        status = "partial"
    elif not history:
        # Never synced - no history at all
        status = "not_synced"
    elif last_sync_status == "success" and (
        rows_processed == 0 or row_count == 0 or row_count is None
    ):
        # Synced but no data loaded
        status = "empty"
    elif row_count is not None and row_count > 0:
        # Has data
        status = "synced"
    else:
        # Edge case
        status = "not_synced"

    # Look up source capabilities from registry
    from dango.ingestion.sources.registry import get_source_capabilities

    capabilities = get_source_capabilities(source_type)
    supports_incremental = capabilities.get("incremental", True) if capabilities else True
    supports_date_range = capabilities.get("date_range", False) if capabilities else False

    # Derive sync mode from actual sync history, then fall back to registry.
    # Registry `incremental` flag is often wrong (e.g., chess says False but
    # actually uses incremental cursors).  Sync history records the real
    # `full_refresh` boolean set by _detect_write_disposition() at runtime.
    sync_mode_from_history: str | None = None
    if history:
        # Check if the most recent successful sync was incremental or full refresh
        for entry in history:
            if entry.get("status") == "success":
                if entry.get("full_refresh", False):
                    sync_mode_from_history = "full_refresh"
                else:
                    sync_mode_from_history = "incremental"
                break

    # CSV and local_files sources are always full refresh
    is_file_source = source_type in ("csv", "local_files")

    # Fetch registry metadata early — reused for write_disposition and lookback_days
    from dango.ingestion.sources.registry import get_source_metadata

    meta = get_source_metadata(source_type)

    if sync_mode_from_history is not None:
        sync_mode = sync_mode_from_history
    elif is_file_source:
        sync_mode = "full_refresh"
    else:
        # Check source config or registry default_config for write_disposition
        source_wd = source.get("write_disposition")
        if source_wd is None and meta:
            source_wd = (meta.get("default_config") or {}).get("write_disposition")
        if source_wd == "replace":
            sync_mode = "full_refresh"
        elif supports_incremental:
            sync_mode = "incremental"
        else:
            sync_mode = "full_refresh"
    write_disposition = "replace" if sync_mode == "full_refresh" else "merge"

    lookback_days = source.get("lookback_days")
    if lookback_days is None and meta:
        lookback_days = (meta.get("default_config") or {}).get("lookback_days")

    return SourceStatus(
        name=source_name,
        type=source_type,
        enabled=enabled,
        last_sync=last_sync,
        row_count=row_count,
        status=status,
        freshness=freshness,
        tables=tables,
        supports_incremental=supports_incremental,
        supports_date_range=supports_date_range,
        sync_mode=sync_mode,
        lookback_days=lookback_days,
        write_disposition=write_disposition,
        last_sync_duration_seconds=last_sync_duration,
    )
