"""dango/analysis/templates.py

Pre-built metric templates for common data sources.

Generates sensible default ``MonitorConfig`` objects when users add a new source,
so analysis starts working automatically from the first sync.
"""

from __future__ import annotations

import re
from pathlib import Path

from dango.analysis.models import ComparisonType, MonitorConfig

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_metrics_for_source(
    source_type: str,
    source_name: str,
    *,
    project_root: Path | None = None,
) -> list[MonitorConfig]:
    """Generate pre-built metric templates for a data source.

    Args:
        source_type: The type of source (e.g. ``"stripe"``, ``"google_analytics"``).
        source_name: The user-chosen name for this source instance.
        project_root: Optional project root for inspecting actual DuckDB columns.
            When provided, GA4 templates use actual column names from the warehouse.

    Returns:
        A list of ``MonitorConfig`` objects.  Empty if the source type has no
        pre-built templates or if the warehouse hasn't been synced yet.
    """
    generators = {
        "stripe": lambda name: _stripe_metrics(name),
        "google_analytics": lambda name: _google_analytics_metrics(name, project_root=project_root),
        "csv": lambda name: _csv_metrics(name),
    }
    generator = generators.get(source_type)
    if generator is not None:
        result = generator(source_name)
        if result:
            return result
    # Fallback: auto-discover tables and generate generic metrics
    return _generic_metrics(source_name, project_root)


# ---------------------------------------------------------------------------
# Private template generators
# ---------------------------------------------------------------------------


def _stripe_metrics(name: str) -> list[MonitorConfig]:
    """Stripe metrics: revenue, customers, refund rate, AOV."""
    schema = f"raw_{name}"
    return [
        MonitorConfig(
            name=f"{name}_daily_revenue",
            source_table=f"{schema}.charge",
            value_expression="SUM(amount) / 100.0",
            filter="status = 'succeeded'",
            compare=ComparisonType.week_over_week,
            alert_threshold=20.0,
            drill_down=["currency"],
        ),
        MonitorConfig(
            name=f"{name}_new_customers",
            source_table=f"{schema}.customer",
            value_expression="COUNT(*)",
            compare=ComparisonType.week_over_week,
            alert_threshold=25.0,
        ),
        MonitorConfig(
            name=f"{name}_refund_rate",
            source_table=f"{schema}.charge",
            value_expression=("COUNT(CASE WHEN refunded THEN 1 END) * 100.0 / NULLIF(COUNT(*), 0)"),
            compare=ComparisonType.rolling_7day_avg,
            alert_threshold=50.0,
        ),
        MonitorConfig(
            name=f"{name}_avg_order_value",
            source_table=f"{schema}.charge",
            value_expression="AVG(amount) / 100.0",
            filter="status = 'succeeded'",
            compare=ComparisonType.rolling_7day_avg,
            alert_threshold=15.0,
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
) -> list[MonitorConfig]:
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

    metrics: list[MonitorConfig] = []
    for keyword, exclude, suffix, agg, compare, threshold in _GA4_METRIC_DEFS:
        col = _find_column(columns, keyword, exclude)
        if col is None:
            continue
        drill = [source_col] if source_col and suffix == "daily_sessions" else []
        metrics.append(
            MonitorConfig(
                name=f"{name}_{suffix}",
                source_table=f"{schema}.{table}",
                value_expression=f"{agg}({col})",
                compare=compare,
                alert_threshold=threshold,
                drill_down=drill,
            )
        )
    return metrics


def _csv_metrics(name: str) -> list[MonitorConfig]:
    """CSV metrics: skipped — table name unknown until first sync.

    CSV table names derive from filenames, unknown at source-add time.
    Add metrics manually after first sync via 'dango db status'.
    """
    return []


def _generic_metrics(name: str, project_root: Path | None) -> list[MonitorConfig]:
    """Generate generic row-count and freshness metrics by discovering tables.

    Queries ``information_schema.tables`` for the ``raw_{name}`` schema,
    skipping dlt internal tables.  For each user table, creates:

    - ``{name}_{table}_row_count`` — ``COUNT(*)`` with week-over-week comparison
    - ``{name}_{table}_freshness`` — ``MAX(_dlt_load_id)``
      (only if the table has a ``_dlt_load_id`` column)

    Uses a single DuckDB connection for all queries to avoid N+1 overhead.
    """
    if project_root is None:
        return []
    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        return []
    schema = f"raw_{name}"
    try:
        import duckdb

        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
        try:
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = ? AND table_name NOT LIKE '_dlt_%' "
                "ORDER BY table_name",
                [schema],
            ).fetchall()
            if not tables:
                return []

            # Batch-fetch columns for all tables to check for _dlt_load_id
            cols_rows = conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND column_name = '_dlt_load_id'",
                [schema],
            ).fetchall()
            tables_with_load_id = {r[0] for r in cols_rows}
        finally:
            conn.close()
    except Exception:
        return []

    metrics: list[MonitorConfig] = []
    for (table_name,) in tables:
        sanitized = _sanitize_table_name(table_name)
        metrics.append(
            MonitorConfig(
                name=f"{name}_{sanitized}_row_count",
                source_table=f"{schema}.{table_name}",
                value_expression="COUNT(*)",
                compare=ComparisonType.week_over_week,
                alert_threshold=25.0,
            )
        )
        # Add freshness metric only if _dlt_load_id column exists
        if table_name in tables_with_load_id:
            metrics.append(
                MonitorConfig(
                    name=f"{name}_{sanitized}_freshness",
                    source_table=f"{schema}.{table_name}",
                    value_expression="MAX(_dlt_load_id)",
                    compare=None,
                    alert_threshold=None,
                )
            )
    return metrics


_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")
_LEADING_DIGITS_RE = re.compile(r"^[0-9]+")


def _sanitize_table_name(table_name: str) -> str:
    """Sanitize a table name for use in a metric name.

    Replaces non-alphanumeric characters with ``_`` and strips leading digits.
    """
    result = _SANITIZE_RE.sub("_", table_name)
    result = _LEADING_DIGITS_RE.sub("", result)
    return result.strip("_") or "table"


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

        conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
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
