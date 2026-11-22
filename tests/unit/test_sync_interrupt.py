"""tests/unit/test_sync_interrupt.py

An interrupted `dango sync` stops, says so, and exits 130 so a script cannot mistake it for success.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import dlt
import pytest
from click.testing import CliRunner
from dlt.pipeline.exceptions import PipelineStepFailed
from rich.console import Console

from dango.cli.commands.source import sync
from dango.ingestion.dlt_runner import DltPipelineRunner, run_sync
from dango.utils.sync_history import load_sync_history
from tests.unit.test_cli_sync import _patch_sync_prereqs
from tests.unit.test_runner_error_text import _run
from tests.unit.test_sync_full_refresh_prompt import _child_env, _project

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".dlt").mkdir()
    return tmp_path


@pytest.fixture
def out() -> Iterator[StringIO]:
    buf = StringIO()
    with patch("dango.ingestion.dlt_runner.console", Console(file=buf, width=2000, markup=True)):
        yield buf


MESSAGE = "Sync interrupted"
RESUME = "Resume with the same command"


def _wrapped_interrupt() -> PipelineStepFailed:
    """What dlt raises when Ctrl+C lands inside a pipeline step."""
    pipeline = MagicMock()
    pipeline.pipeline_name = "p"
    pipeline.has_pending_data = False
    return PipelineStepFailed(pipeline, "extract", "L1", KeyboardInterrupt(), None)


def _returns(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Call *call*; a KeyboardInterrupt escaping it is a test failure, not the end of the session."""
    try:
        return call(*args, **kwargs)
    except KeyboardInterrupt:
        pytest.fail("a KeyboardInterrupt escaped; run_sync must return an interrupted summary")


def _source(name: str) -> MagicMock:
    src = MagicMock()
    src.name = name
    src.type = MagicMock()
    src.type.value = "hubspot"
    src.enabled = True
    src.csv = None
    src.dlt_native = None
    return src


class TestCommandExitCode:
    def _invoke(self, side_effect: BaseException):
        patches = [
            *_patch_sync_prereqs(),
            patch("dango.ingestion.run_sync", MagicMock(side_effect=side_effect)),
            patch("dango.oauth.validation.validate_before_sync"),
            patch("dango.cli.scaffold.enforce_scaffold"),
        ]
        for p in patches:
            p.start()
        try:
            return CliRunner().invoke(sync, ["--yes"], obj={"project_root": "/tmp"})
        finally:
            for p in patches:
                p.stop()

    def test_interrupt_prints_the_message_and_exits_130(self) -> None:
        result = self._invoke(KeyboardInterrupt())
        assert result.exit_code == 130, result.output
        assert MESSAGE in result.output and RESUME in result.output
        assert "Error:" not in result.output

    def test_a_failed_sync_still_exits_1(self) -> None:
        result = self._invoke(RuntimeError("boom"))
        assert result.exit_code == 1

    def test_the_interrupted_flag_gives_the_same_message_and_exit_130(self) -> None:
        summary = {
            "interrupted": True,
            "interrupted_source": "s",
            "failed_count": 1,
            "success_count": 0,
            "failed_sources": [{"name": "s", "error": "Sync interrupted before it finished"}],
            "oauth_warnings": [],
        }
        result = self._invoke_returning(summary)
        assert result.exit_code == 130, result.output
        assert MESSAGE in result.output and RESUME in result.output
        assert "Sync failed" not in result.output  # not reported as an ordinary failure
        assert "Error:" not in result.output

    def _invoke_returning(self, summary: dict[str, Any]):
        patches = [
            *_patch_sync_prereqs(),
            patch("dango.ingestion.run_sync", MagicMock(return_value=summary)),
            patch("dango.oauth.validation.validate_before_sync"),
            patch("dango.cli.scaffold.enforce_scaffold"),
        ]
        for p in patches:
            p.start()
        try:
            return CliRunner().invoke(sync, ["--yes"], obj={"project_root": "/tmp"})
        finally:
            for p in patches:
                p.stop()


_CHILD = """
import sys, time
import dango.ingestion as ing
def slow_sync(**kwargs):
    print("SYNC-RUNNING", flush=True)
    time.sleep(60)
ing.run_sync = slow_sync
from dango.cli.main import cli
cli(["sync", "--yes"])
"""


class TestRealSignal:
    """A real SIGINT delivered to the real command (the way Ctrl+C reaches it)."""

    def test_sigint_gives_exit_130_and_the_message(self, tmp_path: Path) -> None:
        project_dir = _project(tmp_path / "proj")
        proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            cwd=project_dir,
            env=_child_env(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdout is not None
        seen = ""
        deadline = time.monotonic() + 60
        while "SYNC-RUNNING" not in seen:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            seen += line
            assert time.monotonic() < deadline, seen
        # a python parent sends it: a background job in a non-interactive shell ignores SIGINT.
        # Windows: the CTRL_C_EVENT branch below has never run (the child is not a console process
        # group leader, so delivery is unverified); it is a real-device checklist item and this
        # test is not Windows evidence. The product path (KeyboardInterrupt, Exit(130)) is portable.
        os.kill(proc.pid, getattr(signal, "CTRL_C_EVENT", signal.SIGINT))
        try:
            rest, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            rest, _ = proc.communicate()
            pytest.fail(f"did not stop after the interrupt:\n{seen}{rest}")
        text = seen + rest
        assert proc.returncode == 130, text
        assert MESSAGE in text and RESUME in text


class TestRunSyncStopsOnInterrupt:
    def _mock_runner(self, statuses: list[str]) -> MagicMock:
        runner = MagicMock()
        runner.run_source.side_effect = [
            {"status": s, "source": f"s{i}", "rows_loaded": 1} for i, s in enumerate(statuses)
        ]
        return runner

    def test_remaining_sources_dbt_and_hooks_do_not_run(self, tmp_path: Path) -> None:
        runner = self._mock_runner(["success", "interrupted", "success"])
        hooks = MagicMock()
        dbt = MagicMock(return_value=(True, "OK"))
        with (
            patch("dango.ingestion.dlt_runner.DltPipelineRunner", return_value=runner),
            patch("dango.transformation.run_dbt_models", dbt),
            patch("dango.utils.post_sync.dispatch_post_sync_hooks", hooks),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            summary = _returns(run_sync, tmp_path, [_source("s0"), _source("s1"), _source("s2")])
        assert runner.run_source.call_count == 2  # the third source was never started
        dbt.assert_not_called()
        hooks.assert_not_called()
        # callers outside the CLI do not expect a KeyboardInterrupt: it is a flag on the summary
        assert summary["interrupted"] is True
        assert summary["interrupted_source"] == "s1"
        assert summary["success_sources"] == ["s0"]
        assert summary["failed_count"] == 1  # an unaware caller sees a failure, never a success
        assert summary["failed_sources"][0]["name"] == "s1"
        assert summary["transform_status"] == "skipped"

    def test_an_ordinary_sync_summary_is_not_flagged(self, tmp_path: Path) -> None:
        runner = self._mock_runner(["failed"])
        with (
            patch("dango.ingestion.dlt_runner.DltPipelineRunner", return_value=runner),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            summary = _returns(run_sync, tmp_path, [_source("s0")])
        assert not summary.get("interrupted")

    def test_interrupt_during_dbt_runs_no_hooks_and_releases_the_lock(self, tmp_path: Path) -> None:
        runner = self._mock_runner(["success"])
        hooks = MagicMock()
        lock = MagicMock()
        gen = MagicMock()
        gen.generate_all_models.return_value = {"generated": [], "skipped": []}
        with (
            patch("dango.ingestion.dlt_runner.DltPipelineRunner", return_value=runner),
            patch("dango.transformation.generator.DbtModelGenerator", return_value=gen),
            patch("dango.governance.schema_drift.detect_drift_for_sources", return_value=[]),
            patch("dango.transformation.run_dbt_models", side_effect=KeyboardInterrupt),
            patch("dango.utils.dbt_lock.DbtLock", return_value=lock),
            patch("dango.utils.post_sync.dispatch_post_sync_hooks", hooks),
            patch("duckdb.connect"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            with pytest.raises(KeyboardInterrupt):
                run_sync(tmp_path, [_source("s0")])
        hooks.assert_not_called()
        lock.release.assert_called()


class TestInterruptInsideDltSteps:
    """dlt wraps an interrupt in PipelineStepFailed; Dango must not retry or report it as a failure."""

    def test_extract_is_not_retried(self, tmp_path: Path) -> None:
        pipeline = MagicMock(spec=dlt.Pipeline)
        pipeline.extract.side_effect = _wrapped_interrupt()
        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        with (
            patch("dango.ingestion.dlt_runner.time.sleep") as sleep,
            patch("dango.ingestion.dlt_runner.console"),
        ):
            with pytest.raises(KeyboardInterrupt):
                runner._run_extract_with_retry(pipeline, MagicMock())
        assert pipeline.extract.call_count == 1
        sleep.assert_not_called()

    def test_a_real_dlt_pipeline_interrupted_in_a_resource_is_not_retried(
        self, tmp_path: Path
    ) -> None:
        calls: list[int] = []

        @dlt.resource(name="items")
        def items() -> Any:
            calls.append(1)
            yield {"id": 1}
            raise KeyboardInterrupt

        pipeline = dlt.pipeline(
            "wp39_interrupt",
            destination=dlt.destinations.duckdb(str(tmp_path / "w.duckdb")),
            pipelines_dir=str(tmp_path / "pipes"),
            dataset_name="raw_x",
        )
        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        with (
            patch("dango.ingestion.dlt_runner.time.sleep") as sleep,
            patch("dango.ingestion.dlt_runner.console"),
        ):
            with pytest.raises(KeyboardInterrupt):
                runner._run_extract_with_retry(pipeline, items)
        assert len(calls) == 1
        sleep.assert_not_called()

    @pytest.mark.parametrize("kind", ["native", "dlt"])
    @pytest.mark.parametrize("step", ["extract", "normalize"])
    @pytest.mark.parametrize("wrapped", [True, False])
    def test_sync_stops_instead_of_reporting_a_failure(
        self,
        project: Path,
        out: Any,
        kind: str,
        step: str,
        wrapped: bool,
    ) -> None:
        exc = _wrapped_interrupt() if wrapped else KeyboardInterrupt()
        extra = {"extract": None, "normalize": None, step: exc}
        summary = _returns(
            _run, project, kind, RuntimeError("unused"), ("src_a", "src_b"), pipeline_extra=extra
        )
        assert summary["interrupted"] is True
        assert summary["interrupted_source"] == "src_a"
        printed = out.getvalue()
        assert "Unknown error" not in printed
        assert "Retrying" not in printed
        history = load_sync_history(project, "src_a")
        assert history and history[0]["status"] == "interrupted"
        assert load_sync_history(project, "src_b") == []  # the next source never started

    def test_partial_resume_interrupt_is_an_interrupt_not_a_failed_resume(
        self,
        project: Path,
        out: Any,
    ) -> None:
        """Ctrl+C while the retry without the failing resource runs must stop the sync."""
        source = MagicMock()
        source.resources = {"a": MagicMock(), "b": MagicMock()}
        failing = RuntimeError("Error in a: boom")
        extra = {"extract": [failing, failing, failing, _wrapped_interrupt()]}
        summary = _returns(
            _run,
            project,
            "dlt",
            RuntimeError("unused"),
            ("src_a", "src_b"),
            pipeline_extra=extra,
            source=source,
        )
        assert summary["interrupted"] is True
        assert "Partial sync also failed" not in out.getvalue()
        history = load_sync_history(project, "src_a")
        assert history and history[0]["status"] == "interrupted"

    def test_staging_reload_interrupt_is_an_interrupt(self, project: Path, out: Any) -> None:
        """The full-refresh staging pipeline's normalize step is covered like the others."""
        pipeline = MagicMock(spec=dlt.Pipeline)
        pipeline.extract.return_value = None
        pipeline.normalize.side_effect = _wrapped_interrupt()
        runner = DltPipelineRunner(project)
        with (
            patch("dlt.pipeline", return_value=pipeline),
            patch("dango.ingestion.dlt_runner._connect_with_lock_retry"),
            patch.object(DltPipelineRunner, "_clear_local_pipeline_cache"),
            patch("dango.ingestion.dlt_runner.console"),
            pytest.raises(KeyboardInterrupt),
        ):
            runner._full_refresh_via_staging(
                source=MagicMock(),
                source_name="s",
                dataset_name="raw_s",
                pipeline_name="s",
                max_lock_wait=1,
                uses_replace_mode=False,
            )

    def test_load_phase_interrupt_is_an_interrupt(self, tmp_path: Path) -> None:
        pipeline = MagicMock(spec=dlt.Pipeline)
        pipeline.load.side_effect = _wrapped_interrupt()
        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        with (
            patch("dango.platform.common.metabase_lifecycle.stop_metabase_for_writes"),
            patch("dango.platform.common.metabase_lifecycle.start_metabase_after_writes"),
            patch("dango.utils.dbt_lock.DbtLock"),
            patch("dango.ingestion.dlt_runner.console"),
        ):
            with pytest.raises(KeyboardInterrupt):
                runner._load_with_lock(pipeline, "src")
