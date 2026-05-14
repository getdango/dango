"""tests/unit/test_analysis_templates.py

Tests for pre-built metric templates (dango/analysis/templates.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from dango.analysis.models import ComparisonType
from dango.analysis.templates import (
    _sanitize_table_name,
    generate_metrics_for_source,
)

_MOD = "dango.analysis.templates"


def _create_ga4_warehouse(tmp_path: Path, columns: list[str]) -> Path:
    """Create a minimal DuckDB warehouse with a GA4 traffic table."""
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw_ga")
    col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
    conn.execute(f"CREATE TABLE raw_ga.traffic ({col_defs})")
    conn.close()
    return db_path


@pytest.mark.unit
class TestGenerateMetricsForSource:
    """generate_metrics_for_source returns valid MetricConfig lists."""

    def test_stripe_generates_four_metrics(self) -> None:
        """Stripe template produces 4 metrics."""
        metrics = generate_metrics_for_source("stripe", "my_stripe")
        assert len(metrics) == 4

    def test_stripe_table_prefixes(self) -> None:
        """All Stripe metrics reference raw_<name>.* tables."""
        metrics = generate_metrics_for_source("stripe", "payments")
        for m in metrics:
            assert m.source_table.startswith("raw_payments.")

    def test_stripe_names_prefixed(self) -> None:
        """All Stripe metric names are prefixed with source_name."""
        metrics = generate_metrics_for_source("stripe", "shop")
        for m in metrics:
            assert m.name.startswith("shop_")

    def test_google_analytics_no_metrics_without_project_root(self) -> None:
        """GA4 returns empty when no project_root is provided."""
        metrics = generate_metrics_for_source("google_analytics", "ga")
        assert len(metrics) == 0

    def test_google_analytics_no_metrics_without_warehouse(self, tmp_path: Path) -> None:
        """GA4 returns empty when warehouse doesn't exist."""
        metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)
        assert len(metrics) == 0

    def test_google_analytics_generates_metrics_from_duckdb(self, tmp_path: Path) -> None:
        """GA4 generates metrics using actual DuckDB column names."""
        _create_ga4_warehouse(
            tmp_path,
            [
                "date",
                "sessions_integer",
                "bounce_rate_float",
                "average_session_duration_seconds",
                "session_source",
            ],
        )

        metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)

        assert len(metrics) == 3
        exprs = {m.name: m.value_expression for m in metrics}
        assert exprs["ga_daily_sessions"] == "SUM(sessions_integer)"
        assert exprs["ga_bounce_rate"] == "AVG(bounce_rate_float)"
        assert exprs["ga_avg_session_duration"] == "AVG(average_session_duration_seconds)"

    def test_google_analytics_drill_down_uses_source_column(self, tmp_path: Path) -> None:
        """GA4 sessions metric uses the actual source column for drill-down."""
        _create_ga4_warehouse(
            tmp_path,
            [
                "sessions_integer",
                "session_source",
            ],
        )

        metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)

        sessions_metric = next(m for m in metrics if "sessions" in m.name)
        assert sessions_metric.drill_down == ["session_source"]

    def test_google_analytics_table_prefixes(self, tmp_path: Path) -> None:
        """All GA metrics reference raw_<name>.traffic tables."""
        _create_ga4_warehouse(
            tmp_path,
            [
                "sessions_integer",
                "bounce_rate_float",
                "average_session_duration_seconds",
            ],
        )

        metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)

        for m in metrics:
            assert m.source_table == "raw_ga.traffic"

    def test_google_analytics_excludes_engaged_sessions(self, tmp_path: Path) -> None:
        """GA4 sessions metric should not match 'engaged_sessions'."""
        _create_ga4_warehouse(
            tmp_path,
            [
                "sessions_integer",
                "engaged_sessions_integer",
            ],
        )

        metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)

        sessions_metric = next(m for m in metrics if "sessions" in m.name)
        assert "engaged" not in sessions_metric.value_expression

    def test_csv_generates_no_metrics_without_warehouse(self) -> None:
        """CSV without project_root returns empty (table name unknown at add time)."""
        metrics = generate_metrics_for_source("csv", "uploads")
        assert len(metrics) == 0

    def test_csv_falls_through_to_generic_after_sync(self, tmp_path: Path) -> None:
        """CSV with warehouse falls through to generic metrics (BUG-174)."""
        _create_generic_warehouse(tmp_path, "uploads", {"sales_2024": ["id", "amount"]})

        metrics = generate_metrics_for_source("csv", "uploads", project_root=tmp_path)

        assert len(metrics) >= 1
        assert any("sales_2024" in m.source_table for m in metrics)

    def test_unknown_source_returns_empty(self) -> None:
        """Unknown source type returns an empty list."""
        assert generate_metrics_for_source("unknown_db", "test") == []

    def test_all_templates_unique_names(self, tmp_path: Path) -> None:
        """All templates produce unique metric names within a source."""
        # Stripe has hardcoded metrics
        metrics = generate_metrics_for_source("stripe", "test")
        names = [m.name for m in metrics]
        assert len(names) == len(set(names)), "Duplicate names in stripe"

        # GA4 with real DuckDB
        _create_ga4_warehouse(
            tmp_path,
            [
                "sessions_integer",
                "bounce_rate_float",
                "average_session_duration_seconds",
            ],
        )
        metrics = generate_metrics_for_source("google_analytics", "test", project_root=tmp_path)
        names = [m.name for m in metrics]
        assert len(names) == len(set(names)), "Duplicate names in google_analytics"

    def test_metrics_are_valid_pydantic(self) -> None:
        """All generated metrics pass MetricConfig validation."""
        for m in generate_metrics_for_source("stripe", "test"):
            assert m.name
            assert m.source_table
            assert m.value_expression
        # CSV and GA4 without project_root return empty
        assert generate_metrics_for_source("csv", "test") == []
        assert generate_metrics_for_source("google_analytics", "test") == []


@pytest.mark.unit
class TestGA4DynamicMetrics:
    """Tests for dynamic GA4 column name resolution."""

    def test_partial_columns_produces_partial_metrics(self, tmp_path: Path) -> None:
        """Only matching columns produce metrics."""
        _create_ga4_warehouse(tmp_path, ["sessions_integer", "other_column"])

        metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)

        assert len(metrics) == 1
        assert metrics[0].name == "ga_daily_sessions"

    def test_no_matching_columns_falls_through_to_generic(self, tmp_path: Path) -> None:
        """No matching GA4 columns → falls through to generic row_count metrics."""
        _create_ga4_warehouse(tmp_path, ["other_column", "another_column"])

        metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)

        # Generic fallback discovers the traffic table and generates row_count
        assert len(metrics) >= 1
        assert any("row_count" in m.name for m in metrics)

    def test_duckdb_error_returns_empty(self, tmp_path: Path) -> None:
        """DuckDB errors are swallowed gracefully."""
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        db_path.touch()  # Invalid DuckDB file

        with patch(f"{_MOD}._get_table_columns", return_value=[]):
            metrics = generate_metrics_for_source("google_analytics", "ga", project_root=tmp_path)

        assert len(metrics) == 0


# ---------------------------------------------------------------------------
# Generic metric templates (BUG-148)
# ---------------------------------------------------------------------------


def _create_generic_warehouse(
    tmp_path: Path,
    source_name: str,
    tables: dict[str, list[str]],
) -> Path:
    """Create a DuckDB warehouse with arbitrary tables.

    Args:
        tables: Mapping of table_name → list of column names.
    """
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = f"raw_{source_name}"
    conn = duckdb.connect(str(db_path))
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for table_name, cols in tables.items():
        col_defs = ", ".join(f'"{c}" VARCHAR' for c in cols)
        conn.execute(f'CREATE TABLE {schema}."{table_name}" ({col_defs})')
    conn.close()
    return db_path


@pytest.mark.unit
class TestGenericMetrics:
    """Tests for the generic metric fallback (unknown source types)."""

    def test_no_project_root_returns_empty(self) -> None:
        metrics = generate_metrics_for_source("hubspot", "hs")
        assert metrics == []

    def test_no_warehouse_returns_empty(self, tmp_path: Path) -> None:
        metrics = generate_metrics_for_source("hubspot", "hs", project_root=tmp_path)
        assert metrics == []

    def test_no_tables_returns_empty(self, tmp_path: Path) -> None:
        """Schema exists but has no user tables."""
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw_empty")
        conn.close()

        metrics = generate_metrics_for_source("hubspot", "empty", project_root=tmp_path)
        assert metrics == []

    def test_generates_row_count_per_table(self, tmp_path: Path) -> None:
        _create_generic_warehouse(
            tmp_path,
            "hs",
            {
                "contact": ["id", "email"],
                "deal": ["id", "amount"],
            },
        )

        metrics = generate_metrics_for_source("hubspot", "hs", project_root=tmp_path)

        row_count_metrics = [m for m in metrics if "row_count" in m.name]
        assert len(row_count_metrics) == 2
        names = {m.name for m in row_count_metrics}
        assert "hs_contact_row_count" in names
        assert "hs_deal_row_count" in names

    def test_freshness_only_when_dlt_load_id_exists(self, tmp_path: Path) -> None:
        _create_generic_warehouse(
            tmp_path,
            "src",
            {
                "with_load_id": ["id", "_dlt_load_id"],
                "without_load_id": ["id", "name"],
            },
        )

        metrics = generate_metrics_for_source("salesforce", "src", project_root=tmp_path)

        freshness_metrics = [m for m in metrics if "freshness" in m.name]
        assert len(freshness_metrics) == 1
        assert freshness_metrics[0].name == "src_with_load_id_freshness"
        assert "MAX(_dlt_load_id)" in freshness_metrics[0].value_expression
        assert freshness_metrics[0].compare == ComparisonType.none
        assert freshness_metrics[0].alert_threshold is None

    def test_skips_dlt_internal_tables(self, tmp_path: Path) -> None:
        _create_generic_warehouse(
            tmp_path,
            "src",
            {
                "user_table": ["id"],
                "_dlt_loads": ["load_id"],
                "_dlt_version": ["version"],
            },
        )

        metrics = generate_metrics_for_source("salesforce", "src", project_root=tmp_path)

        metric_tables = {m.source_table for m in metrics}
        assert "raw_src.user_table" in metric_tables
        assert all("_dlt_" not in t for t in metric_tables)

    def test_unique_metric_names(self, tmp_path: Path) -> None:
        _create_generic_warehouse(
            tmp_path,
            "src",
            {
                "table_a": ["id", "_dlt_load_id"],
                "table_b": ["id", "_dlt_load_id"],
            },
        )

        metrics = generate_metrics_for_source("salesforce", "src", project_root=tmp_path)

        names = [m.name for m in metrics]
        assert len(names) == len(set(names))


@pytest.mark.unit
class TestSanitizeTableName:
    """Tests for _sanitize_table_name()."""

    def test_basic_name(self) -> None:
        assert _sanitize_table_name("users") == "users"

    def test_hyphens_replaced(self) -> None:
        assert _sanitize_table_name("my-table") == "my_table"

    def test_dots_replaced(self) -> None:
        assert _sanitize_table_name("my.table") == "my_table"

    def test_leading_digits_stripped(self) -> None:
        assert _sanitize_table_name("123abc") == "abc"

    def test_all_special_chars_returns_table(self) -> None:
        assert _sanitize_table_name("---") == "table"

    def test_all_digits_returns_table(self) -> None:
        assert _sanitize_table_name("123") == "table"

    def test_mixed_special_chars(self) -> None:
        assert _sanitize_table_name("my table (v2)") == "my_table__v2"
