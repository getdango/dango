"""tests/unit/test_upgrade.py

Unit tests for dango/platform/cloud/upgrade.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError
from dango.platform.cloud.ssh import CommandResult

# Patch paths — lazy imports mean we patch at the SOURCE module.
_PATCH_PYPI = "dango.platform.cloud.server_status.check_latest_pypi_version"
_PATCH_BACKUP = "dango.platform.cloud.backup.create_backup"


def _make_ssh_mock(
    *,
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> MagicMock:
    """Return a mock SSHManager with configurable exec_command results."""
    results = exec_results or {}

    def _exec_side_effect(command: str, timeout: int | None = None) -> CommandResult:
        for substr, (stdout, stderr, exit_code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec_side_effect
    ssh.write_remote_file = MagicMock()
    ssh.connect = MagicMock()
    ssh.disconnect = MagicMock()
    return ssh


@pytest.mark.unit
class TestValidateVersionString:
    """Tests for validate_version_string()."""

    def test_valid_versions(self) -> None:
        from dango.platform.cloud.upgrade import validate_version_string

        validate_version_string("1.0.0")
        validate_version_string("0.1.1")
        validate_version_string("99.88.77")
        # PEP 440 pre-release versions
        validate_version_string("1.0.0b4")
        validate_version_string("1.0.0rc1")
        validate_version_string("1.0.0a1")
        # PEP 440 shorthand (e.g. "1.0" normalizes to "1.0")
        validate_version_string("1.0")
        validate_version_string("v1.0.0")

    def test_invalid_versions(self) -> None:
        from dango.platform.cloud.upgrade import validate_version_string

        with pytest.raises(CloudError, match="Invalid version string"):
            validate_version_string("not-a-version")

        with pytest.raises(CloudError, match="Invalid version string"):
            validate_version_string("abc")

    def test_injection_attempts(self) -> None:
        from dango.platform.cloud.upgrade import validate_version_string

        with pytest.raises(CloudError):
            validate_version_string("1.0.0; rm -rf /")

        with pytest.raises(CloudError):
            validate_version_string("1.0.0 && echo pwned")

        with pytest.raises(CloudError):
            validate_version_string("$(whoami)")


@pytest.mark.unit
class TestCheckVersions:
    """Tests for check_versions()."""

    def test_returns_both_versions(self) -> None:
        from dango.platform.cloud.upgrade import check_versions

        ssh = _make_ssh_mock(exec_results={"import dango": ("1.0.0", "", 0)})
        with patch(_PATCH_PYPI, return_value="1.1.0"):
            current, latest = check_versions(ssh)

        assert current == "1.0.0"
        assert latest == "1.1.0"

    def test_handles_missing_version(self) -> None:
        from dango.platform.cloud.upgrade import check_versions

        ssh = _make_ssh_mock(exec_results={"import dango": ("", "", 1)})
        with patch(_PATCH_PYPI, return_value=None):
            current, latest = check_versions(ssh)

        assert current is None
        assert latest is None


@pytest.mark.unit
class TestUpgradeDango:
    """Tests for upgrade_dango()."""

    def test_upgrade_to_latest(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _make_ssh_mock(
            exec_results={
                "import dango": ("1.0.0", "", 0),
                "pip install": ("", "", 0),
                "migrations run": ("No pending migrations", "", 0),
                "docker compose": ("", "", 0),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
            }
        )

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        with (
            patch(_PATCH_PYPI, return_value="1.1.0"),
            patch(_PATCH_BACKUP, return_value=backup_result),
        ):
            result = upgrade_dango(ssh)

        assert result.old_version == "1.0.0"
        assert result.backup_path is not None
        assert result.health_check_passed is True

    def test_upgrade_to_specific_version(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _make_ssh_mock(
            exec_results={
                "import dango": ("1.0.0", "", 0),
                "pip install getdango==1.2.0": ("", "", 0),
                "migrations run": ("", "", 0),
                "docker compose": ("", "", 0),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
            }
        )

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        with patch(_PATCH_BACKUP, return_value=backup_result):
            result = upgrade_dango(ssh, version="1.2.0")

        assert result.old_version == "1.0.0"
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("getdango==1.2.0" in cmd for cmd in cmds)

    def test_already_at_target_version(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _make_ssh_mock(exec_results={"import dango": ("1.1.0", "", 0)})

        with patch(_PATCH_PYPI, return_value="1.1.0"):
            result = upgrade_dango(ssh)

        assert result.old_version == "1.1.0"
        assert result.new_version == "1.1.0"
        assert result.backup_path is None
        assert any("Already at target" in w for w in result.warnings)

    def test_skip_backup_flag(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _make_ssh_mock(
            exec_results={
                "import dango": ("1.0.0", "", 0),
                "pip install": ("", "", 0),
                "migrations run": ("", "", 0),
                "docker compose": ("", "", 0),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
            }
        )

        with patch(_PATCH_PYPI, return_value="1.1.0"):
            result = upgrade_dango(ssh, skip_backup=True)

        assert result.backup_path is None
        assert any("backup skipped" in w for w in result.warnings)

    def test_health_check_failure_suggests_rollback(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _make_ssh_mock(
            exec_results={
                "import dango": ("1.0.0", "", 0),
                "pip install": ("", "", 0),
                "migrations run": ("", "", 0),
                "docker compose": ("", "", 0),
                "curl -sf": ("", "", 1),  # Health check fails
                "systemctl": ("", "", 0),
            }
        )

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        with (
            patch(_PATCH_PYPI, return_value="1.1.0"),
            patch(_PATCH_BACKUP, return_value=backup_result),
            patch("dango.platform.cloud.backup.time.sleep"),
            patch(
                "dango.platform.cloud.upgrade.time.monotonic",
                side_effect=[
                    0.0,  # start_time
                    100.0,  # final duration
                ],
            ),
            patch(
                "dango.platform.cloud.backup.time.monotonic",
                side_effect=[
                    0.0,
                    1.0,  # verify_health loop iterations
                    100.0,
                    100.0,  # timeout exceeded
                ],
            ),
        ):
            result = upgrade_dango(ssh)

        assert result.health_check_passed is False
        assert any("rollback" in w.lower() for w in result.warnings)

    def test_pypi_unavailable_raises(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _make_ssh_mock(exec_results={"import dango": ("1.0.0", "", 0)})

        with (
            patch(_PATCH_PYPI, return_value=None),
            pytest.raises(CloudError, match="Could not determine latest"),
        ):
            upgrade_dango(ssh)

    def test_progress_callback_called(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _make_ssh_mock(
            exec_results={
                "import dango": ("1.0.0", "", 0),
                "pip install": ("", "", 0),
                "migrations run": ("", "", 0),
                "docker compose": ("", "", 0),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
            }
        )
        progress_calls: list[tuple[str, str]] = []

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        with (
            patch(_PATCH_PYPI, return_value="1.1.0"),
            patch(_PATCH_BACKUP, return_value=backup_result),
        ):
            upgrade_dango(
                ssh,
                on_progress=lambda step, status: progress_calls.append((step, status)),
            )

        steps = [s for s, _ in progress_calls]
        assert "check_version" in steps
        assert "pip_install" in steps
        assert "verify_health" in steps
