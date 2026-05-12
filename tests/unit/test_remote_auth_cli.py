"""tests/unit/test_remote_auth_cli.py

Unit tests for ``dango remote auth`` CLI commands.

Uses Click's CliRunner with mocked SSH modules to avoid any real
network or filesystem access.  Follows the same pattern as
``test_remote_management_cli.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote

# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------

_PATCH_LOAD_CFG = "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip"
_PATCH_MAKE_SSH = "dango.cli.commands.remote_mgmt._make_ssh_manager"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cloud_config(droplet_ip: str = "1.2.3.4") -> MagicMock:
    cfg = MagicMock()
    cfg.droplet_ip = droplet_ip
    cfg.ssh_key_path = ".dango/cloud_key"
    return cfg


def _make_command_result(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.exit_code = exit_code
    result.success = exit_code == 0
    return result


def _run(args: list[str], tmp_path, catch_exceptions: bool = False):
    runner = CliRunner()
    return runner.invoke(
        remote,
        args,
        obj={"project_root": tmp_path},
        catch_exceptions=catch_exceptions,
    )


def _patch_ssh_success(
    tmp_path,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
):
    """Return a context manager stack that patches config + SSH with a successful connection."""
    cloud_cfg = _make_cloud_config()
    ssh = MagicMock()
    ssh.exec_command.return_value = _make_command_result(stdout, stderr, exit_code)

    return (
        cloud_cfg,
        ssh,
        patch(_PATCH_LOAD_CFG, return_value=(cloud_cfg, tmp_path)),
        patch(_PATCH_MAKE_SSH, return_value=ssh),
    )


# ---------------------------------------------------------------------------
# TestRemoteAuthAddUser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteAuthAddUser:
    def test_add_user_default_role(self, tmp_path):
        """add-user uses viewer role by default."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User created")
        with p1, p2:
            result = _run(["auth", "add-user", "test@co.com"], tmp_path)

        assert result.exit_code == 0
        cmd = ssh.exec_command.call_args[0][0]
        assert "add-user" in cmd
        assert "--role viewer" in cmd
        assert "--password" in cmd

    def test_add_user_custom_role(self, tmp_path):
        """add-user passes --role editor when specified."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User created")
        with p1, p2:
            result = _run(["auth", "add-user", "test@co.com", "--role", "editor"], tmp_path)

        assert result.exit_code == 0
        cmd = ssh.exec_command.call_args[0][0]
        assert "--role editor" in cmd

    def test_add_user_email_quoting(self, tmp_path):
        """add-user shell-quotes the email argument."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User created")
        with p1, p2:
            result = _run(["auth", "add-user", "user+tag@co.com"], tmp_path)

        assert result.exit_code == 0
        cmd = ssh.exec_command.call_args[0][0]
        # shlex.quote wraps in single quotes for special chars
        assert "user+tag@co.com" in cmd

    def test_add_user_output_displayed(self, tmp_path):
        """add-user displays remote stdout."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(
            tmp_path, stdout="User created: test@co.com\nTemp password: abc123"
        )
        with p1, p2:
            result = _run(["auth", "add-user", "test@co.com"], tmp_path)

        assert result.exit_code == 0
        assert "User created" in result.output

    def test_add_user_failure(self, tmp_path):
        """add-user shows error on failure."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stderr="User already exists", exit_code=1)
        with p1, p2:
            result = _run(["auth", "add-user", "test@co.com"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Error" in result.output

    def test_add_user_disconnects_on_success(self, tmp_path):
        """SSH is disconnected after successful add-user."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User created")
        with p1, p2:
            _run(["auth", "add-user", "test@co.com"], tmp_path)

        ssh.disconnect.assert_called_once()

    def test_add_user_disconnects_on_failure(self, tmp_path):
        """SSH is disconnected even when add-user fails."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stderr="fail", exit_code=1)
        with p1, p2:
            _run(["auth", "add-user", "test@co.com"], tmp_path, catch_exceptions=True)

        ssh.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# TestRemoteAuthListUsers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteAuthListUsers:
    def test_list_users(self, tmp_path):
        """list-users displays remote output."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(
            tmp_path, stdout="admin@co.com  admin  active\nuser@co.com  viewer  active"
        )
        with p1, p2:
            result = _run(["auth", "list-users"], tmp_path)

        assert result.exit_code == 0
        assert "admin@co.com" in result.output
        cmd = ssh.exec_command.call_args[0][0]
        assert "list-users" in cmd

    def test_list_users_empty(self, tmp_path):
        """list-users handles empty output."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="")
        with p1, p2:
            result = _run(["auth", "list-users"], tmp_path)

        assert result.exit_code == 0

    def test_list_users_failure(self, tmp_path):
        """list-users shows error on failure."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stderr="database locked", exit_code=1)
        with p1, p2:
            result = _run(["auth", "list-users"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Error" in result.output

    def test_list_users_disconnects(self, tmp_path):
        """SSH is disconnected after list-users."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="users")
        with p1, p2:
            _run(["auth", "list-users"], tmp_path)

        ssh.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# TestRemoteAuthRemoveUser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteAuthRemoveUser:
    def test_remove_user(self, tmp_path):
        """remove-user calls delete-user on remote."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User deleted")
        with p1, p2:
            result = _run(["auth", "remove-user", "test@co.com"], tmp_path)

        assert result.exit_code == 0
        cmd = ssh.exec_command.call_args[0][0]
        assert "delete-user" in cmd

    def test_remove_user_pipes_yes(self, tmp_path):
        """remove-user pipes yes to bypass interactive confirm."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User deleted")
        with p1, p2:
            _run(["auth", "remove-user", "test@co.com"], tmp_path)

        cmd = ssh.exec_command.call_args[0][0]
        assert "yes |" in cmd

    def test_remove_user_email_quoting(self, tmp_path):
        """remove-user shell-quotes the email."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User deleted")
        with p1, p2:
            _run(["auth", "remove-user", "user+tag@co.com"], tmp_path)

        cmd = ssh.exec_command.call_args[0][0]
        assert "user+tag@co.com" in cmd

    def test_remove_user_failure(self, tmp_path):
        """remove-user shows error on failure."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stderr="User not found", exit_code=1)
        with p1, p2:
            result = _run(["auth", "remove-user", "test@co.com"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Error" in result.output

    def test_remove_user_disconnects(self, tmp_path):
        """SSH is disconnected after remove-user."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="User deleted")
        with p1, p2:
            _run(["auth", "remove-user", "test@co.com"], tmp_path)

        ssh.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# TestRemoteAuthResetPassword
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteAuthResetPassword:
    def test_reset_password(self, tmp_path):
        """reset-password runs reset-password on remote."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(
            tmp_path, stdout="Password reset for test@co.com\nNew password: xyz789"
        )
        with p1, p2:
            result = _run(["auth", "reset-password", "test@co.com"], tmp_path)

        assert result.exit_code == 0
        cmd = ssh.exec_command.call_args[0][0]
        assert "reset-password" in cmd

    def test_reset_password_output_displayed(self, tmp_path):
        """reset-password displays remote stdout."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(
            tmp_path, stdout="Password reset\nNew password: secret123"
        )
        with p1, p2:
            result = _run(["auth", "reset-password", "test@co.com"], tmp_path)

        assert result.exit_code == 0
        assert "Password reset" in result.output

    def test_reset_password_failure(self, tmp_path):
        """reset-password shows error on failure."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stderr="User not found", exit_code=1)
        with p1, p2:
            result = _run(
                ["auth", "reset-password", "test@co.com"],
                tmp_path,
                catch_exceptions=True,
            )

        assert result.exit_code != 0
        assert "Error" in result.output

    def test_reset_password_email_quoting(self, tmp_path):
        """reset-password shell-quotes the email."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="Password reset")
        with p1, p2:
            _run(["auth", "reset-password", "user+tag@co.com"], tmp_path)

        cmd = ssh.exec_command.call_args[0][0]
        assert "user+tag@co.com" in cmd

    def test_reset_password_disconnects(self, tmp_path):
        """SSH is disconnected after reset-password."""
        _cfg, ssh, p1, p2 = _patch_ssh_success(tmp_path, stdout="done")
        with p1, p2:
            _run(["auth", "reset-password", "test@co.com"], tmp_path)

        ssh.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# TestRemoteAuthConnectionFailure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteAuthConnectionFailure:
    def test_add_user_connection_failure(self, tmp_path):
        """add-user exits when SSH connection fails."""
        cloud_cfg = _make_cloud_config()
        ssh = MagicMock()
        ssh.connect.side_effect = Exception("Connection refused")

        with (
            patch(_PATCH_LOAD_CFG, return_value=(cloud_cfg, tmp_path)),
            patch(_PATCH_MAKE_SSH, return_value=ssh),
        ):
            result = _run(["auth", "add-user", "test@co.com"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Cannot connect" in result.output

    def test_list_users_connection_failure(self, tmp_path):
        """list-users exits when SSH connection fails."""
        cloud_cfg = _make_cloud_config()
        ssh = MagicMock()
        ssh.connect.side_effect = Exception("Timeout")

        with (
            patch(_PATCH_LOAD_CFG, return_value=(cloud_cfg, tmp_path)),
            patch(_PATCH_MAKE_SSH, return_value=ssh),
        ):
            result = _run(["auth", "list-users"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Cannot connect" in result.output

    def test_remove_user_connection_failure(self, tmp_path):
        """remove-user exits when SSH connection fails."""
        cloud_cfg = _make_cloud_config()
        ssh = MagicMock()
        ssh.connect.side_effect = Exception("Host unreachable")

        with (
            patch(_PATCH_LOAD_CFG, return_value=(cloud_cfg, tmp_path)),
            patch(_PATCH_MAKE_SSH, return_value=ssh),
        ):
            result = _run(["auth", "remove-user", "test@co.com"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "Cannot connect" in result.output

    def test_reset_password_connection_failure(self, tmp_path):
        """reset-password exits when SSH connection fails."""
        cloud_cfg = _make_cloud_config()
        ssh = MagicMock()
        ssh.connect.side_effect = Exception("Auth failed")

        with (
            patch(_PATCH_LOAD_CFG, return_value=(cloud_cfg, tmp_path)),
            patch(_PATCH_MAKE_SSH, return_value=ssh),
        ):
            result = _run(
                ["auth", "reset-password", "test@co.com"],
                tmp_path,
                catch_exceptions=True,
            )

        assert result.exit_code != 0
        assert "Cannot connect" in result.output


# ---------------------------------------------------------------------------
# TestRemoteAuthHelp
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteAuthHelp:
    def test_auth_group_help(self):
        """auth group --help lists subcommands."""
        runner = CliRunner()
        result = runner.invoke(remote, ["auth", "--help"])

        assert result.exit_code == 0
        assert "add-user" in result.output
        assert "list-users" in result.output
        assert "remove-user" in result.output
        assert "reset-password" in result.output

    def test_add_user_help(self):
        """add-user --help shows usage."""
        runner = CliRunner()
        result = runner.invoke(remote, ["auth", "add-user", "--help"])

        assert result.exit_code == 0
        assert "EMAIL" in result.output
        assert "--role" in result.output
