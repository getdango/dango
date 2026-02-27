"""dango/platform/scheduling/history.py

Execution history tracking for scheduled jobs. Records start/completion/failure
of each job run in the scheduler SQLite database, and provides query functions
for the web UI, average duration warnings (TASK-037), and schedule listings.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dango.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"

VALID_STATUSES: frozenset[str] = frozenset(
    {STATUS_RUNNING, STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEOUT}
)

# Stale threshold — running records older than this are marked failed on cleanup
_STALE_RUNNING_HOURS = 24


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode enabled.

    Args:
        db_path: Path to the scheduler SQLite database.

    Returns:
        A configured sqlite3 connection.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a Row to a plain dict, parsing ``sources`` from JSON.

    Args:
        row: A sqlite3.Row from the execution_history table.

    Returns:
        Dict with all columns, ``sources`` parsed to a list.
    """
    d: dict[str, Any] = dict(row)
    if d.get("sources") is not None:
        try:
            d["sources"] = json.loads(d["sources"])
        except (json.JSONDecodeError, TypeError):
            logger.debug("sources_json_parse_failed", raw_value=d.get("sources"))
    return d


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, stripping timezone suffix for 3.10 compat.

    Python 3.10's ``fromisoformat()`` cannot parse ``+00:00`` or ``Z``
    suffixes, so we strip them before parsing and assume UTC.

    Args:
        value: ISO 8601 timestamp string.

    Returns:
        A timezone-aware UTC datetime.
    """
    cleaned = value.replace("+00:00", "").replace("Z", "")
    dt = datetime.fromisoformat(cleaned)
    return dt.replace(tzinfo=timezone.utc)


def _utcnow_iso() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def get_scheduler_db_path(project_root: str | Path) -> Path:
    """Return the path to the scheduler database.

    Args:
        project_root: Dango project root directory.

    Returns:
        Path to ``.dango/scheduler.db``.
    """
    return Path(project_root) / ".dango" / "scheduler.db"


# ---------------------------------------------------------------------------
# Record lifecycle (write)
# ---------------------------------------------------------------------------


def record_start(
    db_path: Path,
    schedule_name: str,
    sources: list[str] | None = None,
) -> int:
    """Insert a new running execution record.

    Args:
        db_path: Path to the scheduler database.
        schedule_name: Name of the schedule being executed.
        sources: Optional list of source names involved.

    Returns:
        The ``id`` of the newly created record.
    """
    sources_json = json.dumps(sources) if sources is not None else None
    now = _utcnow_iso()

    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO execution_history (schedule_name, sources, started_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (schedule_name, sources_json, now, STATUS_RUNNING),
        )
        conn.commit()
        row_id: int = cursor.lastrowid  # type: ignore[assignment]
        logger.debug("execution_record_started", record_id=row_id, schedule=schedule_name)
        return row_id
    finally:
        conn.close()


def _finish_record(
    db_path: Path,
    record_id: int,
    status: str,
    *,
    error: str | None = None,
    rows_processed: int | None = None,
) -> None:
    """Shared helper to finalize a running execution record.

    Reads ``started_at`` from the DB, computes duration, and updates the row.
    """
    now = datetime.now(tz=timezone.utc)
    now_iso = now.isoformat()

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT started_at FROM execution_history WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            logger.warning("execution_record_not_found", record_id=record_id)
            return

        started = _parse_iso(row["started_at"])
        duration = (now - started).total_seconds()

        conn.execute(
            """
            UPDATE execution_history
            SET status = ?, ended_at = ?, duration_seconds = ?,
                error = ?, rows_processed = ?
            WHERE id = ?
            """,
            (status, now_iso, duration, error, rows_processed, record_id),
        )
        conn.commit()
        logger.debug(
            "execution_record_finished",
            record_id=record_id,
            status=status,
            duration=round(duration, 2),
        )
    finally:
        conn.close()


def record_completion(db_path: Path, record_id: int, rows_processed: int | None = None) -> None:
    """Mark an execution as successfully completed.

    Args:
        db_path: Path to the scheduler database.
        record_id: The execution record ID.
        rows_processed: Optional row count from the job.
    """
    _finish_record(db_path, record_id, STATUS_SUCCESS, rows_processed=rows_processed)


def record_failure(db_path: Path, record_id: int, error: str) -> None:
    """Mark an execution as failed.

    Args:
        db_path: Path to the scheduler database.
        record_id: The execution record ID.
        error: Human-readable error description.
    """
    _finish_record(db_path, record_id, STATUS_FAILED, error=error)


def record_timeout(db_path: Path, record_id: int) -> None:
    """Mark an execution as timed out.

    Args:
        db_path: Path to the scheduler database.
        record_id: The execution record ID.
    """
    _finish_record(db_path, record_id, STATUS_TIMEOUT)


def record_cancellation(db_path: Path, record_id: int) -> None:
    """Mark an execution as cancelled.

    Args:
        db_path: Path to the scheduler database.
        record_id: The execution record ID.
    """
    _finish_record(db_path, record_id, STATUS_CANCELLED)


# ---------------------------------------------------------------------------
# Queries (read)
# ---------------------------------------------------------------------------


def get_execution_record(
    db_path: Path,
    record_id: int,
) -> dict[str, Any] | None:
    """Get a single execution record by its ID.

    Used by the ``GET /api/sync/status/{record_id}`` polling endpoint.

    Args:
        db_path: Path to the scheduler database.
        record_id: The execution record ID to look up.

    Returns:
        Record dict or ``None`` if no record with that ID exists.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM execution_history WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def get_schedule_history(
    db_path: Path,
    schedule_name: str,
    *,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Get paginated execution history for a schedule.

    Args:
        db_path: Path to the scheduler database.
        schedule_name: Filter by schedule name.
        status: Optional status filter.
        since: Optional ISO 8601 lower bound for ``started_at``.
        until: Optional ISO 8601 upper bound for ``started_at``.
        limit: Maximum records to return.
        offset: Number of records to skip.

    Returns:
        Tuple of (list of record dicts, total matching count).
    """
    conditions = ["schedule_name = ?"]
    params: list[Any] = [schedule_name]

    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if since is not None:
        conditions.append("started_at >= ?")
        params.append(since)
    if until is not None:
        conditions.append("started_at <= ?")
        params.append(until)

    where = " AND ".join(conditions)

    conn = _connect(db_path)
    try:
        count_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM execution_history WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
        total: int = count_row["cnt"] if count_row else 0

        rows = conn.execute(
            f"SELECT * FROM execution_history WHERE {where} "  # noqa: S608
            "ORDER BY started_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

        return [_row_to_dict(r) for r in rows], total
    finally:
        conn.close()


def get_recent_history(
    db_path: Path,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get the most recent execution records across all schedules.

    Args:
        db_path: Path to the scheduler database.
        limit: Maximum records to return.

    Returns:
        List of record dicts ordered by ``started_at`` descending.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM execution_history ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_average_duration(
    db_path: Path,
    schedule_name: str,
    *,
    window_days: int = 7,
) -> float | None:
    """Compute average duration of successful runs within a time window.

    Only considers records with ``status='success'`` and a non-null
    ``duration_seconds``.

    Args:
        db_path: Path to the scheduler database.
        schedule_name: Schedule to compute for.
        window_days: Number of days to look back.

    Returns:
        Average duration in seconds, or ``None`` if no qualifying records.
    """
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=window_days)).isoformat()

    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT AVG(duration_seconds) as avg_duration
            FROM execution_history
            WHERE schedule_name = ?
              AND status = ?
              AND duration_seconds IS NOT NULL
              AND started_at >= ?
            """,
            (schedule_name, STATUS_SUCCESS, cutoff),
        ).fetchone()
        if row is None or row["avg_duration"] is None:
            return None
        result: float = row["avg_duration"]
        return result
    finally:
        conn.close()


def get_last_run(
    db_path: Path,
    schedule_name: str,
) -> dict[str, Any] | None:
    """Get the most recent execution record for a schedule.

    Args:
        db_path: Path to the scheduler database.
        schedule_name: Schedule name to look up.

    Returns:
        Record dict or ``None`` if no history exists.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM execution_history
            WHERE schedule_name = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (schedule_name,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_old_records(
    db_path: Path,
    retention_days: int = 30,
) -> int:
    """Delete old records and mark stale running records as failed.

    Args:
        db_path: Path to the scheduler database.
        retention_days: Delete completed records older than this many days.

    Returns:
        Number of records deleted.
    """
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=retention_days)).isoformat()
    stale_cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(hours=_STALE_RUNNING_HOURS)
    ).isoformat()

    conn = _connect(db_path)
    try:
        # Mark stale running records as failed
        conn.execute(
            """
            UPDATE execution_history
            SET status = ?, error = 'Marked as failed: stale running record',
                ended_at = ?
            WHERE status = ? AND started_at < ?
            """,
            (STATUS_FAILED, _utcnow_iso(), STATUS_RUNNING, stale_cutoff),
        )

        # Delete old completed records
        cursor = conn.execute(
            """
            DELETE FROM execution_history
            WHERE started_at < ? AND status != ?
            """,
            (cutoff, STATUS_RUNNING),
        )
        deleted: int = cursor.rowcount
        conn.commit()

        if deleted > 0:
            logger.info("execution_history_cleanup", deleted=deleted)

        return deleted
    finally:
        conn.close()


def cleanup_history_job(project_root: str) -> None:
    """Module-level cleanup wrapper for APScheduler pickle serialization.

    APScheduler 3.x requires module-level functions for job persistence.

    Args:
        project_root: Dango project root as a string (APScheduler serializes args).
    """
    db_path = get_scheduler_db_path(project_root)
    if db_path.exists():
        cleanup_old_records(db_path)
