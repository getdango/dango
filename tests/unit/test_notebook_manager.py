"""tests/unit/test_notebook_manager.py

Tests for dango.notebooks.manager — Marimo process lifecycle.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestGetMarimoPidFilePath:
    def test_returns_correct_path(self, tmp_path):
        result = self._call(tmp_path)
        assert result == tmp_path / ".dango" / "marimo.pid"

    def _call(self, project_root):
        from dango.notebooks.manager import get_marimo_pid_file_path

        return get_marimo_pid_file_path(project_root)


@pytest.mark.unit
class TestStartMarimo:
    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    @patch("dango.notebooks.manager.subprocess.Popen")
    def test_start_creates_pid_file(self, mock_popen, mock_running, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notebooks").mkdir(parents=True, exist_ok=True)

        with patch("dango.notebooks.manager.time.sleep"):
            pid = self._call(tmp_path, port=7805)

        assert pid == 12345
        pid_file = tmp_path / ".dango" / "marimo.pid"
        assert pid_file.exists()
        assert pid_file.read_text() == "12345"

    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    def test_start_raises_if_already_running(self, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999")

        with pytest.raises(RuntimeError, match="already running"):
            self._call(tmp_path, port=7805)

    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    @patch("dango.notebooks.manager.subprocess.Popen")
    def test_start_cleans_stale_pid(self, mock_popen, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999")

        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        with patch("dango.notebooks.manager.time.sleep"):
            pid = self._call(tmp_path, port=7805)

        assert pid == 11111

    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    @patch("dango.notebooks.manager.subprocess.Popen")
    def test_start_raises_if_process_exits_immediately(self, mock_popen, mock_running, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 1  # Exited with error
        mock_popen.return_value = mock_proc

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)

        with patch("dango.notebooks.manager.time.sleep"):
            with pytest.raises(RuntimeError, match="failed to start"):
                self._call(tmp_path, port=7805)

    def _call(self, project_root, port=None):
        from dango.notebooks.manager import start_marimo

        return start_marimo(project_root, port=port)


@pytest.mark.unit
class TestStopMarimo:
    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    @patch("dango.notebooks.manager.kill_process", return_value=True)
    def test_stop_returns_true(self, mock_kill, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("12345")

        from dango.notebooks.manager import stop_marimo

        result = stop_marimo(tmp_path)
        assert result is True
        mock_kill.assert_called_once_with(12345, timeout=10)

    def test_stop_returns_false_no_pid_file(self, tmp_path):
        from dango.notebooks.manager import stop_marimo

        result = stop_marimo(tmp_path)
        assert result is False

    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    def test_stop_cleans_stale_pid(self, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999")

        from dango.notebooks.manager import stop_marimo

        result = stop_marimo(tmp_path)
        assert result is False
        assert not pid_file.exists()


@pytest.mark.unit
class TestGetMarimoStatus:
    def test_status_not_running(self, tmp_path):
        from dango.notebooks.manager import get_marimo_status

        status = get_marimo_status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None

    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    def test_status_running(self, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("12345")

        from dango.notebooks.manager import get_marimo_status

        with patch("dango.config.loader.ConfigLoader") as mock_loader:
            mock_config = MagicMock()
            mock_config.platform.marimo_port = 7805
            mock_loader.return_value.load_config.return_value = mock_config

            status = get_marimo_status(tmp_path)

        assert status["running"] is True
        assert status["pid"] == 12345
        assert status["port"] == 7805
