"""tests/unit/test_cli_start_guardrails.py

Unit tests for `dango start` guard rails (cloned project info, cloud warning).
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.platform import start
from dango.utils.process import PidRecord

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _make_cloud_config(droplet_ip: str = "1.2.3.4", domain: str | None = None) -> MagicMock:
    """Create a mock CloudConfig."""
    cfg = MagicMock()
    cfg.droplet_ip = droplet_ip
    cfg.domain = domain
    return cfg


def _make_config_loader(cloud_config: MagicMock | None = None) -> MagicMock:
    """Create a mock ConfigLoader that returns the given cloud config."""
    mock_loader = MagicMock()
    mock_loader.return_value.load_config.return_value = MagicMock()
    mock_loader.return_value.load_config.return_value.project.name = "test-project"
    mock_loader.return_value.load_config.return_value.project.organization = None
    mock_loader.return_value.load_config.return_value.platform.port = 8800
    mock_loader.return_value.load_config.return_value.platform.metabase_port = 3000
    mock_loader.return_value.load_config.return_value.platform.dbt_docs_port = 8081
    mock_loader.return_value.load_config.return_value.platform.auto_sync = False
    mock_loader.return_value.load_cloud_config.return_value = cloud_config
    return mock_loader


def _mock_port_free():
    """Return a mock socket whose connect_ex always returns non-zero (port free)."""
    mock_sock = MagicMock()
    mock_sock.connect_ex.return_value = 1  # non-zero = port free
    return mock_sock


def _mock_sockets(*connect_ex_codes):
    """List of mock sockets for successive socket.socket() calls.

    Each code is the value connect_ex() returns for that call (0 = port in use,
    non-zero = free) — used as `side_effect` for `patch("socket.socket", ...)` so the
    initial port check and (if reached) the post-kill recheck can return different
    results.
    """
    sockets = []
    for code in connect_ex_codes:
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = code
        sockets.append(mock_sock)
    return sockets


def _mock_lsof_and_ps(pid: int, cmd_line: str) -> list[MagicMock]:
    """subprocess.run side_effect for one `lsof -ti :<port>` + one `ps -p <pid>` call."""
    return [
        MagicMock(returncode=0, stdout=f"{pid}\n"),
        MagicMock(returncode=0, stdout=cmd_line),
    ]


_UVICORN_CMD = "python -m uvicorn dango.web.app:app --host 0.0.0.0 --port 8800"


@pytest.mark.unit
class TestStartGuardRails:
    """Tests for start command guard rails."""

    def test_cloned_project_message_shown(self, tmp_path: Path) -> None:
        """Cloned project message shown when sources.yml exists but no dango.db/warehouse."""
        # Set up cloned project state
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text("sources: []")
        # No dango.db, no data/warehouse.duckdb

        mock_loader = _make_config_loader()
        runner = CliRunner()
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
            patch("socket.socket", return_value=_mock_port_free()),
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        ):
            # Abort after cloud check by injecting an error in version check
            mock_loader.return_value.load_cloud_config.return_value = None
            # Let it fail on the next step (version alignment already patched)
            # We just need to see the cloned project message in output
            result = runner.invoke(start, ["--yes"], obj={"project_root": str(tmp_path)})

        plain = _ANSI_RE.sub("", result.output)
        assert "cloned project" in plain.lower()
        assert "dango sync" in plain

    def test_cloned_project_message_not_shown_when_warehouse_exists(self, tmp_path: Path) -> None:
        """Cloned project message NOT shown when warehouse.duckdb exists."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text("sources: []")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "warehouse.duckdb").touch()

        mock_loader = _make_config_loader()
        runner = CliRunner()
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
            patch("socket.socket", return_value=_mock_port_free()),
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        ):
            mock_loader.return_value.load_cloud_config.return_value = None
            result = runner.invoke(start, ["--yes"], obj={"project_root": str(tmp_path)})

        plain = _ANSI_RE.sub("", result.output)
        assert "cloned project" not in plain.lower()

    def test_cloud_info_not_shown(self, tmp_path: Path) -> None:
        """Cloud info note removed — no mention of cloud deployment on start (D1)."""
        cloud_cfg = _make_cloud_config(droplet_ip="1.2.3.4")
        mock_loader = _make_config_loader(cloud_config=cloud_cfg)
        runner = CliRunner()
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
            patch("socket.socket", return_value=_mock_port_free()),
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        ):
            result = runner.invoke(start, [], obj={"project_root": str(tmp_path)})

        plain = _ANSI_RE.sub("", result.output)
        assert "deployed to 1.2.3.4" not in plain


@pytest.mark.unit
class TestStartPortConflictProcessIdentity:
    """1.0.8-AI: `start`'s port-conflict auto-recovery must only kill THIS project's
    own previously-recorded server (verified via read_pid_record_for_project() +
    is_process_running()'s expected_start_time param), never a Dango process it can't
    confirm as its own — see BUGS-FOUND.md's "dango start's port-conflict
    auto-recovery kills ANY Dango process on the target port" entry.
    """

    def test_start_kills_own_stale_process(self, tmp_path: Path) -> None:
        """A PID on the port matching this project's own recorded PidRecord (and
        still alive with a matching start time) is killed, with expected_start_time
        passed through to kill_process()."""
        mock_loader = _make_config_loader()
        mock_loader.return_value.load_cloud_config.return_value = None
        my_record = PidRecord(pid=1234, start_time=100.0)
        runner = CliRunner()

        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
            # my_record is also what the earlier stale-PID-file-detection block sees
            # (it reads the same PidRecord via read_pid_file()) — force os.kill() to
            # report the PID as not found there so that block's real-OS liveness probe
            # doesn't interfere with this test, which is only about the port-conflict
            # block further down reusing the same record.
            patch("os.kill", side_effect=ProcessLookupError),
            # First call: port in use. Second call (post-kill recheck): still in use,
            # so the command aborts right after the kill logic we're asserting on,
            # without needing to mock the rest of the start() pipeline.
            patch("socket.socket", side_effect=_mock_sockets(0, 0)),
            patch(
                "subprocess.run",
                side_effect=_mock_lsof_and_ps(1234, _UVICORN_CMD),
            ),
            patch(
                "dango.cli.helpers.process_manager.read_pid_record_for_project",
                return_value=my_record,
            ),
            patch("dango.utils.process.is_process_running", return_value=True),
            patch("dango.utils.process.kill_process", return_value=True) as mock_kill,
        ):
            result = runner.invoke(start, [], obj={"project_root": str(tmp_path)})

        mock_kill.assert_called_once_with(1234, timeout=5, expected_start_time=100.0)
        plain = _ANSI_RE.sub("", result.output)
        assert "Stopped Dango process 1234" in plain

    def test_start_refuses_to_kill_foreign_dango_process(self, tmp_path: Path) -> None:
        """A Dango process on the port that doesn't match this project's own recorded
        PID (here: no record at all, e.g. first-ever start) is refused, not killed,
        and click.Abort() is raised."""
        mock_loader = _make_config_loader()
        mock_loader.return_value.load_cloud_config.return_value = None
        runner = CliRunner()

        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
            patch("socket.socket", side_effect=_mock_sockets(0)),
            patch(
                "subprocess.run",
                side_effect=_mock_lsof_and_ps(5678, _UVICORN_CMD),
            ),
            patch(
                "dango.cli.helpers.process_manager.read_pid_record_for_project",
                return_value=None,
            ),
            patch("dango.utils.process.is_process_running") as mock_is_running,
            patch("dango.utils.process.kill_process") as mock_kill,
        ):
            result = runner.invoke(start, [], obj={"project_root": str(tmp_path)})

        mock_kill.assert_not_called()
        mock_is_running.assert_not_called()  # no record to check identity against
        assert result.exit_code != 0
        plain = _ANSI_RE.sub("", result.output)
        assert "different project" in plain.lower()
        assert "PID 5678" in plain

    def test_start_refuses_to_kill_when_own_pid_reused(self, tmp_path: Path) -> None:
        """The PID on the port matches this project's own recorded PidRecord's PID
        number, but is_process_running() reports the start time no longer matches
        (PID reuse) — treated as foreign, not killed."""
        mock_loader = _make_config_loader()
        mock_loader.return_value.load_cloud_config.return_value = None
        my_record = PidRecord(pid=1234, start_time=100.0)
        runner = CliRunner()

        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
            # See test_start_kills_own_stale_process for why this is needed.
            patch("os.kill", side_effect=ProcessLookupError),
            patch("socket.socket", side_effect=_mock_sockets(0)),
            patch(
                "subprocess.run",
                side_effect=_mock_lsof_and_ps(1234, _UVICORN_CMD),
            ),
            patch(
                "dango.cli.helpers.process_manager.read_pid_record_for_project",
                return_value=my_record,
            ),
            # PID number matches, but identity (start time) doesn't — PID was reused.
            patch("dango.utils.process.is_process_running", return_value=False),
            patch("dango.utils.process.kill_process") as mock_kill,
        ):
            result = runner.invoke(start, [], obj={"project_root": str(tmp_path)})

        mock_kill.assert_not_called()
        assert result.exit_code != 0
        plain = _ANSI_RE.sub("", result.output)
        assert "different project" in plain.lower()
        assert "PID 1234" in plain
