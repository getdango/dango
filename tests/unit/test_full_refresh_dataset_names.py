"""tests/unit/test_full_refresh_dataset_names.py

A staged `--full-refresh` must work for every dataset name, including the ones dlt rewrites when it
writes them (`raw-dash` -> `raw_dash`, `x_` -> `xx`): swap, row counts, empty-replace guard and the
cleanup of its own staging schemas all use the schema dlt really wrote. Real dlt + DuckDB, both
sync methods, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest

from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from tests.factories.empty_guard_runs import sync
from tests.factories.refresh_cursor_runs import events, source

pytestmark = pytest.mark.unit

# (id, method, source name, configured dataset_name or None, the schema dlt writes)
CASES = [
    ("native-default", "native", "ev", None, "raw_ev"),
    ("native-hyphen", "native", "ev", "raw-dash", "raw_dash"),
    ("native-mixed-case", "native", "ev", "Raw_Custom", "raw_custom"),
    ("native-camel-case", "native", "ev", "CamelCase", "camel_case"),
    ("native-trailing-underscore", "native", "ev", "x_", "xx"),
    ("dlt-default", "dlt", "ev", None, "raw_ev"),
    ("dlt-trailing-underscore", "dlt", "ev_", None, "raw_evx"),
]


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def run(
    runner: DltPipelineRunner, case: tuple[Any, ...], src: Any, **kwargs: Any
) -> dict[str, Any]:
    _id, method, name, dataset, _written = case
    native = {"dataset_name": dataset} if dataset else None
    return sync(runner, method, name, src, native=native, **kwargs)


def ids_in(runner: DltPipelineRunner, schema: str, table: str = "events") -> list[int]:
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        return sorted(r[0] for r in con.execute(f'SELECT id FROM "{schema}"."{table}"').fetchall())
    finally:
        con.close()


def schemas(runner: DltPipelineRunner) -> list[str]:
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        return sorted(
            s
            for (s,) in con.execute(
                "SELECT schema_name FROM information_schema.schemata"
            ).fetchall()
            if s not in ("information_schema", "main", "pg_catalog", "temp", "system")
        )
    finally:
        con.close()


def upto(n: int) -> list[int]:
    return list(range(1, n + 1))


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
class TestStagedRefreshOfAnyDatasetName:
    def test_the_refresh_swaps_in_the_new_rows_and_the_next_sync_adds_only_newer_ones(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        written = case[4]
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, source(events(5)))["status"] == "success"
        assert ids_in(runner, written) == upto(5)

        refreshed = run(runner, case, source(events(8)), full_refresh=True)
        assert refreshed["status"] == "success", refreshed.get("error")
        assert refreshed["rows_loaded"] == 8
        assert ids_in(runner, written) == upto(8)
        assert schemas(runner) == [written]  # no staging schema is left behind

        assert run(runner, case, source(events(10)))["status"] == "success"
        assert ids_in(runner, written) == upto(
            10
        )  # the cursor moved with the refresh: no duplicates

    def test_a_refresh_that_would_empty_the_table_is_blocked_and_nothing_changes(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        written = case[4]
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, source(events(5)))["status"] == "success"

        blocked = run(runner, case, source(events(0)), full_refresh=True)

        assert blocked["status"] == "failed"
        assert blocked["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "staged 0 rows" in blocked["error"]
        assert ids_in(runner, written) == upto(5)
        assert schemas(runner) == [written]

    def test_a_table_that_returns_no_rows_is_emptied_not_dropped_when_allowed(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        written = case[4]
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, source(events(5), events(3, table="b")))["status"] == "success"

        refreshed = run(
            runner,
            case,
            source(events(8), events(0, table="b")),
            full_refresh=True,
            allow_empty_replace=True,
        )

        assert refreshed["status"] == "success", refreshed.get("error")
        assert ids_in(runner, written) == upto(8)
        assert ids_in(runner, written, "b") == []
        plain = run(runner, case, source(events(10), events(3, table="b")))
        assert plain["status"] == "success", plain.get("error")
        assert ids_in(runner, written) == upto(10)

    def test_a_failed_staged_load_leaves_the_data_and_no_staging_schema(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        written = case[4]
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, source(events(5)))["status"] == "success"

        with patch.object(DltPipelineRunner, "_load_with_lock", side_effect=RuntimeError("down")):
            failed = run(runner, case, source(events(8)), full_refresh=True)

        assert failed["status"] == "failed"
        assert ids_in(runner, written) == upto(5)
        assert schemas(runner) == [written]

    def test_staging_schemas_an_earlier_crashed_refresh_left_are_cleared_first(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        written = case[4]
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, source(events(5)))["status"] == "success"
        con = duckdb.connect(str(runner.duckdb_path))
        try:
            for leftover in (
                f"{written}_fullrefresh_staging",
                f"{written}_fullrefresh_staging_staging",
            ):
                con.execute(f'CREATE SCHEMA "{leftover}"')
                con.execute(f'CREATE TABLE "{leftover}"."events" AS SELECT 999 AS id')
        finally:
            con.close()

        refreshed = run(runner, case, source(events(8)), full_refresh=True)

        assert refreshed["status"] == "success", refreshed.get("error")
        assert ids_in(runner, written) == upto(8)  # the planted row never reached the real table
        assert schemas(runner) == [written]
