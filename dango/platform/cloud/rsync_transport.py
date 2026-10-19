"""dango/platform/cloud/rsync_transport.py

rsync over system ssh that verifies the host key SSHManager pinned (never skips the check).

rsync uses OpenSSH, not paramiko.  It is pointed at the same known_hosts file
paramiko pinned the server into, with ``StrictHostKeyChecking=yes``, so a
server presenting any other key is refused.  The target must already be
pinned (paramiko connects first); rsync never adds keys itself.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError, CloudSSHError
from dango.platform.cloud.project_files import AS_DANGO

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager


@dataclass(frozen=True)
class RsyncTarget:
    """Host-key settings for rsync, taken from a connected SSHManager."""

    known_hosts_path: Path
    legacy_known_hosts_paths: list[Path]
    port: int
    username: str = "root"


def rsync_target(ssh: SSHManager, remote_host: str) -> RsyncTarget:
    """Return rsync's host-key settings for *remote_host* via *ssh*.

    Raises:
        CloudProvisioningError: If *remote_host* is not the host *ssh* connected to.
    """
    if ssh.host is not None and ssh.host != remote_host:
        raise CloudProvisioningError(
            f"rsync target {remote_host!r} does not match the verified SSH host {ssh.host!r}"
        )
    return RsyncTarget(
        known_hosts_path=ssh.known_hosts_path,
        legacy_known_hosts_paths=list(ssh.legacy_known_hosts_paths),
        port=ssh.port or 22,
        username=ssh.username or "root",
    )


def _build_rsync_ssh_arg(ssh_key_path: Path, known_hosts_path: Path, port: int = 22) -> str:
    """Build the ``-e`` argument: verify the host against the paramiko-pinned known_hosts.

    Raises:
        CloudProvisioningError: If a path contains a quote character, or the known_hosts
            path contains ``$`` (ssh would expand it).
    """
    from dango.platform.cloud.ssh import ssh_argv_path, ssh_config_path

    try:
        key = ssh_argv_path(ssh_key_path)
        known_hosts = ssh_config_path(known_hosts_path)
    except CloudSSHError as exc:
        raise CloudProvisioningError(str(exc)) from exc
    # rsync splits -e on whitespace and strips one level of quotes: the single quotes
    # keep the option together for rsync, the double quotes keep ssh from splitting.
    return (
        f'ssh -i "{key}" -p {int(port)} -o StrictHostKeyChecking=yes '
        f"-o 'UserKnownHostsFile=\"{known_hosts}\"' -o BatchMode=yes"
    )


def _require_pinned_host(
    known_hosts_path: Path, legacy_paths: Sequence[Path], host: str, port: int
) -> None:
    """Raise unless *host* has a pinned key in *known_hosts_path* (legacy pins carried over)."""
    from dango.platform.cloud.ssh import ensure_host_pinned, host_key_name

    name = host_key_name(host, port)
    try:
        pinned = ensure_host_pinned(known_hosts_path, legacy_paths, name)
    except CloudSSHError as exc:
        raise CloudProvisioningError(str(exc)) from exc
    if not pinned:
        raise CloudProvisioningError(
            f"Host key for {name} is not pinned in {known_hosts_path}; "
            "reconnect / re-run the command so the host key is verified first."
        )


def _rsync_directory(
    local_dir: Path,
    remote_user_host_dir: str,
    ssh_key_path: Path,
    *,
    known_hosts_path: Path,
    port: int = 22,
    legacy_known_hosts_paths: Sequence[Path] = (),
    delete: bool = True,
    dry_run: bool = False,
    run_as_service_user: bool = True,
) -> str:
    """Sync *local_dir* to *remote_user_host_dir* (``user@host:path``) via rsync.

    Returns:
        rsync stdout output.

    Raises:
        CloudProvisioningError: If the host is not pinned, or rsync exits non-zero
            (including a host key mismatch).
    """
    user_host = remote_user_host_dir.partition(":")[0]
    host = user_host.rpartition("@")[2]
    _require_pinned_host(known_hosts_path, legacy_known_hosts_paths, host, port)

    cmd: list[str] = [
        "rsync",
        "-avz",
        "-e",
        _build_rsync_ssh_arg(ssh_key_path, known_hosts_path, port),
    ]
    if run_as_service_user:
        # The server-side rsync runs as dango, so it can only touch what dango can reach:
        # a symlink planted in the project tree is replaced, never followed to a root-only file.
        cmd.append(f"--rsync-path={AS_DANGO} rsync")
    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd.append("--dry-run")

    # Trailing slash on source ensures contents are synced, not the dir itself
    cmd.append(f"{local_dir}/")
    cmd.append(remote_user_host_dir)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise CloudProvisioningError(
            f"rsync failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout
