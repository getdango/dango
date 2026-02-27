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
| Docker / file watcher / cloud deployment | `dango/platform/` | [`dango/platform/CLAUDE.md`](dango/platform/CLAUDE.md) |
| Jinja2 templates / Dockerfiles | `dango/templates/` | [`dango/templates/CLAUDE.md`](dango/templates/CLAUDE.md) |
| Auth / users / sessions | `dango/auth/` | [`dango/auth/CLAUDE.md`](dango/auth/CLAUDE.md) |
| Notebooks | `dango/notebooks/` | Not yet created (Phase 6) |
| Data governance | `dango/governance/` | Not yet created (Phase 7) |

## Don't Read First

| Path | Reason |
|------|--------|
| `dango/ingestion/dlt_sources/` | Vendored third-party dlt connectors (127 files). Rarely modified. |
| `dango/web/static/` | Frontend HTML/CSS/JS assets. |
| `tests/` | Read source module first, then find its tests. |
| `dango/ingestion/sources/registry.py` | 1440-line metadata registry. Only when adding a new source. |
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
  - OAuth flow (Google, Facebook, Shopify) → `oauth/`
  - Token encryption (keychain) → `security/`
  - Config file format (`.dango/*.yml`) → `config/`
- **Shared utility** (locking, logging, DB helpers) → `utils/`
- **Docker containers / file watching** → `platform/`
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
│   ├── main.py                 # Slim entry point (~88 lines) — registers commands
│   ├── commands/               # Command modules (extracted from main.py by TASK-005)
│   │   ├── __init__.py         # Package marker
│   │   ├── auth.py             # auth group (12 subcommands, 538 lines)
│   │   ├── oauth.py            # oauth group + 10 subcommands (815 lines)
│   │   ├── config_cmd.py       # config group (validate/show)
│   │   ├── dashboard.py        # dashboard group (provision)
│   │   ├── data.py             # db group (status/clean) + validate
│   │   ├── metabase_cmd.py     # metabase group (save/load/refresh)
│   │   ├── model.py            # model group (add/remove)
│   │   ├── platform.py         # start/stop/status + port helpers (941 lines)
│   │   ├── project.py          # init/rename/info
│   │   ├── source.py           # source group (add/list/remove) + sync (549 lines)
│   │   ├── transform.py        # run/docs/generate
│   │   ├── web.py              # web dev server
│   │   ├── serve.py            # serve production foreground server
│   │   ├── deploy.py           # deploy group (wizard default, destroy)
│   │   ├── deploy_wizard.py    # Interactive deploy wizard (579 lines)
│   │   ├── deploy_provision.py # Provisioning orchestration (543 lines)
│   │   ├── migrate.py          # migrate group (status, run)
│   │   ├── remote.py           # remote group + push/rollback/firewall/domain (651 lines)
│   │   ├── remote_env.py       # remote env subgroup (set/get/list/delete)
│   │   ├── remote_ops.py       # remote upgrade/resize/migrate
│   │   ├── remote_backup.py    # remote backup subgroup
│   │   └── remote_mgmt.py      # remote status/logs/ssh/query
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
│   ├── security.py             # Bcrypt hashing, token generation (375 lines)
│   ├── sessions.py             # Session + API key lifecycle (289 lines)
│   ├── permissions.py          # 29 permissions, 3 role mappings
│   ├── lockout.py              # Brute-force protection (5 attempts / 15-min)
│   ├── audit.py                # 22 event types → .dango/logs/audit.jsonl
│   ├── admin.py                # Bootstrap admin, config path helpers
│   ├── totp.py                 # TOTP 2FA + recovery codes
│   ├── oauth_login.py          # OAuth provider ABC + Google/GitHub
│   ├── metabase_sync.py        # Sync users/roles to Metabase (498 lines)
│   └── metabase_bridge.py      # Async SSO session bridging
│
├── web/                        # Level 2 — FastAPI web server
│   ├── app.py                  # Entry point (~301 lines) — routers, middleware, admin bootstrap
│   ├── models.py               # Pydantic request/response DTOs (incl. auth DTOs)
│   ├── helpers.py              # Shared helpers: DuckDB queries, config, logging (798 lines)
│   ├── middleware/             # Request middleware
│   │   ├── auth.py             # Session/API key auth + CSRF check (~325 lines)
│   │   └── rate_limit.py       # Rate limiting (~212 lines)
│   ├── routes/                 # Route modules
│   │   ├── __init__.py         # Package marker
│   │   ├── auth.py             # Login/logout, OAuth, invite, API keys (~854 lines)
│   │   ├── auth_2fa.py         # TOTP 2FA endpoints (~328 lines)
│   │   ├── users.py            # Admin user CRUD (525 lines)
│   │   ├── health.py           # /api/status, /api/watcher/status, /api/health/platform
│   │   ├── config.py           # /api/config, /api/metabase-config
│   │   ├── sources.py          # /api/sources, /api/sources/{name}/details
│   │   ├── sync.py             # /api/sources/{name}/sync + run_sync_task()
│   │   ├── logs.py             # /api/logs, /api/sources/{name}/logs
│   │   ├── dbt.py              # /api/dbt/models, /api/dbt/models/{name}/run + dbt docs proxy
│   │   ├── upload.py           # CSV upload/list/delete (680 lines)
│   │   ├── websocket.py        # ConnectionManager, ws_manager, /ws
│   │   ├── ui.py               # /, /health, /logs, /login, /account, /admin/users
│   │   ├── metabase_proxy.py   # Metabase reverse proxy + SSO session
│   │   ├── secrets.py          # Secrets + OAuth credential management (admin-only)
│   │   ├── oauth_connect.py    # Web-based OAuth connect/callback
│   │   └── initial_sync.py     # Initial data sync after first deploy
│   ├── templates/              # Auth page templates
│   │   ├── login.html          # Alpine.js two-step login (credentials → totp)
│   │   ├── change_password.html # First-login password change
│   │   ├── admin_users.html    # Admin user management
│   │   ├── account.html        # User account settings
│   │   ├── invite.html         # Invite acceptance page
│   │   └── secrets.html        # Secrets management page
│   └── static/                 # Frontend HTML/CSS/JS
│
├── visualization/              # Level 2 — Metabase integration
│   ├── metabase.py             # Metabase API (1142 lines)
│   └── dashboard_manager.py    # Dashboard export/import (1113 lines)
│
├── platform/                   # Level 2 — Docker, network, file watcher
│   ├── __main__.py             # Platform CLI entry point
│   ├── docker.py               # Docker Compose lifecycle (shared local + cloud)
│   ├── CLAUDE.md               # Module navigation doc
│   ├── common/                 # Shared startup helpers (local + cloud)
│   │   └── startup.py          # run_pending_migrations, start_docker_services, etc.
│   ├── local/                  # Local-only components
│   │   ├── network.py          # NetworkConfig, NginxManager, HostsManager
│   │   ├── watcher.py          # File change detection (506 lines)
│   │   ├── watcher_lifecycle.py # Watcher subprocess lifecycle
│   │   └── watcher_runner.py   # Background watcher process
│   ├── cloud/                  # Cloud components (TASK-022+)
│   │   ├── __init__.py         # Re-exports 63 symbols
│   │   ├── digitalocean.py     # DO REST API v2 client (542 lines)
│   │   ├── provisioning.py     # Size tiers, regions, provision_droplet()
│   │   ├── firewall.py         # Firewall lifecycle, IP allowlisting
│   │   ├── spaces.py           # DO Spaces (S3-compatible via boto3)
│   │   ├── ssh.py              # SSH key mgmt, TOFU, exec/SFTP (665 lines)
│   │   ├── server_setup.py     # Server setup orchestration (16 steps)
│   │   ├── server_status.py    # Server metrics + service status
│   │   ├── domain.py           # DNS check, domain set/remove
│   │   ├── backup.py           # Backup + rollback + service lifecycle
│   │   ├── file_sync.py        # Project file sync (SFTP + rsync)
│   │   ├── deployer.py         # Push deploy workflow + deploy lock
│   │   ├── scheduled_backup.py # Server-side scheduled backup (505 lines)
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
│   ├── dlt_runner.py           # ⚠ 1759 lines — orchestrates full sync pipeline
│   ├── csv_loader.py           # CSV loading with dedup (742 lines)
│   ├── sources/
│   │   └── registry.py         # Source metadata (1440 lines, 33 source types)
│   └── dlt_sources/            # ⚠ DO NOT MODIFY — vendored connectors (127 files)
│
├── transformation/             # Level 1 — dbt model generation & execution
│   ├── __init__.py             # run_dbt_models(), generate_dbt_docs()
│   └── generator.py            # DbtModelGenerator (577 lines)
│
├── oauth/                      # Level 1 — OAuth flows
│   ├── __init__.py             # OAuthManager
│   ├── providers.py            # Google, Facebook, Shopify providers (801 lines)
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
│   ├── db_health.py            # DuckDB health checks
│   ├── dbt_status.py           # dbt model status tracking
│   ├── data_validation.py      # Data validation utilities
│   └── env_file.py             # .env file parsing and serialization
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
| 1 (core) | Imports Level 0 only | `auth/`, `oauth/`, `ingestion/`, `transformation/` |
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
| `ingestion/dlt_runner.py` | 1759 | — (exempt, too risky) |
| `ingestion/sources/registry.py` | 1440 | — (metadata-only) |
| `cli/source_wizard.py` | 1324 | — |
| `visualization/metabase.py` | 1142 | — |
| `visualization/dashboard_manager.py` | 1113 | — |
| `cli/init.py` | 965 | — |
| `cli/commands/platform.py` | 941 | — (extracted from main.py by TASK-005) |
| `web/routes/auth.py` | ~854 | — (split evaluated in DOC-025: exempt, security-critical) |
| `cli/commands/oauth.py` | 815 | — (renamed from auth.py by TASK-093) |
| `oauth/providers.py` | 801 | — |
| `web/helpers.py` | 798 | — (extracted from app.py by TASK-085) |
| `ingestion/csv_loader.py` | 742 | — |
| `web/routes/upload.py` | 680 | — (extracted from app.py by TASK-085) |
| `platform/cloud/ssh.py` | 665 | — (SSH key mgmt, TOFU, exec/SFTP) |
| `cli/commands/remote.py` | 651 | — (remote group + push/rollback/firewall/domain) |
| `cli/validate.py` | 651 | — |
| `cli/commands/deploy_wizard.py` | 579 | — (interactive deploy wizard) |
| `transformation/generator.py` | 577 | — |
| `cli/commands/source.py` | 549 | — (extracted from main.py by TASK-005) |
| `cli/commands/deploy_provision.py` | 543 | — (provisioning orchestration) |
| `platform/cloud/digitalocean.py` | 542 | — (DO REST API v2 client) |
| `web/routes/users.py` | 527 | — (admin user CRUD) |
| `cli/model_wizard.py` | 507 | — |
| `platform/local/watcher.py` | 506 | — |
| `platform/cloud/scheduled_backup.py` | 505 | — (server-side scheduled backup) |

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

**Planned later phases:**
- `dango/notebooks/CLAUDE.md` (Phase 6)
- `dango/governance/CLAUDE.md` (Phase 7)

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

# Merge via API (avoids local checkout conflicts)
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
