"""tests/unit/test_server_status.py

Unit tests for dango/platform/cloud/server_status.py.

All SSH interactions are mocked — no real infrastructure needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud.server_status import (
    ServerStatus,
    ServiceInfo,
    check_latest_pypi_version,
    collect_server_status,
    get_local_resource_usage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(stdout="", stderr="", exit_code=0):
    """Create a mock CommandResult."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.exit_code = exit_code
    result.success = exit_code == 0
    return result


def _make_ssh(responses=None):
    """Create a mock SSHManager with optional command→response mapping."""
    ssh = MagicMock()
    if responses:

        def side_effect(cmd, **kwargs):
            for pattern, result in responses.items():
                if pattern in cmd:
                    return result
            return _make_result()

        ssh.exec_command.side_effect = side_effect
    else:
        ssh.exec_command.return_value = _make_result()
    return ssh


def _make_cloud_config():
    cfg = MagicMock()
    cfg.droplet_id = 42
    cfg.droplet_ip = "1.2.3.4"
    cfg.region = "nyc1"
    cfg.size = "s-2vcpu-4gb"
    return cfg


# ---------------------------------------------------------------------------
# TestCollectServerStatus
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollectServerStatus:
    def test_parses_cpu_usage(self):
        """CPU usage is parsed from top output."""
        responses = {
            "top": _make_result(
                stdout="%Cpu(s):  2.5 us,  1.0 sy,  0.0 ni, 95.5 id,  0.5 wa,  0.0 hi,  0.5 si,  0.0 st"
            ),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.cpu_usage_pct == 4.5

    def test_parses_ram(self):
        """RAM total and used are parsed from free -m."""
        responses = {
            "free": _make_result(
                stdout="              total        used        free      shared  buff/cache   available\n"
                "Mem:           3944        1200        1500         100        1244        2400\n"
                "Swap:          2048           0        2048\n"
            ),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.ram_total_mb == 3944
        assert status.ram_used_mb == 1200

    def test_parses_disk(self):
        """Disk metrics are parsed from df -BM."""
        responses = {
            "df": _make_result(
                stdout="Filesystem     1M-blocks  Used Available Use% Mounted on\n"
                "/dev/vda1          49150M 8200M    38500M  18% /srv/dango\n"
            ),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.disk_total_mb == 49150
        assert status.disk_used_mb == 8200
        assert status.disk_available_mb == 38500

    def test_parses_services(self):
        """Service statuses are collected for dango-web, caddy, metabase."""
        responses = {
            "systemctl is-active dango-web": _make_result(stdout="active"),
            "systemctl is-active caddy": _make_result(stdout="active"),
            "docker inspect": _make_result(stdout="running"),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert len(status.services) == 3
        assert status.services[0] == ServiceInfo(name="dango-web", status="active")
        assert status.services[1] == ServiceInfo(name="caddy", status="active")
        assert status.services[2] == ServiceInfo(name="metabase", status="running")

    def test_parses_duckdb_size(self):
        """DuckDB size in bytes is parsed from stat output."""
        responses = {
            "stat --format": _make_result(stdout="12345678"),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.duckdb_size_bytes == 12345678

    def test_parses_dango_version(self):
        """Installed dango version is parsed from python command."""
        responses = {
            "import dango": _make_result(stdout="1.0.0"),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.dango_version == "1.0.0"

    def test_parses_sync_history(self):
        """Sync history JSONL is parsed into per-source timestamps."""
        responses = {
            "sync_history": _make_result(
                stdout='{"source": "stripe", "timestamp": "2024-01-15T10:00:00"}\n'
                '{"source": "google_sheets", "timestamp": "2024-01-15T11:00:00"}\n'
                '{"source": "stripe", "timestamp": "2024-01-15T12:00:00"}\n'
            ),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.last_sync_per_source["stripe"] == "2024-01-15T12:00:00"
        assert status.last_sync_per_source["google_sheets"] == "2024-01-15T11:00:00"

    def test_parses_last_backup(self):
        """Last backup filename is returned from ls."""
        responses = {
            "ls -t": _make_result(stdout="backup-2024-01-15-120000.tar.gz"),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.last_backup == "backup-2024-01-15-120000.tar.gz"


# ---------------------------------------------------------------------------
# TestMissingData
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMissingData:
    def test_all_none_when_commands_fail(self):
        """All fields are None/empty when SSH commands return errors."""
        ssh = MagicMock()
        ssh.exec_command.return_value = _make_result(exit_code=1)
        status = collect_server_status(ssh, _make_cloud_config())

        assert status.cpu_usage_pct is None
        assert status.ram_total_mb is None
        assert status.ram_used_mb is None
        assert status.disk_total_mb is None
        assert status.disk_used_mb is None
        assert status.disk_available_mb is None
        assert status.duckdb_size_bytes is None
        assert status.dango_version is None
        assert status.last_deploy is None
        assert status.last_backup is None
        assert status.last_sync_per_source == {}

    def test_empty_stdout_returns_none(self):
        """Empty stdout with success=True returns None."""
        ssh = MagicMock()
        ssh.exec_command.return_value = _make_result(stdout="")
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.cpu_usage_pct is None

    def test_malformed_json_in_sync_history(self):
        """Malformed JSONL lines are silently skipped."""
        responses = {
            "sync_history": _make_result(stdout='not json\n{"source": "x", "timestamp": "t"}\n'),
        }
        ssh = _make_ssh(responses)
        status = collect_server_status(ssh, _make_cloud_config())
        assert status.last_sync_per_source == {"x": "t"}

    def test_services_not_found(self):
        """Services show not-found when commands fail."""
        ssh = MagicMock()
        fail_result = _make_result(stdout="", exit_code=1)
        ssh.exec_command.return_value = fail_result
        status = collect_server_status(ssh, _make_cloud_config())
        for svc in status.services:
            assert svc.status == "not-found"


# ---------------------------------------------------------------------------
# TestCheckLatestPypiVersion
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckLatestPypiVersion:
    def test_returns_version_on_success(self):
        """Returns version string from PyPI JSON response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"info": {"version": "1.2.3"}}

        with patch("httpx.get", return_value=mock_response):
            result = check_latest_pypi_version()

        assert result == "1.2.3"

    def test_returns_none_on_error(self):
        """Returns None on network error."""
        with patch("httpx.get", side_effect=Exception("network error")):
            result = check_latest_pypi_version()

        assert result is None

    def test_returns_none_on_non_200(self):
        """Returns None on non-200 status code."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("httpx.get", return_value=mock_response):
            result = check_latest_pypi_version()

        assert result is None


# ---------------------------------------------------------------------------
# TestGetLocalResourceUsage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetLocalResourceUsage:
    def test_returns_dict_with_expected_keys(self):
        """Returns dict with all expected keys (values may be None on macOS)."""
        result = get_local_resource_usage()
        assert "cpu_usage_pct" in result
        assert "ram_total_mb" in result
        assert "ram_used_mb" in result
        assert "disk_total_mb" in result
        assert "disk_used_mb" in result
        assert "disk_free_mb" in result

    def test_disk_none_on_missing_path(self):
        """Disk values are None when /srv/dango does not exist."""
        with patch("shutil.disk_usage", side_effect=OSError("No such file")):
            result = get_local_resource_usage()
        assert result["disk_total_mb"] is None
        assert result["disk_used_mb"] is None
        assert result["disk_free_mb"] is None


# ---------------------------------------------------------------------------
# TestServerStatusDataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServerStatusDataclass:
    def test_frozen_dataclass(self):
        """ServerStatus is frozen — fields cannot be modified."""
        status = ServerStatus()
        with pytest.raises(AttributeError):
            status.cpu_usage_pct = 50.0  # type: ignore[misc]

    def test_default_values(self):
        """All fields have sensible defaults."""
        status = ServerStatus()
        assert status.cpu_usage_pct is None
        assert status.services == []
        assert status.last_sync_per_source == {}
