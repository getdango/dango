"""
Persistent dbt Model Status Manager

Maintains a persistent record of dbt model execution status, independent of
dbt's run_results.json which gets overwritten on each run.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional


def update_model_status(project_root: Path) -> None:
    """
    Update persistent model status from dbt's run_results.json

    Call this after any dbt run completes to update the persistent status.
    Only updates models that were in the current run, preserving others.

    Args:
        project_root: Path to project root
    """
    dbt_dir = project_root / "dbt"
    run_results_path = dbt_dir / "target" / "run_results.json"

    if not run_results_path.exists():
        return

    try:
        # Read dbt's run_results.json
        with open(run_results_path, 'r') as f:
            run_results = json.load(f)

        # Load our persistent status
        persistent_status = _load_persistent_status(project_root)

        # Update status for models that ran
        for result in run_results.get('results', []):
            unique_id = result.get('unique_id')
            if not unique_id:
                continue

            # Only track actual models (not tests, operations, etc)
            if not unique_id.startswith('model.'):
                continue

            # Extract timing info
            last_run = None
            timing = result.get('timing', [])
            for timing_entry in timing:
                if timing_entry.get('name') == 'execute':
                    last_run = timing_entry.get('started_at')
                    break

            # Fallback to compile start time
            if not last_run and timing:
                last_run = timing[0].get('started_at')

            # Update persistent status
            persistent_status[unique_id] = {
                'status': result.get('status'),
                'last_run': last_run
            }

        # Save updated persistent status
        _save_persistent_status(project_root, persistent_status)

    except Exception:
        # Don't fail the whole process if status update fails
        pass


def get_model_statuses(project_root: Path) -> Dict[str, Dict[str, Any]]:
    """
    Get persistent model statuses for UI display

    Returns:
        Dictionary mapping unique_id to {"status": str, "last_run": Optional[str]}
    """
    return _load_persistent_status(project_root)


def _load_persistent_status(project_root: Path) -> Dict[str, Dict[str, Any]]:
    """Load persistent status from file"""
    status_path = project_root / ".dango" / "dbt_model_status.json"

    if not status_path.exists():
        return {}

    try:
        with open(status_path, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_persistent_status(project_root: Path, status: Dict[str, Dict[str, Any]]) -> None:
    """Save persistent status to file"""
    status_path = project_root / ".dango" / "dbt_model_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(status_path, 'w') as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass
