"""tests/factories/empty_guard_runs.py

Real dlt + DuckDB helpers for the empty-replace guard tests: run one sync through either sync method
and read the loaded tables back.
"""

from __future__ import annotations

import importlib as _real_importlib
import types
from typing import Any
from unittest.mock import patch

import dlt
import duckdb

from dango.config.models import DataSource, DltNativeConfig, SourceType

METHODS = ["native", "dlt"]


def resource(name, rows, disposition, **hints):
    @dlt.resource(name=name, write_disposition=disposition, **hints)
    def _gen():
        yield from rows

    return _gen()


def sync(runner, method, name, source, native=None, **kwargs) -> dict[str, Any]:
    """Run one sync of `source` through the chosen method (real dlt, real DuckDB).

    `native` adds fields (e.g. `pipeline_name`, `dataset_name`) to the dlt_native config.
    """
    if method == "dlt":
        config = DataSource(name=name, type=SourceType.CHESS)
        with (
            patch(
                "dango.ingestion.dlt_runner.get_source_metadata",
                return_value={"dlt_package": "fixture", "dlt_function": "fixture_func"},
            ),
            patch.object(runner, "_build_source_config", return_value={}),
            patch.object(runner, "_load_dlt_source", return_value=source),
            patch.object(runner, "_check_oauth_token_expiry", return_value=None),
            patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
        ):
            return runner._run_dlt_source(config, **kwargs)
    config = DataSource(
        name=name,
        type=SourceType.DLT_NATIVE,
        dlt_native=DltNativeConfig(
            source_module="fixture_module",
            source_function="make",
            function_kwargs={},
            **(native or {}),
        ),
    )
    module = types.SimpleNamespace(make=lambda: source)
    real_import = _real_importlib.import_module

    def _import(mod_name, *a, **k):
        return module if mod_name == "fixture_module" else real_import(mod_name, *a, **k)

    with patch("dango.ingestion.dlt_runner.importlib.import_module", side_effect=_import):
        return runner._run_dlt_native_source(config, **kwargs)


def ids(runner, name, table) -> list[int]:
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        return sorted(
            r[0] for r in con.execute(f'SELECT id FROM "raw_{name}"."{table}"').fetchall()
        )
    finally:
        con.close()


def count(runner, name, table) -> int:
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        return con.execute(f'SELECT COUNT(*) FROM "raw_{name}"."{table}"').fetchone()[0]
    finally:
        con.close()


def snapshot(runner, name) -> dict[str, list[tuple]]:
    """Every table of `raw_<name>` with all columns (incl. `_dlt_load_id`, `_dlt_id`), rows sorted."""
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        tables = [
            t
            for (t,) in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = ? "
                "AND table_name NOT LIKE '\\_dlt\\_%' ESCAPE '\\'",
                [f"raw_{name}"],
            ).fetchall()
        ]
        return {
            t: sorted(
                con.execute(f'SELECT * FROM "raw_{name}"."{t}"').fetchall(), key=lambda r: repr(r)
            )
            for t in tables
        }
    finally:
        con.close()
