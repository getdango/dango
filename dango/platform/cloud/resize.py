"""dango/platform/cloud/resize.py

Remote server resize (in-place CPU/RAM change) for Dango cloud deployments.

Orchestrates the DigitalOcean resize workflow: power off → resize → power on,
then regenerates ``dbt/profiles.yml`` to match the new hardware specs.  All
functions require an already-connected ``SSHManager`` (as root).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from dango.exceptions import CloudError

if TYPE_CHECKING:
    from dango.platform.cloud.digitalocean import DigitalOceanClient
    from dango.platform.cloud.ssh import SSHManager

_SIZE_SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9\-_.]+$")
_PROFILES_PATH = "/srv/dango/project/dbt/profiles.yml"
_DBT_PROJECT_PATH = "/srv/dango/project/dbt/dbt_project.yml"


@dataclass
class ResizeResult:
    """Result returned by :func:`resize_droplet`."""

    old_size: str
    new_size: str
    old_tier: str | None
    new_tier: str | None
    duration_seconds: float
    backup_path: str | None
    dbt_profiles_regenerated: bool
    warnings: list[str] = field(default_factory=list)


def validate_size_slug(slug: str) -> None:
    """Validate a DigitalOcean size slug against an allowlist pattern.

    Raises:
        CloudError: If *slug* contains characters outside ``[a-zA-Z0-9\\-_.]``.
    """
    if not _SIZE_SLUG_PATTERN.match(slug):
        raise CloudError(
            f"Invalid size slug: {slug!r}. "
            "Only alphanumeric characters, hyphens, underscores, and dots are allowed.",
            error_code="DANGO-D030",
        )


def get_disk_warning(current_slug: str, new_slug: str) -> str | None:
    """Return a warning if the new tier has less disk than the current one.

    DigitalOcean disk resizes only grow — this is informational only.
    Returns ``None`` if no warning is needed.
    """
    from dango.platform.cloud.provisioning import get_size_tier

    current_tier = get_size_tier(current_slug)
    new_tier = get_size_tier(new_slug)
    if current_tier and new_tier and new_tier.disk_gb < current_tier.disk_gb:
        return (
            f"New tier has {new_tier.disk_gb} GB disk vs current {current_tier.disk_gb} GB. "
            "DigitalOcean does not shrink disks — disk size will remain unchanged."
        )
    return None


def generate_dbt_profiles_yml(
    project_name: str,
    vcpus: int,
    ram_gb: int,
    dbt_overrides: Any | None = None,
) -> str:
    """Generate ``dbt/profiles.yml`` content tuned for the given hardware.

    Args:
        project_name: The dbt project name (from ``dbt_project.yml``).
        vcpus: Number of virtual CPUs on the server.
        ram_gb: Total RAM in gigabytes.
        dbt_overrides: Optional ``DbtOverrides`` with explicit ``threads``
            and/or ``memory_limit`` overrides.

    Returns:
        The full YAML content for ``profiles.yml``.
    """
    threads = vcpus
    memory_limit = f"{max(1, ram_gb // 4)}GB"

    if dbt_overrides is not None:
        if getattr(dbt_overrides, "threads", None) is not None:
            threads = dbt_overrides.threads
        if getattr(dbt_overrides, "memory_limit", None) is not None:
            memory_limit = dbt_overrides.memory_limit

    profile: dict[str, Any] = {
        project_name: {
            "target": "dev",
            "outputs": {
                "dev": {
                    "type": "duckdb",
                    "path": "/srv/dango/project/data/warehouse.duckdb",
                    "schema": "main",
                    "threads": threads,
                    "extensions": ["httpfs", "parquet"],
                    "settings": {
                        "memory_limit": memory_limit,
                        "threads": threads,
                    },
                }
            },
        }
    }
    header = (
        "# dbt Profile Configuration for DuckDB\n"
        "# Auto-generated for cloud deployment — do not edit manually\n\n"
    )
    return header + yaml.dump(profile, default_flow_style=False, sort_keys=False)


def regenerate_dbt_profiles(
    ssh: SSHManager,
    new_size_slug: str,
    dbt_overrides: Any | None = None,
) -> bool:
    """Regenerate ``dbt/profiles.yml`` on the server for the new size.

    Reads the project name from ``dbt_project.yml`` on the server, looks
    up the tier specs (or uses defaults for custom sizes), and writes the
    new profiles via SSH.

    Returns:
        ``True`` if profiles were written, ``False`` if ``dbt_project.yml``
        was not found on the server.
    """
    from dango.platform.cloud.provisioning import get_size_tier

    # Read project name from dbt_project.yml on server
    result = ssh.exec_command(f"cat {_DBT_PROJECT_PATH} 2>/dev/null")
    if not result.success or not result.stdout.strip():
        return False

    # Parse project name via YAML
    project_name = "dango"
    try:
        data: dict[str, Any] = yaml.safe_load(result.stdout)
        if isinstance(data, dict) and "name" in data:
            project_name = str(data["name"])
    except yaml.YAMLError:
        pass  # Fall back to default name

    # Determine specs from tier or defaults
    tier = get_size_tier(new_size_slug)
    if tier:
        vcpus = tier.vcpus
        ram_gb = tier.ram_gb
    else:
        # Custom size: parse slug pattern s-{vcpus}vcpu-{ram}gb
        vcpus = 2
        ram_gb = 4
        parts = new_size_slug.split("-")
        for part in parts:
            if part.endswith("vcpu"):
                try:
                    vcpus = int(part[:-4])
                except ValueError:
                    pass
            elif part.endswith("gb"):
                try:
                    ram_gb = int(part[:-2])
                except ValueError:
                    pass

    content = generate_dbt_profiles_yml(project_name, vcpus, ram_gb, dbt_overrides)
    ssh.write_remote_file(_PROFILES_PATH, content, mode=0o644)
    return True


def _notify(callback: Callable[[str, str], None] | None, step: str, status: str) -> None:
    """Call the progress callback if provided."""
    if callback is not None:
        callback(step, status)


def resize_droplet(
    client: DigitalOceanClient,
    ssh: SSHManager,
    droplet_id: int,
    new_size: str,
    *,
    create_backup: bool = True,
    dbt_overrides: Any | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    project_root: Path | None = None,
    region: str | None = None,
) -> ResizeResult:
    """Resize a Droplet to a new size slug.

    Workflow:
        1. Validate size slug
        2. Get current droplet info
        3. Create pre-resize backup (services stay stopped)
        4. Power off → resize → power on
        5. Wait for droplet active + SSH reachable
        6. Regenerate dbt profiles.yml
        7. Start services + verify health
        8. Persist new size to cloud.yml (if *project_root* provided)

    Args:
        client: ``DigitalOceanClient`` instance.
        ssh: Connected ``SSHManager`` (as root).
        droplet_id: Droplet ID to resize.
        new_size: Target size slug (e.g. ``"s-4vcpu-8gb"``).
        create_backup: If ``True``, create a backup before resizing.
        dbt_overrides: Optional ``DbtOverrides`` for profiles.yml.
        on_progress: Optional ``(step, status)`` callback.
        project_root: Local project root. When provided (along with
            *region*), the new size is persisted to ``cloud.yml``.
        region: Current region slug — required when *project_root* is set.

    Returns:
        ``ResizeResult`` with size change details.

    Raises:
        CloudError: If the size slug is invalid or action fails.
        CloudProvisioningError: If any SSH command fails.
    """
    from dango.platform.cloud.provisioning import (
        get_size_tier,
        save_provisioning_metadata,
        wait_for_droplet_ready,
        wait_for_ssh,
    )

    start_time = time.monotonic()
    warnings: list[str] = []

    # 1. Validate
    validate_size_slug(new_size)

    # 2. Get current droplet info
    droplet = client.get_droplet(droplet_id)
    current_size = droplet.get("size_slug", droplet.get("size", {}).get("slug", "unknown"))
    droplet_ip = _extract_ipv4(droplet)

    current_tier = get_size_tier(current_size)
    new_tier_obj = get_size_tier(new_size)

    # 3. Disk warning
    disk_warning = get_disk_warning(current_size, new_size)
    if disk_warning:
        warnings.append(disk_warning)

    # 4. Pre-resize backup (services stay stopped)
    backup_path: str | None = None
    if create_backup:
        from dango.platform.cloud.backup import create_backup as _create_backup

        _notify(on_progress, "backup", "running")
        backup_result = _create_backup(ssh, backup_type="pre-resize", restart_services=False)
        backup_path = backup_result.archive_path
        _notify(on_progress, "backup", "done")
    else:
        # Stop services manually if no backup (backup stops them)
        from dango.platform.cloud.backup import stop_services

        stop_services(ssh)

    try:
        # 5. Power off
        _notify(on_progress, "power_off", "running")
        action = client.power_off(droplet_id)
        client.wait_for_action(action["id"])
        _notify(on_progress, "power_off", "done")

        # 6. Resize
        _notify(on_progress, "resize", "running")
        action = client.resize(droplet_id, new_size)
        client.wait_for_action(action["id"])
        _notify(on_progress, "resize", "done")

        # 7. Power on
        _notify(on_progress, "power_on", "running")
        action = client.power_on(droplet_id)
        client.wait_for_action(action["id"])
        _notify(on_progress, "power_on", "done")

        # 8. Wait for droplet active + SSH
        _notify(on_progress, "wait_active", "running")
        wait_for_droplet_ready(client, droplet_id)
        _notify(on_progress, "wait_active", "done")

        _notify(on_progress, "wait_ssh", "running")
        wait_for_ssh(droplet_ip)
        _notify(on_progress, "wait_ssh", "done")
    except Exception:
        # Best effort: try to power on if we failed after power off
        try:
            client.power_on(droplet_id)
        except Exception:
            pass
        raise

    # 9. Reconnect SSH (connection was dropped during power cycle)
    ssh.disconnect()
    ssh.connect(droplet_ip, username="root")

    # 10. Regenerate dbt profiles
    _notify(on_progress, "dbt_profiles", "running")
    dbt_regenerated = regenerate_dbt_profiles(ssh, new_size, dbt_overrides)
    if not dbt_regenerated:
        warnings.append("dbt_project.yml not found on server — profiles.yml not regenerated.")
    _notify(on_progress, "dbt_profiles", "done")

    # 11. Start services + verify health
    from dango.platform.cloud.backup import start_services, verify_health

    _notify(on_progress, "start_services", "running")
    start_services(ssh)
    _notify(on_progress, "start_services", "done")

    _notify(on_progress, "verify_health", "running")
    health_ok = verify_health(ssh)
    if not health_ok:
        warnings.append("Health check did not pass within 90 seconds.")
    _notify(on_progress, "verify_health", "done")

    # 12. Persist new size to cloud.yml
    if project_root is not None and region is not None:
        save_provisioning_metadata(
            project_root,
            droplet_id=droplet_id,
            droplet_ip=droplet_ip,
            region=region,
            size=new_size,
        )

    return ResizeResult(
        old_size=current_size,
        new_size=new_size,
        old_tier=current_tier.name if current_tier else None,
        new_tier=new_tier_obj.name if new_tier_obj else None,
        duration_seconds=round(time.monotonic() - start_time, 1),
        backup_path=backup_path,
        dbt_profiles_regenerated=dbt_regenerated,
        warnings=warnings,
    )


def _extract_ipv4(droplet: dict[str, Any]) -> str:
    """Extract the public IPv4 address from a droplet dict."""
    for network in droplet.get("networks", {}).get("v4", []):
        if network.get("type") == "public":
            return str(network["ip_address"])
    raise CloudError(
        "Could not find public IPv4 address for droplet.",
        error_code="DANGO-D031",
    )
