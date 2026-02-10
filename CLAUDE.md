# Dango

Data platform for small teams: **dlt** (ingestion) + **DuckDB** (warehouse) + **dbt** (transformation) + **Metabase** (visualization). One `pip install getdango` + `dango start` boots the full stack.

See [ARCHITECTURE.md](ARCHITECTURE.md) for system diagram, data flow, and cross-module workflows.

## Quick Routing Table

| Task Type | Go To | Read First |
|-----------|-------|------------|
| CLI commands | `dango/cli/` | `dango/cli/CLAUDE.md` (Phase 1) |
| Data ingestion / sync | `dango/ingestion/` | [`dango/ingestion/CLAUDE.md`](dango/ingestion/CLAUDE.md) |
| OAuth / token flows | `dango/oauth/` | [`dango/oauth/CLAUDE.md`](dango/oauth/CLAUDE.md) |
| Web UI / API endpoints | `dango/web/` | `dango/web/CLAUDE.md` (Phase 1) |
| Config loading / models | `dango/config/` | [`dango/config/CLAUDE.md`](dango/config/CLAUDE.md) |
| Dashboards / Metabase | `dango/visualization/` | [`dango/visualization/CLAUDE.md`](dango/visualization/CLAUDE.md) |
| dbt / transformations | `dango/transformation/` | [`dango/transformation/CLAUDE.md`](dango/transformation/CLAUDE.md) |
| Token encryption / keychain | `dango/security/` | [`dango/security/CLAUDE.md`](dango/security/CLAUDE.md) |
| Shared utilities | `dango/utils/` | [`dango/utils/CLAUDE.md`](dango/utils/CLAUDE.md) |
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
├── cli/                        # Level 3 — Click CLI (primary user interface)
│   ├── main.py                 # ⚠ 3927 lines — refactored in TASK-005
│   ├── init.py                 # Project initialization wizard
│   ├── wizard.py               # Interactive setup wizards
│   ├── source_wizard.py        # Source configuration wizard
│   ├── model_wizard.py         # dbt model creation wizard
│   ├── oauth.py                # OAuth setup commands
│   ├── utils.py                # CLI utilities
│   ├── db_helpers.py           # Database inspection helpers
│   ├── env_helpers.py          # Environment setup helpers
│   ├── schema_manager.py       # Schema management
│   └── validate.py             # Validation commands
│
├── web/                        # Level 2 — FastAPI web server
│   ├── app.py                  # ⚠ 2900 lines — refactored in TASK-085
│   └── static/                 # Frontend HTML/CSS/JS
│
├── visualization/              # Level 2 — Metabase integration
│   ├── metabase.py             # Metabase API (1207 lines)
│   └── dashboard_manager.py    # Dashboard export/import (1102 lines)
│
├── platform/                   # Level 2 — Docker, network, file watcher
│   ├── __main__.py             # Platform CLI entry point
│   ├── docker.py               # Docker Compose lifecycle
│   ├── network.py              # Network utilities
│   ├── watcher.py              # File change detection
│   └── watcher_runner.py       # Watcher subprocess runner
│
├── ingestion/                  # Level 1 — Data loading
│   ├── dlt_runner.py           # ⚠ 1696 lines — orchestrates full sync pipeline
│   ├── csv_loader.py           # CSV loading with dedup (761 lines)
│   ├── sources/
│   │   └── registry.py         # Source metadata (1440 lines, 34 source types)
│   └── dlt_sources/            # ⚠ DO NOT MODIFY — vendored connectors (127 files)
│
├── transformation/             # Level 1 — dbt model generation & execution
│   ├── __init__.py             # run_dbt_models(), generate_dbt_docs()
│   └── generator.py            # DbtModelGenerator (560 lines)
│
├── oauth/                      # Level 1 — OAuth flows
│   ├── __init__.py             # OAuthManager
│   ├── providers.py            # Google, Facebook, Shopify providers (761 lines)
│   ├── storage.py              # Credential CRUD in .dlt/secrets.toml
│   └── router.py               # FastAPI OAuth callback endpoints
│
├── config/                     # Level 0 — Configuration & credentials
│   ├── models.py               # Pydantic models (DangoConfig, DataSource, etc.)
│   ├── loader.py               # ConfigLoader — loads project.yml, sources.yml
│   ├── credentials.py          # CredentialManager — manages .dlt/secrets.toml, .env
│   └── exceptions.py           # Config-specific exceptions
│
├── utils/                      # Level 0 — Shared utilities
│   ├── dbt_lock.py             # DbtLock — single-writer DuckDB serialization
│   ├── activity_log.py         # Append-only JSON activity log
│   ├── sync_history.py         # Per-source sync results
│   ├── database.py             # Schema creation helpers
│   ├── db_health.py            # DuckDB health checks
│   ├── dbt_status.py           # dbt model status tracking
│   └── data_validation.py      # Data validation utilities
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
| 0 (base) | No dango imports | `config/`, `utils/`, `security/`, `templates/` |
| 1 (core) | Imports Level 0 only | `oauth/`, `ingestion/`, `transformation/` |
| 2 (platform) | Imports Level 0–1 | `platform/`, `web/`, `visualization/` |
| 3 (ui) | Imports any level | `cli/` |

Imports flow downward only. Same-level imports are allowed if non-circular. See `ARCHITECTURE.md` §3 for full rules.

### Single-Writer DuckDB

DuckDB allows only one writer process at a time. All write operations are serialized through `DbtLock` in `utils/dbt_lock.py` (file-based lock at `.dango/state/dbt.lock`). Concurrent reads during writes are allowed. See `ARCHITECTURE.md` §5.

### Monolithic Files

These files exceed 500 lines. Two have planned refactoring; the rest are exempt (stable MVP code).
Full exemption registry: [`docs/file-exemptions.yml`](docs/file-exemptions.yml)

| File | Lines | Refactoring Task |
|------|-------|-----------------|
| `cli/main.py` | 3927 | TASK-005 (split into `cli/commands/`) |
| `web/app.py` | 2900 | TASK-085 (split into `web/routes/`) |
| `ingestion/dlt_runner.py` | 1696 | — (exempt, too risky) |
| `ingestion/sources/registry.py` | 1440 | — (metadata-only) |
| `cli/source_wizard.py` | 1225 | — |
| `visualization/metabase.py` | 1207 | — |
| `visualization/dashboard_manager.py` | 1102 | — |
| `cli/init.py` | 945 | — |
| `cli/utils.py` | 780 | — |
| `oauth/providers.py` | 761 | — |
| `ingestion/csv_loader.py` | 761 | — |
| `cli/validate.py` | 677 | — |
| `transformation/generator.py` | 560 | — |
| `platform/watcher.py` | 531 | — |
| `cli/model_wizard.py` | 517 | — |
| `platform/network.py` | 509 | — |

## Module Documentation Index

Module CLAUDE.md files provide per-module navigation, public API, and patterns.

**Existing:**
- [`dango/config/CLAUDE.md`](dango/config/CLAUDE.md)
- [`dango/ingestion/CLAUDE.md`](dango/ingestion/CLAUDE.md)
- [`dango/oauth/CLAUDE.md`](dango/oauth/CLAUDE.md)
- [`dango/transformation/CLAUDE.md`](dango/transformation/CLAUDE.md)
- [`dango/visualization/CLAUDE.md`](dango/visualization/CLAUDE.md)
- [`dango/security/CLAUDE.md`](dango/security/CLAUDE.md)
- [`dango/utils/CLAUDE.md`](dango/utils/CLAUDE.md)
- [`dango/templates/CLAUDE.md`](dango/templates/CLAUDE.md)

**Planned Phase 1:**
- `dango/cli/CLAUDE.md` (after TASK-005)
- `dango/web/CLAUDE.md` (after TASK-085)

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

# Subsequent sessions
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
