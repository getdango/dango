"""tests/unit/test_empty_guard_untouched_shapes.py

Which tables the empty-replace guard protects when a run selects fewer resources than an earlier one:
nested, computed-name and dispatched tables, an unselected resource, a stale table name. Real dlt +
DuckDB through both `_run_dlt_native_source` and `_run_dlt_source`; the data-loss oracle is the table
content (all columns, incl. `_dlt_load_id`) before and after.
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


def nested_source(keys, run_no, nest_empty=()):
    """Replace resources `p` and `q`, each with a child table `<key>__items`."""

    @dlt.source(name="nest")
    def _src():
        for key in keys:
            items = [] if key in nest_empty else [{"x": run_no}, {"x": run_no + 1}]
            yield resource(key, [{"id": run_no, "items": items}], "replace")

    return _src()


def computed_source(run_no, dyn_keys=("x", "y"), fixed=True):
    """A replace resource whose table name is computed per row, plus a fixed-name replace table."""

    @dlt.resource(name="dyn", write_disposition="replace", table_name=lambda it: "dyn_" + it["k"])
    def dyn():
        for key in dyn_keys:
            yield {"id": run_no, "k": key}

    @dlt.source(name="comp")
    def _src():
        yield dyn
        if fixed:
            yield resource("fx", [{"id": run_no}], "replace")

    return _src()


def dispatch_source(run_no, keys):
    """One replace resource `disp` that writes to several tables through `with_table_name`."""

    @dlt.resource(name="disp", write_disposition="replace")
    def disp():
        for key in keys:
            yield dlt.mark.with_table_name({"id": run_no, "k": key}, "disp_" + key)

    @dlt.source(name="dsp")
    def _src():
        yield disp
        yield resource("other", [{"id": run_no}], "replace")

    return _src()


def run1(tmp_path, method, name, source):
    runner = DltPipelineRunner(tmp_path)
    first = sync(runner, method, name, source)
    assert first["status"] == "success", first.get("error")
    return runner, snapshot(runner, name)


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestNestedTables:
    def test_children_of_an_unselected_parent_are_untouched(self, tmp_path, method):
        runner, before = run1(tmp_path, method, "nest", nested_source(["p", "q"], 1))
        assert {"p", "p__items", "q", "q__items"} <= set(before)
        result = sync(runner, method, "nest", nested_source(["q"], 2))
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "nest")
        assert after["p"] == before["p"] and after["p__items"] == before["p__items"]
        assert [r[0] for r in after["q"]] == [2]

    def test_a_child_table_of_a_selected_parent_is_still_protected(self, tmp_path, method):
        runner, before = run1(tmp_path, method, "nest", nested_source(["p", "q"], 1))
        result = sync(runner, method, "nest", nested_source(["p"], 2, nest_empty=("p",)))
        assert blocked(result)  # dlt would empty `p__items` while rewriting `p`
        assert "p__items: 2 rows" in result["error"]
        assert "q:" not in result["error"] and "q__items" not in result["error"]
        assert snapshot(runner, "nest") == before


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestComputedAndDispatchedTables:
    def test_a_fixed_table_still_loads_beside_a_computed_name_resource(self, tmp_path, method):
        runner, before = run1(tmp_path, method, "comp", computed_source(1))
        result = sync(runner, method, "comp", computed_source(2))
        assert result["status"] == "success", result.get("error")
        assert set(before) == {"dyn_x", "dyn_y", "fx"}

    def test_a_computed_table_that_vanishes_is_blocked(self, tmp_path, method):
        runner, before = run1(tmp_path, method, "comp", computed_source(1))
        result = sync(runner, method, "comp", computed_source(2, dyn_keys=("x",)))
        assert blocked(result)
        assert "dyn_y: 1 rows" in result["error"] and "fx" not in result["error"].replace(
            "--allow-empty-replace", ""
        )
        assert snapshot(runner, "comp") == before

    def test_a_table_no_selected_resource_can_name_stays_protected_beside_a_computed_name(
        self, tmp_path, method
    ):
        """With a computed name this run cannot tell which tables it writes: prefer safe."""
        runner, before = run1(tmp_path, method, "comp", computed_source(1))
        result = sync(runner, method, "comp", computed_source(2, fixed=False))
        assert blocked(result)
        assert "fx: 1 rows" in result["error"]
        assert snapshot(runner, "comp") == before

    def test_a_dispatched_table_that_vanishes_is_blocked_and_the_other_resource_is_not_named(
        self, tmp_path, method
    ):
        runner, before = run1(tmp_path, method, "dsp", dispatch_source(1, ("a", "b")))
        result = sync(runner, method, "dsp", dispatch_source(2, ("a",)))
        assert blocked(result)
        assert "disp_b: 1 rows" in result["error"] and "other:" not in result["error"]
        assert snapshot(runner, "dsp") == before

    def test_a_dispatched_table_of_a_dropped_resource_is_untouched(self, tmp_path, method):
        runner, before = run1(tmp_path, method, "dsp", dispatch_source(1, ("a", "b")))

        @dlt.source(name="dsp")
        def only_other():
            yield resource("other", [{"id": 2}], "replace")

        result = sync(runner, method, "dsp", only_other())
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "dsp")
        assert after["disp_a"] == before["disp_a"] and after["disp_b"] == before["disp_b"]


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestUnselectedAndRenamedTables:
    def test_a_with_resources_subset_leaves_the_deselected_replace_table_alone(
        self, tmp_path, method
    ):
        def build(run_no, subset):
            @dlt.source(name="wr")
            def _src():
                yield resource("a", [{"id": run_no}], "replace")
                yield resource("b", [{"id": run_no}], "replace")

            src = _src()
            return src.with_resources(*subset) if subset else src

        runner, before = run1(tmp_path, method, "wr", build(1, None))
        result = sync(runner, method, "wr", build(2, ["a"]))
        assert result["status"] == "success", result.get("error")
        assert snapshot(runner, "wr")["b"] == before["b"]

    def test_a_table_renamed_by_its_own_resource_is_still_guarded(self, tmp_path, method):
        def build(run_no, table):
            @dlt.source(name="rn")
            def _src():
                yield resource("res", [{"id": run_no}], "replace", table_name=table)
                yield resource("keep", [{"id": run_no}], "replace")

            return _src()

        runner, before = run1(tmp_path, method, "rn", build(1, "old"))
        result = sync(runner, method, "rn", build(2, "new"))
        assert blocked(result)  # the resource rewrites `old` as empty
        assert "old: 1 rows" in result["error"] and "keep:" not in result["error"]
        assert snapshot(runner, "rn") == before

    def test_an_unselected_fixed_table_does_not_unprotect_a_computed_name_table(
        self, tmp_path, method
    ):
        """A selected computed-name resource writes `t`; an unselected resource also names `t`."""

        def build(run_no, rows):
            @dlt.resource(name="a", write_disposition="replace", table_name=lambda it: "t")
            def a():
                yield from rows

            @dlt.source(name="col")
            def _src():
                yield a
                yield resource("u", [], "merge", table_name="t", primary_key="id", selected=False)

            return _src()

        runner, before = run1(tmp_path, method, "col", build(1, [{"id": 1}, {"id": 2}]))
        result = sync(runner, method, "col", build(2, []))
        assert blocked(result)
        assert snapshot(runner, "col") == before
