"""tests/integration/test_post_sync_chain_integration.py

Integration tests for the full post-sync hook chain with real DuckDB + SQLite.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from dango.utils.dango_db import _schema_initialized, connect
from dango.utils.post_sync import dispatch_post_sync_hooks


def _clear_schema_cache() -> None:
    """Clear the dango_db schema initialization cache for test isolation."""
    _schema_initialized.clear()


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
            status VARCHAR,
            email VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO raw_testshop.orders VALUES
        (1, 10.50, 'succeeded', 'alice@example.com'),
        (2, 25.00, 'succeeded', 'bob@example.com'),
        (3, 5.00, 'failed', 'charlie@example.com')
    """)
    conn.close()
    return db_path


@pytest.mark.integration
class TestPostSyncChainIntegration:
    """Integration tests for the full 4-hook post-sync chain."""

    def test_profiling_stores_stats(self, tmp_path: Path) -> None:
        """dispatch_post_sync_hooks stores profiling_stats rows for testshop."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        # Mock PII analyzer to avoid spaCy dependency
        with patch("dango.governance.pii_detector._get_analyzer", return_value=None):
            dispatch_post_sync_hooks(tmp_path, ["testshop"])

        with connect(tmp_path) as sqlite_conn:
            rows = sqlite_conn.execute(
                "SELECT source, table_name, column_name FROM profiling_stats "
                "WHERE source = 'testshop'"
            ).fetchall()

        assert len(rows) > 0
        sources = {r["source"] for r in rows}
        assert "testshop" in sources

    def test_drift_baseline_created(self, tmp_path: Path) -> None:
        """First dispatch creates schema_baselines with no drift_events."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        with patch("dango.governance.pii_detector._get_analyzer", return_value=None):
            dispatch_post_sync_hooks(tmp_path, ["testshop"])

        with connect(tmp_path) as sqlite_conn:
            baselines = sqlite_conn.execute(
                "SELECT source FROM schema_baselines WHERE source = 'testshop'"
            ).fetchall()
            drift_events = sqlite_conn.execute(
                "SELECT source FROM drift_events WHERE source = 'testshop'"
            ).fetchall()

        assert len(baselines) > 0
        assert len(drift_events) == 0  # First run = baseline only, no drift

    def test_second_dispatch_detects_drift(self, tmp_path: Path) -> None:
        """ALTER TABLE between dispatches produces drift_events."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        with patch("dango.governance.pii_detector._get_analyzer", return_value=None):
            # First run — establishes baseline
            dispatch_post_sync_hooks(tmp_path, ["testshop"])

        # ALTER TABLE — add a column
        conn = duckdb.connect(str(db_path))
        conn.execute("ALTER TABLE raw_testshop.orders ADD COLUMN notes VARCHAR")
        conn.close()

        with patch("dango.governance.pii_detector._get_analyzer", return_value=None):
            # Second run — should detect drift
            dispatch_post_sync_hooks(tmp_path, ["testshop"])

        with connect(tmp_path) as sqlite_conn:
            drift_events = sqlite_conn.execute(
                "SELECT source, table_name, event_type FROM drift_events WHERE source = 'testshop'"
            ).fetchall()

        assert len(drift_events) > 0
        event_types = {r["event_type"] for r in drift_events}
        assert "column_added" in event_types

    def test_hook_failure_does_not_block_chain(self, tmp_path: Path) -> None:
        """Patch drift engine to raise — profiling still ran, analysis still ran."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        with (
            patch(
                "dango.governance.schema_drift.detect_drift_for_sources",
                side_effect=RuntimeError("drift engine boom"),
            ),
            patch("dango.governance.pii_detector._get_analyzer", return_value=None),
            patch("dango.analysis.metrics.run_analysis", return_value=[]) as mock_analysis,
        ):
            dispatch_post_sync_hooks(tmp_path, ["testshop"])

        # Profiling ran before drift (check profiling_stats)
        with connect(tmp_path) as sqlite_conn:
            rows = sqlite_conn.execute(
                "SELECT source FROM profiling_stats WHERE source = 'testshop'"
            ).fetchall()

        assert len(rows) > 0  # Profiling succeeded despite drift failure

        # Analysis ran after drift
        mock_analysis.assert_called_once()
