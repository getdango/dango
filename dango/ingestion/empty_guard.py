"""dango/ingestion/empty_guard.py

Per-table scoping for the empty-replace guard: which tables of a source the guard protects, and the
wording of the failure messages. Pure helpers, no I/O; the checks themselves stay in dlt_runner.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Value recorded for a resource that is not selected for this run: its table is never written, so
# no empty result can erase it.
_UNTOUCHED = "untouched"


def disposition_name(value: Any) -> str | None:
    """Name of a write disposition given as ``"merge"`` or ``{"disposition": "merge", ...}``."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("disposition")
        return name if isinstance(name, str) else None
    return None


@dataclass(frozen=True)
class _RunShape:
    """What the resources of one run say about the tables it can write."""

    hints: dict[str, str | None]  # fixed table name -> disposition ("untouched" if never written)
    selected: dict[str, str | None]  # name of each selected resource -> its disposition
    computed: bool  # a selected resource names its table per row


def _inspect(source: Any) -> _RunShape | None:
    """Read the resources of *source*; ``None`` if its shape is not recognised."""
    selected_values: dict[str, list[str | None]] = {}
    unselected: set[str] = set()
    selected: dict[str, str | None] = {}
    computed = False
    try:
        for key, resource in source.resources.items():
            table_name = resource.table_name
            if not getattr(resource, "selected", True):
                if isinstance(table_name, str):
                    unselected.add(table_name)
                continue
            value = disposition_name(resource.write_disposition)
            selected[getattr(resource, "name", key)] = value
            if isinstance(table_name, str):
                selected_values.setdefault(table_name, []).append(value)
            else:
                computed = True
    except Exception:
        # Unreadable source: the guard falls back to protecting every table it cannot place.
        return None
    hints: dict[str, str | None] = dict.fromkeys(unselected, _UNTOUCHED)
    for table_name, values in selected_values.items():
        # Several selected resources can write one table: an undetermined or replace
        # disposition wins, and a selected resource always beats an unselected one.
        if None in values:
            hints[table_name] = None
        elif "replace" in values:
            hints[table_name] = "replace"
        else:
            hints[table_name] = values[-1]
    return _RunShape(hints, selected, computed)


def resource_dispositions(source: Any) -> dict[str, str | None]:
    """``{table name: write disposition}`` for the resources of *source* with a fixed table name.

    A resource whose table name is computed per row is left out (the schema answers for it). A
    resource that is not selected for this run maps to ``"untouched"``. A disposition that cannot
    be read maps to ``None`` (treated as protected by the caller). An unrecognised source shape
    gives an empty mapping.
    """
    shape = _inspect(source)
    return {} if shape is None else shape.hints


def _schema_tables(source: Any, pipeline: Any) -> Mapping[str, Any] | None:
    """Tables of the schema dlt extracts *source* with, else the default schema; ``None`` if unreadable.

    A pipeline holds one schema per source name (a source renamed between runs, or two sources
    sharing a pipeline, leave several), and dlt extracts into the one named after the source.
    """
    try:
        try:
            tables = pipeline.schemas[source.schema.name].tables
        except Exception:
            tables = pipeline.default_schema.tables
    except Exception:
        # A missing or unreadable schema means we cannot tell which resource wrote a table.
        return None
    return tables if isinstance(tables, Mapping) else None


def _lineage(table_name: str, tables: Mapping[str, Any]) -> list[str]:
    """*table_name*, then its parent, then that parent's parent, up to the root table."""
    chain: list[str] = []
    current: str | None = table_name
    while current is not None and current not in chain:
        chain.append(current)
        table = tables.get(current)
        parent = table.get("parent") if isinstance(table, Mapping) else None
        current = parent if isinstance(parent, str) else None
    return chain


def _schema_disposition(chain: list[str], tables: Mapping[str, Any]) -> str | None:
    """Write disposition the schema records for a table or its nearest parent, else ``None``."""
    for name in chain:
        table = tables.get(name)
        if not isinstance(table, Mapping):
            return None
        disposition = disposition_name(table.get("write_disposition"))
        if disposition is not None:
            return disposition
    return None


def _resolve(
    table_name: str,
    shape: _RunShape | None,
    tables: Mapping[str, Any] | None,
) -> str | None:
    """Write disposition this run applies to *table_name*, ``"untouched"`` if it cannot write it.

    ``None`` means undetermined (the caller protects it). Order of evidence: the fixed table name
    of a resource (the table or one of its parents), then the resource that the schema records as
    the table's writer: a selected one decides, any other leaves the table untouched. A table with
    neither is untouched, unless this run cannot tell what it writes (a computed table name, an
    unreadable source or schema): then the schema's own disposition decides and a table the schema
    does not know stays protected.
    """
    chain = _lineage(table_name, tables or {})
    owner = None
    for name in chain:
        table = (tables or {}).get(name)
        if isinstance(table, Mapping) and isinstance(table.get("resource"), str):
            owner = table["resource"]
            break
    if shape is not None:
        for name in chain:
            if name in shape.hints:
                value = shape.hints[name]
                # An unselected resource naming the table does not hide a selected writer.
                if value == _UNTOUCHED and owner in shape.selected:
                    return shape.selected[owner]
                return value
        if owner in shape.selected:
            return shape.selected[owner]
        # The schema names another resource as the table's writer: dlt empties only the tables of
        # selected resources, so this run cannot. A table with no known writer is untouched only
        # when no selected resource computes its table names.
        if owner is not None or (not shape.computed and tables is not None):
            return _UNTOUCHED
    return _schema_disposition(chain, tables or {})


def replace_protected_tables(source: Any, pipeline: Any, candidates: Iterable[str]) -> set[str]:
    """The *candidates* the empty-replace guard protects: replace-mode tables this run can write.

    A table is protected when a selected resource of this run writes it (by fixed table name, as
    the writer the schema records, or as a nested table of one) and its write disposition is
    ``replace`` or cannot be read. Merge and append tables are not protected, and neither is a
    table that no selected resource can write (a deselected or removed resource, a narrowed
    selection): dlt rewrites only tables that are part of the load. When the run cannot tell what
    it writes (a computed table name, an unreadable source or schema) a table with no known writer
    is protected unless the schema shows it is not replace-mode, so that case is never less safe
    than before.
    """
    shape = _inspect(source)
    tables = _schema_tables(source, pipeline)
    return {name for name in candidates if _resolve(name, shape, tables) in (None, "replace")}


def tables_run_can_write(source: Any, pipeline: Any, candidates: Iterable[str]) -> set[str]:
    """The *candidates* a run of *source* can write or empty, whatever their write disposition.

    The complement is left alone by the run: tables of resources that are not selected, of removed
    resources, and tables that no resource of the source writes. Undetermined tables count as
    writable, so the answer is never narrower than the one ``replace_protected_tables`` uses.
    """
    shape = _inspect(source)
    tables = _schema_tables(source, pipeline)
    return {name for name in candidates if _resolve(name, shape, tables) != _UNTOUCHED}


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
