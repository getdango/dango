"""tests/unit/test_process_manager.py

Tests for dango.cli.helpers.process_manager — FastAPI server process management.
"""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.helpers.process_manager import (
    get_fastapi_status,
    get_pid_file_path,
    read_pid_file,
    remove_pid_file,
    start_fastapi_server,
    stop_fastapi_server,
    write_pid_file,
)


@pytest.mark.unit
class TestGetPidFilePath:
    def test_returns_correct_path(self, tmp_path):
        result = get_pid_file_path(tmp_path)
        assert result == tmp_path / ".dango" / "web.pid"


@pytest.mark.unit
class TestWritePidFile:
    def test_writes_pid_creates_parent_dirs(self, tmp_path):
        write_pid_file(tmp_path, 1234)
        pid_file = tmp_path / ".dango" / "web.pid"
        assert pid_file.read_text() == "1234"

    def test_overwrites_existing_pid_file(self, tmp_path):
        write_pid_file(tmp_path, 1111)
        write_pid_file(tmp_path, 2222)
        pid_file = tmp_path / ".dango" / "web.pid"
        assert pid_file.read_text() == "2222"


@pytest.mark.unit
class TestReadPidFile:
    def test_valid_pid(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "web.pid").write_text("5678")
        assert read_pid_file(tmp_path) == 5678

    def test_missing_file(self, tmp_path):
        assert read_pid_file(tmp_path) is None

    def test_invalid_content(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "web.pid").write_text("not-a-pid")
        assert read_pid_file(tmp_path) is None

    def test_empty_file(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "web.pid").write_text("")
        assert read_pid_file(tmp_path) is None


@pytest.mark.unit
class TestRemovePidFile:
    def test_removes_existing_file(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        pid_file = dango_dir / "web.pid"
        pid_file.write_text("1234")

        remove_pid_file(tmp_path)
        assert not pid_file.exists()

    def test_no_file_no_error(self, tmp_path):
        # Should not raise
        remove_pid_file(tmp_path)

    def test_oserror_on_unlink_silently_caught(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        pid_file = dango_dir / "web.pid"
        pid_file.write_text("1234")

        with patch.object(type(pid_file), "unlink", side_effect=OSError("perm denied")):
            # Should not raise
            remove_pid_file(tmp_path)


@pytest.mark.unit
class TestStartFastapiServer:
    def _setup_project(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    @patch("dango.cli.helpers.process_manager.time.sleep")
    @patch("dango.cli.helpers.process_manager.subprocess.Popen")
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_successful_start(self, _mock_running, _mock_port, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 6000
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pid = start_fastapi_server(tmp_path)

        assert pid == 6000
        pid_file = tmp_path / ".dango" / "web.pid"
        assert pid_file.read_text() == "6000"

    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=True)
    def test_already_running_raises(self, _mock_running, tmp_path):
        self._setup_project(tmp_path)
        (tmp_path / ".dango" / "web.pid").write_text("1234")

        with pytest.raises(RuntimeError, match="already running"):
            start_fastapi_server(tmp_path)

    @patch("dango.cli.helpers.process_manager.time.sleep")
    @patch("dango.cli.helpers.process_manager.subprocess.Popen")
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_stale_pid_cleaned_up(
        self, _mock_running, _mock_port, mock_popen, mock_sleep, tmp_path
    ):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text("9999")

        mock_proc = MagicMock()
        mock_proc.pid = 7000
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pid = start_fastapi_server(tmp_path)
        assert pid == 7000

    @patch("dango.cli.helpers.process_manager.get_process_using_port", return_value=5555)
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=True)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_port_in_use_pid_identifiable(
        self, _mock_running, _mock_port, _mock_get_port, tmp_path
    ):
        self._setup_project(tmp_path)
        with pytest.raises(RuntimeError, match="PID 5555"):
            start_fastapi_server(tmp_path)

    @patch("dango.cli.helpers.process_manager.get_process_using_port", return_value=None)
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=True)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_port_in_use_pid_unknown(self, _mock_running, _mock_port, _mock_get_port, tmp_path):
        self._setup_project(tmp_path)
        with pytest.raises(RuntimeError, match="already in use"):
            start_fastapi_server(tmp_path)

    @patch("dango.cli.helpers.process_manager.time.sleep")
    @patch("dango.cli.helpers.process_manager.subprocess.Popen")
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_process_exits_immediately_raises(
        self, _mock_running, _mock_port, mock_popen, mock_sleep, tmp_path
    ):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_popen.return_value = mock_proc

        with pytest.raises(RuntimeError, match="failed to start"):
            start_fastapi_server(tmp_path)

    @patch("dango.cli.helpers.process_manager.time.sleep")
    @patch("dango.cli.helpers.process_manager.subprocess.Popen")
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_sleep_called(self, _mock_running, _mock_port, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 6001
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        start_fastapi_server(tmp_path)
        mock_sleep.assert_called_once_with(2)

    @patch("dango.cli.helpers.process_manager.time.sleep")
    @patch("dango.cli.helpers.process_manager.subprocess.Popen")
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_popen_args(self, _mock_running, _mock_port, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 6002
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        start_fastapi_server(tmp_path)

        popen_args = mock_popen.call_args
        cmd = popen_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"
        assert cmd[2] == "uvicorn"
        assert cmd[3] == "dango.web.app:app"
        assert "--host" in cmd
        assert "--port" in cmd
        assert "8080" in cmd
        assert popen_args[1]["cwd"] == tmp_path
        assert popen_args[1]["start_new_session"] is True

    @patch("dango.cli.helpers.process_manager.time.sleep")
    @patch("dango.cli.helpers.process_manager.subprocess.Popen")
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_custom_host_port(self, _mock_running, _mock_port, mock_popen, mock_sleep, tmp_path):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 6003
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        start_fastapi_server(tmp_path, host="127.0.0.1", port=9090)

        cmd = mock_popen.call_args[0][0]
        assert "127.0.0.1" in cmd
        assert "9090" in cmd


@pytest.mark.unit
class TestStopFastapiServer:
    def _setup_project(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    # --- PID-based phase ---

    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.kill_process", return_value=True)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=True)
    def test_pid_running_kill_succeeds(self, _mock_running, mock_kill, _mock_console, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text("4000")

        assert stop_fastapi_server(tmp_path) is True
        mock_kill.assert_called_once_with(4000, timeout=10)

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.kill_process", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=True)
    def test_pid_running_kill_fails_falls_through(
        self, _mock_running, mock_kill, _mock_console, mock_sub_run, tmp_path
    ):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text("4000")

        # Port fallback: ConfigLoader + lsof
        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config
            # lsof finds nothing
            mock_sub_run.return_value = MagicMock(returncode=1, stdout="")

            assert stop_fastapi_server(tmp_path) is False

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_stale_pid_removed_falls_through(
        self, _mock_running, mock_console, mock_sub_run, tmp_path
    ):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text("9999")

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config
            mock_sub_run.return_value = MagicMock(returncode=1, stdout="")

            assert stop_fastapi_server(tmp_path) is False

        # PID file removed
        assert not (dango_dir / "web.pid").exists()

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_stale_pid_verbose_prints_message(
        self, _mock_running, mock_console, mock_sub_run, tmp_path
    ):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text("9999")

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config
            mock_sub_run.return_value = MagicMock(returncode=1, stdout="")

            stop_fastapi_server(tmp_path, verbose=True)

        # Should have printed stale message
        print_calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("stale" in c.lower() or "not running" in c.lower() for c in print_calls)

    # --- Port-based fallback phase ---

    @patch("dango.cli.helpers.process_manager.kill_process", return_value=True)
    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_no_pid_dango_process_on_port(
        self, _mock_running, _mock_console, mock_sub_run, mock_kill, tmp_path
    ):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config

            # lsof finds a PID
            lsof_result = MagicMock(returncode=0, stdout="1234\n")
            # ps shows it's a dango uvicorn process
            ps_result = MagicMock(
                returncode=0,
                stdout="python -m uvicorn dango.web.app:app --host 0.0.0.0 --port 8080",
            )
            mock_sub_run.side_effect = [lsof_result, ps_result]

            assert stop_fastapi_server(tmp_path) is True
            mock_kill.assert_called_once_with(1234, timeout=5)

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_no_pid_non_dango_process_on_port(
        self, _mock_running, _mock_console, mock_sub_run, tmp_path
    ):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config

            lsof_result = MagicMock(returncode=0, stdout="5555\n")
            ps_result = MagicMock(returncode=0, stdout="nginx: master process")
            mock_sub_run.side_effect = [lsof_result, ps_result]

            assert stop_fastapi_server(tmp_path) is False

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_no_pid_no_processes_on_port(
        self, _mock_running, _mock_console, mock_sub_run, tmp_path
    ):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config
            mock_sub_run.return_value = MagicMock(returncode=1, stdout="")

            assert stop_fastapi_server(tmp_path) is False

    @patch("dango.cli.helpers.process_manager.kill_process", return_value=False)
    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_dango_pids_found_but_all_kills_fail(
        self, _mock_running, _mock_console, mock_sub_run, mock_kill, tmp_path
    ):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config

            lsof_result = MagicMock(returncode=0, stdout="1234\n")
            ps_result = MagicMock(
                returncode=0,
                stdout="python -m uvicorn dango.web.app:app",
            )
            mock_sub_run.side_effect = [lsof_result, ps_result]

            assert stop_fastapi_server(tmp_path) is False

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_processes_disappear_between_lsof_and_ps(
        self, _mock_running, _mock_console, mock_sub_run, tmp_path
    ):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config

            lsof_result = MagicMock(returncode=0, stdout="1234\n")
            # ps fails — process gone
            ps_result = MagicMock(returncode=1, stdout="")
            mock_sub_run.side_effect = [lsof_result, ps_result]

            assert stop_fastapi_server(tmp_path) is False

    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_config_loader_exception_returns_false(self, _mock_running, mock_console, tmp_path):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_cl.side_effect = Exception("config broken")

            assert stop_fastapi_server(tmp_path) is False
            # Warning printed
            print_calls = [str(c) for c in mock_console.print.call_args_list]
            assert any("config broken" in c for c in print_calls)

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_verbose_false_no_console_print(
        self, _mock_running, mock_console, mock_sub_run, tmp_path
    ):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config
            mock_sub_run.return_value = MagicMock(returncode=1, stdout="")

            stop_fastapi_server(tmp_path, verbose=False)
            mock_console.print.assert_not_called()

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_lsof_timeout_returns_false(self, _mock_running, mock_console, mock_sub_run, tmp_path):
        self._setup_project(tmp_path)

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config
            mock_sub_run.side_effect = subprocess.TimeoutExpired(cmd="lsof", timeout=5)

            assert stop_fastapi_server(tmp_path) is False


@pytest.mark.unit
class TestGetFastapiStatus:
    def _setup_project(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    def test_no_pid_file(self, tmp_path):
        self._setup_project(tmp_path)
        status = get_fastapi_status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None
        assert status["url"] is None
        # Default port (8800 from PlatformSettings) and log file path
        assert status["port"] == 8800
        assert status["log_file"] == tmp_path / ".dango" / "web.log"

    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=True)
    def test_running_process(self, _mock_running, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text("7777")
        status = get_fastapi_status(tmp_path)
        assert status["running"] is True
        assert status["pid"] == 7777
        assert status["url"] == "http://localhost:8800"

    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_stale_pid(self, _mock_running, tmp_path):
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text("9999")
        status = get_fastapi_status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None
        assert status["url"] is None
