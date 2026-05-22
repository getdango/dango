# Dango Architecture

## 1. Overview

Dango is an open-source data platform for small teams that integrates four production-grade tools into a single `pip install` + `dango start` workflow:

- **dlt** (data load tool) for ingestion from 34 source types
- **DuckDB** as the embedded analytical warehouse
- **dbt** for SQL-based data transformation
- **Metabase** for dashboards and visualization

This document describes the **target v1 architecture**. Not-yet-implemented features are annotated with their target phase (e.g., "(Phase 2)"). The audience is developers and LLMs working on the codebase.

## 2. System Diagram

```
                            ┌─────────────────────────────────────────────┐
                            │              User Entry Points              │
                            │                                             │
                            │  CLI Terminal    Web UI    File Watcher     │
                            │  (click)         :8800     (watchdog)       │
                            └──────┬───────────┬──────────┬───────────────┘
                                   │           │          │
              ┌────────────────────┴───────────┴──────────┴────────────────┐
              │                     Level 3 — UI                           │
              │                                                            │
              │   cli/                                                     │
              │   (cli/commands/)                                          │
              └──────────┬─────────────────────────────────────────────────┘
                         │
              ┌──────────┴─────────────────────────────────────────────────┐
              │                  Level 2 — Platform                        │
              │                                                            │
              │   platform/        web/            visualization/          │
              │   (Docker,         (web/routes/    (Metabase)              │
              │    watcher)         web/middleware/)                       │
              └──────────┬────────────────┬────────────────────────────────┘
                         │                │
              ┌──────────┴────────────────┴────────────────────────────────┐
              │                   Level 1 — Core                           │
              │                                                            │
              │   ingestion/       transformation/      oauth/             │
              │   (dlt, CSV)       (dbt)                (Google,           │
              │                                          Facebook,         │
              │                    auth/                  Shopify)          │
              │                    (users, sessions,                       │
              │                     RBAC, 2FA, audit)   governance/        │
              │                    notebooks/            (drift, PII)      │
              │                    (Marimo)              analysis/          │
              │                                         (metrics)          │
              └──────────┬────────────────┬────────────────────────────────┘
                         │                │
              ┌──────────┴────────────────┴────────────────────────────────┐
              │                  Level 0 — Base                            │
              │                                                            │
              │   config/     utils/      security/    templates/          │
              │   migrations/                                              │
              │   exceptions.py    logging.py                              │
              └───────────────────────────────────────────────────────────┘
                                        │
              ┌─────────────────────────┴──────────────────────────────────┐
              │                  External Services                         │
              │                                                            │
              │  data/*.duckdb       Metabase           dbt CLI            │
              │  (DuckDB file)       (Docker :3000)     (subprocess)       │
              │                      nginx (Docker      SaaS APIs          │
              │                       :8081 dbt docs)   (Google, Stripe…)  │
              └───────────────────────────────────────────────────────────┘
```

## 3. Module Dependency Hierarchy

| Level | Role | Modules |
|-------|------|---------|
| 0 (base) | No dango imports | `config/`, `utils/`, `security/`, `migrations/`, `templates/`, `logging.py`, `exceptions.py` |
| 1 (core) | Imports Level 0 only | `oauth/`, `ingestion/`, `transformation/`, `auth/`, `governance/`, `notebooks/`, `analysis/` |
| 2 (platform) | Imports Level 0-1 | `platform/`, `web/`, `visualization/` |
| 3 (ui) | Imports any level | `cli/` |

**Three rules govern imports:**

1. **Downward only.** Higher levels import lower levels. Never reverse.
2. **Same-level OK if non-circular.** For example, `transformation/` can import `ingestion/sources/registry` to look up source metadata, as long as `ingestion/` never imports `transformation/` at module level.
3. **Lazy imports for orchestration.** `dlt_runner.py` contains lazy imports from `transformation/` and `visualization/` inside function bodies (lines 1602, 1640, 1659, 1669). This is a documented pragmatic concession for the sync orchestration flow — not a pattern to follow elsewhere.

## 4. Module Reference

### Level 0 — Base

#### `config/`
**Responsibility:** Project configuration loading, validation, and credential management via Pydantic models.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports all public symbols |
| `models.py` | Pydantic models: `DangoConfig`, `ProjectContext`, `SourcesConfig`, `DataSource`, `SourceType` (33 types), `DeduplicationStrategy`, `PlatformSettings`, source-specific configs |
| `loader.py` | `ConfigLoader` — finds project root, loads/saves `.dango/project.yml` and `.dango/sources.yml` |
| `credentials.py` | `CredentialManager` — manages `.dlt/secrets.toml` and `.env` files |

**Public API:** `DangoConfig`, `ProjectContext`, `SourcesConfig`, `DataSource`, `SourceType`, `DeduplicationStrategy`, `ConfigLoader`, `get_config()`, `ConfigError`, `ConfigNotFoundError`, `ConfigValidationError`, `ProjectNotFoundError`

**Imports from:** None (Level 0). Uses pydantic, yaml, pathlib.

---

#### `utils/`
**Responsibility:** Shared utilities — DuckDB write serialization, activity logging, sync history, database helpers.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports public symbols |
| `dbt_lock.py` | `DbtLock` — file-based inter-process lock preventing concurrent DuckDB writes (fcntl on Unix, msvcrt on Windows). Detects stale locks via PID checking. |
| `activity_log.py` | `log_activity()` — append-only JSON activity log |
| `sync_history.py` | `save_sync_history_entry()`, `load_sync_history()` — per-source sync results |
| `database.py` | `ensure_dbt_schemas()` — creates raw/staging/intermediate/marts schemas |
| `db_health.py` | `check_disk_space()`, `check_duckdb_health()`, `get_disk_usage_summary()`, `get_component_disk_usage()` |
| `dbt_status.py` | `get_model_statuses()`, `update_model_status()` |
| `log_rotation.py` | `rotate_jsonl_log()`, `cleanup_old_archives()`, `get_log_disk_usage()` |
| `data_validation.py` | Data validation utilities |

**Public API:** `DbtLock`, `DbtLockError`, `dbt_lock()`, `log_activity()`, `save_sync_history_entry()`, `load_sync_history()`, `ensure_dbt_schemas()`

**Imports from:** None (Level 0). Uses psutil, fcntl/msvcrt.

---

#### `security/`
**Responsibility:** Optional encryption for OAuth tokens using OS keychain + Fernet symmetric encryption.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports `SecureTokenStorage` |
| `token_storage.py` | `SecureTokenStorage` — master key in OS keychain (macOS Keychain / Windows Credential Manager / Linux Secret Service), Fernet encryption for token data in `.dlt/secrets.toml` |

**Public API:** `SecureTokenStorage`

**Imports from:** None (Level 0).

---

#### `templates/`
**Responsibility:** Jinja2 templates and Dockerfiles used during project initialization and model generation.

| File | Description |
|------|-------------|
| `docker-compose.yml.j2` | Metabase + dbt-docs containers, volumes, healthchecks |
| `Dockerfile.metabase` | Custom Metabase image with DuckDB driver (Debian-based, not Alpine) |
| `nginx.conf.j2` | Reverse proxy for dbt docs serving |
| `dbt/sources.yml.j2` | dbt source documentation per data source |
| `dbt/staging_model.sql.j2` | Staging model SQL with dedup strategy support |
| `dbt/staging_schema.yml.j2` | Schema YAML for staging models |

**Public API:** Templates consumed by `cli/init.py` and `transformation/generator.py`.

**Imports from:** None (Level 0).

---

#### `migrations/`
**Responsibility:** Database migration framework for managing schema changes across Dango versions.

| File | Description |
|------|-------------|
| `__init__.py` | Public API: `apply_all_pending()`, `get_all_status()` |
| `runner.py` | `MigrationRunner`, `MigrationInfo`, `MigrationStatus` — discovers, tracks, and applies migration scripts |

**Public API:** `apply_all_pending(project_root)`, `get_all_status(project_root)`

**Imports from:** None (Level 0). Uses pathlib, importlib.

---

#### `logging.py`
**Responsibility:** Structured logging infrastructure (structlog wrapping stdlib). JSON to rotating log file, human-readable to stderr. Correlation ID support via contextvars.

**Public API:** `configure_logging()`, `get_logger()`, `bind_contextvars`, `clear_contextvars`, `unbind_contextvars`

**Imports from:** None (Level 0). Uses structlog + stdlib only.

---

#### `exceptions.py`
**Responsibility:** Centralized exception hierarchy for consistent error handling across modules.

**Public API:** `DangoError` base class and domain-specific subclasses: `ConfigError`, `IngestionError`, `InfrastructureError`, `MigrationError`, `OAuthError`, `ValidationError`, `WebAPIError`, and their specializations (e.g., `DbtLockError`, `OAuthTokenExpiredError`, `CSVSchemaMismatchError`).

**Imports from:** None (Level 0).

### Level 1 — Core

#### `ingestion/`
**Responsibility:** Data loading from 34 source types into DuckDB via dlt pipelines and CSV loader.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports `DltPipelineRunner`, `run_sync`, `CSVLoader`, `SOURCE_REGISTRY` |
| `dlt_runner.py` | `DltPipelineRunner` — orchestrates sync: load data, generate staging models, run dbt, refresh Metabase (1796 lines). Contains lazy imports from Level 1-2 modules. |
| `csv_loader.py` | `CSVLoader` — incremental CSV loading with 4 dedup strategies, file metadata tracking |
| `sources/registry.py` | `SOURCE_REGISTRY` — metadata dict for all 34 sources (auth type, params, setup guide, cost warnings) |
| `dlt_sources/` | 29 vendored dlt source integrations (third-party code, rarely modified) |

**Public API:** `DltPipelineRunner`, `run_sync()`, `CSVLoader`, `SOURCE_REGISTRY`, `CATEGORIES`, `get_source_metadata()`

**Imports from:** `config/` (Level 0), `utils/` (Level 0). Lazy imports inside function bodies: `transformation/` (Level 1), `visualization/` (Level 2), `oauth/storage` (Level 1).

---

#### `transformation/`
**Responsibility:** dbt model generation and execution.

| File | Description |
|------|-------------|
| `__init__.py` | `run_dbt_models()`, `generate_dbt_docs()` — subprocess calls to dbt CLI with 5-min/60s timeouts |
| `generator.py` | `DbtModelGenerator` — auto-generates `stg_*.sql` models and `sources.yml` per data source. Supports 4 dedup strategies (none, latest_only, append_only, scd_type2). |

**Public API:** `run_dbt_models(project_root, select)`, `generate_dbt_docs(project_root)`

**Imports from:** `config/` (Level 0), `utils/` (Level 0), `ingestion/sources/registry` (Level 1, same-level).

---

#### `oauth/`
**Responsibility:** OAuth flows for Google, Facebook, and Shopify data sources.

| File | Description |
|------|-------------|
| `__init__.py` | `OAuthManager` — manages OAuth flows, credential persistence |
| `providers.py` | `GoogleOAuthProvider`, `FacebookOAuthProvider` — provider-specific OAuth implementations |
| `storage.py` | `OAuthStorage`, `OAuthCredential` — credential CRUD in `.dlt/secrets.toml` |
| `router.py` | FastAPI OAuth callback endpoints |

**Public API:** `OAuthManager`, `OAuthCallbackHandler`, `create_oauth_manager()`

**Imports from:** `config/credentials` (Level 0).

---

#### `auth/`
**Responsibility:** User authentication and access control — password login with optional TOTP 2FA, OAuth social login (Google + GitHub), cookie/API key sessions, RBAC (3 roles, 29 permissions), invite-based onboarding, Metabase SSO bridging, brute-force lockout, and audit logging.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports 96 public symbols |
| `models.py` | Pydantic models: `Role`, `User`, `UserCreate`, `UserUpdate`, `UserResponse`, `Session`, `APIKey` |
| `database.py` | SQLite CRUD (WAL mode, FK enforcement) — user, session, API key operations |
| `security.py` | Pure crypto: bcrypt hashing (pwdlib), token generation, password strength checks |
| `sessions.py` | High-level session + API key lifecycle with timeout validation |
| `permissions.py` | 29 permissions across 9 domains, 3 role mappings, `require_permission()` FastAPI Depends |
| `lockout.py` | Brute-force protection: 5 attempts / 15-min lockout window |
| `audit.py` | 22 event types to `.dango/logs/audit.jsonl` (append-only JSONL) |
| `admin.py` | Bootstrap admin user, auth config path helpers |
| `totp.py` | TOTP 2FA: setup/verify/enable/disable, 8 recovery codes |
| `oauth_login.py` | OAuth provider ABC + Google/GitHub implementations |
| `metabase_sync.py` | Sync users/roles to Metabase (Fernet-encrypted passwords) |
| `metabase_bridge.py` | Async SSO session bridging on login/logout |

**Public API:** `create_session()`, `validate_session()`, `require_permission()`, `has_permission()`, `hash_password()`, `verify_password()`, `AuditEvent`, `log_auth_event()`, `bridge_metabase_login()`, `bridge_metabase_logout()`, `sync_user_to_metabase()`, `Role`, `User`, `Session`, `APIKey`

**Imports from:** `config/` (Level 0), `security/` (Level 0), `logging` (Level 0), `exceptions` (Level 0).

#### `governance/`
**Responsibility:** Data governance — schema drift detection and PII scanning. Monitors DuckDB warehouse schemas for changes, records drift events, scans string columns for PII, and alerts via webhooks.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports public API |
| `models.py` | Pydantic V2 response models: `DriftEvent`, `DriftResponse`, `PiiFinding`, `PiiResponse` |
| `schema_drift.py` | Schema drift detection engine (490 lines): `detect_drift_for_sources()`, `detect_table_drift()`, `get_drift_history()` |
| `pii_detector.py` | PII scanning engine using Presidio + spaCy (454 lines): `scan_sources_for_pii()`, `scan_table_for_pii()`, `get_pii_findings()` |

**Public API:** `detect_drift_for_sources()`, `detect_table_drift()`, `get_drift_history()`, `scan_sources_for_pii()`, `scan_table_for_pii()`, `get_pii_findings()`

**Imports from:** `utils/dango_db` (Level 0), `logging` (Level 0), `platform/notifications/` (Level 2 — lazy import for webhooks).

---

#### `notebooks/`
**Responsibility:** Marimo notebook server lifecycle, DuckDB read-only snapshots, file-level locking, and HTTP/WebSocket reverse proxy.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports public symbols |
| `manager.py` | Marimo process lifecycle: start/stop/status (197 lines) |
| `locking.py` | File-level notebook locking via SQLite `notebook_locks` table (249 lines) |
| `snapshot.py` | DuckDB snapshot management: create, list, cleanup |
| `proxy.py` | HTTP + WebSocket reverse proxy to Marimo (186 lines) |
| `templates/` | Starter templates: explore, quality, blank |

**Public API:** `start_marimo()`, `stop_marimo()`, `get_marimo_status()`, `acquire_lock()`, `release_lock()`, `create_snapshot()`, `proxy_to_marimo()`, `proxy_websocket_to_marimo()`

**Imports from:** `utils/` (Level 0), `config/` (Level 0).

---

#### `analysis/`
**Responsibility:** Automated metric monitoring and comparison engine. Executes user-defined SQL metrics against DuckDB, stores results in SQLite, and compares values against historical baselines.

| File | Description |
|------|-------------|
| `__init__.py` | Public API re-exports |
| `models.py` | Pydantic V2 models: `MetricConfig`, `ComparisonResult`, `AnalysisResult` |
| `config.py` | YAML config load/save for `.dango/metrics.yml` |
| `comparisons.py` | Comparison engine + trend detection |
| `drilldown.py` | GROUP BY breakdown + contributor ranking |
| `metrics.py` | Orchestration: execute → store → compare → drill-down |
| `templates.py` | Pre-built metric templates for common sources |
| `formatter.py` | Result categorization + display formatting |

**Public API:** `run_analysis()`, `load_metrics_config()`, `save_metrics_config()`, `generate_metrics_for_source()`

**Imports from:** `utils/dango_db` (Level 0), `logging` (Level 0), `exceptions` (Level 0).

---

### Level 2 — Platform

#### `platform/`
**Responsibility:** Docker container management, network utilities, and file watcher for auto-sync.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports `DockerManager`, `ServiceStatus` |
| `docker.py` | `DockerManager` — Docker Compose lifecycle (up, down, status, logs, pull) |
| `network.py` | Network utilities (port checking) |
| `watcher.py` | File change detection (watchdog-based) |
| `watcher_runner.py` | Watcher subprocess runner — monitors CSV changes, triggers sync |
| `__main__.py` | Platform CLI entry point |

**Public API:** `DockerManager`, `ServiceStatus` (enum: RUNNING, STOPPED, UNHEALTHY, STARTING, UNKNOWN)

**Imports from:** `config/` (Level 0), `utils/` (Level 0). `watcher_runner.py` also lazy-imports `transformation/` (Level 1).

---

#### `visualization/`
**Responsibility:** Metabase dashboard provisioning and git-based dashboard workflow.

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports `provision_dashboard`, `create_pipeline_health_dashboard` |
| `metabase.py` | Metabase API integration — auto-setup, database connection, schema sync, dashboard provisioning |
| `dashboard_manager.py` | `DashboardManager` — export/import dashboards and questions as YAML files |

**Public API:** `provision_dashboard()`, `create_pipeline_health_dashboard()`

**Imports from:** None (self-contained — receives `project_root` and `metabase_url` as parameters).

### Level 3 — UI

#### `cli/`
**Responsibility:** Click-based CLI — the primary user interface. All commands defined here.

| File | Description |
|------|-------------|
| `main.py` | Slim entry point (~109 lines) — registers command groups from `commands/` subpackage |
| `commands/` | Command modules extracted from main.py by TASK-005: `platform.py` (start/stop/status), `source.py` (add/list/remove/sync), `oauth.py` (OAuth credential management), `auth.py` (user auth placeholder, Phase 2), `project.py` (init/rename/info), `transform.py` (run/docs/generate), etc. |
| `init.py` | Project initialization wizard — creates directory structure, config files, Docker setup |
| `wizard.py` | Interactive setup wizards |
| `source_wizard.py` | Source configuration wizard |
| `model_wizard.py` | dbt model creation wizard |
| `oauth.py` | OAuth setup commands |
| `utils.py` | CLI display helpers (`print_error`, `print_success`, `require_project_context`) |
| `helpers/port_manager.py` | Port checking utilities |
| `helpers/process_manager.py` | FastAPI server process management + PID file helpers |
| `db_helpers.py` | Database inspection helpers |
| `env_helpers.py` | Environment setup helpers |
| `schema_manager.py` | Schema management |
| `validate.py` | Validation commands |

**Public API:** `cli` (Click group entry point via `dango` console script)

**Imports from:** All levels — `config/`, `utils/`, `ingestion/`, `transformation/`, `visualization/`, `oauth/`, `platform/`.

---

#### `web/`
**Responsibility:** FastAPI web server — REST API, WebSocket, Metabase reverse proxy, auth middleware, static UI.

| File | Description |
|------|-------------|
| `app.py` | Entry point (~370 lines) — creates FastAPI app, registers routers + middleware, admin bootstrap |
| `helpers.py` | Shared helpers: DuckDB queries, config loading, service health checks, log management |
| `models.py` | Pydantic request/response DTOs (incl. auth DTOs: `LoginRequest`, `AcceptInviteRequest`, etc.) |
| `middleware/auth.py` | Session/API key auth + CSRF check on every request (~325 lines) |
| `middleware/rate_limit.py` | Rate limiting: login 10/min, API 200/min, localhost exempt (~212 lines) |
| `routes/auth.py` | Login/logout, password change, OAuth flows, invite accept, API key CRUD (~854 lines) |
| `routes/auth_2fa.py` | TOTP 2FA setup/verify/disable/recovery (~328 lines) |
| `routes/users.py` | Admin user CRUD: create, edit, deactivate, delete, unlock, invite (525 lines) |
| `routes/` (data) | `health.py`, `config.py`, `sources.py`, `sync.py`, `logs.py`, `dbt.py`, `upload.py`, `websocket.py`, `ui.py`, `metabase_proxy.py` |

**Public API:** FastAPI `app` instance.

**Imports from:** `config/` (Level 0), `utils/` (Level 0), `auth/` (Level 1), `ingestion/` (Level 1), `transformation/` (Level 1), `visualization/` (Level 2).

## 5. Data Flow

```
  Data Sources              Ingestion               Warehouse            Transformation         Visualization
 ┌─────────────┐         ┌─────────────┐         ┌──────────────┐      ┌──────────────┐      ┌─────────────┐
 │ SaaS APIs   │         │             │         │              │      │              │      │             │
 │ (Stripe,    │──dlt──→ │ dlt_runner  │──────→  │  raw schema  │      │              │      │             │
 │  Google,    │         │             │         │  raw_{src}   │──→   │  dbt run     │──→   │  Metabase   │
 │  HubSpot…) │         └─────────────┘         │              │      │              │      │  (Docker    │
 │             │                                 │ _dlt_loads   │      │  staging/    │      │   :3000)    │
 └─────────────┘         ┌─────────────┐         │ _dlt_state   │      │  intermediate│      │             │
 ┌─────────────┐         │             │         │              │      │  marts/      │      │             │
 │ CSV files   │──────→  │ csv_loader  │──────→  │              │      │              │      │             │
 │ (uploads/)  │         │             │         │              │      │              │      │             │
 └─────────────┘         └─────────────┘         └──────────────┘      └──────────────┘      └─────────────┘
                                                  data/*.duckdb                                :8800 Web UI
```

**Schema naming conventions:**
- **Raw:** `raw_{source_name}.{table_name}` (e.g., `raw_stripe.customers`)
- **Staging:** `staging.stg_{source_name}__{table_name}` (auto-generated by `DbtModelGenerator`)
- **Intermediate:** `intermediate.int_{description}` (user-created)
- **Marts:** `marts.fct_*`, `marts.dim_*`, `marts.agg_*` (user-created)

**dlt internal tables** (in each raw schema): `_dlt_loads` (load metadata with timestamps), `_dlt_pipeline_state` (pipeline checkpoint state). These are hidden from Metabase.

**Single-writer constraint (VAL-003):** DuckDB allows only one writer process at a time. All write operations are serialized through `DbtLock` (file-based lock at `.dango/state/dbt.lock`). Concurrent reads during writes are allowed.

## 6. Cross-Module Workflows

### 6.1 Data Sync (CLI)

```
User runs: dango sync [--source xyz]

cli/commands/source.py @click.command("sync")
  → utils/dbt_lock.py: dbt_lock() — acquire write lock
  → ingestion/dlt_runner.py: run_sync(project_root, source_names)
      → config/loader.py: ConfigLoader.load_config() — load sources.yml
      → For each source:
          → dlt_sources/* or csv_loader.py — load into DuckDB raw schema
          → utils/sync_history.py: save_sync_history_entry()
      → transformation/generator.py: DbtModelGenerator.generate_all_models() [lazy import]
      → transformation/__init__.py: run_dbt_models() [lazy import]
      → transformation/__init__.py: generate_dbt_docs() [lazy import]
      → visualization/metabase.py: refresh_metabase_connection() [lazy import]
  → utils/dbt_lock.py: release lock
```

### 6.2 Project Initialization

```
User runs: dango init

cli/commands/project.py @click.command("init")
  → cli/wizard.py — interactive project setup
  → cli/init.py — create directory structure:
      .dango/, data/, data/uploads/, custom_sources/, dbt/models/...
  → config/loader.py: ConfigLoader — write project.yml, sources.yml
  → templates/docker-compose.yml.j2 — render Docker config
  → templates/Dockerfile.metabase — copy Metabase Dockerfile
  → utils/database.py: ensure_dbt_schemas() — create DuckDB schemas
  → dbt project setup (dbt_project.yml, profiles.yml, macros/)
```

### 6.3 OAuth Source Setup

```
User runs: dango oauth setup <provider>

cli/oauth.py
  → cli/source_wizard.py — interactive source config
  → oauth/__init__.py: OAuthManager.start_oauth_flow()
      → oauth/providers.py: GoogleOAuthProvider.authenticate()
      → Local HTTP server for OAuth callback
  → oauth/storage.py: OAuthStorage.save_credential()
  → config/credentials.py: CredentialManager — write to .dlt/secrets.toml
```

### 6.4 Metabase Provisioning

```
User runs: dango start (first time)

cli/commands/platform.py @click.command("start")
  → platform/docker.py: DockerManager.up() — start containers
  → visualization/metabase.py — auto-setup:
      → Create admin account (random password)
      → Configure DuckDB database connection
      → Create default collections
      → Store credentials in .dango/metabase.yml
```

### 6.5 File Watcher Auto-Sync

```
CSV file dropped in data/uploads/

platform/watcher.py — detect file change (watchdog)
  → platform/watcher_runner.py — debounce (600s default)
  → utils/dbt_lock.py: dbt_lock() — acquire write lock
  → ingestion/dlt_runner.py: run_sync()
      → ingestion/csv_loader.py: CSVLoader.load_all_csv_files()
  → transformation/__init__.py: run_dbt_models()
  → release lock
```

### 6.6 Web API Sync Trigger

```
POST /api/sources/{source_name}/sync

web/routes/sync.py @router.post("/api/sources/{source_name}/sync")
  → utils/dbt_lock.py: dbt_lock() — acquire write lock
  → ingestion/dlt_runner.py: run_sync() (in BackgroundTask)
  → WebSocket /ws — broadcast progress updates to connected clients
  → utils/sync_history.py: save_sync_history_entry()
  → release lock
```

### 6.7 Adding a New Source Type

```
Extension workflow (developer adds a new data source):

1. ingestion/sources/registry.py — add entry to SOURCE_REGISTRY dict
   (display_name, category, auth_type, dlt_package, required_params)
2. oauth/providers.py — add OAuth provider class (if OAuth source)
3. ingestion/dlt_sources/ — add or vendor dlt source module
4. config/models.py — add source-specific config class (optional)
   CLI auto-discovers new sources from the registry.
```

### 6.8 Password Login Flow

```
POST /api/auth/login

web/routes/auth.py @router.post("/api/auth/login")
  → auth/lockout.py: check_account_locked() — 423 if locked
  → auth/database.py: get_user_by_email() — 400 if not found (with timing equalization)
  → auth/security.py: verify_password() — 400 if wrong + record_failed_login()
  → Check is_active — 400 if inactive
  → If totp_enabled:
      → auth/sessions.py: create_session(is_partial=True) — 200 {requires_2fa}
      → User calls POST /api/auth/2fa/verify with TOTP code
      → auth/sessions.py: create_session() — upgrade to full session
  → Else:
      → auth/sessions.py: create_session() — full session
  → auth/lockout.py: reset_failed_logins()
  → web/routes/auth.py: _bridge_metabase_session() — set metabase.SESSION cookie
  → 200 + Set-Cookie: dango_session
```

### 6.9 Metabase SSO Bridge

```
On login (password, OAuth, or 2FA verify):

web/routes/auth.py: _bridge_metabase_session()
  → auth/metabase_bridge.py: bridge_metabase_login(user, project_root)
      → auth/metabase_sync.py: decrypt_metabase_password() — Fernet via SecureTokenStorage
      → POST {metabase_url}/api/session — authenticate as the Dango user
      → Return Metabase session ID
  → Set-Cookie: metabase.SESSION (HttpOnly, SameSite=Lax, Secure)

On logout:
  → auth/metabase_bridge.py: bridge_metabase_logout(metabase_token, project_root)
      → DELETE {metabase_url}/api/session — destroy Metabase session
  → Delete metabase.SESSION cookie

On Metabase proxy 401 (stale session):
  → web/routes/metabase_proxy.py: re-bridge automatically using stored credentials
```

### 6.10 Post-Sync Hooks

```
After each sync completes (in dlt_runner.py run_sync):

ingestion/dlt_runner.py: run_sync()
  → utils/post_sync.py: dispatch_post_sync_hooks(project_root, sources)
      → _run_profiling(project_root, sources)
          → Column stats (row count, null %, distinct count) cached in dango.db
      → _run_drift_detection(project_root, sources)
          → governance/schema_drift.py: detect_drift_for_sources()
          → Drift events stored in dango.db, webhook if configured
      → _run_pii_scan(project_root, sources)
          → governance/pii_detector.py: scan_sources_for_pii()
          → PII findings stored in dango.db, webhook if configured
      → _run_analysis(project_root, sources)
          → analysis/metrics.py: run_analysis()
          → Metric results stored in dango.db

Each hook is wrapped in try/except — failures are logged but never propagate.
```

## 7. Database Schemas

### DuckDB Warehouse (`data/{project}.duckdb`)

| Schema | Purpose | Created by |
|--------|---------|------------|
| `raw` / `raw_{source}` | Untransformed source data | dlt pipelines, CSVLoader |
| `staging` | Clean, deduplicated, renamed columns | dbt (auto-generated models) |
| `intermediate` | Reusable business logic joins | dbt (user-created models) |
| `marts` | Final business metrics and dimensions | dbt (user-created models) |

Internal tables per raw schema: `_dlt_loads`, `_dlt_pipeline_state`. CSV sources also have `_dango_file_metadata` for tracking processed files.

### auth.db (`.dango/auth.db`)
SQLite database (WAL mode, FK enforcement) for user authentication state. Managed by the `auth/` module, schema applied via `migrations/auth/`.

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `users` | User accounts | `id`, `email`, `password_hash`, `role` (admin/editor/viewer), `is_active`, `totp_secret`, `totp_enabled`, `failed_login_attempts`, `locked_until`, `invite_token_hash`, `metabase_user_id`, `metabase_password_enc` |
| `sessions` | Active login sessions | `id`, `user_id` (FK), `token_hash`, `ip_address`, `user_agent`, `is_partial`, `created_at`, `last_activity`, `expires_at` |
| `api_keys` | API key authentication | `id`, `user_id` (FK), `name`, `key_hash`, `key_prefix`, `is_active`, `last_used_at` |
| `recovery_codes` | TOTP 2FA backup codes | `id`, `user_id` (FK), `code_hash`, `is_used` |
| `_migration_history` | Schema version tracking | `name`, `applied_at` |

### Metabase Internal
H2 database managed by the Metabase Docker container. Not directly modified by Dango — interaction is via Metabase REST API only.

## 8. Configuration Hierarchy

| File | Purpose | Sensitive | Git |
|------|---------|-----------|-----|
| `.dango/project.yml` | Project metadata, platform settings | No | Committed |
| `.dango/sources.yml` | Source definitions and type-specific config | No | Committed |
| `schedules.yml` (Phase 3) | Sync schedules | No | Committed |
| `.env` | API keys, environment variables | Yes | Gitignored |
| `.dlt/secrets.toml` | OAuth tokens, dlt credentials | Yes | Gitignored |
| `.dango/metabase.yml` | Auto-generated Metabase admin credentials | Yes | Gitignored |
| `dbt/profiles.yml` | DuckDB connection path for dbt | No | Committed |
| `dbt/dbt_project.yml` | dbt project configuration | No | Committed |
| `docker-compose.yml` | Docker service definitions | No | Committed |

**Loading priority for credentials:** `.dlt/secrets.toml` > `.env` (dlt-native format preferred).

## 9. Secret Management

Three-tier storage:

1. **`.env`** — API keys loaded by python-dotenv. Simplest option for non-OAuth sources.
2. **`.dlt/secrets.toml`** — OAuth tokens and dlt credentials. Optionally encrypted via `security/token_storage.py` (OS keychain master key + Fernet symmetric encryption).
3. **`.dango/metabase.yml`** — Auto-generated Metabase admin email and random password. Created on first `dango start`.

**Rules:**
- All secret files are gitignored (enforced by generated `.gitignore`)
- No hardcoded secrets in source code
- OAuth tokens auto-refreshed by dlt at runtime (VAL-004). Google tokens refresh automatically; Facebook tokens require manual re-auth every 60 days.

## 10. API Design Principles

The web module (`web/routes/`) exposes REST endpoints across 18 route files, 1 WebSocket, and a Metabase reverse proxy.

**Current endpoints (all under `/api/`):**

| Category | Endpoints |
|----------|-----------|
| Status & Config | `GET /api/status`, `GET /api/health/platform`, `GET /api/watcher/status`, `GET /api/config`, `GET /api/metabase-config` |
| Sources | `GET /api/sources`, `GET /api/sources/{name}/details`, `POST /api/sources/{name}/sync`, `GET /api/sources/{name}/logs`, `GET /api/sources/{name}/csv-files`, `POST /api/sources/{name}/upload-csv`, `DELETE /api/sources/{name}/csv-files` |
| Auth | `POST /api/auth/login`, `POST /api/auth/logout`, `POST /api/auth/change-password`, `POST /api/auth/accept-invite`, `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`, `GET /api/auth/api-keys`, `POST /api/auth/api-keys`, `DELETE /api/auth/api-keys/{id}`, `GET /api/auth/me` |
| 2FA | `POST /api/auth/2fa/setup`, `POST /api/auth/2fa/verify-setup`, `POST /api/auth/2fa/verify`, `POST /api/auth/2fa/disable`, `POST /api/auth/2fa/regenerate-codes` |
| OAuth | `GET /api/auth/oauth/{provider}/authorize`, `GET /api/auth/oauth/{provider}/callback` |
| Admin | `GET /api/admin/users`, `POST /api/admin/users`, `GET /api/admin/users/{id}`, `PUT /api/admin/users/{id}`, `PUT /api/admin/users/{id}/role`, `POST /api/admin/users/{id}/reset-password`, `POST /api/admin/users/{id}/deactivate`, `POST /api/admin/users/{id}/reactivate`, `DELETE /api/admin/users/{id}`, `POST /api/admin/users/{id}/unlock`, `POST /api/admin/users/{id}/reinvite` |
| dbt | `GET /api/dbt/models`, `POST /api/dbt/models/{name}/run` |
| Logs | `GET /api/logs` |
| Catalog | `GET /api/catalog/{source}/{table}/columns`, `POST /api/catalog/{source}/{table}/profile`, `GET /api/catalog/lineage`, `GET /api/catalog/impact/{model_name}` |
| Governance | `GET /api/governance/schema-drift`, `GET /api/governance/pii` |
| Notebooks | `GET /api/notebooks`, `POST /api/notebooks`, `DELETE /api/notebooks/{name}`, `POST /api/notebooks/{name}/lock`, `POST /api/notebooks/{name}/heartbeat`, `POST /api/notebooks/{name}/release`, `DELETE /api/notebooks/{name}/lock`, `POST /api/notebooks/{name}/copy` |
| Monitoring | `GET /api/monitoring`, `POST /api/monitoring/run`, `GET /api/monitoring/history` |
| Docs | `GET /api/docs` (Swagger), `GET /api/redoc` |
| Real-time | `WS /ws` (sync progress, errors) |
| Proxy | `/metabase/*` (reverse proxy with SSO session injection) |

**Conventions:**
- `/api/{resource}` — plural nouns, standard HTTP methods
- JSON error format: `error_code`, `message`, `detail`
- Login returns 400 for bad credentials (not 401 — avoids browser native auth dialog)
- Session auth via cookie (`dango_session`) or API key (`Authorization: Bearer dango_ak_...`)

## 11. Extension Points

1. **New data source:** Add entry to `ingestion/sources/registry.py` (auth type, params, metadata). Optionally add OAuth provider in `oauth/providers.py` and dlt source in `ingestion/dlt_sources/`. The CLI auto-discovers sources from the registry.

2. **New CLI command:** Create a new command module in `cli/commands/` and register it in `cli/main.py` via `cli.add_command()`.

3. **New API endpoint:** Add a FastAPI route in a new or existing module in `web/routes/` and register it in `web/app.py`.

4. **New dbt dedup strategy:** Add template in `templates/dbt/`, enum value in `config/models.py` `DeduplicationStrategy`, and generation logic in `transformation/generator.py`.

## 12. Known Violations & Migration Notes

### Import Violations

1. **`ingestion/dlt_runner.py` lazy-imports from Level 1 and Level 2** (lines 1602, 1640, 1659, 1669):
   - `from dango.transformation.generator import DbtModelGenerator`
   - `from dango.transformation import run_dbt_models`
   - `from dango.transformation import generate_dbt_docs`
   - `from dango.visualization.metabase import refresh_metabase_connection, sync_metabase_schema`

   These exist because `run_sync()` orchestrates the full pipeline (load → transform → visualize). Extraction to a dedicated orchestration module is planned for Phase 3.

2. ~~**`web/routes/health.py` imported `dango.cli.utils`** (Level 2 → Level 3)~~ — **Fixed in TASK-006**: `get_watcher_status` moved to `dango.platform.watcher_lifecycle` (Level 2), eliminating the violation.

### Monolithic Files

| File | Lines | Refactoring Task |
|------|-------|-----------------|
| ~~`cli/main.py`~~ | ~~3927~~ | ~~TASK-005~~ — **Done:** split into `cli/commands/`, main.py is now ~109 lines |
| ~~`web/app.py`~~ | ~~2900~~ | ~~TASK-085~~ — **Done:** split into `web/routes/`, app.py is now ~202 lines |
| `ingestion/dlt_runner.py` | 1796 | Phase 3 (extract orchestration) |

### Runtime Architecture

`dango start` boots multiple processes coordinated by `DbtLock` for write serialization:

- **Docker containers:** Metabase (:3000), nginx/dbt-docs (:8081)
- **Uvicorn web server:** FastAPI app (:8800)
- **File watcher daemon:** watchdog-based, subprocess runner (optional)
- **DuckDB:** Embedded, single-file database (no separate process)

### User Project Structure

After `dango init`, a user project has this layout:

```
my-project/
├── .dango/              # Dango state and config
│   ├── project.yml      # Project metadata
│   ├── sources.yml      # Source definitions
│   ├── metrics.yml      # Metric analysis config (auto-generated)
│   ├── metabase.yml     # Auto-generated Metabase credentials (gitignored)
│   ├── dango.db         # SQLite: notebook locks, profiling cache, drift/PII/metric results
│   └── snapshots/       # DuckDB read-only snapshots for notebooks
├── .dlt/                # dlt credentials
│   └── secrets.toml     # OAuth tokens (gitignored)
├── .env                 # API keys (gitignored)
├── data/
│   ├── uploads/         # CSV drop zone (watched for auto-sync)
│   └── warehouse.duckdb # DuckDB database file
├── custom_sources/      # User dlt source extensions
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.yml
│   ├── models/
│   │   ├── staging/     # Auto-generated by Dango
│   │   ├── intermediate/# User-created
│   │   └── marts/       # User-created
│   ├── macros/          # get_custom_schema.sql
│   ├── tests/
│   └── seeds/
├── notebooks/           # Marimo notebook files (.py)
├── metabase/            # Dashboard export/import (YAML)
├── docker-compose.yml
├── Dockerfile.metabase
└── .gitignore
```
