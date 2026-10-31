"""dango/notebooks/snapshot.py

DuckDB read-only snapshot management for notebooks.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from dango.notebooks.safe_files import copy_regular_file
from dango.platform import run_as

logger = logging.getLogger(__name__)

#: Where snapshots live when notebooks run as the restricted user (created by server setup).
ISOLATED_SNAPSHOT_DIR = Path("/srv/dango/runner/snapshots")
_ISOLATED_MODE = 0o640

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"
_NAME_ATTEMPTS = 100


def _snapshots_dir(project_root: Path) -> Path:
    """Snapshot directory: the runner-readable one when isolated, else under ``.dango``."""
    if run_as.uses_isolation():
        return ISOLATED_SNAPSHOT_DIR
    return project_root / ".dango" / "snapshots"


def _create_isolated_snapshot(warehouse: Path, username: str, now: datetime) -> Path:
    """Copy *warehouse* into the runner snapshot directory with mode 0640.

    The group (``dango-nb``) comes from the directory.  The source is opened
    without following a symlink (``data`` itself may be a link) and the mode is set with ``fchmod``.
    A name already taken (another snapshot in the same second) moves the
    timestamp on by a second until it is free, so the name stays parseable.
    """
    directory = ISOLATED_SNAPSHOT_DIR
    if not directory.is_dir():
        raise RuntimeError(
            f"Notebook snapshot directory {directory} is missing; run the server setup again"
        )
    for attempt in range(_NAME_ATTEMPTS):
        stamp = (now + timedelta(seconds=attempt)).strftime(_TIMESTAMP_FMT)
        target = directory / f"warehouse_{username}_{stamp}.duckdb"
        try:
            copy_regular_file(warehouse, target, mode=_ISOLATED_MODE)
        except FileExistsError:
            continue
        return target
    raise RuntimeError(f"Could not find a free snapshot name in {directory}")


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
        IsolationRequiredError: On a cloud server whose isolation is missing (D2).
        FileNotFoundError: If ``data/warehouse.duckdb`` does not exist.
    """
    run_as.require_isolation_ready()
    warehouse = project_root / "data" / "warehouse.duckdb"
    if not warehouse.exists():
        raise FileNotFoundError(
            f"Warehouse database not found at {warehouse}. Run a sync first to create it."
        )

    now = datetime.now()
    snapshot_name = f"warehouse_{username}_{now.strftime(_TIMESTAMP_FMT)}.duckdb"

    if run_as.uses_isolation():
        cleanup_snapshots(project_root, username, keep=3)
        snapshot_path = _create_isolated_snapshot(warehouse, username, now)
    else:
        snapshots_dir = project_root / ".dango" / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        cleanup_snapshots(project_root, username, keep=3)
        snapshot_path = snapshots_dir / snapshot_name
        shutil.copy2(str(warehouse), str(snapshot_path))
    logger.info("Created snapshot %s (%d bytes)", snapshot_path.name, snapshot_path.stat().st_size)

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
    snapshots_dir = _snapshots_dir(project_root)
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
