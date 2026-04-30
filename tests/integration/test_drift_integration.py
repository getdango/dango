"""tests/integration/test_drift_integration.py

Integration tests for schema drift detection using real DuckDB and SQLite.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dango.governance.schema_drift import (
    detect_drift_for_sources,
    detect_table_drift,
    get_drift_history,
)
from dango.utils.dango_db import _schema_initialized, connect


def _create_test_warehouse(tmp_path: Path) -> Path:
    """Create a real DuckDB warehouse with test data.

    Returns:
        Path to the DuckDB file.
    """
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA raw_testshop")
    conn.execute("""
        CREATE TABLE raw_testshop.orders (
            id INTEGER NOT NULL,
            total DOUBLE,
            email VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO raw_testshop.orders VALUES
        (1, 10.50, 'alice@example.com'),
        (2, 25.00, 'bob@example.com')
    """)

    # dlt internal table (should be excluded)
    conn.execute("""
        CREATE TABLE raw_testshop._dlt_loads (
            load_id VARCHAR,
            status INTEGER
        )
    """)

    conn.close()
    return db_path


def _clear_schema_cache() -> None:
    """Clear the dango_db schema initialization cache for test isolation."""
    _schema_initialized.clear()


@pytest.mark.integration
class TestDriftDetectionIntegration:
    """Integration tests for drift detection with real databases."""

    def test_first_sync_creates_baseline(self, tmp_path: Path) -> None:
        """First sync stores baseline silently, no events."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        events = detect_table_drift(tmp_path, "testshop", "orders")
        assert events == []

        # Verify baseline was stored
        with connect(tmp_path) as conn:
            rows = conn.execute(
                "SELECT column_name, column_type FROM schema_baselines "
                "WHERE source = 'testshop' AND table_name = 'orders' "
                "ORDER BY column_name"
            ).fetchall()

        assert len(rows) == 3
        columns = {row[0]: row[1] for row in rows}
        assert "id" in columns
        assert "total" in columns
        assert "email" in columns

    def test_column_added_produces_event(self, tmp_path: Path) -> None:
        """Adding a column produces a column_added event."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        # First sync — establish baseline
        detect_table_drift(tmp_path, "testshop", "orders")

        # Add a column
        conn = duckdb.connect(str(db_path))
        conn.execute("ALTER TABLE raw_testshop.orders ADD COLUMN status VARCHAR")
        conn.close()

        # Second sync — detect drift
        events = detect_table_drift(tmp_path, "testshop", "orders")

        assert len(events) == 1
        assert events[0]["event_type"] == "column_added"
        assert events[0]["column_name"] == "status"

    def test_column_removed_produces_event(self, tmp_path: Path) -> None:
        """Removing a column produces a column_removed event."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        # First sync — establish baseline
        detect_table_drift(tmp_path, "testshop", "orders")

        # Remove a column (DuckDB supports ALTER TABLE DROP COLUMN)
        conn = duckdb.connect(str(db_path))
        conn.execute("ALTER TABLE raw_testshop.orders DROP COLUMN email")
        conn.close()

        # Second sync — detect drift
        events = detect_table_drift(tmp_path, "testshop", "orders")

        assert len(events) == 1
        assert events[0]["event_type"] == "column_removed"
        assert events[0]["column_name"] == "email"

    def test_type_changed_produces_event(self, tmp_path: Path) -> None:
        """Changing a column type produces a type_changed event."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        # First sync — establish baseline
        detect_table_drift(tmp_path, "testshop", "orders")

        # Change column type by recreating the table
        conn = duckdb.connect(str(db_path))
        conn.execute("ALTER TABLE raw_testshop.orders ALTER COLUMN total TYPE VARCHAR")
        conn.close()

        # Second sync — detect drift
        events = detect_table_drift(tmp_path, "testshop", "orders")

        assert len(events) == 1
        assert events[0]["event_type"] == "type_changed"
        assert events[0]["column_name"] == "total"

    def test_no_drift_no_events(self, tmp_path: Path) -> None:
        """No schema changes means no events on second sync."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        # First sync
        detect_table_drift(tmp_path, "testshop", "orders")

        # Second sync — same schema
        events = detect_table_drift(tmp_path, "testshop", "orders")
        assert events == []

    def test_baseline_reflects_current_after_drift(self, tmp_path: Path) -> None:
        """Baseline is updated to current schema after drift is detected."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        # First sync
        detect_table_drift(tmp_path, "testshop", "orders")

        # Add a column
        conn = duckdb.connect(str(db_path))
        conn.execute("ALTER TABLE raw_testshop.orders ADD COLUMN status VARCHAR")
        conn.close()

        # Second sync — drift detected
        events = detect_table_drift(tmp_path, "testshop", "orders")
        assert len(events) == 1

        # Third sync — no drift (baseline was updated)
        events = detect_table_drift(tmp_path, "testshop", "orders")
        assert events == []


@pytest.mark.integration
class TestHookBoundaryIntegration:
    """Integration tests for the full hook boundary path."""

    def test_drift_detection_direct(self, tmp_path: Path) -> None:
        """detect_drift_for_sources detects drift (now called pre-dbt, not from post_sync)."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        # First call — establishes baseline
        detect_drift_for_sources(tmp_path, ["testshop"])

        # Add a column
        conn = duckdb.connect(str(db_path))
        conn.execute("ALTER TABLE raw_testshop.orders ADD COLUMN status VARCHAR")
        conn.close()

        # Second call — detects drift
        detect_drift_for_sources(tmp_path, ["testshop"])

        # Verify drift events were recorded
        with connect(tmp_path) as sqlite_conn:
            count = sqlite_conn.execute(
                "SELECT COUNT(*) FROM drift_events "
                "WHERE source = 'testshop' AND table_name = 'orders'"
            ).fetchone()[0]

        assert count >= 1


@pytest.mark.integration
class TestDriftHistoryQuery:
    """Integration tests for drift history queries."""

    def test_real_data_query_with_filters(self, tmp_path: Path) -> None:
        """Query drift events with source and table filters using real data."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        # Establish baseline
        detect_drift_for_sources(tmp_path, ["testshop"])

        # Add a column to trigger drift
        conn = duckdb.connect(str(db_path))
        conn.execute("ALTER TABLE raw_testshop.orders ADD COLUMN status VARCHAR")
        conn.close()

        # Detect drift
        detect_drift_for_sources(tmp_path, ["testshop"])

        # Query all
        all_events = get_drift_history(tmp_path)
        assert len(all_events) >= 1

        # Query with source filter
        filtered = get_drift_history(tmp_path, source="testshop")
        assert len(filtered) >= 1

        # Query with table filter
        table_filtered = get_drift_history(tmp_path, source="testshop", table_name="orders")
        assert len(table_filtered) >= 1

        # Query with limit
        limited = get_drift_history(tmp_path, limit=1)
        assert len(limited) == 1

        # Newest first
        assert limited[0]["id"] == all_events[0]["id"]
