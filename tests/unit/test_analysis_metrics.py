"""tests/unit/test_analysis_metrics.py

Tests for the metric engine (dango/analysis/metrics.py).

Mocks ``duckdb.connect`` at ``dango.analysis.metrics.duckdb`` for DuckDB queries
and uses real SQLite via ``dango.utils.dango_db.connect()`` for storage.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.analysis.metrics import (
    _build_metric_sql,
    _parse_source_from_table,
    _store_comparison_result,
    _store_metric_value,
    run_analysis,
)
from dango.analysis.models import (
    ComparisonResult,
    ComparisonType,
    MetricConfig,
    MetricValue,
)
from dango.utils.dango_db import connect


@pytest.mark.unit
class TestParseSourceFromTable:
    """_parse_source_from_table splits schema.table."""

    def test_normal(self):
        """Schema-qualified name splits correctly."""
        source, table = _parse_source_from_table("raw_stripe.payments")
        assert source == "raw_stripe"
        assert table == "payments"

    def test_no_dot(self):
        """No dot returns (None, None)."""
        source, table = _parse_source_from_table("payments")
        assert source is None
        assert table is None

    def test_multiple_dots(self):
        """Multiple dots split on first dot only."""
        source, table = _parse_source_from_table("raw.stripe.payments")
        assert source == "raw"
        assert table == "stripe.payments"


@pytest.mark.unit
class TestBuildMetricSql:
    """_build_metric_sql constructs SQL."""

    def test_without_filter(self):
        """SQL without WHERE clause."""
        mc = MetricConfig(name="m", source_table="s.t", value_expression="COUNT(*)")
        sql = _build_metric_sql(mc)
        assert sql == "SELECT COUNT(*) FROM s.t"

    def test_with_filter(self):
        """SQL with WHERE clause."""
        mc = MetricConfig(
            name="m",
            source_table="s.t",
            value_expression="SUM(amount)",
            filter="status = 'active'",
        )
        sql = _build_metric_sql(mc)
        assert sql == "SELECT SUM(amount) FROM s.t WHERE status = 'active'"


@pytest.mark.unit
class TestStoreMetricValue:
    """_store_metric_value inserts into metric_history."""

    def test_stores_value(self, tmp_path):
        """Value is stored in metric_history table."""
        mv = MetricValue(
            metric_name="revenue",
            source="raw_stripe",
            table_name="payments",
            value=42.5,
        )
        _store_metric_value(tmp_path, mv)

        with connect(tmp_path) as conn:
            row = conn.execute(
                "SELECT * FROM metric_history WHERE metric_name = 'revenue'"
            ).fetchone()
        assert row is not None
        assert row["metric_value"] == 42.5

    def test_skips_none_value(self, tmp_path):
        """None value is not stored."""
        mv = MetricValue(metric_name="broken", value=None, error="fail")
        _store_metric_value(tmp_path, mv)

        with connect(tmp_path) as conn:
            rows = conn.execute("SELECT * FROM metric_history").fetchall()
        assert len(rows) == 0


@pytest.mark.unit
class TestStoreComparisonResult:
    """_store_comparison_result inserts into metric_results."""

    def test_stores_result(self, tmp_path):
        """Comparison result is stored in metric_results table."""
        cr = ComparisonResult(
            metric_name="revenue",
            comparison_type=ComparisonType.week_over_week,
            current_value=110.0,
            baseline_value=100.0,
            change_pct=10.0,
        )
        _store_comparison_result(tmp_path, cr)

        with connect(tmp_path) as conn:
            row = conn.execute(
                "SELECT * FROM metric_results WHERE metric_name = 'revenue'"
            ).fetchone()
        assert row is not None
        assert row["result_type"] == "week_over_week"
        assert "110.0" in row["result_value"]


@pytest.mark.unit
class TestRunAnalysis:
    """run_analysis orchestration."""

    def _write_config(self, tmp_path, yaml_content):
        """Write monitors.yml config."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(exist_ok=True)
        (dango_dir / "monitors.yml").write_text(yaml_content)

    def _create_warehouse(self, tmp_path):
        """Create a minimal DuckDB warehouse file."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        return data_dir / "warehouse.duckdb"

    def test_empty_config_returns_empty(self, tmp_path):
        """No config file returns empty results."""
        results = run_analysis(tmp_path)
        assert results == []

    def test_disabled_config_returns_empty(self, tmp_path):
        """Disabled config returns empty results."""
        self._write_config(tmp_path, "enabled: false\nmetrics: []\n")
        results = run_analysis(tmp_path)
        assert results == []

    def test_no_warehouse_returns_empty(self, tmp_path):
        """Missing DuckDB warehouse returns empty results."""
        self._write_config(
            tmp_path,
            """\
metrics:
  - name: m1
    source_table: s.t
    value_expression: "COUNT(*)"
""",
        )
        results = run_analysis(tmp_path)
        assert results == []

    @patch("dango.analysis.metrics.duckdb")
    def test_runs_metrics(self, mock_duckdb, tmp_path):
        """Runs metrics and returns AnalysisResult objects."""
        self._write_config(
            tmp_path,
            """\
metrics:
  - name: revenue
    source_table: raw_stripe.payments
    value_expression: "SUM(amount)"
""",
        )
        warehouse_path = self._create_warehouse(tmp_path)
        warehouse_path.touch()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (42.5,)
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        results = run_analysis(tmp_path)

        assert len(results) == 1
        assert results[0].metric.metric_name == "revenue"
        assert results[0].metric.value == 42.5
        assert results[0].comparison is not None

    @patch("dango.analysis.metrics.duckdb")
    def test_handles_query_error(self, mock_duckdb, tmp_path):
        """DuckDB query error is caught and stored in MetricValue.error."""
        self._write_config(
            tmp_path,
            """\
metrics:
  - name: bad_metric
    source_table: raw.missing_table
    value_expression: "COUNT(*)"
""",
        )
        warehouse_path = self._create_warehouse(tmp_path)
        warehouse_path.touch()

        mock_duckdb.Error = Exception
        mock_duckdb.connect.return_value.execute.side_effect = Exception("Table not found")

        results = run_analysis(tmp_path)

        assert len(results) == 1
        assert results[0].metric.error == "Table not found"
        assert results[0].comparison is None

    @patch("dango.analysis.metrics.duckdb")
    def test_source_filter(self, mock_duckdb, tmp_path):
        """source_filter limits which metrics are executed."""
        self._write_config(
            tmp_path,
            """\
metrics:
  - name: m1
    source_table: raw_stripe.payments
    value_expression: "SUM(amount)"
  - name: m2
    source_table: raw_app.users
    value_expression: "COUNT(*)"
""",
        )
        warehouse_path = self._create_warehouse(tmp_path)
        warehouse_path.touch()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (10.0,)
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        results = run_analysis(tmp_path, source_filter=["raw_stripe"])

        assert len(results) == 1
        assert results[0].metric.metric_name == "m1"

    @patch("dango.analysis.metrics.duckdb")
    def test_null_result_from_duckdb(self, mock_duckdb, tmp_path):
        """DuckDB returning NULL is stored as None value."""
        self._write_config(
            tmp_path,
            """\
metrics:
  - name: empty_metric
    source_table: raw.t
    value_expression: "SUM(amount)"
""",
        )
        warehouse_path = self._create_warehouse(tmp_path)
        warehouse_path.touch()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (None,)
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        results = run_analysis(tmp_path)

        assert len(results) == 1
        assert results[0].metric.value is None
        assert results[0].metric.error is None

    @patch("dango.analysis.metrics.duckdb")
    def test_compare_none_skips_comparison(self, mock_duckdb, tmp_path):
        """compare: none skips compute_comparison (freshness metrics)."""
        self._write_config(
            tmp_path,
            """\
metrics:
  - name: freshness
    source_table: raw_stripe.payments
    value_expression: "MAX(_dlt_load_id)"
    compare: none
""",
        )
        warehouse_path = self._create_warehouse(tmp_path)
        warehouse_path.touch()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (123456,)
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        with patch("dango.analysis.metrics.compute_comparison") as mock_compare:
            results = run_analysis(tmp_path)

        assert len(results) == 1
        assert results[0].metric.value == 123456
        assert results[0].comparison is None
        mock_compare.assert_not_called()
