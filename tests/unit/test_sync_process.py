"""tests/unit/test_sync_process.py

Tests for dango.platform.sync_process — subprocess launch and polling utilities.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MOD = "dango.platform.sync_process"
_WS_MOD = "dango.web.routes.websocket"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_status_file(tmp_path, phase="starting", **extra):
    """Write a sync status file for testing."""
    state_dir = tmp_path / ".dango" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "pid": os.getpid(),
        "phase": phase,
        "message": f"Test phase: {phase}",
        "sources": ["test_source"],
        **extra,
    }
    with open(state_dir / "sync_status.json", "w") as f:
        json.dump(status, f)
    return status


# ---------------------------------------------------------------------------
# get_sync_status_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetSyncStatusPath:
    def test_returns_expected_path(self, tmp_path):
        from dango.platform.sync_process import get_sync_status_path

        path = get_sync_status_path(tmp_path)
        assert path == tmp_path / ".dango" / "state" / "sync_status.json"


# ---------------------------------------------------------------------------
# read_sync_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadSyncStatus:
    def test_returns_none_when_missing(self, tmp_path):
        from dango.platform.sync_process import read_sync_status

        assert read_sync_status(tmp_path) is None

    def test_reads_valid_file(self, tmp_path):
        from dango.platform.sync_process import read_sync_status

        _write_status_file(tmp_path, phase="data_load")
        status = read_sync_status(tmp_path)
        assert status is not None
        assert status["phase"] == "data_load"

    def test_returns_none_on_invalid_json(self, tmp_path):
        from dango.platform.sync_process import read_sync_status

        state_dir = tmp_path / ".dango" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "sync_status.json").write_text("not json{{{")
        assert read_sync_status(tmp_path) is None


# ---------------------------------------------------------------------------
# cleanup_sync_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCleanupSyncStatus:
    def test_removes_completed_status(self, tmp_path):
        from dango.platform.sync_process import cleanup_sync_status, get_sync_status_path

        _write_status_file(tmp_path, phase="completed")
        cleanup_sync_status(tmp_path)
        assert not get_sync_status_path(tmp_path).exists()

    def test_removes_failed_status(self, tmp_path):
        from dango.platform.sync_process import cleanup_sync_status, get_sync_status_path

        _write_status_file(tmp_path, phase="failed")
        cleanup_sync_status(tmp_path)
        assert not get_sync_status_path(tmp_path).exists()

    def test_removes_dead_pid(self, tmp_path):
        from dango.platform.sync_process import cleanup_sync_status, get_sync_status_path

        _write_status_file(tmp_path, phase="data_load", pid=999999)
        with patch(f"{_MOD}._is_pid_alive", return_value=False):
            cleanup_sync_status(tmp_path)
        assert not get_sync_status_path(tmp_path).exists()

    def test_noop_when_no_file(self, tmp_path):
        from dango.platform.sync_process import cleanup_sync_status

        cleanup_sync_status(tmp_path)  # should not raise

    def test_keeps_active_status(self, tmp_path):
        from dango.platform.sync_process import cleanup_sync_status, get_sync_status_path

        _write_status_file(tmp_path, phase="data_load", pid=os.getpid())
        cleanup_sync_status(tmp_path)
        assert get_sync_status_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# launch_sync_subprocess
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLaunchSyncSubprocess:
    @patch("subprocess.Popen")
    def test_builds_correct_command(self, mock_popen, tmp_path):
        from dango.platform.sync_process import launch_sync_subprocess

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        process = launch_sync_subprocess(
            project_root=tmp_path,
            sources=["hubspot"],
            full_refresh=True,
            source_label="ui",
        )

        assert process is mock_process
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"
        assert cmd[2] == "dango.platform.scheduling.sync_trigger"

        # Verify JSON args
        json_str = cmd[3]
        args = json.loads(json_str)
        assert args["sources"] == ["hubspot"]
        assert args["full_refresh"] is True
        assert args["write_progress"] is True
        assert args["source_label"] == "ui"

    @patch("subprocess.Popen")
    def test_includes_optional_params(self, mock_popen, tmp_path):
        from dango.platform.sync_process import launch_sync_subprocess

        mock_popen.return_value = MagicMock(pid=1)

        launch_sync_subprocess(
            project_root=tmp_path,
            sources=["src"],
            start_date="2026-01-01",
            end_date="2026-01-31",
            backfill_days=7,
            skip_dbt=True,
            max_lock_wait=300,
        )

        json_str = mock_popen.call_args[0][0][3]
        args = json.loads(json_str)
        assert args["start_date"] == "2026-01-01"
        assert args["end_date"] == "2026-01-31"
        assert args["backfill_days"] == 7
        assert args["skip_dbt"] is True
        assert args["max_lock_wait"] == 300

    @patch("subprocess.Popen")
    def test_cleans_stale_status_before_launch(self, mock_popen, tmp_path):
        from dango.platform.sync_process import get_sync_status_path, launch_sync_subprocess

        _write_status_file(tmp_path, phase="completed")
        mock_popen.return_value = MagicMock(pid=1)

        launch_sync_subprocess(project_root=tmp_path, sources=["src"])

        assert not get_sync_status_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# poll_sync_status (async)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPollSyncStatus:
    @pytest.mark.anyio
    async def test_detects_completed_phase(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status

        _write_status_file(tmp_path, phase="completed")
        process = MagicMock()
        process.poll.return_value = 0

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with patch(f"{_WS_MOD}.ws_manager", mock_ws):
            success, result = await poll_sync_status(
                tmp_path, process, "test_source", poll_interval=0.01
            )

        assert success is True
        assert result["phase"] == "completed"

    @pytest.mark.anyio
    async def test_detects_failed_phase(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status

        _write_status_file(tmp_path, phase="failed", error="boom")
        process = MagicMock()
        process.poll.return_value = 1

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with patch(f"{_WS_MOD}.ws_manager", mock_ws):
            success, result = await poll_sync_status(
                tmp_path, process, "test_source", poll_interval=0.01
            )

        assert success is False

    @pytest.mark.anyio
    async def test_crash_detection(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status

        # No status file, but process exits with error
        process = MagicMock()
        process.poll.return_value = 1

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with patch(f"{_WS_MOD}.ws_manager", mock_ws):
            success, result = await poll_sync_status(
                tmp_path, process, "test_source", poll_interval=0.01
            )

        assert success is False
        assert "unexpectedly" in result["error"]
        # Verify sync_failed was broadcast
        broadcast_calls = mock_ws.broadcast.call_args_list
        events = [c.args[0]["event"] for c in broadcast_calls]
        assert "sync_failed" in events

    @pytest.mark.anyio
    async def test_broadcasts_phase_transitions(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status

        # Start with data_load phase, then transition to completed
        call_count = [0]

        def _mock_read(proj_root):
            call_count[0] += 1
            if call_count[0] <= 1:
                return {"pid": 1, "phase": "data_load", "message": "Loading"}
            return {"pid": 1, "phase": "completed", "message": "Done"}

        process = MagicMock()
        process.poll.return_value = None

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with (
            patch(f"{_MOD}.read_sync_status", side_effect=_mock_read),
            patch(f"{_WS_MOD}.ws_manager", mock_ws),
        ):
            success, result = await poll_sync_status(
                tmp_path, process, "test_source", poll_interval=0.01
            )

        assert success is True
        # Should have broadcast for data_load and completed transitions
        assert mock_ws.broadcast.call_count >= 2


# ---------------------------------------------------------------------------
# poll_sync_status_blocking (sync)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPollSyncStatusBlocking:
    def test_detects_completed(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(tmp_path, phase="completed")
        process = MagicMock()
        process.poll.return_value = 0

        with patch(f"{_MOD}.time.sleep"):
            success, result = poll_sync_status_blocking(tmp_path, process, poll_interval=0.01)

        assert success is True

    def test_detects_failed(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(tmp_path, phase="failed", error="oops")
        process = MagicMock()
        process.poll.return_value = 1

        with patch(f"{_MOD}.time.sleep"):
            success, result = poll_sync_status_blocking(tmp_path, process, poll_interval=0.01)

        assert success is False

    def test_crash_detection(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status_blocking

        process = MagicMock()
        process.poll.return_value = 137

        with patch(f"{_MOD}.time.sleep"):
            success, result = poll_sync_status_blocking(tmp_path, process, poll_interval=0.01)

        assert success is False
        assert "unexpectedly" in result["error"]

    def test_calls_broadcast_fn_on_transitions(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(tmp_path, phase="completed")
        process = MagicMock()
        process.poll.return_value = 0
        broadcast_fn = MagicMock()

        with patch(f"{_MOD}.time.sleep"):
            poll_sync_status_blocking(
                tmp_path, process, broadcast_fn=broadcast_fn, poll_interval=0.01
            )

        broadcast_fn.assert_called_once()
        call_msg = broadcast_fn.call_args[0][0]
        assert call_msg["event"] == "sync_completed"
