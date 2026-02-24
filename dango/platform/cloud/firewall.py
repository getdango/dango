"""dango/platform/cloud/firewall.py

Firewall lifecycle and IP allowlisting for Dango cloud deployments.

Manages DigitalOcean Cloud Firewalls: creation with default rules, IP-based
access restriction on ports 80/443, and reversion to public access.

Default inbound rules
---------------------
- SSH (22): open to all (0.0.0.0/0 and ::/0)
- HTTP (80): open to all
- HTTPS (443): open to all

Default outbound rules
----------------------
All TCP, UDP, and ICMP to all destinations.

IP allowlisting
---------------
``add_allowed_ip()`` uses a read-modify-write pattern:

- If the firewall currently allows ``0.0.0.0/0`` on 80/443, it switches to
  allowlist mode with just the new IP.
- If already in allowlist mode, it appends the new IP (deduplicating via set).

``restrict_web_to_ips()`` replaces the 80/443 rules entirely while preserving
SSH open-to-all and existing outbound rules.

Error codes
-----------
- DANGO-D020: Invalid IP address or CIDR notation
- DANGO-D021: Empty IP list for allowlisting
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from dango.exceptions import CloudError

__all__ = [
    "DEFAULT_INBOUND_RULES",
    "DEFAULT_OUTBOUND_RULES",
    "validate_ip_or_cidr",
    "create_default_firewall",
    "delete_firewall",
    "get_firewall_rules",
    "add_allowed_ip",
    "restrict_web_to_ips",
    "allow_all_web",
    "format_firewall_rules",
    "save_firewall_metadata",
]

# ---------------------------------------------------------------------------
# Default rule constants
# ---------------------------------------------------------------------------

_PUBLIC_SOURCES: dict[str, Any] = {"addresses": ["0.0.0.0/0", "::/0"]}
_ALL_DESTINATIONS: dict[str, Any] = {"addresses": ["0.0.0.0/0", "::/0"]}

DEFAULT_INBOUND_RULES: list[dict[str, Any]] = [
    {"protocol": "tcp", "ports": "22", "sources": _PUBLIC_SOURCES},
    {"protocol": "tcp", "ports": "80", "sources": _PUBLIC_SOURCES},
    {"protocol": "tcp", "ports": "443", "sources": _PUBLIC_SOURCES},
]

DEFAULT_OUTBOUND_RULES: list[dict[str, Any]] = [
    {"protocol": "tcp", "ports": "all", "destinations": _ALL_DESTINATIONS},
    {"protocol": "udp", "ports": "all", "destinations": _ALL_DESTINATIONS},
    {"protocol": "icmp", "destinations": _ALL_DESTINATIONS},
]

_WEB_PORTS = {"80", "443"}
_SSH_PORT = "22"


# ---------------------------------------------------------------------------
# IP / CIDR validation
# ---------------------------------------------------------------------------


def validate_ip_or_cidr(value: str) -> str:
    """Validate and normalise *value* as an IPv4 address or CIDR range.

    - Bare IPv4 addresses are normalised to ``/32`` (e.g. ``"1.2.3.4"``
      → ``"1.2.3.4/32"``).
    - CIDR ranges with host bits set are normalised to the network address
      (``strict=False``), e.g. ``"10.0.0.5/24"`` → ``"10.0.0.0/24"``.
    - IPv6 addresses are rejected (IPv4-only for v1).

    Args:
        value: IP address or CIDR string to validate.

    Returns:
        Normalised CIDR string.

    Raises:
        CloudError: DANGO-D020 if *value* is invalid or IPv6.
    """
    if not value or not value.strip():
        raise CloudError(
            "IP address or CIDR cannot be empty.",
            error_code="DANGO-D020",
        )

    stripped = value.strip()

    # Reject IPv6
    if ":" in stripped:
        raise CloudError(
            f"IPv6 addresses are not supported for allowlisting (got '{stripped}'). "
            "Use an IPv4 address or CIDR range.",
            error_code="DANGO-D020",
        )

    try:
        if "/" in stripped:
            network = ipaddress.IPv4Network(stripped, strict=False)
            return str(network)
        else:
            # Bare IP — validate then append /32
            ipaddress.IPv4Address(stripped)
            return f"{stripped}/32"
    except ValueError as exc:
        raise CloudError(
            f"Invalid IP address or CIDR notation: '{stripped}'. {exc}",
            error_code="DANGO-D020",
        ) from exc


# ---------------------------------------------------------------------------
# Firewall lifecycle
# ---------------------------------------------------------------------------


def create_default_firewall(
    client: Any,
    droplet_id: int,
) -> dict[str, Any]:
    """Create a Dango firewall with default rules and apply it to *droplet_id*.

    The firewall name is ``dango-fw-{droplet_id}``.

    Args:
        client: ``DigitalOceanClient`` instance.
        droplet_id: Droplet ID to associate with the new firewall.

    Returns:
        The firewall dict returned by the DO API.
    """
    name = f"dango-fw-{droplet_id}"
    return client.create_firewall(  # type: ignore[no-any-return]
        name=name,
        inbound_rules=DEFAULT_INBOUND_RULES,
        outbound_rules=DEFAULT_OUTBOUND_RULES,
        droplet_ids=[droplet_id],
    )


def delete_firewall(client: Any, firewall_id: str) -> None:
    """Delete a firewall by ID.

    Args:
        client: ``DigitalOceanClient`` instance.
        firewall_id: UUID of the firewall to delete.
    """
    client.delete_firewall(firewall_id)


def get_firewall_rules(client: Any, firewall_id: str) -> dict[str, Any]:
    """Return the full firewall dict for *firewall_id*.

    Args:
        client: ``DigitalOceanClient`` instance.
        firewall_id: UUID of the firewall to retrieve.

    Returns:
        The firewall dict from the DO API.
    """
    return client.get_firewall(firewall_id)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# IP allowlisting helpers
# ---------------------------------------------------------------------------


def _is_public_sources(sources: dict[str, Any]) -> bool:
    """Return True if *sources* includes the open-to-all ``0.0.0.0/0`` address."""
    addresses: list[str] = sources.get("addresses", [])
    return "0.0.0.0/0" in addresses


def _build_web_inbound_rule(port: str, cidr_list: list[str]) -> dict[str, Any]:
    """Build a single inbound rule for *port* restricted to *cidr_list*."""
    return {
        "protocol": "tcp",
        "ports": port,
        "sources": {"addresses": cidr_list},
    }


def add_allowed_ip(client: Any, firewall_id: str, ip_cidr: str) -> dict[str, Any]:
    """Add *ip_cidr* to the 80/443 allowlist, switching from public mode if needed.

    Behaviour:
    - If the firewall currently allows ``0.0.0.0/0`` on both 80 and 443 (public
      mode), switches to allowlist mode with *ip_cidr* as the sole source.
    - If already in allowlist mode, appends *ip_cidr* (deduplicates via set).
    - SSH (port 22) is always left unchanged.

    Args:
        client: ``DigitalOceanClient`` instance.
        firewall_id: UUID of the firewall to modify.
        ip_cidr: IPv4 address or CIDR to allow (will be validated/normalised).

    Returns:
        The updated firewall dict.
    """
    normalised = validate_ip_or_cidr(ip_cidr)
    fw = client.get_firewall(firewall_id)

    existing_inbound: list[dict[str, Any]] = fw.get("inbound_rules", [])
    existing_outbound: list[dict[str, Any]] = fw.get("outbound_rules", [])
    droplet_ids: list[int] = fw.get("droplet_ids", [])
    tags: list[str] = fw.get("tags", [])
    name: str = fw.get("name", f"dango-fw-{firewall_id}")

    # Separate SSH rule from web rules; preserve any other custom inbound rules
    ssh_rules = [r for r in existing_inbound if r.get("ports") == _SSH_PORT]
    web_rules = {r["ports"]: r for r in existing_inbound if r.get("ports") in _WEB_PORTS}
    other_rules = [r for r in existing_inbound if r.get("ports") not in (_SSH_PORT, *_WEB_PORTS)]

    # Determine if currently in public mode (any web rule has 0.0.0.0/0)
    public_mode = any(_is_public_sources(rule.get("sources", {})) for rule in web_rules.values())

    if public_mode:
        new_cidrs = [normalised]
    else:
        # Collect all existing IPs across 80/443 rules, then add new one
        existing_cidrs: set[str] = set()
        for rule in web_rules.values():
            existing_cidrs.update(rule.get("sources", {}).get("addresses", []))
        existing_cidrs.add(normalised)
        new_cidrs = sorted(existing_cidrs)

    new_web_rules = [_build_web_inbound_rule(port, new_cidrs) for port in sorted(_WEB_PORTS)]
    new_inbound = ssh_rules + other_rules + new_web_rules

    return client.update_firewall(  # type: ignore[no-any-return]
        firewall_id=firewall_id,
        name=name,
        inbound_rules=new_inbound,
        outbound_rules=existing_outbound,
        droplet_ids=droplet_ids,
        tags=tags,
    )


def restrict_web_to_ips(
    client: Any,
    firewall_id: str,
    ip_cidrs: list[str],
) -> dict[str, Any]:
    """Replace 80/443 inbound rules with the given IP list.

    SSH (port 22) is left open to all.  Existing outbound rules and droplet
    associations are preserved.

    Args:
        client: ``DigitalOceanClient`` instance.
        firewall_id: UUID of the firewall to modify.
        ip_cidrs: Non-empty list of IPv4 addresses or CIDRs to allow.

    Returns:
        The updated firewall dict.

    Raises:
        CloudError: DANGO-D021 if *ip_cidrs* is empty.
    """
    if not ip_cidrs:
        raise CloudError(
            "ip_cidrs must not be empty when restricting web access.",
            error_code="DANGO-D021",
        )

    normalised = [validate_ip_or_cidr(ip) for ip in ip_cidrs]

    fw = client.get_firewall(firewall_id)
    existing_inbound: list[dict[str, Any]] = fw.get("inbound_rules", [])
    existing_outbound: list[dict[str, Any]] = fw.get("outbound_rules", [])
    droplet_ids: list[int] = fw.get("droplet_ids", [])
    tags: list[str] = fw.get("tags", [])
    name: str = fw.get("name", f"dango-fw-{firewall_id}")

    ssh_rules = [r for r in existing_inbound if r.get("ports") == _SSH_PORT]
    other_rules = [r for r in existing_inbound if r.get("ports") not in (_SSH_PORT, *_WEB_PORTS)]
    new_web_rules = [_build_web_inbound_rule(port, normalised) for port in sorted(_WEB_PORTS)]
    new_inbound = ssh_rules + other_rules + new_web_rules

    return client.update_firewall(  # type: ignore[no-any-return]
        firewall_id=firewall_id,
        name=name,
        inbound_rules=new_inbound,
        outbound_rules=existing_outbound,
        droplet_ids=droplet_ids,
        tags=tags,
    )


def allow_all_web(client: Any, firewall_id: str) -> dict[str, Any]:
    """Revert 80/443 inbound rules to allow all traffic.

    SSH (port 22) and outbound rules are left unchanged.

    Args:
        client: ``DigitalOceanClient`` instance.
        firewall_id: UUID of the firewall to modify.

    Returns:
        The updated firewall dict.
    """
    fw = client.get_firewall(firewall_id)
    existing_inbound: list[dict[str, Any]] = fw.get("inbound_rules", [])
    existing_outbound: list[dict[str, Any]] = fw.get("outbound_rules", [])
    droplet_ids: list[int] = fw.get("droplet_ids", [])
    tags: list[str] = fw.get("tags", [])
    name: str = fw.get("name", f"dango-fw-{firewall_id}")

    ssh_rules = [r for r in existing_inbound if r.get("ports") == _SSH_PORT]
    other_rules = [r for r in existing_inbound if r.get("ports") not in (_SSH_PORT, *_WEB_PORTS)]
    new_web_rules = [
        _build_web_inbound_rule(port, list(_PUBLIC_SOURCES["addresses"]))
        for port in sorted(_WEB_PORTS)
    ]
    new_inbound = ssh_rules + other_rules + new_web_rules

    return client.update_firewall(  # type: ignore[no-any-return]
        firewall_id=firewall_id,
        name=name,
        inbound_rules=new_inbound,
        outbound_rules=existing_outbound,
        droplet_ids=droplet_ids,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Display helper
# ---------------------------------------------------------------------------


def format_firewall_rules(firewall: dict[str, Any]) -> list[dict[str, str]]:
    """Format firewall inbound and outbound rules for Rich table display.

    Args:
        firewall: Firewall dict from the DO API.

    Returns:
        List of dicts with keys ``direction``, ``protocol``, ``ports``,
        ``sources_or_destinations``.
    """
    rows: list[dict[str, str]] = []

    for rule in firewall.get("inbound_rules", []):
        sources = rule.get("sources", {})
        addresses = sources.get("addresses", [])
        rows.append(
            {
                "direction": "inbound",
                "protocol": rule.get("protocol", ""),
                "ports": rule.get("ports", "all"),
                "sources_or_destinations": ", ".join(addresses) if addresses else "all",
            }
        )

    for rule in firewall.get("outbound_rules", []):
        destinations = rule.get("destinations", {})
        addresses = destinations.get("addresses", [])
        rows.append(
            {
                "direction": "outbound",
                "protocol": rule.get("protocol", ""),
                "ports": rule.get("ports", "all"),
                "sources_or_destinations": ", ".join(addresses) if addresses else "all",
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def save_firewall_metadata(project_root: Path, firewall_id: str) -> None:
    """Persist the firewall ID to ``.dango/cloud.yml``.

    Loads the existing ``CloudConfig`` (or creates a new one), sets
    ``firewall_id``, and saves.

    Args:
        project_root: Project root directory (contains ``.dango/``).
        firewall_id: DO firewall UUID to persist.
    """
    from dango.config.loader import ConfigLoader
    from dango.config.models import CloudConfig

    loader = ConfigLoader(project_root)
    existing = loader.load_cloud_config() or CloudConfig()

    updated = existing.model_copy(update={"firewall_id": firewall_id})
    loader.save_cloud_config(updated)
