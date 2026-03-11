"""dango/analysis/formatter.py

Pure formatting functions for analysis results.

Converts ``AnalysisResult`` objects into sorted, categorised dicts suitable
for API responses, CLI rendering, and webhook summaries.  No I/O or
database access — all functions are deterministic on their inputs.
"""

from __future__ import annotations

from typing import Any

from dango.analysis.models import AnalysisResult


def categorize_results(results: list[AnalysisResult]) -> list[dict[str, Any]]:
    """Sort results into dicts with a ``status`` field.

    Status priority: flagged > trending > normal > error.  Within each
    group, items are sorted by ``abs(change_pct)`` descending.

    Returns:
        List of dicts suitable for API responses and CLI rendering.
    """
    categorized: list[dict[str, Any]] = []

    for r in results:
        status = _assign_status(r)
        change_pct = r.comparison.change_pct if r.comparison else None

        drill_down_list: list[dict[str, Any]] = []
        for dim in r.drill_down:
            contributors = [
                {
                    "group_value": c.group_value,
                    "current_value": c.current_value,
                    "previous_value": c.previous_value,
                    "change_pct": c.change_pct,
                    "change_abs": c.change_abs,
                }
                for c in dim.contributors
            ]
            drill_down_list.append({"dimension": dim.dimension, "contributors": contributors})

        categorized.append(
            {
                "name": r.metric.metric_name,
                "status": status,
                "value": r.metric.value,
                "change_pct": change_pct,
                "comparison_type": (r.comparison.comparison_type.value if r.comparison else None),
                "baseline_value": (r.comparison.baseline_value if r.comparison else None),
                "exceeds_threshold": (r.comparison.exceeds_threshold if r.comparison else False),
                "trend_direction": (r.comparison.trend_direction if r.comparison else None),
                "forecast_threshold_days": (
                    r.comparison.forecast_threshold_days if r.comparison else None
                ),
                "source": r.metric.source,
                "table_name": r.metric.table_name,
                "drill_down": drill_down_list,
                "error": r.metric.error,
            }
        )

    # Sort: flagged first, then trending, normal, error
    # Within each group, sort by abs(change_pct) descending
    status_order = {"flagged": 0, "trending": 1, "normal": 2, "error": 3}
    categorized.sort(
        key=lambda d: (
            status_order.get(d["status"], 4),
            -(abs(d["change_pct"]) if d["change_pct"] is not None else 0),
        )
    )

    return categorized


def format_webhook_summary(
    results: list[AnalysisResult],
    flagged: list[AnalysisResult],
) -> str:
    """Build a one-line summary for webhook payloads.

    Example: ``"1 flagged (stripe_revenue +25.3%), 1 trending. 3 total."``

    Args:
        results: All analysis results.
        flagged: Only the flagged (threshold-exceeded) results.

    Returns:
        A human-readable one-line summary string.
    """
    parts: list[str] = []

    # Flagged details
    if flagged:
        flagged_details: list[str] = []
        for r in flagged[:3]:  # cap at 3 to keep it short
            pct = r.comparison.change_pct if r.comparison else None
            if pct is not None:
                sign = "+" if pct > 0 else ""
                flagged_details.append(f"{r.metric.metric_name} {sign}{pct:.1f}%")
            else:
                flagged_details.append(r.metric.metric_name)
        parts.append(f"{len(flagged)} flagged ({', '.join(flagged_details)})")

    # Trending count
    trending_count = sum(
        bool(
            r.comparison
            and r.comparison.trend_direction is not None
            and r.comparison.trend_direction != "stable"
            and not (r.comparison and r.comparison.exceeds_threshold)
        )
        for r in results
    )
    if trending_count:
        parts.append(f"{trending_count} trending")

    summary = ", ".join(parts) if parts else "No issues"
    return f"{summary}. {len(results)} total."


def _assign_status(result: AnalysisResult) -> str:
    """Determine the status category for an analysis result.

    Args:
        result: A single analysis result.

    Returns:
        One of ``"error"``, ``"flagged"``, ``"trending"``, or ``"normal"``.
    """
    if result.metric.error is not None:
        return "error"
    if result.comparison and result.comparison.exceeds_threshold:
        return "flagged"
    if (
        result.comparison
        and result.comparison.trend_direction is not None
        and result.comparison.trend_direction != "stable"
    ):
        return "trending"
    return "normal"
