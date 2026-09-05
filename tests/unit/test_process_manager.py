"""tests/unit/test_process_manager.py

Tests for dango.cli.helpers.process_manager — FastAPI server process management.
"""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.helpers.process_manager import (
    get_fastapi_status,
    get_pid_file_path,
    read_pid_file,
    read_pid_record_for_project,
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
    @patch("dango.utils.process.get_process_start_time", return_value=1234567.89)
    def test_writes_pid_creates_parent_dirs(self, _mock_start_time, tmp_path):
        write_pid_file(tmp_path, 1234)
        pid_file = tmp_path / ".dango" / "web.pid"
        data = json.loads(pid_file.read_text())
        assert data == {"pid": 1234, "start_time": 1234567.89}

    @patch("dango.utils.process.get_process_start_time", return_value=None)
    def test_writes_pid_with_unknown_start_time(self, _mock_start_time, tmp_path):
        # e.g. the process couldn't be inspected right after being spawned
        write_pid_file(tmp_path, 1234)
        pid_file = tmp_path / ".dango" / "web.pid"
        data = json.loads(pid_file.read_text())
        assert data == {"pid": 1234, "start_time": None}

    @patch("dango.utils.process.get_process_start_time", return_value=111.0)
    def test_overwrites_existing_pid_file(self, mock_start_time, tmp_path):
        write_pid_file(tmp_path, 1111)
        mock_start_time.return_value = 222.0
        write_pid_file(tmp_path, 2222)
        pid_file = tmp_path / ".dango" / "web.pid"
        data = json.loads(pid_file.read_text())
        assert data == {"pid": 2222, "start_time": 222.0}


@pytest.mark.unit
class TestReadPidFile:
    def test_valid_pid_json_format(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "web.pid").write_text(json.dumps({"pid": 5678, "start_time": 42.0}))
        assert read_pid_file(tmp_path) == 5678

    def test_valid_pid_old_bare_integer_format(self, tmp_path):
        """Old-format (pre-1.0.8-OPS-1) PID files are a bare integer, no JSON."""
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
class TestReadPidRecordForProject:
    def test_json_format_returns_full_record(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "web.pid").write_text(json.dumps({"pid": 5678, "start_time": 42.5}))
        record = read_pid_record_for_project(tmp_path)
        assert record.pid == 5678
        assert record.start_time == 42.5

    def test_old_bare_integer_format_has_unknown_start_time(self, tmp_path):
        """Old-format PID files (pre-1.0.8-OPS-1) don't crash — identity unknown."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "web.pid").write_text("5678")
        record = read_pid_record_for_project(tmp_path)
        assert record.pid == 5678
        assert record.start_time is None

    def test_missing_file(self, tmp_path):
        assert read_pid_record_for_project(tmp_path) is None

    def test_invalid_content(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "web.pid").write_text("not-a-pid")
        assert read_pid_record_for_project(tmp_path) is None


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

    @patch("dango.utils.process.get_process_start_time", return_value=99.0)
    @patch("dango.cli.helpers.process_manager.time.sleep")
    @patch("dango.cli.helpers.process_manager.subprocess.Popen")
    @patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False)
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_successful_start(
        self, _mock_running, _mock_port, mock_popen, mock_sleep, _mock_start_time, tmp_path
    ):
        self._setup_project(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 6000
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pid = start_fastapi_server(tmp_path)

        assert pid == 6000
        pid_file = tmp_path / ".dango" / "web.pid"
        assert json.loads(pid_file.read_text()) == {"pid": 6000, "start_time": 99.0}

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
        # Old-format (bare-integer) PID file in this test → identity unknown (None) —
        # kill_process() falls back to existence-only checking, as documented.
        mock_kill.assert_called_once_with(4000, timeout=10, expected_start_time=None)

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

    @patch("psutil.Process")
    @patch("dango.cli.helpers.process_manager.kill_process", return_value=True)
    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_no_pid_dango_process_on_port(
        self, _mock_running, _mock_console, mock_sub_run, mock_kill, mock_psutil, tmp_path
    ):
        self._setup_project(tmp_path)

        # Mock psutil.Process to return CWD matching project root
        mock_proc = MagicMock()
        mock_proc.cwd.return_value = str(tmp_path)
        mock_psutil.return_value = mock_proc

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

    @patch("psutil.Process")
    @patch("dango.cli.helpers.process_manager.kill_process", return_value=False)
    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_dango_pids_found_but_all_kills_fail(
        self, _mock_running, _mock_console, mock_sub_run, mock_kill, mock_psutil, tmp_path
    ):
        self._setup_project(tmp_path)

        # Mock psutil.Process to return CWD matching project root
        mock_proc = MagicMock()
        mock_proc.cwd.return_value = str(tmp_path)
        mock_psutil.return_value = mock_proc

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

    @patch("psutil.Process")
    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_dango_process_from_another_project_not_killed(
        self, _mock_running, mock_console, mock_sub_run, mock_psutil, tmp_path
    ):
        """Port fallback finds a Dango process but CWD differs → skip, don't kill."""
        self._setup_project(tmp_path)

        # Mock psutil.Process so CWD points to a different project
        mock_proc = MagicMock()
        mock_proc.cwd.return_value = "/tmp/other-project"
        mock_psutil.return_value = mock_proc

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config

            lsof_result = MagicMock(returncode=0, stdout="1234\n")
            ps_result = MagicMock(
                returncode=0,
                stdout="python -m uvicorn dango.web.app:app --host 0.0.0.0 --port 8080",
            )
            mock_sub_run.side_effect = [lsof_result, ps_result]

            with patch("dango.cli.helpers.process_manager.kill_process") as mock_kill:
                result = stop_fastapi_server(tmp_path, verbose=True)
                assert result is False
                mock_kill.assert_not_called()

        # Should print a warning about skipping (different project)
        print_calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("different project" in str(c).lower() for c in print_calls)

    @patch("psutil.Process")
    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_dango_process_cwd_unreadable_skips_safely(
        self, _mock_running, mock_console, mock_sub_run, mock_psutil, tmp_path
    ):
        """Port fallback: Dango process found but CWD is unreadable → skip gracefully."""
        import psutil as psutil_mod

        self._setup_project(tmp_path)

        mock_proc = MagicMock()
        mock_proc.cwd.side_effect = psutil_mod.AccessDenied()
        mock_psutil.return_value = mock_proc

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

            with patch("dango.cli.helpers.process_manager.kill_process") as mock_kill:
                result = stop_fastapi_server(tmp_path, verbose=True)
                assert result is False
                mock_kill.assert_not_called()

        # Should print a warning about being unable to verify CWD
        print_calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("Could not verify CWD" in str(c) for c in print_calls)

    @patch("psutil.Process")
    @patch("dango.cli.helpers.process_manager.kill_process", return_value=True)
    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.cli.helpers.process_manager.is_process_running", return_value=False)
    def test_symlinked_project_root_matches_after_resolution(
        self, _mock_running, _mock_console, mock_sub_run, mock_kill, mock_psutil, tmp_path
    ):
        """Port fallback: symlinked project root matches resolved CWD → kills process."""
        self._setup_project(tmp_path)

        # Create a symlink pointing to tmp_path
        link_dir = tmp_path / "link-to-project"
        link_dir.symlink_to(tmp_path, target_is_directory=True)

        # Mock CWD returns the real path (not the symlink)
        mock_proc = MagicMock()
        mock_proc.cwd.return_value = str(tmp_path)
        mock_psutil.return_value = mock_proc

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

            result = stop_fastapi_server(link_dir, verbose=True)
            assert result is True
            mock_kill.assert_called_once_with(1234, timeout=5)


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


@pytest.mark.unit
class TestStopFastapiServerIdentityIntegration:
    """1.0.8-OPS-1 end-to-end coverage: real is_process_running()/kill_process()
    (only psutil itself is mocked, not our own identity-check functions) — proves
    the fix through the actual call chain stop_fastapi_server() exercises, not just
    through isolated unit mocks."""

    def _setup_project(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir(parents=True, exist_ok=True)
        return dango_dir

    def _mock_psutil_exceptions(self, mock_psutil):
        import psutil as real_psutil

        mock_psutil.NoSuchProcess = real_psutil.NoSuchProcess
        mock_psutil.AccessDenied = real_psutil.AccessDenied
        mock_psutil.ZombieProcess = real_psutil.ZombieProcess

    @patch("dango.cli.helpers.process_manager.subprocess.run")
    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.utils.process.psutil")
    def test_reused_pid_is_not_signaled(self, mock_psutil, _mock_console, mock_sub_run, tmp_path):
        """Mismatch case: PID file records a process that has since exited; the OS
        reused the PID number for something unrelated (different create_time()).
        stop_fastapi_server() must treat this as stale, not kill the new occupant."""
        self._mock_psutil_exceptions(mock_psutil)
        dango_dir = self._setup_project(tmp_path)
        # Recorded identity for the ORIGINAL tracked process
        (dango_dir / "web.pid").write_text(json.dumps({"pid": 4321, "start_time": 1000.0}))

        # A live process currently holds PID 4321, but it's not the one we tracked —
        # its create_time() doesn't match what was recorded (simulated PID reuse).
        mock_psutil.pid_exists.return_value = True
        mock_reused_proc = MagicMock()
        mock_reused_proc.create_time.return_value = 9_999_999.0
        mock_psutil.Process.return_value = mock_reused_proc

        with patch("dango.config.ConfigLoader") as mock_cl:
            mock_config = MagicMock()
            mock_config.platform.port = 8080
            mock_cl.return_value.load_config.return_value = mock_config
            mock_sub_run.return_value = MagicMock(returncode=1, stdout="")  # lsof: nothing on port

            result = stop_fastapi_server(tmp_path, verbose=False)

        assert result is False
        # The reused process must never be signaled.
        mock_reused_proc.terminate.assert_not_called()
        mock_reused_proc.kill.assert_not_called()
        # Stale PID file cleaned up, same as any other stale-PID case.
        assert not (dango_dir / "web.pid").exists()

    @patch("dango.cli.helpers.process_manager.console")
    @patch("dango.utils.process.psutil")
    def test_matching_pid_is_signaled(self, mock_psutil, _mock_console, tmp_path):
        """Match case (positive control): PID file's recorded start_time agrees with
        the live process's create_time() — the normal case of a project's own PID
        file, read shortly after its own `dango start`. Must still kill normally."""
        self._mock_psutil_exceptions(mock_psutil)
        dango_dir = self._setup_project(tmp_path)
        (dango_dir / "web.pid").write_text(json.dumps({"pid": 4321, "start_time": 1000.0}))

        mock_psutil.pid_exists.return_value = True
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 1000.0  # matches recorded start_time
        mock_proc.children.return_value = []
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.wait_procs.return_value = ([mock_proc], [])

        result = stop_fastapi_server(tmp_path, verbose=False)

        assert result is True
        mock_proc.terminate.assert_called_once()
        assert not (dango_dir / "web.pid").exists()
