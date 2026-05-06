"""dango/governance/schema_drift.py

Schema drift detection engine for Dango.

Compares current DuckDB column schemas against stored baselines in SQLite,
detects added/removed/type-changed columns, records drift events, and sends
webhook notifications.  Called automatically after each ``dango sync`` via
the post-sync hook dispatcher.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.logging import get_logger
from dango.utils.dango_db import connect
from dango.validation import validate_identifier, validate_source_name

logger = get_logger(__name__)

# Event types that represent breaking schema changes.
_BREAKING_EVENT_TYPES: frozenset[str] = frozenset({"column_removed", "type_changed"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_drift_for_sources(
    project_root: Path,
    sources: list[str],
) -> list[dict[str, Any]]:
    """Detect schema drift for freshly synced sources.

    Discovers all user tables in each source's ``raw_{source}`` schema
    (excluding dlt internal tables) and runs :func:`detect_table_drift`
    for each.

    Args:
        project_root: Path to the Dango project root.
        sources: Names of sources that synced successfully.

    Returns:
        Flat list of drift event dicts across all sources and tables.
    """
    import duckdb  # lazy import

    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        logger.debug("drift_skip_no_warehouse", path=str(db_path))
        return []

    all_events: list[dict[str, Any]] = []

    for source in sources:
        try:
            logger.debug("drift_source_start", source=source)
            schema = f"raw_{source}"

            conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
            try:
                tables = conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = ? "
                    "AND table_name NOT LIKE '_dlt_%' "
                    "AND table_name NOT IN ('spreadsheet', 'spreadsheet_info') "
                    "ORDER BY table_name",
                    [schema],
                ).fetchall()
            finally:
                conn.close()

            for (tbl_name,) in tables:
                try:
                    tbl_name = validate_identifier(tbl_name)
                    events = detect_table_drift(project_root, source, tbl_name)
                    all_events.extend(events)
                except Exception:
                    logger.warning(
                        "drift_table_error",
                        source=source,
                        table=tbl_name,
                    )

            logger.debug("drift_source_complete", source=source)
        except Exception:
            logger.warning("drift_source_error", source=source)

    if all_events:
        _send_drift_webhook(project_root, sources, all_events)

    return all_events


def detect_table_drift(
    project_root: Path,
    source: str,
    table_name: str,
) -> list[dict[str, Any]]:
    """Compare current DuckDB columns against the stored baseline.

    On first sync (no baseline), stores the current schema silently and
    returns an empty list.  On subsequent syncs, computes the diff and
    records events.

    Args:
        project_root: Path to the Dango project root.
        source: Source name (used as ``raw_{source}`` schema).
        table_name: Table name within the source schema.

    Returns:
        List of drift event dicts (``column_added``, ``column_removed``,
        ``type_changed``).  Empty if no drift or first sync.
    """
    source = validate_source_name(source)
    table_name = validate_identifier(table_name)

    current_schema = _get_current_schema(project_root, source, table_name)
    if not current_schema:
        return []

    baseline = _get_baseline(project_root, source, table_name)

    # First sync — store baseline silently
    if baseline is None:
        try:
            _save_baseline(project_root, source, table_name, current_schema)
        except Exception:
            logger.warning(
                "drift_baseline_save_error",
                source=source,
                table=table_name,
            )
        return []

    # Compute diff
    events: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    # Columns added
    for col_name, col_type in current_schema.items():
        if col_name not in baseline:
            event_type = "column_added"
            events.append(
                {
                    "source": source,
                    "table_name": table_name,
                    "column_name": col_name,
                    "event_type": event_type,
                    "severity": "breaking" if event_type in _BREAKING_EVENT_TYPES else "additive",
                    "detail": f"type={col_type}",
                    "detected_at": now,
                }
            )

    # Columns removed
    for col_name in baseline:
        if col_name not in current_schema:
            event_type = "column_removed"
            events.append(
                {
                    "source": source,
                    "table_name": table_name,
                    "column_name": col_name,
                    "event_type": event_type,
                    "severity": "breaking" if event_type in _BREAKING_EVENT_TYPES else "additive",
                    "detail": f"was={baseline[col_name]}",
                    "detected_at": now,
                }
            )

    # Type changed
    for col_name, col_type in current_schema.items():
        if col_name in baseline and baseline[col_name] != col_type:
            event_type = "type_changed"
            events.append(
                {
                    "source": source,
                    "table_name": table_name,
                    "column_name": col_name,
                    "event_type": event_type,
                    "severity": "breaking" if event_type in _BREAKING_EVENT_TYPES else "additive",
                    "detail": f"{baseline[col_name]} -> {col_type}",
                    "detected_at": now,
                }
            )

    if not events:
        return []

    # Determine if any events are breaking
    has_breaking = any(ev.get("severity") == "breaking" for ev in events)

    # Record events (resilient cache pattern)
    try:
        if has_breaking:
            # Breaking drift: record events but do NOT update baseline.
            # Baseline stays old so drift keeps being detected until user accepts.
            # Note: when mixed with additive events, ALL events (including additive)
            # will be re-detected on subsequent syncs until the user accepts.
            # This is intentional — the user must accept the full batch.
            _record_drift_events_only(project_root, events)
            _set_source_attention(project_root, source, events)
        else:
            # Additive-only: record events + update baseline as before
            _record_drift_events(project_root, source, table_name, events, current_schema)
    except Exception:
        logger.warning(
            "drift_record_error",
            source=source,
            table=table_name,
        )

    return events


def get_drift_history(
    project_root: Path,
    *,
    source: str | None = None,
    table_name: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query drift events from the ``drift_events`` table.

    Args:
        project_root: Path to the Dango project root.
        source: Optional source name filter.
        table_name: Optional table name filter.
        limit: Maximum number of events to return (newest first).

    Returns:
        List of drift event dicts, newest first.
    """
    conditions: list[str] = []
    params: list[str | int] = []

    if source is not None:
        source = validate_source_name(source)
        conditions.append("source = ?")
        params.append(source)

    if table_name is not None:
        table_name = validate_identifier(table_name)
        conditions.append("table_name = ?")
        params.append(table_name)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    params.append(limit)

    query = (
        "SELECT id, source, table_name, column_name, event_type, severity, "
        "detail, detected_at "
        f"FROM drift_events {where_clause} "
        "ORDER BY id DESC LIMIT ?"
    )

    with connect(project_root) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        {
            "id": row[0],
            "source": row[1],
            "table_name": row[2],
            "column_name": row[3],
            "event_type": row[4],
            "severity": row[5],
            "detail": row[6],
            "detected_at": row[7],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_current_schema(
    project_root: Path,
    source: str,
    table_name: str,
) -> dict[str, str]:
    """Query current column schema from DuckDB.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name.

    Returns:
        Mapping of ``{column_name: data_type}``.  Empty if table not found.
    """
    import duckdb  # lazy import

    db_path = project_root / "data" / "warehouse.duckdb"
    schema = f"raw_{source}"

    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
    try:
        columns = conn.execute(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [schema, table_name],
        ).fetchall()
    finally:
        conn.close()

    return dict(columns)


def _get_baseline(
    project_root: Path,
    source: str,
    table_name: str,
) -> dict[str, str] | None:
    """Read the stored baseline from SQLite.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name.

    Returns:
        Mapping of ``{column_name: column_type}``, or ``None`` if no
        baseline exists for this source/table.
    """
    with connect(project_root) as conn:
        rows = conn.execute(
            "SELECT column_name, column_type FROM schema_baselines "
            "WHERE source = ? AND table_name = ?",
            (source, table_name),
        ).fetchall()

    if not rows:
        return None

    return {row[0]: row[1] for row in rows}


def _save_baseline(
    project_root: Path,
    source: str,
    table_name: str,
    columns: dict[str, str],
) -> None:
    """Store a schema baseline (DELETE + INSERT in single transaction).

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name.
        columns: Mapping of ``{column_name: column_type}``.
    """
    now = datetime.now(timezone.utc).isoformat()

    with connect(project_root) as conn:
        conn.execute(
            "DELETE FROM schema_baselines WHERE source = ? AND table_name = ?",
            (source, table_name),
        )
        for col_name, col_type in columns.items():
            conn.execute(
                "INSERT INTO schema_baselines "
                "(source, table_name, column_name, column_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, table_name, col_name, col_type, now),
            )
        conn.commit()


def _insert_drift_events(
    conn: Any,
    events: list[dict[str, Any]],
) -> None:
    """Insert drift event rows into the ``drift_events`` table.

    Shared helper used by both :func:`_record_drift_events` and
    :func:`_record_drift_events_only`.

    Args:
        conn: An open SQLite connection (caller manages transaction).
        events: List of drift event dicts.
    """
    for ev in events:
        conn.execute(
            "INSERT INTO drift_events "
            "(source, table_name, column_name, event_type, severity, detail, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev["source"],
                ev["table_name"],
                ev.get("column_name"),
                ev["event_type"],
                ev.get("severity"),
                ev.get("detail"),
                ev["detected_at"],
            ),
        )


def _record_drift_events(
    project_root: Path,
    source: str,
    table_name: str,
    events: list[dict[str, Any]],
    current_schema: dict[str, str],
) -> None:
    """Record drift events and update baseline atomically.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name.
        events: List of drift event dicts.
        current_schema: Current column schema to replace the baseline.
    """
    now = datetime.now(timezone.utc).isoformat()

    with connect(project_root) as conn:
        _insert_drift_events(conn, events)

        # Update baseline
        conn.execute(
            "DELETE FROM schema_baselines WHERE source = ? AND table_name = ?",
            (source, table_name),
        )
        for col_name, col_type in current_schema.items():
            conn.execute(
                "INSERT INTO schema_baselines "
                "(source, table_name, column_name, column_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, table_name, col_name, col_type, now),
            )
        conn.commit()


def _record_drift_events_only(
    project_root: Path,
    events: list[dict[str, Any]],
) -> None:
    """Record drift events WITHOUT updating baseline.

    Used for breaking drift so drift keeps being detected until user accepts.

    Args:
        project_root: Path to the Dango project root.
        events: List of drift event dicts.
    """
    with connect(project_root) as conn:
        _insert_drift_events(conn, events)
        conn.commit()


def _set_source_attention(
    project_root: Path,
    source: str,
    events: list[dict[str, Any]],
) -> None:
    """Flag a source as needing attention due to breaking drift.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        events: Breaking drift event dicts.
    """
    import json

    now = datetime.now(timezone.utc).isoformat()
    breaking = [e for e in events if e.get("severity") == "breaking"]
    reason = f"{len(breaking)} breaking schema change(s) detected"

    with connect(project_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO source_attention "
            "(source, reason, drift_events, created_at) "
            "VALUES (?, ?, ?, ?)",
            (source, reason, json.dumps(breaking), now),
        )
        conn.commit()


def accept_drift(project_root: Path, source: str) -> None:
    """Accept current schema as new baseline and clear attention flag.

    For each table in the source, reads the current DuckDB schema and saves
    it as the new baseline, then removes the attention flag.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
    """
    source = validate_source_name(source)

    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        # No warehouse — just clear the attention flag
        with connect(project_root) as conn:
            conn.execute("DELETE FROM source_attention WHERE source = ?", (source,))
            conn.commit()
        logger.info("drift_accepted_no_warehouse", source=source)
        return

    # Get all tables for this source from the current baseline
    with connect(project_root) as conn:
        rows = conn.execute(
            "SELECT DISTINCT table_name FROM schema_baselines WHERE source = ?",
            (source,),
        ).fetchall()
    table_names = [row[0] for row in rows]

    # Re-save baseline from current DuckDB state for each table
    for table_name in table_names:
        current_schema = _get_current_schema(project_root, source, table_name)
        if current_schema:
            _save_baseline(project_root, source, table_name, current_schema)

    # Clear the attention flag
    with connect(project_root) as conn:
        conn.execute("DELETE FROM source_attention WHERE source = ?", (source,))
        conn.commit()

    logger.info("drift_accepted", source=source, tables=len(table_names))


def get_sources_needing_attention(project_root: Path) -> list[dict[str, Any]]:
    """Return list of sources with unresolved breaking drift.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        List of dicts with source, reason, drift_events, created_at.
    """
    import json

    with connect(project_root) as conn:
        rows = conn.execute(
            "SELECT source, reason, drift_events, created_at FROM source_attention"
        ).fetchall()

    return [
        {
            "source": row[0],
            "reason": row[1],
            "drift_events": json.loads(row[2]) if row[2] else [],
            "created_at": row[3],
        }
        for row in rows
    ]


def _send_drift_webhook(
    project_root: Path,
    sources: list[str],
    events: list[dict[str, Any]],
) -> None:
    """Send webhook notification for detected drift events.

    Uses sync ``httpx.Client`` since post-sync hooks run in a thread.
    Never raises — entire body wrapped in ``try/except Exception``.

    Args:
        project_root: Path to the Dango project root.
        sources: Source names that were checked.
        events: All drift events detected.
    """
    try:
        from dango.platform.notifications.webhook import (
            EventType,
            WebhookPayload,
            load_notification_config,
            should_notify,
        )

        config = load_notification_config(project_root)
        if config is None:
            return

        if not should_notify(EventType.SCHEMA_DRIFT_DETECTED, config):
            return

        if not config.webhooks:
            return

        # Build summary string with severity breakdown
        event_counts: dict[str, int] = {}
        for ev in events:
            event_counts[ev["event_type"]] = event_counts.get(ev["event_type"], 0) + 1
        breaking_count = sum(1 for ev in events if ev.get("severity") == "breaking")
        additive_count = len(events) - breaking_count
        severity_parts = []
        if breaking_count:
            severity_parts.append(f"{breaking_count} breaking")
        if additive_count:
            severity_parts.append(f"{additive_count} additive")
        type_parts = [f"{count} {etype}" for etype, count in event_counts.items()]
        summary = (
            f"Schema drift detected: {', '.join(type_parts)} ({', '.join(severity_parts)} changes)"
        )

        payload = WebhookPayload(
            event_type=EventType.SCHEMA_DRIFT_DETECTED,
            schedule_name="post_sync",
            sources=sources,
            error=summary,
            occurred_at=datetime.now(tz=timezone.utc),
        )

        import httpx

        for webhook in config.webhooks:
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
                        "timestamp": (
                            payload.occurred_at.isoformat() if payload.occurred_at else None
                        ),
                    }

                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(webhook.url, json=json_payload)

                logger.info(
                    "drift_webhook_delivered",
                    webhook=webhook.name,
                    status=resp.status_code,
                )
            except Exception:
                logger.warning(
                    "drift_webhook_error",
                    webhook=webhook.name,
                    exc_info=True,
                )
    except Exception:
        logger.warning("drift_webhook_unexpected_error", exc_info=True)
