"""dango/notebooks/locking.py

File-level locking for notebook concurrent editing protection.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from dango.utils.dango_db import connect

logger = logging.getLogger(__name__)

_LOCK_DURATION_MINUTES = 15


def _clean_expired_locks(conn) -> None:  # type: ignore[no-untyped-def]
    """Remove expired lock entries.

    Args:
        conn: SQLite connection with ``notebook_locks`` table.
    """
    conn.execute("DELETE FROM notebook_locks WHERE expires_at < datetime('now')")
    conn.commit()


def acquire_lock(project_root: Path, notebook_id: str, user: str) -> bool:
    """Acquire an editing lock on a notebook.

    Args:
        project_root: Project root directory.
        notebook_id: Unique identifier for the notebook.
        user: Username requesting the lock.

    Returns:
        ``True`` if lock was acquired, ``False`` if already locked by another user.
    """
    with connect(project_root) as conn:
        _clean_expired_locks(conn)

        row = conn.execute(
            "SELECT locked_by FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()

        if row is not None:
            if row["locked_by"] == user:
                # Refresh existing lock
                conn.execute(
                    "UPDATE notebook_locks "
                    "SET expires_at = datetime('now', '+15 minutes') "
                    "WHERE notebook_id = ?",
                    (notebook_id,),
                )
                conn.commit()
                return True
            return False

        conn.execute(
            "INSERT INTO notebook_locks (notebook_id, locked_by, locked_at, expires_at) "
            "VALUES (?, ?, datetime('now'), datetime('now', '+15 minutes'))",
            (notebook_id, user),
        )
        conn.commit()
        return True


def release_lock(project_root: Path, notebook_id: str, user: str) -> bool:
    """Release an editing lock on a notebook.

    Args:
        project_root: Project root directory.
        notebook_id: Unique identifier for the notebook.
        user: Username releasing the lock.

    Returns:
        ``True`` if lock was released, ``False`` if not locked by this user.
    """
    with connect(project_root) as conn:
        _clean_expired_locks(conn)

        row = conn.execute(
            "SELECT locked_by FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()

        if row is None or row["locked_by"] != user:
            return False

        conn.execute(
            "DELETE FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        )
        conn.commit()
        return True


def refresh_lock(project_root: Path, notebook_id: str, user: str) -> bool:
    """Extend lock expiry by 15 minutes.

    Args:
        project_root: Project root directory.
        notebook_id: Unique identifier for the notebook.
        user: Username refreshing the lock.

    Returns:
        ``True`` if lock was refreshed, ``False`` if not locked by this user.
    """
    with connect(project_root) as conn:
        _clean_expired_locks(conn)

        row = conn.execute(
            "SELECT locked_by FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()

        if row is None or row["locked_by"] != user:
            return False

        conn.execute(
            "UPDATE notebook_locks "
            "SET expires_at = datetime('now', '+15 minutes') "
            "WHERE notebook_id = ?",
            (notebook_id,),
        )
        conn.commit()
        return True


def force_release_lock(project_root: Path, notebook_id: str) -> bool:
    """Force-release a lock regardless of owner (admin action).

    Args:
        project_root: Project root directory.
        notebook_id: Unique identifier for the notebook.

    Returns:
        ``True`` if a lock was released, ``False`` if none existed.
    """
    with connect(project_root) as conn:
        row = conn.execute(
            "SELECT locked_by FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()

        if row is None:
            return False

        conn.execute(
            "DELETE FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        )
        conn.commit()
        logger.info("Force-released lock on %s (was held by %s)", notebook_id, row["locked_by"])
        return True


def is_locked(project_root: Path, notebook_id: str) -> bool:
    """Check if a notebook is currently locked.

    Args:
        project_root: Project root directory.
        notebook_id: Unique identifier for the notebook.

    Returns:
        ``True`` if locked by any user with a non-expired lock.
    """
    with connect(project_root) as conn:
        _clean_expired_locks(conn)

        row = conn.execute(
            "SELECT 1 FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()

        return row is not None


def get_lock_info(project_root: Path, notebook_id: str) -> dict[str, str] | None:
    """Get lock details for a notebook.

    Args:
        project_root: Project root directory.
        notebook_id: Unique identifier for the notebook.

    Returns:
        Dict with ``locked_by``, ``locked_at``, ``expires_at``; or ``None``.
    """
    with connect(project_root) as conn:
        _clean_expired_locks(conn)

        row = conn.execute(
            "SELECT locked_by, locked_at, expires_at FROM notebook_locks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()

        if row is None:
            return None

        return {
            "locked_by": row["locked_by"],
            "locked_at": row["locked_at"],
            "expires_at": row["expires_at"],
        }


def copy_locked_notebook(project_root: Path, notebook_id: str, new_owner: str) -> str:
    """Copy a locked notebook file and register the copy in metadata.

    Creates ``{name}_copy_{timestamp}.py`` and registers it in
    ``notebook_metadata``.

    Args:
        project_root: Project root directory.
        notebook_id: ID of the locked notebook (used as filename stem).
        new_owner: Username who will own the copy.

    Returns:
        Filename of the new copy.

    Raises:
        FileNotFoundError: If the original notebook file does not exist.
    """
    notebooks_dir = project_root / "notebooks"
    original = notebooks_dir / f"{notebook_id}.py"
    if not original.exists():
        raise FileNotFoundError(f"Notebook file not found: {original}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    copy_name = f"{notebook_id}_copy_{timestamp}"
    copy_path = notebooks_dir / f"{copy_name}.py"
    shutil.copy2(str(original), str(copy_path))

    new_id = str(uuid.uuid4())
    now = datetime.now().isoformat()

    with connect(project_root) as conn:
        conn.execute(
            "INSERT INTO notebook_metadata (id, name, description, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, copy_name, f"Copy of {notebook_id}", new_owner, now, now),
        )
        conn.commit()

    return f"{copy_name}.py"
