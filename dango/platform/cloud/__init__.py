"""dango/platform/cloud/__init__.py

Cloud deployment platform components.

Populated by TASK-022+ (cloud provisioning, Caddy, remote sync).
"""

from ._server_templates import build_caddyfile
from .backup import (
    BackupManifest,
    BackupResult,
    RestoreResult,
    create_backup,
    list_local_backups,
    rollback,
    rotate_local_backups,
    start_services,
    stop_services,
    verify_health,
)
from .deployer import DeployLock, DeployResult, push_deploy
from .digitalocean import DigitalOceanClient
from .domain import check_dns, remove_domain, set_domain
from .file_sync import SyncResult, sync_project_files
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
from .migrate import MigrateResult, migrate_server
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
from .resize import ResizeResult, resize_droplet, validate_size_slug
from .server_setup import SetupResult, setup_server
from .server_status import (
    ServerStatus,
    ServiceInfo,
    check_latest_pypi_version,
    collect_server_status,
    get_local_resource_usage,
)
from .spaces import SpacesClient
from .ssh import CommandResult, SSHManager
from .upgrade import UpgradeResult, upgrade_dango, validate_version_string

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
    # Server status
    "ServerStatus",
    "ServiceInfo",
    "collect_server_status",
    "check_latest_pypi_version",
    "get_local_resource_usage",
    # Domain management (TASK-027)
    "build_caddyfile",
    "check_dns",
    "set_domain",
    "remove_domain",
    # Backup & rollback
    "BackupManifest",
    "BackupResult",
    "RestoreResult",
    "create_backup",
    "list_local_backups",
    "rollback",
    "rotate_local_backups",
    "stop_services",
    "start_services",
    "verify_health",
    # File sync (TASK-028)
    "SyncResult",
    "sync_project_files",
    # Deploy (TASK-030)
    "DeployLock",
    "DeployResult",
    "push_deploy",
    # Resize (TASK-104)
    "ResizeResult",
    "resize_droplet",
    "validate_size_slug",
    # Migrate (TASK-105)
    "MigrateResult",
    "migrate_server",
    # Upgrade (TASK-106)
    "UpgradeResult",
    "upgrade_dango",
    "validate_version_string",
]
