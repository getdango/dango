"""tests/unit/test_deployer.py

Unit tests for deployer helpers: lock management, source validation,
model name validation, dbt commands, and dataclasses.

push_deploy() orchestration tests are in test_deployer_push.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

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
    _validate_model_names,
    _validate_remote_sources,
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

    def test_is_lock_expired_handles_utc_suffix(self):
        """Python 3.10 compat: +00:00 suffix is stripped before parsing."""
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        # Explicitly include the +00:00 suffix
        lock = DeployLock(deployer="x", started_at="", expires_at=future.isoformat())
        assert _is_lock_expired(lock) is False

    def test_is_lock_expired_handles_z_suffix(self):
        """Python 3.10 compat: Z suffix is stripped before parsing."""
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        ts = future.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        lock = DeployLock(deployer="x", started_at="", expires_at=ts)
        assert _is_lock_expired(lock) is False

    def test_acquire_lock_no_existing(self):
        ssh = _make_ssh_mock()
        # No existing lock
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        lock = _acquire_lock(ssh)
        assert lock.deployer != ""

    def test_acquire_lock_uses_noclobber(self):
        """Lock creation uses atomic noclobber (set -C)."""
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        _acquire_lock(ssh)
        # Find the lock creation command
        lock_cmds = [
            args[0][0] for args in ssh.exec_command.call_args_list if "set -C" in args[0][0]
        ]
        assert len(lock_cmds) == 1
        assert DEPLOY_LOCK_PATH in lock_cmds[0]

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

    def test_acquire_lock_concurrent_race_fails(self):
        """If noclobber fails (another deployer grabbed it), raise error."""
        ssh = _make_ssh_mock()
        call_count = 0

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            nonlocal call_count
            call_count += 1
            # No existing lock on read
            if f"cat {DEPLOY_LOCK_PATH}" in cmd:
                return CommandResult(stdout="", stderr="", exit_code=1)
            # Noclobber write fails (another deployer beat us)
            if "set -C" in cmd:
                return CommandResult(stdout="", stderr="cannot overwrite", exit_code=1)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        with pytest.raises(CloudProvisioningError, match="another deployment"):
            _acquire_lock(ssh)

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
# Model name validation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelNameValidation:
    """Test _validate_model_names shell safety check."""

    def test_valid_names_pass(self):
        _validate_model_names(["stg_orders", "stg_users", "dim_customer_v2"])

    def test_empty_list_passes(self):
        _validate_model_names([])

    def test_invalid_name_with_semicolon_raises(self):
        with pytest.raises(CloudProvisioningError, match="Invalid model name"):
            _validate_model_names(["stg_orders; rm -rf /"])

    def test_invalid_name_with_spaces_raises(self):
        with pytest.raises(CloudProvisioningError, match="Invalid model name"):
            _validate_model_names(["stg orders"])

    def test_invalid_name_with_slash_raises(self):
        with pytest.raises(CloudProvisioningError, match="Invalid model name"):
            _validate_model_names(["staging/stg_orders"])


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
