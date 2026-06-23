"""tests/unit/test_sync_process.py

Tests for dango.platform.sync_process — subprocess launch and polling.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MOD = "dango.platform.sync_process"
_WS_MOD = "dango.web.routes.websocket"


def _write_status_file(tmp_path, phase="starting", sync_id=None, **extra):
    """Write a sync status file for testing."""
    state_dir = tmp_path / ".dango" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    filename = f"sync_status_{sync_id}.json" if sync_id else "sync_status.json"
    status = {
        "pid": os.getpid(),
        "phase": phase,
        "message": f"Test phase: {phase}",
        "sources": ["test_source"],
        **extra,
    }
    with open(state_dir / filename, "w") as f:
        json.dump(status, f)
    return status


@pytest.mark.unit
class TestGetSyncStatusPath:
    def test_returns_expected_path_without_sync_id(self, tmp_path):
        from dango.platform.sync_process import get_sync_status_path

        path = get_sync_status_path(tmp_path)
        assert path == tmp_path / ".dango" / "state" / "sync_status.json"

    def test_returns_expected_path_with_sync_id(self, tmp_path):
        from dango.platform.sync_process import get_sync_status_path

        path = get_sync_status_path(tmp_path, sync_id="abc123")
        assert path == tmp_path / ".dango" / "state" / "sync_status_abc123.json"


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

    def test_reads_file_with_sync_id(self, tmp_path):
        from dango.platform.sync_process import read_sync_status

        _write_status_file(tmp_path, phase="completed", sync_id="xyz789")
        status = read_sync_status(tmp_path, sync_id="xyz789")
        assert status is not None
        assert status["phase"] == "completed"

    def test_returns_none_on_invalid_json(self, tmp_path):
        from dango.platform.sync_process import read_sync_status

        state_dir = tmp_path / ".dango" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "sync_status.json").write_text("not json{{{")
        assert read_sync_status(tmp_path) is None


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

    def test_cleanup_with_sync_id(self, tmp_path):
        from dango.platform.sync_process import cleanup_sync_status, get_sync_status_path

        _write_status_file(tmp_path, phase="completed", sync_id="test123")
        cleanup_sync_status(tmp_path, sync_id="test123")
        assert not get_sync_status_path(tmp_path, sync_id="test123").exists()


@pytest.mark.unit
class TestLaunchSyncSubprocess:
    @patch("subprocess.Popen")
    def test_builds_correct_command(self, mock_popen, tmp_path):
        from dango.platform.sync_process import launch_sync_subprocess

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        process, sync_id, log_path = launch_sync_subprocess(
            project_root=tmp_path,
            sources=["hubspot"],
            full_refresh=True,
            source_label="ui",
        )

        assert process is mock_process
        assert len(sync_id) == 12  # uuid hex[:12]
        assert log_path.name == f"sync_{sync_id}.log"
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
        assert args["sync_id"] == sync_id

    @patch("subprocess.Popen")
    def test_captures_output_to_log_file(self, mock_popen, tmp_path):
        """Stdout/stderr must be captured to a log file for crash diagnostics."""
        import subprocess

        mock_popen.return_value = MagicMock(pid=1)
        launch = __import__(
            "dango.platform.sync_process", fromlist=["launch_sync_subprocess"]
        ).launch_sync_subprocess

        _proc, _sid, log_path = launch(project_root=tmp_path, sources=["src"])

        call_kwargs = mock_popen.call_args[1]
        # stderr should be merged into stdout
        assert call_kwargs["stderr"] == subprocess.STDOUT
        # stdout should NOT be DEVNULL (it's a file handle that was opened then closed)
        assert call_kwargs["stdout"] != subprocess.DEVNULL
        # Log file path should exist in the logs directory
        assert log_path.parent == tmp_path / ".dango" / "logs"

    @patch("subprocess.Popen")
    def test_includes_optional_params(self, mock_popen, tmp_path):
        from dango.platform.sync_process import launch_sync_subprocess

        mock_popen.return_value = MagicMock(pid=1)

        _process, _sync_id, _log_path = launch_sync_subprocess(
            project_root=tmp_path,
            sources=["src"],
            start_date="2026-01-01",
            end_date="2026-01-31",
            backfill_days=7,
            skip_dbt=True,
            max_lock_wait=300,
            record_id=42,
        )

        json_str = mock_popen.call_args[0][0][3]
        args = json.loads(json_str)
        assert args["start_date"] == "2026-01-01"
        assert args["end_date"] == "2026-01-31"
        assert args["backfill_days"] == 7
        assert args["skip_dbt"] is True
        assert args["max_lock_wait"] == 300
        assert args["record_id"] == 42

    @patch("subprocess.Popen")
    def test_returns_unique_sync_ids(self, mock_popen, tmp_path):
        from dango.platform.sync_process import launch_sync_subprocess

        mock_popen.return_value = MagicMock(pid=1)

        _, id1, _ = launch_sync_subprocess(project_root=tmp_path, sources=["src"])
        _, id2, _ = launch_sync_subprocess(project_root=tmp_path, sources=["src"])
        assert id1 != id2


@pytest.mark.unit
class TestPollSyncStatus:
    @pytest.mark.anyio
    async def test_detects_completed_phase(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status

        sid = "test1"
        _write_status_file(tmp_path, phase="completed", sync_id=sid)
        process = MagicMock()
        process.poll.return_value = 0

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with patch(f"{_WS_MOD}.ws_manager", mock_ws):
            success, result = await poll_sync_status(
                tmp_path, process, "test_source", sync_id=sid, poll_interval=0.01
            )

        assert success is True
        assert result["phase"] == "completed"

    @pytest.mark.anyio
    async def test_detects_failed_phase(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status

        sid = "test2"
        _write_status_file(tmp_path, phase="failed", error="boom", sync_id=sid)
        process = MagicMock()
        process.poll.return_value = 1

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with patch(f"{_WS_MOD}.ws_manager", mock_ws):
            success, result = await poll_sync_status(
                tmp_path, process, "test_source", sync_id=sid, poll_interval=0.01
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
                tmp_path, process, "test_source", sync_id="nosuch", poll_interval=0.01
            )

        assert success is False
        assert "unexpectedly" in result["error"]
        broadcast_calls = mock_ws.broadcast.call_args_list
        events = [c.args[0]["event"] for c in broadcast_calls]
        assert "sync_failed" in events

    @pytest.mark.anyio
    async def test_broadcasts_phase_transitions(self, tmp_path):
        from dango.platform.sync_process import poll_sync_status

        call_count = [0]

        def _mock_read(proj_root, sync_id=None):
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
        assert mock_ws.broadcast.call_count >= 2

    @pytest.mark.anyio
    async def test_timeout_terminates_process(self, tmp_path):
        """Polling should terminate subprocess and return failure after max_poll_time."""
        from dango.platform.sync_process import poll_sync_status

        process = MagicMock()
        process.poll.return_value = None  # never exits

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        # Use very short max_poll_time
        with (
            patch(f"{_MOD}.read_sync_status", return_value=None),
            patch(f"{_WS_MOD}.ws_manager", mock_ws),
        ):
            success, result = await poll_sync_status(
                tmp_path,
                process,
                "test_source",
                poll_interval=0.01,
                max_poll_time=0.02,
            )

        assert success is False
        assert "timed out" in result["error"]
        process.terminate.assert_called_once()

    @pytest.mark.anyio
    async def test_heartbeat_emitted(self, tmp_path):
        """Heartbeat should be broadcast every heartbeat_interval seconds."""
        from dango.platform.sync_process import poll_sync_status

        call_count = [0]

        def _mock_read(proj_root, sync_id=None):
            call_count[0] += 1
            # Return completed on 4th read to allow some heartbeats
            if call_count[0] >= 4:
                return {"pid": 1, "phase": "completed", "message": "Done"}
            return None  # no status yet

        process = MagicMock()
        process.poll.return_value = None

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with (
            patch(f"{_MOD}.read_sync_status", side_effect=_mock_read),
            patch(f"{_WS_MOD}.ws_manager", mock_ws),
        ):
            success, _ = await poll_sync_status(
                tmp_path,
                process,
                "test_source",
                poll_interval=0.01,
                heartbeat_interval=0.02,
            )

        assert success is True
        # Check that at least one heartbeat was emitted
        events = [c.args[0]["event"] for c in mock_ws.broadcast.call_args_list]
        assert "sync_progress" in events


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

    def test_broadcast_includes_source_name(self, tmp_path):
        """Blocking poller should include source in broadcast messages."""
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(tmp_path, phase="completed")
        process = MagicMock()
        process.poll.return_value = 0
        broadcast_fn = MagicMock()

        with patch(f"{_MOD}.time.sleep"):
            poll_sync_status_blocking(
                tmp_path,
                process,
                source_name="hubspot",
                broadcast_fn=broadcast_fn,
                poll_interval=0.01,
            )

        call_msg = broadcast_fn.call_args[0][0]
        assert call_msg["source"] == "hubspot"

    def test_locally_polled_registration(self, tmp_path):
        """poll_sync_status_blocking does not use _locally_polled (only async does)."""
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(tmp_path, phase="completed", sync_id="blk1")
        process = MagicMock()
        process.poll.return_value = 0

        with patch(f"{_MOD}.time.sleep"):
            success, _ = poll_sync_status_blocking(
                tmp_path, process, sync_id="blk1", poll_interval=0.01
            )

        assert success is True

    def test_timeout_terminates_process(self, tmp_path):
        """Blocking poller should terminate process after max_poll_time."""
        from dango.platform.sync_process import poll_sync_status_blocking

        process = MagicMock()
        process.poll.return_value = None  # never exits

        # Use time mock so sleep doesn't actually sleep but time advances
        elapsed = [0.0]

        def fake_sleep(n):
            elapsed[0] += n

        def fake_time():
            return elapsed[0]

        with (
            patch(f"{_MOD}.time.sleep", side_effect=fake_sleep),
            patch(f"{_MOD}.time.time", side_effect=fake_time),
            patch(f"{_MOD}.read_sync_status", return_value=None),
        ):
            success, result = poll_sync_status_blocking(
                tmp_path, process, max_poll_time=5.0, poll_interval=2.0
            )

        assert success is False
        assert "timed out" in result["error"]
        process.terminate.assert_called_once()


@pytest.mark.unit
class TestPollSyncStatusLocallyPolled:
    """Tests for _locally_polled registration in async poll_sync_status."""

    @pytest.mark.anyio
    async def test_registers_and_unregisters(self, tmp_path):
        """poll_sync_status should add sync_id to _locally_polled during polling."""
        from dango.platform.sync_process import _locally_polled, poll_sync_status

        sid = "reg_test"
        _write_status_file(tmp_path, phase="completed", sync_id=sid)
        process = MagicMock()
        process.poll.return_value = 0

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with patch(f"{_WS_MOD}.ws_manager", mock_ws):
            assert sid not in _locally_polled
            await poll_sync_status(
                tmp_path, process, "test_source", sync_id=sid, poll_interval=0.01
            )

        # Should be unregistered after poll completes
        assert sid not in _locally_polled


@pytest.mark.unit
class TestSyncStatusWatcher:
    """Tests for start_sync_status_watcher and _sync_status_watcher_loop."""

    @pytest.mark.anyio
    async def test_watcher_broadcasts_on_phase_change(self, tmp_path):
        """Watcher should broadcast when a status file has a new phase."""
        import asyncio

        from dango.platform.sync_process import _sync_status_watcher_loop

        sid = "watch1"
        _write_status_file(tmp_path, phase="data_load", sync_id=sid)

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        iteration = [0]

        async def _counting_sleep(n):
            iteration[0] += 1
            if iteration[0] >= 2:
                raise asyncio.CancelledError

        with (
            patch(f"{_WS_MOD}.ws_manager", mock_ws),
            patch(f"{_MOD}.asyncio.sleep", side_effect=_counting_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _sync_status_watcher_loop(tmp_path, poll_interval=0.1)

        # Should have broadcast at least once
        assert mock_ws.broadcast.call_count >= 1

    @pytest.mark.anyio
    async def test_watcher_skips_locally_polled(self, tmp_path):
        """Watcher should NOT broadcast for sync_ids in _locally_polled."""
        import asyncio

        from dango.platform.sync_process import (
            _locally_polled,
            _sync_status_watcher_loop,
        )

        sid = "polled1"
        _write_status_file(tmp_path, phase="data_load", sync_id=sid)
        _locally_polled.add(sid)

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        iteration = [0]

        async def _counting_sleep(n):
            iteration[0] += 1
            if iteration[0] >= 2:
                raise asyncio.CancelledError

        try:
            with (
                patch(f"{_WS_MOD}.ws_manager", mock_ws),
                patch(f"{_MOD}.asyncio.sleep", side_effect=_counting_sleep),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await _sync_status_watcher_loop(tmp_path, poll_interval=0.1)

            # Should NOT have broadcast since sync_id is in _locally_polled
            mock_ws.broadcast.assert_not_called()
        finally:
            _locally_polled.discard(sid)

    @pytest.mark.anyio
    async def test_watcher_uses_mtime_for_efficiency(self, tmp_path):
        """Watcher should not re-broadcast if file mtime hasn't changed."""
        import asyncio

        from dango.platform.sync_process import _sync_status_watcher_loop

        sid = "mtime1"
        _write_status_file(tmp_path, phase="data_load", sync_id=sid)

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        iteration = [0]

        async def _counting_sleep(n):
            iteration[0] += 1
            if iteration[0] >= 3:
                raise asyncio.CancelledError

        with (
            patch(f"{_WS_MOD}.ws_manager", mock_ws),
            patch(f"{_MOD}.asyncio.sleep", side_effect=_counting_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _sync_status_watcher_loop(tmp_path, poll_interval=0.1)

        # Should broadcast on first detection, but NOT re-broadcast on
        # subsequent iterations (same mtime, same phase)
        assert mock_ws.broadcast.call_count == 1

    @pytest.mark.anyio
    async def test_watcher_no_rebroadcast_terminal_phase(self, tmp_path):
        """Watcher should NOT re-broadcast a terminal phase on subsequent iterations."""
        import asyncio

        from dango.platform.sync_process import _sync_status_watcher_loop

        sid = "term1"
        _write_status_file(tmp_path, phase="completed", sync_id=sid)

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        iteration = [0]

        async def _counting_sleep(n):
            iteration[0] += 1
            if iteration[0] >= 4:
                raise asyncio.CancelledError

        with (
            patch(f"{_WS_MOD}.ws_manager", mock_ws),
            patch(f"{_MOD}.asyncio.sleep", side_effect=_counting_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _sync_status_watcher_loop(tmp_path, poll_interval=0.1)

        # Should broadcast "completed" once, not on every subsequent iteration
        assert mock_ws.broadcast.call_count == 1

    @pytest.mark.anyio
    async def test_start_returns_task(self, tmp_path):
        """start_sync_status_watcher should return a cancellable asyncio.Task."""
        import asyncio

        from dango.platform.sync_process import start_sync_status_watcher

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        with patch(f"{_WS_MOD}.ws_manager", mock_ws):
            task = await start_sync_status_watcher(tmp_path, poll_interval=0.01)

        assert isinstance(task, asyncio.Task)
        assert not task.done()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.done()


@pytest.mark.unit
class TestBroadcastDbtError:
    """Tests for BUG-230: dbt_error flag in broadcast functions."""

    @pytest.mark.anyio
    async def test_broadcast_dbt_error_sends_dbt_run_all_failed(self):
        """_broadcast_phase_transition with dbt_error should emit dbt_run_all_failed."""
        from dango.platform.sync_process import _broadcast_phase_transition

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        status = {"message": "dbt models failed", "error": "dbt models failed", "dbt_error": True}
        await _broadcast_phase_transition(mock_ws, "hubspot", "failed", status)

        call_msg = mock_ws.broadcast.call_args[0][0]
        assert call_msg["event"] == "dbt_run_all_failed"
        assert call_msg["source"] == "dbt (triggered by hubspot)"
        assert call_msg["error"] == "dbt models failed"

    @pytest.mark.anyio
    async def test_broadcast_generic_failure_sends_sync_failed(self):
        """_broadcast_phase_transition without dbt_error should emit sync_failed."""
        from dango.platform.sync_process import _broadcast_phase_transition

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        status = {"message": "DuckDB crash", "error": "DuckDB crash"}
        await _broadcast_phase_transition(mock_ws, "hubspot", "failed", status)

        call_msg = mock_ws.broadcast.call_args[0][0]
        assert call_msg["event"] == "sync_failed"
        assert call_msg["source"] == "hubspot"

    def test_blocking_poll_dbt_error_overrides_event(self, tmp_path):
        """poll_sync_status_blocking with dbt_error should emit dbt_run_all_failed."""
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(
            tmp_path,
            phase="failed",
            sync_id="dbt_err",
            error="dbt models failed",
            dbt_error=True,
        )
        process = MagicMock()
        process.poll.return_value = 1
        broadcast_fn = MagicMock()

        with patch(f"{_MOD}.time.sleep"):
            success, result = poll_sync_status_blocking(
                tmp_path,
                process,
                source_name="hubspot",
                sync_id="dbt_err",
                broadcast_fn=broadcast_fn,
                poll_interval=0.01,
            )

        assert success is False
        call_msg = broadcast_fn.call_args[0][0]
        assert call_msg["event"] == "dbt_run_all_failed"
        assert call_msg["source"] == "dbt (triggered by hubspot)"


@pytest.mark.unit
class TestPhaseToEvent:
    """Tests for _phase_to_event mapping function."""

    def test_post_sync_phase_mappings(self):
        """post_sync_started and post_sync_completed map to themselves."""
        from dango.platform.sync_process import _phase_to_event

        assert _phase_to_event("post_sync_started") == "post_sync_started"
        assert _phase_to_event("post_sync_completed") == "post_sync_completed"

    def test_existing_mappings_unchanged(self):
        """Existing phase mappings are not affected."""
        from dango.platform.sync_process import _phase_to_event

        assert _phase_to_event("lock_waiting") == "sync_queued"
        assert _phase_to_event("data_load_complete") == "data_load_complete"
        assert _phase_to_event("dbt_started") == "dbt_run_all_started"
        assert _phase_to_event("dbt_complete") == "dbt_run_all_completed"
        assert _phase_to_event("completed") == "sync_completed"
        assert _phase_to_event("failed") == "sync_failed"

    def test_unknown_phase_defaults_to_sync_progress(self):
        """Unknown phases (including removed 'starting') fall back to sync_progress."""
        from dango.platform.sync_process import _phase_to_event

        assert _phase_to_event("starting") == "sync_progress"
        assert _phase_to_event("nonexistent_phase") == "sync_progress"


@pytest.mark.unit
class TestBroadcastPostSyncPhases:
    """Tests for broadcast behavior with post_sync phase events."""

    @pytest.mark.anyio
    async def test_broadcast_post_sync_started_includes_source(self):
        """_broadcast_phase_transition for post_sync_started includes source name."""
        from dango.platform.sync_process import _broadcast_phase_transition

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        status = {"message": "Running post-sync hooks (profiling, PII scan, analysis, snapshots)"}
        await _broadcast_phase_transition(mock_ws, "hubspot", "post_sync_started", status)

        call_msg = mock_ws.broadcast.call_args[0][0]
        assert call_msg["event"] == "post_sync_started"
        assert call_msg["source"] == "hubspot"
        assert call_msg["message"] == status["message"]

    @pytest.mark.anyio
    async def test_broadcast_post_sync_completed_includes_source(self):
        """_broadcast_phase_transition for post_sync_completed includes source name."""
        from dango.platform.sync_process import _broadcast_phase_transition

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()

        status = {"message": "Post-sync hooks complete"}
        await _broadcast_phase_transition(mock_ws, "stripe", "post_sync_completed", status)

        call_msg = mock_ws.broadcast.call_args[0][0]
        assert call_msg["event"] == "post_sync_completed"
        assert call_msg["source"] == "stripe"
        assert call_msg["message"] == status["message"]


@pytest.mark.unit
class TestBlockingPollPostSyncPhases:
    """Tests for poll_sync_status_blocking with post_sync phase transitions."""

    def test_blocking_poll_post_sync_started(self, tmp_path):
        """poll_sync_status_blocking broadcasts post_sync_started for new phase."""
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(
            tmp_path,
            phase="post_sync_started",
            sync_id="ps_block",
            message="Running post-sync hooks",
        )
        process = MagicMock()
        process.poll.return_value = None  # still running
        broadcast_fn = MagicMock()

        read_count = [0]

        def _side_effect(proj_root, sync_id=None):
            read_count[0] += 1
            if read_count[0] == 1:
                return {
                    "pid": os.getpid(),
                    "phase": "post_sync_started",
                    "message": "Running post-sync hooks",
                    "sources": ["hubspot"],
                }
            # Return completed on next read to terminate
            return {
                "pid": os.getpid(),
                "phase": "completed",
                "message": "Done",
                "sources": ["hubspot"],
            }

        with (
            patch(f"{_MOD}.read_sync_status", side_effect=_side_effect),
            patch(f"{_MOD}.time.sleep"),
        ):
            success, _ = poll_sync_status_blocking(
                tmp_path,
                process,
                source_name="hubspot",
                sync_id="ps_block",
                broadcast_fn=broadcast_fn,
                poll_interval=0.01,
            )

        assert success is True
        # Should have broadcast at least post_sync_started and sync_completed
        events = [c[0][0]["event"] for c in broadcast_fn.call_args_list]
        assert "post_sync_started" in events
        assert "sync_completed" in events

    def test_blocking_poll_post_sync_completed(self, tmp_path):
        """poll_sync_status_blocking broadcasts post_sync_completed for completed phase."""
        from dango.platform.sync_process import poll_sync_status_blocking

        _write_status_file(
            tmp_path,
            phase="post_sync_completed",
            sync_id="ps_comp",
            message="Post-sync hooks complete",
        )
        process = MagicMock()
        process.poll.return_value = None  # still running
        broadcast_fn = MagicMock()

        read_count = [0]

        def _side_effect(proj_root, sync_id=None):
            read_count[0] += 1
            if read_count[0] == 1:
                return {
                    "pid": os.getpid(),
                    "phase": "post_sync_completed",
                    "message": "Post-sync hooks complete",
                    "sources": ["stripe"],
                }
            return {
                "pid": os.getpid(),
                "phase": "completed",
                "message": "Done",
                "sources": ["stripe"],
            }

        with (
            patch(f"{_MOD}.read_sync_status", side_effect=_side_effect),
            patch(f"{_MOD}.time.sleep"),
        ):
            success, _ = poll_sync_status_blocking(
                tmp_path,
                process,
                source_name="stripe",
                sync_id="ps_comp",
                broadcast_fn=broadcast_fn,
                poll_interval=0.01,
            )

        assert success is True
        events = [c[0][0]["event"] for c in broadcast_fn.call_args_list]
        assert "post_sync_completed" in events
        assert "sync_completed" in events
