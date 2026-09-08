"""tests/unit/test_cli_status_metabase_port.py

1.0.8-Q1: `dango status` used to hardcode the literal string
"Metabase (port 3000)" in its table row label, even when the project is
configured for a different Metabase port. Regression test for the fix
that reads the real configured port off `config.platform.metabase_port`.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.platform import status


def _make_config_loader(metabase_port: int) -> MagicMock:
    """Create a mock ConfigLoader returning a config with the given
    Metabase port and otherwise-sane defaults for `status()`."""
    mock_config = MagicMock()
    mock_config.project.name = "test-project"
    mock_config.project.organization = None
    mock_config.platform.metabase_port = metabase_port
    mock_config.platform.auto_sync = False

    mock_loader = MagicMock()
    mock_loader.return_value.load_config.return_value = mock_config
    return mock_loader


def _make_fastapi_status(tmp_path: Path) -> dict:
    return {
        "running": False,
        "pid": None,
        "port": 8800,
        "url": "http://localhost:8800",
        "log_file": tmp_path / ".dango" / "web.log",  # doesn't exist
    }


@pytest.mark.unit
class TestStatusMetabasePort:
    """`status()` must show the project's configured Metabase port, not a
    hardcoded 3000."""

    def _invoke(self, tmp_path: Path, metabase_port: int, docker_running: bool = True):
        mock_loader = _make_config_loader(metabase_port)

        mock_svc_status = MagicMock()
        mock_svc_status.value = "running" if docker_running else "stopped"
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
                return_value=_make_fastapi_status(tmp_path),
            ),
            patch(
                "dango.platform.watcher_lifecycle.get_watcher_status",
                return_value={"running": False},
            ),
            patch("dango.config.helpers.is_running_on_cloud", return_value=False),
            patch(
                "dango.cli.commands.upgrade.get_latest_version_cached",
                return_value=None,
            ),
        ):
            return runner.invoke(status, obj={"project_root": str(tmp_path)})

    def test_status_shows_configured_metabase_port(self, tmp_path: Path) -> None:
        """A non-default configured Metabase port (3001) is shown, and the
        old hardcoded 3000 is not."""
        result = self._invoke(tmp_path, metabase_port=3001)

        assert "Metabase (port 3001)" in result.output
        assert "Metabase (port 3000)" not in result.output

    def test_status_shows_default_metabase_port(self, tmp_path: Path) -> None:
        """The default port (3000) still renders correctly when configured
        as such — this is not a case of "any number works", it's the real
        configured value happening to be 3000."""
        result = self._invoke(tmp_path, metabase_port=3000)

        assert "Metabase (port 3000)" in result.output

    def test_status_shows_configured_port_when_stopped(self, tmp_path: Path) -> None:
        """The non-default port also renders correctly in the 'Stopped' row."""
        result = self._invoke(tmp_path, metabase_port=3001, docker_running=False)

        assert "Metabase (port 3001)" in result.output
        assert "Metabase (port 3000)" not in result.output
