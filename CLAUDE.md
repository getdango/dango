# Dango

Data platform for small teams: **dlt** (ingestion) + **DuckDB** (warehouse) + **dbt** (transformation) + **Metabase** (visualization). One `pip install getdango` + `dango start` boots the full stack.

See [ARCHITECTURE.md](ARCHITECTURE.md) for system diagram, data flow, and cross-module workflows.

## Quick Routing Table

| Task Type | Go To | Read First |
|-----------|-------|------------|
| CLI commands | `dango/cli/` | [`dango/cli/CLAUDE.md`](dango/cli/CLAUDE.md) |
| Data ingestion / sync | `dango/ingestion/` | [`dango/ingestion/CLAUDE.md`](dango/ingestion/CLAUDE.md) |
| OAuth / token flows | `dango/oauth/` | [`dango/oauth/CLAUDE.md`](dango/oauth/CLAUDE.md) |
| Web UI / API endpoints | `dango/web/` | [`dango/web/CLAUDE.md`](dango/web/CLAUDE.md) |
| Config loading / models | `dango/config/` | [`dango/config/CLAUDE.md`](dango/config/CLAUDE.md) |
| Dashboards / Metabase | `dango/visualization/` | [`dango/visualization/CLAUDE.md`](dango/visualization/CLAUDE.md) |
| dbt / transformations | `dango/transformation/` | [`dango/transformation/CLAUDE.md`](dango/transformation/CLAUDE.md) |
| Token encryption / keychain | `dango/security/` | [`dango/security/CLAUDE.md`](dango/security/CLAUDE.md) |
| Shared utilities | `dango/utils/` | [`dango/utils/CLAUDE.md`](dango/utils/CLAUDE.md) |
| Logging / diagnostics | `dango/logging.py` | Module docstring |
| Database migrations | `dango/migrations/` | [`dango/migrations/CLAUDE.md`](dango/migrations/CLAUDE.md) |
| Docker / file watcher / cloud / scheduling / notifications | `dango/platform/` | [`dango/platform/CLAUDE.md`](dango/platform/CLAUDE.md) |
| Scheduling / job orchestration | `dango/platform/scheduling/` | [`dango/platform/scheduling/CLAUDE.md`](dango/platform/scheduling/CLAUDE.md) |
| Notifications / webhooks | `dango/platform/notifications/` | [`dango/platform/notifications/CLAUDE.md`](dango/platform/notifications/CLAUDE.md) |
| Jinja2 templates / Dockerfiles | `dango/templates/` | [`dango/templates/CLAUDE.md`](dango/templates/CLAUDE.md) |
| Auth / users / sessions | `dango/auth/` | [`dango/auth/CLAUDE.md`](dango/auth/CLAUDE.md) |
| Notebooks | `dango/notebooks/` | [`dango/notebooks/CLAUDE.md`](dango/notebooks/CLAUDE.md) |
| Data governance | `dango/governance/` | [`dango/governance/CLAUDE.md`](dango/governance/CLAUDE.md) |
| Monitoring / analysis | `dango/analysis/` | [`dango/analysis/CLAUDE.md`](dango/analysis/CLAUDE.md) |

## Don't Read First

| Path | Reason |
|------|--------|
| `dango/ingestion/dlt_sources/` | Vendored third-party dlt connectors (127 files). Rarely modified. |
| `dango/web/static/` | Frontend HTML/CSS/JS assets. |
| `tests/` | Read source module first, then find its tests. |
| `dango/ingestion/sources/registry.py` | 2008-line metadata registry. Only when adding a new source. |
| `dango/templates/` | Jinja2 templates. Only when modifying project init or model generation. |

## Decision Tree

When unsure which module to look at:

- **HOW data gets into DuckDB** → `ingestion/`
- **HOW data gets transformed after loading** → `transformation/`
- **WHAT user sees in browser:**
  - Metabase dashboards → `visualization/`
  - Dango web UI → `web/`
  - Terminal commands → `cli/`
- **WHERE credentials are stored:**
  - OAuth flow (Google, Facebook) → `oauth/`
  - Token encryption (keychain) → `security/`
  - Config file format (`.dango/*.yml`) → `config/`
- **Shared utility** (locking, logging, DB helpers) → `utils/`
- **HOW schema drift / PII is detected** → `governance/`
- **HOW metrics are tracked / compared** → `analysis/`
- **Marimo notebooks** → `notebooks/`
- **Docker containers / file watching** → `platform/`
- **HOW scheduled jobs run** → `platform/scheduling/`
- **HOW notifications are sent** → `platform/notifications/`
- **HOW code gets deployed to the cloud** → `platform/cloud/`
- **CLI for cloud operations** → `cli/commands/remote*.py`, `cli/commands/deploy*.py`
- **Still unsure** → read `ARCHITECTURE.md` §6 (Cross-Module Workflows)

## Repository Structure

```
dango/                          # Python package source
├── __init__.py
├── logging.py                  # Level 0 — Structured logging (structlog + stdlib)
├── cli/                        # Level 3 — Click CLI (primary user interface)
│   ├── __init__.py             # Shared Console instance
│   ├── main.py                 # Slim entry point (~125 lines) — registers commands
│   ├── commands/               # Command modules (extracted from main.py by TASK-005)
│   │   ├── __init__.py         # Package marker
│   │   ├── auth.py             # auth group (13 subcommands, 660 lines)
│   │   ├── cleanup.py          # cleanup old logs, dbt artifacts, Python cache
│   │   ├── oauth.py            # oauth group + 10 subcommands (813 lines)
│   │   ├── config_cmd.py       # config group (validate/show)
│   │   ├── dashboard.py        # dashboard group (provision)
│   │   ├── data.py             # db group (status/clean) + validate
│   │   ├── metabase_cmd.py     # metabase group (save/load/refresh)
│   │   ├── model.py            # model group (add/remove)
│   │   ├── platform.py         # start/stop/status + port helpers (1061 lines)
│   │   ├── project.py          # init/rename/info
│   │   ├── source.py           # source group (add/list/remove/edit) + sync (884 lines)
│   │   ├── transform.py        # run/docs/generate
│   │   ├── upgrade.py          # local Dango upgrade via pip + migrations
│   │   ├── web.py              # web dev server
│   │   ├── serve.py            # serve production foreground server
│   │   ├── deploy.py           # deploy group (wizard default, --byos, destroy)
│   │   ├── deploy_wizard.py    # Interactive deploy wizard + BYOS (877 lines)
│   │   ├── deploy_provision.py # Provisioning orchestration + BYOS (897 lines)
│   │   ├── dev.py              # dev group (default run + clean) — branch-based dbt dev
│   │   ├── migrate.py          # migrate group (status, run)
│   │   ├── remote.py           # remote group + push/rollback/firewall/domain (700 lines)
│   │   ├── remote_env.py       # remote env subgroup (set/get/list/delete)
│   │   ├── remote_ops.py       # remote upgrade/resize/migrate
│   │   ├── remote_backup.py    # remote backup subgroup
│   │   ├── remote_auth.py      # remote auth subgroup (reset-password, reset-2fa)
│   │   ├── remote_mgmt.py      # remote status/logs/ssh/query
│   │   ├── schedule.py         # schedule group (add/list/remove/status/enable/disable/webhook)
│   │   ├── governance.py       # governance group (drift-report/pii-report)
│   │   ├── notebook.py         # notebook group (new/open)
│   │   ├── snapshot.py         # snapshot group (add/list/run/db) — dbt snapshots + DuckDB snapshots
│   │   └── analyze.py          # monitor group + analyze alias
│   ├── init.py                 # Project initialization wizard
│   ├── wizard.py               # Interactive setup wizards
│   ├── source_wizard.py        # Source configuration wizard
│   ├── model_wizard.py         # dbt model creation wizard
│   ├── oauth.py                # OAuth setup commands
│   ├── helpers/                # CLI helper subpackage (TASK-006)
│   │   ├── __init__.py         # Package marker
│   │   ├── port_manager.py     # Port checking utilities
│   │   └── process_manager.py  # FastAPI server process management
│   ├── utils.py                # CLI display helpers + project context
│   ├── db_helpers.py           # Database inspection helpers
│   ├── env_helpers.py          # Environment setup helpers
│   ├── schema_manager.py       # Schema management
│   └── validate.py             # Validation commands
│
├── auth/                       # Level 1 — User authentication & access control
│   ├── __init__.py             # Re-exports 96 public symbols
│   ├── models.py               # Role, User, Session, APIKey Pydantic models
│   ├── database.py             # SQLite CRUD (WAL mode, 529 lines)
│   ├── security.py             # Bcrypt hashing, token generation (386 lines)
│   ├── sessions.py             # Session + API key lifecycle (289 lines)
│   ├── permissions.py          # 29 permissions, 3 role mappings
│   ├── lockout.py              # Brute-force protection (5 attempts / 15-min)
│   ├── audit.py                # 22 event types → .dango/logs/audit.jsonl
│   ├── admin.py                # Bootstrap admin, config path helpers
│   ├── totp.py                 # TOTP 2FA + recovery codes
│   ├── oauth_login.py          # OAuth provider ABC + Google/GitHub
│   ├── metabase_sync.py        # Sync users/roles to Metabase (552 lines)
│   └── metabase_bridge.py      # Async SSO session bridging
│
├── governance/                 # Level 1 — Data governance (schema drift + PII scanning)
│   ├── __init__.py             # Re-exports public API
│   ├── models.py               # Pydantic V2 response models
│   ├── schema_drift.py         # Schema drift detection engine (654 lines)
│   ├── pii_detector.py         # PII scanning engine (614 lines)
│   └── pii_overrides.py        # PII override CRUD
│
├── notebooks/                  # Level 1 — Marimo notebook management
│   ├── __init__.py             # Re-exports public symbols
│   ├── manager.py              # Marimo process lifecycle (330 lines)
│   ├── locking.py              # File-level notebook locking (285 lines)
│   ├── snapshot.py             # DuckDB snapshot management
│   ├── proxy.py                # HTTP + WebSocket reverse proxy (186 lines)
│   └── templates/              # Marimo starter templates (explore, quality, blank)
│
├── analysis/                   # Level 1 — Metric monitoring + comparison engine
│   ├── __init__.py             # Public API re-exports
│   ├── models.py               # Pydantic V2 models (MetricConfig, ComparisonResult, etc.)
│   ├── config.py               # YAML config load/save
│   ├── comparisons.py          # Comparison engine + trend detection
│   ├── drilldown.py            # Drill-down engine
│   ├── metrics.py              # Orchestration: execute → store → compare
│   ├── templates.py            # Pre-built metric templates for common sources
│   └── formatter.py            # Result categorization + display formatting
│
├── web/                        # Level 2 — FastAPI web server
│   ├── app.py                  # Entry point (~480 lines) — routers, middleware, admin bootstrap
│   ├── models.py               # Pydantic request/response DTOs (incl. auth DTOs)
│   ├── helpers.py              # Shared helpers: DuckDB queries, config, logging (868 lines)
│   ├── middleware/             # Request middleware
│   │   ├── auth.py             # Session/API key auth + CSRF check (~324 lines)
│   │   └── rate_limit.py       # Rate limiting (~235 lines)
│   ├── routes/                 # Route modules
│   │   ├── __init__.py         # Package marker
│   │   ├── auth.py             # Login/logout, OAuth, invite, API keys (~886 lines)
│   │   ├── auth_2fa.py         # TOTP 2FA endpoints (~340 lines)
│   │   ├── users.py            # Admin user CRUD (531 lines)
│   │   ├── health.py           # /api/status, /api/watcher/status, /api/health/platform
│   │   ├── config.py           # /api/config, /api/metabase-config
│   │   ├── sources.py          # /api/sources, /api/sources/{name}/details
│   │   ├── sync.py             # /api/sources/{name}/sync + run_sync_task()
│   │   ├── logs.py             # /api/logs, /api/sources/{name}/logs
│   │   ├── dbt.py              # /api/dbt/models, /api/dbt/models/{name}/run + dbt docs proxy
│   │   ├── upload.py           # CSV upload/list/delete (705 lines)
│   │   ├── websocket.py        # ConnectionManager, ws_manager, /ws
│   │   ├── ui.py               # /, /health, /logs, /login, /account, /admin/users
│   │   ├── metabase_proxy.py   # Metabase reverse proxy + SSO session
│   │   ├── secrets.py          # Secrets + OAuth credential management (admin-only)
│   │   ├── oauth_connect.py    # Web-based OAuth connect/callback
│   │   ├── catalog.py          # Data catalog: columns, profiling, lineage, impact, models, search (1336 lines)
│   │   ├── governance.py       # Schema drift + PII results API
│   │   ├── monitoring.py       # Monitor results, run trigger, history
│   │   ├── notebooks.py        # Notebook management API + page route
│   │   └── initial_sync.py     # Initial data sync after first deploy
│   ├── templates/              # Jinja2 page templates
│   │   ├── login.html          # Alpine.js two-step login (credentials → totp)
│   │   ├── change_password.html # First-login password change
│   │   ├── admin_users.html    # Admin user management
│   │   ├── account.html        # User account settings
│   │   ├── invite.html         # Invite acceptance page
│   │   ├── secrets.html        # Secrets management page
│   │   ├── schedules.html      # Schedule management page
│   │   ├── notebooks.html      # Notebook management page
│   │   ├── catalog.html        # Data catalog page (929 lines)
│   │   └── monitoring.html     # Monitoring page
│   └── static/                 # Frontend HTML/CSS/JS
│
├── visualization/              # Level 2 — Metabase integration
│   ├── metabase.py             # Metabase API (1152 lines)
│   └── dashboard_manager.py    # Dashboard export/import (1112 lines)
│
├── platform/                   # Level 2 — Docker, network, file watcher, scheduling
│   ├── __main__.py             # Platform CLI entry point
│   ├── docker.py               # Docker Compose lifecycle (shared local + cloud)
│   ├── CLAUDE.md               # Module navigation doc
│   ├── common/                 # Shared startup helpers (local + cloud)
│   │   └── startup.py          # run_pending_migrations, start_docker_services, etc.
│   ├── local/                  # Local-only components
│   │   ├── network.py          # NetworkConfig, NginxManager, HostsManager
│   │   ├── watcher.py          # File change detection (518 lines)
│   │   ├── watcher_lifecycle.py # Watcher subprocess lifecycle
│   │   └── watcher_runner.py   # Background watcher process
│   ├── scheduling/             # APScheduler-based job scheduling (TASK-036+)
│   │   ├── scheduler.py        # SchedulerService (lifecycle, events, cancellation)
│   │   ├── resilience.py       # Retry, timeout, cancellation
│   │   ├── history.py          # Execution history tracking
│   │   ├── jobs.py             # Module-level job functions (860 lines)
│   │   └── sync_trigger.py     # Server-side manual sync runner
│   ├── notifications/          # Webhook notifications (TASK-043+)
│   │   ├── webhook.py          # Event types, config, async sender
│   │   └── slack.py            # Slack Block Kit formatter
│   ├── cloud/                  # Cloud components (TASK-022+)
│   │   ├── __init__.py         # Re-exports 69 symbols
│   │   ├── digitalocean.py     # DO REST API v2 client (547 lines)
│   │   ├── provisioning.py     # Size tiers, regions, provision_droplet()
│   │   ├── firewall.py         # Firewall lifecycle, IP allowlisting
│   │   ├── spaces.py           # DO Spaces (S3-compatible via boto3)
│   │   ├── ssh.py              # SSH key mgmt, TOFU, exec/SFTP (665 lines)
│   │   ├── server_setup.py     # Server setup orchestration (16 steps)
│   │   ├── server_status.py    # Server metrics + service status
│   │   ├── domain.py           # DNS check, domain set/remove
│   │   ├── backup.py           # Backup + rollback + service lifecycle
│   │   ├── file_sync.py        # Project file sync (SFTP + rsync)
│   │   ├── deployer.py         # Push deploy workflow + deploy lock (598 lines)
│   │   ├── deploy_journal.py   # Append-only JSONL deployment history
│   │   ├── scheduled_backup.py # Server-side scheduled backup (508 lines)
│   │   ├── resize.py           # In-place droplet resize
│   │   ├── migrate.py          # Server migration via Spaces
│   │   ├── upgrade.py          # Remote Dango version upgrade
│   │   └── _server_templates.py # Config file templates
│   │   # Backwards-compatible shims (re-export from local/):
│   ├── network.py              # → local/network.py
│   ├── watcher.py              # → local/watcher.py
│   ├── watcher_lifecycle.py    # → local/watcher_lifecycle.py
│   └── watcher_runner.py       # → local/watcher_runner.py
│
├── ingestion/                  # Level 1 — Data loading
│   ├── dlt_runner.py           # ⚠ 2534 lines — orchestrates full sync pipeline
│   ├── csv_loader.py           # Multi-format file loading with dedup (922 lines)
│   ├── sources/
│   │   └── registry.py         # Source metadata (33 source types)
│   └── dlt_sources/            # ⚠ DO NOT MODIFY — vendored connectors (127 files)
│
├── transformation/             # Level 1 — dbt model generation & execution
│   ├── __init__.py             # run_dbt_models(), generate_dbt_docs()
│   └── generator.py            # DbtModelGenerator (613 lines)
│
├── oauth/                      # Level 1 — OAuth flows
│   ├── __init__.py             # OAuthManager
│   ├── providers.py            # Google, Facebook providers (670 lines)
│   ├── storage.py              # Credential CRUD in .dlt/secrets.toml
│   ├── router.py               # FastAPI OAuth callback endpoints
│   ├── validation.py           # Live token validation + refresh checking
│   └── web_flow.py             # Browser-based OAuth for cloud deployments
│
├── config/                     # Level 0 — Configuration & credentials
│   ├── models.py               # Pydantic models (DangoConfig, DataSource, etc.)
│   ├── loader.py               # ConfigLoader — loads project.yml, sources.yml
│   ├── helpers.py              # Convenience functions (find_project_root, get_config, load_config, save_config)
│   ├── credentials.py          # CredentialManager — manages .dlt/secrets.toml, .env
│   └── exceptions.py           # Config-specific exceptions
│
├── utils/                      # Level 0 — Shared utilities
│   ├── process.py              # Generic process utilities (is_process_running, kill_process)
│   ├── dbt_lock.py             # DbtLock — single-writer DuckDB serialization
│   ├── activity_log.py         # Append-only JSON activity log
│   ├── sync_history.py         # Per-source sync results
│   ├── database.py             # Schema creation helpers
│   ├── db_health.py            # DuckDB health checks + component disk breakdown
│   ├── dbt_status.py           # dbt model status tracking
│   ├── log_rotation.py         # JSONL log rotation with gzip compression
│   ├── data_validation.py      # Data validation utilities
│   ├── env_file.py             # .env file parsing and serialization
│   ├── dango_db.py             # SQLite context manager for .dango/dango.db + schema init
│   ├── post_sync.py            # Post-sync hook dispatcher (~661 lines)
│   └── git_info.py             # Git repository info + deployment guardrails
│
├── migrations/                 # Level 0 — Database migration framework
│   ├── __init__.py             # Public API: apply_all_pending(), get_all_status()
│   └── runner.py               # MigrationRunner, MigrationInfo, MigrationStatus
│
├── security/                   # Level 0 — Token encryption
│   └── token_storage.py        # SecureTokenStorage (OS keychain + Fernet)
│
└── templates/                  # Level 0 — Jinja2 templates & Dockerfiles
    ├── docker-compose.yml.j2
    ├── Dockerfile.metabase
    ├── nginx.conf.j2
    └── dbt/                    # dbt model templates

tests/
├── unit/                       # Unit tests (pytest -m unit)
├── integration/                # Integration tests (pytest -m integration)
└── fixtures/                   # Static test fixture files
```

## Key Architectural Concepts

### Module Dependency Hierarchy

| Level | Role | Modules |
|-------|------|---------|
| 0 (base) | No dango imports | `config/`, `utils/`, `security/`, `migrations/`, `templates/`, `logging.py` |
| 1 (core) | Imports Level 0 only | `auth/`, `oauth/`, `ingestion/`, `transformation/`, `governance/`, `notebooks/`, `analysis/` |
| 2 (platform) | Imports Level 0–1 | `platform/`, `web/`, `visualization/` |
| 3 (ui) | Imports any level | `cli/` |

Imports flow downward only. Same-level imports are allowed if non-circular. See `ARCHITECTURE.md` §3 for full rules.

### Single-Writer DuckDB

DuckDB allows only one writer process at a time. All write operations are serialized through `DbtLock` in `utils/dbt_lock.py` (file-based lock at `.dango/state/dbt.lock`). Concurrent reads during writes are allowed. See `ARCHITECTURE.md` §5.

### Monolithic Files

These files exceed 500 lines. One has planned refactoring; the rest are exempt (stable MVP code).
Full exemption registry: [`docs/file-exemptions.yml`](docs/file-exemptions.yml)

| File | Lines | Refactoring Task |
|------|-------|-----------------|
| `ingestion/dlt_runner.py` | 2534 | — (exempt, too risky) |
| `ingestion/sources/registry.py` | 2008 | — (metadata-only) |
| `cli/source_wizard.py` | 2311 | — |
| `visualization/metabase.py` | 1152 | — |
| `cli/init.py` | 1334 | — |
| `visualization/dashboard_manager.py` | 1112 | — |
| `cli/commands/platform.py` | 1061 | — (extracted from main.py by TASK-005) |
| `web/routes/auth.py` | 886 | — (split evaluated in DOC-025: exempt, security-critical) |
| `cli/commands/oauth.py` | 813 | — (renamed from auth.py by TASK-093) |
| `web/helpers.py` | 868 | — (extracted from app.py by TASK-085) |
| `ingestion/csv_loader.py` | 922 | — |
| `platform/scheduling/jobs.py` | 860 | — (module-level job functions) |
| `utils/post_sync.py` | 661 | — (post-sync hooks + sync notification) |
| `web/routes/schedules.py` | 519 | — (schedule read-only, history, trigger, notifications) |
| `web/routes/notebooks.py` | 576 | — (notebook management API + heartbeat lock expiry + WS notify) |
| `web/routes/upload.py` | 705 | — (extracted from app.py by TASK-085) |
| `oauth/providers.py` | 670 | — |
| `platform/cloud/ssh.py` | 665 | — (SSH key mgmt, TOFU, exec/SFTP) |
| `cli/commands/source.py` | 884 | — (extracted from main.py by TASK-005) |
| `cli/commands/remote.py` | 700 | — (remote group + push/rollback/firewall/domain) |
| `cli/commands/remote_mgmt.py` | 509 | — (remote status/logs/ssh/query + deployment history) |
| `platform/cloud/deployer.py` | 598 | — (push deploy workflow + deploy lock + journal) |
| `cli/validate.py` | 652 | — |
| `config/models.py` | 606 | — (Pydantic config models) |
| `cli/commands/deploy_wizard.py` | 877 | — (interactive deploy wizard + BYOS) |
| `transformation/generator.py` | 613 | — |
| `web/routes/catalog.py` | 1336 | — (data catalog: columns, profiling, lineage, impact, models, search, raw table discovery, per-source stats) |
| `cli/commands/deploy_provision.py` | 897 | — (provisioning orchestration + BYOS) |
| `platform/cloud/digitalocean.py` | 547 | — (DO REST API v2 client) |
| `platform/cloud/server_setup.py` | 690 | — (server setup + install source detection) |
| `cli/commands/auth.py` | 660 | — (13 auth subcommands) |
| `auth/database.py` | 529 | — (SQLite CRUD) |
| `web/routes/users.py` | 531 | — (admin user CRUD + invite) |
| `platform/local/watcher.py` | 518 | — |
| `cli/commands/schedule.py` | 770 | — (schedule wizard + time customization) |
| `cli/model_wizard.py` | 507 | — |
| `platform/cloud/scheduled_backup.py` | 508 | — (server-side scheduled backup) |
| `governance/schema_drift.py` | 654 | — (R9-D: breaking drift protection + accept flow) |
| `governance/pii_detector.py` | 614 | — (BUG-027/BUG-133/BUG-139/BUG-185: spaCy fallback + PERSON threshold + override application + structured data heuristic) |

## Module Documentation Index

Module CLAUDE.md files provide per-module navigation, public API, and patterns.

**Existing:**
- [`dango/CLAUDE.md`](dango/CLAUDE.md) (package root)
- [`dango/cli/CLAUDE.md`](dango/cli/CLAUDE.md)
- [`dango/config/CLAUDE.md`](dango/config/CLAUDE.md)
- [`dango/ingestion/CLAUDE.md`](dango/ingestion/CLAUDE.md)
- [`dango/oauth/CLAUDE.md`](dango/oauth/CLAUDE.md)
- [`dango/transformation/CLAUDE.md`](dango/transformation/CLAUDE.md)
- [`dango/visualization/CLAUDE.md`](dango/visualization/CLAUDE.md)
- [`dango/security/CLAUDE.md`](dango/security/CLAUDE.md)
- [`dango/utils/CLAUDE.md`](dango/utils/CLAUDE.md)
- [`dango/templates/CLAUDE.md`](dango/templates/CLAUDE.md)
- [`dango/web/CLAUDE.md`](dango/web/CLAUDE.md)
- [`dango/migrations/CLAUDE.md`](dango/migrations/CLAUDE.md)
- [`dango/auth/CLAUDE.md`](dango/auth/CLAUDE.md)

- [`dango/platform/CLAUDE.md`](dango/platform/CLAUDE.md)
- [`dango/platform/scheduling/CLAUDE.md`](dango/platform/scheduling/CLAUDE.md)
- [`dango/platform/notifications/CLAUDE.md`](dango/platform/notifications/CLAUDE.md)
- [`dango/notebooks/CLAUDE.md`](dango/notebooks/CLAUDE.md)
- [`dango/governance/CLAUDE.md`](dango/governance/CLAUDE.md)
- [`dango/analysis/CLAUDE.md`](dango/analysis/CLAUDE.md)

## Development Setup

**Prerequisites:** Python >=3.10,<3.13 (macOS: use `python3.11` — system `python3` is 3.9), Docker

```bash
# First time setup
python3.11 -m venv venv && source venv/bin/activate && pip install -e ".[dev]"

# Subsequent sessions — ALWAYS activate before any Python or git operations
source venv/bin/activate

# Run locally
dango start

# Run tests
pytest                    # all tests
pytest -m unit            # unit tests only
pytest -m integration     # integration tests only

# Code quality (config in pyproject.toml)
ruff check dango/
ruff format --check dango/
mypy dango/
```

### Git Workflow

All v1 development happens on feature branches off `v1`. Never commit directly to `v1` or `main`.

```bash
# Start a task
git checkout v1 && git pull && git checkout -b feat/<task-name>

# Use feat/ prefix — git can't create v1/... branches when v1 exists
# Rebase onto v1 before creating PR (surfaces conflicts, keeps linear history)
git rebase v1

# Push and create PR
git push -u origin feat/<task-name>
gh pr create --base v1 --title "TASK-XXX: Description" --body "..."

# Merge — Option A (preferred when merge queue is enabled):
gh pr merge NUMBER --merge-queue

# Merge — Option B (direct merge via API):
gh api repos/getdango/dango/pulls/NUMBER/merge -X PUT -f merge_method=merge

# If merge blocked by strict branch protection:
gh api repos/getdango/dango/pulls/NUMBER/update-branch -X PUT
# Wait ~2-3 min for CI, then retry merge

# Cleanup remote branch
gh api repos/getdango/dango/git/refs/heads/feat/<task-name> -X DELETE
```

### Pre-commit hooks

Pre-commit hooks run automatically on `git commit`. The local hooks (`language: system`) use whatever `python3` is in your PATH.

**You must activate the venv before committing.** Without it, system Python (3.9 on macOS) lacks required dependencies (PyYAML, ruff, mypy) and can't parse modern type syntax (`X | None`).

```bash
# Always do this before git commit
source venv/bin/activate

# If hooks fail, check which Python is active
which python3  # should point to venv/bin/python3, not /usr/bin/python3
```

Hooks that run on every commit: trailing-whitespace, end-of-file-fixer, check-yaml, check-added-large-files, ruff, ruff-format, file-size-check, file-header-check, docstring-check, claude-md-staleness. Mypy runs only on manual invocation (`pre-commit run mypy --hook-stage manual`).
