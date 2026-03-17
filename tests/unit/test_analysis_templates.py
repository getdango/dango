"""tests/unit/test_analysis_templates.py

Tests for pre-built metric templates (dango/analysis/templates.py).
"""

from __future__ import annotations

import pytest

from dango.analysis.templates import generate_metrics_for_source


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

    def test_google_analytics_generates_three_metrics(self) -> None:
        """Google Analytics template produces 3 metrics."""
        metrics = generate_metrics_for_source("google_analytics", "ga")
        assert len(metrics) == 3

    def test_google_analytics_table_prefixes(self) -> None:
        """All GA metrics reference raw_<name>.* tables."""
        metrics = generate_metrics_for_source("google_analytics", "ga")
        for m in metrics:
            assert m.source_table.startswith("raw_ga.")

    def test_csv_generates_no_metrics(self) -> None:
        """CSV template returns empty (table name unknown at add time)."""
        metrics = generate_metrics_for_source("csv", "uploads")
        assert len(metrics) == 0

    def test_unknown_source_returns_empty(self) -> None:
        """Unknown source type returns an empty list."""
        assert generate_metrics_for_source("unknown_db", "test") == []

    def test_all_templates_unique_names(self) -> None:
        """All templates produce unique metric names within a source."""
        for source_type in ("stripe", "google_analytics", "csv"):
            metrics = generate_metrics_for_source(source_type, "test")
            names = [m.name for m in metrics]
            assert len(names) == len(set(names)), f"Duplicate names in {source_type}"

    def test_metrics_are_valid_pydantic(self) -> None:
        """All generated metrics pass MetricConfig validation."""
        for source_type in ("stripe", "google_analytics", "csv"):
            metrics = generate_metrics_for_source(source_type, "test")
            for m in metrics:
                # MetricConfig is frozen — accessing fields verifies construction
                assert m.name
                assert m.source_table
                assert m.value_expression
