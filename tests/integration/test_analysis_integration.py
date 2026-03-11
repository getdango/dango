"""tests/integration/test_analysis_integration.py

Integration tests for the analysis hook boundary using real DuckDB and SQLite.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dango.analysis.config import save_metrics_config
from dango.analysis.models import ComparisonType, MetricConfig, MetricsConfig
from dango.utils.dango_db import _schema_initialized, connect
from dango.utils.post_sync import dispatch_post_sync_hooks


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
            status VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO raw_testshop.orders VALUES
        (1, 10.50, 'succeeded'),
        (2, 25.00, 'succeeded'),
        (3, 5.00, 'failed')
    """)
    conn.close()
    return db_path


def _clear_schema_cache() -> None:
    """Clear the dango_db schema initialization cache for test isolation."""
    _schema_initialized.clear()


@pytest.mark.integration
class TestAnalysisHookIntegration:
    """Integration tests for the full analysis hook boundary path."""

    def test_dispatch_to_analysis(self, tmp_path: Path) -> None:
        """dispatch_post_sync_hooks -> _run_analysis -> run_analysis -> SQLite."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        # Create metrics config
        config = MetricsConfig(
            enabled=True,
            metrics=[
                MetricConfig(
                    name="testshop_order_total",
                    source_table="raw_testshop.orders",
                    value_expression="SUM(total)",
                    filter="status = 'succeeded'",
                    compare=ComparisonType.week_over_week,
                    warn_threshold=20.0,
                ),
            ],
        )
        save_metrics_config(tmp_path, config)

        # Run via dispatcher (full hook boundary)
        dispatch_post_sync_hooks(tmp_path, ["testshop"])

        # Verify metric was stored in SQLite
        with connect(tmp_path) as sqlite_conn:
            rows = sqlite_conn.execute(
                "SELECT metric_name, metric_value FROM metric_history "
                "WHERE metric_name = 'testshop_order_total'"
            ).fetchall()

        assert len(rows) >= 1
        assert rows[0][1] == pytest.approx(35.5)  # 10.50 + 25.00
