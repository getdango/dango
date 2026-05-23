"""dango/utils/db_health.py

DuckDB Health Monitoring and Disk Space Utilities.
"""

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import duckdb
from rich.console import Console

from dango.exceptions import DiskSpaceError, DuckDBHealthError, format_structured_error

logger = logging.getLogger(__name__)

console = Console()


def check_disk_space(project_root: Path, min_free_gb: int = 5) -> bool:
    """
    Check disk space before sync to prevent corruption

    Args:
        project_root: Path to project root directory
        min_free_gb: Minimum required free space in GB (default: 5GB)

    Returns:
        True if disk space is sufficient

    Raises:
        DiskSpaceError: If free space is below minimum
    """
    try:
        disk_usage = shutil.disk_usage(project_root)
        free_gb = disk_usage.free / (1024**3)

        # Critical: Less than minimum required
        if free_gb < min_free_gb:
            raise DiskSpaceError(
                f"Insufficient disk space: {free_gb:.1f}GB free (minimum {min_free_gb}GB required)"
            )

        # Warning: Less than 10GB (could become critical during sync)
        if free_gb < 10:
            console.print(f"[yellow]⚠️  Low disk space: {free_gb:.1f}GB free[/yellow]")
            console.print("[yellow]   Consider freeing up space before large syncs[/yellow]")

        return True

    except DiskSpaceError:
        raise
    except Exception as e:
        console.print(f"[yellow]⚠️  Could not check disk space: {e}[/yellow]")
        return True  # Don't block sync if check fails


def check_duckdb_health(duckdb_path: Path) -> dict[str, Any]:
    """
    Check DuckDB database health and size

    Args:
        duckdb_path: Path to DuckDB database file

    Returns:
        Dictionary with health information:
        {
            "size_gb": float,
            "size_mb": float,
            "tables": int,
            "status": "healthy" | "large" | "critical",
            "raw_tables": int,
            "staging_tables": int,
            "marts_tables": int
        }

    Raises:
        DuckDBHealthError: If database cannot be checked
    """
    import sys
    import time

    try:
        # Check if database file exists
        if not duckdb_path.exists():
            return {
                "size_gb": 0,
                "size_mb": 0,
                "tables": 0,
                "status": "new",
                "raw_tables": 0,
                "staging_tables": 0,
                "marts_tables": 0,
            }

        # Get file size
        size_bytes = duckdb_path.stat().st_size
        size_gb = size_bytes / (1024**3)
        size_mb = size_bytes / (1024**2)

        # Connect to database (read-only to avoid locks)
        # On Windows, retry if file is locked by Explorer or other processes
        max_retries = 3 if sys.platform == "win32" else 1
        last_error = None

        for attempt in range(max_retries):
            try:
                conn = duckdb.connect(str(duckdb_path), config={"access_mode": "read_only"})
                break
            except Exception as e:
                last_error = e
                if "already open" in str(e).lower() and attempt < max_retries - 1:
                    # File locked by another process (e.g., Windows Explorer)
                    # Wait and retry
                    time.sleep(0.5)
                    continue
                raise
        else:
            # All retries failed
            if last_error:
                raise last_error

        try:
            # Count tables by schema (including source-specific schemas like raw_stripe_test_1)
            # Exclude dlt internal tables (_dlt_*) as they are metadata, not user data
            row = conn.execute("""
                SELECT count(*)
                FROM information_schema.tables
                WHERE (table_schema='raw' OR table_schema LIKE 'raw_%')
                AND table_name NOT LIKE '_dlt_%'
            """).fetchone()
            raw_tables = row[0] if row else 0

            row = conn.execute("""
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema='staging' OR table_schema LIKE 'staging_%'
            """).fetchone()
            staging_tables = row[0] if row else 0

            row = conn.execute("""
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema='marts' OR table_schema LIKE 'marts_%'
            """).fetchone()
            marts_tables = row[0] if row else 0

            total_tables = raw_tables + staging_tables + marts_tables

            # Determine health status based on size
            if size_gb < 50:
                status = "healthy"
            elif size_gb < 100:
                status = "large"
            else:
                status = "critical"

            return {
                "size_gb": round(size_gb, 2),
                "size_mb": round(size_mb, 2),
                "tables": total_tables,
                "status": status,
                "raw_tables": raw_tables,
                "staging_tables": staging_tables,
                "marts_tables": marts_tables,
            }

        finally:
            conn.close()

    except Exception as e:
        error_lower = str(e).lower()
        if "already open" in error_lower or "lock" in error_lower:
            causes = [
                "Another process holds the DuckDB write lock",
                "A crashed process left a stale lock",
            ]
            fix = "Stop other dango processes, or wait and retry"
        elif "permission" in error_lower:
            causes = [
                "File permission denied on the DuckDB file",
                "Database directory is read-only",
            ]
            fix = "Check file permissions on the data/ directory"
        elif "no such file" in error_lower or "not found" in error_lower:
            causes = [
                "DuckDB file does not exist yet",
                "data/ directory was moved or deleted",
            ]
            fix = "Run 'dango sync' to create the database, or check the data/ path"
        else:
            causes = ["DuckDB file may be corrupt", "Incompatible DuckDB version"]
            fix = "Check DuckDB version compatibility or restore from backup"
        raise DuckDBHealthError(
            f"Failed to check DuckDB health: {e}",
            user_message=format_structured_error(
                what_failed=f"DuckDB health check failed for {duckdb_path}",
                causes=causes,
                suggested_fix=fix,
            ),
            context={"db_path": str(duckdb_path)},
        ) from e


def get_disk_usage_summary(project_root: Path) -> dict[str, Any]:
    """
    Get detailed disk usage information

    Args:
        project_root: Path to project root directory

    Returns:
        Dictionary with disk usage information
    """
    try:
        disk_usage = shutil.disk_usage(project_root)

        free_gb = disk_usage.free / (1024**3)
        total_gb = disk_usage.total / (1024**3)
        used_gb = disk_usage.used / (1024**3)
        used_pct = (disk_usage.used / disk_usage.total) * 100

        # Determine status
        if free_gb > 10:
            status = "healthy"
        elif free_gb > 5:
            status = "warning"
        else:
            status = "critical"

        return {
            "free_gb": round(free_gb, 2),
            "total_gb": round(total_gb, 2),
            "used_gb": round(used_gb, 2),
            "used_pct": round(used_pct, 1),
            "status": status,
        }

    except Exception as e:
        console.print(f"[yellow]⚠️  Could not get disk usage: {e}[/yellow]")
        return {"free_gb": 0, "total_gb": 0, "used_gb": 0, "used_pct": 0, "status": "unknown"}


def get_duckdb_capacity(duckdb_path: Path, project_root: Path) -> dict[str, Any]:
    """Compute DuckDB warehouse capacity relative to system resources.

    Uses disk free space and total RAM to estimate a recommended maximum
    database size, then reports what percentage of that maximum the current
    database occupies.

    Args:
        duckdb_path: Path to the DuckDB database file.
        project_root: Path to the project root (used for disk_usage).

    Returns:
        Dictionary with capacity information. Returns safe fallback on error.
    """
    try:
        import psutil

        # Current DB size
        if duckdb_path.exists():
            duckdb_size_bytes = duckdb_path.stat().st_size
        else:
            duckdb_size_bytes = 0

        # System resources
        disk = shutil.disk_usage(project_root)
        disk_free_bytes = disk.free
        ram_bytes = psutil.virtual_memory().total

        # Recommended max: min(4x RAM, 80% of free disk)
        # Note: DB size uses disk space that reduces disk_free, so the
        # denominator already accounts for existing DB consumption.
        recommended_max = min(ram_bytes * 4, int(disk_free_bytes * 0.8))
        # Avoid division by zero
        if recommended_max > 0:
            capacity_pct = min(round((duckdb_size_bytes / recommended_max) * 100, 1), 100.0)
        else:
            capacity_pct = 100.0 if duckdb_size_bytes > 0 else 0.0

        # Status thresholds
        if capacity_pct > 75:
            status = "critical"
        elif capacity_pct > 50:
            status = "warning"
        else:
            status = "healthy"

        return {
            "duckdb_size_bytes": duckdb_size_bytes,
            "duckdb_capacity_pct": capacity_pct,
            "recommended_max_db_size_bytes": recommended_max,
            "duckdb_capacity_status": status,
            "duckdb_capacity_warning": capacity_pct > 75,
        }
    except Exception:  # noqa: BLE001
        logger.debug("duckdb_capacity_check_failed", exc_info=True)
        return {
            "duckdb_size_bytes": 0,
            "duckdb_capacity_pct": 0.0,
            "recommended_max_db_size_bytes": 0,
            "duckdb_capacity_status": "unknown",
            "duckdb_capacity_warning": False,
        }


def _dir_size_bytes(path: Path) -> int:
    """Sum file sizes in a directory tree.

    Returns 0 if the directory does not exist or is empty.
    """
    if not path.is_dir():
        return 0
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


_component_disk_cache: dict[str, Any] | None = None
_component_disk_cache_time: float = 0
_COMPONENT_DISK_TTL: float = 60  # seconds


def get_component_disk_usage(project_root: Path) -> dict[str, Any]:
    """Get per-component disk usage breakdown.

    Collects sizes for DuckDB (file + per-schema estimates), Metabase H2,
    CSV uploads (per source), dbt artifacts, dlt pipeline state, and backups.

    Results are cached for 60 seconds to avoid repeated directory traversal
    on the 30-second health poll interval.

    Args:
        project_root: Path to Dango project root directory.

    Returns:
        Dictionary with per-component sizes in MB, plus a total.
        Missing components return 0 or None (never raises).
    """
    global _component_disk_cache, _component_disk_cache_time  # noqa: PLW0603
    now = time.monotonic()
    if (
        _component_disk_cache is not None
        and (now - _component_disk_cache_time) < _COMPONENT_DISK_TTL
    ):
        return _component_disk_cache
    result: dict[str, Any] = {}

    # DuckDB file size + per-schema estimates
    duckdb_info: dict[str, Any] = {"file_size_mb": 0, "schema_sizes": {}}
    duckdb_path = project_root / "data" / "warehouse.duckdb"
    try:
        if duckdb_path.exists():
            duckdb_info["file_size_mb"] = round(duckdb_path.stat().st_size / (1024**2), 2)
            # Per-schema estimated sizes (read-only connection).
            # NOTE: DuckDB stores everything in a single file; there is no true
            # per-schema disk size.  ``estimated_size`` from ``duckdb_tables()``
            # reflects *uncompressed in-memory* size, so it will usually be
            # larger than the actual on-disk footprint.  We label these as
            # "estimated data size" in the UI to avoid confusion.
            try:
                conn = duckdb.connect(str(duckdb_path), config={"access_mode": "read_only"})
                try:
                    rows = conn.execute(
                        "SELECT schema_name, COALESCE(SUM(estimated_size), 0) "
                        "FROM duckdb_tables() GROUP BY schema_name"
                    ).fetchall()
                    duckdb_info["schema_sizes"] = {row[0]: int(row[1]) for row in rows}
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                logger.debug("duckdb_schema_sizes_failed", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("duckdb_file_size_failed", exc_info=True)
    result["duckdb"] = duckdb_info

    # Metabase H2 database size (inside Docker container)
    metabase_info: dict[str, Any] = {"size_mb": None}
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "metabase",
                "stat",
                "-c",
                "%s",
                "/metabase-data/metabase.db.mv.db",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_root,
        )
        if proc.returncode == 0 and proc.stdout.strip().isdigit():
            metabase_info["size_mb"] = round(int(proc.stdout.strip()) / (1024**2), 2)
    except Exception:  # noqa: BLE001
        pass  # Container not running or Docker unavailable
    result["metabase"] = metabase_info

    # CSV uploads — per source subdirectory
    csv_info: dict[str, Any] = {"total_mb": 0, "by_source": {}}
    uploads_dir = project_root / "data" / "uploads"
    try:
        if uploads_dir.is_dir():
            total_csv_bytes = 0
            for child in sorted(uploads_dir.iterdir()):
                if child.is_dir():
                    source_bytes = _dir_size_bytes(child)
                    csv_info["by_source"][child.name] = round(source_bytes / (1024**2), 2)
                    total_csv_bytes += source_bytes
            csv_info["total_mb"] = round(total_csv_bytes / (1024**2), 2)
    except Exception:  # noqa: BLE001
        logger.debug("csv_uploads_size_failed", exc_info=True)
    result["csv_uploads"] = csv_info

    # dbt artifacts
    dbt_bytes = _dir_size_bytes(project_root / "dbt" / "target")
    result["dbt_artifacts"] = {"size_mb": round(dbt_bytes / (1024**2), 2)}

    # dlt pipeline state
    dlt_bytes = _dir_size_bytes(project_root / ".dlt" / "pipelines")
    result["dlt_pipelines"] = {"size_mb": round(dlt_bytes / (1024**2), 2)}

    # Local backups
    backup_bytes = _dir_size_bytes(project_root / ".dango" / "backups")
    result["backups"] = {"size_mb": round(backup_bytes / (1024**2), 2)}

    # Total across all components
    total_mb = duckdb_info["file_size_mb"]
    total_mb += metabase_info["size_mb"] or 0
    total_mb += csv_info["total_mb"]
    total_mb += result["dbt_artifacts"]["size_mb"]
    total_mb += result["dlt_pipelines"]["size_mb"]
    total_mb += result["backups"]["size_mb"]
    result["total_mb"] = round(total_mb, 2)

    _component_disk_cache = result
    _component_disk_cache_time = now

    return result


def print_health_summary(project_root: Path, duckdb_path: Path) -> None:
    """
    Print a summary of platform health (for CLI display)

    Args:
        project_root: Path to project root directory
        duckdb_path: Path to DuckDB database file
    """
    try:
        # Get disk usage
        disk = get_disk_usage_summary(project_root)

        # Get DB health
        db_health = check_duckdb_health(duckdb_path)

        console.print("\n[bold]Platform Health:[/bold]")
        console.print(f"  💾 Disk Space: {disk['free_gb']}GB free ({disk['used_pct']}% used)")

        if db_health["status"] != "new":
            console.print(f"  🗄️  Database: {db_health['size_mb']}MB ({db_health['tables']} tables)")
            console.print(f"     • Raw: {db_health['raw_tables']} tables")
            console.print(f"     • Staging: {db_health['staging_tables']} tables")
            console.print(f"     • Marts: {db_health['marts_tables']} tables")
        else:
            console.print("  🗄️  Database: New (no data yet)")

        # Show warnings
        if disk["status"] == "warning":
            console.print("[yellow]  ⚠️  Low disk space - consider freeing up space[/yellow]")
        elif disk["status"] == "critical":
            console.print("[red]  ❌ Critical disk space - sync may fail[/red]")

        if db_health["status"] == "large":
            console.print("[yellow]  ⚠️  Large database - consider archiving old data[/yellow]")
        elif db_health["status"] == "critical":
            console.print("[red]  ❌ Very large database - performance may be affected[/red]")

        console.print()

    except Exception as e:
        console.print(f"[yellow]⚠️  Could not print health summary: {e}[/yellow]")
