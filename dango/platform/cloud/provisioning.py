"""dango/platform/cloud/provisioning.py

Droplet provisioning orchestration for Dango cloud deployments.

Provides size tier selection, region recommendations, and the full
``provision_droplet()`` workflow: create → poll until active → SSH check →
cleanup on failure.

Size tiers
----------
Three pre-defined tiers cover most use cases:

- Budget (s-1vcpu-2gb, $12/mo) — Metabase may be slow
- Standard (s-2vcpu-4gb, $24/mo) — recommended default
- Performance (s-4vcpu-8gb, $48/mo) — for heavier workloads

Region selection
----------------
``suggest_nearest_region()`` uses the local UTC offset (from ``time.timezone``)
to recommend the geographically closest DO region without making any network
calls.

Error codes
-----------
- DANGO-D010: Droplet entered errored or archived state
- DANGO-D011: Droplet did not become active within timeout
- DANGO-D012: SSH not available after maximum attempts
- DANGO-D013: Droplet is active but has no public IPv4 address
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dango.exceptions import CloudError

__all__ = [
    "DropletSizeTier",
    "RegionInfo",
    "SIZE_TIERS",
    "DEFAULT_TIER",
    "BUDGET_TIER",
    "STANDARD_TIER",
    "PERFORMANCE_TIER",
    "get_size_tier",
    "validate_custom_size",
    "get_region_info",
    "list_regions",
    "suggest_nearest_region",
    "provision_droplet",
    "save_provisioning_metadata",
    "wait_for_droplet_ready",
    "wait_for_ssh",
]

_DANGO_IMAGE = "ubuntu-22-04-x64"
_DANGO_TAG = "dango"

# ---------------------------------------------------------------------------
# Size tiers
# ---------------------------------------------------------------------------

_ERRORED_STATES = {"errored", "archive"}


@dataclass(frozen=True)
class DropletSizeTier:
    """Describes a pre-defined Droplet size option.

    Attributes:
        name: Human-readable tier name (e.g. ``"Standard"``).
        slug: DO size slug passed to the API (e.g. ``"s-2vcpu-4gb"``).
        vcpus: Number of virtual CPUs.
        ram_gb: RAM in gigabytes.
        disk_gb: SSD disk in gigabytes.
        price_monthly: Monthly cost in USD.
        warning: Optional caution note shown to the user (e.g. performance caveats).
    """

    name: str
    slug: str
    vcpus: int
    ram_gb: int
    disk_gb: int
    price_monthly: int
    warning: str | None = None


BUDGET_TIER = DropletSizeTier(
    name="Budget",
    slug="s-1vcpu-2gb",
    vcpus=1,
    ram_gb=2,
    disk_gb=50,
    price_monthly=12,
    warning="Metabase may be slow on this tier.",
)

STANDARD_TIER = DropletSizeTier(
    name="Standard",
    slug="s-2vcpu-4gb",
    vcpus=2,
    ram_gb=4,
    disk_gb=80,
    price_monthly=24,
)

PERFORMANCE_TIER = DropletSizeTier(
    name="Performance",
    slug="s-4vcpu-8gb",
    vcpus=4,
    ram_gb=8,
    disk_gb=160,
    price_monthly=48,
)

SIZE_TIERS: list[DropletSizeTier] = [STANDARD_TIER, PERFORMANCE_TIER]
DEFAULT_TIER: DropletSizeTier = STANDARD_TIER

_TIER_BY_SLUG: dict[str, DropletSizeTier] = {t.slug: t for t in SIZE_TIERS}


def get_size_tier(slug: str) -> DropletSizeTier | None:
    """Return the ``DropletSizeTier`` for the given slug, or ``None`` if not found."""
    return _TIER_BY_SLUG.get(slug)


def validate_custom_size(slug: str) -> bool:
    """Return ``True`` if *slug* looks like a valid DO size slug.

    Accepts any non-empty string that starts with ``"s-"`` or ``"g-"`` or
    ``"so"``-prefix (General Purpose, CPU-Optimized, etc.).  This is a
    lightweight heuristic — the DO API is the authoritative validator.
    """
    return bool(slug) and slug.startswith(("s-", "g-", "so", "c-", "m-"))


# ---------------------------------------------------------------------------
# Region data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionInfo:
    """Metadata for a DigitalOcean region.

    Attributes:
        slug: DO region slug (e.g. ``"nyc1"``).
        name: Full region name (e.g. ``"New York 1"``).
        city: City name.
        country: ISO country name or abbreviation.
        utc_offset: UTC offset in hours (e.g. ``-5`` for EST).
        gdpr: ``True`` if this region is in the EU and subject to GDPR.
    """

    slug: str
    name: str
    city: str
    country: str
    utc_offset: float
    gdpr: bool = False


_REGIONS: list[RegionInfo] = [
    RegionInfo("nyc1", "New York 1", "New York", "US", -5.0),
    RegionInfo("nyc3", "New York 3", "New York", "US", -5.0),
    RegionInfo("sfo3", "San Francisco 3", "San Francisco", "US", -8.0),
    RegionInfo("ams3", "Amsterdam 3", "Amsterdam", "NL", 1.0, gdpr=True),
    RegionInfo("sgp1", "Singapore 1", "Singapore", "SG", 8.0),
    RegionInfo("lon1", "London 1", "London", "GB", 0.0),
    RegionInfo("fra1", "Frankfurt 1", "Frankfurt", "DE", 1.0, gdpr=True),
    RegionInfo("tor1", "Toronto 1", "Toronto", "CA", -5.0),
    RegionInfo("blr1", "Bangalore 1", "Bangalore", "IN", 5.5),
    RegionInfo("syd1", "Sydney 1", "Sydney", "AU", 10.0),
]

_REGION_BY_SLUG: dict[str, RegionInfo] = {r.slug: r for r in _REGIONS}


def get_region_info(slug: str) -> RegionInfo | None:
    """Return the ``RegionInfo`` for the given slug, or ``None`` if not found."""
    return _REGION_BY_SLUG.get(slug)


def list_regions() -> list[RegionInfo]:
    """Return all known regions."""
    return list(_REGIONS)


def suggest_nearest_region() -> RegionInfo:
    """Suggest the geographically closest DO region based on local UTC offset.

    Uses ``time.timezone`` / ``time.altzone`` to determine the local UTC offset
    without making any network requests.  Picks the region whose ``utc_offset``
    has the smallest absolute difference from the local offset.

    Returns:
        The closest ``RegionInfo`` — defaults to ``nyc1`` on any error.
    """
    try:
        # time.timezone is seconds *west* of UTC (sign is opposite of UTC offset).
        # Use tm_isdst > 0 to check if DST is *currently active* (not just observed).
        if time.localtime().tm_isdst > 0:
            local_offset_seconds = -time.altzone
        else:
            local_offset_seconds = -time.timezone
        local_offset_hours = local_offset_seconds / 3600.0
    except Exception:
        return _REGION_BY_SLUG["nyc1"]

    best = min(_REGIONS, key=lambda r: abs(r.utc_offset - local_offset_hours))
    return best


# ---------------------------------------------------------------------------
# Provisioning helpers
# ---------------------------------------------------------------------------


def _extract_public_ipv4(droplet: dict[str, Any]) -> str | None:
    """Extract the public IPv4 address from a droplet dict.

    Returns:
        IP address string if found, else ``None``.
    """
    networks = droplet.get("networks", {})
    v4_list = networks.get("v4", [])
    for network in v4_list:
        if network.get("type") == "public":
            ip = network.get("ip_address")
            return str(ip) if ip else None
    return None


def wait_for_droplet_ready(
    client: Any,
    droplet_id: int,
    *,
    poll_interval: float = 5.0,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Poll the DO API until the droplet reaches ``active`` status.

    Args:
        client: ``DigitalOceanClient`` instance.
        droplet_id: ID of the droplet to poll.
        poll_interval: Seconds between polls. Default: 5.
        timeout: Maximum total seconds to wait. Default: 120.

    Returns:
        The active droplet dict.

    Raises:
        CloudError: DANGO-D010 if droplet enters an errored/archived state.
        CloudError: DANGO-D011 if the timeout elapses before the droplet is active.
    """
    deadline = time.monotonic() + timeout

    while True:
        droplet = client.get_droplet(droplet_id)
        status = droplet.get("status", "")

        if status == "active":
            return droplet  # type: ignore[no-any-return]

        if status in _ERRORED_STATES:
            raise CloudError(
                f"Droplet {droplet_id} entered state '{status}' during provisioning.",
                error_code="DANGO-D010",
            )

        if time.monotonic() >= deadline:
            raise CloudError(
                f"Droplet {droplet_id} did not become active within {timeout:.0f}s "
                f"(current status: '{status}').",
                error_code="DANGO-D011",
            )

        time.sleep(poll_interval)


def wait_for_ssh(
    ip_address: str,
    *,
    port: int = 22,
    max_attempts: int = 12,
    attempt_interval: float = 5.0,
) -> None:
    """Wait for SSH to become reachable on *ip_address*.

    Opens a TCP socket connection on *port* to check reachability.  Does not
    perform any SSH handshake or authentication.

    Args:
        ip_address: Target IP address.
        port: SSH port. Default: 22.
        max_attempts: Maximum connection attempts. Default: 12.
        attempt_interval: Seconds between attempts. Default: 5.

    Raises:
        CloudError: DANGO-D012 if the port does not become reachable.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            with socket.create_connection((ip_address, port), timeout=5.0):
                return  # success
        except OSError as exc:
            if attempt == max_attempts:
                raise CloudError(
                    f"SSH port {port} on {ip_address} did not become reachable "
                    f"after {max_attempts} attempts ({max_attempts * attempt_interval:.0f}s).",
                    error_code="DANGO-D012",
                ) from exc
            time.sleep(attempt_interval)


def provision_droplet(
    client: Any,
    name: str,
    region: str,
    size: str,
    ssh_key_ids: list[int],
    *,
    user_data: str | None = None,
    extra_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Provision a new Droplet and wait for it to be fully reachable.

    Workflow:
    1. Create the droplet (always adds the ``"dango"`` tag, pins Ubuntu 22.04).
    2. Poll until ``status == "active"`` (up to 120s).
    3. Extract the public IPv4 address.
    4. Verify SSH reachability (up to 12 × 5s attempts).

    If any step fails after the droplet is created, a best-effort cleanup
    (``delete_droplet``) is attempted before re-raising the original error.

    Args:
        client: ``DigitalOceanClient`` instance.
        name: Hostname for the new droplet.
        region: DO region slug (e.g. ``"nyc1"``).
        size: Droplet size slug (e.g. ``"s-2vcpu-4gb"``).
        ssh_key_ids: List of SSH key IDs to install.
        user_data: Cloud-init script (optional).
        extra_tags: Additional tags beyond ``"dango"`` (optional).

    Returns:
        The active droplet dict (as returned by the DO API).

    Raises:
        CloudError: DANGO-D010/D011/D012/D013 on provisioning failure.
        CloudAPIError / CloudAuthError: On DO API errors.
    """
    tags = [_DANGO_TAG] + (extra_tags or [])

    droplet = client.create_droplet(
        name=name,
        region=region,
        size=size,
        image=_DANGO_IMAGE,
        ssh_key_ids=ssh_key_ids,
        tags=tags,
        user_data=user_data,
    )
    droplet_id: int = droplet["id"]

    try:
        active_droplet = wait_for_droplet_ready(client, droplet_id)

        ip_address = _extract_public_ipv4(active_droplet)
        if ip_address is None:
            raise CloudError(
                f"Droplet {droplet_id} is active but has no public IPv4 address.",
                error_code="DANGO-D013",
            )

        wait_for_ssh(ip_address)

    except Exception:
        # Best-effort cleanup — swallow delete errors, re-raise original
        try:
            client.delete_droplet(droplet_id)
        except Exception:
            pass
        raise

    return active_droplet  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def save_provisioning_metadata(
    project_root: Path,
    droplet_id: int,
    droplet_ip: str,
    region: str,
    size: str,
) -> None:
    """Persist droplet provisioning metadata to ``.dango/cloud.yml``.

    Loads the existing ``CloudConfig`` (or creates a new one) and updates
    ``droplet_id``, ``droplet_ip``, ``region``, and ``size``, then saves.

    Args:
        project_root: Project root directory (contains ``.dango/``).
        droplet_id: Numeric DO droplet ID.
        droplet_ip: Public IPv4 address of the droplet.
        region: DO region slug used during provisioning.
        size: Droplet size slug used during provisioning.
    """
    from dango.config.loader import ConfigLoader
    from dango.config.models import CloudConfig

    loader = ConfigLoader(project_root)
    existing = loader.load_cloud_config() or CloudConfig()

    updated = existing.model_copy(
        update={
            "droplet_id": droplet_id,
            "droplet_ip": droplet_ip,
            "region": region,
            "size": size,
        }
    )
    loader.save_cloud_config(updated)
