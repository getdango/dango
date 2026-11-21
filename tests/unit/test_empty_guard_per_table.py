"""tests/unit/test_empty_guard_per_table.py

The empty-replace guard protects only replace-mode tables: real dlt + DuckDB runs of mixed sources
(replace + merge/append) through both `_run_dlt_native_source` and `_run_dlt_source`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import dlt
import duckdb
import pytest

from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, count, ids, resource, sync


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch, tmp_path):
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def mixed(dim, fact, fact_disposition="merge"):
    """A source with a replace table `dim` and a merge (or append) table `fact`."""
    hints = {"primary_key": "id"} if fact_disposition == "merge" else {}

    @dlt.source(name="mixed")
    def _src():
        yield resource("dim", dim, "replace")
        yield resource("fact", fact, fact_disposition, **hints)

    return _src()


def single(disposition, rows):
    hints = {"primary_key": "id"} if disposition == "merge" else {}

    @dlt.source(name="single")
    def _src():
        yield resource("t", rows, disposition, **hints)

    return _src()


def seeded(tmp_path, method, name="mx"):
    """A runner whose first sync loaded dim=[1, 2] (replace) and fact=[1, 2, 3] (merge)."""
    runner = DltPipelineRunner(tmp_path)
    first = sync(
        runner, method, name, mixed([{"id": 1}, {"id": 2}], [{"id": i} for i in (1, 2, 3)])
    )
    assert first["status"] == "success"
    return runner


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestMixedSource:
    def test_merge_table_without_new_rows_is_not_blocked(self, tmp_path, method):
        runner = seeded(tmp_path, method)
        result = sync(runner, method, "mx", mixed([{"id": 7}, {"id": 8}], []))
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "mx", "dim") == [7, 8]  # replace table rewritten
        assert ids(runner, "mx", "fact") == [1, 2, 3]  # merge table untouched

    def test_append_table_without_new_rows_is_not_blocked(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        first = sync(runner, method, "ap", mixed([{"id": 1}], [{"id": 1}, {"id": 2}], "append"))
        assert first["status"] == "success"
        result = sync(runner, method, "ap", mixed([{"id": 5}], [], "append"))
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "ap", "dim") == [5]
        assert ids(runner, "ap", "fact") == [1, 2]

    def test_empty_replace_table_blocks_whole_run_without_partial_load(self, tmp_path, method):
        runner = seeded(tmp_path, method)
        result = sync(runner, method, "mx", mixed([], [{"id": 4}, {"id": 5}]))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert result["rows_loaded"] == 0
        assert "dim: 2 rows" in result["error"]
        assert "fact" not in result["error"].replace("--allow-empty-replace", "")
        # Nothing was applied: the merge table did not receive its new rows either.
        assert ids(runner, "mx", "dim") == [1, 2]
        assert ids(runner, "mx", "fact") == [1, 2, 3]
        # The next sync with data retries from the same state and loads everything.
        retry = sync(runner, method, "mx", mixed([{"id": 9}], [{"id": 4}]))
        assert retry["status"] == "success"
        assert ids(runner, "mx", "dim") == [9]
        assert ids(runner, "mx", "fact") == [1, 2, 3, 4]

    def test_both_empty_names_only_the_replace_table(self, tmp_path, method):
        runner = seeded(tmp_path, method)
        result = sync(runner, method, "mx", mixed([], []))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "Sync would truncate 1 table(s)" in result["error"]
        assert "dim: 2 rows → 0 rows" in result["error"]
        assert "fact:" not in result["error"]
        assert "--allow-empty-replace" in result["error"]
        assert (count(runner, "mx", "dim"), count(runner, "mx", "fact")) == (2, 3)

    def test_allow_flag_still_overrides(self, tmp_path, method):
        runner = seeded(tmp_path, method)
        result = sync(runner, method, "mx", mixed([], [{"id": 4}]), allow_empty_replace=True)
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "mx", "fact") == [1, 2, 3, 4]

    def test_first_run_with_empty_replace_table_loads(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        result = sync(runner, method, "fresh", mixed([], [{"id": 1}]))
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "fresh", "fact") == [1]

    def test_only_merge_table_has_data_and_run_is_empty(self, tmp_path, method):
        """Replace table was never populated; the merge table's rows are not at risk."""
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "mo", mixed([], [{"id": 1}, {"id": 2}]))["status"] == "success"
        result = sync(runner, method, "mo", mixed([], []))
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "mo", "fact") == [1, 2]


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestPureSources:
    def test_pure_replace_zero_rows_is_blocked_and_data_kept(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "pr", single("replace", [{"id": 1}, {"id": 2}]))["status"] == (
            "success"
        )
        result = sync(runner, method, "pr", single("replace", []))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "t: 2 rows → 0 rows" in result["error"]
        assert ids(runner, "pr", "t") == [1, 2]

    def test_pure_replace_allow_flag_overrides(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "pa", single("replace", [{"id": 1}]))["status"] == "success"
        result = sync(runner, method, "pa", single("replace", []), allow_empty_replace=True)
        assert result["status"] == "success", result.get("error")

    def test_pure_merge_without_new_rows_is_unaffected(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "pm", single("merge", [{"id": 1}]))["status"] == "success"
        result = sync(runner, method, "pm", single("merge", []))
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "pm", "t") == [1]


def _nested(dim, facts):
    @dlt.source(name="nested")
    def _src():
        yield resource("dim", dim, "replace")
        yield resource("fact", facts, "merge", primary_key="id")

    return _src()


def _dynamic(dim, facts):
    @dlt.resource(
        name="fact",
        write_disposition="merge",
        primary_key="id",
        table_name=lambda item: "dyn_" + item["k"],
    )
    def _fact():
        yield from facts

    @dlt.source(name="dynamic")
    def _src():
        yield resource("dim", dim, "replace")
        yield _fact

    return _src()


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestTableShapes:
    def test_child_table_of_merge_resource_follows_its_parent(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        facts = [{"id": 1, "items": [{"x": 1}, {"x": 2}]}]
        assert sync(runner, method, "nt", _nested([{"id": 1}], facts))["status"] == "success"
        assert count(runner, "nt", "fact__items") == 2
        result = sync(runner, method, "nt", _nested([{"id": 2}], []))
        assert result["status"] == "success", result.get("error")
        assert count(runner, "nt", "fact__items") == 2

    def test_computed_table_name_resolves_through_the_schema(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert (
            sync(runner, method, "dy", _dynamic([{"id": 1}], [{"id": 1, "k": "a"}]))["status"]
            == "success"
        )
        result = sync(runner, method, "dy", _dynamic([{"id": 2}], []))
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "dy", "dyn_a") == [1]

    def test_unknown_table_stays_protected(self, tmp_path, method):
        """Computed table name and no local schema: the table is protected, as before."""
        runner = DltPipelineRunner(tmp_path)
        assert (
            sync(runner, method, "du", _dynamic([{"id": 1}], [{"id": 1, "k": "a"}]))["status"]
            == "success"
        )
        shutil.rmtree(Path(tmp_path) / "dlt_data" / "pipelines" / "du")
        result = sync(runner, method, "du", _dynamic([{"id": 2}], []))
        assert result["status"] == "failed"
        assert "dyn_a: 1 rows → 0 rows" in result["error"]
        assert ids(runner, "du", "dyn_a") == [1]

    def test_unselected_replace_resource_is_not_protected(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "us", mixed([{"id": 1}], [{"id": 1}]))["status"] == "success"
        source = mixed([{"id": 2}], [{"id": 2}]).with_resources("fact")
        result = sync(runner, method, "us", source)
        assert result["status"] == "success", result.get("error")
        assert ids(runner, "us", "dim") == [1]
        assert ids(runner, "us", "fact") == [1, 2]


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestPostLoadChecks:
    """Backstops after the load: a destination that ends up empty is still caught per table."""

    @staticmethod
    def _empty_after_load(runner, tables, rows_loaded=None):
        real_load = runner._load_with_lock
        real_stats = runner._extract_load_stats

        def _load(*args, **kwargs):
            info = real_load(*args, **kwargs)
            con = duckdb.connect(str(runner.duckdb_path))
            try:
                for table in tables:
                    con.execute(f'DELETE FROM "raw_mx"."{table}"')
            finally:
                con.close()
            return info

        def _stats(info):
            stats = real_stats(info)
            return {**stats, "rows_loaded": rows_loaded} if rows_loaded is not None else stats

        return (
            patch.object(runner, "_load_with_lock", side_effect=_load),
            patch.object(runner, "_extract_load_stats", side_effect=_stats),
        )

    def test_zero_rows_loaded_blocks_when_replace_table_emptied(self, tmp_path, method):
        runner = seeded(tmp_path, method)
        load, stats = self._empty_after_load(runner, ["dim"], rows_loaded=0)
        with load, stats:
            result = sync(runner, method, "mx", mixed([{"id": 5}], [{"id": 4}]))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "existing 2 rows preserved" in result["error"]
        assert "dim" in result["error"]

    def test_zero_rows_loaded_does_not_block_for_merge_rows_only(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "mx", mixed([], [{"id": 1}]))["status"] == "success"
        with patch.object(runner, "_extract_load_stats", return_value={"rows_loaded": 0}):
            result = sync(runner, method, "mx", mixed([], []))
        assert result["status"] == "success", result.get("error")

    def test_per_table_check_names_only_the_replace_table(self, tmp_path, method):
        runner = seeded(tmp_path, method)
        load, stats = self._empty_after_load(runner, ["dim", "fact"], rows_loaded=3)
        with load, stats:
            result = sync(runner, method, "mx", mixed([{"id": 5}], [{"id": 4}]))
        assert result["status"] == "failed"
        assert "Sync would truncate 1 table(s)" in result["error"]
        assert "dim: 2 rows → 0 rows" in result["error"]
        assert "fact:" not in result["error"]
