"""dango/platform/cloud/server_setup.py

SSH-based server setup orchestration for Dango cloud deployments.

Runs 16 idempotent setup steps on a freshly provisioned Ubuntu 22.04
droplet via an already-connected ``SSHManager``.  Each step checks whether
the target state already exists before acting, making the entire sequence
safe to re-run.  Config file templates are in ``_server_templates.py``.

All steps raise ``CloudProvisioningError`` on failure.  The ``on_progress``
callback, if provided, is called with ``(step_name, status)`` where status
is ``"running"``, ``"done"``, or ``"skipped"``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud._server_templates import (
    CADDYFILE,
    DOCKER_DAEMON_JSON,
    FAIL2BAN_JAIL,
    JOURNALD_CONF,
    LOGROTATE_CONF,
    SYSTEMD_UNIT,
    UNATTENDED_UPGRADES_CONF,
)

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class SetupResult:
    """Result of a full server setup run."""

    steps_completed: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_checked(
    ssh: SSHManager,
    command: str,
    *,
    step: str,
    timeout: int = 120,
) -> str:
    """Run *command* via SSH, raising ``CloudProvisioningError`` on failure.

    Returns:
        The stdout of the command.
    """
    result = ssh.exec_command(command, timeout=timeout)
    if not result.success:
        raise CloudProvisioningError(
            f"Server setup step '{step}' failed (exit {result.exit_code}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _write_remote_config(
    ssh: SSHManager,
    path: str,
    content: str,
    *,
    step: str,
    mode: int = 0o644,
) -> bool:
    """Write a config file to *path* on the remote host.

    Creates parent directories automatically.  If the file already exists
    with identical content, the write is skipped.

    Returns:
        ``True`` if the file was written, ``False`` if skipped (unchanged).
    """
    # Check if the file already has the expected content.
    check = ssh.exec_command(f"cat {path} 2>/dev/null")
    if check.success and check.stdout == content:
        return False
    parent = "/".join(path.split("/")[:-1])
    if parent:
        _run_checked(ssh, f"mkdir -p {parent}", step=step)
    ssh.write_remote_file(path, content, mode=mode)
    return True


def _notify(
    callback: Callable[[str, str], None] | None,
    step: str,
    status: str,
) -> None:
    """Call the progress callback if provided."""
    if callback is not None:
        callback(step, status)


def _mark(
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
    step: str,
    changed: bool,
) -> None:
    """Record step as completed or skipped based on *changed*."""
    if changed:
        result.steps_completed.append(step)
        _notify(on_progress, step, "done")
    else:
        result.steps_skipped.append(step)
        _notify(on_progress, step, "skipped")


# ---------------------------------------------------------------------------
# Individual setup steps
# ---------------------------------------------------------------------------


def _setup_apt_packages(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 1: Install system packages via apt."""
    step = "apt_packages"
    _notify(on_progress, step, "running")
    _run_checked(
        ssh,
        "DEBIAN_FRONTEND=noninteractive apt-get update -qq"
        " && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq"
        " python3-pip python3-venv fail2ban unattended-upgrades"
        " logrotate curl",
        step=step,
        timeout=300,
    )
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_dango_user(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 2: Create the dango system user."""
    step = "create_user"
    _notify(on_progress, step, "running")
    check = ssh.exec_command("id -u dango")
    if check.success:
        result.steps_skipped.append(step)
        _notify(on_progress, step, "skipped")
        return
    _run_checked(ssh, "useradd -m -s /bin/bash dango", step=step)
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_docker(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 3: Install Docker via get.docker.com."""
    step = "install_docker"
    _notify(on_progress, step, "running")
    check = ssh.exec_command("docker --version")
    if check.success:
        result.steps_skipped.append(step)
        _notify(on_progress, step, "skipped")
        return
    _run_checked(
        ssh,
        "curl -fsSL https://get.docker.com | sh",
        step=step,
        timeout=300,
    )
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_docker_group(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 4: Add dango user to docker group."""
    step = "docker_group"
    _notify(on_progress, step, "running")
    _run_checked(ssh, "usermod -aG docker dango", step=step)
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_caddy(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 5: Install Caddy via official apt repository."""
    step = "install_caddy"
    _notify(on_progress, step, "running")
    check = ssh.exec_command("caddy version")
    if check.success:
        result.steps_skipped.append(step)
        _notify(on_progress, step, "skipped")
        return
    _run_checked(
        ssh,
        "apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https"
        " && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key'"
        " | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
        " && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt'"
        " | tee /etc/apt/sources.list.d/caddy-stable.list"
        " && apt-get update -qq"
        " && apt-get install -y -qq caddy",
        step=step,
        timeout=300,
    )
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_directories(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 6: Create /srv/dango directory structure."""
    step = "directories"
    _notify(on_progress, step, "running")
    _run_checked(
        ssh,
        "mkdir -p /srv/dango/project/.dango/logs"
        " /srv/dango/project/data"
        " /srv/dango/project/.dlt"
        " /srv/dango/project/dbt"
        " /srv/dango/backups/deploy"
        " && chown -R dango:dango /srv/dango",
        step=step,
    )
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_venv(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 7: Create Python venv and install getdango."""
    step = "python_venv"
    _notify(on_progress, step, "running")
    check = ssh.exec_command("test -x /srv/dango/venv/bin/dango")
    if check.success:
        result.steps_skipped.append(step)
        _notify(on_progress, step, "skipped")
        return
    _run_checked(
        ssh,
        "python3 -m venv /srv/dango/venv"
        " && /srv/dango/venv/bin/pip install --upgrade pip -q"
        " && /srv/dango/venv/bin/pip install getdango -q",
        step=step,
        timeout=300,
    )
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_ssh_hardening(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 8: Disable SSH password authentication."""
    step = "ssh_hardening"
    _notify(on_progress, step, "running")
    _run_checked(
        ssh,
        "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/'"
        " /etc/ssh/sshd_config"
        " && sed -i 's/^#\\?ChallengeResponseAuthentication.*"
        "/ChallengeResponseAuthentication no/'"
        " /etc/ssh/sshd_config"
        " && sed -i 's/^#\\?KbdInteractiveAuthentication.*"
        "/KbdInteractiveAuthentication no/'"
        " /etc/ssh/sshd_config"
        " && systemctl reload sshd",
        step=step,
    )
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_ssh_key_copy(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 9: Copy root's authorized_keys to dango user."""
    step = "ssh_key_copy"
    _notify(on_progress, step, "running")
    _run_checked(
        ssh,
        "mkdir -p /home/dango/.ssh"
        " && cp /root/.ssh/authorized_keys /home/dango/.ssh/authorized_keys"
        " && chown -R dango:dango /home/dango/.ssh"
        " && chmod 700 /home/dango/.ssh"
        " && chmod 600 /home/dango/.ssh/authorized_keys",
        step=step,
    )
    result.steps_completed.append(step)
    _notify(on_progress, step, "done")


def _setup_docker_daemon(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 10: Configure Docker daemon log rotation."""
    step = "docker_daemon"
    _notify(on_progress, step, "running")
    changed = _write_remote_config(ssh, "/etc/docker/daemon.json", DOCKER_DAEMON_JSON, step=step)
    if changed:
        _run_checked(ssh, "systemctl restart docker", step=step, timeout=60)
    _mark(result, on_progress, step, changed)


def _setup_journald(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 11: Configure journald storage limits."""
    step = "journald"
    _notify(on_progress, step, "running")
    changed = _write_remote_config(
        ssh, "/etc/systemd/journald.conf.d/dango.conf", JOURNALD_CONF, step=step
    )
    if changed:
        _run_checked(ssh, "systemctl restart systemd-journald", step=step)
    _mark(result, on_progress, step, changed)


def _setup_logrotate(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 12: Configure logrotate for activity log."""
    step = "logrotate"
    _notify(on_progress, step, "running")
    changed = _write_remote_config(ssh, "/etc/logrotate.d/dango", LOGROTATE_CONF, step=step)
    _mark(result, on_progress, step, changed)


def _setup_systemd_unit(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 13: Create and enable dango-web systemd service (NOT started)."""
    step = "systemd_unit"
    _notify(on_progress, step, "running")
    changed = _write_remote_config(
        ssh, "/etc/systemd/system/dango-web.service", SYSTEMD_UNIT, step=step
    )
    # Always daemon-reload + enable (idempotent)
    _run_checked(ssh, "systemctl daemon-reload && systemctl enable dango-web", step=step)
    _mark(result, on_progress, step, changed)


def _setup_caddyfile(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 14: Write minimal HTTP Caddyfile."""
    step = "caddyfile"
    _notify(on_progress, step, "running")
    changed = _write_remote_config(ssh, "/etc/caddy/Caddyfile", CADDYFILE, step=step)
    if changed:
        _run_checked(ssh, "systemctl reload-or-restart caddy", step=step)
    _mark(result, on_progress, step, changed)


def _setup_fail2ban(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 15: Configure fail2ban SSH jail."""
    step = "fail2ban"
    _notify(on_progress, step, "running")
    changed = _write_remote_config(ssh, "/etc/fail2ban/jail.local", FAIL2BAN_JAIL, step=step)
    if changed:
        _run_checked(ssh, "systemctl restart fail2ban", step=step)
    _mark(result, on_progress, step, changed)


def _setup_unattended_upgrades(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 16: Enable unattended security upgrades."""
    step = "unattended_upgrades"
    _notify(on_progress, step, "running")
    changed = _write_remote_config(
        ssh, "/etc/apt/apt.conf.d/51unattended-upgrades-dango", UNATTENDED_UPGRADES_CONF, step=step
    )
    # Always enable+start (idempotent)
    _run_checked(
        ssh,
        "systemctl enable unattended-upgrades && systemctl start unattended-upgrades",
        step=step,
    )
    _mark(result, on_progress, step, changed)


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------

# Ordered list of step functions for the setup sequence.
_SETUP_STEPS: list[
    Callable[
        [SSHManager, SetupResult, Callable[[str, str], None] | None],
        None,
    ]
] = [
    _setup_apt_packages,
    _setup_dango_user,
    _setup_docker,
    _setup_docker_group,
    _setup_caddy,
    _setup_directories,
    _setup_venv,
    _setup_ssh_hardening,
    _setup_ssh_key_copy,
    _setup_docker_daemon,
    _setup_journald,
    _setup_logrotate,
    _setup_systemd_unit,
    _setup_caddyfile,
    _setup_fail2ban,
    _setup_unattended_upgrades,
]


def setup_server(
    ssh: SSHManager,
    *,
    on_progress: Callable[[str, str], None] | None = None,
) -> SetupResult:
    """Run all server setup steps on a connected droplet.

    The SSH connection must already be established as **root**.  Each step
    is idempotent — safe to re-run on a partially or fully configured server.

    The systemd service is **enabled but not started**; the deploy wizard
    (TASK-029) starts it after file sync.

    Args:
        ssh: A connected ``SSHManager`` (as root).
        on_progress: Optional callback ``(step_name, status)`` where status
            is ``"running"``, ``"done"``, or ``"skipped"``.

    Returns:
        ``SetupResult`` with completed, skipped, and warning lists.

    Raises:
        CloudProvisioningError: If any step fails.
    """
    result = SetupResult()

    for step_fn in _SETUP_STEPS:
        step_fn(ssh, result, on_progress)

    return result
