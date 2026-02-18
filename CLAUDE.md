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
| Docker / file watcher | `dango/platform/` | `dango/platform/CLAUDE.md` (Phase 3) |
| Jinja2 templates / Dockerfiles | `dango/templates/` | [`dango/templates/CLAUDE.md`](dango/templates/CLAUDE.md) |
| Auth / users / sessions | `dango/auth/` | Not yet created (Phase 2) |
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
│   │   ├── auth.py             # auth group placeholder (Phase 2 user management)
│   │   ├── oauth.py            # oauth group + 10 subcommands (815 lines)
│   │   ├── config_cmd.py       # config group (validate/show)
│   │   ├── dashboard.py        # dashboard group (provision)
│   │   ├── data.py             # db group (status/clean) + validate
│   │   ├── metabase_cmd.py     # metabase group (save/load/refresh)
│   │   ├── model.py            # model group (add/remove)
│   │   ├── platform.py         # start/stop/status + port helpers (1026 lines)
│   │   ├── project.py          # init/rename/info
│   │   ├── source.py           # source group (add/list/remove) + sync (549 lines)
│   │   ├── transform.py        # run/docs/generate
│   │   └── web.py              # web dev server
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
├── web/                        # Level 2 — FastAPI web server
│   ├── app.py                  # Slim entry point (~109 lines) — registers routers
│   ├── models.py               # Pydantic request/response DTOs
│   ├── helpers.py              # Shared helpers: DuckDB queries, config, logging (798 lines)
│   ├── routes/                 # Route modules (extracted from app.py by TASK-085)
│   │   ├── __init__.py         # Package marker
│   │   ├── health.py           # /api/status, /api/watcher/status, /api/health/platform
│   │   ├── config.py           # /api/config, /api/metabase-config
│   │   ├── sources.py          # /api/sources, /api/sources/{name}/details
│   │   ├── sync.py             # /api/sources/{name}/sync + run_sync_task()
│   │   ├── logs.py             # /api/logs, /api/sources/{name}/logs
│   │   ├── dbt.py              # /api/dbt/models, /api/dbt/models/{name}/run + dbt docs proxy
│   │   ├── upload.py           # CSV upload/list/delete (680 lines)
│   │   ├── websocket.py        # ConnectionManager, ws_manager, /ws
│   │   ├── ui.py               # /, /health, /logs, /api, /api/docs, /api/redoc
│   │   └── metabase_proxy.py   # Metabase reverse proxy + SSO session
│   └── static/                 # Frontend HTML/CSS/JS
│
├── visualization/              # Level 2 — Metabase integration
│   ├── metabase.py             # Metabase API (1142 lines)
│   └── dashboard_manager.py    # Dashboard export/import (1113 lines)
│
├── platform/                   # Level 2 — Docker, network, file watcher
│   ├── __main__.py             # Platform CLI entry point
│   ├── docker.py               # Docker Compose lifecycle
│   ├── network.py              # Network utilities
│   ├── watcher.py              # File change detection
│   ├── watcher_lifecycle.py    # Watcher subprocess lifecycle (start/stop/status)
│   └── watcher_runner.py       # Watcher subprocess runner
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
│   └── router.py               # FastAPI OAuth callback endpoints
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
│   └── data_validation.py      # Data validation utilities
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
| 1 (core) | Imports Level 0 only | `oauth/`, `ingestion/`, `transformation/` |
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
| `cli/commands/platform.py` | 1026 | — (extracted from main.py by TASK-005) |
| `cli/init.py` | 965 | — |
| `cli/commands/oauth.py` | 815 | — (renamed from auth.py by TASK-093) |
| `oauth/providers.py` | 801 | — |
| `web/helpers.py` | 798 | — (extracted from app.py by TASK-085) |
| `ingestion/csv_loader.py` | 742 | — |
| `web/routes/upload.py` | 680 | — (extracted from app.py by TASK-085) |
| `cli/validate.py` | 651 | — |
| `transformation/generator.py` | 577 | — |
| `cli/commands/source.py` | 549 | — (extracted from main.py by TASK-005) |
| `cli/model_wizard.py` | 507 | — |
| `platform/watcher.py` | 506 | — |

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

**Planned later phases:**
- `dango/platform/CLAUDE.md` (Phase 3)
- `dango/auth/CLAUDE.md` (Phase 2)
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
