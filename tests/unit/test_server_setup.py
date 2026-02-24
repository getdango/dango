"""tests/unit/test_server_setup.py

Unit tests for SSH-based server setup orchestration
(dango/platform/cloud/server_setup.py).

Mocks SSHManager.exec_command() and write_remote_file() to verify each
setup step calls the correct commands and handles idempotency.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dango.exceptions import CloudProvisioningError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ssh_mock(
    *,
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> MagicMock:
    """Return a mock SSHManager with configurable exec_command results.

    Args:
        exec_results: Map of command substring → (stdout, stderr, exit_code).
            If a command matches multiple substrings, the first match wins.
            Commands not matched default to success ("", "", 0).
    """
    from dango.platform.cloud.ssh import CommandResult

    results = exec_results or {}

    def _exec_side_effect(command, timeout=None):
        for substr, (stdout, stderr, exit_code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec_side_effect
    ssh.write_remote_file = MagicMock()
    return ssh


# ---------------------------------------------------------------------------
# 1. SetupResult
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetupResult:
    def test_defaults(self):
        """SetupResult initialises with empty lists."""
        from dango.platform.cloud.server_setup import SetupResult

        result = SetupResult()
        assert result.steps_completed == []
        assert result.steps_skipped == []
        assert result.warnings == []


# ---------------------------------------------------------------------------
# 2. Full orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetupServer:
    def test_all_steps_complete(self):
        """setup_server() runs all steps when nothing is pre-installed."""
        from dango.platform.cloud.server_setup import setup_server

        # Docker and Caddy not installed, user doesn't exist
        ssh = _make_ssh_mock(
            exec_results={
                "id -u dango": ("", "no such user", 1),
                "docker --version": ("", "", 1),
                "caddy version": ("", "", 1),
            }
        )

        result = setup_server(ssh)

        assert "apt_packages" in result.steps_completed
        assert "create_user" in result.steps_completed
        assert "install_docker" in result.steps_completed
        assert "install_caddy" in result.steps_completed
        assert "systemd_unit" in result.steps_completed
        assert "caddyfile" in result.steps_completed
        assert len(result.steps_skipped) == 0

    def test_idempotent_skips(self):
        """Docker, Caddy, and user are skipped when already present."""
        from dango.platform.cloud.server_setup import setup_server

        ssh = _make_ssh_mock(
            exec_results={
                "id -u dango": ("1001", "", 0),
                "docker --version": ("Docker version 24.0", "", 0),
                "caddy version": ("v2.7.5", "", 0),
            }
        )

        result = setup_server(ssh)

        assert "create_user" in result.steps_skipped
        assert "install_docker" in result.steps_skipped
        assert "install_caddy" in result.steps_skipped
        # Steps that always run should still be completed
        assert "apt_packages" in result.steps_completed
        assert "directories" in result.steps_completed

    def test_progress_callback(self):
        """on_progress is called with (step, status) for each step."""
        from dango.platform.cloud.server_setup import setup_server

        ssh = _make_ssh_mock(
            exec_results={
                "id -u dango": ("1001", "", 0),
                "docker --version": ("Docker version 24.0", "", 0),
                "caddy version": ("v2.7.5", "", 0),
            }
        )
        progress_calls: list[tuple[str, str]] = []

        def on_progress(step: str, status: str) -> None:
            progress_calls.append((step, status))

        setup_server(ssh, on_progress=on_progress)

        # Every step should have at least "running" then "done" or "skipped"
        step_names = [c[0] for c in progress_calls]
        assert "apt_packages" in step_names
        assert "create_user" in step_names
        # Skipped steps should show running then skipped
        assert ("create_user", "running") in progress_calls
        assert ("create_user", "skipped") in progress_calls

    def test_failure_raises_provisioning_error(self):
        """A failing step raises CloudProvisioningError."""
        from dango.platform.cloud.server_setup import setup_server

        ssh = _make_ssh_mock(
            exec_results={
                "apt-get update": ("", "apt lock held", 100),
            }
        )

        with pytest.raises(CloudProvisioningError, match="apt_packages"):
            setup_server(ssh)


# ---------------------------------------------------------------------------
# 3. Individual step tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetupSteps:
    def test_apt_packages_runs_install(self):
        """apt step runs update + install."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_apt_packages

        ssh = _make_ssh_mock()
        result = SetupResult()
        _setup_apt_packages(ssh, result, None)

        cmd = ssh.exec_command.call_args_list[0][0][0]
        assert "apt-get update" in cmd
        assert "apt-get install" in cmd
        assert "fail2ban" in cmd
        assert "apt_packages" in result.steps_completed

    def test_dango_user_created(self):
        """User step creates dango user when not present."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_dango_user

        ssh = _make_ssh_mock(exec_results={"id -u dango": ("", "no such user", 1)})
        result = SetupResult()
        _setup_dango_user(ssh, result, None)

        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("useradd" in cmd for cmd in cmds)
        assert "create_user" in result.steps_completed

    def test_dango_user_skipped(self):
        """User step skipped when dango user exists."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_dango_user

        ssh = _make_ssh_mock(exec_results={"id -u dango": ("1001", "", 0)})
        result = SetupResult()
        _setup_dango_user(ssh, result, None)

        assert "create_user" in result.steps_skipped
        assert "create_user" not in result.steps_completed

    def test_docker_skipped_when_installed(self):
        """Docker install skipped when docker is already available."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_docker

        ssh = _make_ssh_mock(exec_results={"docker --version": ("Docker version 24.0", "", 0)})
        result = SetupResult()
        _setup_docker(ssh, result, None)

        assert "install_docker" in result.steps_skipped

    def test_caddy_skipped_when_installed(self):
        """Caddy install skipped when caddy is already available."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_caddy

        ssh = _make_ssh_mock(exec_results={"caddy version": ("v2.7.5", "", 0)})
        result = SetupResult()
        _setup_caddy(ssh, result, None)

        assert "install_caddy" in result.steps_skipped

    def test_directories_creates_structure(self):
        """Directory step creates /srv/dango structure."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_directories

        ssh = _make_ssh_mock()
        result = SetupResult()
        _setup_directories(ssh, result, None)

        cmd = ssh.exec_command.call_args_list[0][0][0]
        assert "/srv/dango/project/.dango" in cmd
        assert "/srv/dango/project/data" in cmd
        assert "chown -R dango:dango" in cmd
        assert "directories" in result.steps_completed

    def test_venv_installs_getdango(self):
        """Venv step creates venv and installs getdango."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        ssh = _make_ssh_mock()
        result = SetupResult()
        _setup_venv(ssh, result, None)

        cmd = ssh.exec_command.call_args_list[0][0][0]
        assert "python3 -m venv" in cmd
        assert "pip install getdango" in cmd

    def test_ssh_hardening_disables_password(self):
        """SSH hardening step disables password auth."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_ssh_hardening

        ssh = _make_ssh_mock()
        result = SetupResult()
        _setup_ssh_hardening(ssh, result, None)

        cmd = ssh.exec_command.call_args_list[0][0][0]
        assert "PasswordAuthentication no" in cmd
        assert "systemctl reload sshd" in cmd

    def test_systemd_unit_content(self):
        """Systemd unit file contains correct paths and settings."""
        from dango.platform.cloud._server_templates import SYSTEMD_UNIT
        from dango.platform.cloud.server_setup import SetupResult, _setup_systemd_unit

        ssh = _make_ssh_mock()
        result = SetupResult()
        _setup_systemd_unit(ssh, result, None)

        assert "WorkingDirectory=/srv/dango/project" in SYSTEMD_UNIT
        assert "DLT_DATA_DIR=/srv/dango/project/.dlt" in SYSTEMD_UNIT
        assert "/srv/dango/venv/bin/dango serve" in SYSTEMD_UNIT
        assert "User=dango" in SYSTEMD_UNIT

        # Verify daemon-reload + enable (not start)
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        enable_cmd = [c for c in cmds if "systemctl" in c and "enable" in c]
        assert len(enable_cmd) > 0
        # Should NOT start the service
        start_cmds = [c for c in cmds if "systemctl start dango-web" in c]
        assert len(start_cmds) == 0

    def test_caddyfile_content(self):
        """Caddyfile has :80 reverse proxy to localhost:8800."""
        from dango.platform.cloud._server_templates import CADDYFILE

        assert ":80" in CADDYFILE
        assert "reverse_proxy localhost:8800" in CADDYFILE

    def test_docker_daemon_config(self):
        """Docker daemon config has log rotation."""
        from dango.platform.cloud._server_templates import DOCKER_DAEMON_JSON

        assert "json-file" in DOCKER_DAEMON_JSON
        assert "10m" in DOCKER_DAEMON_JSON

    def test_fail2ban_config(self):
        """Fail2ban config has sshd jail."""
        from dango.platform.cloud._server_templates import FAIL2BAN_JAIL

        assert "sshd" in FAIL2BAN_JAIL
        assert "maxretry = 5" in FAIL2BAN_JAIL


# ---------------------------------------------------------------------------
# 4. Helper functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHelpers:
    def test_run_checked_success(self):
        """_run_checked returns stdout on success."""
        from dango.platform.cloud.server_setup import _run_checked

        ssh = _make_ssh_mock(exec_results={"echo": ("hello\n", "", 0)})
        result = _run_checked(ssh, "echo hello", step="test_step")
        assert result == "hello\n"

    def test_run_checked_failure(self):
        """_run_checked raises CloudProvisioningError on non-zero exit."""
        from dango.platform.cloud.server_setup import _run_checked

        ssh = _make_ssh_mock(exec_results={"bad": ("", "error msg", 1)})
        with pytest.raises(CloudProvisioningError, match="test_step"):
            _run_checked(ssh, "bad command", step="test_step")

    def test_write_remote_config(self):
        """_write_remote_config creates parent dir and writes file."""
        from dango.platform.cloud.server_setup import _write_remote_config

        ssh = _make_ssh_mock()
        _write_remote_config(
            ssh,
            "/etc/caddy/Caddyfile",
            "content",
            step="test",
            mode=0o644,
        )

        # Should have called mkdir -p for parent
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("/etc/caddy" in cmd for cmd in cmds)
        # Should have called write_remote_file
        ssh.write_remote_file.assert_called_once_with("/etc/caddy/Caddyfile", "content", mode=0o644)
