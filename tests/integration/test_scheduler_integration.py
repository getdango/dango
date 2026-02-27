"""tests/integration/test_scheduler_integration.py

Integration test: schedule registration -> job fire -> history recorded.
Uses real SQLite via migration loading (importlib.util), not mocks.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


def _apply_migration(db_path: Path) -> None:
    """Apply the 001_execution_history migration to create the table."""
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "dango"
        / "migrations"
        / "scheduler"
        / "001_execution_history.py"
    )
    spec = importlib.util.spec_from_file_location("migration_001", str(migration_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        mod.upgrade(conn)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.integration
class TestScheduleToHistoryIntegration:
    """Full path: record_start → record_completion/timeout/cancellation."""

    def test_record_start_then_completion(self, tmp_path):
        """record_start creates a row, record_completion finalizes it."""
        from dango.platform.scheduling.history import (
            get_scheduler_db_path,
            record_completion,
            record_start,
        )

        db_path = get_scheduler_db_path(tmp_path)
        _apply_migration(db_path)

        record_id = record_start(db_path, "daily_sync", sources=["google_sheets", "stripe"])
        assert isinstance(record_id, int)

        record_completion(db_path, record_id, rows_processed=1500)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM execution_history WHERE id = ?", (record_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["status"] == "success"
        assert row["rows_processed"] == 1500
        assert row["duration_seconds"] is not None
        assert row["ended_at"] is not None

    def test_record_start_then_timeout(self, tmp_path):
        """record_timeout marks the execution with 'timeout' status."""
        from dango.platform.scheduling.history import (
            get_scheduler_db_path,
            record_start,
            record_timeout,
        )

        db_path = get_scheduler_db_path(tmp_path)
        _apply_migration(db_path)

        record_id = record_start(db_path, "hourly_sync", sources=["src1"])
        record_timeout(db_path, record_id)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM execution_history WHERE id = ?", (record_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row["status"] == "timeout"
        assert row["ended_at"] is not None

    def test_record_start_then_cancellation(self, tmp_path):
        """record_cancellation marks the execution with 'cancelled' status."""
        from dango.platform.scheduling.history import (
            get_scheduler_db_path,
            record_cancellation,
            record_start,
        )

        db_path = get_scheduler_db_path(tmp_path)
        _apply_migration(db_path)

        record_id = record_start(db_path, "weekly_sync", sources=["hubspot"])
        record_cancellation(db_path, record_id)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM execution_history WHERE id = ?", (record_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row["status"] == "cancelled"
        assert row["ended_at"] is not None
