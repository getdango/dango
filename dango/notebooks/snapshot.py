"""dango/notebooks/snapshot.py

DuckDB read-only snapshot management for notebooks.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"


def create_snapshot(project_root: Path, username: str = "default") -> Path:
    """Create a read-only DuckDB snapshot for notebook use.

    Copies ``data/warehouse.duckdb`` to
    ``.dango/snapshots/warehouse_{username}_{timestamp}.duckdb``.
    Cleans up old snapshots (keeps 3 per user) before creating.

    Args:
        project_root: Project root directory.
        username: Username to associate with the snapshot.

    Returns:
        Path to the new snapshot file.

    Raises:
        FileNotFoundError: If ``data/warehouse.duckdb`` does not exist.
    """
    warehouse = project_root / "data" / "warehouse.duckdb"
    if not warehouse.exists():
        raise FileNotFoundError(
            f"Warehouse database not found at {warehouse}. Run a sync first to create it."
        )

    snapshots_dir = project_root / ".dango" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    cleanup_snapshots(project_root, username, keep=3)

    timestamp = datetime.now().strftime(_TIMESTAMP_FMT)
    snapshot_name = f"warehouse_{username}_{timestamp}.duckdb"
    snapshot_path = snapshots_dir / snapshot_name

    shutil.copy2(str(warehouse), str(snapshot_path))
    logger.info("Created snapshot %s (%d bytes)", snapshot_name, snapshot_path.stat().st_size)

    return snapshot_path


def list_snapshots(
    project_root: Path, username: str | None = None
) -> list[dict[str, str | int | Path]]:
    """List available DuckDB snapshots.

    Args:
        project_root: Project root directory.
        username: If given, filter to this user's snapshots only.

    Returns:
        List of dicts with keys: ``name``, ``path``, ``username``,
        ``created_at``, ``size_bytes``.  Sorted newest first.
    """
    snapshots_dir = project_root / ".dango" / "snapshots"
    if not snapshots_dir.exists():
        return []

    results: list[dict[str, str | int | Path]] = []
    for f in snapshots_dir.glob("warehouse_*.duckdb"):
        parsed = _parse_snapshot_filename(f.name)
        if parsed is None:
            continue
        snap_user, snap_ts = parsed
        if username is not None and snap_user != username:
            continue
        results.append(
            {
                "name": f.name,
                "path": f,
                "username": snap_user,
                "created_at": snap_ts,
                "size_bytes": f.stat().st_size,
            }
        )

    results.sort(key=lambda x: str(x["created_at"]), reverse=True)
    return results


def cleanup_snapshots(project_root: Path, username: str, keep: int = 3) -> int:
    """Remove old snapshots for a user, keeping the newest *keep*.

    Args:
        project_root: Project root directory.
        username: Username whose snapshots to clean up.
        keep: Number of newest snapshots to retain.

    Returns:
        Number of snapshots removed.
    """
    snapshots = list_snapshots(project_root, username=username)
    to_remove = snapshots[keep:]
    removed = 0
    for snap in to_remove:
        try:
            Path(str(snap["path"])).unlink()
            removed += 1
        except OSError:
            logger.warning("Failed to remove snapshot %s", snap["name"])
    return removed


def _parse_snapshot_filename(filename: str) -> tuple[str, str] | None:
    """Extract username and timestamp from a snapshot filename.

    Expected format: ``warehouse_{username}_{YYYYMMDD_HHMMSS}.duckdb``

    Args:
        filename: The snapshot filename (basename only).

    Returns:
        Tuple of ``(username, timestamp_str)`` or ``None`` if unparseable.
    """
    if not filename.startswith("warehouse_") or not filename.endswith(".duckdb"):
        return None
    stem = filename[len("warehouse_") : -len(".duckdb")]
    # Timestamp is last 15 chars: YYYYMMDD_HHMMSS
    if len(stem) < 16:
        return None
    ts_part = stem[-15:]
    username = stem[:-16]
    if not username:
        return None
    try:
        datetime.strptime(ts_part, _TIMESTAMP_FMT)
    except ValueError:
        return None
    return username, ts_part
