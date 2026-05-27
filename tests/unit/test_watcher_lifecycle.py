"""tests/unit/test_watcher_lifecycle.py

Tests for dango.platform.watcher_lifecycle — watcher subprocess lifecycle management.
"""

from unittest.mock import MagicMock, patch

import pytest

from dango.platform.watcher_lifecycle import (
    get_watcher_pid_file_path,
    get_watcher_status,
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

    @patch("dango.platform.local.watcher_lifecycle.time.sleep")
    @patch("dango.platform.local.watcher_lifecycle.subprocess.Popen")
    @patch("dango.platform.local.watcher_lifecycle.is_process_running")
    def test_successful_start_returns_pid(self, mock_running, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 5555
        mock_proc.poll.return_value = None  # Still running
        mock_popen.return_value = mock_proc

        pid = start_file_watcher(tmp_path)

        assert pid == 5555
        pid_file = tmp_path / ".dango" / "watcher.pid"
        assert pid_file.read_text() == "5555"

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
        mock_kill.assert_called_once_with(4444, timeout=10)
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
