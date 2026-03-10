"""tests/unit/test_analysis_drilldown.py

Tests for dango.analysis.drilldown — drill-down engine and contributor ranking.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from dango.analysis.drilldown import (
    NULL_SENTINEL,
    _compute_contributors,
    _get_previous_snapshot,
    _query_dimension_breakdown,
    _store_snapshot,
    run_drill_down,
)
from dango.analysis.models import (
    MetricConfig,
)
from dango.utils.dango_db import connect


def _make_metric(**kwargs):
    """Create a MetricConfig with defaults."""
    defaults = {
        "name": "revenue",
        "source_table": "raw_stripe.payments",
        "value_expression": "SUM(amount)",
    }
    defaults.update(kwargs)
    return MetricConfig(**defaults)


@pytest.mark.unit
class TestQueryDimensionBreakdown:
    """_query_dimension_breakdown GROUP BY queries."""

    @patch("dango.analysis.drilldown.duckdb")
    def test_builds_group_by_sql(self, mock_duckdb, tmp_path):
        """SQL includes GROUP BY dimension with filter."""
        metric = _make_metric(filter="status = 'active'")
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("Widget", 100.0),
            ("Gadget", 50.0),
        ]
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        result = _query_dimension_breakdown(tmp_path / "warehouse.duckdb", metric, "product")

        sql_arg = mock_conn.execute.call_args[0][0]
        assert "GROUP BY product" in sql_arg
        assert "WHERE status = 'active'" in sql_arg
        assert "CAST(product AS VARCHAR)" in sql_arg
        assert result == {"Widget": 100.0, "Gadget": 50.0}

    @patch("dango.analysis.drilldown.duckdb")
    def test_null_dimension_value(self, mock_duckdb, tmp_path):
        """DuckDB returning NULL dimension value stored as None key."""
        metric = _make_metric()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            (None, 100.0),
            ("Widget", 50.0),
        ]
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        result = _query_dimension_breakdown(tmp_path / "warehouse.duckdb", metric, "region")

        assert result[None] == 100.0
        assert result["Widget"] == 50.0

    @patch("dango.analysis.drilldown.duckdb")
    def test_handles_duckdb_error(self, mock_duckdb, tmp_path):
        """DuckDB error returns empty dict."""
        metric = _make_metric()
        mock_duckdb.Error = Exception
        mock_duckdb.connect.side_effect = Exception("Connection failed")

        result = _query_dimension_breakdown(tmp_path / "warehouse.duckdb", metric, "product")

        assert result == {}

    @patch("dango.analysis.drilldown.duckdb")
    def test_empty_result(self, mock_duckdb, tmp_path):
        """Empty fetchall returns empty dict."""
        metric = _make_metric()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        result = _query_dimension_breakdown(tmp_path / "warehouse.duckdb", metric, "product")

        assert result == {}

    @patch("dango.analysis.drilldown.duckdb")
    def test_null_metric_value_defaults_to_zero(self, mock_duckdb, tmp_path):
        """NULL aggregated value (row[1]) defaults to 0.0."""
        metric = _make_metric()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("Widget", None),
            ("Gadget", 50.0),
        ]
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        result = _query_dimension_breakdown(tmp_path / "warehouse.duckdb", metric, "product")

        assert result["Widget"] == 0.0
        assert result["Gadget"] == 50.0


@pytest.mark.unit
class TestGetPreviousSnapshot:
    """_get_previous_snapshot reads from metric_results."""

    def test_returns_previous(self, tmp_path):
        """Existing snapshot is parsed and returned."""
        snapshot = json.dumps({"Widget": 100.0, "Gadget": 50.0})
        with connect(tmp_path) as conn:
            conn.execute(
                "INSERT INTO metric_results "
                "(metric_name, source, table_name, result_type, result_value, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("revenue", None, None, "drill_down:product", snapshot, "2026-01-01T00:00:00"),
            )
            conn.commit()

        result = _get_previous_snapshot(tmp_path, "revenue", "product")

        assert result == {"Widget": 100.0, "Gadget": 50.0}

    def test_returns_none_when_missing(self, tmp_path):
        """No snapshot returns None."""
        result = _get_previous_snapshot(tmp_path, "revenue", "product")
        assert result is None

    def test_returns_most_recent_snapshot(self, tmp_path):
        """Multiple snapshots returns the most recent one."""
        old_snapshot = json.dumps({"Widget": 50.0})
        new_snapshot = json.dumps({"Widget": 200.0})
        with connect(tmp_path) as conn:
            conn.execute(
                "INSERT INTO metric_results "
                "(metric_name, source, table_name, result_type, result_value, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("revenue", None, None, "drill_down:product", old_snapshot, "2026-01-01T00:00:00"),
            )
            conn.execute(
                "INSERT INTO metric_results "
                "(metric_name, source, table_name, result_type, result_value, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("revenue", None, None, "drill_down:product", new_snapshot, "2026-01-02T00:00:00"),
            )
            conn.commit()

        result = _get_previous_snapshot(tmp_path, "revenue", "product")

        assert result == {"Widget": 200.0}

    def test_null_sentinel_roundtrip(self, tmp_path):
        """NULL_SENTINEL in JSON maps back to None key."""
        snapshot = json.dumps({NULL_SENTINEL: 100.0, "Widget": 50.0})
        with connect(tmp_path) as conn:
            conn.execute(
                "INSERT INTO metric_results "
                "(metric_name, source, table_name, result_type, result_value, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("revenue", None, None, "drill_down:region", snapshot, "2026-01-01T00:00:00"),
            )
            conn.commit()

        result = _get_previous_snapshot(tmp_path, "revenue", "region")

        assert result is not None
        assert result[None] == 100.0
        assert result["Widget"] == 50.0


@pytest.mark.unit
class TestStoreSnapshot:
    """_store_snapshot persists to metric_results."""

    def test_stores_snapshot(self, tmp_path):
        """Breakdown is stored as JSON in metric_results."""
        breakdown: dict[str | None, float] = {"Widget": 100.0, None: 25.0}
        _store_snapshot(tmp_path, "revenue", "product", breakdown)

        with connect(tmp_path) as conn:
            row = conn.execute(
                "SELECT * FROM metric_results WHERE metric_name = 'revenue'"
            ).fetchone()

        assert row is not None
        assert row["result_type"] == "drill_down:product"
        parsed = json.loads(row["result_value"])
        assert parsed["Widget"] == 100.0
        assert parsed[NULL_SENTINEL] == 25.0

    @patch("dango.analysis.drilldown.connect")
    def test_resilient_on_error(self, mock_connect):
        """Storage error does not propagate."""
        mock_connect.side_effect = Exception("DB locked")

        # Should not raise
        _store_snapshot(
            MagicMock(),
            "revenue",
            "product",
            {"Widget": 100.0},
        )


@pytest.mark.unit
class TestComputeContributors:
    """_compute_contributors pure logic tests."""

    def test_top_three_by_change(self):
        """Returns top 3 groups by absolute change."""
        current: dict[str | None, float] = {
            "A": 100.0,
            "B": 200.0,
            "C": 300.0,
            "D": 400.0,
            "E": 500.0,
        }
        previous: dict[str | None, float] = {
            "A": 90.0,
            "B": 250.0,
            "C": 280.0,
            "D": 395.0,
            "E": 600.0,
        }

        result = _compute_contributors(current, previous)

        assert len(result) == 3
        # E changed by -100, B changed by -50, C changed by +20
        assert result[0].group_value == "E"
        assert result[0].change_abs == -100.0
        assert result[1].group_value == "B"
        assert result[1].change_abs == -50.0
        assert result[2].group_value == "C"
        assert result[2].change_abs == 20.0

    def test_first_run_empty(self):
        """previous=None returns empty list."""
        result = _compute_contributors({"A": 100.0}, None)
        assert result == []

    def test_new_group(self):
        """Group in current but not previous has no change_abs."""
        current: dict[str | None, float] = {"A": 100.0, "NEW": 50.0}
        previous: dict[str | None, float] = {"A": 90.0}

        result = _compute_contributors(current, previous)

        new_contrib = [c for c in result if c.group_value == "NEW"]
        assert len(new_contrib) == 1
        assert new_contrib[0].previous_value is None
        assert new_contrib[0].change_pct is None
        assert new_contrib[0].change_abs is None

    def test_disappeared_group(self):
        """Group in previous but not current gets current_value=0."""
        current: dict[str | None, float] = {"A": 100.0}
        previous: dict[str | None, float] = {"A": 90.0, "GONE": 80.0}

        result = _compute_contributors(current, previous)

        gone_contrib = [c for c in result if c.group_value == "GONE"]
        assert len(gone_contrib) == 1
        assert gone_contrib[0].current_value == 0.0
        assert gone_contrib[0].previous_value == 80.0
        assert gone_contrib[0].change_abs == -80.0

    def test_null_group_value(self):
        """None key (NULL dimension) is handled."""
        current: dict[str | None, float] = {None: 100.0, "A": 50.0}
        previous: dict[str | None, float] = {None: 80.0, "A": 50.0}

        result = _compute_contributors(current, previous)

        null_contrib = [c for c in result if c.group_value is None]
        assert len(null_contrib) == 1
        assert null_contrib[0].change_abs == 20.0

    def test_change_pct_computed(self):
        """change_pct is computed correctly."""
        current: dict[str | None, float] = {"A": 150.0}
        previous: dict[str | None, float] = {"A": 100.0}

        result = _compute_contributors(current, previous)

        assert len(result) == 1
        assert result[0].change_pct == 50.0
        assert result[0].change_abs == 50.0

    def test_zero_previous_no_pct(self):
        """Zero previous value yields None change_pct."""
        current: dict[str | None, float] = {"A": 100.0}
        previous: dict[str | None, float] = {"A": 0.0}

        result = _compute_contributors(current, previous)

        assert len(result) == 1
        assert result[0].change_pct is None
        assert result[0].change_abs == 100.0


@pytest.mark.unit
class TestRunDrillDown:
    """run_drill_down orchestration tests."""

    @patch("dango.analysis.drilldown.duckdb")
    def test_runs_all_dimensions(self, mock_duckdb, tmp_path):
        """Metric with 2 drill_down dims produces 2 DrillDownDimension results."""
        metric = _make_metric(drill_down=["product", "region"])
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("Widget", 100.0)]
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        result = run_drill_down(tmp_path / "warehouse.duckdb", tmp_path, metric)

        assert len(result) == 2
        assert result[0].dimension == "product"
        assert result[1].dimension == "region"

    @patch("dango.analysis.drilldown.duckdb")
    def test_empty_drill_down_list(self, mock_duckdb, tmp_path):
        """No dimensions configured returns empty result."""
        metric = _make_metric(drill_down=[])

        result = run_drill_down(tmp_path / "warehouse.duckdb", tmp_path, metric)

        assert result == []

    @patch("dango.analysis.drilldown.duckdb")
    def test_stores_and_compares_snapshots(self, mock_duckdb, tmp_path):
        """Second run produces contributors by comparing against stored snapshot."""
        metric = _make_metric(drill_down=["product"])
        mock_conn = MagicMock()
        mock_duckdb.connect.return_value = mock_conn
        mock_duckdb.Error = Exception

        # First run: Widget=100, Gadget=50
        mock_conn.execute.return_value.fetchall.return_value = [
            ("Widget", 100.0),
            ("Gadget", 50.0),
        ]
        result1 = run_drill_down(tmp_path / "warehouse.duckdb", tmp_path, metric)
        # First run: no previous snapshot → no contributors
        assert len(result1) == 1
        assert result1[0].contributors == []

        # Second run: Widget=60, Gadget=90
        mock_conn.execute.return_value.fetchall.return_value = [
            ("Widget", 60.0),
            ("Gadget", 90.0),
        ]
        result2 = run_drill_down(tmp_path / "warehouse.duckdb", tmp_path, metric)
        # Second run: has previous snapshot → contributors computed
        assert len(result2) == 1
        assert len(result2[0].contributors) == 2
        # Widget dropped 40, Gadget rose 40 — both should appear
        values = {c.group_value: c.change_abs for c in result2[0].contributors}
        assert values["Widget"] == -40.0
        assert values["Gadget"] == 40.0
