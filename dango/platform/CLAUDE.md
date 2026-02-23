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
│   ├── __init__.py      # Exports DigitalOceanClient, SpacesClient
│   ├── digitalocean.py  # DO REST API v2 client (Droplets, SSH Keys, Firewalls)
│   └── spaces.py        # DO Spaces client (S3-compatible via boto3)
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
| `cloud/spaces.py` | DigitalOcean Spaces (S3-compatible) client | `SpacesClient` |

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
```

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a startup step for both local + cloud | `common/startup.py` | `pytest tests/unit/test_platform_startup.py` |
| Add a local-only startup step | `local/` and `cli/commands/platform.py` | `dango start` manually |
| Add cloud infrastructure | `cloud/` | `pytest tests/unit/test_digitalocean_client.py tests/unit/test_spaces_client.py` |
| Provision a Droplet | `cloud/digitalocean.py` → `DigitalOceanClient` | `pytest tests/unit/test_digitalocean_client.py` |
| Backup to Spaces | `cloud/spaces.py` → `SpacesClient` | `pytest tests/unit/test_spaces_client.py` |
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
- `dango.exceptions` — CloudError, CloudAPIError, CloudAuthError (cloud/)
- `dango.migrations` — apply_all_pending (startup.py)
- `dango.utils.database` — ensure_dbt_schemas (startup.py)
- `dango.utils.process` — is_process_running, kill_process (watcher_lifecycle.py)
- `dango.visualization` — setup_metabase, import_dashboards (startup.py)
- `httpx` — HTTP transport for DigitalOcean API (cloud/digitalocean.py)
- `boto3` (optional, `[cloud]` extra) — Spaces S3 client (cloud/spaces.py)
- `watchdog` — Observer, FileSystemEventHandler (watcher.py)

**Used by:**
- `dango.cli.commands.platform` — start/stop/status commands
- `dango.web.routes.health` — get_watcher_status
