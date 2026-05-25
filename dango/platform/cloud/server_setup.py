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
from dango.logging import get_logger
from dango.platform.cloud._server_templates import (
    DOCKER_DAEMON_JSON,
    FAIL2BAN_JAIL,
    JOURNALD_CONF,
    LOGROTATE_CONF,
    UNATTENDED_UPGRADES_CONF,
    build_caddyfile,
    build_systemd_unit,
)

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

_logger = get_logger(__name__)


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
    # BUG-121: Set system-wide dpkg lock timeout so ALL apt commands
    # (including Docker's get.docker.com install script) wait for locks.
    _write_remote_config(
        ssh,
        "/etc/apt/apt.conf.d/99lock-timeout",
        'DPkg::Lock::Timeout "120";\n',
        step=step,
    )
    # BUG-250: Wait for any cloud-init apt locks before running apt commands.
    # fuser is pre-installed on Ubuntu 22.04; if absent, the while loop exits
    # immediately (safe fallback).
    # Note: "|| true" scopes to add-apt-repository only (left-to-right
    # associativity), so apt-get update/install still fail on real errors.
    _run_checked(
        ssh,
        "while fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend"
        " >/dev/null 2>&1; do sleep 5; done"
        " && add-apt-repository -y universe 2>/dev/null || true"
        " && DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update -qq"
        " && DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y -qq"
        " python3-pip python3-venv fail2ban unattended-upgrades"
        " logrotate curl",
        step=step,
        timeout=420,
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
    *,
    dango_version: str | None = None,
    install_source: tuple[str, str] | None = None,
) -> None:
    """Step 7: Create Python venv and install getdango."""
    step = "python_venv"
    _notify(on_progress, step, "running")
    check = ssh.exec_command("test -x /srv/dango/venv/bin/dango")
    if check.success:
        result.steps_skipped.append(step)
        _notify(on_progress, step, "skipped")
        return
    if install_source:
        # BUG-123: Use resolved install source (PyPI, git, or editable)
        pkg = install_source[1]
    elif dango_version:
        import re

        if not re.match(r"^[a-zA-Z0-9._-]+$", dango_version):
            raise ValueError(f"Invalid version string: {dango_version!r}")
        pkg = f"getdango=={dango_version}"
    else:
        pkg = "getdango"
    _run_checked(
        ssh,
        "python3 -m venv /srv/dango/venv"
        " && /srv/dango/venv/bin/pip install --upgrade pip -q"
        f" && /srv/dango/venv/bin/pip install '{pkg}' -q",
        step=step,
        timeout=300,
    )
    # Install spaCy model for PII scanning (best-effort, non-fatal)
    spacy_result = ssh.exec_command(
        "/srv/dango/venv/bin/python -m spacy download en_core_web_sm -q",
        timeout=120,
    )
    if not spacy_result.success:
        result.warnings.append(
            "spaCy model 'en_core_web_sm' could not be installed. "
            "PII scanning will be unavailable until installed manually: "
            "/srv/dango/venv/bin/python -m spacy download en_core_web_sm"
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
    *,
    workers: int | None = None,
) -> None:
    """Step 13: Create and enable dango-web systemd service (NOT started)."""
    step = "systemd_unit"
    _notify(on_progress, step, "running")
    unit_content = build_systemd_unit(workers=workers)
    changed = _write_remote_config(
        ssh, "/etc/systemd/system/dango-web.service", unit_content, step=step
    )
    # Always daemon-reload + enable (idempotent)
    _run_checked(ssh, "systemctl daemon-reload && systemctl enable dango-web", step=step)
    _mark(result, on_progress, step, changed)


def _setup_caddyfile(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
    *,
    domain: str | None = None,
) -> None:
    """Step 14: Write Caddyfile (HTTPS with domain or HTTP-only)."""
    step = "caddyfile"
    _notify(on_progress, step, "running")
    content = build_caddyfile(domain)
    changed = _write_remote_config(ssh, "/etc/caddy/Caddyfile", content, step=step)
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


def _setup_ufw(
    ssh: SSHManager,
    result: SetupResult,
    on_progress: Callable[[str, str], None] | None,
) -> None:
    """Step 17 (BYOS only): Install and configure UFW firewall."""
    step = "ufw"
    _notify(on_progress, step, "running")

    # Idempotent check: if UFW active with our rules, skip
    check = ssh.exec_command("ufw status 2>/dev/null | grep -q 'Status: active'")
    if check.success:
        rules = ssh.exec_command("ufw status | grep '80/tcp' && ufw status | grep '443/tcp'")
        if rules.success:
            _mark(result, on_progress, step, changed=False)
            return

    _run_checked(
        ssh,
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw"
        " && ufw default deny incoming"
        " && ufw default allow outgoing"
        " && ufw allow ssh"
        " && ufw allow 80/tcp"
        " && ufw allow 443/tcp"
        " && echo 'y' | ufw enable",
        step=step,
        timeout=60,
    )
    _mark(result, on_progress, step, changed=True)


# ---------------------------------------------------------------------------
# Install source detection (BUG-123 / BUG-249)
# ---------------------------------------------------------------------------


def _normalize_git_url(url: str) -> str:
    """Convert SSH-format Git URLs to HTTPS for remote server installs.

    ``git@github.com:org/repo.git`` → ``https://github.com/org/repo.git``

    Remote servers typically lack SSH keys for GitHub. HTTPS works for
    public repos and repos with token auth.  Already-HTTPS URLs are
    returned unchanged.
    """
    # SSH shorthand: git@github.com:org/repo.git
    if "://" not in url and ":" in url and "@" in url.split(":")[0]:
        host_part, path_part = url.split(":", 1)
        host = host_part.split("@", 1)[1] if "@" in host_part else host_part
        return f"https://{host}/{path_part}"
    # ssh:// URL
    if url.startswith("ssh://"):
        stripped = url[len("ssh://") :]
        if "@" in stripped:
            stripped = stripped.split("@", 1)[1]
        return f"https://{stripped}"
    return url


def resolve_install_source() -> tuple[str, str]:
    """Determine how getdango should be installed on the remote server.

    Inspects the local installation's ``direct_url.json`` (PEP 610) to decide:

    - **PyPI install** (no ``direct_url.json``): ``("pypi", "getdango==<version>")``
    - **Git install** (``vcs_info`` present): ``("git", "git+<url>@<commit>#egg=getdango")``
    - **Editable install** (``dir_info.editable``): resolve git remote + HEAD from
      the source directory, return git install command.  Falls back to
      ``("editable", "getdango")`` if git info is unavailable.

    Returns:
        ``(source_type, pip_install_arg)`` tuple.
    """
    import importlib.metadata
    import json
    import subprocess  # noqa: S404 — git info lookup only

    try:
        dist = importlib.metadata.distribution("getdango")
    except importlib.metadata.PackageNotFoundError:
        return ("editable", "getdango")

    raw = dist.read_text("direct_url.json")
    if raw is None:
        # No direct_url.json — could be PyPI or an editable install without
        # metadata (e.g., worktree installs).  Check if the package source
        # is inside a git repo.
        from pathlib import Path as _Path

        import dango

        pkg_dir = str(_Path(dango.__file__).resolve().parent)
        try:
            remote = subprocess.run(  # noqa: S603, S607
                ["git", "-C", pkg_dir, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            head = subprocess.run(  # noqa: S603, S607
                ["git", "-C", pkg_dir, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if remote.returncode == 0 and head.returncode == 0:
                url = remote.stdout.strip()
                commit = head.stdout.strip()
                return ("git", f"git+{_normalize_git_url(url)}@{commit}#egg=getdango")
        except Exception:
            _logger.debug("worktree_git_info_failed", exc_info=True)

        return ("pypi", f"getdango=={dango.__version__}")

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        import dango

        return ("pypi", f"getdango=={dango.__version__}")

    # Git install (pip install git+https://...)
    vcs_info = data.get("vcs_info")
    if vcs_info:
        vcs = vcs_info.get("vcs", "git")
        url = data.get("url", "")
        commit = vcs_info.get("commit_id", "")
        if url and commit:
            return ("git", f"{vcs}+{_normalize_git_url(url)}@{commit}#egg=getdango")
        if url:
            return ("git", f"{vcs}+{_normalize_git_url(url)}#egg=getdango")

    # Editable install (pip install -e .)
    dir_info = data.get("dir_info", {})
    if dir_info.get("editable"):
        src_dir = data.get("url", "").replace("file://", "")
        if src_dir:
            try:
                remote = subprocess.run(  # noqa: S603, S607
                    ["git", "-C", src_dir, "remote", "get-url", "origin"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                head = subprocess.run(  # noqa: S603, S607
                    ["git", "-C", src_dir, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if remote.returncode == 0 and head.returncode == 0:
                    url = remote.stdout.strip()
                    commit = head.stdout.strip()
                    return ("git", f"git+{_normalize_git_url(url)}@{commit}#egg=getdango")
            except Exception:
                _logger.debug("editable_install_git_info_failed", exc_info=True)

    return ("editable", "getdango")


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


# Ordered list of step functions for the setup sequence.
def _preflight_check(ssh: SSHManager, result: SetupResult) -> None:
    """Check server meets minimum requirements before setup.

    Warns (does not abort) if RAM < 4 GB or available disk < 20 GB.
    """
    # Check RAM (total in MB)
    ram_result = ssh.exec_command("free -m | awk '/Mem:/ {print $2}'", timeout=10)
    if ram_result.success and ram_result.stdout.strip().isdigit():
        ram_mb = int(ram_result.stdout.strip())
        if ram_mb < 3800:  # ~4 GB with overhead
            result.warnings.append(
                f"Low RAM: {ram_mb} MB detected (4 GB minimum recommended). "
                "Metabase and Docker may not run reliably."
            )
            _logger.warning("preflight_low_ram", ram_mb=ram_mb, minimum=4096)

    # Check available disk (in MB)
    disk_result = ssh.exec_command(
        "df / --output=avail -BM 2>/dev/null | tail -1 | tr -d ' M'",
        timeout=10,
    )
    if disk_result.success and disk_result.stdout.strip().isdigit():
        disk_mb = int(disk_result.stdout.strip())
        if disk_mb < 20000:  # 20 GB
            result.warnings.append(
                f"Low disk: {disk_mb} MB available (20 GB minimum recommended). "
                "Docker images, Python venv, and data may not fit."
            )
            _logger.warning("preflight_low_disk", available_mb=disk_mb, minimum_mb=20000)


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
    domain: str | None = None,
    setup_ufw: bool = False,
    dango_version: str | None = None,
    install_source: tuple[str, str] | None = None,
) -> SetupResult:
    """Run all server setup steps on a connected server.

    SSH must be established as **root**.  Each step is idempotent.

    Args:
        ssh: A connected ``SSHManager`` (as root).
        on_progress: ``(step_name, status)`` callback.
        domain: FQDN for HTTPS (Caddy auto-TLS). ``None`` → HTTP-only.
        setup_ufw: If ``True``, install and configure UFW firewall
            (used for BYOS deployments that lack a cloud-managed firewall).
        dango_version: Pin getdango to this version. ``None`` → latest.
            Ignored when *install_source* is provided.
        install_source: ``(source_type, pip_arg)`` from
            :func:`resolve_install_source`.  Takes precedence over
            *dango_version*.

    Raises:
        CloudProvisioningError: If any step fails.
    """
    result = SetupResult()

    # --- Pre-flight checks: RAM and disk ---
    _preflight_check(ssh, result)

    # Single worker for v1 — multi-worker breaks WebSocket broadcasts
    # (ws_manager is per-process, so broadcasts only reach clients on the
    # same worker).  Multi-worker requires Redis pub/sub for cross-worker
    # message delivery, which is a post-v1 infrastructure change.
    # Single worker handles small team load fine.

    for step_fn in _SETUP_STEPS:
        if step_fn is _setup_caddyfile:
            _setup_caddyfile(ssh, result, on_progress, domain=domain)
        elif step_fn is _setup_systemd_unit:
            _setup_systemd_unit(ssh, result, on_progress, workers=None)
        elif step_fn is _setup_venv:
            _setup_venv(
                ssh,
                result,
                on_progress,
                dango_version=dango_version,
                install_source=install_source,
            )
        else:
            step_fn(ssh, result, on_progress)

    if setup_ufw:
        _setup_ufw(ssh, result, on_progress)

    return result
