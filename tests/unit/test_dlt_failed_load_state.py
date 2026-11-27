"""tests/unit/test_dlt_failed_load_state.py

After a failure that leaves an extracted package behind (load or normalize failed), the next sync must
load that package AND extract only newer rows: an append-mode incremental source must not see the same
rows twice. Real dlt pipelines against a local DuckDB file, both sync methods.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import dlt
import pytest
from dlt.pipeline.exceptions import PipelineStepFailed

from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, ids, sync

pytestmark = pytest.mark.unit

NAME = "ev"


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def source(
    count: int, *, disposition: str = "append", incremental: bool = True, fail_at: int = 0
) -> Any:
    """Table `events` with ids 1..count; `fail_at` makes the extract of that row fail."""

    def rows() -> Any:
        for i in range(1, count + 1):
            if i == fail_at:
                raise RuntimeError("source failed while extracting")
            yield {"id": i, "updated_at": i}

    hints = {"primary_key": "id"} if disposition == "merge" else {}

    @dlt.resource(name="events", write_disposition=disposition, **hints)
    def plain() -> Any:
        yield from rows()

    @dlt.resource(name="events", write_disposition=disposition, **hints)
    def tracked(updated_at: Any = dlt.sources.incremental("updated_at", initial_value=0)) -> Any:
        yield from rows()

    @dlt.source(name=NAME)
    def _src() -> Any:
        return tracked if incremental else plain

    return _src()


def cursor() -> Any:
    """The incremental cursor in the pipeline's local dlt state (None when there is none)."""
    path = Path(os.environ["DLT_DATA_DIR"]) / "pipelines" / NAME / "state.json"
    if not path.exists():
        return None
    resources = json.loads(path.read_text(encoding="utf-8"))["sources"][NAME]["resources"]
    return resources["events"]["incremental"]["updated_at"]["last_value"]


def backups() -> list[str]:
    """State backups Dango left in dlt's pipelines directory."""
    root = Path(os.environ["DLT_DATA_DIR"]) / "pipelines"
    return sorted(p.name for p in root.iterdir() if "_backup_" in p.name)


def pending() -> bool:
    return bool(dlt.attach(NAME).has_pending_data)


def run(runner: DltPipelineRunner, method: str, src: Any, **kwargs: Any) -> dict[str, Any]:
    return sync(runner, method, NAME, src, **kwargs)


def fail_load(runner: DltPipelineRunner, method: str, src: Any, error: BaseException) -> Any:
    """One sync whose load step fails after extract and normalize: a package stays pending."""
    with patch.object(DltPipelineRunner, "_load_with_lock", side_effect=error):
        try:
            return run(runner, method, src)
        except RuntimeError as exc:
            return exc
        finally:
            assert pending()


def events(runner: DltPipelineRunner) -> list[int]:
    return ids(runner, NAME, "events")


ONCE = [list(range(1, n + 1)) for n in (8, 11)]


@pytest.mark.parametrize("method", METHODS)
class TestFailedLoadThenRetry:
    def test_append_incremental_rows_arrive_once(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5))["status"] == "success"
        assert isinstance(fail_load(runner, method, source(8), RuntimeError("lf")), RuntimeError)
        assert backups() == []

        assert run(runner, method, source(8))["status"] == "success"
        assert events(runner) == ONCE[0]
        assert run(runner, method, source(11))["status"] == "success"
        assert events(runner) == ONCE[1]

    def test_second_failed_load_on_a_pending_package(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5))["status"] == "success"
        fail_load(runner, method, source(8), RuntimeError("lf"))
        fail_load(runner, method, source(11), RuntimeError("lf"))

        assert run(runner, method, source(11))["status"] == "success"
        assert events(runner) == ONCE[1]

    def test_failed_normalize_leaves_matching_state(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5))["status"] == "success"
        real = dlt.Pipeline.normalize
        calls: list[int] = []

        def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("normalize failed")
            return real(self, *args, **kwargs)

        with patch.object(dlt.Pipeline, "normalize", flaky):
            with pytest.raises(RuntimeError, match="normalize failed"):
                run(runner, method, source(8))
            assert pending()
            assert run(runner, method, source(8))["status"] == "success"
        assert events(runner) == ONCE[0]

    def test_extract_failure_on_top_of_a_pending_package(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5))["status"] == "success"
        fail_load(runner, method, source(8), RuntimeError("lf"))
        with patch("dango.ingestion.dlt_runner.time.sleep"), pytest.raises(PipelineStepFailed):
            run(runner, method, source(11, fail_at=10))

        assert run(runner, method, source(11))["status"] == "success"
        assert events(runner) == ONCE[1]

    def test_failed_first_ever_load(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        fail_load(runner, method, source(5), RuntimeError("lf"))

        assert run(runner, method, source(8))["status"] == "success"
        assert events(runner) == ONCE[0]

    def test_interrupt_during_load_then_retry(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5))["status"] == "success"
        with patch.object(DltPipelineRunner, "_load_with_lock", side_effect=KeyboardInterrupt):
            interrupted = run(runner, method, source(8))
        assert interrupted["status"] == "interrupted"
        assert pending()

        assert run(runner, method, source(8))["status"] == "success"
        assert events(runner) == ONCE[0]


@pytest.mark.parametrize("method", METHODS)
class TestControlsUnchanged:
    def test_extract_failure_restores_the_state(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5))["status"] == "success"
        with patch("dango.ingestion.dlt_runner.time.sleep"), pytest.raises(PipelineStepFailed):
            run(runner, method, source(8, fail_at=7))
        assert not pending()
        assert cursor() == 5

        assert run(runner, method, source(8))["status"] == "success"
        assert events(runner) == ONCE[0]

    def test_state_is_restored_when_no_package_is_left(self, tmp_path: Any, method: str) -> None:
        """dlt advanced the cursor but the package is gone (as after the empty-replace guard drops
        it): the old state must come back, or the rows between old and new cursor are never loaded."""
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5))["status"] == "success"
        real = dlt.Pipeline.extract

        def extract_then_lose_the_package(self: Any, *args: Any, **kwargs: Any) -> Any:
            real(self, *args, **kwargs)
            self.drop_pending_packages()
            raise RuntimeError("extract ended badly")

        with (
            patch.object(dlt.Pipeline, "extract", extract_then_lose_the_package),
            patch("dango.ingestion.dlt_runner.time.sleep"),
            pytest.raises(RuntimeError, match="extract ended badly"),
        ):
            run(runner, method, source(8))
        assert not pending()
        assert cursor() == 5

        assert run(runner, method, source(8))["status"] == "success"
        assert events(runner) == ONCE[0]

    def test_merge_with_primary_key_absorbs_the_retry(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5, disposition="merge"))["status"] == "success"
        fail_load(runner, method, source(8, disposition="merge"), RuntimeError("lf"))

        assert run(runner, method, source(8, disposition="merge"))["status"] == "success"
        assert events(runner) == ONCE[0]

    def test_append_without_incremental_is_unaffected(self, tmp_path: Any, method: str) -> None:
        """No cursor: every run extracts everything, so state never matters (ids 1-5 come from the
        first run, the kept package and the re-extract; ids 6-8 from the last two)."""
        runner = DltPipelineRunner(tmp_path)
        assert run(runner, method, source(5, incremental=False))["status"] == "success"
        fail_load(runner, method, source(8, incremental=False), RuntimeError("lf"))
        assert run(runner, method, source(8, incremental=False))["status"] == "success"

        expected = Counter(dict.fromkeys(range(1, 6), 3)) + Counter(dict.fromkeys((6, 7, 8), 2))
        assert Counter(events(runner)) == expected


class TestKeepDecision:
    """Which failures keep the state: only those that left a package this run did not start with."""

    @staticmethod
    def pipeline(extracted: list[str], normalized: list[str]) -> Any:
        fake = MagicMock()
        fake.list_extracted_load_packages.return_value = extracted
        fake.list_normalized_load_packages.return_value = normalized
        return fake

    @pytest.mark.parametrize(
        ("before", "extracted", "normalized", "keeps"),
        [
            (frozenset(), [], [], False),
            (frozenset(), ["p1"], [], True),
            (frozenset(), [], ["p1"], True),
            (frozenset({"p1"}), [], ["p1"], False),
            (frozenset({"p1"}), ["p2"], ["p1"], True),
            (frozenset({"p1"}), [], ["p2"], True),
            (None, [], ["p1"], False),
        ],
    )
    def test_decision(
        self,
        tmp_path: Any,
        before: frozenset[str] | None,
        extracted: list[str],
        normalized: list[str],
        keeps: bool,
    ) -> None:
        backup = tmp_path / "x_backup_20260101_000000"
        backup.mkdir()
        runner = DltPipelineRunner(tmp_path)
        fake = self.pipeline(extracted, normalized)

        assert runner._keep_state_for_new_package(fake, backup, before) is keeps
        assert backup.exists() is not keeps

    def test_unreadable_package_list_keeps_nothing(self, tmp_path: Any) -> None:
        fake = MagicMock()
        fake.list_extracted_load_packages.side_effect = OSError("gone")
        backup = tmp_path / "x_backup_20260101_000000"
        backup.mkdir()

        assert (
            DltPipelineRunner(tmp_path)._keep_state_for_new_package(fake, backup, frozenset())
            is False
        )
        assert backup.exists()
