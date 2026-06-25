# Changelog

All notable changes to Dango will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.5] - 2026-06-25

### Added

- Sync queue — concurrent syncs wait instead of failing with "Lock unavailable," with queued status in the web UI (F-1)
- WebSocket sync phase events — UI shows "Processing..." during post-sync hooks instead of "Syncing..." (P1-2)
- Structured logging wired up — JSON logs to `.dango/logs/dango.log` with daily rotation, gzip compression, and `dango_version` field in every entry
- `dango_version` field in activity log, audit log, and sync subprocess headers
- Schedules page: sources displayed as alphabetically sorted bullet list instead of comma-separated text
- OAuth timeout with 120s limit, retry option, and provider-specific troubleshooting (Google Cloud Console checklist)
- Quality gate automated tests (`test_quality_gate.py`) — verifies version consistency across API responses, logging, and sync subprocesses
- Shared `timeAgoIso()` frontend utility in `static/js/utils.js`

### Fixed

- OAuth credentials saved only after token exchange succeeds, not before entry (P2-3)
- Cross-project port kill — `dango stop` scoped to current project via CWD verification, Docker containers filtered by compose project label (P0-3)
- Staging models generate explicit column lists instead of `SELECT *`, exclude `_dlt_*` and `_dango_*` internal columns, cast GA4 date columns from TIMESTAMPTZ to DATE (P7-2)
- Console log level set to WARNING — no more structlog INFO spam in terminal during sync
- Presidio CARDINAL, PRODUCT, MONEY entity warnings suppressed
- `dango source list` shows full source names without truncation
- Timestamp display unified across Sources and Schedules pages (relative < 24h, day+time < 7d, month+day beyond)
- Sync log filenames include source name and timestamp (was `sync_<uuid>.log`)
- Notebook startup waits indefinitely for marimo to respond (removed 10-second timeout that redirected to unready server)
- `configure_logging()` default `log_dir` respects `DANGO_PROJECT_ROOT` env var instead of CWD
- FastAPI version field uses `dango.__version__` instead of hardcoded `"0.1.0"`
- `DANGO_LOG_LEVEL=ERROR` scoped to CI workflow only, not global docs — prevents tests from silently breaking when env var is set
- `_is_marimo_responding()` no longer called twice in `start_marimo()` — removed redundant early-exit guard

### Removed

- `--source` flag from `dango sync` — use positional arg: `dango sync <name>`
- `--merge-queue` references from CLAUDE.md (not available in this environment)
- Duplicate `sync_started` event emission

## [1.0.4] - 2026-06-15

### Added

- Global sync status indicator in UI header — shows "Syncing N source(s)" on all pages (F-5)
- Activity log entries for CSV uploads, CSV deletes, and manual schedule triggers (P4-2)
- "View in Metabase" link in catalog table detail (P8-1)
- Empty sync protection — replace-mode syncs that return 0 rows when previous data existed now fail and preserve existing data instead of silently wiping it (P1-4)
- `--allow-empty-replace` hidden CLI flag to override empty sync protection when intentional
- Pytest session logging with timestamps and worktree IDs for crash diagnosis (P0-8)

### Fixed

- OAuth wizard: success message only shown after actual auth tokens obtained, not just client credentials (P2-2)
- OAuth wizard: warns and asks before continuing setup after failed/skipped OAuth (P2-4)
- OAuth wizard: "No" to continue exits cleanly instead of looping (P2-6)
- OAuth wizard: non-GA4 sources use YYYY-MM-DD format for start_date (P2-7)
- OAuth wizard: end-of-wizard block only shows incomplete steps (P8-3)
- OAuth port released after Ctrl+C — second attempt no longer fails with "port in use" (P2-1)
- Orphaned file watcher processes detected and killed on `dango start` and `dango stop` (P0-1)
- Metabase error messages show actual Docker volume name instead of placeholder (P0-2)
- Stale DbtLock from crashed processes auto-recovered on startup (P1-1)
- Geo targets seed auto-provisioned for existing Google Ads projects on sync (P3-1)
- GA4 date columns cast from TIMESTAMPTZ to DATE in staging models (P7-1)
- Empty dlt staging schemas (`raw_*_staging`) dropped from DuckDB after successful sync (P7-3)
- Schema drift banner Accept button restyled for visibility, CLI hint added (P8-2)
- Installer help text: removed outdated "(CSV or Stripe)" from `dango source add` (P6-4)
- Scheduled syncs no longer crash with "Bad file descriptor" after launching terminal closes (P0-9)
- `create_app()` no longer sets project_root from CWD at import time — prevents `.dango/` directories leaking into worktrees during pytest (P0-6)
- Audit log path no longer creates `.dango/logs/` in non-project directories (P0-6)
- `test_cli_start_guardrails` no longer kills real running services during pytest (P0-7)
- XSS fix: escape source name in drift banner onclick handler

### Removed

- SIGTERM signal handler from web/app.py — added in 1.0.3 for diagnostics, investigation concluded, no longer needed (P0-5)

### Changed

- Full refresh no longer drops the raw schema before loading — dlt's `write_disposition="replace"` handles table replacement. If an upstream API changes column types, the sync will fail with an error instead of silently recreating. Recovery: `dango db clean --source <name>` + re-sync.
- Pin `fastapi<0.137` — 0.137.0 changes `app.routes` internals, breaking route introspection tests

## [1.0.3] - 2026-06-11

### Added

- Activity log category field (`core` vs `auxiliary`) with UI filter dropdown on logs page
- Subprocess stderr captured to `.dango/logs/sync_*.log` files (was discarded to `/dev/null`)
- Scheduler now writes start/complete/fail/timeout/cancel events to activity log
- "Stale" status badge (yellow) on models page when upstream source sync fails
- Stale sync status files and old sync logs (>7 days) cleaned on server startup
- SIGTERM signal handler logs shutdown events to activity log for crash diagnostics

### Fixed

- "undefined synced successfully" toast when scheduled sync broadcasts missing source name
- Phantom toast notifications on server restart from stale sync status files
- Subprocess crashes now recorded in activity log and sync history (were silent)
- Source failure now cascades to mark downstream dbt models as stale
- Stale status does not overwrite error status on models (error is more severe)
- HTTP connection pooling for Metabase proxy, health checks, and Metabase API (prevents TCP port exhaustion from per-request connections)

## [1.0.2] - 2026-06-09

### Fixed

- `dango start` health check: use `/api/health` (public) instead of `/api/status` (requires auth) — eliminates 180s startup wait
- Staging schema test dedup: clean up duplicate `not_null` tests where both plain and configured (`severity: warn`) versions coexist — fixes dbt compilation error
- Schema drift CLI: add `dango governance accept <source>` remediation hint when breaking drift is detected
- Sources page: fix false "dbt is paused" label — dbt is not actually paused for drift
- Catalog API: add `last_run` and `status` from `dbt_model_status.json` so intermediate/marts models show "Last Updated" in the catalog
- Schema manager: generate default descriptions for new intermediate/marts models instead of empty strings

## [1.0.1] - 2026-06-08

### Fixed

- Duplicate `not_null` dbt tests causing compilation error on sync
- `dango start` timeout waiting for web UI (auth middleware blocking health check)
- Web UI display fixes (sources table count, catalog seed labels, logs filter naming)
- Cloud, health, governance, Metabase, and startup UX fixes
- Windows compatibility fixes for local code paths
- Review feedback: consistent API fields, type hints, async I/O

## [1.0.0] - 2026-06-07

### Added

#### Complete Data Platform

- Pre-configured stack: dlt (ingestion) + DuckDB (warehouse) + dbt (transformation) + Metabase (visualization) + Marimo (notebooks)
- One-command install and setup (`pip install getdango && dango init && dango start`)
- 33 data sources across 9 categories (Stripe, Google Sheets, Google Analytics, HubSpot, Salesforce, PostgreSQL, CSV, REST APIs, and more)
- Auto-generated dbt staging models from ingested sources
- Web dashboard for monitoring syncs, browsing data, and managing sources
- Metabase integration with auto-configured connections and schema sync
- DuckDB single-writer serialization for safe concurrent access

#### Authentication & Security

- User authentication with bcrypt password hashing and strength validation
- Role-based access control (admin, editor, viewer) with 29 granular permissions
- Two-factor authentication (TOTP) with QR code setup and recovery codes
- API key authentication for programmatic access (`dango_ak_*` prefix)
- Brute-force protection with account lockout (5 attempts / 15-minute window)
- Audit logging with 22 event types (login, logout, user CRUD, role changes, etc.)
- OAuth social login (Google, GitHub) with automatic account linking
- Session management with configurable idle and absolute timeouts
- CSRF protection on all state-mutating endpoints
- Invite-based user onboarding with expiring tokens
- First-login password change enforcement

#### Cloud Deployment

- One-command DigitalOcean provisioning (`dango deploy`)
- Bring Your Own Server support (`dango deploy --byos`) for any cloud provider
- Auto-TLS via Caddy with Let's Encrypt
- SSH key-based access (Ed25519) with trust-on-first-use validation
- IP allowlisting and firewall management (DO firewall + UFW for BYOS)
- Push-based deployment model with deploy lock and deployment journal
- Pre-deploy backups with rollback support
- Scheduled backups to DigitalOcean Spaces (S3-compatible)
- Remote server management (`dango remote status/logs/ssh/query`)
- Remote environment variable management (`dango remote env set/get/list/delete`)
- Domain management with DNS validation and auto-HTTPS
- In-place droplet resize and server migration
- Remote Dango version upgrade (`dango remote upgrade`)
- Git guardrails with branch and dirty-state checks before deploy

#### Data Catalog & Governance

- Interactive data catalog with column profiling and full-text search
- Schema drift detection with breaking-change protection and admin acceptance workflow
- PII scanning with Presidio and spaCy (targeted entity types for low false positives)
- Column-level data lineage visualization
- Impact analysis for downstream models
- PII override management for false positive suppression
- Drift and PII webhook notifications

#### Scheduling & Monitoring

- Cron-based scheduling with APScheduler and human-readable presets
- Schedule types: Sync + Transform, Sync Only, Transform Only
- Automatic retry with exponential backoff and configurable timeouts
- Thread-kill timeout enforcement with cancellation support
- Webhook notifications for sync results, schema drift, and PII detection
- Slack Block Kit message formatting
- SQL-based metric monitoring with trend detection (linear regression)
- Drill-down analysis with top contributor ranking
- Pre-built monitor templates for common sources
- Execution history tracking with 90-day retention

#### Notebooks

- Marimo notebook integration with headless server management
- Three starter templates (explore, quality, blank)
- DuckDB snapshot isolation for concurrent sync and notebook use
- File-level notebook locking with heartbeat refresh
- Idle auto-shutdown for notebook server
- HTTP and WebSocket reverse proxy to Marimo

#### Developer Experience

- 50+ CLI commands organized into logical groups
- Branch-based dbt development (`dango dev`) with isolated copy of production database
- Project validation (`dango validate`) for config, sources, dbt, and credentials
- Config validation for CI (`dango config validate`)
- File watcher for auto-syncing on local file changes
- CSV upload via web UI with schema mismatch detection
- Database health checks with disk usage breakdown
- Log rotation with gzip compression and configurable retention
- `dango cleanup` for removing old logs, dbt artifacts, and cache
- dbt snapshot support (`dango snapshot add/list/run`) for SCD Type 2 change tracking
- DuckDB snapshot management (`dango snapshot db`)
- Database migration framework (`dango migrate status/run`)
- Local Dango version upgrade (`dango upgrade`)

### Changed

- Complete rewrite from v0.1.x (not backwards compatible)

### Migration from v0.1.x

- v1.0.0 requires a new project. Run `dango init` to get started.
- Back up any v0.1.x data before upgrading.
- See [docs.getdango.dev](https://docs.getdango.dev) for the migration guide.

## [0.1.0] - 2025-12-17

### Added
- **MVP Release** - First stable release for early adopters
- **Google Ads** - Full OAuth support (tested and working)

### Changed
- Install scripts now available at `getdango.dev/install.sh` (shorter URL)
- Windows support fully tested and documented

### Notes
This is the v0.1.0 MVP release marking Dango as ready for early adopters. All OAuth sources (Google Sheets, GA4, Facebook Ads, Google Ads) are production-ready.

## [0.0.5] - 2025-12-08

### Added
- **Unreferenced Custom Sources Warning**
  - Detects Python files in `custom_sources/` not referenced in `sources.yml`
  - Shows actionable warning in `dango sync`, `dango validate`, and `dango source list`
  - Includes example configuration snippet to help users fix the issue
- **Dry Run Mode for Sync**
  - `dango sync --dry-run` shows what would be synced without executing
- **`__init__.py` in custom_sources/**
  - `dango init` now creates `__init__.py` for proper Python imports

### Fixed
- **Database validation check** now correctly counts tables across all schemas (not just `main`)
- **Model validation count** now shows accurate count instead of "unknown number of"
- **Skip message in sync** now correctly categorizes reasons:
  - "user-customized" for models where marker was removed
  - "tables pending" for tables not yet synced

## [0.0.4] - 2025-12-06

### Fixed
- Fixed version string mismatch between `pyproject.toml` and `__init__.py`

### Changed
- Install scripts now use PyPI (`pip install getdango`) instead of git tags

## [0.0.3] - 2025-12-05

### Added
- **OAuth Authentication**
  - Google Sheets OAuth with browser-based flow
  - Google Analytics (GA4) OAuth with browser-based flow
  - Facebook Ads OAuth with long-lived token support (60-day expiry)
  - `dango oauth <provider>` commands for all OAuth sources
  - Inline OAuth prompts during `dango source add` wizard

- **OAuth Token Management**
  - Token expiry tracking and validation
  - Pre-sync expiry warnings (7 days before expiration)
  - Expired token blocking with clear re-auth instructions
  - Facebook token auto-extend for still-valid tokens

- **Pre-flight Validation**
  - OAuth credential validation in `dango validate`
  - Shows pass/warn/fail status for each OAuth source

- **dlt_native Source Type**
  - Support for ANY dlt source via `type: dlt_native`
  - Custom source support from `custom_sources/` directory
  - Full dlt configuration control via `sources.yml`

### Changed
- Shopify support deferred (awaiting upstream dlt updates)
- Google Ads deferred to future release

### Notes
This release adds OAuth support for Google and Facebook data sources, enabling users to connect to Google Sheets, Google Analytics (GA4), and Facebook Ads with browser-based authentication flows.

## [0.0.2] - 2025-11-21

### Added
- **Bootstrap Installer Improvements**
  - Interactive installation mode selection (global vs virtual environment)
  - Custom virtual environment location support
  - Global installation with automatic PATH configuration on all platforms
  - Conflict detection for existing global installations
  - Comprehensive error handling and validation messages
  - PowerShell execution policy auto-detection and fix for Windows
  - PATH refresh in current PowerShell session for immediate use
  - Better shell detection using `$SHELL` variable on Unix systems

- **Windows Platform Support**
  - Complete Windows compatibility throughout the codebase
  - Platform-specific service health checks (HTTP on Windows, Docker on Mac/Linux)
  - Cross-platform file locking (msvcrt on Windows, fcntl on Unix)
  - DuckDB connection retry logic to handle Windows file locking
  - UTF-8 encoding for all file operations to prevent encoding errors

- **Documentation**
  - Complete Windows installation instructions with prerequisites
  - Expanded Python version requirements (3.10-3.12) with installation guides
  - Comprehensive troubleshooting section for both platforms
  - Platform-specific uninstall instructions
  - Enhanced PATH configuration guidance

### Fixed
- **Windows Compatibility**
  - UTF-8 encoding errors in file read/write operations
  - DuckDB file locking by Windows Explorer (dllhost.exe)
  - Docker Desktop performance issues with timeout handling
  - Service health check timeouts (switched to HTTP-based checks on Windows)
  - Frontend timeout handling (5s → 15s for slower Windows operations)
  - Cross-platform hostname detection (replaced Unix-only `os.uname()`)

- **Installer**
  - PATH not updating in current PowerShell session
  - Better Python version detection across all platforms (3.10-3.12 only)
  - User bin path detection on macOS/Linux for global installs
  - Removed direnv dependency to simplify installation UX
  - Fixed success message to acknowledge when venv is already activated

- **Service Management**
  - dbt-docs health check port correction (8080 → 8081)
  - Docker service status detection performance on Windows
  - Async parallel service status checks to improve performance

### Changed
- **Python Support**: Restricted to Python 3.10-3.12 (3.13+ not yet supported due to dependency compatibility, specifically DuckDB binary wheels)
- **Installer UX**: Softer messaging, clearer prompts, better validation and error messages
- **Documentation**: Restructured README with clear platform-specific sections

### Technical Details
- Modified files: 10 core files
- Total changes: +1,490 additions, -357 deletions
- Platform-specific code paths for Windows vs Mac/Linux
- HTTP-based health checks 10x faster than Docker commands on Windows

### Notes
This release focuses on Windows compatibility and installer improvements. All platforms now fully supported with optimized performance characteristics for each OS.

## [0.0.1] - 2025-11-14

### Added
- Initial pre-MVP preview release
- CLI framework with 9 core commands
- CSV and Stripe data source integration (fully tested)
- dbt auto-generation for staging models
- Web UI with FastAPI backend and live monitoring
- Metabase integration with auto-setup
- File watcher with auto-triggers for CSV and dbt changes
- Interactive wizards for project setup and source configuration
- DuckDB as embedded analytics database
- Docker Compose orchestration for services

### Core Commands
- `dango init` - Initialize new project with interactive wizard
- `dango source add/list/remove` - Manage data sources
- `dango sync` - Load data from sources with auto-dbt generation
- `dango start/stop/status` - Service management
- `dango run` - Run dbt transformations
- `dango model add` - Create intermediate/marts models with wizard
- `dango dashboard export/import` - Dashboard version control
- `dango validate` - Comprehensive project validation
- `dango config` - Configuration management

### Known Limitations
- **Only CSV and Stripe sources tested** in v0.0.1
- Other dlt sources available but not verified

### Notes
This is a **preview release** for early feedback. Not recommended for production use.

[1.0.0]: https://github.com/getdango/dango/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/getdango/dango/compare/v0.0.5...v0.1.0
[0.0.5]: https://github.com/getdango/dango/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/getdango/dango/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/getdango/dango/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/getdango/dango/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/getdango/dango/releases/tag/v0.0.1
