"""dango/governance/pii_overrides.py

CRUD operations for PII override records.  Overrides let users dismiss
false-positive PII detections (``not_pii``) or manually mark columns as
containing PII (``pii``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.logging import get_logger
from dango.utils.dango_db import connect
from dango.validation import validate_identifier, validate_source_name

logger = get_logger(__name__)


def get_overrides_for_table(
    project_root: Path,
    source: str,
    table_name: str,
) -> dict[str, str]:
    """Return PII overrides for a single table.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name within the source.

    Returns:
        Mapping of ``{column_name: pii_status}`` (``"pii"`` or ``"not_pii"``).
        Returns an empty dict on any failure (never-fail pattern).
    """
    try:
        source = validate_source_name(source)
        table_name = validate_identifier(table_name)
        with connect(project_root) as conn:
            rows = conn.execute(
                "SELECT column_name, pii_status FROM pii_overrides "
                "WHERE source = ? AND table_name = ?",
                (source, table_name),
            ).fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception:
        logger.warning(
            "pii_overrides_read_error",
            source=source,
            table=table_name,
        )
        return {}


def get_pii_overrides(
    project_root: Path,
    *,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """List all PII overrides, optionally filtered by source.

    Args:
        project_root: Path to the Dango project root.
        source: Optional source name filter.

    Returns:
        List of override dicts.
    """
    conditions: list[str] = []
    params: list[str] = []

    if source is not None:
        source = validate_source_name(source)
        conditions.append("source = ?")
        params.append(source)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    query = (
        "SELECT id, source, table_name, column_name, pii_status, "
        "set_by, reason, updated_at "
        f"FROM pii_overrides {where_clause} "
        "ORDER BY updated_at DESC"
    )

    with connect(project_root) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        {
            "id": row[0],
            "source": row[1],
            "table_name": row[2],
            "column_name": row[3],
            "pii_status": row[4],
            "set_by": row[5],
            "reason": row[6],
            "updated_at": row[7],
        }
        for row in rows
    ]


def set_pii_override(
    project_root: Path,
    source: str,
    table_name: str,
    column_name: str,
    pii_status: str,
    set_by: str,
    reason: str | None = None,
) -> None:
    """Create or update a PII override for a column.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name within the source.
        column_name: Column name to override.
        pii_status: ``"pii"`` or ``"not_pii"``.
        set_by: Email or identifier of the user setting the override.
        reason: Optional human-readable reason.
    """
    source = validate_source_name(source)
    table_name = validate_identifier(table_name)
    column_name = validate_identifier(column_name)

    if pii_status not in ("pii", "not_pii"):
        msg = "pii_status must be 'pii' or 'not_pii'"
        raise ValueError(msg)

    now = datetime.now(timezone.utc).isoformat()

    with connect(project_root) as conn:
        conn.execute(
            "INSERT INTO pii_overrides "
            "(source, table_name, column_name, pii_status, set_by, reason, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, table_name, column_name) DO UPDATE SET "
            "pii_status=excluded.pii_status, set_by=excluded.set_by, "
            "reason=excluded.reason, updated_at=excluded.updated_at",
            (source, table_name, column_name, pii_status, set_by, reason, now),
        )
        conn.commit()


def delete_pii_override(
    project_root: Path,
    source: str,
    table_name: str,
    column_name: str,
) -> bool:
    """Delete a PII override for a column.

    Args:
        project_root: Path to the Dango project root.
        source: Source name.
        table_name: Table name within the source.
        column_name: Column name.

    Returns:
        ``True`` if a row was deleted, ``False`` if no matching override existed.
    """
    source = validate_source_name(source)
    table_name = validate_identifier(table_name)
    column_name = validate_identifier(column_name)

    with connect(project_root) as conn:
        cursor = conn.execute(
            "DELETE FROM pii_overrides WHERE source = ? AND table_name = ? AND column_name = ?",
            (source, table_name, column_name),
        )
        conn.commit()
        return cursor.rowcount > 0
