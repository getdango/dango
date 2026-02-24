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

# All 16 step names, in the order they must execute.
_ALL_STEP_NAMES = [
    "apt_packages",
    "create_user",
    "install_docker",
    "docker_group",
    "install_caddy",
    "directories",
    "python_venv",
    "ssh_hardening",
    "ssh_key_copy",
    "docker_daemon",
    "journald",
    "logrotate",
    "systemd_unit",
    "caddyfile",
    "fail2ban",
    "unattended_upgrades",
]


def _make_ssh_mock(
    *,
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> MagicMock:
    """Return a mock SSHManager with configurable exec_command results.

    Args:
        exec_results: Map of command substring -> (stdout, stderr, exit_code).
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
        """setup_server() runs all 16 steps when nothing is pre-installed."""
        from dango.platform.cloud.server_setup import setup_server

        # Docker/Caddy not installed, user doesn't exist, venv doesn't exist
        ssh = _make_ssh_mock(
            exec_results={
                "id -u dango": ("", "no such user", 1),
                "docker --version": ("", "", 1),
                "caddy version": ("", "", 1),
                "test -x /srv/dango/venv/bin/dango": ("", "", 1),
                # cat for config files returns not-found (triggers write)
                "cat /etc/docker/daemon.json": ("", "No such file", 1),
                "cat /etc/systemd/journald.conf.d/dango.conf": ("", "", 1),
                "cat /etc/logrotate.d/dango": ("", "", 1),
                "cat /etc/systemd/system/dango-web.service": ("", "", 1),
                "cat /etc/caddy/Caddyfile": ("", "", 1),
                "cat /etc/fail2ban/jail.local": ("", "", 1),
                "cat /etc/apt/apt.conf.d/51unattended-upgrades-dango": ("", "", 1),
            }
        )

        result = setup_server(ssh)

        # L1: Verify ALL 16 steps are accounted for
        total = len(result.steps_completed) + len(result.steps_skipped)
        assert total == 16, (
            f"Expected 16 steps, got {total}: "
            f"completed={result.steps_completed}, skipped={result.steps_skipped}"
        )
        assert len(result.steps_skipped) == 0
        for name in _ALL_STEP_NAMES:
            assert name in result.steps_completed, f"Step '{name}' missing from completed"

    def test_idempotent_skips(self):
        """Docker, Caddy, user, and venv are skipped when already present."""
        from dango.platform.cloud.server_setup import setup_server

        ssh = _make_ssh_mock(
            exec_results={
                "id -u dango": ("1001", "", 0),
                "docker --version": ("Docker version 24.0", "", 0),
                "caddy version": ("v2.7.5", "", 0),
                "test -x /srv/dango/venv/bin/dango": ("", "", 0),
                # Config files return "not found" so they still get written
                "cat /etc/docker/daemon.json": ("", "", 1),
                "cat /etc/systemd/journald.conf.d/dango.conf": ("", "", 1),
                "cat /etc/logrotate.d/dango": ("", "", 1),
                "cat /etc/systemd/system/dango-web.service": ("", "", 1),
                "cat /etc/caddy/Caddyfile": ("", "", 1),
                "cat /etc/fail2ban/jail.local": ("", "", 1),
                "cat /etc/apt/apt.conf.d/51unattended-upgrades-dango": ("", "", 1),
            }
        )

        result = setup_server(ssh)

        assert "create_user" in result.steps_skipped
        assert "install_docker" in result.steps_skipped
        assert "install_caddy" in result.steps_skipped
        assert "python_venv" in result.steps_skipped
        # Steps that always run should still be completed
        assert "apt_packages" in result.steps_completed
        assert "directories" in result.steps_completed
        # L1: total must still be 16
        total = len(result.steps_completed) + len(result.steps_skipped)
        assert total == 16

    def test_step_ordering(self):
        """Steps execute in the correct order (L3)."""
        from dango.platform.cloud.server_setup import setup_server

        ssh = _make_ssh_mock(
            exec_results={
                "id -u dango": ("", "", 1),
                "docker --version": ("", "", 1),
                "caddy version": ("", "", 1),
                "test -x /srv/dango/venv/bin/dango": ("", "", 1),
                "cat /etc/docker/daemon.json": ("", "", 1),
                "cat /etc/systemd/journald.conf.d/dango.conf": ("", "", 1),
                "cat /etc/logrotate.d/dango": ("", "", 1),
                "cat /etc/systemd/system/dango-web.service": ("", "", 1),
                "cat /etc/caddy/Caddyfile": ("", "", 1),
                "cat /etc/fail2ban/jail.local": ("", "", 1),
                "cat /etc/apt/apt.conf.d/51unattended-upgrades-dango": ("", "", 1),
            }
        )
        progress_calls: list[tuple[str, str]] = []

        setup_server(ssh, on_progress=lambda s, st: progress_calls.append((s, st)))

        # Extract step names in order (take "running" events to get execution order)
        order = [s for s, st in progress_calls if st == "running"]
        assert order == _ALL_STEP_NAMES

    def test_progress_callback(self):
        """on_progress is called with (step, status) for each step."""
        from dango.platform.cloud.server_setup import setup_server

        ssh = _make_ssh_mock(
            exec_results={
                "id -u dango": ("1001", "", 0),
                "docker --version": ("Docker version 24.0", "", 0),
                "caddy version": ("v2.7.5", "", 0),
                "cat /etc/docker/daemon.json": ("", "", 1),
                "cat /etc/systemd/journald.conf.d/dango.conf": ("", "", 1),
                "cat /etc/logrotate.d/dango": ("", "", 1),
                "cat /etc/systemd/system/dango-web.service": ("", "", 1),
                "cat /etc/caddy/Caddyfile": ("", "", 1),
                "cat /etc/fail2ban/jail.local": ("", "", 1),
                "cat /etc/apt/apt.conf.d/51unattended-upgrades-dango": ("", "", 1),
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

    def test_venv_skipped_when_installed(self):
        """Venv step skipped when /srv/dango/venv/bin/dango exists."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        ssh = _make_ssh_mock(exec_results={"test -x /srv/dango/venv/bin/dango": ("", "", 0)})
        result = SetupResult()
        _setup_venv(ssh, result, None)

        assert "python_venv" in result.steps_skipped

    def test_directories_creates_structure(self):
        """Directory step creates /srv/dango structure including logs dir."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_directories

        ssh = _make_ssh_mock()
        result = SetupResult()
        _setup_directories(ssh, result, None)

        cmd = ssh.exec_command.call_args_list[0][0][0]
        assert "/srv/dango/project/.dango/logs" in cmd
        assert "/srv/dango/project/data" in cmd
        assert "chown -R dango:dango" in cmd
        assert "directories" in result.steps_completed

    def test_venv_installs_getdango(self):
        """Venv step creates venv and installs getdango when not present."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        ssh = _make_ssh_mock(exec_results={"test -x /srv/dango/venv/bin/dango": ("", "", 1)})
        result = SetupResult()
        _setup_venv(ssh, result, None)

        # Find the command that has python3 -m venv (skip the test -x check)
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        venv_cmds = [c for c in cmds if "python3 -m venv" in c]
        assert len(venv_cmds) == 1
        assert "pip install getdango" in venv_cmds[0]

    def test_ssh_hardening_disables_password(self):
        """SSH hardening step disables password auth and KbdInteractive."""
        from dango.platform.cloud.server_setup import SetupResult, _setup_ssh_hardening

        ssh = _make_ssh_mock()
        result = SetupResult()
        _setup_ssh_hardening(ssh, result, None)

        cmd = ssh.exec_command.call_args_list[0][0][0]
        assert "PasswordAuthentication no" in cmd
        assert "KbdInteractiveAuthentication no" in cmd
        assert "systemctl reload sshd" in cmd

    def test_systemd_unit_writes_correct_content(self):
        """Systemd unit writes template content to remote file (L2)."""
        from dango.platform.cloud._server_templates import SYSTEMD_UNIT
        from dango.platform.cloud.server_setup import SetupResult, _setup_systemd_unit

        ssh = _make_ssh_mock(
            exec_results={
                "cat /etc/systemd/system/dango-web.service": ("", "", 1),
            }
        )
        result = SetupResult()
        _setup_systemd_unit(ssh, result, None)

        # L2: Verify write_remote_file was called with the actual template
        ssh.write_remote_file.assert_called_once()
        written_path, written_content = ssh.write_remote_file.call_args[0]
        assert written_path == "/etc/systemd/system/dango-web.service"
        assert written_content == SYSTEMD_UNIT
        assert "WorkingDirectory=/srv/dango/project" in written_content
        assert "/srv/dango/venv/bin/dango serve" in written_content

        # Verify daemon-reload + enable (not start)
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        enable_cmd = [c for c in cmds if "systemctl" in c and "enable" in c]
        assert len(enable_cmd) > 0
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
        """Fail2ban config has sshd jail with systemd backend."""
        from dango.platform.cloud._server_templates import FAIL2BAN_JAIL

        assert "sshd" in FAIL2BAN_JAIL
        assert "maxretry = 5" in FAIL2BAN_JAIL
        assert "backend = systemd" in FAIL2BAN_JAIL

    def test_config_steps_skip_when_unchanged(self):
        """Config-writing steps skip when file content matches (M3)."""
        from dango.platform.cloud._server_templates import DOCKER_DAEMON_JSON
        from dango.platform.cloud.server_setup import SetupResult, _setup_docker_daemon

        # Return the exact config content from cat -> skips write
        ssh = _make_ssh_mock(
            exec_results={
                "cat /etc/docker/daemon.json": (DOCKER_DAEMON_JSON, "", 0),
            }
        )
        result = SetupResult()
        _setup_docker_daemon(ssh, result, None)

        assert "docker_daemon" in result.steps_skipped
        ssh.write_remote_file.assert_not_called()


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

    def test_write_remote_config_creates_and_writes(self):
        """_write_remote_config creates parent dir and writes file."""
        from dango.platform.cloud.server_setup import _write_remote_config

        ssh = _make_ssh_mock(exec_results={"cat /etc/caddy/Caddyfile": ("", "", 1)})
        changed = _write_remote_config(
            ssh, "/etc/caddy/Caddyfile", "content", step="test", mode=0o644
        )

        assert changed is True
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("/etc/caddy" in cmd for cmd in cmds)
        ssh.write_remote_file.assert_called_once_with("/etc/caddy/Caddyfile", "content", mode=0o644)

    def test_write_remote_config_skips_when_unchanged(self):
        """_write_remote_config returns False when content matches."""
        from dango.platform.cloud.server_setup import _write_remote_config

        ssh = _make_ssh_mock(exec_results={"cat /etc/caddy/Caddyfile": ("content", "", 0)})
        changed = _write_remote_config(
            ssh, "/etc/caddy/Caddyfile", "content", step="test", mode=0o644
        )

        assert changed is False
        ssh.write_remote_file.assert_not_called()
