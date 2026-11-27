"""tests/unit/test_full_refresh_mixed_source.py

`--full-refresh` of a source that mixes replace and append/merge resources must rebuild every table
once: the append table must not receive its rows a second time. A source whose selected resources
are all replace keeps its own path. Real dlt pipelines against a local DuckDB, both sync methods.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from unittest.mock import patch

import pytest

from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, sync
from tests.factories.refresh_cursor_runs import (
    NAME,
    cursors,
    dims,
    events,
    source,
    table_ids,
    table_names,
)

pytestmark = pytest.mark.unit

NEW_DIMS = [{"id": 7}, {"id": 8}]


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def run(runner: DltPipelineRunner, method: str, src: Any, **kwargs: Any) -> dict[str, Any]:
    return sync(runner, method, NAME, src, **kwargs)


def mixed(n: int, *, rows: list[dict[str, int]] | None = None, **event_kwargs: Any) -> Any:
    return source(dims(rows), events(n, **event_kwargs))


def upto(n: int) -> list[int]:
    return list(range(1, n + 1))


def staging_calls() -> tuple[list[int], Any]:
    """A spy for `_full_refresh_via_staging` that counts calls and still runs the real thing."""
    calls: list[int] = []
    real = DltPipelineRunner._full_refresh_via_staging

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(self, *args, **kwargs)

    return calls, patch.object(DltPipelineRunner, "_full_refresh_via_staging", spy)


@pytest.mark.parametrize("method", METHODS)
class TestMixedRefreshRebuildsEveryTableOnce:
    def test_append_rows_are_not_loaded_a_second_time(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5))["status"] == "success"
        refreshed = run(runner, method, mixed(8, rows=NEW_DIMS), full_refresh=True)
        assert refreshed["status"] == "success", refreshed.get("error")
        assert table_ids(runner, "events") == upto(8)
        assert table_ids(runner, "dims") == [7, 8]
        assert cursors() == {"events": 8}

        assert run(runner, method, mixed(10, rows=NEW_DIMS))["status"] == "success"
        assert table_ids(runner, "events") == upto(10)
        assert table_ids(runner, "dims") == [7, 8]

    def test_nested_tables_of_the_append_resource_are_rebuilt_once(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5, tags=True))["status"] == "success"
        refreshed = run(runner, method, mixed(8, tags=True), full_refresh=True)
        assert refreshed["status"] == "success", refreshed.get("error")

        wanted = Counter(f"{i}{s}" for i in upto(8) for s in "ab")
        assert Counter(table_ids(runner, "events__tags", "t")) == wanted
        assert table_ids(runner, "events") == upto(8)

    def test_merge_with_key_next_to_replace_is_exact(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        merge = {"disposition": "merge", "key": True}
        assert run(runner, method, mixed(5, **merge))["status"] == "success"
        assert run(runner, method, mixed(8, **merge), full_refresh=True)["status"] == "success"
        assert run(runner, method, mixed(10, **merge))["status"] == "success"
        assert table_ids(runner, "events") == upto(10)

    def test_a_mixed_source_is_refreshed_through_staging(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5))["status"] == "success"
        calls, spy = staging_calls()
        with spy:
            result = run(runner, method, mixed(8), full_refresh=True)
        assert calls == [1]
        assert result["uses_replace_mode"] is True

    def test_only_replace_resources_keep_their_own_path(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)

        def replace_only(rows: list[dict[str, int]]) -> Any:
            return source(dims(rows), dims(rows).with_name("more_dims"))

        assert run(runner, method, replace_only(NEW_DIMS[:1]))["status"] == "success"
        calls, spy = staging_calls()
        with spy:
            result = run(runner, method, replace_only(NEW_DIMS), full_refresh=True)
        assert result["status"] == "success", result.get("error")
        assert calls == []
        assert table_ids(runner, "dims") == [7, 8]
        assert table_ids(runner, "more_dims") == [7, 8]

    def test_a_narrowed_run_without_the_replace_resource_is_refreshed_once(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5))["status"] == "success"
        narrowed = mixed(8).with_resources("events")
        assert run(runner, method, narrowed, full_refresh=True)["status"] == "success"
        assert table_ids(runner, "events") == upto(8)
        assert table_ids(runner, "dims") == [1, 2, 3]


@pytest.mark.parametrize("method", METHODS)
class TestMixedRefreshFailuresLeaveTheSourceAsItWas:
    def test_a_failed_staged_load_changes_nothing(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5))["status"] == "success"
        with patch.object(DltPipelineRunner, "_load_with_lock", side_effect=RuntimeError("lf")):
            try:
                outcome = run(runner, method, mixed(8, rows=NEW_DIMS), full_refresh=True)["status"]
            except RuntimeError:
                outcome = "failed"  # the replace path raises where the staged path returns
        assert outcome == "failed"
        assert table_ids(runner, "events") == upto(5)
        assert table_ids(runner, "dims") == [1, 2, 3]
        assert cursors() == {"events": 5}

        assert run(runner, method, mixed(8))["status"] == "success"
        assert table_ids(runner, "events") == upto(8)

    def test_an_empty_replace_table_blocks_the_refresh(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5))["status"] == "success"
        blocked = run(runner, method, mixed(8, rows=[]), full_refresh=True)
        assert blocked["status"] == "failed"
        assert blocked["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert table_ids(runner, "events") == upto(5)
        assert table_ids(runner, "dims") == [1, 2, 3]
        assert cursors() == {"events": 5}

    def test_allowing_an_empty_replace_table_still_rebuilds_the_append_table_once(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5))["status"] == "success"
        allowed = run(
            runner, method, mixed(8, rows=[]), full_refresh=True, allow_empty_replace=True
        )
        assert allowed["status"] == "success", allowed.get("error")
        assert table_ids(runner, "events") == upto(8)
        assert table_names(runner) == ["dims", "events"]  # emptied, not dropped
        assert table_ids(runner, "dims") == []

    def test_the_next_plain_sync_fills_the_emptied_replace_table(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, mixed(5))["status"] == "success"
        refreshed = run(
            runner, method, mixed(8, rows=[]), full_refresh=True, allow_empty_replace=True
        )
        assert refreshed["status"] == "success", refreshed.get("error")

        assert run(runner, method, mixed(10, rows=NEW_DIMS))["status"] == "success"
        assert table_ids(runner, "events") == upto(10)
        assert table_ids(runner, "dims") == [7, 8]
        assert run(runner, method, mixed(11, rows=NEW_DIMS))["status"] == "success"
        assert table_ids(runner, "events") == upto(11)


@pytest.mark.parametrize("method", METHODS)
class TestAnEmptiedTableStaysForTheNextSync:
    def test_an_append_table_that_returned_no_rows_is_emptied_not_dropped(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)

        def two(n_b: int) -> Any:
            return source(events(5, table="a"), events(n_b, table="b"))

        assert run(runner, method, two(5))["status"] == "success"
        allowed = run(runner, method, two(0), full_refresh=True, allow_empty_replace=True)
        assert allowed["status"] == "success", allowed.get("error")
        assert table_ids(runner, "a") == upto(5)
        assert table_ids(runner, "b") == []

        assert run(runner, method, two(5))["status"] == "success"
        assert table_ids(runner, "a") == upto(5)
        assert table_ids(runner, "b") == upto(5)
