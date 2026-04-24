"""dango/analysis/templates.py

Pre-built metric templates for common data sources.

Generates sensible default ``MetricConfig`` objects when users add a new source,
so analysis starts working automatically from the first sync.
"""

from __future__ import annotations

from pathlib import Path

from dango.analysis.models import ComparisonType, MetricConfig

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_metrics_for_source(
    source_type: str,
    source_name: str,
    *,
    project_root: Path | None = None,
) -> list[MetricConfig]:
    """Generate pre-built metric templates for a data source.

    Args:
        source_type: The type of source (e.g. ``"stripe"``, ``"google_analytics"``).
        source_name: The user-chosen name for this source instance.
        project_root: Optional project root for inspecting actual DuckDB columns.
            When provided, GA4 templates use actual column names from the warehouse.

    Returns:
        A list of ``MetricConfig`` objects.  Empty if the source type has no
        pre-built templates or if the warehouse hasn't been synced yet.
    """
    generators = {
        "stripe": lambda name: _stripe_metrics(name),
        "google_analytics": lambda name: _google_analytics_metrics(name, project_root=project_root),
        "csv": lambda name: _csv_metrics(name),
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


# GA4 metric definitions: (keyword_in_col, exclude_keyword, metric_name_suffix,
#                          agg_func, compare_type, warn_threshold)
_GA4_METRIC_DEFS: list[tuple[str, str | None, str, str, ComparisonType, float]] = [
    ("sessions", "engaged", "daily_sessions", "SUM", ComparisonType.week_over_week, 20.0),
    ("bounce_rate", None, "bounce_rate", "AVG", ComparisonType.rolling_7day_avg, 25.0),
    ("duration", None, "avg_session_duration", "AVG", ComparisonType.rolling_7day_avg, 20.0),
]


def _google_analytics_metrics(
    name: str,
    *,
    project_root: Path | None = None,
) -> list[MetricConfig]:
    """Google Analytics metrics using actual DuckDB column names.

    When ``project_root`` is provided and the warehouse contains the GA4 traffic
    table, column names are read from DuckDB (e.g. ``sessions_integer``,
    ``bounce_rate_float``).  Without a warehouse the function returns an empty
    list — metrics will be generated after the first sync.
    """
    schema = f"raw_{name}"
    table = "traffic"

    columns = _get_table_columns(schema, table, project_root)
    if not columns:
        return []

    # Find drill-down column for sessions (any column containing "source")
    source_col = next((c for c in columns if "source" in c.lower()), None)

    metrics: list[MetricConfig] = []
    for keyword, exclude, suffix, agg, compare, threshold in _GA4_METRIC_DEFS:
        col = _find_column(columns, keyword, exclude)
        if col is None:
            continue
        drill = [source_col] if source_col and suffix == "daily_sessions" else []
        metrics.append(
            MetricConfig(
                name=f"{name}_{suffix}",
                source_table=f"{schema}.{table}",
                value_expression=f"{agg}({col})",
                compare=compare,
                warn_threshold=threshold,
                drill_down=drill,
            )
        )
    return metrics


def _csv_metrics(name: str) -> list[MetricConfig]:
    """CSV metrics: skipped — table name unknown until first sync.

    CSV table names derive from filenames, unknown at source-add time.
    Add metrics manually after first sync via 'dango db status'.
    """
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_table_columns(schema: str, table: str, project_root: Path | None) -> list[str]:
    """Query DuckDB for column names of a table.  Returns empty list on failure."""
    if project_root is None:
        return []
    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        return []
    try:
        import duckdb

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = ? AND table_name = ?
                ORDER BY ordinal_position
                """,
                [schema, table],
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _find_column(columns: list[str], keyword: str, exclude: str | None = None) -> str | None:
    """Find the first column containing ``keyword`` (case-insensitive).

    If ``exclude`` is set, skip columns that also contain the exclude keyword.
    """
    kw = keyword.lower()
    exc = exclude.lower() if exclude else None
    for col in columns:
        cl = col.lower()
        if kw in cl and (exc is None or exc not in cl):
            return col
    return None
