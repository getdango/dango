"""tests/unit/test_remote_management_cli.py

Unit tests for remote management CLI commands: status, logs, ssh, query.

Uses Click's CliRunner with mocked SSH/status modules to avoid any real
network or filesystem access.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote
from dango.platform.cloud.server_status import ServerStatus, ServiceInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_REQUIRE_CTX = "dango.cli.utils.require_project_context"
_PATCH_LOADER = "dango.config.loader.ConfigLoader"
_PATCH_SSH = "dango.platform.cloud.ssh.SSHManager"
_PATCH_COLLECT = "dango.platform.cloud.server_status.collect_server_status"
_PATCH_PYPI = "dango.platform.cloud.server_status.check_latest_pypi_version"


def _make_cloud_config(
    droplet_ip="1.2.3.4",
    droplet_id=42,
    ssh_key_path=".dango/cloud_key",
    region="nyc1",
    size="s-2vcpu-4gb",
    domain=None,
):
    cfg = MagicMock()
    cfg.droplet_id = droplet_id
    cfg.droplet_ip = droplet_ip
    cfg.firewall_id = "fw-abc"
    cfg.ssh_key_path = ssh_key_path
    cfg.region = region
    cfg.size = size
    cfg.domain = domain
    return cfg


_UNSET = object()


def _make_loader(cloud_cfg=_UNSET):
    loader = MagicMock()
    loader.load_cloud_config.return_value = (
        _make_cloud_config() if cloud_cfg is _UNSET else cloud_cfg
    )
    return loader


def _make_command_result(stdout="", stderr="", exit_code=0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.exit_code = exit_code
    result.success = exit_code == 0
    return result


def _run(args, tmp_path, catch_exceptions=False):
    runner = CliRunner()
    return runner.invoke(
        remote,
        args,
        obj={"project_root": tmp_path},
        catch_exceptions=catch_exceptions,
    )


def _make_server_status(**kwargs):
    defaults = {
        "cpu_usage_pct": 15.2,
        "ram_total_mb": 4096,
        "ram_used_mb": 2048,
        "disk_total_mb": 50000,
        "disk_used_mb": 10000,
        "disk_available_mb": 38000,
        "services": [
            ServiceInfo(name="dango-web", status="active"),
            ServiceInfo(name="caddy", status="active"),
            ServiceInfo(name="metabase", status="running"),
        ],
        "duckdb_size_bytes": 5242880,
        "dango_version": "1.0.0",
        "last_deploy": "2024-01-15T10:00:00",
        "last_backup": "backup-2024-01-15.tar.gz",
        "last_sync_per_source": {"stripe": "2024-01-15T12:00:00"},
    }
    defaults.update(kwargs)
    return ServerStatus(**defaults)


# ---------------------------------------------------------------------------
# TestRemoteStatus
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteStatusCommand:
    def test_shows_server_info(self, tmp_path):
        """status command displays server info panel."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
            patch(_PATCH_COLLECT, return_value=_make_server_status()),
            patch(_PATCH_PYPI, return_value="1.0.0"),
        ):
            result = _run(["status"], tmp_path)

        assert result.exit_code == 0
        assert "1.2.3.4" in result.output
        assert "nyc1" in result.output

    def test_shows_services(self, tmp_path):
        """status command displays service status table."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
            patch(_PATCH_COLLECT, return_value=_make_server_status()),
            patch(_PATCH_PYPI, return_value="1.0.0"),
        ):
            result = _run(["status"], tmp_path)

        assert result.exit_code == 0
        assert "dango-web" in result.output

    def test_shows_update_available(self, tmp_path):
        """status command shows update notice when versions differ."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
            patch(_PATCH_COLLECT, return_value=_make_server_status(dango_version="0.9.0")),
            patch(_PATCH_PYPI, return_value="1.0.0"),
        ):
            result = _run(["status"], tmp_path)

        assert result.exit_code == 0
        assert "Update available" in result.output

    def test_no_deployment_exits_with_error(self, tmp_path):
        """Exits when no cloud deployment is configured."""
        mock_loader = _make_loader(cloud_cfg=None)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            result = _run(["status"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "No cloud deployment" in result.output

    def test_no_droplet_ip_exits_with_error(self, tmp_path):
        """Exits when droplet_ip is missing (no active deployment)."""
        cloud_cfg = _make_cloud_config(droplet_ip=None)
        mock_loader = _make_loader(cloud_cfg=cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            result = _run(["status"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "No cloud deployment" in result.output

    def test_ssh_connection_failure(self, tmp_path):
        """Exits when SSH connection fails."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.connect.side_effect = Exception("Connection refused")

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["status"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Failed to connect" in result.output


# ---------------------------------------------------------------------------
# TestRemoteLogs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteLogsCommand:
    def test_default_dango_logs(self, tmp_path):
        """logs command uses journalctl for dango-web by default."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result(
            stdout="Jan 15 10:00:00 server dango[123]: Starting..."
        )

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["logs"], tmp_path)

        assert result.exit_code == 0
        mock_ssh_instance.exec_command.assert_called_once()
        cmd = mock_ssh_instance.exec_command.call_args[0][0]
        assert "journalctl -u dango-web" in cmd
        assert "-n 50" in cmd

    def test_caddy_service_logs(self, tmp_path):
        """logs --service caddy uses journalctl for caddy."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result(stdout="caddy logs")

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["logs", "--service", "caddy"], tmp_path)

        assert result.exit_code == 0
        cmd = mock_ssh_instance.exec_command.call_args[0][0]
        assert "journalctl -u caddy" in cmd

    def test_metabase_service_logs(self, tmp_path):
        """logs --service metabase uses docker logs."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result(stdout="metabase logs")

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["logs", "--service", "metabase"], tmp_path)

        assert result.exit_code == 0
        cmd = mock_ssh_instance.exec_command.call_args[0][0]
        assert "docker logs metabase" in cmd

    def test_custom_tail_count(self, tmp_path):
        """logs --tail 20 passes through the tail count."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["logs", "--tail", "20"], tmp_path)

        assert result.exit_code == 0
        cmd = mock_ssh_instance.exec_command.call_args[0][0]
        assert "-n 20" in cmd

    def test_follow_mode_uses_stream(self, tmp_path):
        """logs -f calls _stream_ssh_command for streaming."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_channel = MagicMock()
        mock_channel.exit_status_ready.return_value = True
        mock_channel.recv_ready.return_value = False
        mock_channel.recv_stderr_ready.return_value = False
        mock_transport = MagicMock()
        mock_transport.open_session.return_value = mock_channel
        mock_ssh_instance.get_transport.return_value = mock_transport

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["logs", "-f"], tmp_path)

        assert result.exit_code == 0
        mock_transport.open_session.assert_called_once()
        mock_channel.exec_command.assert_called_once()
        cmd = mock_channel.exec_command.call_args[0][0]
        assert "-f" in cmd

    def test_no_deployment_exits(self, tmp_path):
        """Exits when no deployment configured."""
        mock_loader = _make_loader(cloud_cfg=None)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            result = _run(["logs"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "No cloud deployment" in result.output


# ---------------------------------------------------------------------------
# TestRemoteSSH
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteSSHCommand:
    def test_calls_execvp_with_correct_args(self, tmp_path):
        """ssh command calls os.execvp with correct SSH arguments."""
        mock_loader = _make_loader()
        key_path = tmp_path / ".dango" / "cloud_key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.touch()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch("dango.cli.commands.remote_mgmt.os.execvp") as mock_execvp,
        ):
            result = _run(["ssh"], tmp_path)

        assert result.exit_code == 0
        mock_execvp.assert_called_once()
        args = mock_execvp.call_args[0]
        assert args[0] == "ssh"
        assert "-i" in args[1]
        assert "root@1.2.3.4" in args[1]

    def test_missing_key_exits_with_error(self, tmp_path):
        """Exits when SSH key file does not exist."""
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            result = _run(["ssh"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "SSH key not found" in result.output

    def test_ssh_not_found(self, tmp_path):
        """Exits when ssh binary is not on PATH."""
        mock_loader = _make_loader()
        key_path = tmp_path / ".dango" / "cloud_key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.touch()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch("dango.cli.commands.remote_mgmt.os.execvp", side_effect=FileNotFoundError),
        ):
            result = _run(["ssh"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "SSH client not found" in result.output


# ---------------------------------------------------------------------------
# TestRemoteQuery
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteQueryCommand:
    def test_successful_query(self, tmp_path):
        """query command prints stdout on success."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result(
            stdout="┌──────────┐\n│ count(*) │\n│    42    │\n└──────────┘"
        )

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["query", "SELECT count(*) FROM information_schema.tables"], tmp_path)

        assert result.exit_code == 0
        assert "42" in result.output

    def test_query_uses_readonly(self, tmp_path):
        """query command passes read_only=True to DuckDB."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result(stdout="OK")

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            _run(["query", "SELECT 1"], tmp_path)

        cmd = mock_ssh_instance.exec_command.call_args[0][0]
        assert "read_only=True" in cmd

    def test_query_failure_shows_error(self, tmp_path):
        """query command prints stderr on failure."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result(
            stderr="Error: table not found", exit_code=1
        )

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            result = _run(["query", "SELECT * FROM nonexistent"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Error" in result.output

    def test_timeout_passthrough(self, tmp_path):
        """--timeout value is passed to exec_command."""
        mock_loader = _make_loader()
        mock_ssh_instance = MagicMock()
        mock_ssh_instance.exec_command.return_value = _make_command_result(stdout="OK")

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_SSH, return_value=mock_ssh_instance),
        ):
            _run(["query", "SELECT 1", "--timeout", "120"], tmp_path)

        kwargs = mock_ssh_instance.exec_command.call_args[1]
        assert kwargs["timeout"] == 120

    def test_no_deployment_exits(self, tmp_path):
        """Exits when no deployment configured."""
        mock_loader = _make_loader(cloud_cfg=None)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            result = _run(["query", "SELECT 1"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "No cloud deployment" in result.output
