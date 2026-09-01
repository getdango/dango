"""dango/transformation/__init__.py

Dango Transformation Module

Handles dbt integration and SQL model generation.
"""

import subprocess
import sys
from pathlib import Path

from rich.console import Console

from dango.exceptions import format_structured_error

console = Console()


def _dbt_telemetry_env() -> dict[str, str]:
    """Return an env dict for the dbt subprocess, with telemetry disabled if opted out.

    Always a complete copy of os.environ ({**os.environ}) — never mutates
    os.environ in place. The opt-out state comes from the sentinel file
    dango/cli/commands/telemetry.py writes (~/.dango/dbt_telemetry),
    machine-level like Dango's own telemetry identity. DBT_SEND_ANONYMOUS_USAGE_STATS
    is dbt-core's actual documented env var (dbt/cli/params.py); DO_NOT_TRACK
    is set alongside it as defense in depth, matching the community
    convention dango/telemetry.py also honors.
    """
    import os

    env = {**os.environ}
    sentinel = Path.home() / ".dango" / "dbt_telemetry"
    if sentinel.exists() and sentinel.read_text().strip() == "false":
        env["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
        env["DO_NOT_TRACK"] = "1"
    return env


def _get_dbt_executable() -> str:
    """
    Get the path to the dbt executable.

    Tries to find dbt in the same venv as the current Python interpreter.
    Falls back to 'dbt' if not found (system PATH).

    Returns:
        Path to dbt executable
    """
    # Get the directory where the current Python executable is located
    python_bin_dir = Path(sys.executable).parent

    # Check if dbt exists in the same bin directory
    dbt_path = python_bin_dir / "dbt"

    if dbt_path.exists():
        return str(dbt_path)

    # Fall back to system dbt (likely to fail, but preserves backward compatibility)
    return "dbt"


def run_dbt_models(
    project_root: Path,
    select: str | None = None,
    full_refresh: bool = False,
) -> tuple[bool, str]:
    """
    Run dbt models to create staging/marts tables in DuckDB.

    Args:
        project_root: Path to project root
        select: Optional dbt selection criteria (e.g., "source:test_csv+", "model_name+")
                If None, runs all models. Use source-based selection for targeted runs.
        full_refresh: If True, pass --full-refresh to rebuild models from scratch.

    Returns:
        Tuple of (success, output)
    """
    from dango.utils.dbt_status import update_model_status

    dbt_dir = project_root / "dbt"

    # Get dbt executable path
    dbt_cmd = _get_dbt_executable()

    # Build dbt command with optional selection
    cmd = [dbt_cmd, "build", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
    if select:
        cmd.extend(["--select", select])
    if full_refresh:
        cmd.append("--full-refresh")

    try:
        result = subprocess.run(
            cmd,
            cwd=dbt_dir,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            env=_dbt_telemetry_env(),
        )

        output = result.stdout + result.stderr

        # Update persistent model status after run (even with test failures)
        if result.returncode == 0:
            update_model_status(project_root)
            return (True, output)

        # dbt build returns non-zero if tests fail, even when models succeed.
        # Treat test-only failures as success (data was transformed correctly).
        if "Completed with" in output and "test" in output.lower():
            update_model_status(project_root)
            return (True, output)
        output_lower = output.lower()
        if "compilation error" in output_lower or "parsing error" in output_lower:
            causes = [
                "SQL syntax error in a dbt model",
                "Missing ref() or source() target",
                "Undefined macro",
            ]
            fix = "Check the dbt model file indicated in the error output above"
        elif "database error" in output_lower or "duckdb" in output_lower:
            causes = [
                "DuckDB write lock held by another process",
                "Database file corrupted",
            ]
            fix = "Stop other syncs, then retry: dango transform"
        else:
            causes = ["dbt model logic error", "Missing dependency or configuration"]
            fix = "Review the error output and check dbt model files in dbt/models/"
        structured = format_structured_error(
            what_failed="dbt build failed", causes=causes, suggested_fix=fix
        )
        return (False, f"{structured}\n\nFull output:\n{output}")

    except subprocess.TimeoutExpired:
        return (
            False,
            format_structured_error(
                what_failed="dbt build timed out after 5 minutes",
                causes=[
                    "Large number of models",
                    "Complex SQL queries",
                    "DuckDB lock contention",
                ],
                suggested_fix="Run a subset: dango transform --select model_name",
            ),
        )
    except Exception as e:
        return (False, f"dbt build failed: {str(e)}")


def run_dbt_snapshots(
    project_root: Path,
    select: str | None = None,
) -> tuple[bool, str]:
    """
    Run dbt snapshots to capture SCD Type 2 change history.

    Args:
        project_root: Path to project root
        select: Optional snapshot name to run (e.g., "snap_shopify_orders").
                If None, runs all snapshots.

    Returns:
        Tuple of (success, output)
    """
    dbt_dir = project_root / "dbt"

    # Get dbt executable path
    dbt_cmd = _get_dbt_executable()

    # Build dbt command
    cmd = [dbt_cmd, "snapshot", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
    if select:
        cmd.extend(["--select", select])

    try:
        result = subprocess.run(
            cmd,
            cwd=dbt_dir,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            env=_dbt_telemetry_env(),
        )

        if result.returncode == 0:
            return (True, result.stdout + result.stderr)

        output = result.stdout + result.stderr
        output_lower = output.lower()
        if "compilation error" in output_lower or "parsing error" in output_lower:
            causes = [
                "SQL syntax error in a snapshot file",
                "Missing source() target",
                "Invalid snapshot configuration",
            ]
            fix = "Check the snapshot SQL file indicated in the error output"
        elif "database error" in output_lower or "duckdb" in output_lower:
            causes = [
                "DuckDB write lock held by another process",
                "Database file corrupted",
            ]
            fix = "Stop other syncs, then retry: dango snapshot run"
        else:
            causes = ["Snapshot configuration error", "Missing dependency or source data"]
            fix = "Review the error output and check snapshot files in dbt/snapshots/"
        structured = format_structured_error(
            what_failed="dbt snapshot failed", causes=causes, suggested_fix=fix
        )
        return (False, f"{structured}\n\nFull output:\n{output}")

    except subprocess.TimeoutExpired:
        return (
            False,
            format_structured_error(
                what_failed="dbt snapshot timed out after 5 minutes",
                causes=[
                    "Large number of snapshots",
                    "Complex SQL queries",
                    "DuckDB lock contention",
                ],
                suggested_fix="Run a subset: dango snapshot run --select snapshot_name",
            ),
        )
    except Exception as e:
        return (False, f"dbt snapshot failed: {str(e)}")


def generate_dbt_docs(project_root: Path) -> tuple[bool, str]:
    """
    Generate dbt documentation.

    Args:
        project_root: Path to project root

    Returns:
        Tuple of (success, output)
    """
    dbt_dir = project_root / "dbt"

    # Get dbt executable path
    dbt_cmd = _get_dbt_executable()

    try:
        result = subprocess.run(
            [
                dbt_cmd,
                "docs",
                "generate",
                "--project-dir",
                str(dbt_dir),
                "--profiles-dir",
                str(dbt_dir),
            ],
            cwd=dbt_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env=_dbt_telemetry_env(),
        )

        return (result.returncode == 0, result.stdout + result.stderr)

    except subprocess.TimeoutExpired:
        return (False, "dbt docs generate timed out")
    except Exception as e:
        return (False, f"dbt docs generate failed: {str(e)}")
