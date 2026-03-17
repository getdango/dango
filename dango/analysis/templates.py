"""dango/analysis/templates.py

Pre-built metric templates for common data sources.

Generates sensible default ``MetricConfig`` objects when users add a new source,
so analysis starts working automatically from the first sync.
"""

from __future__ import annotations

from dango.analysis.models import ComparisonType, MetricConfig

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_metrics_for_source(
    source_type: str,
    source_name: str,
) -> list[MetricConfig]:
    """Generate pre-built metric templates for a data source.

    Args:
        source_type: The type of source (e.g. ``"stripe"``, ``"google_analytics"``).
        source_name: The user-chosen name for this source instance.

    Returns:
        A list of ``MetricConfig`` objects.  Empty if the source type has no
        pre-built templates.
    """
    generators = {
        "stripe": _stripe_metrics,
        "google_analytics": _google_analytics_metrics,
        "csv": _csv_metrics,
    }
    generator = generators.get(source_type)
    if generator is None:
        return []
    return generator(source_name)


# ---------------------------------------------------------------------------
# Private template generators
# ---------------------------------------------------------------------------


def _stripe_metrics(name: str) -> list[MetricConfig]:
    """Stripe metrics: revenue, customers, refund rate, AOV."""
    schema = f"raw_{name}"
    return [
        MetricConfig(
            name=f"{name}_daily_revenue",
            source_table=f"{schema}.charge",
            value_expression="SUM(amount) / 100.0",
            filter="status = 'succeeded'",
            compare=ComparisonType.week_over_week,
            warn_threshold=20.0,
            drill_down=["currency"],
        ),
        MetricConfig(
            name=f"{name}_new_customers",
            source_table=f"{schema}.customer",
            value_expression="COUNT(*)",
            compare=ComparisonType.week_over_week,
            warn_threshold=25.0,
        ),
        MetricConfig(
            name=f"{name}_refund_rate",
            source_table=f"{schema}.charge",
            value_expression=("COUNT(CASE WHEN refunded THEN 1 END) * 100.0 / NULLIF(COUNT(*), 0)"),
            compare=ComparisonType.rolling_7day_avg,
            warn_threshold=50.0,
        ),
        MetricConfig(
            name=f"{name}_avg_order_value",
            source_table=f"{schema}.charge",
            value_expression="AVG(amount) / 100.0",
            filter="status = 'succeeded'",
            compare=ComparisonType.rolling_7day_avg,
            warn_threshold=15.0,
        ),
    ]


def _google_analytics_metrics(name: str) -> list[MetricConfig]:
    """Google Analytics metrics: sessions, bounce rate, avg session duration."""
    schema = f"raw_{name}"
    return [
        MetricConfig(
            name=f"{name}_daily_sessions",
            source_table=f"{schema}.traffic",
            value_expression="SUM(sessions)",
            compare=ComparisonType.week_over_week,
            warn_threshold=20.0,
            drill_down=["session_source"],
        ),
        MetricConfig(
            name=f"{name}_bounce_rate",
            source_table=f"{schema}.traffic",
            value_expression="AVG(bounce_rate)",
            compare=ComparisonType.rolling_7day_avg,
            warn_threshold=25.0,
        ),
        MetricConfig(
            name=f"{name}_avg_session_duration",
            source_table=f"{schema}.traffic",
            value_expression="AVG(average_session_duration)",
            compare=ComparisonType.rolling_7day_avg,
            warn_threshold=20.0,
        ),
    ]


def _csv_metrics(name: str) -> list[MetricConfig]:
    """CSV metrics: skipped — table name unknown until first sync.

    CSV table names derive from filenames, unknown at source-add time.
    Add metrics manually after first sync via 'dango db status'.
    """
    return []
