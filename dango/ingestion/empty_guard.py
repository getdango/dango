"""dango/ingestion/empty_guard.py

Per-table scoping for the empty-replace guard: which tables of a source the guard protects, and the
wording of the failure messages. Pure helpers, no I/O; the checks themselves stay in dlt_runner.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Value recorded for a resource that is not selected for this run: its table is never written, so
# no empty result can erase it.
_UNTOUCHED = "untouched"


def _disposition_name(value: Any) -> str | None:
    """Name of a write disposition given as ``"merge"`` or ``{"disposition": "merge", ...}``."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("disposition")
        return name if isinstance(name, str) else None
    return None


def resource_dispositions(source: Any) -> dict[str, str | None]:
    """``{table name: write disposition}`` for the resources of *source* with a fixed table name.

    A resource whose table name is computed per row is left out (the schema answers for it). A
    resource that is not selected for this run maps to ``"untouched"``. A disposition that cannot
    be read maps to ``None`` (treated as protected by the caller). An unrecognised source shape
    gives an empty mapping.
    """
    dispositions: dict[str, str | None] = {}
    try:
        resources = source.resources.values()
        for resource in resources:
            table_name = resource.table_name
            if not isinstance(table_name, str):
                continue
            if not getattr(resource, "selected", True):
                dispositions[table_name] = _UNTOUCHED
            else:
                dispositions[table_name] = _disposition_name(resource.write_disposition)
    except Exception:
        # Unreadable source: the guard falls back to protecting every table.
        return {}
    return dispositions


def _schema_tables(pipeline: Any) -> Mapping[str, Any]:
    """The pipeline's default schema tables, or an empty mapping if they cannot be read."""
    try:
        tables = pipeline.default_schema.tables
    except Exception:
        # A missing or unreadable schema just means "not known yet".
        return {}
    return tables if isinstance(tables, Mapping) else {}


def _resolve(
    table_name: str,
    hints: Mapping[str, str | None],
    tables: Mapping[str, Any],
) -> str | None:
    """Write disposition of *table_name*: its resource's hint, else the schema, else its parent's."""
    seen: set[str] = set()
    current: str | None = table_name
    while current is not None and current not in seen:
        seen.add(current)
        if current in hints:
            return hints[current]
        table = tables.get(current)
        if not isinstance(table, Mapping):
            return None
        disposition = _disposition_name(table.get("write_disposition"))
        if disposition is not None:
            return disposition
        parent = table.get("parent")
        current = parent if isinstance(parent, str) else None
    return None


def replace_protected_tables(source: Any, pipeline: Any, candidates: Iterable[str]) -> set[str]:
    """The *candidates* the empty-replace guard protects: replace-mode tables of this run.

    A table is protected when its own write disposition is ``replace`` or when it cannot be
    determined (no resource and no schema entry names it), so an unknown table is never less safe
    than before. Merge and append tables, and tables of resources not selected for this run,
    are not protected: an empty result for them never erases data.
    """
    hints = resource_dispositions(source)
    tables = _schema_tables(pipeline)
    return {name for name in candidates if _resolve(name, hints, tables) in (None, "replace")}


def truncate_message(source_name: str, truncated: list[tuple[str, int]]) -> str:
    """Failure text when replace-mode tables with existing data would be emptied."""
    details = "\n  - ".join(
        f"{name}: {count:,} rows → 0 rows (table would be lost)" for name, count in truncated
    )
    return (
        f"Sync would truncate {len(truncated)} table(s) with existing data:\n  - {details}\n"
        f"Replace-mode tables are rewritten on every sync, so an empty result would erase them; "
        f"other tables of this source are not affected by this check.\n"
        f"To force sync with empty data, use: dango sync {source_name} --allow-empty-replace"
    )


def zero_rows_message(source_name: str, existing_rows: int, tables: list[str] | None) -> str:
    """Failure text when a sync loaded 0 rows while replace-mode tables hold *existing_rows*."""
    scope = f" in replace-mode table(s): {', '.join(sorted(tables))}" if tables else ""
    return (
        f"Sync returned 0 rows — existing {existing_rows:,} rows preserved{scope}. "
        f"To force sync with empty data, use: dango sync {source_name} --allow-empty-replace"
    )
