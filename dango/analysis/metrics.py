"""dango/analysis/metrics.py

Metric engine orchestration.

Loads configured metrics, executes each against DuckDB (read-only), stores
values in ``metric_history``, runs comparisons against historical data, and
stores comparison results in ``metric_results``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from dango.analysis.comparisons import compute_comparison
from dango.analysis.config import load_metrics_config
from dango.analysis.models import (
    AnalysisResult,
    ComparisonResult,
    MetricConfig,
    MetricValue,
)
from dango.logging import get_logger
from dango.utils.dango_db import connect

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_analysis(
    project_root: Path,
    *,
    source_filter: list[str] | None = None,
) -> list[AnalysisResult]:
    """Run all configured metrics and return analysis results.

    Args:
        project_root: Path to the Dango project root.
        source_filter: If provided, only run metrics whose ``source_table``
            starts with one of these source names.

    Returns:
        A list of ``AnalysisResult`` objects, one per metric.
    """
    config = load_metrics_config(project_root)
    if not config.enabled or not config.metrics:
        return []

    duckdb_path = project_root / "data" / "warehouse.duckdb"
    if not duckdb_path.exists():
        logger.warning("analysis_skip_no_warehouse", path=str(duckdb_path))
        return []

    results: list[AnalysisResult] = []

    for metric in config.metrics:
        if source_filter is not None:
            source, _table = _parse_source_from_table(metric.source_table)
            if source and source not in source_filter:
                continue

        metric_value = _execute_metric(duckdb_path, metric)
        _store_metric_value(project_root, metric_value)

        comparison: ComparisonResult | None = None
        if metric_value.error is None:
            comparison = compute_comparison(
                project_root,
                metric_value,
                metric.compare,
                metric.warn_threshold,
            )
            _store_comparison_result(project_root, comparison)

        results.append(AnalysisResult(metric=metric_value, comparison=comparison))

    logger.info(
        "analysis_complete",
        total=len(results),
        errors=sum(1 for r in results if r.metric.error),
    )
    return results


# ---------------------------------------------------------------------------
# Metric execution
# ---------------------------------------------------------------------------


def _execute_metric(duckdb_path: Path, metric: MetricConfig) -> MetricValue:
    """Execute a single metric query against DuckDB.

    Args:
        duckdb_path: Path to the DuckDB warehouse file.
        metric: The metric configuration to execute.

    Returns:
        A ``MetricValue`` with the query result or an error message.
    """
    source, table = _parse_source_from_table(metric.source_table)
    sql = _build_metric_sql(metric)

    try:
        conn = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            row = conn.execute(sql).fetchone()
        finally:
            conn.close()
    except duckdb.Error as e:
        logger.warning(
            "metric_execution_failed",
            metric=metric.name,
            error=str(e),
        )
        return MetricValue(
            metric_name=metric.name,
            source=source,
            table_name=table,
            error=str(e),
        )

    value = float(row[0]) if row and row[0] is not None else None

    return MetricValue(
        metric_name=metric.name,
        source=source,
        table_name=table,
        value=value,
    )


def _build_metric_sql(metric: MetricConfig) -> str:
    """Build the SQL query for a metric.

    Args:
        metric: The metric configuration.

    Returns:
        A SQL SELECT string.
    """
    sql = f"SELECT {metric.value_expression} FROM {metric.source_table}"  # noqa: S608
    if metric.filter:
        sql += f" WHERE {metric.filter}"
    return sql


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _store_metric_value(project_root: Path, metric_value: MetricValue) -> None:
    """Insert a metric value into the ``metric_history`` table.

    Args:
        project_root: Path to the Dango project root.
        metric_value: The value to store.
    """
    if metric_value.value is None:
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        with connect(project_root) as conn:
            conn.execute(
                "INSERT INTO metric_history "
                "(metric_name, source, table_name, metric_value, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    metric_value.metric_name,
                    metric_value.source,
                    metric_value.table_name,
                    metric_value.value,
                    now,
                ),
            )
            conn.commit()
    except Exception:
        logger.warning(
            "metric_history_store_failed",
            metric=metric_value.metric_name,
            exc_info=True,
        )


def _store_comparison_result(
    project_root: Path,
    result: ComparisonResult,
) -> None:
    """Insert a comparison result into the ``metric_results`` table.

    Args:
        project_root: Path to the Dango project root.
        result: The comparison result to store.
    """
    now = datetime.now(timezone.utc).isoformat()
    result_value = json.dumps(result.model_dump(mode="json"))
    try:
        with connect(project_root) as conn:
            conn.execute(
                "INSERT INTO metric_results "
                "(metric_name, source, table_name, result_type, result_value, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    result.metric_name,
                    None,
                    None,
                    result.comparison_type.value,
                    result_value,
                    now,
                ),
            )
            conn.commit()
    except Exception:
        logger.warning(
            "metric_result_store_failed",
            metric=result.metric_name,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_source_from_table(source_table: str) -> tuple[str | None, str | None]:
    """Split ``schema.table`` into ``(source, table)`` components.

    Args:
        source_table: A schema-qualified table name.

    Returns:
        A tuple of ``(source, table)``.  Both ``None`` if the string
        does not contain a dot.
    """
    if "." not in source_table:
        return None, None
    parts = source_table.split(".", 1)
    return parts[0], parts[1]
