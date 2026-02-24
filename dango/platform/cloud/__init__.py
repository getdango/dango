"""dango/platform/cloud/__init__.py

Cloud deployment platform components.

Populated by TASK-022+ (cloud provisioning, Caddy, remote sync).
"""

from .digitalocean import DigitalOceanClient
from .firewall import (
    add_allowed_ip,
    allow_all_web,
    create_default_firewall,
    delete_firewall,
    format_firewall_rules,
    get_firewall_rules,
    restrict_web_to_ips,
    save_firewall_metadata,
    validate_ip_or_cidr,
)
from .provisioning import (
    BUDGET_TIER,
    DEFAULT_TIER,
    PERFORMANCE_TIER,
    SIZE_TIERS,
    STANDARD_TIER,
    DropletSizeTier,
    RegionInfo,
    get_region_info,
    get_size_tier,
    list_regions,
    provision_droplet,
    save_provisioning_metadata,
    suggest_nearest_region,
    validate_custom_size,
    wait_for_droplet_ready,
    wait_for_ssh,
)
from .server_setup import SetupResult, setup_server
from .spaces import SpacesClient
from .ssh import CommandResult, SSHManager

__all__ = [
    "CommandResult",
    "DigitalOceanClient",
    "SpacesClient",
    "SSHManager",
    # Provisioning
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
    # Firewall
    "validate_ip_or_cidr",
    "create_default_firewall",
    "delete_firewall",
    "get_firewall_rules",
    "add_allowed_ip",
    "restrict_web_to_ips",
    "allow_all_web",
    "format_firewall_rules",
    "save_firewall_metadata",
    # Server setup
    "setup_server",
    "SetupResult",
]
