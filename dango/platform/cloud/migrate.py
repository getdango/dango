"""dango/platform/cloud/migrate.py

Server migration for Dango cloud deployments.

Creates a new Droplet, transfers data via DigitalOcean Spaces, and (on
success) destroys the old Droplet.  Used when disk size or region changes
are needed, since DigitalOcean in-place resize cannot shrink disks or
change regions.

All functions require an already-connected ``SSHManager`` (as root on the
old server).  A new SSH connection is established to the new server during
migration.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dango.exceptions import CloudError, CloudProvisioningError

if TYPE_CHECKING:
    from dango.platform.cloud.digitalocean import DigitalOceanClient
    from dango.platform.cloud.ssh import SSHManager

_PROJECT_DIR = "/srv/dango/project"
_SECRETS_FILES = [
    f"{_PROJECT_DIR}/.env",
    f"{_PROJECT_DIR}/.dlt/secrets.toml",
]


@dataclass
class MigrateResult:
    """Result returned by :func:`migrate_server`."""

    old_droplet_id: int
    new_droplet_id: int
    new_droplet_ip: str
    new_region: str
    new_size: str
    duration_seconds: float
    old_droplet_destroyed: bool
    dns_updated: bool
    warnings: list[str] = field(default_factory=list)


def _notify(callback: Callable[[str, str], None] | None, step: str, status: str) -> None:
    """Call the progress callback if provided."""
    if callback is not None:
        callback(step, status)


def _upload_backup_to_spaces(
    ssh: SSHManager,
    archive_path: str,
    spaces_config: Any,
) -> str:
    """Upload a backup archive to Spaces from the remote server via SSH.

    Writes a Python script to the server and executes it.  Uses ``repr()``
    for all interpolated values to prevent injection.

    Returns:
        The Spaces object key for the uploaded archive.
    """
    archive_name = archive_path.rsplit("/", 1)[-1]
    spaces_key = f"migration/{archive_name}"

    region = spaces_config.region or "nyc3"
    endpoint = f"https://{region}.digitaloceanspaces.com"

    script = (
        "import boto3, os\n"
        f"s3 = boto3.client('s3', region_name={region!r},\n"
        f"    endpoint_url={endpoint!r},\n"
        f"    aws_access_key_id=os.environ[{spaces_config.access_key_env!r}],\n"
        f"    aws_secret_access_key=os.environ[{spaces_config.secret_key_env!r}])\n"
        f"s3.upload_file({archive_path!r}, {spaces_config.bucket!r}, {spaces_key!r})\n"
    )

    script_path = "/tmp/_dango_upload.py"
    ssh.write_remote_file(script_path, script, mode=0o600)
    try:
        result = ssh.exec_command(
            f"source {_PROJECT_DIR}/.env 2>/dev/null && /srv/dango/venv/bin/python {script_path}",
            timeout=600,
        )
    finally:
        ssh.exec_command(f"rm -f {script_path}")

    if not result.success:
        raise CloudProvisioningError(
            f"Failed to upload backup to Spaces (exit {result.exit_code}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return spaces_key


def _download_backup_from_spaces(
    ssh: SSHManager,
    spaces_key: str,
    spaces_config: Any,
) -> str:
    """Download a backup archive from Spaces to the remote server via SSH.

    Writes a Python script to the server and executes it.  Uses ``repr()``
    for all interpolated values to prevent injection.

    Returns:
        The local path of the downloaded archive on the server.
    """
    archive_name = spaces_key.rsplit("/", 1)[-1]
    local_path = f"/srv/dango/backups/deploy/{archive_name}"

    region = spaces_config.region or "nyc3"
    endpoint = f"https://{region}.digitaloceanspaces.com"

    script = (
        "import boto3, os\n"
        "os.makedirs('/srv/dango/backups/deploy', exist_ok=True)\n"
        f"s3 = boto3.client('s3', region_name={region!r},\n"
        f"    endpoint_url={endpoint!r},\n"
        f"    aws_access_key_id=os.environ[{spaces_config.access_key_env!r}],\n"
        f"    aws_secret_access_key=os.environ[{spaces_config.secret_key_env!r}])\n"
        f"s3.download_file({spaces_config.bucket!r}, {spaces_key!r}, {local_path!r})\n"
    )

    script_path = "/tmp/_dango_download.py"
    ssh.write_remote_file(script_path, script, mode=0o600)
    try:
        result = ssh.exec_command(
            f"source {_PROJECT_DIR}/.env 2>/dev/null && /srv/dango/venv/bin/python {script_path}",
            timeout=600,
        )
    finally:
        ssh.exec_command(f"rm -f {script_path}")

    if not result.success:
        raise CloudProvisioningError(
            f"Failed to download backup from Spaces (exit {result.exit_code}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return local_path


def _copy_secrets_between_servers(
    old_ssh: SSHManager,
    new_ssh: SSHManager,
) -> list[str]:
    """Copy secret files from old server to new server via SFTP.

    Downloads ``.env`` and ``.dlt/secrets.toml`` from the old server and
    uploads them to the new server.

    Returns:
        List of warnings for missing files.
    """
    warnings: list[str] = []
    for remote_path in _SECRETS_FILES:
        content = old_ssh.exec_command(f"cat {remote_path} 2>/dev/null")
        if not content.success or not content.stdout:
            warnings.append(f"Secret file not found on old server: {remote_path}")
            continue
        # Ensure parent directory exists on new server
        parent_dir = remote_path.rsplit("/", 1)[0]
        new_ssh.exec_command(f"mkdir -p {parent_dir}")
        new_ssh.write_remote_file(remote_path, content.stdout, mode=0o600)
    return warnings


def _update_firewall_droplets(
    client: DigitalOceanClient,
    firewall_id: str,
    old_droplet_id: int,
    new_droplet_id: int,
) -> None:
    """Swap old droplet for new in the firewall's droplet list."""
    fw = client.get_firewall(firewall_id)
    droplet_ids: list[int] = list(fw.get("droplet_ids", []))

    if old_droplet_id in droplet_ids:
        droplet_ids.remove(old_droplet_id)
    if new_droplet_id not in droplet_ids:
        droplet_ids.append(new_droplet_id)

    client.update_firewall(
        firewall_id=firewall_id,
        name=fw["name"],
        inbound_rules=fw.get("inbound_rules", []),
        outbound_rules=fw.get("outbound_rules", []),
        droplet_ids=droplet_ids,
    )


def migrate_server(
    client: DigitalOceanClient,
    old_ssh: SSHManager,
    old_config: Any,
    new_size: str,
    new_region: str,
    *,
    project_root: Path,
    on_progress: Callable[[str, str], None] | None = None,
) -> MigrateResult:
    """Migrate to a new server with different size/region.

    Workflow:
        1. Validate size and Spaces config
        2. Create backup on old server
        3. Upload backup to Spaces
        4. Provision new droplet
        5. Setup new server
        6. Copy secrets from old → new
        7. Download backup from Spaces on new server
        8. Restore from archive on new server
        9. Configure domain (if set)
        10. Update firewall (if configured)
        11. Verify health on new server
        12. Destroy old droplet (if healthy) or keep both

    Args:
        client: ``DigitalOceanClient`` instance.
        old_ssh: Connected ``SSHManager`` to the old server (as root).
        old_config: ``CloudConfig`` of the current deployment.
        new_size: Target size slug for the new droplet.
        new_region: Target region slug for the new droplet.
        project_root: Local project root for saving updated config.
        on_progress: Optional ``(step, status)`` callback.

    Returns:
        ``MigrateResult`` with migration details.

    Raises:
        CloudError: If Spaces is not configured or validation fails.
        CloudProvisioningError: If any step fails.
    """
    from dango.platform.cloud.backup import create_backup, restore_from_archive, verify_health
    from dango.platform.cloud.provisioning import (
        provision_droplet,
        save_provisioning_metadata,
    )
    from dango.platform.cloud.resize import validate_size_slug
    from dango.platform.cloud.server_setup import setup_server
    from dango.platform.cloud.ssh import SSHManager as SSHManagerCls

    start_time = time.monotonic()
    warnings: list[str] = []
    new_droplet_id: int | None = None
    new_ssh: SSHManager | None = None

    # 1. Validate
    validate_size_slug(new_size)
    if old_config.spaces is None:
        raise CloudError(
            "Migration requires Spaces for data transfer. "
            "Configure Spaces first with: dango remote backup enable",
            error_code="DANGO-D040",
        )

    old_droplet_id = old_config.droplet_id

    try:
        # 2. Create backup on old server
        _notify(on_progress, "backup", "running")
        backup_result = create_backup(old_ssh, backup_type="pre-migrate")
        archive_path = backup_result.archive_path
        _notify(on_progress, "backup", "done")

        # 3. Upload to Spaces
        _notify(on_progress, "upload_spaces", "running")
        spaces_key = _upload_backup_to_spaces(old_ssh, archive_path, old_config.spaces)
        _notify(on_progress, "upload_spaces", "done")

        # 4. Provision new droplet
        _notify(on_progress, "provision", "running")
        ssh_key_ids: list[int] = []
        if old_config.ssh_key_id is not None:
            ssh_key_ids = [old_config.ssh_key_id]

        droplet_name = f"dango-{new_region}"
        new_droplet = provision_droplet(
            client,
            droplet_name,
            new_region,
            new_size,
            ssh_key_ids,
        )
        new_droplet_id = new_droplet["id"]
        new_droplet_ip = _extract_ipv4(new_droplet)
        _notify(on_progress, "provision", "done")

        # 5. Connect SSH to new server
        key_path = project_root / old_config.ssh_key_path
        new_ssh = SSHManagerCls(key_path=key_path)
        new_ssh.connect(new_droplet_ip, username="root")

        # 6. Setup new server
        _notify(on_progress, "setup_server", "running")
        setup_server(new_ssh, domain=old_config.domain)
        _notify(on_progress, "setup_server", "done")

        # 7. Copy secrets
        _notify(on_progress, "copy_secrets", "running")
        secret_warnings = _copy_secrets_between_servers(old_ssh, new_ssh)
        warnings.extend(secret_warnings)
        _notify(on_progress, "copy_secrets", "done")

        # 8. Download backup from Spaces on new server
        _notify(on_progress, "download_spaces", "running")
        new_archive_path = _download_backup_from_spaces(new_ssh, spaces_key, old_config.spaces)
        _notify(on_progress, "download_spaces", "done")

        # 9. Restore from archive on new server
        _notify(on_progress, "restore", "running")
        restore_from_archive(new_ssh, new_archive_path)
        _notify(on_progress, "restore", "done")

        # 10. Configure domain
        dns_updated = False
        if old_config.domain:
            _notify(on_progress, "domain", "running")
            try:
                from dango.platform.cloud.domain import set_domain

                set_domain(new_ssh, project_root, old_config.domain)
                dns_updated = True
            except Exception as exc:
                warnings.append(f"Domain configuration failed: {exc}")
            _notify(on_progress, "domain", "done")

        # 11. Update firewall
        if old_config.firewall_id:
            _notify(on_progress, "firewall", "running")
            try:
                _update_firewall_droplets(
                    client, old_config.firewall_id, old_droplet_id, new_droplet_id
                )
            except Exception as exc:
                warnings.append(f"Firewall update failed: {exc}")
            _notify(on_progress, "firewall", "done")

        # 12. Verify health
        _notify(on_progress, "verify_health", "running")
        health_ok = verify_health(new_ssh)
        _notify(on_progress, "verify_health", "done")

        # 13. Destroy old or keep both
        old_destroyed = False
        if health_ok:
            _notify(on_progress, "cleanup", "running")
            try:
                client.delete_droplet(old_droplet_id)
                old_destroyed = True
            except Exception as exc:
                warnings.append(f"Failed to destroy old droplet: {exc}")
            _notify(on_progress, "cleanup", "done")

            # Update cloud.yml
            save_provisioning_metadata(
                project_root,
                droplet_id=new_droplet_id,
                droplet_ip=new_droplet_ip,
                region=new_region,
                size=new_size,
            )
        else:
            warnings.append(
                f"Health check failed on new server ({new_droplet_ip}). "
                f"Old server ({old_config.droplet_ip}) is still running. "
                "Investigate both servers manually."
            )

    except Exception:
        # Cleanup new droplet on failure (best effort)
        if new_droplet_id is not None:
            try:
                client.delete_droplet(new_droplet_id)
            except Exception:
                pass
        if new_ssh is not None:
            try:
                new_ssh.disconnect()
            except Exception:
                pass
        raise

    if new_ssh is not None:
        try:
            new_ssh.disconnect()
        except Exception:
            pass

    return MigrateResult(
        old_droplet_id=old_droplet_id,
        new_droplet_id=new_droplet_id,
        new_droplet_ip=new_droplet_ip,
        new_region=new_region,
        new_size=new_size,
        duration_seconds=round(time.monotonic() - start_time, 1),
        old_droplet_destroyed=old_destroyed,
        dns_updated=dns_updated,
        warnings=warnings,
    )


def _extract_ipv4(droplet: dict[str, Any]) -> str:
    """Extract the public IPv4 address from a droplet dict."""
    for network in droplet.get("networks", {}).get("v4", []):
        if network.get("type") == "public":
            return str(network["ip_address"])
    raise CloudError(
        "Could not find public IPv4 address for new droplet.",
        error_code="DANGO-D041",
    )
