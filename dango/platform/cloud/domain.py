"""dango/platform/cloud/domain.py

DNS validation and domain management for cloud deployments.

Provides ``set_domain()`` and ``remove_domain()`` to configure HTTPS
via Caddy's automatic Let's Encrypt integration, and ``check_dns()``
for pre-flight DNS propagation checks.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud._server_templates import build_caddyfile

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager


def check_dns(domain: str, expected_ip: str) -> tuple[bool, str]:
    """Check whether *domain* resolves to *expected_ip*.

    Uses ``socket.getaddrinfo()`` for A-record lookup.  This is a
    best-effort check — Caddy retries certificate acquisition
    automatically, so a temporary DNS mismatch is not fatal.

    Returns:
        ``(matches, message)`` — *matches* is ``True`` when at least one
        resolved address equals *expected_ip*.
    """
    try:
        results = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        resolved_ips: list[str] = sorted({str(r[4][0]) for r in results})
    except socket.gaierror as exc:
        return (False, f"DNS lookup failed for {domain}: {exc}")

    if not resolved_ips:
        return (False, f"No A records found for {domain}")

    if expected_ip in resolved_ips:
        return (True, f"{domain} resolves to {expected_ip}")

    return (
        False,
        f"{domain} resolves to {', '.join(resolved_ips)} "
        f"(expected {expected_ip}). DNS may still be propagating.",
    )


def _write_caddyfile(ssh: SSHManager, content: str) -> bool:
    """Write Caddyfile and reload Caddy if changed. Returns whether changed."""
    check = ssh.exec_command("cat /etc/caddy/Caddyfile 2>/dev/null")
    if check.success and check.stdout == content:
        return False
    ssh.write_remote_file("/etc/caddy/Caddyfile", content, mode=0o644)
    result = ssh.exec_command("systemctl reload-or-restart caddy")
    if not result.success:
        raise CloudProvisioningError(
            f"Failed to reload Caddy: {result.stderr.strip() or result.stdout.strip()}"
        )
    return True


def set_domain(
    ssh: SSHManager,
    project_root: Path,
    domain: str,
) -> dict[str, Any]:
    """Configure HTTPS for *domain* on the remote server.

    1. Loads cloud.yml to get ``droplet_ip``
    2. Runs a DNS pre-flight check (warning only)
    3. Writes an HTTPS Caddyfile and reloads Caddy
    4. Saves the domain to cloud.yml

    Returns:
        Dict with ``domain``, ``dns_ok``, ``dns_message``, ``caddyfile_updated``.
    """
    from dango.config.loader import ConfigLoader

    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()
    if cloud_cfg is None or cloud_cfg.droplet_ip is None:
        raise CloudProvisioningError("No droplet IP found in cloud.yml")

    dns_ok, dns_message = check_dns(domain, cloud_cfg.droplet_ip)

    content = build_caddyfile(domain)
    caddyfile_updated = _write_caddyfile(ssh, content)

    cloud_cfg.domain = domain
    loader.save_cloud_config(cloud_cfg)

    return {
        "domain": domain,
        "dns_ok": dns_ok,
        "dns_message": dns_message,
        "caddyfile_updated": caddyfile_updated,
    }


def remove_domain(
    ssh: SSHManager,
    project_root: Path,
) -> dict[str, Any]:
    """Revert to HTTP-only access (remove domain configuration).

    Writes an HTTP-only Caddyfile on port 80, reloads Caddy, and clears
    the ``domain`` field from cloud.yml.

    Returns:
        Dict with ``previous_domain`` and ``caddyfile_updated``.
    """
    from dango.config.loader import ConfigLoader

    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()
    if cloud_cfg is None:
        raise CloudProvisioningError("No cloud configuration found in cloud.yml")

    previous_domain = cloud_cfg.domain

    content = build_caddyfile(None)
    caddyfile_updated = _write_caddyfile(ssh, content)

    cloud_cfg.domain = None
    loader.save_cloud_config(cloud_cfg)

    return {
        "previous_domain": previous_domain,
        "caddyfile_updated": caddyfile_updated,
    }
