"""dango/utils/post_sync.py

Post-sync dispatcher for data governance hooks.

Called after a successful ``dango sync`` to run profiling, drift detection,
PII scanning, and automated analysis on freshly-loaded data.  Each hook is
a stub that will be populated by subsequent Phase 7 tasks.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.logging import get_logger
from dango.utils.dango_db import connect
from dango.validation import validate_identifier

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Type classification helpers
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = frozenset(
    {
        "INTEGER",
        "INT",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "HUGEINT",
        "DOUBLE",
        "FLOAT",
        "REAL",
        "DECIMAL",
        "NUMERIC",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
        "INT1",
        "INT2",
        "INT4",
        "INT8",
    }
)

_STRING_TYPES = frozenset(
    {
        "VARCHAR",
        "TEXT",
        "CHAR",
        "BLOB",
        "UUID",
        "STRING",
    }
)

_PRECISION_RE = re.compile(r"\(.*\)$")


def _is_numeric(data_type: str) -> bool:
    """Check whether *data_type* is a numeric DuckDB type.

    Strips precision/scale suffixes (e.g. ``DECIMAL(10,2)`` → ``DECIMAL``).
    """
    base = _PRECISION_RE.sub("", data_type.strip()).upper()
    return base in _NUMERIC_TYPES


def _is_string(data_type: str) -> bool:
    """Check whether *data_type* is a string/text DuckDB type.

    Strips length suffixes (e.g. ``VARCHAR(255)`` → ``VARCHAR``).
    """
    base = _PRECISION_RE.sub("", data_type.strip()).upper()
    return base in _STRING_TYPES


# ---------------------------------------------------------------------------
# Profiling engine
# ---------------------------------------------------------------------------


def profile_table(
    project_root: Path,
    source: str,
    table_name: str,
) -> dict[str, dict[str, str | None]]:
    """Profile all columns of a single table and cache results.

    Opens DuckDB read-only, queries column metadata and computes per-column
    statistics (null counts, distinct counts, min/max, etc.).  Results are
    cached in the ``profiling_stats`` table of ``.dango/dango.db``.

    Args:
        project_root: Path to the Dango project root.
        source: Source name (used as ``raw_{source}`` schema).
        table_name: Table name within the source schema.

    Returns:
        Mapping of ``{column_name: {stat_type: stat_value}}``.  Stat values
        are always strings (or ``None``).
    """
    import duckdb  # lazy import (matches dlt_runner.py pattern)

    db_path = project_root / "data" / "warehouse.duckdb"
    schema = f"raw_{source}"

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        # Discover columns
        columns = conn.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = '{table_name}' "
            "ORDER BY ordinal_position"
        ).fetchall()

        if not columns:
            return {}

        # Total row count
        total_row_result = conn.execute(
            f'SELECT COUNT(*) FROM "{schema}"."{table_name}"'
        ).fetchone()
        total_rows: int = total_row_result[0] if total_row_result else 0

        stats: dict[str, dict[str, str | None]] = {}

        for col_name, data_type, _is_nullable in columns:
            try:
                col_stats = _profile_column(
                    conn,
                    schema,
                    table_name,
                    col_name,
                    data_type,
                    total_rows,
                )
                stats[col_name] = col_stats
            except Exception:
                logger.warning(
                    "profile_column_error",
                    source=source,
                    table=table_name,
                    column=col_name,
                )
    finally:
        conn.close()

    try:
        _cache_stats(project_root, source, table_name, stats)
    except Exception:
        logger.warning(
            "profiling_cache_error",
            source=source,
            table=table_name,
        )
    return stats


def _profile_column(
    conn: Any,
    schema: str,
    table_name: str,
    col_name: str,
    data_type: str,
    total_rows: int,
) -> dict[str, str | None]:
    """Compute statistics for a single column.

    Args:
        conn: Open DuckDB connection (read-only).
        schema: DuckDB schema name (e.g. ``raw_shopify``).
        table_name: Table name.
        col_name: Column name.
        data_type: DuckDB data type string.
        total_rows: Total number of rows in the table.

    Returns:
        Mapping of ``{stat_type: stat_value}`` for this column.
    """
    # Build aggregation expressions
    agg_parts = [
        f'COUNT(*) - COUNT("{col_name}") AS null_count',
        f'COUNT(DISTINCT "{col_name}") AS distinct_count',
    ]

    if _is_numeric(data_type):
        agg_parts.extend(
            [
                f'MIN("{col_name}")::VARCHAR AS min_val',
                f'MAX("{col_name}")::VARCHAR AS max_val',
                f'AVG("{col_name}")::VARCHAR AS mean_val',
            ]
        )
    elif _is_string(data_type):
        agg_parts.extend(
            [
                f'MIN(LENGTH("{col_name}"))::VARCHAR AS min_length',
                f'MAX(LENGTH("{col_name}"))::VARCHAR AS max_length',
            ]
        )

    agg_sql = ", ".join(agg_parts)
    row = conn.execute(f'SELECT {agg_sql} FROM "{schema}"."{table_name}"').fetchone()

    col_stats: dict[str, str | None] = {}

    if row is not None:
        null_count = row[0]
        distinct_count = row[1]
        col_stats["null_count"] = str(null_count)
        null_pct = (null_count / total_rows * 100) if total_rows > 0 else 0.0
        col_stats["null_pct"] = str(round(null_pct, 1))
        col_stats["distinct_count"] = str(distinct_count)

        idx = 2
        if _is_numeric(data_type):
            col_stats["min"] = str(row[idx]) if row[idx] is not None else None
            col_stats["max"] = str(row[idx + 1]) if row[idx + 1] is not None else None
            col_stats["mean"] = str(row[idx + 2]) if row[idx + 2] is not None else None
        elif _is_string(data_type):
            col_stats["min_length"] = str(row[idx]) if row[idx] is not None else None
            col_stats["max_length"] = str(row[idx + 1]) if row[idx + 1] is not None else None

    # Sample values (separate query, up to 5 distinct non-null values)
    try:
        sample_rows = conn.execute(
            f'SELECT DISTINCT "{col_name}"::VARCHAR '
            f'FROM "{schema}"."{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL '
            f"LIMIT 5"
        ).fetchall()
        sample_values = [r[0] for r in sample_rows]
        col_stats["sample_values"] = json.dumps(sample_values)
    except Exception:
        col_stats["sample_values"] = None

    return col_stats


def _cache_stats(
    project_root: Path,
    source: str,
    table_name: str,
    stats: dict[str, dict[str, str | None]],
) -> None:
    """Write profiling stats to the ``profiling_stats`` table.

    Uses ``INSERT OR REPLACE`` to upsert all stat rows in a single
    transaction.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name.
        stats: Mapping of ``{column_name: {stat_type: stat_value}}``.
    """
    now = datetime.now(timezone.utc).isoformat()

    with connect(project_root) as conn:
        for col_name, col_stats in stats.items():
            for stat_type, stat_value in col_stats.items():
                conn.execute(
                    "INSERT OR REPLACE INTO profiling_stats "
                    "(source, table_name, column_name, stat_type, stat_value, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (source, table_name, col_name, stat_type, stat_value, now),
                )
        conn.commit()


# ---------------------------------------------------------------------------
# Post-sync dispatcher
# ---------------------------------------------------------------------------


def dispatch_post_sync_hooks(
    project_root: Path,
    sources: list[str],
    *,
    sync_result: dict[str, Any] | None = None,
    skip_sync_notification: bool = False,
    trigger: str = "cli",
) -> None:
    """Run post-sync hooks for successfully synced sources.

    Invokes each hook in order: profiling, PII scanning, analysis,
    and (optionally) sync notification.

    Args:
        project_root: Path to the Dango project root.
        sources: Names of sources that synced successfully.
        sync_result: Summary dict from ``run_sync()`` (used for notifications).
        skip_sync_notification: If True, skip sending sync webhooks
            (e.g. when the scheduler sends its own notifications).
        trigger: Label for the sync trigger (e.g. ``"cli"``, ``"web"``).
    """
    if not sources:
        return

    logger.info("post_sync_hooks_start", sources=sources)

    _run_profiling(project_root, sources)
    _run_pii_scan(project_root, sources)
    _run_analysis(project_root, sources)

    if not skip_sync_notification and sync_result is not None:
        _send_sync_notification(project_root, sources, sync_result, trigger=trigger)

    logger.info("post_sync_hooks_complete", sources=sources)


def _run_profiling(project_root: Path, sources: list[str]) -> None:
    """Profile columns for freshly synced sources.

    Discovers all user tables in each source's ``raw_{source}`` schema
    (excluding dlt internal tables) and runs :func:`profile_table` on each.

    Args:
        project_root: Path to the Dango project root.
        sources: Names of sources that synced successfully.
    """
    import duckdb  # lazy import

    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        logger.debug("profiling_skip_no_warehouse", path=str(db_path))
        return

    for source in sources:
        try:
            logger.debug("profiling_source_start", source=source)
            schema = f"raw_{source}"

            conn = duckdb.connect(str(db_path), read_only=True)
            try:
                tables = conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    f"WHERE table_schema = '{schema}' "
                    "AND table_name NOT LIKE '_dlt_%' "
                    "AND table_name NOT IN ('spreadsheet', 'spreadsheet_info') "
                    "ORDER BY table_name"
                ).fetchall()
            finally:
                conn.close()

            for (tbl_name,) in tables:
                try:
                    tbl_name = validate_identifier(tbl_name)
                    profile_table(project_root, source, tbl_name)
                except Exception:
                    logger.warning(
                        "profiling_table_error",
                        source=source,
                        table=tbl_name,
                    )

            logger.debug("profiling_source_complete", source=source)
        except Exception:
            logger.warning("profiling_source_error", source=source)


def _run_pii_scan(project_root: Path, sources: list[str]) -> None:
    """Scan for PII in freshly synced sources.

    Args:
        project_root: Path to the Dango project root.
        sources: Names of sources that synced successfully.
    """
    try:
        from dango.governance.pii_detector import scan_sources_for_pii

        scan_sources_for_pii(project_root, sources)
    except Exception:
        logger.warning("pii_scan_error", sources=sources, exc_info=True)


def _run_analysis(project_root: Path, sources: list[str]) -> None:
    """Run automated analysis on freshly synced sources.

    Also auto-generates GA4 metric templates after first sync (column names
    are only known once DuckDB has data).
    """
    try:
        # Auto-generate GA4 metric templates now that columns exist in DuckDB
        _ensure_ga4_metrics(project_root, sources)

        from dango.analysis.metrics import run_analysis

        results = run_analysis(project_root, source_filter=[f"raw_{s}" for s in sources])
        flagged = [r for r in results if r.comparison and r.comparison.exceeds_threshold]
        if flagged:
            _send_analysis_webhook(project_root, sources, results, flagged)
    except Exception:
        logger.warning("analysis_hook_error", sources=sources, exc_info=True)


def _ensure_ga4_metrics(project_root: Path, sources: list[str]) -> None:
    """Generate GA4 metric templates if not already present.  Never raises."""
    try:
        from dango.analysis.config import add_metrics_to_config, load_metrics_config
        from dango.analysis.templates import generate_metrics_for_source
        from dango.config import get_config

        config = get_config(project_root)
        existing_names = {m.name for m in load_metrics_config(project_root).metrics}
        for source in config.sources.sources:
            if source.type.value != "google_analytics" or source.name not in sources:
                continue
            new = [
                m
                for m in generate_metrics_for_source(
                    "google_analytics", source.name, project_root=project_root
                )
                if m.name not in existing_names
            ]
            if new:
                add_metrics_to_config(project_root, new)
                logger.info("ga4_metrics_auto_generated", source=source.name, count=len(new))
    except Exception:
        logger.debug("ga4_metrics_auto_generate_skipped", exc_info=True)


def _deliver_to_webhooks(
    webhooks: list[Any],
    payload: Any,
) -> None:
    """Deliver a ``WebhookPayload`` to a list of webhook configs.

    Formats each webhook (Slack vs generic) and sends via synchronous httpx.
    Never raises — individual delivery errors are logged and skipped.
    """
    import httpx

    for webhook in webhooks:
        try:
            if webhook.format == "slack":
                from dango.platform.notifications.slack import format_slack_message

                json_payload: dict[str, Any] = format_slack_message(payload)
            else:
                json_payload = {
                    "event": payload.event_type.value,
                    "schedule": payload.schedule_name,
                    "sources": payload.sources,
                    "error": payload.error,
                    "duration_seconds": payload.duration_seconds,
                    "rows_loaded": payload.rows_loaded,
                    "dashboard_url": payload.dashboard_url,
                    "metadata": payload.metadata,
                    "timestamp": payload.occurred_at.isoformat() if payload.occurred_at else None,
                }
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(webhook.url, json=json_payload)
            logger.info("webhook_delivered", webhook=webhook.name, status=resp.status_code)
        except Exception:
            logger.warning("webhook_delivery_error", webhook=webhook.name, exc_info=True)


def _send_analysis_webhook(
    project_root: Path,
    sources: list[str],
    results: list[Any],
    flagged: list[Any],
) -> None:
    """Send webhook for flagged metric alerts.  Never raises."""
    try:
        from dango.analysis.formatter import format_webhook_summary
        from dango.platform.notifications.webhook import (
            EventType,
            WebhookPayload,
            load_notification_config,
            should_notify,
        )

        config = load_notification_config(project_root)
        if config is None or not config.webhooks:
            return
        if not should_notify(EventType.METRIC_ALERT, config):
            return

        summary = format_webhook_summary(results, flagged)
        payload = WebhookPayload(
            event_type=EventType.METRIC_ALERT,
            schedule_name="post_sync",
            sources=sources,
            metadata={
                "summary": summary,
                "flagged_count": len(flagged),
                "total_count": len(results),
            },
            occurred_at=datetime.now(tz=timezone.utc),
        )

        _deliver_to_webhooks(config.webhooks, payload)
    except Exception:
        logger.warning("analysis_webhook_outer_error", exc_info=True)


def _send_sync_notification(
    project_root: Path,
    sources: list[str],
    sync_result: dict[str, Any],
    *,
    trigger: str = "cli",
) -> None:
    """Send sync completed/failed webhook notification.  Never raises."""
    try:
        from dango.platform.notifications.webhook import (
            EventType,
            WebhookPayload,
            load_notification_config,
            should_notify,
        )

        config = load_notification_config(project_root)
        if config is None or not config.webhooks:
            return

        failed = sync_result.get("failed_count", 0)
        event_type = EventType.SYNC_FAILED if failed > 0 else EventType.SYNC_COMPLETED
        if not should_notify(event_type, config):
            return

        # Compute total rows loaded
        total_rows = sum(
            r.get("rows_loaded", 0) for r in sync_result.get("results", []) if isinstance(r, dict)
        )

        error_msg: str | None = None
        if failed > 0:
            failed_names = [f.get("name", "?") for f in sync_result.get("failed_sources", [])]
            error_msg = f"Failed sources: {', '.join(failed_names)}"

        payload = WebhookPayload(
            event_type=event_type,
            schedule_name=f"{trigger}_sync",
            sources=sources,
            rows_loaded=total_rows,
            error=error_msg,
            occurred_at=datetime.now(tz=timezone.utc),
        )

        _deliver_to_webhooks(config.webhooks, payload)
    except Exception:
        logger.warning("sync_notification_error", exc_info=True)
