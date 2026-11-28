"""dango/utils/orphan_scan.py

Finds the raw tables no configured source owns, and the raw schemas it leaves alone because their
owner is unclear. `dango db clean` drops only what this returns as orphaned. Read-only; local code
that runs on Windows, macOS and Linux.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import duckdb

from dango.utils.db_health import build_schema_table_mapping, is_table_configured

if TYPE_CHECKING:
    from dango.config.models import DangoConfig

__all__ = ["OrphanScan", "scan_orphans"]

_STATE_TABLE = "_dlt_pipeline_state"


class OrphanScan(NamedTuple):
    """`orphans`: (schema, table, size) to drop. `kept`: one line per schema left alone."""

    orphans: list[tuple[str, str, str]]
    kept: list[str]


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _state_names(conn: Any, schema: str) -> set[str] | None:
    """Pipeline names in ``<schema>._dlt_pipeline_state``; None when the table cannot be read."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT pipeline_name FROM query_table(?)",
            [f"{_quoted(schema)}.{_STATE_TABLE}"],
        ).fetchall()
    except Exception:
        return None
    return {str(row[0]) for row in rows if row[0]}


def _claimants(config: DangoConfig) -> dict[str, list[str]]:
    """Source names by the lower-case schema each may write to."""
    from dango.config.source_datasets import schemas_of

    claimed: dict[str, list[str]] = {}
    for source in config.sources.sources:
        for schema in schemas_of(source):
            if source.name not in claimed.setdefault(schema, []):
                claimed[schema].append(source.name)
    return claimed


def _kept_by_state(
    conn: Any, config: DangoConfig, candidates: dict[str, str], with_state: set[str]
) -> dict[str, str]:
    """Unclaimed schemas whose sync state names a configured pipeline (or cannot be read)."""
    from dango.config.source_datasets import pipeline_name_of

    owners = {
        name.casefold(): source.name
        for source in config.sources.sources
        if (name := pipeline_name_of(source))
    }
    kept: dict[str, str] = {}
    for key, schema in sorted(candidates.items()):
        if key not in with_state:
            continue
        names = _state_names(conn, schema)
        if names is None:
            kept[key] = f"Kept {schema}: its sync state could not be read, so its owner is unclear."
            continue
        owner = next((owners[n.casefold()] for n in sorted(names) if n.casefold() in owners), None)
        if owner:
            kept[key] = (
                f"Kept {schema}: its sync state belongs to the configured source '{owner}', "
                "which does not load into this schema now."
            )
    return kept


def scan_orphans(duckdb_path: Path, config: DangoConfig) -> OrphanScan:
    """Orphaned raw tables of the warehouse, and the schemas kept because their owner is unclear.

    A schema is orphaned only when no configured source (enabled or not) may write to it AND its
    sync state, if it has one, names no configured source's pipeline. A schema that two sources
    share, or whose state cannot be read, is kept and reported.
    """
    conn = duckdb.connect(str(duckdb_path), config={"access_mode": "read_only"})
    try:
        tables = conn.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema = 'raw' OR table_schema LIKE 'raw_%'
            ORDER BY table_schema, table_name
        """).fetchall()

        result: list[tuple[str, str, str]] = []
        for schema, table in tables:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM query_table(?)", [f"{_quoted(schema)}.{_quoted(table)}"]
                ).fetchone()
                count = row[0] if row else 0
                result.append((schema, table, f"{count:,} rows"))
            except Exception:
                result.append((schema, table, "Error"))

        schema_to_tables_map, source_to_schema_map = build_schema_table_mapping(config)

        actual_raw_tables: dict[str, set[str]] = {}
        for schema, table, _ in result:
            if schema.startswith("raw_") and not table.startswith("_dlt_"):
                actual_raw_tables.setdefault(schema, set()).add(table)

        unowned = [
            row
            for row in result
            if not is_table_configured(
                row[0], row[1], schema_to_tables_map, source_to_schema_map, actual_raw_tables
            )
        ]
        candidates = {schema.casefold(): schema for schema, _t, _s in unowned}
        with_state = {schema.casefold() for schema, table, _s in result if table == _STATE_TABLE}
        kept = _kept_by_state(conn, config, candidates, with_state)
        for key in sorted(candidates):  # a staging schema follows the schema it belongs to
            if key.endswith("_staging") and key.removesuffix("_staging") in kept:
                kept.setdefault(key, kept[key.removesuffix("_staging")])

        for schema_key, names in sorted(_claimants(config).items()):
            present = next((s for s, _t, _z in result if s.casefold() == schema_key), None)
            if present and len(names) > 1:
                kept[schema_key] = (
                    f"Kept {present}: more than one source ({', '.join(names)}) writes to it."
                )

        orphans = [row for row in unowned if row[0].casefold() not in kept]
        reasons = sorted(set(kept.values()))  # a staging schema repeats its schema's line
        return OrphanScan(orphans, reasons)
    finally:
        conn.close()
