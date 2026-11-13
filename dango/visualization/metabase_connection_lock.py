"""dango/visualization/metabase_connection_lock.py

Restrict Metabase's DuckDB connection to the warehouse file: the shared connection-details helper
and the one-time catch-up that applies it to connections created before the restriction existed.
"""

from __future__ import annotations

from pathlib import Path

EXTERNAL_ACCESS_LOCK_SQL = "SET enable_external_access=false;"
MAX_FAILED_ATTEMPTS = 3
_MARKER_NAME = "metabase_duckdb_lock_applied"
_ATTEMPTS_NAME = "metabase_duckdb_lock_attempts"
#: State files a Metabase app-DB restore must delete, so the next start re-checks the connection.
STATE_FILE_NAMES = (_MARKER_NAME, _ATTEMPTS_NAME)


def locked_duckdb_details(details: dict[str, object]) -> dict[str, object]:
    """Return *details* with the external-access lock in ``init_sql`` (idempotent, preserves other keys)."""
    out = dict(details)
    current = str(out.get("init_sql") or "")
    if EXTERNAL_ACCESS_LOCK_SQL not in current:
        out["init_sql"] = (current.rstrip() + " " + EXTERNAL_ACCESS_LOCK_SQL).strip()
    return out


def _state_file(project_root: Path, name: str) -> Path:
    return Path(project_root) / ".dango" / "state" / name


def failed_attempts(project_root: Path) -> int:
    """Return how many catch-up attempts have failed so far (0 when unrecorded or unreadable)."""
    try:
        return max(0, int(_state_file(project_root, _ATTEMPTS_NAME).read_text().strip()))
    except (OSError, ValueError):
        return 0


def catchup_pending(project_root: Path) -> bool:
    """True while Metabase is set up, the lock is not yet recorded and attempts remain.

    Read-only; makes no HTTP request.
    """
    from dango.auth.metabase_sync import _get_database_id

    if _state_file(project_root, _MARKER_NAME).exists():
        return False
    if failed_attempts(project_root) >= MAX_FAILED_ATTEMPTS:
        return False
    return _get_database_id(project_root) is not None


def apply_lock_catchup(
    project_root: Path,
    metabase_url: str,
    session_id: str | None = None,
    recheck: bool = False,
) -> bool | None:
    """Apply the lock to an existing connection; never raises, never retries.

    By default it runs once (the marker ends it). With ``recheck`` (the sync path, which already
    has a session) it ignores the marker, because a restored Metabase database can be unlocked
    while the marker survives: one GET, a PUT only when the details differ, and a failure is not
    counted against the start-time attempts.

    Returns None when nothing was attempted (already recorded, or Metabase not set up), True
    when the connection is locked (marker written), False when the attempt failed.
    """
    from dango.auth.metabase_sync import _get_database_id, ensure_duckdb_readonly

    marker = _state_file(project_root, _MARKER_NAME)
    try:
        if (marker.exists() and not recheck) or _get_database_id(project_root) is None:
            return None
        applied = ensure_duckdb_readonly(project_root, metabase_url, session_id=session_id)
        state_dir = marker.parent
        state_dir.mkdir(parents=True, exist_ok=True)
        if applied:
            marker.write_text("applied\n", encoding="utf-8")
            return True
        if not recheck:
            _state_file(project_root, _ATTEMPTS_NAME).write_text(
                f"{failed_attempts(project_root) + 1}\n", encoding="utf-8"
            )
        return False
    except Exception:
        return False
