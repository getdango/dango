"""tests/unit/test_dlt_extract_timeout.py

A sync timeout that fires while dlt is extracting arrives wrapped in dlt's `PipelineStepFailed`. The
extract retry must not treat it as an ordinary failure and try again (the timeout alarm fires once, so
a retry would run with no time limit at all). Ordinary extract errors still retry as before.
"""

from __future__ import annotations

import signal
import time
from typing import Any
from unittest.mock import patch

import dlt
import pytest
from dlt.pipeline.exceptions import PipelineStepFailed

from dango.exceptions import SyncTimeoutError
from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, sync

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def pipeline_for(tmp_path: Any) -> Any:
    return dlt.pipeline(
        pipeline_name="tmo",
        destination=dlt.destinations.duckdb(credentials=str(tmp_path / "w.duckdb")),
        dataset_name="raw_tmo",
    )


def failing_source(calls: list[int], error: Exception) -> Any:
    @dlt.resource(name="events", write_disposition="append")
    def events() -> Any:
        calls.append(1)
        raise error
        yield  # makes this a generator

    return events


class TestExtractRetry:
    def test_a_wrapped_timeout_is_not_retried(self, tmp_path: Any) -> None:
        calls: list[int] = []
        runner = DltPipelineRunner(tmp_path)
        pipeline = pipeline_for(tmp_path)
        with patch("dango.ingestion.dlt_runner.time.sleep") as slept:
            with pytest.raises(SyncTimeoutError, match="timeout"):
                runner._run_extract_with_retry(
                    pipeline,
                    failing_source(calls, SyncTimeoutError("Sync exceeded 1 minute timeout")),
                )
        assert len(calls) == 1
        slept.assert_not_called()

    def test_an_ordinary_error_still_retries_with_the_same_backoff(self, tmp_path: Any) -> None:
        calls: list[int] = []
        runner = DltPipelineRunner(tmp_path)
        pipeline = pipeline_for(tmp_path)
        with patch("dango.ingestion.dlt_runner.time.sleep") as slept:
            with pytest.raises(PipelineStepFailed):
                runner._run_extract_with_retry(
                    pipeline, failing_source(calls, RuntimeError("api said no"))
                )
        assert len(calls) == 3
        assert [c.args[0] for c in slept.call_args_list] == [2, 4]


def fast_alarm(seconds: int) -> Any:
    """signal.alarm with a 0.3 second budget instead of whole seconds (real signal delivery)."""
    return signal.setitimer(signal.ITIMER_REAL, 0.3 if seconds else 0)


@pytest.mark.parametrize("method", METHODS)
def test_a_timeout_during_extract_ends_the_sync_instead_of_succeeding(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Real SIGALRM: the first extract hangs past the budget; the sync must end as a timeout."""
    monkeypatch.setattr(signal, "alarm", fast_alarm, raising=False)
    calls: list[int] = []

    @dlt.resource(name="events", write_disposition="append")
    def events() -> Any:
        calls.append(1)
        if len(calls) == 1:
            time.sleep(5)  # a hung API call, cut short by the alarm
        yield {"id": 1}

    @dlt.source(name="tmo")
    def src() -> Any:
        return events

    runner = DltPipelineRunner(tmp_path)
    started = time.monotonic()
    with pytest.raises(SyncTimeoutError):
        runner._run_with_timeout(lambda: sync(runner, method, "tmo", src()), 1)
    assert len(calls) == 1
    assert time.monotonic() - started < 2
