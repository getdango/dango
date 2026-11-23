"""dango/transformation/__init__.py

Dango Transformation Module

Handles dbt integration and SQL model generation.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from dango.exceptions import format_structured_error

console = Console()


def _dbt_telemetry_env() -> dict[str, str]:
    """Return an env dict for the dbt subprocess, with telemetry disabled if opted out.

    Always a complete copy of os.environ ({**os.environ}) — never mutates
    os.environ in place. The opt-out state comes from the dbt_telemetry key
    in ~/.dango/config.yml, written by dango/cli/commands/telemetry.py,
    machine-level like Dango's own telemetry identity. DBT_SEND_ANONYMOUS_USAGE_STATS
    is dbt-core's actual documented env var (dbt/cli/params.py); DO_NOT_TRACK
    is set alongside it as defense in depth, matching the community
    convention dango/telemetry.py also honors.
    """
    import os

    from dango.telemetry import _read_global_config

    env = {**os.environ}
    data = _read_global_config()
    if data.get("dbt_telemetry", True) is False:
        env["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
        env["DO_NOT_TRACK"] = "1"
    return env


def _get_dbt_executable() -> str:
    """
    Get the path to the dbt executable.

    Prefers the dbt installed beside the running Python interpreter (the
    project's own environment), then a dbt found on PATH, then bare 'dbt'.

    Returns:
        Path to dbt executable
    """
    name = "dbt.exe" if os.name == "nt" else "dbt"
    dbt_path = Path(sys.executable).parent / name
    if dbt_path.is_file():
        return str(dbt_path)

    found = shutil.which("dbt")
    if found:
        return found

    return "dbt"


def installed_dbt_version() -> str:
    """The installed dbt-core version for display, from local package metadata (no network)."""
    from importlib import metadata

    try:
        return f"dbt-core {metadata.version('dbt-core')}"
    except metadata.PackageNotFoundError:
        return "dbt CLI starts (dbt-core version not found in package metadata)"


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
    from dango.transformation.build_outcome import (
        assess_failed_build,
        failure_notes,
        tests_only_note,
    )
    from dango.utils.dbt_status import update_model_status

    dbt_dir = project_root / "dbt"

    # Get dbt executable path
    dbt_cmd = _get_dbt_executable()

    # Taken before dbt starts: a run_results.json older than this belongs to an earlier build.
    build_started = time.time()

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
            stdin=subprocess.DEVNULL,  # never inherit the MCP server's JSON-RPC stdin
            text=True,
            timeout=300,  # 5 minute timeout
            env=_dbt_telemetry_env(),
            encoding="utf-8",
            errors="replace",
        )

        output = result.stdout + result.stderr

        # Update persistent model status after run (even with test failures)
        if result.returncode == 0:
            update_model_status(project_root)
            return (True, output)

        # dbt build exits non-zero when any test fails, and then SKIPS everything downstream of
        # the failure. Decide from this build's run_results.json, never from the output text:
        # only failed tests with nothing skipped or errored mean the data was transformed.
        verdict = assess_failed_build(project_root, since=build_started)
        if verdict.transformed:
            update_model_status(project_root)
            return (True, f"{output}\n{tests_only_note(verdict)}\n")
        output_lower = output.lower()
        if verdict.usable and verdict.tests_failed and not verdict.errored:
            causes = ["A data test failed, so dbt skipped the models that depend on it"]
            fix = (
                "Fix the data behind the failing test named below (often duplicate or blank "
                "key values in the source), then run: dango run"
            )
        elif "compilation error" in output_lower or "parsing error" in output_lower:
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
            fix = "Stop other syncs, then retry: dango run"
        else:
            causes = ["dbt model logic error", "Missing dependency or configuration"]
            fix = "Review the error output and check dbt model files in dbt/models/"
        structured = format_structured_error(
            what_failed="dbt build failed", causes=causes, suggested_fix=fix
        )
        detail = "\n".join(failure_notes(verdict)) or (
            "dango: the dbt build exited with an error that its run results do not explain (they are "
            "missing, stale, unreadable or show no failed node), so the build is treated as "
            "failed."
        )
        return (False, f"{structured}\n\n{detail}\n\nFull output:\n{output}")

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
                suggested_fix="Run a subset: dango run --select model_name",
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
            stdin=subprocess.DEVNULL,  # never inherit the MCP server's JSON-RPC stdin
            text=True,
            timeout=300,  # 5 minute timeout
            env=_dbt_telemetry_env(),
            encoding="utf-8",
            errors="replace",
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
            stdin=subprocess.DEVNULL,  # never inherit the MCP server's JSON-RPC stdin
            text=True,
            timeout=60,
            env=_dbt_telemetry_env(),
            encoding="utf-8",
            errors="replace",
        )

        return (result.returncode == 0, result.stdout + result.stderr)

    except subprocess.TimeoutExpired:
        return (False, "dbt docs generate timed out")
    except Exception as e:
        return (False, f"dbt docs generate failed: {str(e)}")
