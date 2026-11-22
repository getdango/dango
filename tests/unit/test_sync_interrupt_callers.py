"""tests/unit/test_sync_interrupt_callers.py

An interrupted sync is a flag on the run_sync summary: the scheduler/web subprocess, the MCP tool
and the initial-sync page record it as a stopped sync instead of crashing on a KeyboardInterrupt.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.cli.commands import mcp_operations
from dango.platform.scheduling.sync_trigger import run_manual_sync
from dango.utils.sync_history import load_sync_history, save_sync_history_entry
from tests.unit.test_schedule_history_api import _create_db
from tests.unit.test_sync_full_refresh_prompt import _child_env
from tests.unit.test_sync_trigger import _make_config

pytestmark = pytest.mark.unit

INTERRUPTED = {
    "interrupted": True,
    "interrupted_source": "src1",
    "failed_count": 1,
    "success_count": 0,
    "failed_sources": [{"name": "src1", "error": "Sync interrupted before it finished"}],
    "results": [{"status": "interrupted", "source": "src1", "rows_loaded": 0}],
}


def _status(project: Path, sync_id: str) -> dict[str, Any]:
    path = project / ".dango" / "state" / f"sync_status_{sync_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _records(project: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(project / ".dango" / "scheduler.db"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM execution_history").fetchall()
    finally:
        conn.close()


class TestSyncTriggerRecordsAStoppedSync:
    def _run(self, project: Path, **patches: Any) -> dict[str, Any]:
        _create_db(project / ".dango")
        run = patches.pop("run_sync")
        with (
            patch("dango.ingestion.run_sync", run),
            patch("dango.config.helpers.load_config", return_value=_make_config(["src1"])),
            patch("dango.oauth.validation.validate_before_sync"),
        ):
            try:
                return run_manual_sync(
                    project,
                    sources=["src1"],
                    write_progress=True,
                    sync_id="t1",
                    source_label="manual",
                )
            except KeyboardInterrupt:  # a BaseException would end the whole test session
                pytest.fail("run_manual_sync let a KeyboardInterrupt escape")

    def test_interrupted_summary_is_a_terminal_stopped_sync(self, tmp_path: Path) -> None:
        (tmp_path / ".dango").mkdir()
        result = self._run(tmp_path, run_sync=MagicMock(return_value=dict(INTERRUPTED)))
        assert result["status"] == "failed"
        assert "stopped by a signal" in result["error"]
        status = _status(tmp_path, "t1")
        assert status["phase"] == "failed"
        assert "stopped by a signal" in status["error"]
        record = _records(tmp_path)[0]
        assert record["status"] == "failed" and record["ended_at"] is not None
        # run_source already wrote the one history entry; the trigger adds no second one
        assert load_sync_history(tmp_path, "src1") == []

    def test_a_raw_interrupt_before_run_source_is_recorded_not_raised(self, tmp_path: Path) -> None:
        (tmp_path / ".dango").mkdir()
        result = self._run(tmp_path, run_sync=MagicMock(side_effect=KeyboardInterrupt()))
        assert result["status"] == "failed"
        assert _status(tmp_path, "t1")["phase"] == "failed"
        record = _records(tmp_path)[0]
        assert record["status"] == "failed" and record["ended_at"] is not None


class TestCrashHandlerLeavesAnInterruptedEntryAlone:
    def test_no_second_sync_crashed_entry(self, tmp_path: Path) -> None:
        from dango.platform.sync_process import _handle_crash

        (tmp_path / ".dango").mkdir()
        save_sync_history_entry(
            tmp_path,
            "src1",
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                "status": "interrupted",
                "duration_seconds": 1,
            },
        )
        _handle_crash(tmp_path, ["src1"], -15, None, "Sync process exited")
        history = load_sync_history(tmp_path, "src1", limit=10)
        assert [h["status"] for h in history] == ["interrupted"]


@pytest.fixture
def mcp_project(tmp_path: Path, sample_config: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_operations, "_get_project_root", lambda: tmp_path)
    return tmp_path


class TestMcpRunSync:
    def test_interrupted_summary_returns_status_interrupted(
        self, mcp_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dango.ingestion.run_sync", lambda **kw: dict(INTERRUPTED))
        result = mcp_operations.run_sync("test_source")
        assert result["status"] == "interrupted"
        assert result["interrupted_source"] == "src1"

    def test_a_raw_interrupt_is_returned_not_raised(
        self, mcp_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def stop(**kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("dango.ingestion.run_sync", stop)
        try:
            result = mcp_operations.run_sync("test_source")
        except KeyboardInterrupt:  # in the MCP server this would end the event loop
            pytest.fail("the MCP tool let a KeyboardInterrupt escape")
        assert result["status"] == "interrupted"


class TestInitialSync:
    def test_an_interrupted_source_is_a_failed_source(self, tmp_path: Path) -> None:
        from dango.web.routes.initial_sync import _sync_single_source

        source = MagicMock()
        with (
            patch("dango.config.helpers.load_config") as load,
            patch("dango.ingestion.run_sync", return_value=dict(INTERRUPTED)),
        ):
            load.return_value.sources.get_source.return_value = source
            with pytest.raises(RuntimeError, match="interrupted"):
                asyncio.run(_sync_single_source(tmp_path, "src1"))


_SOURCE = """
import dlt

@dlt.source
def tiny_source():
    @dlt.resource(write_disposition="replace")
    def items():
        yield [{"id": i, "v": i} for i in range(50)]
    return items
"""

# Stands in for "the signal arrived while dlt was loading": dlt's own handler is installed around
# Pipeline.load, and Load.run is where it looks for a pending signal between runs.
_TRIGGER_CHILD = """
import runpy, sys, time
from dlt.common.runtime import signals
from dlt.load import Load
_orig = Load.run
def _run(self, pool):
    print("IN-LOAD", flush=True)
    time.sleep(10)
    signals.raise_if_signalled()
    return _orig(self, pool)
Load.run = _run
sys.argv = ["sync_trigger", sys.argv[1]]
runpy.run_module("dango.platform.scheduling.sync_trigger", run_name="__main__")
"""


def _trigger_project(root: Path) -> Path:
    dango_dir = root / ".dango"
    dango_dir.mkdir(parents=True)
    project = {"project": {"name": "T", "created_by": "t@example.com", "purpose": "tests"}}
    (dango_dir / "project.yml").write_text(yaml.safe_dump(project), encoding="utf-8")
    sources = {
        "version": "1.0",
        "sources": [
            {
                "name": "tiny",
                "type": "dlt_native",
                "dlt_native": {"source_module": "tiny_src", "source_function": "tiny_source"},
            }
        ],
    }
    (dango_dir / "sources.yml").write_text(yaml.safe_dump(sources), encoding="utf-8")
    (root / "custom_sources").mkdir()
    (root / "custom_sources" / "tiny_src.py").write_text(_SOURCE, encoding="utf-8")
    (root / "data").mkdir()
    _create_db(dango_dir)
    return root


class TestRealSigtermToTheSyncSubprocess:
    """The scheduler and web run `python -m ...sync_trigger`; a service stop sends it SIGTERM."""

    def test_sigterm_during_load_leaves_a_clean_stopped_sync(self, tmp_path: Path) -> None:
        project = _trigger_project(tmp_path / "proj")
        args = {"project_root": str(project), "sources": ["tiny"], "write_progress": True}
        args["sync_id"] = "real1"
        env = _child_env(tmp_path)
        env.update(
            DO_NOT_TRACK="1",
            RUNTIME__DLTHUB_TELEMETRY="false",
            DBT_SEND_ANONYMOUS_USAGE_STATS="false",
            DANGO_TELEMETRY="false",
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", _TRIGGER_CHILD, json.dumps(args)],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        assert proc.stdout is not None
        seen = ""
        deadline = time.monotonic() + 90
        while "IN-LOAD" not in seen:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            seen += line
            assert time.monotonic() < deadline, seen
        # POSIX signal: the scheduler's process-group stop; Windows delivery is a real-device item
        os.kill(proc.pid, signal.SIGTERM)
        try:
            rest, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            rest, _ = proc.communicate()
            pytest.fail(f"did not stop after SIGTERM:\n{seen}{rest}")
        text = seen + rest
        assert proc.returncode == 0, text  # the result is printed, like any other failed sync
        assert "Traceback" not in text, text
        status = _status(project, "real1")
        assert status["phase"] == "failed", status
        record = _records(project)[0]
        assert record["status"] == "failed" and record["ended_at"] is not None
        history = load_sync_history(project, "tiny", limit=10)
        assert [h["status"] for h in history] == ["interrupted"]
