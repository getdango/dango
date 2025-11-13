"""
Dango Transformation Module

Handles dbt integration and SQL model generation.
"""

import subprocess
import sys
from pathlib import Path
from typing import Tuple, Optional
from rich.console import Console

console = Console()


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


def run_dbt_models(project_root: Path, select: Optional[str] = None) -> Tuple[bool, str]:
    """
    Run dbt models to create staging/marts tables in DuckDB.

    Args:
        project_root: Path to project root
        select: Optional dbt selection criteria (e.g., "source:test_csv+", "model_name+")
                If None, runs all models. Use source-based selection for targeted runs.

    Returns:
        Tuple of (success, output)
    """
    from dango.utils.dbt_status import update_model_status

    dbt_dir = project_root / "dbt"

    # Get dbt executable path
    dbt_cmd = _get_dbt_executable()

    # Build dbt command with optional selection
    cmd = [dbt_cmd, "run", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
    if select:
        cmd.extend(["--select", select])

    try:
        result = subprocess.run(
            cmd,
            cwd=dbt_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        # Update persistent model status after successful run
        if result.returncode == 0:
            update_model_status(project_root)

        return (result.returncode == 0, result.stdout + result.stderr)

    except subprocess.TimeoutExpired:
        return (False, "dbt run timed out after 5 minutes")
    except Exception as e:
        return (False, f"dbt run failed: {str(e)}")


def generate_dbt_docs(project_root: Path) -> Tuple[bool, str]:
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
            [dbt_cmd, "docs", "generate", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)],
            cwd=dbt_dir,
            capture_output=True,
            text=True,
            timeout=60
        )

        return (result.returncode == 0, result.stdout + result.stderr)

    except subprocess.TimeoutExpired:
        return (False, "dbt docs generate timed out")
    except Exception as e:
        return (False, f"dbt docs generate failed: {str(e)}")
