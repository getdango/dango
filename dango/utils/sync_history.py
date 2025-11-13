"""
Shared sync history management for both CLI and Web UI
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def get_sync_history_file(project_root: Path, source_name: str) -> Path:
    """Get path to sync history file for a source"""
    history_dir = project_root / ".dango" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir / f"{source_name}.json"


def save_sync_history_entry(project_root: Path, source_name: str, entry: Dict[str, Any]):
    """Save a sync history entry for a source"""
    history_file = get_sync_history_file(project_root, source_name)

    try:
        # Load existing history
        history = []
        if history_file.exists():
            with open(history_file, 'r') as f:
                history = json.load(f)

        # Add new entry
        history.append(entry)

        # Keep only last 100 entries
        if len(history) > 100:
            history = history[-100:]

        # Save back
        with open(history_file, 'w') as f:
            json.dump(history, f, indent=2)

    except Exception as e:
        print(f"Warning: Failed to save sync history for {source_name}: {e}")


def load_sync_history(project_root: Path, source_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Load sync history for a source"""
    history_file = get_sync_history_file(project_root, source_name)

    if not history_file.exists():
        return []

    try:
        with open(history_file, 'r') as f:
            history = json.load(f)
            # Return most recent entries first
            return history[-limit:][::-1] if history else []
    except Exception as e:
        print(f"Warning: Failed to load sync history for {source_name}: {e}")
        return []
