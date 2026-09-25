# Changelog

All notable changes to Dango will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Fixed

### Security

### Changed

## [1.0.9] - 2026-09-25

### Fixed

- Docker project identity upgrades now preserve a pre-1.0.8 project's existing Metabase volume even when its containers were previously stopped or Docker Desktop is temporarily unavailable, preventing an accidental empty replacement Metabase instance
- `dango start` now recognizes each project's actual Compose-built Metabase image instead of incorrectly reporting every existing project as a first-time build
- If Docker Compose times out after services have already started, Dango retains those services instead of immediately tearing them down and shows captured Compose output for actionable diagnosis
- Cloud backup and scheduled backup operations now use the project's persisted Compose identity for Metabase volumes and remote lifecycle commands

## [1.0.8] - 2026-09-22

### Added

- Telemetry — `dango init` now asks for one-time, anonymous opt-in consent before sending an install ping; declining, running in CI, or setting `DO_NOT_TRACK`/`DANGO_TELEMETRY` skips it silently
- `dango telemetry` command (`status` / `on` / `off`) — control telemetry for Dango, dbt, dlt, and Metabase together with `--all`, or one at a time with `--provider`
- `/settings/telemetry` page — toggle telemetry status from the web UI, matching the CLI command
- MCP server — `dango mcp setup` configures Claude Code, Cursor, or Windsurf to talk to your Dango project; 15 tools cover reading (sources, schema, catalog, lineage, models, SQL, sync history, ad-hoc queries) and making changes (running syncs/transforms/doctor, adding sources/schedules, creating models) through the same functions the CLI itself uses
- `dango init` now generates both `CLAUDE.md` and `AGENTS.md` (identical content) in the project, so Cursor, Windsurf, and other AGENTS.md-aware tools get the same onboarding guidance Claude Code does — not just Claude Code
- `dango validate` now warns when model or column descriptions are missing or left as `# TODO` placeholders
- Deploy output now warns when backups aren't configured for BYOS/self-hosted deploys, with a link to the backup setup docs
- Data Pipeline Health dashboard now shows real sync history, dbt test results, and source counts instead of placeholder queries
- Git guardrail warnings (previously only on `dango remote push`) now also cover model/source/schedule creation — CLI wizards and MCP's mutation tools warn when run on a protected branch
- `dango status` now shows whether an MCP server process is running for the current project, alongside the existing web server, watcher, and Metabase rows
- `dango source inspect-state <name>` — a new read-only command that decodes and displays a source's dlt incremental sync state (the cursor value it's tracking per resource), useful for diagnosing why an incremental sync isn't picking up data you'd expect it to
- `dango docker-audit` — a new diagnostic command that lists every Dango-managed Docker resource on the machine (not just the current project), grouped by real Compose project identity, and classifies each as live, safe to clean, or needing manual attention

### Fixed

- dbt-core 1.10.22 → 1.11.14, dbt-duckdb 1.10.1 → 1.11.0, Metabase v0.59.1 → v0.62.18, Metabase DuckDB JDBC driver 1.5.3.0 → 1.5.4.0
- `dango generate` no longer rewrites every staging model file (and its timestamp) on every sync when nothing actually changed — previously this produced diff noise on every run even with no real schema changes. Manually customized models are also no longer overwritten by default; use the new `--force` flag to regenerate them anyway
- Sync progress: the UI no longer looks like it's stuck for up to ~90 seconds between a sync finishing and dbt starting — a "data loaded, running transforms" update now appears in between
- Metabase: `dango start`'s port pre-flight check now reads the project's configured Metabase/dbt-docs ports instead of always checking 3000/8081
- Metabase: first-run setup now targets the project's configured Metabase port instead of always defaulting to `localhost:3000`
- Metabase: dashboard card attachment fixed — the API endpoint it called was removed as of Metabase 0.47, so cards silently failed to attach while the CLI reported the dashboard as successfully provisioned
- `dango dashboard provision`: no longer hardcodes port 3000 or fails to find the project's DuckDB database in Metabase (name-based lookup never matched the actual connection name)
- MCP's `run_transform` tool now acquires the same DuckDB write lock as every other transform path, preventing warehouse corruption if it runs concurrently with a sync or scheduled job
- Process management: a tracked PID's start time is now verified before signaling it, so a stale PID file can no longer kill an unrelated process that has since reused the same PID
- Telemetry: dlt's telemetry setting now lives in machine-level config alongside dango/dbt/Metabase, instead of the project's own `.dlt/config.toml` — toggling telemetry no longer produces a git diff
- `dango init`: project names containing a dot (e.g. `my.project`) no longer produce a broken dbt project
- Scripts page: per-script execution timeout is now configurable per script instead of a hardcoded 5 minutes
- `dango source add`: selecting MySQL no longer crashes with a validation error and losing all entered configuration
- Scripts page: a script that times out or is killed now correctly shows the output it had already printed, instead of an empty log — a race between two competing reads of the same process output was discarding it
- `dango sync ... --full-refresh` on a merge-based source now actually clears cached incremental sync state, instead of only resetting the destination tables and leaving stale state behind that could prevent a full reload
- Docker project identity: a project's Compose identity is now a stable ID persisted once in `project.yml` at `dango init`, instead of a hash of its filesystem path — moving or renaming a project directory could previously produce a different identity and orphan its existing containers/volumes. Existing projects migrate automatically and losslessly on their next `dango start`/`dango stop`. `dango start`/`dango stop` also now verify a project's identity against Docker's own record of which directory actually created a given container before operating on it, and `dango stop` no longer reports success when containers were left running
- Metabase: the local `/metabase/` proxy could show a blank page (JS assets served as HTML instead of JavaScript) because Metabase's own Site URL setting never matched the path it was actually being served under — now set automatically during setup and on the first sync after upgrading
- Metabase: a second (and later) data source's tables could fail to appear in Metabase without manually clicking "Sync database schema now," even though the sync itself succeeded — the automatic post-sync refresh was polling the wrong readiness signal and could report a re-sync complete before it had actually finished
- Metabase: `dango sync`'s automatic post-Metabase-restart check could report the container ready before it could actually accept a login, causing the schema refresh that depends on that login to silently fail — the readiness check now confirms a real login succeeds, not just that the container is listening
- Metabase: `dango start`'s automatic dashboard restore no longer silently skips projects using the current `metabase/` export directory (it previously only checked for the legacy `dashboards/` path)
- `dango metabase load`'s rollback-on-failure no longer leaves orphaned cards behind in Metabase while reporting "Rollback complete - no changes applied"
- Google Sheets OAuth: `dango source add` no longer fails with `invalid_scope` immediately after completing the browser consent flow, when the very next step (listing sheet names) needed to refresh the just-issued token
- Google OAuth setup instructions (CLI panel and docs) now match Google's current "Google Auth Platform" Console UI (renamed and restructured from the old "OAuth consent screen" flow) instead of describing a UI that no longer exists
- CSV/local-files sources: a file with a value that can't convert to an existing column's type (e.g. text landing in a numeric column) is now rejected with a clear error, instead of being silently skipped while the sync still reports success
- CSV/local-files sources: the same type-mismatch check now also applies when syncing with `--allow-schema-changes`, which previously had no type protection at all
- CSV/local-files sources: a source directory or filename containing an apostrophe no longer breaks loading with a raw database error
- `dango source add`: pasting a long Google Sheets URL into the "Spreadsheet ID or URL" prompt no longer causes the terminal to redraw the line dozens of times
- `dango source add`: pressing Ctrl+C at the "Spreadsheet ID or URL" prompt now cancels cleanly like every other field, instead of showing a bare error message
- `dango source add`: choosing "Skip for now" at the OAuth setup prompt no longer crashes
- Metabase: schema sync triggered by `dango metabase refresh`, `dango model remove`, `dango sync`, and initial cloud onboarding now refreshes Metabase's connection first, so newly-changed tables reliably become visible — previously only 3 of 7 call sites did this
- Metabase: first-run setup no longer fails with "Metabase not ready after 60 seconds" on a slow cold start — readiness now confirms Metabase's own startup-complete log line first, with a realistic (measured, not guessed) timeout as a fallback
- Metabase: `refresh_metabase_connection()`'s post-restart readiness check now uses the same proven log-based signal as first-run setup, with a realistic timeout — previously a fixed 20-second budget was often too short for a real restart, silently skipping the post-sync schema refresh that makes new tables visible in Metabase
- `dango start` no longer kills another project's live server when the configured port is already in use — it now verifies the process is this project's own before stopping it, and shows a clear error if it can't confirm that instead
- `docker-compose.yml` is now regenerated from current config before every `dango start` — previously it was only written once at `dango init`, so changing `metabase_port`/`dbt_docs_port` afterward (including via this project's own suggested port-conflict fix) silently had no effect
- CSV and local-files sources (and a few related write paths) could fail a sync outright if Metabase happened to be querying the database at that exact moment — now retried automatically, and as a last resort Metabase is temporarily stopped to force a write window through rather than giving up
- `dango source add`'s final hint for how to sync a newly added source referenced a `--source` flag that doesn't exist (`dango sync --source my_api`), which errored immediately — the correct syntax, `dango sync my_api`, is used everywhere now (11 sites across 4 files had the same wrong pattern)
- A few error-recovery hints referenced commands that were renamed and never updated — `dango transform` (dbt build failures) is now `dango run`, and `dango restore <path>` (a failed-migration rollback hint) is now `dango backup restore <path>`

### Security

- Metabase: `MetabaseProvisioner` no longer defaults to a guessable admin username/password when none is supplied
- postcss-selector-parser updated 6.1.2 → 6.1.4, resolving Dependabot alert #7

### Changed

- MCP server setup (`dango mcp setup`) now configures Claude Code via its own `claude mcp add --scope local` command instead of writing directly into `~/.claude/settings.json` — the entry is now private to the current project instead of shared machine-wide across every Dango project. Cursor gets a project-scoped, git-committable `.cursor/mcp.json`. Windsurf keeps a machine-wide config (no per-project option exists in Windsurf itself), now with the current project's path included so at least one project is unambiguous. New `dango mcp remove` command reverses whatever `dango mcp setup` configured
- `dango source add --help` and `dango status`'s Metabase row no longer show a stale source count or a hardcoded port — both now read from the live configuration/registry
- `/settings/telemetry` and `dango telemetry status` now state plainly that the dango/dbt/dlt toggles apply machine-wide (shared across every Dango project) while the Metabase toggle applies only to the current project; `dango init` on any project after the first now prints a one-line note when it silently inherits a telemetry decision made elsewhere, instead of staying completely silent
- The first-run telemetry consent prompt no longer defaults to declining if you just press Enter — it keeps asking until you give a real yes/no. It also now states plainly that agreeing enables an ongoing periodic heartbeat, not just a one-time install ping
- `dango model add`/`dango source add` no longer print two separate warnings for the same "you're on a protected branch" condition — only the more complete one remains

## [1.0.7] - 2026-08-27

### Added

- `dango doctor` — credential health check command; shows ✓ OK / ✗ Missing / Expired / Unknown status for all configured sources at a glance
- Model wizard — interactive upstream table selection with CTE scaffold auto-generation; each selected table becomes a named CTE with `{{ ref(...) }}`
- Schedule wizard — weekday preset (Mon–Fri pre-selected) in `dango schedule add`; `dango schedule reload` command to apply config changes without restart
- Service account auth — `dango oauth google_sheets` and `dango oauth google_analytics` now offer a choice between browser OAuth and JSON key file (service account), useful for server deployments
- Script UI history — scheduled script runs now appear in the Scripts page with timestamp, status, and "View Log" link (previously always showed "not run")
- Script failure history — cancelled, timed-out, and pre-launch-failed script runs write history entries so failures are visible in the UI
- Secrets page partial masking — env var values show last 4 characters (`****efgh`) instead of blanket `***`
- Metabase Collection hierarchy — nested collections are now preserved correctly across `dango metabase save` / `dango metabase load` roundtrips
- Column descriptions — default descriptions for columns in 7 high-value sources (Google Ads, GA4, Google Sheets, Stripe, HubSpot, Facebook Ads, BigQuery) visible in the catalog
- Catalog module — catalog data access extracted to `dango/catalog/` for reuse by CLI and future consumers
- Notebook role access — editor role can now open notebooks not created by them
- Scripts cloud sync — scripts and `scripts/requirements.txt` synced to remote server on `dango remote push`; invalid script paths rejected with a clear error

### Fixed

- Scripts page: scheduled runs no longer permanently show "not run" — `.jsonl` history file written after every scheduled execution
- Scripts page: log viewer route (`/scripts/{name}/logs/{run_id}`) no longer 404s on scheduled runs — log files now written to the correct `runs/{run_id}/` path
- Secrets page: duplicate `/settings/variables` page removed; env vars consolidated into `/settings/secrets`; `/settings/variables` now redirects (301) to `/settings/secrets`
- DuckDB lock conflicts on local: syncs now retry up to 5 times with 10s backoff when Metabase holds the DuckDB connection (previously failed immediately)
- Schedule reload: source-list changes are now detected and applied; previously `dango schedule reload` only compared cron triggers, silently ignoring added/removed sources
- Google Sheets: empty range no longer silently bypasses empty-replace protection — raises a clear error instead of yielding no rows
- Service account validation: no longer returns `valid=True` when `google-auth` is not installed
- Model wizard: duplicate CTE alias no longer generated when two upstream table names collide twice (e.g. `stg_stripe_customers` + `stg_hubspot_customers` both mapping to `customers`)
- Sort/filter UI: broken on Schedules, Scripts, and Catalog pages after JS refactor — missing `app.js` script tag added to all three templates
- `InquirerPy` added to declared package dependencies — `dango oauth google_sheets` / `dango oauth google_analytics` no longer crash with `No module named 'InquirerPy'` on fresh installs

### Security

- PostCSS updated 8.5.10 → 8.5.26, resolving three Dependabot alerts for path traversal via `sourceMappingURL` in CSS comments (build-time only; not exploitable at runtime)

## [1.0.6] - 2026-08-19

### Added

- Script scheduling — cron-based scheduling for Python scripts in `scripts/` via `dango schedule add` (Script type)
- Scripts tab in web UI — list, run, cancel, and view logs for scripts; Scripts positioned before Schedules in nav
- dbt seed command — `dango transform seed` runs dbt seeds; seed tables appear in catalog with row counts and profiling
- Python 3.13 support
- Schedule-aware staleness detection — sources with a schedule show yellow "Stale" badge when last sync exceeds 2× the schedule interval; unscheduled sources always show "Synced"
- Per-table empty-replace protection — multi-resource dlt sources block syncs that would truncate individual tables with existing data
- Source row counts on Sources page
- Model row counts in catalog list API
- Seed profiling in catalog alongside dbt models
- Configurable backup retention, secrets exclusion from backups, and post-restore guidance
- Backup guards, CLI additions, and download/verify commands
- Activity log deduplication and sync history gap prevention (history written on lock timeout, exception, and poll timeout)
- `dlt` upgraded from 1.24.0 to 1.28.1

### Fixed

- Sources page: `dlt_native` sources crash with 500 when `incremental` capability is `null` — now shows correct sync mode label
- Sources page: `@dlt.source` decorated custom sources crash during import inspection — fixed by registering module in `sys.modules` before `exec_module()`
- Sources page: custom `dlt_native` sources in `custom_sources/` auto-discovered without manual config
- Sources page: "Incremental" label derived from actual write_disposition in DuckDB, not registry default
- Sources page: columns consistently aligned; Sync button repositioned as primary action
- Sources page: filter input now visible with border and padding; placeholder indicates filter scope
- Models page: "Run" button overwrote Schema column with "●Running" — fixed by targeting column via `data-column` attribute instead of `nth-child`
- Models page: "Last Run" column now sortable
- Schedules page: "Enabled" column now sortable
- Schedules page: schedule execution history error messages now visible when expanding failed rows
- Schedules page/Scripts page: filter inputs now visible and functional
- Catalog: tab navigation frozen when `sourceFilter` active — tab clicks now clear filter and update URL
- Catalog: 📖 dbt docs link opens in new tab instead of navigating in-place
- Catalog: default name-ascending sort on initial load
- Activity log: source column readable for coalesced dbt runs (bullet list instead of comma-joined)
- Notebooks: startup hang fixed; admin restart shows warning modal; background startup exceptions now logged
- Scheduled sync toast: "Unknown sync completed" replaced with correct source name
- DbtLock scope narrowed to write phase only — long-running API extract no longer blocks concurrent syncs
- DbtLock unlink race condition fixed — lock file no longer deleted while held by another process
- Metabase kept running during sync extract phase; stop/start narrowed to CLI sync and initial deploy only
- Scheduled sync lock timeout aligned with direct sync (60s → 300s)
- `run_with_resilience()` wired into all scheduled sync and dbt jobs (retry, backoff, timeout)
- Full refresh on merge/append sources: schema dropped before pipeline state for a true reload
- Double scrollbar on table pages fixed (`overflow-y: clip` on scroll container)
- Stale Tailwind CSS rebuilt — filter inputs and other utilities now render correctly
- Default name-ascending sort on Schedules, Scripts, and Catalog pages on first load
- Empty-replace protection: underscore-prefixed source name schema resolved via `pipeline.dataset_name`
- stdlib logger kwargs crash on `/api/sources` endpoint fixed
- DuckDB 1.5.2 → 1.5.4, Metabase JDBC driver 1.5.1.0 → 1.5.3.0, dbt-core 1.10.20 → 1.10.22, FastAPI unpinned

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
