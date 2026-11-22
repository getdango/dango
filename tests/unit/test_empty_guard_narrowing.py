"""tests/unit/test_empty_guard_narrowing.py

A sync that selects fewer tables than an earlier one (narrowed `table_names`, endpoints or resources, a
removed config entry) must not be blocked by tables it cannot write, and must leave them byte-identical.
Real dlt + DuckDB through both `_run_dlt_native_source` and `_run_dlt_source`.
"""

from __future__ import annotations

import dlt
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
