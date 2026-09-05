"""tests/unit/test_watcher_lifecycle.py

Tests for dango.platform.watcher_lifecycle — watcher subprocess lifecycle management.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from dango.platform.watcher_lifecycle import (
    get_watcher_pid_file_path,
    get_watcher_status,
    kill_orphan_watchers,
    start_file_watcher,
    stop_file_watcher,
)


@pytest.mark.unit
class TestGetWatcherPidFilePath:
    def test_returns_correct_path(self, tmp_path):
        result = get_watcher_pid_file_path(tmp_path)
        assert result == tmp_path / ".dango" / "watcher.pid"


@pytest.mark.unit
class TestStartFileWatcher:
    def _setup_project(self, tmp_path):
        """Create .dango dir so PID file operations work."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    @patch("dango.utils.process.get_process_start_time", return_value=42.0)
    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running")
    def test_successful_start_returns_pid(
        self, mock_running, mock_popen, mock_sleep, _mock_start_time, tmp_path
    ):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 5555
        mock_proc.poll.return_value = None  # Still running
        mock_popen.return_value = mock_proc

        pid = start_file_watcher(tmp_path)

        assert pid == 5555
        pid_file = tmp_path / ".dango" / "watcher.pid"
        assert json.loads(pid_file.read_text()) == {"pid": 5555, "start_time": 42.0}

    @patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=True)
    def test_already_running_raises_runtime_error(self, _mock_running, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text("1234")

        with pytest.raises(RuntimeError, match="already running"):
            start_file_watcher(tmp_path)

    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=False)
    def test_stale_pid_file_cleaned_up(self, mock_running, mock_popen, mock_sleep, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text("9999")

        mock_proc = MagicMock()
        mock_proc.pid = 7777
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pid = start_file_watcher(tmp_path)
        assert pid == 7777

    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running")
    def test_invalid_pid_file_cleaned_up(self, mock_running, mock_popen, mock_sleep, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text("not-a-number")

        mock_proc = MagicMock()
        mock_proc.pid = 8888
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pid = start_file_watcher(tmp_path)
        assert pid == 8888

    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running")
    def test_process_exits_immediately_raises(self, mock_running, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # Exited with error
        mock_popen.return_value = mock_proc

        with pytest.raises(RuntimeError, match="failed to start"):
            start_file_watcher(tmp_path)

    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running")
    def test_popen_called_with_correct_args(self, mock_running, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 1111
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        start_file_watcher(tmp_path)

        args = mock_popen.call_args
        cmd = args[0][0]
        # cmd[0] is sys.executable, cmd[1] is watcher_runner.py path, cmd[2] is project_root
        assert cmd[1].endswith("watcher_runner.py")
        assert cmd[2] == str(tmp_path)
        assert args[1]["start_new_session"] is True

    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running")
    def test_sleep_called(self, mock_running, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 2222
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        start_file_watcher(tmp_path)
        mock_sleep.assert_called_once_with(1)

    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running")
    def test_popen_exception_raises_runtime_error(
        self, mock_running, mock_popen, mock_sleep, tmp_path
    ):
        self._setup_project(tmp_path)
        mock_popen.side_effect = FileNotFoundError("python not found")

        with pytest.raises(RuntimeError, match="Failed to start file watcher"):
            start_file_watcher(tmp_path)


@pytest.mark.unit
class TestStopFileWatcher:
    def _setup_project(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    def test_no_pid_file_returns_false(self, tmp_path):
        self._setup_project(tmp_path)
        assert stop_file_watcher(tmp_path) is False

    def test_invalid_pid_content_returns_false(self, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text("garbage")

        assert stop_file_watcher(tmp_path) is False
        assert not pid_file.exists()

    @patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=False)
    def test_stale_pid_returns_false(self, _mock_running, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text("9999")

        assert stop_file_watcher(tmp_path) is False
        assert not pid_file.exists()

    @patch("dango.platform.local.watcher_lifecycle.kill_process", return_value=True)
    @patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=True)
    def test_successful_kill_returns_true(self, _mock_running, mock_kill, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text("4444")

        assert stop_file_watcher(tmp_path) is True
        # Old-format (bare-integer) PID file in this test → identity unknown (None).
        mock_kill.assert_called_once_with(4444, timeout=10, expected_start_time=None)
        assert not pid_file.exists()

    @patch("dango.platform.local.watcher_lifecycle.kill_process", return_value=False)
    @patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=True)
    def test_kill_fails_returns_false(self, _mock_running, mock_kill, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text("4444")

        assert stop_file_watcher(tmp_path) is False
        assert not pid_file.exists()  # PID file still cleaned up


@pytest.mark.unit
class TestStopFileWatcherIdentityIntegration:
    """1.0.8-OPS-1 end-to-end coverage for watcher.pid: real is_process_running()/
    kill_process() (only psutil mocked), same bug class and fix shape as web.pid."""

    def _setup_project(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    def _mock_psutil_exceptions(self, mock_psutil):
        import psutil as real_psutil

        mock_psutil.NoSuchProcess = real_psutil.NoSuchProcess
        mock_psutil.AccessDenied = real_psutil.AccessDenied
        mock_psutil.ZombieProcess = real_psutil.ZombieProcess

    @patch("dango.utils.process.psutil")
    def test_reused_pid_is_not_signaled(self, mock_psutil, tmp_path):
        """Mismatch case: watcher.pid records a process that has since exited; the
        OS reused the PID for something unrelated. Must not be signaled."""
        self._mock_psutil_exceptions(mock_psutil)
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text(json.dumps({"pid": 6543, "start_time": 1000.0}))

        mock_psutil.pid_exists.return_value = True
        mock_reused_proc = MagicMock()
        mock_reused_proc.create_time.return_value = 9_999_999.0
        mock_psutil.Process.return_value = mock_reused_proc

        assert stop_file_watcher(tmp_path) is False
        mock_reused_proc.terminate.assert_not_called()
        mock_reused_proc.kill.assert_not_called()
        assert not pid_file.exists()

    @patch("dango.utils.process.psutil")
    def test_matching_pid_is_signaled(self, mock_psutil, tmp_path):
        """Match case (positive control): recorded start_time agrees with the live
        process's create_time() — must still kill normally."""
        self._mock_psutil_exceptions(mock_psutil)
        dango_dir = self._setup_project(tmp_path)
        pid_file = dango_dir / "watcher.pid"
        pid_file.write_text(json.dumps({"pid": 6543, "start_time": 1000.0}))

        mock_psutil.pid_exists.return_value = True
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 1000.0
        mock_proc.children.return_value = []
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.wait_procs.return_value = ([mock_proc], [])

        assert stop_file_watcher(tmp_path) is True
        mock_proc.terminate.assert_called_once()
        assert not pid_file.exists()


@pytest.mark.unit
class TestGetWatcherStatus:
    def _setup_project(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    def test_no_pid_file(self, tmp_path):
        self._setup_project(tmp_path)
        status = get_watcher_status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None
        assert status["log_file"] == tmp_path / ".dango" / "watcher.log"

    @patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=True)
    def test_pid_file_with_running_process(self, _mock_running, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "watcher.pid").write_text("3333")

        status = get_watcher_status(tmp_path)
        assert status["running"] is True
        assert status["pid"] == 3333

    @patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=False)
    def test_stale_pid_file(self, _mock_running, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "watcher.pid").write_text("9999")

        status = get_watcher_status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None

    def test_invalid_pid_file(self, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "watcher.pid").write_text("bad")

        status = get_watcher_status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None


def _make_proc(pid: int, cmdline: list[str]) -> MagicMock:
    """Create a mock process for psutil.process_iter."""
    proc = MagicMock()
    proc.pid = pid
    proc.info = {"pid": pid, "cmdline": cmdline}
    return proc


@pytest.mark.unit
class TestKillOrphanWatchers:
    def test_no_matching_processes_returns_zero(self, tmp_path: Path) -> None:
        with patch("psutil.process_iter", return_value=[]):
            assert kill_orphan_watchers(tmp_path) == 0

    def test_matching_process_killed(self, tmp_path: Path) -> None:
        resolved = str(tmp_path.resolve())
        proc = _make_proc(12345, ["python3", "watcher_runner.py", resolved])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch(
                "dango.platform.local.watcher_lifecycle.kill_process", return_value=True
            ) as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 1
        mock_kill.assert_called_once_with(12345)

    def test_different_project_root_not_killed(self, tmp_path: Path) -> None:
        other_root = "/some/other/project"
        proc = _make_proc(12345, ["python3", "watcher_runner.py", other_root])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()

    def test_empty_cmdline_skipped(self, tmp_path: Path) -> None:
        """Process with empty cmdline is skipped gracefully."""
        proc = _make_proc(12345, [])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()

    def test_skips_current_process(self, tmp_path: Path) -> None:
        resolved = str(tmp_path.resolve())
        proc = _make_proc(99999, ["python3", "watcher_runner.py", resolved])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()

    def test_nosuchprocess_skipped(self, tmp_path: Path) -> None:
        """NoSuchProcess during iteration is handled gracefully."""
        proc = MagicMock()
        proc.pid = 12345
        type(proc).info = property(
            lambda self: (_ for _ in ()).throw(psutil.NoSuchProcess(pid=12345))
        )
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()
