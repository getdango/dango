"""tests/unit/test_execution_history.py

Tests for dango.platform.scheduling.history — execution history tracking
for scheduled jobs.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dango.platform.scheduling.history import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    cleanup_old_records,
    get_average_duration,
    get_last_run,
    get_recent_history,
    get_schedule_history,
    record_cancellation,
    record_completion,
    record_failure,
    record_start,
    record_timeout,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_migration():
    """Load the migration module (filename starts with digit, can't import directly)."""
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "dango"
        / "migrations"
        / "scheduler"
        / "001_execution_history.py"
    )
    spec = importlib.util.spec_from_file_location("migration_001", migration_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create_db(tmp_path: Path) -> Path:
    """Create a scheduler DB with the execution_history table."""
    migration = _load_migration()
    db_path = tmp_path / "scheduler.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    migration.upgrade(conn)
    conn.commit()
    conn.close()
    return db_path


def _insert_record(
    db_path: Path,
    schedule_name: str = "daily-sync",
    status: str = STATUS_SUCCESS,
    started_at: str | None = None,
    duration: float | None = 10.0,
    error: str | None = None,
    rows_processed: int | None = None,
) -> int:
    """Insert a record directly for test setup."""
    if started_at is None:
        started_at = datetime.now(tz=timezone.utc).isoformat()
    ended_at = datetime.now(tz=timezone.utc).isoformat() if status != STATUS_RUNNING else None

    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        """
        INSERT INTO execution_history
        (schedule_name, started_at, ended_at, status, duration_seconds, error, rows_processed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (schedule_name, started_at, ended_at, status, duration, error, rows_processed),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMigration:
    """Test the scheduler migration creates the correct schema."""

    def test_creates_execution_history_table(self, tmp_path):
        db_path = _create_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_history'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_creates_indexes(self, tmp_path):
        db_path = _create_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_exec_%'"
        ).fetchall()
        index_names = {r[0] for r in rows}
        conn.close()
        assert index_names == {"idx_exec_schedule_name", "idx_exec_started_at", "idx_exec_status"}


# ---------------------------------------------------------------------------
# Record lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecordLifecycle:
    """Test the write operations for execution records."""

    def test_record_start_creates_running_record(self, tmp_path):
        db_path = _create_db(tmp_path)
        record_id = record_start(db_path, "daily-sync", sources=["shopify", "stripe"])
        assert isinstance(record_id, int)
        assert record_id > 0

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM execution_history WHERE id = ?", (record_id,)).fetchone()
        conn.close()

        assert row["status"] == STATUS_RUNNING
        assert row["schedule_name"] == "daily-sync"
        assert '"shopify"' in row["sources"]

    def test_record_completion_sets_success(self, tmp_path):
        db_path = _create_db(tmp_path)
        record_id = record_start(db_path, "daily-sync")
        record_completion(db_path, record_id, rows_processed=100)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM execution_history WHERE id = ?", (record_id,)).fetchone()
        conn.close()

        assert row["status"] == STATUS_SUCCESS
        assert row["ended_at"] is not None
        assert row["duration_seconds"] is not None
        assert row["duration_seconds"] >= 0
        assert row["rows_processed"] == 100

    def test_record_failure_sets_error(self, tmp_path):
        db_path = _create_db(tmp_path)
        record_id = record_start(db_path, "daily-sync")
        record_failure(db_path, record_id, "Connection refused")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM execution_history WHERE id = ?", (record_id,)).fetchone()
        conn.close()

        assert row["status"] == STATUS_FAILED
        assert row["error"] == "Connection refused"
        assert row["duration_seconds"] is not None

    def test_record_timeout(self, tmp_path):
        db_path = _create_db(tmp_path)
        record_id = record_start(db_path, "daily-sync")
        record_timeout(db_path, record_id)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM execution_history WHERE id = ?", (record_id,)).fetchone()
        conn.close()

        assert row["status"] == STATUS_TIMEOUT

    def test_record_cancellation(self, tmp_path):
        db_path = _create_db(tmp_path)
        record_id = record_start(db_path, "daily-sync")
        record_cancellation(db_path, record_id)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM execution_history WHERE id = ?", (record_id,)).fetchone()
        conn.close()

        assert row["status"] == STATUS_CANCELLED

    def test_duration_computed_correctly(self, tmp_path):
        db_path = _create_db(tmp_path)
        record_id = record_start(db_path, "daily-sync")
        # Small sleep so duration > 0
        time.sleep(0.05)
        record_completion(db_path, record_id)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT duration_seconds FROM execution_history WHERE id = ?", (record_id,)
        ).fetchone()
        conn.close()

        assert row["duration_seconds"] > 0

    def test_sources_stored_as_json(self, tmp_path):
        db_path = _create_db(tmp_path)
        record_id = record_start(db_path, "multi-source", sources=["src_a", "src_b"])

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sources FROM execution_history WHERE id = ?", (record_id,)
        ).fetchone()
        conn.close()

        import json

        parsed = json.loads(row["sources"])
        assert parsed == ["src_a", "src_b"]


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHistoryQueries:
    """Test read operations for execution history."""

    def test_pagination(self, tmp_path):
        db_path = _create_db(tmp_path)
        for _ in range(5):
            _insert_record(db_path, schedule_name="test-sched")

        items, total = get_schedule_history(db_path, "test-sched", limit=2, offset=0)
        assert total == 5
        assert len(items) == 2

        items2, total2 = get_schedule_history(db_path, "test-sched", limit=2, offset=2)
        assert total2 == 5
        assert len(items2) == 2

    def test_status_filter(self, tmp_path):
        db_path = _create_db(tmp_path)
        _insert_record(db_path, status=STATUS_SUCCESS)
        _insert_record(db_path, status=STATUS_FAILED, error="err")
        _insert_record(db_path, status=STATUS_SUCCESS)

        items, total = get_schedule_history(db_path, "daily-sync", status=STATUS_FAILED)
        assert total == 1
        assert items[0]["status"] == STATUS_FAILED

    def test_date_filter(self, tmp_path):
        db_path = _create_db(tmp_path)
        old_time = (datetime.now(tz=timezone.utc) - timedelta(days=10)).isoformat()
        recent_time = datetime.now(tz=timezone.utc).isoformat()

        _insert_record(db_path, started_at=old_time)
        _insert_record(db_path, started_at=recent_time)

        since = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
        items, total = get_schedule_history(db_path, "daily-sync", since=since)
        assert total == 1

    def test_recent_history(self, tmp_path):
        db_path = _create_db(tmp_path)
        _insert_record(db_path, schedule_name="sched-a")
        _insert_record(db_path, schedule_name="sched-b")
        _insert_record(db_path, schedule_name="sched-a")

        results = get_recent_history(db_path, limit=10)
        assert len(results) == 3

    def test_last_run(self, tmp_path):
        db_path = _create_db(tmp_path)
        _insert_record(db_path, schedule_name="my-sched")
        _insert_record(db_path, schedule_name="my-sched")

        result = get_last_run(db_path, "my-sched")
        assert result is not None
        assert result["schedule_name"] == "my-sched"

    def test_last_run_nonexistent(self, tmp_path):
        db_path = _create_db(tmp_path)
        result = get_last_run(db_path, "nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Average duration tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAverageDuration:
    """Test average duration computation."""

    def test_average_of_successful_runs(self, tmp_path):
        db_path = _create_db(tmp_path)
        _insert_record(db_path, duration=10.0)
        _insert_record(db_path, duration=20.0)

        avg = get_average_duration(db_path, "daily-sync")
        assert avg == pytest.approx(15.0)

    def test_no_runs_returns_none(self, tmp_path):
        db_path = _create_db(tmp_path)
        avg = get_average_duration(db_path, "daily-sync")
        assert avg is None

    def test_excludes_failed_runs(self, tmp_path):
        db_path = _create_db(tmp_path)
        _insert_record(db_path, duration=10.0, status=STATUS_SUCCESS)
        _insert_record(db_path, duration=100.0, status=STATUS_FAILED, error="err")

        avg = get_average_duration(db_path, "daily-sync")
        assert avg == pytest.approx(10.0)

    def test_window_days_filter(self, tmp_path):
        db_path = _create_db(tmp_path)
        old_time = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
        recent_time = datetime.now(tz=timezone.utc).isoformat()

        _insert_record(db_path, started_at=old_time, duration=100.0)
        _insert_record(db_path, started_at=recent_time, duration=10.0)

        avg = get_average_duration(db_path, "daily-sync", window_days=7)
        assert avg == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCleanup:
    """Test execution history cleanup."""

    def test_deletes_old_records(self, tmp_path):
        db_path = _create_db(tmp_path)
        old_time = (datetime.now(tz=timezone.utc) - timedelta(days=60)).isoformat()
        _insert_record(db_path, started_at=old_time)
        _insert_record(db_path)  # recent

        deleted = cleanup_old_records(db_path, retention_days=30)
        assert deleted == 1

        items = get_recent_history(db_path, limit=100)
        assert len(items) == 1

    def test_preserves_recent_records(self, tmp_path):
        db_path = _create_db(tmp_path)
        _insert_record(db_path)
        _insert_record(db_path)

        deleted = cleanup_old_records(db_path, retention_days=30)
        assert deleted == 0

    def test_marks_stale_running_as_failed(self, tmp_path):
        db_path = _create_db(tmp_path)
        old_time = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).isoformat()
        _insert_record(db_path, started_at=old_time, status=STATUS_RUNNING, duration=None)

        cleanup_old_records(db_path, retention_days=30)

        # Stale running record should be marked as failed (not deleted, since
        # the cutoff for deletion is 30 days)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM execution_history").fetchone()
        conn.close()

        assert row["status"] == STATUS_FAILED
        assert "stale" in row["error"].lower()

    def test_returns_count(self, tmp_path):
        db_path = _create_db(tmp_path)
        old_time = (datetime.now(tz=timezone.utc) - timedelta(days=60)).isoformat()
        _insert_record(db_path, started_at=old_time)
        _insert_record(db_path, started_at=old_time)

        deleted = cleanup_old_records(db_path, retention_days=30)
        assert deleted == 2
