"""tests/unit/test_deployer.py

Unit tests for push deployment (dango/platform/cloud/deployer.py).

Uses mocked SSHManager, file_sync, and backup to avoid real network
or filesystem access.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.deployer import (
    DEPLOY_LOCK_PATH,
    DeployLock,
    DeployResult,
    _acquire_lock,
    _check_existing_lock,
    _get_deployer_identity,
    _is_lock_expired,
    _release_lock,
    _run_remote_dbt,
    _validate_remote_sources,
    push_deploy,
)
from dango.platform.cloud.ssh import CommandResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ssh_mock() -> MagicMock:
    """Return a mock SSHManager with sensible defaults."""
    ssh = MagicMock()
    ssh.key_path = Path("/tmp/test_key")
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
    return ssh


def _make_sync_result(**kwargs: object) -> MagicMock:
    """Return a mock SyncResult with defaults."""
    from dango.platform.cloud.file_sync import SyncResult

    defaults = {
        "synced_files": ["dbt/models/", ".dango/sources.yml"],
        "changed_models": ["stg_orders"],
        "added_models": [],
        "removed_models": [],
        "packages_changed": False,
        "is_first_deploy": False,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return SyncResult(**defaults)


def _make_backup_result() -> MagicMock:
    """Return a mock BackupResult."""
    result = MagicMock()
    result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"
    result.manifest_path = "/srv/dango/backups/deploy/backup-test.json"
    result.duration_seconds = 5.0
    result.warnings = []
    return result


def _make_lock_json(*, expired: bool = False) -> str:
    """Return JSON for a deploy lock."""
    now = datetime.now(tz=timezone.utc)
    if expired:
        started = now - timedelta(hours=1)
        expires = now - timedelta(minutes=30)
    else:
        started = now - timedelta(minutes=5)
        expires = now + timedelta(minutes=25)
    return json.dumps(
        {
            "deployer": "testuser@testhost",
            "started_at": started.isoformat(),
            "expires_at": expires.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# DeployLock / DeployResult tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDataclasses:
    """Test DeployLock and DeployResult."""

    def test_deploy_lock_fields(self):
        lock = DeployLock(
            deployer="me@host", started_at="2026-01-01T00:00:00", expires_at="2026-01-01T00:30:00"
        )
        assert lock.deployer == "me@host"
        assert lock.started_at == "2026-01-01T00:00:00"
        assert lock.expires_at == "2026-01-01T00:30:00"

    def test_deploy_result_defaults(self):
        from dango.platform.cloud.file_sync import SyncResult

        result = DeployResult(sync_result=SyncResult())
        assert result.backup_result is None
        assert result.dbt_deps_run is False
        assert result.dbt_compile_success is False
        assert result.models_rebuilt == []
        assert result.duration_seconds == 0.0
        assert result.warnings == []
        assert result.dry_run is False


# ---------------------------------------------------------------------------
# Lock management tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLockManagement:
    """Test deploy lock acquisition and release."""

    def test_check_no_existing_lock(self):
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=1)
        assert _check_existing_lock(ssh) is None

    def test_check_existing_lock(self):
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(
            stdout=_make_lock_json(), stderr="", exit_code=0
        )
        lock = _check_existing_lock(ssh)
        assert lock is not None
        assert lock.deployer == "testuser@testhost"

    def test_check_corrupt_lock(self):
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="not json", stderr="", exit_code=0)
        assert _check_existing_lock(ssh) is None

    def test_is_lock_expired_true(self):
        past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        lock = DeployLock(deployer="x", started_at="", expires_at=past)
        assert _is_lock_expired(lock) is True

    def test_is_lock_expired_false(self):
        future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
        lock = DeployLock(deployer="x", started_at="", expires_at=future)
        assert _is_lock_expired(lock) is False

    def test_is_lock_expired_empty_expiry(self):
        lock = DeployLock(deployer="x", started_at="", expires_at="")
        assert _is_lock_expired(lock) is True

    def test_acquire_lock_no_existing(self):
        ssh = _make_ssh_mock()
        # No existing lock
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        lock = _acquire_lock(ssh)
        assert lock.deployer != ""

    def test_acquire_lock_active_lock_raises(self):
        ssh = _make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if f"cat {DEPLOY_LOCK_PATH}" in cmd:
                return CommandResult(stdout=_make_lock_json(expired=False), stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        with pytest.raises(CloudProvisioningError, match="Deploy lock held by"):
            _acquire_lock(ssh)

    def test_acquire_lock_stale_lock_succeeds(self):
        ssh = _make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if f"cat {DEPLOY_LOCK_PATH}" in cmd:
                return CommandResult(stdout=_make_lock_json(expired=True), stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect
        lock = _acquire_lock(ssh)
        assert lock.deployer != ""

    def test_acquire_lock_force_overrides(self):
        ssh = _make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if f"cat {DEPLOY_LOCK_PATH}" in cmd:
                return CommandResult(stdout=_make_lock_json(expired=False), stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect
        lock = _acquire_lock(ssh, force=True)
        assert lock.deployer != ""

    def test_release_lock(self):
        ssh = _make_ssh_mock()
        _release_lock(ssh)
        ssh.exec_command.assert_called_once_with(f"rm -f {DEPLOY_LOCK_PATH}")


# ---------------------------------------------------------------------------
# Source validation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceValidation:
    """Test _validate_remote_sources."""

    def test_valid_sources(self):
        ssh = _make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "sources.yml" in cmd:
                return CommandResult(
                    stdout="sources:\n  - name: google_sheets\n  - name: facebook_ads\n",
                    stderr="",
                    exit_code=0,
                )
            if "secrets.toml" in cmd:
                return CommandResult(
                    stdout='[sources.google_sheets]\nkey = "abc"\n[sources.facebook_ads]\ntoken = "xyz"\n',
                    stderr="",
                    exit_code=0,
                )
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect
        errors = _validate_remote_sources(ssh)
        assert errors == []

    def test_missing_credentials(self):
        ssh = _make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "sources.yml" in cmd:
                return CommandResult(
                    stdout="sources:\n  - name: google_sheets\n",
                    stderr="",
                    exit_code=0,
                )
            if "secrets.toml" in cmd:
                return CommandResult(stdout="# empty", stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect
        errors = _validate_remote_sources(ssh)
        assert len(errors) == 1
        assert "google_sheets" in errors[0]

    def test_no_sources_configured(self):
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=1)
        errors = _validate_remote_sources(ssh)
        assert errors == []


# ---------------------------------------------------------------------------
# Remote dbt command tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunRemoteDbt:
    """Test _run_remote_dbt."""

    def test_basic_command(self):
        ssh = _make_ssh_mock()
        _run_remote_dbt(ssh, "compile")
        cmd = ssh.exec_command.call_args[0][0]
        assert "sudo -u dango" in cmd
        assert "dbt compile" in cmd
        assert "--project-dir" in cmd
        assert "--profiles-dir" in cmd

    def test_extra_args(self):
        ssh = _make_ssh_mock()
        _run_remote_dbt(ssh, "run", "--select stg_orders stg_users")
        cmd = ssh.exec_command.call_args[0][0]
        assert "--select stg_orders stg_users" in cmd


# ---------------------------------------------------------------------------
# Deployer identity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeployerIdentity:
    """Test _get_deployer_identity."""

    def test_returns_user_at_host(self):
        identity = _get_deployer_identity()
        assert "@" in identity


# ---------------------------------------------------------------------------
# push_deploy tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPushDeploy:
    """Test push_deploy orchestration."""

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_dry_run(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(dry_run=True)
        ssh = _make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1", dry_run=True)

        assert result.dry_run is True
        assert result.backup_result is None
        mock_backup.assert_not_called()
        mock_sync.assert_called_once()
        # Verify sync was called with dry_run=True
        assert mock_sync.call_args[1]["dry_run"] is True

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_full_deploy_workflow(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(
            changed_models=["stg_orders"],
            packages_changed=True,
        )
        mock_backup.return_value = _make_backup_result()
        ssh = _make_ssh_mock()

        # Track command order
        commands: list[str] = []

        def _tracking_exec(cmd: str, **kwargs: object) -> CommandResult:
            commands.append(cmd)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _tracking_exec

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        assert result.dry_run is False
        assert result.backup_result is not None
        assert result.dbt_deps_run is True
        assert result.dbt_compile_success is True
        assert "stg_orders" in result.models_rebuilt

        # Verify correct step order
        lock_acquired = any(DEPLOY_LOCK_PATH in c and "cat >" in c for c in commands)
        web_stopped = any("systemctl stop dango-web" in c for c in commands)
        ownership_fixed = any("chown -R dango:dango" in c for c in commands)
        web_started = any("systemctl start dango-web" in c for c in commands)
        lock_released = any(f"rm -f {DEPLOY_LOCK_PATH}" in c for c in commands)

        assert lock_acquired
        assert web_stopped
        assert ownership_fixed
        assert web_started
        assert lock_released

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_dbt_compile_failure_aborts(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = _make_ssh_mock()

        call_count = 0

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            nonlocal call_count
            call_count += 1
            if "dbt compile" in cmd:
                return CommandResult(stdout="", stderr="Compilation Error", exit_code=1)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        with pytest.raises(CloudProvisioningError, match="dbt compile failed"):
            push_deploy(ssh, tmp_path, "10.0.0.1")

        # Verify the lock was released (rm -f deploy.lock called)
        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert any(f"rm -f {DEPLOY_LOCK_PATH}" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_no_models_changed_skips_dbt_run(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(
            changed_models=[],
            added_models=[],
        )
        mock_backup.return_value = _make_backup_result()
        ssh = _make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        assert result.models_rebuilt == []
        # No dbt run command should have been issued
        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert not any("dbt run" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_packages_not_changed_skips_dbt_deps(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(packages_changed=False)
        mock_backup.return_value = _make_backup_result()
        ssh = _make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        assert result.dbt_deps_run is False
        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert not any("dbt deps" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_lock_released_on_error(self, mock_sync, mock_backup, tmp_path):
        mock_backup.side_effect = CloudProvisioningError("backup failed")
        ssh = _make_ssh_mock()

        with pytest.raises(CloudProvisioningError, match="backup failed"):
            push_deploy(ssh, tmp_path, "10.0.0.1")

        # Lock should still be released
        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert any(f"rm -f {DEPLOY_LOCK_PATH}" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_progress_callback(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = _make_ssh_mock()

        steps: list[tuple[str, str]] = []
        push_deploy(ssh, tmp_path, "10.0.0.1", on_progress=lambda s, st: steps.append((s, st)))

        step_names = [s for s, _ in steps]
        assert "acquire_lock" in step_names
        assert "create_backup" in step_names
        assert "stop_web" in step_names
        assert "sync_files" in step_names
        assert "fix_ownership" in step_names
        assert "dbt_compile" in step_names

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_source_validation_warnings(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = _make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "sources.yml" in cmd and "cat" in cmd:
                return CommandResult(
                    stdout="sources:\n  - name: missing_source\n",
                    stderr="",
                    exit_code=0,
                )
            if "secrets.toml" in cmd and "cat" in cmd:
                return CommandResult(stdout="# empty", stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        result = push_deploy(ssh, tmp_path, "10.0.0.1")
        assert any("missing_source" in w for w in result.warnings)
