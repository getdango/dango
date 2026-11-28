"""tests/factories/custom_dataset_projects.py

Scratch projects whose sources load into a custom dataset (real dlt pipelines under a throwaway
HOME), planted warehouse schemas, and readers for the `dango db clean` dataset-ownership tests.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from click.testing import CliRunner

from dango.config import get_config
from dango.utils.db_health import find_orphaned_tables
from dango.validation import quote_identifier as q
from tests.factories.dlt_state_projects import entry, run_sync

_SYSTEM = ("information_schema", "pg_catalog")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def warehouse(root: Path) -> Path:
    return root / "data" / "warehouse.duckdb"


def synced_into(root: Path, name: str, rows: int, dataset: str | None) -> None:
    """One real sync of events 1..rows as source `name`, into `dataset` (None = the default)."""
    native = {"dataset_name": dataset} if dataset else None
    result = run_sync(root, "native", name, rows, native=native)
    assert result["status"] == "success", result


def tables_by_schema(root: Path) -> dict[str, list[str]]:
    """Every table in the warehouse by (lowercase) schema; empty when there is no warehouse."""
    if not warehouse(root).exists():
        return {}
    con = duckdb.connect(str(warehouse(root)), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "ORDER BY table_schema, table_name"
        ).fetchall()
    finally:
        con.close()
    found: dict[str, list[str]] = {}
    for schema, table in rows:
        if schema not in _SYSTEM:
            found.setdefault(schema.lower(), []).append(table)
    return found


def ids_in(root: Path, schema: str, table: str = "events") -> list[int] | str:
    """Sorted ids of `<schema>.<table>`, or the DuckDB error class when it cannot be read."""
    con = duckdb.connect(str(warehouse(root)), read_only=True)
    try:
        return sorted(r[0] for r in con.execute(f'SELECT id FROM "{schema}"."{table}"').fetchall())
    except duckdb.Error as exc:
        return f"<{type(exc).__name__}>"
    finally:
        con.close()


def plant(
    root: Path,
    schema: str,
    tables: Iterable[str] = ("t",),
    *,
    state: str | None = None,
    state_sql: str | None = None,
) -> None:
    """A schema with one-row tables, and (optionally) a `_dlt_pipeline_state` naming `state`.

    `state_sql` plants a state table as given (for a table that cannot be read as a state table).
    """
    warehouse(root).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(warehouse(root)))
    try:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {q(schema)}")
        for table in tables:
            con.execute(f"CREATE TABLE {q(schema)}.{q(table)} AS SELECT 1 AS id")
        if state is not None:
            con.execute(
                f"CREATE TABLE {q(schema)}._dlt_pipeline_state AS SELECT ? AS pipeline_name",
                [state],
            )
        if state_sql is not None:
            con.execute(f"CREATE TABLE {q(schema)}._dlt_pipeline_state AS {state_sql}")
    finally:
        con.close()


def orphans(root: Path) -> list[tuple[str, str]]:
    """(schema, table) the orphan detection lists for the project in `root`."""
    found = find_orphaned_tables(warehouse(root), get_config(root))
    return [(schema, table) for schema, table, _size in found]


def orphan_schemas(root: Path) -> set[str]:
    return {schema for schema, _table in orphans(root)}


def sources_of(**datasets: Any) -> list[dict[str, Any]]:
    """dlt_native source entries `name=dataset_name` (None = default dataset)."""
    return [entry(name, **({"dataset_name": ds} if ds else {})) for name, ds in datasets.items()]


def db_status(root: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`dango db status` in `root` through the real CLI."""
    from dango.cli.main import cli

    monkeypatch.chdir(root)
    monkeypatch.setenv("COLUMNS", "300")
    result = CliRunner().invoke(cli, ["db", "status"], catch_exceptions=True)
    return SimpleNamespace(
        exit_code=result.exit_code, output=_ANSI.sub("", result.output), exception=result.exception
    )
