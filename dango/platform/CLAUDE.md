# platform/

## Purpose

Docker service lifecycle, file watching, and platform startup helpers. Shared between local (`dango start`) and cloud (`dango serve`, TASK-026) startup flows.

## Directory Layout

```
platform/
├── __init__.py          # Re-exports DockerManager, ServiceStatus (unchanged)
├── __main__.py          # python -m dango.platform → runs watcher_runner
├── docker.py            # DockerManager, ServiceStatus (shared local + cloud)
├── CLAUDE.md            # This file
│
├── common/              # Shared startup logic (local + cloud reuse)
│   ├── __init__.py
│   └── startup.py       # run_pending_migrations, ensure_dbt_schemas,
│                        # ensure_duckdb_driver, start_docker_services,
│                        # setup_metabase_if_needed, import_dashboards
│
├── local/               # Local-only components (nginx, file watcher)
│   ├── __init__.py
│   ├── network.py       # NetworkConfig, NginxManager, HostsManager
│   ├── watcher.py       # DebouncedFileHandler, FileWatcher, MultiTargetWatcher
│   ├── watcher_lifecycle.py  # start/stop/status for watcher subprocess
│   └── watcher_runner.py    # Background watcher process entry point
│
├── cloud/               # Cloud-only components (TASK-022+)
│   ├── __init__.py      # Exports CommandResult, DigitalOceanClient, SpacesClient, SSHManager, provisioning, firewall symbols
│   ├── digitalocean.py  # DO REST API v2 client (Droplets, SSH Keys, Firewalls)
│   ├── provisioning.py  # Size tiers, regions, provision_droplet() orchestration (TASK-023)
│   ├── firewall.py      # Firewall lifecycle, IP allowlisting (TASK-025)
│   ├── spaces.py        # DO Spaces client (S3-compatible via boto3)
│   ├── ssh.py           # SSH key management, TOFU known-hosts, exec/SFTP (TASK-024)
│   ├── server_setup.py  # SSH-based server setup orchestration (TASK-026)
│   ├── domain.py        # DNS check, set_domain(), remove_domain() (TASK-027)
│   ├── backup.py        # SSH-based backup + rollback (TASK-035)
│   ├── file_sync.py     # Project file sync: SFTP + rsync (TASK-028)
│   ├── deployer.py      # Push deployment workflow + deploy lock (TASK-030)
│   ├── scheduled_backup.py  # Server-side scheduled backup (TASK-103)
│   └── _server_templates.py  # Config file templates (incl. build_caddyfile(), backup timer)
│
│   # Backwards-compatible shims (re-export from local/)
├── network.py           # → platform.local.network
├── watcher.py           # → platform.local.watcher
├── watcher_lifecycle.py # → platform.local.watcher_lifecycle
└── watcher_runner.py    # → platform.local.watcher_runner
```

## File Purpose

| File | Purpose | Key Symbols |
|------|---------|-------------|
| `docker.py` | Docker Compose lifecycle | `DockerManager`, `ServiceStatus` |
| `common/startup.py` | Shared startup helpers | `run_pending_migrations`, `ensure_dbt_schemas`, `ensure_duckdb_driver`, `start_docker_services`, `setup_metabase_if_needed`, `import_dashboards` |
| `local/network.py` | Shared nginx routing (local dev) | `NetworkConfig`, `NginxManager`, `HostsManager` |
| `local/watcher.py` | File change detection | `DebouncedFileHandler`, `FileWatcher`, `MultiTargetWatcher`, `SyncTrigger` |
| `local/watcher_lifecycle.py` | Watcher subprocess lifecycle | `start_file_watcher`, `stop_file_watcher`, `get_watcher_status`, `get_watcher_pid_file_path` |
| `local/watcher_runner.py` | Background watcher process | `main` |
| `cloud/digitalocean.py` | DigitalOcean REST API v2 client | `DigitalOceanClient` |
| `cloud/provisioning.py` | Droplet size tiers, regions, provisioning orchestration | `DropletSizeTier`, `RegionInfo`, `SIZE_TIERS`, `DEFAULT_TIER`, `provision_droplet`, `wait_for_droplet_ready`, `wait_for_ssh`, `suggest_nearest_region`, `save_provisioning_metadata` |
| `cloud/firewall.py` | Firewall lifecycle and IP allowlisting | `create_default_firewall`, `add_allowed_ip`, `restrict_web_to_ips`, `allow_all_web`, `validate_ip_or_cidr`, `save_firewall_metadata` |
| `cloud/spaces.py` | DigitalOcean Spaces (S3-compatible) client | `SpacesClient` |
| `cloud/ssh.py` | SSH key management, TOFU known-hosts, command exec, SFTP | `SSHManager`, `CommandResult` |
| `cloud/server_setup.py` | SSH-based server setup orchestration (16 idempotent steps) | `setup_server`, `SetupResult` |
| `cloud/domain.py` | DNS check, domain set/remove for HTTPS via Caddy | `check_dns`, `set_domain`, `remove_domain` |
| `cloud/backup.py` | SSH-based backup and rollback | `create_backup`, `rollback`, `list_local_backups`, `rotate_local_backups`, `BackupManifest`, `BackupResult`, `RestoreResult` |
| `cloud/file_sync.py` | Project file sync (SFTP + rsync) with change detection | `sync_project_files`, `SyncResult` |
| `cloud/deployer.py` | Push deployment workflow with deploy lock | `push_deploy`, `DeployLock`, `DeployResult` |
| `cloud/scheduled_backup.py` | Server-side scheduled backup to Spaces | `run_scheduled_backup`, `list_spaces_backups`, `restore_from_spaces`, `enable_scheduled_backup`, `disable_scheduled_backup` |
| `cloud/_server_templates.py` | Config file templates (systemd, Caddy, fail2ban, backup timer, etc.) | `build_caddyfile`, `SYSTEMD_UNIT`, `CADDYFILE`, `SYSTEMD_BACKUP_SERVICE`, `SYSTEMD_BACKUP_TIMER`, etc. |

## Import Patterns

### Preferred (canonical):
```python
# Shared infrastructure
from dango.platform import DockerManager, ServiceStatus
from dango.platform.common.startup import run_pending_migrations, start_docker_services

# Local-only components
from dango.platform.local.watcher_lifecycle import start_file_watcher, get_watcher_status
from dango.platform.local.network import NetworkConfig

# Cloud config (in config module)
from dango.config import CloudConfig

# Cloud clients (TASK-022+)
from dango.platform.cloud import DigitalOceanClient, SpacesClient

# SSH management (TASK-024)
from dango.platform.cloud import SSHManager, CommandResult
from dango.platform.cloud.ssh import SSHManager, CommandResult  # also valid

# Provisioning (TASK-023)
from dango.platform.cloud import provision_droplet, suggest_nearest_region, SIZE_TIERS

# Firewall management (TASK-025)
from dango.platform.cloud import create_default_firewall, add_allowed_ip, restrict_web_to_ips

# Domain management (TASK-027)
from dango.platform.cloud import check_dns, set_domain, remove_domain, build_caddyfile

# Backup & rollback (TASK-035)
from dango.platform.cloud import create_backup, rollback, list_local_backups
from dango.platform.cloud.backup import BackupManifest, BackupResult, RestoreResult

# File sync (TASK-028)
from dango.platform.cloud import SyncResult, sync_project_files
from dango.platform.cloud.file_sync import SyncResult, sync_project_files  # also valid

# Push deploy (TASK-030)
from dango.platform.cloud import DeployLock, DeployResult, push_deploy
from dango.platform.cloud.deployer import DeployLock, DeployResult, push_deploy  # also valid

# Scheduled backup (TASK-103, runs on server)
from dango.platform.cloud.scheduled_backup import run_scheduled_backup, list_spaces_backups
```

### Also valid (backwards-compatible shims):
```python
# These still work — shims re-export from local/
from dango.platform.network import NetworkConfig
from dango.platform.watcher_lifecycle import get_watcher_status
```

### Test patches MUST use canonical paths:
```python
# CORRECT — patches the actual function
@patch("dango.platform.local.watcher_lifecycle.is_process_running")

# WRONG — patches the shim's re-export, not the real function
@patch("dango.platform.watcher_lifecycle.is_process_running")

# SSH tests: inject paramiko via sys.modules (paramiko is optional)
with patch.dict(sys.modules, {"paramiko": pm_mock}):
    ...  # see tests/unit/test_ssh_manager.py for the full pattern
```

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a startup step for both local + cloud | `common/startup.py` | `pytest tests/unit/test_platform_startup.py` |
| Add a local-only startup step | `local/` and `cli/commands/platform.py` | `dango start` manually |
| Add cloud infrastructure | `cloud/` | `pytest tests/unit/test_digitalocean_client.py tests/unit/test_spaces_client.py` |
| Provision a Droplet | `cloud/provisioning.py` → `provision_droplet()` | `pytest tests/unit/test_provisioning.py` |
| Manage firewall rules | `cloud/firewall.py` → `add_allowed_ip()` / `restrict_web_to_ips()` | `pytest tests/unit/test_firewall.py` |
| Backup to Spaces | `cloud/spaces.py` → `SpacesClient` | `pytest tests/unit/test_spaces_client.py` |
| Manage SSH keys / remote exec | `cloud/ssh.py` → `SSHManager` | `pytest tests/unit/test_ssh_manager.py tests/unit/test_ssh_sftp.py` |
| Setup server after provisioning | `cloud/server_setup.py` → `setup_server()` | `pytest tests/unit/test_server_setup.py` |
| Configure domain / HTTPS | `cloud/domain.py` → `set_domain()` / `remove_domain()` | `pytest tests/unit/test_domain.py` |
| Create pre-deploy backup | `cloud/backup.py` → `create_backup()` | `pytest tests/unit/test_backup.py` |
| Rollback from backup | `cloud/backup.py` → `rollback()` | `pytest tests/unit/test_backup.py` |
| Sync files to remote | `cloud/file_sync.py` → `sync_project_files()` | `pytest tests/unit/test_file_sync.py` |
| Push deploy to remote | `cloud/deployer.py` → `push_deploy()` | `pytest tests/unit/test_deployer.py` |
| Scheduled backup (server-side) | `cloud/scheduled_backup.py` → `run_scheduled_backup()` | `pytest tests/unit/test_scheduled_backup.py` |
| CLI backup commands | `cli/commands/remote_backup.py` | `pytest tests/unit/test_remote_backup_cli.py` |
| Modify watcher logic | `local/watcher.py` | `pytest tests/unit/test_watcher_lifecycle.py` |
| Change Docker service startup | `docker.py` | `dango start` manually |
| Load/save cloud.yml | `from dango.config import ConfigLoader` → `load_cloud_config()` / `save_cloud_config()` | `pytest tests/unit/test_cloud_config_loader.py` |

## Architecture Notes

- **`docker.py` is shared** — both local and cloud use `DockerManager`. Cloud may add additional services but reuses the same Docker management abstraction.
- **`local/` is local-only** — nginx routing and file watcher don't apply to cloud deployments (cloud uses Caddy, and `auto_sync=false`).
- **`common/startup.py` raises, never displays** — no `console`, `click`, or `rich` imports. Callers (CLI, cloud serve) handle all user-facing output.
- **Shims for backwards compatibility** — existing code that imports from `dango.platform.watcher_lifecycle` continues to work without changes.

## Dependencies

**Imports from:**
- `dango.config` — ConfigLoader, CloudConfig (load cloud.yml)
- `dango.exceptions` — CloudError, CloudAPIError, CloudAuthError, CloudProvisioningError (cloud/)
- `dango.migrations` — apply_all_pending (startup.py)
- `dango.utils.database` — ensure_dbt_schemas (startup.py)
- `dango.utils.process` — is_process_running, kill_process (watcher_lifecycle.py)
- `dango.visualization` — setup_metabase, import_dashboards (startup.py)
- `httpx` — HTTP transport for DigitalOcean API (cloud/digitalocean.py)
- `boto3` — Spaces S3 client (cloud/spaces.py)
- `paramiko` — SSH transport (cloud/ssh.py)
- `cryptography` — Ed25519 key generation (cloud/ssh.py; core dependency)
- `watchdog` — Observer, FileSystemEventHandler (watcher.py)

**Used by:**
- `dango.cli.commands.platform` — start/stop/status commands
- `dango.cli.commands.serve` — production foreground server
- `dango.cli.commands.remote` — rollback command
- `dango.cli.commands.remote_backup` — backup subcommands
- `dango.web.routes.health` — get_watcher_status
