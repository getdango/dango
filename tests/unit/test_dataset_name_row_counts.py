"""tests/unit/test_dataset_name_row_counts.py

The row counts the empty-replace guard compares before and after a load are read from the schema dlt
really wrote, so a replace table in a dataset whose name dlt rewrites (`raw-dash` -> `raw_dash`,
`raw_ev_` -> `raw_evx`) is protected like any other. Real dlt + DuckDB, both sync methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    ("native-trailing-underscore", "native", "ev", "x_", "xx"),
    ("dlt-trailing-underscore", "dlt", "ev_", None, "raw_evx"),
]


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def run(runner: DltPipelineRunner, case: tuple[Any, ...], count: int, **kwargs: Any) -> Any:
    _id, method, name, dataset, _written = case
    native = {"dataset_name": dataset} if dataset else None
    src = source(events(count, disposition="replace", incremental=False))
    return sync(runner, method, name, src, native=native, **kwargs)


def ids_in(runner: DltPipelineRunner, schema: str) -> list[int]:
    con = duckdb.connect(str(runner.duckdb_path), read_only=True)
    try:
        rows = con.execute(f'SELECT id FROM "{schema}"."events"').fetchall()
        return sorted(r[0] for r in rows)
    finally:
        con.close()


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
class TestReplaceTableOfAnyDatasetName:
    def test_a_sync_that_returns_no_rows_is_blocked_and_the_table_keeps_its_rows(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, 5)["status"] == "success"

        blocked = run(runner, case, 0)

        assert blocked["status"] == "failed"
        assert blocked["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert ids_in(runner, case[4]) == [1, 2, 3, 4, 5]

    def test_the_same_sync_with_allow_empty_replace_empties_it(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, 5)["status"] == "success"

        allowed = run(runner, case, 0, allow_empty_replace=True)

        assert allowed["status"] == "success", allowed.get("error")
        assert ids_in(runner, case[4]) == []

    def test_a_sync_with_new_rows_replaces_the_old_ones(
        self, tmp_path: Path, case: tuple[Any, ...]
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, case, 5)["status"] == "success"

        again = run(runner, case, 3)

        assert again["status"] == "success", again.get("error")
        assert again["rows_loaded"] == 3
        assert ids_in(runner, case[4]) == [1, 2, 3]
