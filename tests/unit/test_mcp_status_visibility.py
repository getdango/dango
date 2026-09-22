"""tests/unit/test_mcp_status_visibility.py

1.0.8-Q4: `dango status` should show a running MCP server process for the
current project. MCP processes are spawned entirely by the LLM client
(Claude Code, Cursor, Windsurf), not by `dango start`, and are never written
to any PID file Dango tracks -- unlike the web server and file watcher.
`_find_mcp_server_process()` in `dango/cli/commands/platform.py` scans all
running processes for a `dango mcp run` server matching this project via its
DANGO_PROJECT_ROOT environment variable (set by `dango mcp setup` since
1.0.8-OPS-4).

Mirrors the test shape of `kill_orphan_watchers()` in
tests/unit/test_watcher_lifecycle.py: psutil is imported lazily inside the
function under test, so patches target the global `psutil.process_iter`
attribute, not a `dango.*`-qualified path.

This is read-only detection -- these tests never assert or exercise any
start/stop/signal behavior against the process found.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest
from click.testing import CliRunner

from dango.cli.commands.platform import _find_mcp_server_process, status


def _make_proc(pid: int, cmdline: list[str], environ: dict | None = None) -> MagicMock:
    """Create a mock process for psutil.process_iter, matching the shape
    used by test_watcher_lifecycle.py's _make_proc()."""
    proc = MagicMock()
    proc.pid = pid
    proc.info = {"pid": pid, "cmdline": cmdline}
    if environ is not None:
        proc.environ.return_value = environ
    return proc


@pytest.mark.unit
class TestFindMcpServerProcess:
    def test_find_mcp_server_process_detects_matching_project(self, tmp_path: Path) -> None:
        resolved = str(tmp_path.resolve())
        proc = _make_proc(
            12345,
            ["/venv/bin/dango", "mcp", "run"],
            environ={"DANGO_PROJECT_ROOT": resolved},
        )
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result == 12345

    def test_find_mcp_server_process_ignores_other_projects(self, tmp_path: Path) -> None:
        other_root = "/some/other/project"
        proc = _make_proc(
            12345,
            ["/venv/bin/dango", "mcp", "run"],
            environ={"DANGO_PROJECT_ROOT": other_root},
        )
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result is None

    def test_find_mcp_server_process_no_processes_running(self, tmp_path: Path) -> None:
        with patch("psutil.process_iter", return_value=[]):
            result = _find_mcp_server_process(tmp_path)
        assert result is None

    def test_find_mcp_server_process_ignores_process_missing_env_var(self, tmp_path: Path) -> None:
        """A real `dango mcp run` process that predates 1.0.8-OPS-4 (or was
        configured by hand) has no DANGO_PROJECT_ROOT in its environment at
        all -- not just a different value. Per spec: don't guess, don't
        count it as this project's."""
        proc = _make_proc(
            12345,
            ["/venv/bin/dango", "mcp", "run"],
            environ={"SOME_OTHER_VAR": "unrelated"},
        )
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result is None

    def test_find_mcp_server_process_detects_shebang_rewritten_argv(self, tmp_path: Path) -> None:
        """Live-verified regression: a `dango` console script has a
        `#!/path/to/python` shebang, so the kernel rewrites argv to
        [<python-interpreter>, <path>/dango, "mcp", "run"] -- "dango" is NOT
        argv[0]. Confirmed via `ps aux` against a real spawned process."""
        resolved = str(tmp_path.resolve())
        proc = _make_proc(
            12345,
            [
                "/opt/homebrew/Cellar/python@3.11/.../Python",
                "/Users/x/venv/bin/dango",
                "mcp",
                "run",
            ],
            environ={"DANGO_PROJECT_ROOT": resolved},
        )
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result == 12345

    def test_find_mcp_server_process_ignores_unrelated_processes(self, tmp_path: Path) -> None:
        """A process whose cmdline doesn't look like `dango mcp run` at all
        (e.g. some other program) must never even reach the environ() check."""
        resolved = str(tmp_path.resolve())
        proc = _make_proc(
            12345,
            ["/usr/bin/some-other-tool", "--flag"],
            environ={"DANGO_PROJECT_ROOT": resolved},
        )
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result is None
        proc.environ.assert_not_called()

    def test_find_mcp_server_process_handles_access_denied(self, tmp_path: Path) -> None:
        """psutil.AccessDenied while reading environ() must not crash
        `dango status` -- skip that PID and keep checking others."""
        resolved = str(tmp_path.resolve())
        denied_proc = _make_proc(111, ["/venv/bin/dango", "mcp", "run"])
        denied_proc.environ.side_effect = psutil.AccessDenied(pid=111)
        matching_proc = _make_proc(
            222,
            ["/venv/bin/dango", "mcp", "run"],
            environ={"DANGO_PROJECT_ROOT": resolved},
        )
        with patch("psutil.process_iter", return_value=[denied_proc, matching_proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result == 222

    def test_find_mcp_server_process_handles_no_such_process(self, tmp_path: Path) -> None:
        proc = _make_proc(111, ["/venv/bin/dango", "mcp", "run"])
        proc.environ.side_effect = psutil.NoSuchProcess(pid=111)
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result is None

    def test_find_mcp_server_process_handles_zombie_process(self, tmp_path: Path) -> None:
        proc = _make_proc(111, ["/venv/bin/dango", "mcp", "run"])
        proc.environ.side_effect = psutil.ZombieProcess(pid=111)
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result is None

    def test_find_mcp_server_process_empty_cmdline_skipped(self, tmp_path: Path) -> None:
        proc = _make_proc(111, [])
        with patch("psutil.process_iter", return_value=[proc]):
            result = _find_mcp_server_process(tmp_path)
        assert result is None
        proc.environ.assert_not_called()

    def test_find_mcp_server_process_never_kills_or_signals(self, tmp_path: Path) -> None:
        """Read-only detection: the function must never call terminate/kill/
        send_signal on anything it finds."""
        resolved = str(tmp_path.resolve())
        proc = _make_proc(
            12345,
            ["/venv/bin/dango", "mcp", "run"],
            environ={"DANGO_PROJECT_ROOT": resolved},
        )
        with patch("psutil.process_iter", return_value=[proc]):
            _find_mcp_server_process(tmp_path)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        proc.send_signal.assert_not_called()


def _make_config_loader() -> MagicMock:
    mock_config = MagicMock()
    mock_config.project.name = "test-project"
    mock_config.project.organization = None
    mock_config.platform.metabase_port = 3000
    mock_config.platform.auto_sync = False

    mock_loader = MagicMock()
    mock_loader.return_value.load_config.return_value = mock_config
    return mock_loader


def _make_fastapi_status(tmp_path: Path, running: bool = True) -> dict:
    return {
        "running": running,
        "pid": 999 if running else None,
        "port": 8800,
        "url": "http://localhost:8800",
        "log_file": tmp_path / ".dango" / "web.log",
    }


@pytest.mark.unit
class TestStatusMcpServerRow:
    """`status()` must show an 'MCP server' row, distinct from the existing
    Web UI/Watcher/Metabase rows, using the dim/neutral 'Not running' style
    rather than the red/Stopped style used for actionable problems."""

    def _invoke(self, tmp_path: Path, mcp_pid: int | None):
        mock_loader = _make_config_loader()

        mock_svc_status = MagicMock()
        mock_svc_status.value = "running"
        mock_docker_manager = MagicMock()
        mock_docker_manager.return_value.get_service_status.return_value = {
            "metabase": mock_svc_status
        }

        mock_net_config = MagicMock()
        mock_net_config.return_value.get_project_info.return_value = None
        mock_net_config.return_value.list_projects.return_value = {}

        mock_nginx_manager = MagicMock()
        mock_nginx_manager.return_value.is_running.return_value = False

        runner = CliRunner()
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.DockerManager", mock_docker_manager),
            patch("dango.platform.network.NetworkConfig", mock_net_config),
            patch("dango.platform.network.NginxManager", mock_nginx_manager),
            patch(
                "dango.cli.helpers.process_manager.get_fastapi_status",
                return_value=_make_fastapi_status(tmp_path, running=True),
            ),
            patch(
                "dango.platform.watcher_lifecycle.get_watcher_status",
                return_value={"running": True, "pid": 888},
            ),
            patch("dango.config.helpers.is_running_on_cloud", return_value=False),
            patch(
                "dango.cli.commands.upgrade.get_latest_version_cached",
                return_value=None,
            ),
            patch(
                "dango.cli.commands.platform._find_mcp_server_process",
                return_value=mcp_pid,
            ),
        ):
            return runner.invoke(status, obj={"project_root": str(tmp_path)})

    def test_status_shows_mcp_server_running(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, mcp_pid=54321)

        assert "MCP server" in result.output
        assert "Running" in result.output
        assert "54321" in result.output

    def test_status_shows_mcp_server_not_running(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, mcp_pid=None)

        assert "MCP server" in result.output
        assert "Not running" in result.output
        assert "Stopped" not in result.output
