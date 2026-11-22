"""tests/unit/test_empty_guard_schemas.py

The guard must read the schema dlt extracts the source with, not the pipeline's first schema: a source
renamed between runs, or two sources sharing a pipeline, leave several schemas in one pipeline. Real
dlt + DuckDB through both `_run_dlt_native_source` and `_run_dlt_source`.
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


def blocked(result):
    return (
        result["status"] == "failed" and result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    )


def dispatch(schema_name, run_no, keys):
    """Replace resource `disp` writing each key to its own table through `with_table_name`."""

    @dlt.resource(name="disp", write_disposition="replace")
    def disp():
        for key in keys:
            yield dlt.mark.with_table_name({"id": run_no, "k": key}, "disp_" + key)

    @dlt.source(name=schema_name)
    def _src():
        yield disp
        yield resource("other", [{"id": run_no}], "replace")

    return _src()


def nested(schema_name, run_no, with_items):
    """Replace parent `p`; its child table `p__items` exists only when `with_items`."""
    items = [{"x": run_no}, {"x": run_no + 1}] if with_items else []

    @dlt.source(name=schema_name)
    def _src():
        yield resource("p", [{"id": run_no, "items": items}], "replace")

    return _src()


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestSourceRenamedBetweenRuns:
    def test_a_dispatched_table_only_in_the_new_schema_is_still_protected(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "ren", dispatch("sa", 1, ("a",)))["status"] == "success"
        assert sync(runner, method, "ren", dispatch("sb", 2, ("a", "b")))["status"] == "success"
        before = snapshot(runner, "ren")
        assert "disp_b" in before
        result = sync(runner, method, "ren", dispatch("sb", 3, ("a",)))
        assert blocked(result)
        assert "disp_b: 1 rows" in result["error"] and "other:" not in result["error"]
        assert snapshot(runner, "ren") == before

    def test_a_new_nested_table_only_in_the_new_schema_is_still_protected(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "ren", nested("sa", 1, False))["status"] == "success"
        assert sync(runner, method, "ren", nested("sb", 2, True))["status"] == "success"
        before = snapshot(runner, "ren")
        assert "p__items" in before
        result = sync(runner, method, "ren", nested("sb", 3, False))
        assert blocked(result)  # dlt empties `p__items` while rewriting `p`
        assert "p__items: 2 rows" in result["error"]
        assert snapshot(runner, "ren") == before

    def test_narrowing_after_a_rename_still_leaves_other_tables_untouched(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "ren", dispatch("sa", 1, ("a",)))["status"] == "success"
        assert sync(runner, method, "ren", dispatch("sb", 2, ("a", "b")))["status"] == "success"
        before = snapshot(runner, "ren")

        @dlt.source(name="sb")
        def only_other():
            yield resource("other", [{"id": 3}], "replace")

        result = sync(runner, method, "ren", only_other())
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "ren")
        assert after["disp_a"] == before["disp_a"] and after["disp_b"] == before["disp_b"]


@pytest.mark.unit
class TestTwoSourcesSharingAPipeline:
    def test_a_dispatched_table_written_under_the_second_source_is_protected(self, tmp_path):
        shared = {"pipeline_name": "shared", "dataset_name": "raw_shared"}
        runner = DltPipelineRunner(tmp_path)
        first = sync(runner, "native", "src_a", dispatch("sa", 1, ("a",)), native=shared)
        assert first["status"] == "success", first.get("error")
        second = sync(runner, "native", "src_b", dispatch("sb", 2, ("a", "b")), native=shared)
        assert second["status"] == "success", second.get("error")
        before = snapshot(runner, "shared")
        assert "disp_b" in before
        result = sync(runner, "native", "src_b", dispatch("sb", 3, ("a",)), native=shared)
        assert blocked(result)
        assert "disp_b: 1 rows" in result["error"]
        assert snapshot(runner, "shared") == before
