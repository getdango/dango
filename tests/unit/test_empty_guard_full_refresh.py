"""tests/unit/test_empty_guard_full_refresh.py

`--full-refresh` of a merge/append source that selects fewer tables than an earlier run: the tables the
run cannot write are left byte-identical (rows, content, `_dlt_load_id`), with and without
`--allow-empty-replace`; the tables it writes are reloaded. Real dlt + DuckDB, both sync methods.
"""

from __future__ import annotations

import dlt
import pytest

from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, resource, snapshot, sync

DISPOSITIONS = {
    "merge": {"a": "merge", "b": "merge", "c": "merge"},
    "append": {"a": "append", "b": "append", "c": "append"},
    "mixed": {"a": "merge", "b": "merge", "c": "append"},
}


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def source(wds, run_no, keep=None, deselect=False, empty=()):
    """Resources a, b, c; run 2 keeps only `keep` (dropped from the source, or deselected)."""

    @dlt.source(name="fr")
    def _src():
        for key, wd in wds.items():
            if keep is not None and not deselect and key not in keep:
                continue
            rows = [] if key in empty else [{"id": 10 * run_no + 1, "v": f"{key}{run_no}"}]
            hints = {"primary_key": "id"} if wd == "merge" else {}
            yield resource(key, rows, wd, **hints)

    src = _src()
    return src.with_resources(*keep) if keep is not None and deselect else src


def seeded(tmp_path, method, wds):
    runner = DltPipelineRunner(tmp_path)
    first = sync(runner, method, "fr", source(wds, 1))
    assert first["status"] == "success", first.get("error")
    return runner, snapshot(runner, "fr")


def ids(rows):
    return [r[0] for r in rows]


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("kind", sorted(DISPOSITIONS))
@pytest.mark.parametrize("deselect", [False, True], ids=["dropped", "deselected"])
class TestNarrowedFullRefresh:
    def test_with_the_allow_flag_unselected_tables_stay_and_the_chosen_one_reloads(
        self, tmp_path, method, kind, deselect
    ):
        runner, before = seeded(tmp_path, method, DISPOSITIONS[kind])
        result = sync(
            runner,
            method,
            "fr",
            source(DISPOSITIONS[kind], 2, keep=["a"], deselect=deselect),
            full_refresh=True,
            allow_empty_replace=True,
        )
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "fr")
        assert after.get("b") == before["b"] and after.get("c") == before["c"]
        assert ids(after.get("a", [])) == [21]  # reloaded: the row only the first run had is gone

    def test_without_the_flag_it_is_not_blocked_by_unselected_tables(
        self, tmp_path, method, kind, deselect
    ):
        runner, before = seeded(tmp_path, method, DISPOSITIONS[kind])
        result = sync(
            runner,
            method,
            "fr",
            source(DISPOSITIONS[kind], 2, keep=["a"], deselect=deselect),
            full_refresh=True,
        )
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "fr")
        assert after.get("b") == before["b"] and after.get("c") == before["c"]
        assert ids(after["a"]) == [21]

    def test_the_chosen_table_going_empty_is_blocked_alone_and_purged_with_the_flag(
        self, tmp_path, method, kind, deselect
    ):
        wds = DISPOSITIONS[kind]
        runner, before = seeded(tmp_path, method, wds)
        empty_run = source(wds, 2, keep=["a"], deselect=deselect, empty=["a"])
        result = sync(runner, method, "fr", empty_run, full_refresh=True)
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert (
            "existing 1 rows were never touched" in result["error"]
        )  # not 3: b and c are not at risk
        assert snapshot(runner, "fr") == before
        forced = sync(
            runner,
            method,
            "fr",
            source(wds, 2, keep=["a"], deselect=deselect, empty=["a"]),
            full_refresh=True,
            allow_empty_replace=True,
        )
        assert forced["status"] == "success", forced.get("error")
        after = snapshot(runner, "fr")
        assert after.get("a", []) == []
        assert after.get("b") == before["b"] and after.get("c") == before["c"]


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestFullRefreshEdges:
    def test_a_chosen_table_that_never_held_rows_does_not_block_on_the_others(
        self, tmp_path, method
    ):
        wds = DISPOSITIONS["merge"]
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "fr", source(wds, 1, empty=["a"]))["status"] == "success"
        before = snapshot(runner, "fr")
        result = sync(
            runner, method, "fr", source(wds, 2, keep=["a"], empty=["a"]), full_refresh=True
        )
        assert result["status"] == "success", result.get("error")
        assert snapshot(runner, "fr").get("b") == before["b"]

    @pytest.mark.parametrize("kind", sorted(DISPOSITIONS))
    def test_with_every_table_selected_full_refresh_reloads_them_all(self, tmp_path, method, kind):
        wds = DISPOSITIONS[kind]
        runner, before = seeded(tmp_path, method, wds)
        result = sync(
            runner, method, "fr", source(wds, 2), full_refresh=True, allow_empty_replace=True
        )
        assert result["status"] == "success", result.get("error")
        after = snapshot(runner, "fr")
        assert {k: ids(v) for k, v in after.items()} == {"a": [21], "b": [21], "c": [21]}

    def test_with_every_table_selected_one_going_empty_is_blocked_and_then_purged(
        self, tmp_path, method
    ):
        wds = DISPOSITIONS["merge"]
        runner, before = seeded(tmp_path, method, wds)
        result = sync(runner, method, "fr", source(wds, 2, empty=["b"]), full_refresh=True)
        assert result["status"] == "failed"
        assert "b: 1 rows" in result["error"] and "a:" not in result["error"]
        assert snapshot(runner, "fr") == before
        forced = sync(
            runner,
            method,
            "fr",
            source(wds, 2, empty=["b"]),
            full_refresh=True,
            allow_empty_replace=True,
        )
        assert forced["status"] == "success", forced.get("error")
        after = snapshot(runner, "fr")
        assert after.get("b", []) == [] and ids(after["a"]) == [21] and ids(after["c"]) == [21]

    def test_a_deselected_replace_resource_does_not_turn_a_merge_source_into_a_replace_source(
        self, tmp_path, method
    ):
        """A full refresh reloads the selected merge tables (rows deleted upstream are purged)."""
        wds = {"a": "merge", "b": "merge", "c": "replace"}
        runner = DltPipelineRunner(tmp_path)

        def build(run_no, rows):
            @dlt.source(name="fr")
            def _src():
                yield resource("a", rows, "merge", primary_key="id")
                yield resource("b", [{"id": 1}], "merge", primary_key="id")
                yield resource("c", [{"id": 1}], "replace")

            return _src().with_resources("a", "b")

        assert wds and sync(runner, method, "fr", build(1, [{"id": 1}, {"id": 2}]))
        assert ids(snapshot(runner, "fr")["a"]) == [1, 2]
        result = sync(runner, method, "fr", build(2, [{"id": 2}]), full_refresh=True)
        assert result["status"] == "success", result.get("error")
        assert ids(snapshot(runner, "fr")["a"]) == [2]  # id 1 was deleted upstream: purged

    def test_one_of_two_chosen_tables_going_empty_is_named_alone(self, tmp_path, method):
        wds = DISPOSITIONS["merge"]
        runner, before = seeded(tmp_path, method, wds)
        result = sync(
            runner,
            method,
            "fr",
            source(wds, 2, keep=["a", "b"], empty=["b"]),
            full_refresh=True,
        )
        assert result["status"] == "failed"
        assert "Full refresh would truncate 1 table(s)" in result["error"]
        assert "b: 1 rows" in result["error"] and "a:" not in result["error"]
        assert "c:" not in result["error"]
        assert snapshot(runner, "fr") == before

    def test_the_run_lists_the_tables_it_leaves_untouched(self, tmp_path, method, capsys):
        wds = DISPOSITIONS["merge"]
        runner, _ = seeded(tmp_path, method, wds)
        capsys.readouterr()
        result = sync(runner, method, "fr", source(wds, 2, keep=["a"]), full_refresh=True)
        assert result["status"] == "success", result.get("error")
        out = " ".join(capsys.readouterr().out.split())
        assert "Left untouched (not part of this sync): b, c" in out


@pytest.mark.unit
class TestDetectWriteDisposition:
    def test_only_selected_resources_count(self, tmp_path):
        runner = DltPipelineRunner(tmp_path)

        @dlt.source(name="d")
        def _src():
            yield resource("a", [{"id": 1}], "merge", primary_key="id")
            yield resource("c", [{"id": 1}], "replace")

        assert runner._detect_write_disposition(_src()) is True
        assert runner._detect_write_disposition(_src().with_resources("a")) is False
        assert runner._detect_write_disposition(_src().with_resources("c")) is True
