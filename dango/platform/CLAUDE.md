# platform/

## Purpose

Docker service lifecycle, file watching, job scheduling, and platform startup helpers. Shared between local (`dango start`) and cloud (`dango serve`, TASK-026) startup flows.

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
├── scheduling/          # APScheduler-based job scheduling (TASK-036+)
│   ├── __init__.py      # Re-exports SchedulerService, ResilienceConfig, run_with_resilience, history functions
│   ├── CLAUDE.md        # Scheduling module navigation doc
│   ├── scheduler.py     # SchedulerService (lifecycle, events, cancellation, async bridge)
│   ├── resilience.py    # Resilience: retry, timeout, cancellation (extracted from scheduler.py)
│   ├── history.py       # Execution history tracking (TASK-039)
│   ├── jobs.py          # Module-level job functions (pickle-safe)
│   └── sync_trigger.py  # Server-side manual sync runner (dango remote sync)
│
├── notifications/       # Webhook notification infrastructure (TASK-043+)
│   ├── __init__.py      # Re-exports WebhookSender, EventType, etc.
│   ├── CLAUDE.md        # Notifications module navigation doc
│   ├── webhook.py       # Event types, config, filtering, async sender with retry
│   └── slack.py         # Slack Block Kit formatter
│
├── cloud/               # Cloud-only components (TASK-022+)
│   ├── __init__.py      # Re-exports 63 symbols (clients, provisioning, firewall, backup, deploy, etc.)
│   ├── digitalocean.py  # DO REST API v2 client (Droplets, SSH Keys, Firewalls)
│   ├── provisioning.py  # Size tiers, regions, provision_droplet() orchestration (TASK-023)
│   ├── firewall.py      # Firewall lifecycle, IP allowlisting (TASK-025)
│   ├── spaces.py        # DO Spaces client (S3-compatible via boto3)
│   ├── ssh.py           # SSH key management, TOFU known-hosts, exec/SFTP (TASK-024)
│   ├── server_setup.py  # SSH-based server setup orchestration (TASK-026)
│   ├── server_status.py # Server resource metrics + service status via SSH (TASK-104)
│   ├── domain.py        # DNS check, set_domain(), remove_domain() (TASK-027)
│   ├── backup.py        # SSH-based backup + rollback (TASK-035)
│   ├── file_sync.py     # Project file sync: SFTP + rsync (TASK-028)
│   ├── deployer.py      # Push deployment workflow + deploy lock (TASK-030)
│   ├── scheduled_backup.py  # Server-side scheduled backup (TASK-103)
│   ├── resize.py        # In-place resize (power off → resize → power on) (TASK-104)
│   ├── migrate.py       # Server migration (new droplet via Spaces) (TASK-105)
│   ├── upgrade.py       # Remote Dango version upgrade (TASK-106)
│   └── _server_templates.py  # Config file templates (incl. build_caddyfile(), backup timer)
│
│   # Backwards-compatible shims (re-export from local/)
├── network.py           # → platform.local.network
├── watcher.py           # → platform.local.watcher
├── watcher_lifecycle.py # → platform.local.watcher_lifecycle
└── watcher_runner.py    # → platform.local.watcher_runner
```

## Files

| File | Purpose | Key Symbols |
|------|---------|-------------|
| `docker.py` | Docker Compose lifecycle | `DockerManager`, `ServiceStatus` |
| `common/startup.py` | Shared startup helpers | `run_pending_migrations`, `ensure_dbt_schemas`, `ensure_duckdb_driver`, `start_docker_services`, `setup_metabase_if_needed`, `import_dashboards` |
| `local/network.py` | Shared nginx routing (local dev) | `NetworkConfig`, `NginxManager`, `HostsManager` |
| `local/watcher.py` | File change detection | `DebouncedFileHandler`, `FileWatcher`, `MultiTargetWatcher`, `SyncTrigger` |
| `local/watcher_lifecycle.py` | Watcher subprocess lifecycle | `start_file_watcher`, `stop_file_watcher`, `get_watcher_status`, `get_watcher_pid_file_path` |
| `local/watcher_runner.py` | Background watcher process | `main` |
| `scheduling/scheduler.py` | APScheduler wrapper with SQLite persistence, event listeners, retry callback | `SchedulerService` |
| `scheduling/resilience.py` | Resilience: retry with backoff, timeout via thread kill, cancellation | `ResilienceConfig`, `run_with_resilience`, `_execute_with_timeout`, `_raise_in_thread` |
| `scheduling/history.py` | Execution history tracking for scheduled jobs | `record_start`, `record_completion`, `record_failure`, `get_schedule_history`, `get_recent_history`, `get_average_duration`, `get_last_run`, `cleanup_old_records` |
| `scheduling/jobs.py` | Module-level job functions (pickle-safe) | `configure_jobs`, `run_scheduled_sync`, `run_scheduled_dbt` |
| `scheduling/sync_trigger.py` | Server-side manual sync runner (invoked via SSH) | `main()`, `_run_sync()` |
| `notifications/webhook.py` | Event types, config models, event filtering, async sender with retry | `WebhookSender`, `WebhookConfig`, `NotificationConfig`, `EventType`, `EventCategory`, `WebhookPayload` |
| `notifications/slack.py` | Slack Block Kit formatter for webhook payloads | `format_slack_message` |
| `cloud/digitalocean.py` | DigitalOcean REST API v2 client | `DigitalOceanClient` |
| `cloud/provisioning.py` | Droplet size tiers, regions, provisioning orchestration | `DropletSizeTier`, `RegionInfo`, `SIZE_TIERS`, `DEFAULT_TIER`, `provision_droplet`, `wait_for_droplet_ready`, `wait_for_ssh`, `suggest_nearest_region`, `save_provisioning_metadata` |
| `cloud/firewall.py` | Firewall lifecycle and IP allowlisting | `create_default_firewall`, `add_allowed_ip`, `restrict_web_to_ips`, `allow_all_web`, `validate_ip_or_cidr`, `save_firewall_metadata` |
| `cloud/spaces.py` | DigitalOcean Spaces (S3-compatible) client | `SpacesClient` |
| `cloud/ssh.py` | SSH key management, TOFU known-hosts, command exec, SFTP | `SSHManager`, `CommandResult` |
| `cloud/server_setup.py` | SSH-based server setup orchestration (16 idempotent steps) | `setup_server`, `SetupResult` |
| `cloud/server_status.py` | Server resource metrics, service status, PyPI version check | `ServerStatus`, `ServiceInfo`, `collect_server_status`, `check_latest_pypi_version`, `get_local_resource_usage` |
| `cloud/domain.py` | DNS check, domain set/remove for HTTPS via Caddy | `check_dns`, `set_domain`, `remove_domain` |
| `cloud/backup.py` | SSH-based backup and rollback | `create_backup`, `rollback`, `list_local_backups`, `rotate_local_backups`, `BackupManifest`, `BackupResult`, `RestoreResult` |
| `cloud/file_sync.py` | Project file sync (SFTP + rsync) with change detection | `sync_project_files`, `SyncResult` |
| `cloud/deployer.py` | Push deployment workflow with deploy lock | `push_deploy`, `DeployLock`, `DeployResult` |
| `cloud/scheduled_backup.py` | Server-side scheduled backup to Spaces | `run_scheduled_backup`, `list_spaces_backups`, `restore_from_spaces`, `enable_scheduled_backup`, `disable_scheduled_backup` |
| `cloud/resize.py` | In-place droplet resize (power off → resize → power on, regenerate dbt profiles) | `resize_droplet`, `ResizeResult`, `validate_size_slug`, `generate_dbt_profiles_yml`, `regenerate_dbt_profiles`, `get_disk_warning` |
| `cloud/migrate.py` | Server migration via Spaces (new droplet, transfer data, destroy old) | `migrate_server`, `MigrateResult` |
| `cloud/upgrade.py` | Remote Dango version upgrade (pip install, migrations, Docker rebuild) | `upgrade_dango`, `UpgradeResult`, `validate_version_string`, `check_versions` |
| `cloud/_server_templates.py` | Config file templates (systemd, Caddy, fail2ban, backup timer, etc.) | `build_caddyfile`, `SYSTEMD_UNIT`, `CADDYFILE`, `SYSTEMD_BACKUP_SERVICE`, `SYSTEMD_BACKUP_TIMER`, etc. |

## Import Patterns

### Preferred (canonical):
```python
# Shared infrastructure
from dango.platform import DockerManager, ServiceStatus
from dango.platform.common.startup import run_pending_migrations, start_docker_services

# Scheduling (TASK-036+)
from dango.platform.scheduling import SchedulerService, ResilienceConfig, run_with_resilience
from dango.platform.scheduling import record_start, record_completion, get_schedule_history
from dango.platform.scheduling.history import get_average_duration, get_last_run
from dango.platform.scheduling.jobs import run_scheduled_sync, run_scheduled_dbt

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

# Resize (TASK-104)
from dango.platform.cloud import resize_droplet, ResizeResult, validate_size_slug
from dango.platform.cloud.resize import generate_dbt_profiles_yml, regenerate_dbt_profiles

# Migrate (TASK-105)
from dango.platform.cloud import migrate_server, MigrateResult

# Upgrade (TASK-106)
from dango.platform.cloud import upgrade_dango, UpgradeResult, validate_version_string
from dango.platform.cloud.upgrade import check_versions
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
| Add/modify scheduled jobs | `scheduling/scheduler.py`, `scheduling/jobs.py` | `pytest tests/unit/test_scheduler.py` |
| Query execution history | `scheduling/history.py` | `pytest tests/unit/test_execution_history.py` |
| Trigger manual sync from SSH | `scheduling/sync_trigger.py` | `pytest tests/unit/test_sync_trigger.py` |
| Configure webhook notifications | `notifications/webhook.py` | `pytest tests/unit/test_webhook.py` |
| Add a notification format | Create `notifications/{format}.py`, add dispatch in `webhook.py` | `pytest tests/unit/test_webhook.py` |
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
| Resize droplet | `cloud/resize.py` → `resize_droplet()` | `pytest tests/unit/test_resize.py` |
| Migrate to new server | `cloud/migrate.py` → `migrate_server()` | `pytest tests/unit/test_migrate.py` |
| Upgrade remote Dango | `cloud/upgrade.py` → `upgrade_dango()` | `pytest tests/unit/test_upgrade.py` |
| CLI resize/migrate/upgrade | `cli/commands/remote_ops.py` | `pytest tests/unit/test_remote_ops_cli.py` |
| CLI backup commands | `cli/commands/remote_backup.py` | `pytest tests/unit/test_remote_backup_cli.py` |
| Modify watcher logic | `local/watcher.py` | `pytest tests/unit/test_watcher_lifecycle.py` |
| Change Docker service startup | `docker.py` | `dango start` manually |
| Load/save cloud.yml | `from dango.config import ConfigLoader` → `load_cloud_config()` / `save_cloud_config()` | `pytest tests/unit/test_cloud_config_loader.py` |

## Architecture Notes

- **`docker.py` is shared** — both local and cloud use `DockerManager`. Cloud may add additional services but reuses the same Docker management abstraction.
- **`local/` is local-only** — nginx routing and file watcher don't apply to cloud deployments (cloud uses Caddy, and `auto_sync=false`).
- **`common/startup.py` raises, never displays** — no `console`, `click`, or `rich` imports. Callers (CLI, cloud serve) handle all user-facing output.
- **Shims for backwards compatibility** — existing code that imports from `dango.platform.watcher_lifecycle` continues to work without changes.
- **`cloud/backup.py` is evolving beyond pure backup** — it also provides `stop_services()`, `start_services()`, and `verify_health()`, used by resize, migrate, and deployer as service lifecycle utilities. If more lifecycle functions accumulate, consider extracting a `service_lifecycle.py` module.

## Development Patterns

### Cloud module patterns

- **S3 pagination:** DO Spaces (via boto3) paginates `list_objects_v2`. Always check `IsTruncated` and pass `ContinuationToken` in a loop — don't assume all results come in one response.
- **SSHManager guard ordering:** Check connection state before attempting operations. Guards must follow: (1) check `self._client is not None`, (2) check connection is active, (3) proceed with operation.
- **Shell injection prevention:** Never interpolate user input into SSH commands. Use parameterized approaches or validate inputs against strict patterns before building command strings.
- **SystemExit convention:** Cloud operations that encounter unrecoverable errors (invalid API key, exhausted retries) raise `SystemExit` rather than propagating implementation-specific exceptions to the CLI layer.
- **Two SSH users:** `root` for system operations (backup, rollback, domain, server setup). `dango` for project file operations (.env, .dlt/secrets.toml). Don't mix — root writes to `/srv/dango/project/` with wrong ownership.
- **Sensitive file writes:** Use `os.open()` with explicit mode (e.g., `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`) for files containing secrets. `Path.write_bytes()` + `chmod()` has a TOCTOU window.
- **Click cross-file command registration:** `remote_mgmt.py`, `remote_ops.py`, `remote_env.py`, and `remote_backup.py` register commands on the `remote` group from `remote.py` via bottom-of-file imports.

### Scheduling patterns

- **APScheduler dual-patch testing:** APScheduler 3.x `AsyncIOScheduler` validates jobstores via `isinstance`. Must mock both `SQLAlchemyJobStore` AND `AsyncIOScheduler` — a `MagicMock` jobstore alone gets rejected. See `test_scheduler.py` `_make_service()` helper.
- **No atomic trigger update:** APScheduler 3.x has no single API for updating a job's trigger. Update = `remove_job()` + `add_job()`. `ReloadResult.unchanged` is always empty as a consequence.
- **Cron interval estimation needs sampling:** Non-uniform crons (e.g., `0 6,18 * * *`, `0 9-17 * * 1-5`) have variable gaps. Sample 5+ intervals from `croniter` and return the minimum, not just two consecutive ticks.
- **`dbt_lock` module/function collision:** See [STANDARDS.md §7 Mocking and patching](../../STANDARDS.md#mocking-and-patching) for the workaround when patching `dango.utils.dbt_lock.DbtLock`.
- **Job function signature coupling:** `reload_schedules()` in `dango/config/schedules.py` references job function names and kwargs from `scheduling/jobs.py`. Any rename must update both files in the same PR.
- **APScheduler is untyped** — mypy can't catch type mismatches at the boundary (e.g., `JobEvent` vs `JobExecutionEvent`). Budget review time for scheduler tasks.

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
- `apscheduler` — AsyncIOScheduler, SQLAlchemyJobStore (scheduling/)
- `sqlalchemy` — Required by APScheduler job store (scheduling/)
- `pydantic` — Notification config models (notifications/)
- `yaml` — Notification config loading (notifications/)

**Used by:**
- `dango.cli.commands.platform` — start/stop/status commands
- `dango.cli.commands.serve` — production foreground server
- `dango.cli.commands.remote` — rollback command
- `dango.cli.commands.remote_backup` — backup subcommands
- `dango.cli.commands.remote_ops` — resize, migrate, upgrade commands
- `dango.cli.commands.deploy_provision` — provisioning orchestration
- `dango.cli.commands.remote_env` — remote env var management (uses file_sync)
- `dango.cli.commands.remote_mgmt` — remote status/logs/ssh/query
- `dango.web.routes.health` — get_watcher_status, scheduler status
- `dango.web.routes.schedules` — schedule CRUD, trigger, cancel, history, notification config
- `dango.web.app` — SchedulerService lifecycle (startup/shutdown)
- `dango.cli.commands.schedule` — CLI schedule management

## Cloud Deployment Flow

```
Local Machine                          DigitalOcean
─────────────                          ────────────

dango deploy
  ├─ deploy_wizard.py (interactive)
  ├─ deploy_provision.py
  │  ├─ ssh.py → generate_key()
  │  ├─ digitalocean.py → create       ──→  Droplet (Ubuntu 22.04)
  │  ├─ provisioning.py → wait         ←──  IP address
  │  ├─ firewall.py → create           ──→  Firewall rules
  │  ├─ server_setup.py → setup        ──→  Docker, Caddy, systemd
  │  ├─ file_sync.py → sync            ──→  Project files via SFTP
  │  └─ deployer.py → push_deploy      ──→  Build + restart services
  └─ initial_sync (web)                ──→  First data sync

dango remote push
  ├─ backup.py → create_backup         ──→  Pre-deploy backup
  ├─ file_sync.py → sync               ──→  Changed files
  └─ deployer.py → push_deploy         ──→  Rebuild + restart

dango remote rollback
  └─ backup.py → rollback              ──→  Restore from backup
```

## Remote Server Layout

```
/srv/dango/                            # Application root (owner: dango)
├── project/                           # Synced project files
│   ├── .dango/                        # Dango state
│   │   ├── cloud.yml                  # Cloud config (IP, region, size, etc.)
│   │   ├── state/                     # Runtime state
│   │   └── logs/                      # Activity + audit logs
│   ├── .dlt/                          # dlt config + secrets
│   ├── .env                           # Environment variables
│   ├── data/warehouse.duckdb          # DuckDB database
│   └── docker-compose.yml             # Generated Docker Compose
├── venv/                              # Python virtual environment
└── backups/deploy/                    # Pre-deploy backups

/etc/caddy/Caddyfile                   # Reverse proxy (HTTP or HTTPS)
/etc/systemd/system/dango.service      # Dango systemd unit
/etc/systemd/system/dango-backup.*     # Scheduled backup timer + service
```

## Security Recommendations

Post-deployment hardening (not automated by Dango):

- **Cloudflare proxy:** Route traffic through Cloudflare for DDoS protection and CDN. Set Caddy to trust Cloudflare IPs for correct client IP logging.
- **UptimeRobot:** Monitor `/api/health` endpoint for uptime alerts. Free tier supports 5-minute check intervals.
- **Firewall IP restriction:** Use `dango remote firewall allow-ip` to restrict web access to known IPs during development/staging.

## Testing

- **Scheduling:** `pytest tests/unit/test_scheduler.py tests/unit/test_scheduler_resilience.py tests/unit/test_execution_history.py tests/unit/test_sync_jobs.py tests/unit/test_sync_trigger.py`
- **Notifications:** `pytest tests/unit/test_webhook.py tests/unit/test_slack_formatter.py`
- **Local platform:** `pytest tests/unit/test_platform_startup.py tests/unit/test_watcher_lifecycle.py`
- **Cloud modules:** `pytest tests/unit/test_digitalocean_client.py tests/unit/test_spaces_client.py tests/unit/test_ssh_manager.py tests/unit/test_ssh_sftp.py tests/unit/test_provisioning.py tests/unit/test_firewall.py tests/unit/test_server_setup.py tests/unit/test_domain.py tests/unit/test_backup.py tests/unit/test_file_sync.py tests/unit/test_deployer.py tests/unit/test_scheduled_backup.py tests/unit/test_resize.py tests/unit/test_migrate.py tests/unit/test_upgrade.py`
- **Manual:** `dango start` (local platform), `dango deploy` (cloud provisioning)

## Don't Modify

| File | Reason |
|------|--------|
| `cloud/__init__.py` export list | Other modules depend on re-exported symbols; changes break downstream imports |
| `cloud/_server_templates.py` systemd unit structure | Running servers depend on the exact systemd unit format |
| `local/` watcher event format | Web UI parses watcher status responses |
