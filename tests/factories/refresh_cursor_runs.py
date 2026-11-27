"""tests/factories/refresh_cursor_runs.py

Sources and readers for the `--full-refresh` cursor tests: real dlt pipelines against a local DuckDB,
a source with an append/merge `events` table (optionally incremental) and a replace `dims` table.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import dlt
import duckdb

NAME = "ev"
DIMS = [{"id": 1}, {"id": 2}, {"id": 3}]


def events(
    count: int,
    *,
    disposition: str = "append",
    incremental: bool = True,
    key: bool = False,
    table: str = "events",
    updated: Callable[[int], Any] = lambda i: i,
    initial: Any = 0,
    tags: bool = False,
    on_row: Callable[[int], None] | None = None,
    store: list[int] | None = None,
) -> Any:
    """Resource `table` with ids 1..count (or the live list `store`); the cursor is `updated(id)`."""

    def rows() -> Iterable[dict[str, Any]]:
        for i in store if store is not None else range(1, count + 1):
            row: dict[str, Any] = {"id": i, "updated_at": updated(i)}
            if tags:
                row["tags"] = [{"t": f"{i}a"}, {"t": f"{i}b"}]
            yield row
            if on_row is not None:
                on_row(i)

    hints = {"primary_key": "id"} if key else {}

    if not incremental:

        @dlt.resource(name=table, write_disposition=disposition, **hints)
        def plain() -> Any:
            yield from rows()

        return plain

    @dlt.resource(name=table, write_disposition=disposition, **hints)
    def tracked(
        updated_at: Any = dlt.sources.incremental("updated_at", initial_value=initial),
    ) -> Any:
        yield from rows()

    return tracked


def dims(rows: list[dict[str, int]] | None = None) -> Any:
    data = DIMS if rows is None else rows

    @dlt.resource(name="dims", write_disposition="replace")
    def _dims() -> Any:
        yield from data

    return _dims


def source(*resources: Any) -> Any:
    @dlt.source(name=NAME)
    def _src() -> Any:
        yield from resources

    return _src()


def table_ids(runner: Any, table: str, column: str = "id") -> list[Any]:
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        return sorted(
            r[0] for r in con.execute(f'SELECT {column} FROM "raw_{NAME}"."{table}"').fetchall()
        )
    finally:
        con.close()


def table_names(runner: Any) -> list[str]:
    """Tables of `raw_ev` (dlt's own `_dlt_*` tables left out)."""
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            [f"raw_{NAME}"],
        ).fetchall()
    finally:
        con.close()
    return sorted(name for (name,) in rows if not name.startswith("_dlt_"))


def cursors() -> dict[str, Any]:
    """`{resource: incremental last_value}` in the real pipeline's local dlt state."""
    path = Path(os.environ["DLT_DATA_DIR"]) / "pipelines" / NAME / "state.json"
    if not path.exists():
        return {}
    resources = json.loads(path.read_text(encoding="utf-8")).get("sources", {}).get(NAME, {})
    return {
        name: state["incremental"]["updated_at"]["last_value"]
        for name, state in resources.get("resources", {}).items()
        if "incremental" in state
    }
