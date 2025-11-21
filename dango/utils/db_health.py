"""
DuckDB Health Monitoring and Disk Space Utilities

Provides utilities for:
- Disk space checking before syncs
- DuckDB database health monitoring
- Database size tracking
"""

import shutil
import duckdb
from pathlib import Path
from typing import Dict, Any
from rich.console import Console

console = Console()


class DiskSpaceError(Exception):
    """Raised when disk space is insufficient"""
    pass


class DuckDBHealthError(Exception):
    """Raised when DuckDB health check fails"""
    pass


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
            console.print(f"[yellow]   Consider freeing up space before large syncs[/yellow]")

        return True

    except DiskSpaceError:
        raise
    except Exception as e:
        console.print(f"[yellow]⚠️  Could not check disk space: {e}[/yellow]")
        return True  # Don't block sync if check fails


def check_duckdb_health(duckdb_path: Path) -> Dict[str, Any]:
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
    import time
    import sys

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
                "marts_tables": 0
            }

        # Get file size
        size_bytes = duckdb_path.stat().st_size
        size_gb = size_bytes / (1024**3)
        size_mb = size_bytes / (1024**2)

        # Connect to database (read-only to avoid locks)
        # On Windows, retry if file is locked by Explorer or other processes
        max_retries = 3 if sys.platform == 'win32' else 1
        last_error = None

        for attempt in range(max_retries):
            try:
                conn = duckdb.connect(str(duckdb_path), read_only=True)
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
            raw_tables = conn.execute("""
                SELECT count(*)
                FROM information_schema.tables
                WHERE (table_schema='raw' OR table_schema LIKE 'raw_%')
                AND table_name NOT LIKE '_dlt_%'
            """).fetchone()[0]

            staging_tables = conn.execute("""
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema='staging' OR table_schema LIKE 'staging_%'
            """).fetchone()[0]

            marts_tables = conn.execute("""
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema='marts' OR table_schema LIKE 'marts_%'
            """).fetchone()[0]

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
                "marts_tables": marts_tables
            }

        finally:
            conn.close()

    except Exception as e:
        raise DuckDBHealthError(f"Failed to check DuckDB health: {e}")


def get_disk_usage_summary(project_root: Path) -> Dict[str, Any]:
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
            "status": status
        }

    except Exception as e:
        console.print(f"[yellow]⚠️  Could not get disk usage: {e}[/yellow]")
        return {
            "free_gb": 0,
            "total_gb": 0,
            "used_gb": 0,
            "used_pct": 0,
            "status": "unknown"
        }


def print_health_summary(project_root: Path, duckdb_path: Path):
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

        if db_health['status'] != 'new':
            console.print(f"  🗄️  Database: {db_health['size_mb']}MB ({db_health['tables']} tables)")
            console.print(f"     • Raw: {db_health['raw_tables']} tables")
            console.print(f"     • Staging: {db_health['staging_tables']} tables")
            console.print(f"     • Marts: {db_health['marts_tables']} tables")
        else:
            console.print(f"  🗄️  Database: New (no data yet)")

        # Show warnings
        if disk['status'] == 'warning':
            console.print(f"[yellow]  ⚠️  Low disk space - consider freeing up space[/yellow]")
        elif disk['status'] == 'critical':
            console.print(f"[red]  ❌ Critical disk space - sync may fail[/red]")

        if db_health['status'] == 'large':
            console.print(f"[yellow]  ⚠️  Large database - consider archiving old data[/yellow]")
        elif db_health['status'] == 'critical':
            console.print(f"[red]  ❌ Very large database - performance may be affected[/red]")

        console.print()

    except Exception as e:
        console.print(f"[yellow]⚠️  Could not print health summary: {e}[/yellow]")
