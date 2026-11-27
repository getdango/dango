"""tests/unit/test_full_refresh_cursor.py

After a staged `--full-refresh` of an incremental source, the real pipeline's cursor must match the
rows the refresh loaded, so the next plain sync loads only newer rows (no duplicates, no gaps).
Real dlt pipelines against a local DuckDB, both sync methods.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import dlt
import pytest

from dango.ingestion import dlt_runner
from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, sync
from tests.factories.refresh_cursor_runs import NAME, cursors, dims, events, source, table_ids

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def run(runner: DltPipelineRunner, method: str, src: Any, **kwargs: Any) -> dict[str, Any]:
    return sync(runner, method, NAME, src, **kwargs)


def ev(runner: DltPipelineRunner) -> list[int]:
    return table_ids(runner, "events")


def upto(n: int) -> list[int]:
    return list(range(1, n + 1))


def refresh(runner: DltPipelineRunner, method: str, src: Any) -> dict[str, Any]:
    result = run(runner, method, src, full_refresh=True)
    assert result["status"] == "success", result.get("error")
    return result


@pytest.mark.parametrize("method", METHODS)
class TestCursorFollowsTheRefresh:
    def test_append_incremental_next_sync_loads_only_newer_rows(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5)))["status"] == "success"
        refresh(runner, method, source(events(8)))
        assert ev(runner) == upto(8)
        assert cursors() == {"events": 8}

        assert run(runner, method, source(events(10)))["status"] == "success"
        assert ev(runner) == upto(10)
        assert cursors() == {"events": 10}

    def test_merge_without_key_next_sync_loads_only_newer_rows(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5, disposition="merge")))["status"] == "success"
        refresh(runner, method, source(events(8, disposition="merge")))
        assert run(runner, method, source(events(10, disposition="merge")))["status"] == "success"
        assert ev(runner) == upto(10)

    def test_a_second_plain_sync_with_no_new_rows_adds_nothing(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5)))["status"] == "success"
        refresh(runner, method, source(events(8)))
        assert run(runner, method, source(events(8)))["status"] == "success"
        assert ev(runner) == upto(8)

    def test_rows_sharing_the_last_cursor_value_are_not_loaded_twice(
        self, tmp_path: Any, method: str
    ) -> None:
        """dlt re-reads the last cursor value on the next run and drops those rows by their hashes:
        the refresh must leave those hashes behind, not only the cursor value."""
        runner = DltPipelineRunner(tmp_path)

        def plateau(i: int) -> int:
            return min(i, 6)  # ids 6, 7, 8 all carry cursor value 6

        assert run(runner, method, source(events(5, updated=plateau)))["status"] == "success"
        refresh(runner, method, source(events(8, updated=plateau)))
        assert cursors() == {"events": 6}

        def later(i: int) -> int:
            return min(i, 6) if i <= 8 else 7

        assert run(runner, method, source(events(10, updated=later)))["status"] == "success"
        assert ev(runner) == upto(10)

    def test_a_timestamp_cursor_next_sync_loads_only_newer_rows(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def stamp(i: int) -> datetime:
            return start + timedelta(days=i)

        def make(n: int) -> Any:
            return source(events(n, updated=stamp, initial=start))

        assert run(runner, method, make(5))["status"] == "success"
        refresh(runner, method, make(8))
        assert run(runner, method, make(10))["status"] == "success"
        assert ev(runner) == upto(10)

    def test_rows_arriving_after_the_refresh_extracted_come_with_the_next_sync(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        store = upto(5)
        assert run(runner, method, source(events(0, store=store)))["status"] == "success"
        store.extend([6, 7, 8])
        real_load = DltPipelineRunner._load_with_lock

        def rows_arrive_during_load(self: Any, *args: Any, **kwargs: Any) -> Any:
            store.extend([9, 10])
            return real_load(self, *args, **kwargs)

        with patch.object(DltPipelineRunner, "_load_with_lock", rows_arrive_during_load):
            refresh(runner, method, source(events(0, store=store)))
        assert ev(runner) == upto(8)

        assert run(runner, method, source(events(0, store=store)))["status"] == "success"
        assert ev(runner) == upto(10)

    def test_rows_arriving_while_the_refresh_extracts_are_loaded_once(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        store = upto(5)
        assert run(runner, method, source(events(0, store=store)))["status"] == "success"
        store.extend([6, 7, 8])

        def grow(i: int) -> None:
            if i == 3:
                store.extend([9, 10])

        refresh(runner, method, source(events(0, store=store, on_row=grow)))
        assert ev(runner) == upto(10)
        assert cursors() == {"events": 10}
        assert run(runner, method, source(events(0, store=store)))["status"] == "success"
        assert ev(runner) == upto(10)

    def test_a_first_ever_refresh_leaves_the_cursor_too(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        refresh(runner, method, source(events(5)))
        assert cursors() == {"events": 5}
        assert run(runner, method, source(events(8)))["status"] == "success"
        assert ev(runner) == upto(8)

    def test_a_narrowed_refresh_moves_only_the_cursor_of_its_own_resource(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)

        def both(n: int, *, narrowed: bool = False) -> Any:
            src = source(events(n, table="a"), events(n, table="b"))
            return src.with_resources("a") if narrowed else src

        assert run(runner, method, both(5))["status"] == "success"
        refresh(runner, method, both(8, narrowed=True))
        assert cursors() == {"a": 8, "b": 5}

        assert run(runner, method, both(10))["status"] == "success"
        assert table_ids(runner, "a") == upto(10)
        assert table_ids(runner, "b") == upto(10)

    def test_a_refresh_after_a_failed_load_does_not_bring_rows_back_twice(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5)))["status"] == "success"
        with (
            patch.object(DltPipelineRunner, "_load_with_lock", side_effect=RuntimeError("lf")),
            pytest.raises(RuntimeError, match="lf"),
        ):
            run(runner, method, source(events(8)))
        refresh(runner, method, source(events(9)))

        assert run(runner, method, source(events(10)))["status"] == "success"
        assert ev(runner) == upto(10)


@pytest.mark.parametrize("method", METHODS)
class TestControlsUnchanged:
    def test_merge_with_key_is_exact(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        merge = {"disposition": "merge", "key": True}
        assert run(runner, method, source(events(5, **merge)))["status"] == "success"
        refresh(runner, method, source(events(8, **merge)))
        assert run(runner, method, source(events(10, **merge)))["status"] == "success"
        assert ev(runner) == upto(10)

    def test_replace_only_is_exact(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(dims()))["status"] == "success"
        refresh(runner, method, source(dims()))
        assert run(runner, method, source(dims()))["status"] == "success"
        assert table_ids(runner, "dims") == [1, 2, 3]

    def test_append_without_incremental_is_rebuilt_by_the_refresh(
        self, tmp_path: Any, method: str
    ) -> None:
        """No cursor: a plain sync appends everything again, but the refresh itself rebuilds the table."""
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5, incremental=False)))["status"] == "success"
        assert run(runner, method, source(events(5, incremental=False)))["status"] == "success"
        assert Counter(ev(runner)) == Counter(dict.fromkeys(upto(5), 2))
        refresh(runner, method, source(events(8, incremental=False)))
        assert ev(runner) == upto(8)
        assert cursors() == {}

    def test_plain_syncs_without_a_refresh(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        for n in (5, 8, 10):
            assert run(runner, method, source(events(n)))["status"] == "success"
        assert ev(runner) == upto(10)
        assert cursors() == {"events": 10}

    def test_a_blocked_refresh_changes_neither_table_nor_cursor(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5)))["status"] == "success"
        blocked = run(runner, method, source(events(0)), full_refresh=True)
        assert blocked["status"] == "failed"
        assert ev(runner) == upto(5)
        assert cursors() == {"events": 5}


def conn_hook(on_commit: Callable[[], None]) -> Any:
    """Replacement for `_connect_with_lock_retry` that runs `on_commit` just before the swap's COMMIT."""
    real = dlt_runner._connect_with_lock_retry

    class Conn:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if sql == "COMMIT":
                on_commit()
            return self.inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    def connect(path: Any, source_name: str, label: str, *args: Any, **kwargs: Any) -> Any:
        conn = real(path, source_name, label, *args, **kwargs)
        return Conn(conn) if label == "fullrefresh-staging-swap" else conn

    return connect


def refresh_that_stops(
    runner: DltPipelineRunner, method: str, error: BaseException, where: str
) -> Any:
    """A refresh at 8 rows that raises `error` at `where`: the staged load or just before the swap COMMIT."""

    def boom() -> None:
        raise error

    if where == "staging-load":
        patched = patch.object(DltPipelineRunner, "_load_with_lock", side_effect=error)
    else:
        patched = patch.object(dlt_runner, "_connect_with_lock_retry", conn_hook(boom))
    with patched:
        return run(runner, method, source(events(8)), full_refresh=True)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("where", ["staging-load", "swap-commit"])
class TestFailureKeepsTableAndCursorTogether:
    def test_a_failed_refresh(self, tmp_path: Any, method: str, where: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5)))["status"] == "success"
        failed = refresh_that_stops(runner, method, RuntimeError("stop"), where)
        assert failed["status"] == "failed"
        assert ev(runner) == upto(5)
        assert cursors() == {"events": 5}

        assert run(runner, method, source(events(8)))["status"] == "success"
        assert ev(runner) == upto(8)

    def test_an_interrupted_refresh(self, tmp_path: Any, method: str, where: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5)))["status"] == "success"
        # The staged path has no interrupt handler of its own: the interrupt reaches the caller.
        with pytest.raises(KeyboardInterrupt):
            refresh_that_stops(runner, method, KeyboardInterrupt(), where)
        assert ev(runner) == upto(5)
        assert cursors() == {"events": 5}

        assert run(runner, method, source(events(8)))["status"] == "success"
        assert ev(runner) == upto(8)
        refresh(runner, method, source(events(10)))  # the interrupted attempt left staging behind
        assert ev(runner) == upto(10)


@pytest.mark.parametrize("method", METHODS)
class TestStateWriteFailsAfterTheSwap:
    """The swap is committed and cannot be undone: the data stays refreshed, the cursor stays where
    it was (the next plain sync may load the rows between the two again, it never skips rows), the
    refresh says so, and a second refresh repairs it."""

    def test_old_cursor_kept_with_a_warning_and_a_second_refresh_repairs_it(
        self, tmp_path: Any, method: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(events(5)))["status"] == "success"
        real_save = dlt.Pipeline._save_state

        def save(self: Any, state: Any) -> Any:
            if self.pipeline_name == NAME:
                raise OSError("disk full")
            return real_save(self, state)

        with patch.object(dlt.Pipeline, "_save_state", save):
            result = refresh(runner, method, source(events(8)))
        assert result["status"] == "success"
        assert ev(runner) == upto(8)
        assert cursors() == {"events": 5}
        out = " ".join(capsys.readouterr().out.split())
        assert "Could not save the new incremental position" in out
        assert "--full-refresh" in out

        refresh(runner, method, source(events(8)))
        assert cursors() == {"events": 8}
        assert run(runner, method, source(events(10)))["status"] == "success"
        assert ev(runner) == upto(10)
