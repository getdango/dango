"""dango/analysis/drilldown.py

Drill-down engine for identifying top contributors to metric changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from dango.analysis.models import (
    ComparisonResult,
    DimensionContributor,
    DrillDownDimension,
    MetricConfig,
    MetricValue,
)
from dango.logging import get_logger
from dango.utils.dango_db import connect

logger = get_logger(__name__)

MAX_GROUPS = 100
TOP_CONTRIBUTORS = 3
NULL_SENTINEL = "__null__"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_drill_down(
    duckdb_path: Path,
    project_root: Path,
    metric: MetricConfig,
    current_value: MetricValue,
    comparison: ComparisonResult,
) -> list[DrillDownDimension]:
    """Run drill-down analysis for each configured dimension.

    For each dimension in ``metric.drill_down``, executes a GROUP BY query,
    compares against previous snapshot, and identifies top contributors.

    Args:
        duckdb_path: Path to the DuckDB warehouse file.
        project_root: Path to the Dango project root.
        metric: The metric configuration with drill_down dimensions.
        current_value: The current metric value.
        comparison: The comparison result that triggered drill-down.

    Returns:
        A list of ``DrillDownDimension`` objects, one per dimension.
    """
    if not metric.drill_down:
        return []

    results: list[DrillDownDimension] = []

    for dimension in metric.drill_down:
        breakdown = _query_dimension_breakdown(duckdb_path, metric, dimension)
        if not breakdown:
            results.append(DrillDownDimension(dimension=dimension))
            continue

        previous = _get_previous_snapshot(project_root, metric.name, dimension)
        _store_snapshot(project_root, metric.name, dimension, breakdown)
        contributors = _compute_contributors(breakdown, previous)

        results.append(DrillDownDimension(dimension=dimension, contributors=contributors))

    logger.info(
        "drill_down_complete",
        metric=metric.name,
        dimensions=len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Internal functions
# ---------------------------------------------------------------------------


def _query_dimension_breakdown(
    duckdb_path: Path,
    metric: MetricConfig,
    dimension: str,
) -> dict[str | None, float]:
    """Query DuckDB for a GROUP BY breakdown of a metric by dimension.

    Args:
        duckdb_path: Path to the DuckDB warehouse file.
        metric: The metric configuration.
        dimension: The column to group by.

    Returns:
        A mapping of dimension value to aggregated metric value.
        Empty dict on error.
    """
    sql = (  # noqa: S608
        f"SELECT CAST({dimension} AS VARCHAR), {metric.value_expression} FROM {metric.source_table}"
    )
    if metric.filter:
        sql += f" WHERE {metric.filter}"
    sql += f" GROUP BY {dimension} ORDER BY ABS({metric.value_expression}) DESC LIMIT {MAX_GROUPS}"

    try:
        conn = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
    except duckdb.Error as e:
        logger.warning(
            "drill_down_query_failed",
            metric=metric.name,
            dimension=dimension,
            error=str(e),
        )
        return {}

    result: dict[str | None, float] = {}
    for row in rows:
        key = row[0]  # None for NULL dimension values
        value = float(row[1]) if row[1] is not None else 0.0
        result[key] = value
    return result


def _get_previous_snapshot(
    project_root: Path,
    metric_name: str,
    dimension: str,
) -> dict[str | None, float] | None:
    """Retrieve the most recent drill-down snapshot from metric_results.

    Args:
        project_root: Path to the Dango project root.
        metric_name: Name of the metric.
        dimension: The dimension column name.

    Returns:
        A mapping of dimension value to metric value, or ``None`` if no
        previous snapshot exists.
    """
    result_type = f"drill_down:{dimension}"

    try:
        with connect(project_root) as conn:
            row = conn.execute(
                "SELECT result_value FROM metric_results "
                "WHERE metric_name = ? AND result_type = ? "
                "ORDER BY computed_at DESC LIMIT 1",
                (metric_name, result_type),
            ).fetchone()
    except Exception:
        logger.warning(
            "drill_down_snapshot_read_failed",
            metric=metric_name,
            dimension=dimension,
            exc_info=True,
        )
        return None

    if row is None:
        return None

    raw: dict[str, float] = json.loads(row["result_value"])
    result: dict[str | None, float] = {}
    for k, v in raw.items():
        key: str | None = None if k == NULL_SENTINEL else k
        result[key] = v
    return result


def _store_snapshot(
    project_root: Path,
    metric_name: str,
    dimension: str,
    breakdown: dict[str | None, float],
) -> None:
    """Store a drill-down breakdown as a snapshot in metric_results.

    Uses the resilient cache pattern: failures are logged but never propagated.

    Args:
        project_root: Path to the Dango project root.
        metric_name: Name of the metric.
        dimension: The dimension column name.
        breakdown: The current GROUP BY breakdown to store.
    """
    serialized: dict[str, float] = {}
    for k, v in breakdown.items():
        key = NULL_SENTINEL if k is None else k
        serialized[key] = v

    result_value = json.dumps(serialized)
    result_type = f"drill_down:{dimension}"
    now = datetime.now(timezone.utc).isoformat()

    try:
        with connect(project_root) as conn:
            conn.execute(
                "INSERT INTO metric_results "
                "(metric_name, source, table_name, result_type, result_value, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (metric_name, None, None, result_type, result_value, now),
            )
            conn.commit()
    except Exception:
        logger.warning(
            "drill_down_snapshot_store_failed",
            metric=metric_name,
            dimension=dimension,
            exc_info=True,
        )


def _compute_contributors(
    current: dict[str | None, float],
    previous: dict[str | None, float] | None,
) -> list[DimensionContributor]:
    """Compute the top contributors to a metric change between snapshots.

    Args:
        current: Current GROUP BY breakdown.
        previous: Previous GROUP BY breakdown, or ``None`` for first run.

    Returns:
        Top contributors sorted by absolute change, descending.
        Empty list on first run (no previous snapshot).
    """
    if previous is None:
        return []

    all_keys = set(current) | set(previous)
    contributors: list[DimensionContributor] = []

    for key in all_keys:
        cur_val = current.get(key, 0.0)
        prev_val = previous.get(key)

        if prev_val is None:
            # New group — no previous value
            contributors.append(
                DimensionContributor(
                    group_value=key,
                    current_value=cur_val,
                    previous_value=None,
                    change_pct=None,
                    change_abs=None,
                )
            )
            continue

        change_abs = cur_val - prev_val
        change_pct: float | None = None
        if prev_val != 0:
            change_pct = ((cur_val - prev_val) / abs(prev_val)) * 100

        contributors.append(
            DimensionContributor(
                group_value=key,
                current_value=cur_val,
                previous_value=prev_val,
                change_pct=change_pct,
                change_abs=change_abs,
            )
        )

    # Sort by absolute change descending, None change_abs treated as 0
    contributors.sort(
        key=lambda c: abs(c.change_abs) if c.change_abs is not None else 0, reverse=True
    )
    return contributors[:TOP_CONTRIBUTORS]
