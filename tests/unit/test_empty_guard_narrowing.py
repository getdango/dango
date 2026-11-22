"""tests/unit/test_empty_guard_narrowing.py

A sync that selects fewer tables than an earlier one (narrowed `table_names`, endpoints or resources, a
removed config entry) must not be blocked by tables it cannot write, and must leave them byte-identical.
Real dlt + DuckDB through both `_run_dlt_native_source` and `_run_dlt_source`.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import dlt
import duckdb
import pytest

from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, resource, snapshot, sync


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch, tmp_path):
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def rows(key, run_no, n=2):
    return [{"id": 10 * run_no + i, "v": f"{key}{run_no}{i}"} for i in range(n)]


def tables(selected, run_no, empty=(), disposition="replace"):
    """A source that defines only the `selected` tables (like a narrowed `table_names`)."""

    @dlt.source(name="multi")
    def _src():
        for key in selected:
            hints = {"primary_key": "id"} if disposition == "merge" else {}
            yield resource(key, [] if key in empty else rows(key, run_no), disposition, **hints)

    return _src()


def seeded(tmp_path, method, keys=("a", "b", "c"), disposition="replace"):
    runner = DltPipelineRunner(tmp_path)
    assert (
        sync(runner, method, "m", tables(keys, 1, disposition=disposition))["status"] == "success"
    )
    return runner, snapshot(runner, "m")


def untouched(before, after, keys):
    return {k: after[k] for k in keys} == {k: before[k] for k in keys}


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestNarrowedSelection:
    def test_selecting_one_of_three_is_not_blocked_and_leaves_the_rest_identical(
        self, tmp_path, method
    ):
        runner, before = seeded(tmp_path, method)
        result = sync(runner, method, "m", tables(["a"], 2))
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "m")
        assert untouched(before, after, ["b", "c"])
        assert [r[0] for r in after["a"]] == [20, 21]  # the selected table was rewritten

    def test_selecting_a_subset_is_not_blocked(self, tmp_path, method):
        runner, before = seeded(tmp_path, method, keys=("a", "b", "c", "d"))
        result = sync(runner, method, "m", tables(["b", "d"], 2))
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "m")
        assert untouched(before, after, ["a", "c"])
        assert [r[0] for r in after["b"]] == [20, 21] and [r[0] for r in after["d"]] == [20, 21]

    def test_a_table_removed_from_the_config_is_not_blocked(self, tmp_path, method):
        runner, before = seeded(tmp_path, method)
        result = sync(runner, method, "m", tables(["a", "b"], 2))
        assert result["status"] == "success", result.get("error")
        assert untouched(before, snapshot(runner, "m"), ["c"])

    def test_the_selected_table_going_empty_is_still_blocked_and_named_alone(
        self, tmp_path, method
    ):
        runner, before = seeded(tmp_path, method)
        result = sync(runner, method, "m", tables(["a"], 2, empty=["a"]))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "Sync would truncate 1 table(s)" in result["error"]
        assert "a: 2 rows" in result["error"]
        assert "b:" not in result["error"] and "c:" not in result["error"]
        assert snapshot(runner, "m") == before  # nothing was applied

    def test_the_allow_flag_empties_only_the_selected_table(self, tmp_path, method):
        runner, before = seeded(tmp_path, method)
        result = sync(runner, method, "m", tables(["a"], 2, empty=["a"]), allow_empty_replace=True)
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "m")
        assert after["a"] == []
        assert untouched(before, after, ["b", "c"])

    def test_unselected_empty_tables_do_not_hide_a_selected_one_that_has_data(
        self, tmp_path, method
    ):
        runner, before = seeded(tmp_path, method)
        result = sync(runner, method, "m", tables(["b"], 2))
        assert result["status"] == "success", result.get("error")
        assert untouched(before, snapshot(runner, "m"), ["a", "c"])

    def test_every_run_after_narrowing_keeps_working(self, tmp_path, method):
        runner, before = seeded(tmp_path, method)
        assert sync(runner, method, "m", tables(["a"], 2))["status"] == "success"
        full = sync(runner, method, "m", tables(["a", "b", "c"], 3))
        assert full["status"] == "success", full.get("error")
        after = snapshot(runner, "m")
        assert [[r[0] for r in after[k]] for k in "abc"] == [[30, 31]] * 3

    def test_a_chosen_table_that_never_held_rows_does_not_block_on_the_others(
        self, tmp_path, method
    ):
        """0 rows loaded in total, but the only table this run writes has nothing to lose."""
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "m", tables(["a", "b", "c"], 1, empty=["a"]))["status"] == (
            "success"
        )
        before = snapshot(runner, "m")
        result = sync(runner, method, "m", tables(["a"], 2, empty=["a"]))
        assert result["status"] == "success", result.get("error")
        assert result["rows_loaded"] == 0
        assert untouched(before, snapshot(runner, "m"), ["b", "c"])

    def test_a_zero_row_load_report_counts_and_names_only_the_chosen_tables(self, tmp_path, method):
        """The post-load 0-row check (reached when the load reports 0 rows) is scoped the same way."""
        runner, before = seeded(tmp_path, method)
        with patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}):
            result = sync(runner, method, "m", tables(["a"], 2))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "existing 2 rows preserved" in result["error"]
        assert "replace-mode table(s): a." in result["error"]
        assert untouched(before, snapshot(runner, "m"), ["b", "c"])

    def test_the_per_table_check_after_the_load_names_only_the_chosen_tables(
        self, tmp_path, method
    ):
        """A destination that ends up empty is caught for the tables this run writes, not for others."""
        runner, _ = seeded(tmp_path, method)
        real_load = runner._load_with_lock

        def _load_then_empty(*args, **kwargs):
            info = real_load(*args, **kwargs)
            con = duckdb.connect(str(runner.duckdb_path))
            try:
                for table in ("a", "b"):
                    con.execute(f'DELETE FROM "raw_m"."{table}"')
            finally:
                con.close()
            return info

        with patch.object(runner, "_load_with_lock", side_effect=_load_then_empty):
            result = sync(runner, method, "m", tables(["a"], 2))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "2 rows" in result["error"]
        assert re.search(r"\ba\b", result["error"]) and not re.search(r"\bb\b", result["error"])
