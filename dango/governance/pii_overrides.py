"""dango/governance/pii_overrides.py

CRUD operations for PII override records.  Overrides let users dismiss
false-positive PII detections (``not_pii``) or manually mark columns as
containing PII (``pii``).

Storage: ``.dango/pii-overrides.yml`` (version-controllable YAML).
Migrates automatically from SQLite on first access if YAML is missing.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from dango.logging import get_logger
from dango.validation import validate_identifier, validate_source_name

logger = get_logger(__name__)


# -- Internal helpers --------------------------------------------------------


def _overrides_path(project_root: Path) -> Path:
    """Return the path to ``.dango/pii-overrides.yml``."""
    return project_root / ".dango" / "pii-overrides.yml"


def _yaml_to_dict(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert short YAML keys to internal keys."""
    return {
        "source": entry.get("source", ""),
        "table_name": entry.get("table", ""),
        "column_name": entry.get("column", ""),
        "pii_status": entry.get("status", ""),
        "set_by": entry.get("set_by", ""),
        "reason": entry.get("reason"),
        "updated_at": entry.get("updated_at", ""),
    }


def _dict_to_yaml(d: dict[str, Any]) -> dict[str, Any]:
    """Convert internal keys to short YAML keys."""
    return {
        "source": d.get("source", ""),
        "table": d.get("table_name", ""),
        "column": d.get("column_name", ""),
        "status": d.get("pii_status", ""),
        "set_by": d.get("set_by", ""),
        "reason": d.get("reason"),
        "updated_at": d.get("updated_at", ""),
    }


def _load_overrides(project_root: Path) -> list[dict[str, Any]]:
    """Read overrides from YAML, triggering migration if needed."""
    path = _overrides_path(project_root)
    if not path.exists():
        _maybe_migrate_from_sqlite(project_root)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not data or "overrides" not in data:
            return []
        return [_yaml_to_dict(e) for e in data["overrides"]]
    except Exception:
        logger.warning("pii_overrides_read_error", path=str(path))
        return []


def _save_overrides(project_root: Path, overrides: list[dict[str, Any]]) -> None:
    """Atomic write overrides to YAML (tmp file + rename)."""
    path = _overrides_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_entries = [_dict_to_yaml(o) for o in overrides]
    content = yaml.dump(
        {"overrides": yaml_entries},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".yml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _maybe_migrate_from_sqlite(project_root: Path) -> None:
    """One-time migration: read SQLite ``pii_overrides`` table, write to YAML."""
    try:
        from dango.utils.dango_db import connect

        with connect(project_root) as conn:
            rows = conn.execute(
                "SELECT source, table_name, column_name, pii_status, "
                "set_by, reason, updated_at FROM pii_overrides"
            ).fetchall()
        if not rows:
            return
        overrides = [
            {
                "source": r[0],
                "table_name": r[1],
                "column_name": r[2],
                "pii_status": r[3],
                "set_by": r[4],
                "reason": r[5],
                "updated_at": r[6],
            }
            for r in rows
        ]
        _save_overrides(project_root, overrides)
        logger.info(
            "pii_overrides_migrated",
            count=len(overrides),
        )
    except Exception:
        logger.warning("pii_overrides_migration_failed", exc_info=True)


# -- Public API --------------------------------------------------------------


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
        overrides = _load_overrides(project_root)
        return {
            o["column_name"]: o["pii_status"]
            for o in overrides
            if o["source"] == source and o["table_name"] == table_name
        }
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
        List of override dicts sorted by updated_at descending.
    """
    if source is not None:
        source = validate_source_name(source)

    overrides = _load_overrides(project_root)

    if source is not None:
        overrides = [o for o in overrides if o["source"] == source]

    overrides.sort(key=lambda o: o.get("updated_at", ""), reverse=True)
    return overrides


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
    overrides = _load_overrides(project_root)

    # Upsert: find existing or append
    found = False
    for o in overrides:
        if (
            o["source"] == source
            and o["table_name"] == table_name
            and o["column_name"] == column_name
        ):
            o["pii_status"] = pii_status
            o["set_by"] = set_by
            o["reason"] = reason
            o["updated_at"] = now
            found = True
            break
    if not found:
        overrides.append(
            {
                "source": source,
                "table_name": table_name,
                "column_name": column_name,
                "pii_status": pii_status,
                "set_by": set_by,
                "reason": reason,
                "updated_at": now,
            }
        )

    _save_overrides(project_root, overrides)


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

    overrides = _load_overrides(project_root)
    original_len = len(overrides)
    overrides = [
        o
        for o in overrides
        if not (
            o["source"] == source
            and o["table_name"] == table_name
            and o["column_name"] == column_name
        )
    ]
    if len(overrides) == original_len:
        return False
    _save_overrides(project_root, overrides)
    return True
