# utils/

## Purpose

Shared utilities for process management, activity logging, sync history tracking, DuckDB schema initialization, database health monitoring, cross-process dbt locking, persistent dbt model status, and pipeline-health materialization for the Metabase dashboard.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports from activity_log, sync_history, database, dbt_lock | Re-exports `log_activity`, `get_activity_log_file`, `save_sync_history_entry`, `load_sync_history`, `get_sync_history_file`, `ensure_dbt_schemas`, `DbtLock`, `DbtLockError`, `dbt_lock` |
| `process.py` | Generic process utilities (shared by platform/ and cli/). `pid_alive()` is the cross-platform existence probe (psutil; zombie and EPERM count as alive); never use `os.kill(pid, 0)` as a probe (CTRL_C_EVENT on Windows) | `pid_alive()`, `is_process_running()`, `kill_process()` |
| `process_group.py` | Process-group signalling and detach kwargs that work without Unix-only os/signal names (Windows falls back to the psutil tree kill) | `group_signal_available()`, `signal_group()`, `group_alive()`, `leads_own_group()`, `sigkill()`, `detach_kwargs()` |
| `private_files.py` | Owner-only mode for an open file without requiring POSIX `os.fchmod` (no-op where `os.fchmod` is missing; on Windows mode bits are not enforced, so it adds no protection there) | `apply_private_mode()` |
| `stdio.py` | Makes stdout/stderr tolerant of non-encodable text (called from the CLI group) | `ensure_safe_stdio()` |
| `activity_log.py` | JSONL activity logging to `.dango/logs/activity.jsonl` | `log_activity()`, `get_activity_log_file()` |
| `sync_history.py` | Per-source sync history (JSON, last 100 entries) | `save_sync_history_entry()`, `load_sync_history()`, `get_sync_history_file()` |
| `database.py` | DuckDB schema initialization (raw, staging, intermediate, marts). Creates the database file and parent directories if they don't exist. | `ensure_dbt_schemas()` |
| `db_health.py` | Disk space checks, DuckDB health monitoring, capacity estimation, per-component disk breakdown with 60-second TTL cache, and orphan detection | `check_disk_space()`, `check_duckdb_health()`, `get_disk_usage_summary()`, `get_duckdb_capacity()`, `get_component_disk_usage()`, `print_health_summary()`, `build_schema_table_mapping()`, `is_table_configured()`, `find_orphaned_tables()` (imports `DiskSpaceError`, `DuckDBHealthError` from `dango.exceptions`) |
| `dbt_lock.py` | Cross-process file lock for dbt operations (fcntl/msvcrt) | `DbtLock`, `dbt_lock()` context manager (imports `DbtLockError` from `dango.exceptions`) |
| `log_rotation.py` | JSONL log rotation with gzip compression and retention management. Rotates audit.jsonl and activity.jsonl when >5 MB or >1 day old. All public functions follow the never-fail contract. | `rotate_jsonl_log()`, `cleanup_old_archives()`, `get_log_disk_usage()` |
| `safe_names.py` | Single-path-component check for a configured name that becomes a directory or file name Dango deletes or writes under (pipeline names, nginx site configs); allows any other character. Also refuses names shaped like a dlt state backup | `contained_child()`, `reject_backup_shaped_name()` |
| `dbt_status.py` | Persistent dbt model status from run_results.json | `update_model_status()`, `get_model_statuses()` |
| `data_validation.py` | Schema/data integrity checks against DuckDB | `validate_cursor_field()`, `detect_schema_changes()`, `validate_data_completeness()`, `print_validation_report()` |
| `env_file.py` | .env file parsing and serialization | `parse_env_file()`, `serialize_env_file()` |
| `redact.py` | Secret redaction for log text (web `/api/logs`, `/api/sources/{n}/logs`, MCP `get_logs`): exact values from `.env` / `.dlt/secrets.toml` (length >= 6, cached per file mtime) plus URL userinfo, auth headers, token/signature query params (also URL-encoded), PEM private keys, Airtable tokens, chat webhook URLs; every pattern is linear-time | `redact_text()`, `redact_patterns()`, `load_secret_values()`, `SECRET_KEY_RE` |
| `error_text.py` | Printable error text: `redacted_error_text` (secrets masked, for logs) and `rich_error_text` (also Rich-escaped and capped at 8000 chars, for `console.print`); used by the dlt runner and `cli/sync_report.py` | `redacted_error_text()`, `rich_error_text()` |
| `duckdb_lock_text.py` | Recognises DuckDB's single-writer lock conflict (Unix "Could not set lock ... Conflicting lock is held in ... (PID n)"; Windows "File is already open in <process> (PID n)" and the older "being used by another process" on a `.duckdb` file; `scripts/check_duckdb_lock_text.py` prints the real message on each platform-smoke leg) and replaces it with a plain explanation (another Dango process is using the warehouse; wait or run `dango status`; PID shown only when numeric). Raw text goes to the debug log. `rich_error_text` applies it, so every printed sync error is covered; the CLI group, web `/api/query` and MCP tools call `describe_error` | `is_duckdb_lock_error()` (`follow_context=False` ignores implicit `__context__`), `rewrite_lock_text()`, `lock_message_from_text()`, `describe_error()`, `lock_message()` |
| `dbt_log_lock.py` | Finds the lock conflict in the part of `dbt/logs/dbt.log` a single dbt run wrote (offset recorded before the run), so `dango run` / `dango docs` can add the plain message after dbt's own failure output | `log_size()`, `lock_message_since()` |
| `spacy_model.py` | The one message for a missing/uninstallable spaCy model (PII scan skipped, exact `<interpreter> -m spacy download en_core_web_sm`, rest of Dango works), printed to stderr once | `spacy_install_command()`, `spacy_unavailable_lines()`, `print_spacy_unavailable()` |
| `docker_hints.py` | OS-correct wording for a missing or stopped Docker (Docker Desktop on macOS/Windows, Docker Engine on Linux), `DockerNotAvailableError`, and the once-per-process note when Metabase steps are skipped | `docker_guidance_lines()`, `docker_binary_present()`, `note_metabase_skipped()`, `DOCKER_UNAVAILABLE_ERROR` |
| `tls_errors.py` | Detects certificate-verification failures (TLS-inspecting proxy) in exceptions or tool output and prints guidance (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `pip --cert`); never suggests disabling verification | `is_certificate_error()`, `is_certificate_error_text()`, `print_certificate_guidance()` |
| `build_output.py` | Printable summary of a failed Docker image build: last lines redacted and escaped, plus a DNS hint on name-resolution failures | `build_failure_report()`, `looks_like_dns_failure()` |
| `dango_db.py` (~210 lines) | SQLite context manager for `.dango/dango.db` + schema init | `connect()`, `get_connection()` |
| `dbt_escaping.py` | Escaping for text generated into dbt SQL/YAML: neutralises Jinja delimiters in data-derived names/descriptions, YAML scalar and SQL comment/string helpers | `neutralize_jinja()`, `yaml_scalar()`, `comment_text()`, `jinja_string_content()` |
| `post_sync.py` (~804 lines) | Post-sync hook dispatcher + sync notification | `dispatch_post_sync_hooks()`, `_run_profiling()`, `_profile_dbt_models()`, `_enrich_staging_tests()`, `_run_pii_scan()`, `_run_analysis()`, `_run_dbt_snapshots()`, `_materialize_pipeline_health()`, `_ensure_default_metrics()`, `_send_sync_notification()` |
| `pipeline_health.py` | Materializes sync history / dbt test results / source list into DuckDB tables (`_dango_meta` schema) for the "Data Pipeline Health" dashboard (1.0.8-DASH-1) — Metabase's container can't read the underlying JSON files directly | `materialize_pipeline_health()`, `SCHEMA` |
| `git_info.py` | Git repository info, deployment guardrails, and warn-only mutation guardrails (CLI wizards + MCP tools, 1.0.8-OPS-3) | `GitInfo`, `GitGuardrailResult`, `collect_git_info()`, `check_git_guardrails()`, `check_mutation_guardrails()` |
| `query_connection.py` | Locked-down read-only DuckDB connection for user/agent SQL (web `/api/query`, MCP `query`/catalog tools): in-memory connection, warehouse attached READ_ONLY under its file stem, memory limit, no spill directory, external access off, configuration locked | `open_locked_query_connection()` |
| `driver.py` | Metabase DuckDB driver version management, pinned SHA-256 verification + HTTPS-only download, version alignment check | `METABASE_DUCKDB_DRIVER_VERSION`, `METABASE_DUCKDB_DRIVER_URL`, `get_duckdb_driver_url()`, `read_driver_version()`, `write_driver_version()`, `driver_needs_update()`, `verify_driver_jar()`, `reuse_verified_driver()`, `download_verified_driver()`, `DRIVER_JAR_NAME`, `DriverIntegrityError`, `DriverDownloadError`, `DriverFilesystemError`, `check_version_alignment()` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new activity log field | `activity_log.py` | Manual: call `log_activity()` and inspect `.dango/logs/activity.jsonl` |
| Change sync history retention | `sync_history.py` (100-entry cap) | Manual: save >100 entries and verify truncation |
| Add a new DuckDB schema | `database.py` | Manual: `dango start` and check schemas in DuckDB |
| Add a health check metric | `db_health.py` | Manual: call `check_duckdb_health()` on a test database |
| Change dbt lock behavior | `dbt_lock.py` | Manual: acquire lock from two processes, verify conflict |
| Change dbt status tracking | `dbt_status.py` | Manual: run dbt, call `get_model_statuses()` |
| Add a data validation check | `data_validation.py` | Manual: call against a DuckDB with test data |
| Rotate JSONL logs | `log_rotation.py` | `pytest tests/unit/test_log_rotation.py` |
| Change log archive retention | `log_rotation.py` (`max_age_days` param, default 90) | `pytest tests/unit/test_log_rotation.py` |
| Change what the pipeline-health dashboard materializes | `pipeline_health.py` | `pytest tests/unit/test_pipeline_health.py` |

## Dependencies

**Imports from:**
- `duckdb` — database operations in database.py, db_health.py, data_validation.py; version detection in driver.py
- `psutil` — process existence checks in dbt_lock.py
- `rich` — console output in db_health.py, data_validation.py
- `shutil` — disk usage in db_health.py
- `fcntl` / `msvcrt` — platform-specific file locking in dbt_lock.py

**Used by:**
- `dango/web/helpers.py` — db_health, sync_history, activity_log, dbt_status
- `dango/web/routes/sync.py` — dbt_lock
- `dango/ingestion/dlt_runner.py` — activity_log, sync_history, db_health, post_sync (dispatch_post_sync_hooks)
- `dango/cli/commands/dashboard.py` — pipeline_health (materialize_pipeline_health, called before provisioning so the dashboard reflects current state even without a prior sync)
- `dango/cli/commands/platform.py` — process (kill_process)
- `dango/cli/commands/cleanup.py` — db_health, log_rotation
- `dango/platform/local/watcher_runner.py` — dbt_lock, dbt_status
- `dango/platform/common/startup.py` — log_rotation, driver (ensure_duckdb_driver)
- `dango/web/routes/query.py`, `dango/cli/commands/mcp_helpers.py` — query_connection (open_locked_query_connection, lazy import)
- `dango/cli/init.py` — driver (DuckDB driver download during init)
- `dango/transformation/__init__.py` — dbt_status
- `dango/governance/` — dango_db (connect)
- `dango/notebooks/` — dango_db (connect)
- `dango/analysis/` — dango_db (connect)
- `dango/cli/commands/remote.py` — git_info (check_git_guardrails, cloud deploy/push path)
- `dango/cli/model_wizard.py`, `dango/cli/source_wizard.py`, `dango/cli/commands/mcp_helpers.py` — git_info (check_mutation_guardrails, 1.0.8-OPS-3 warn-only guardrails for the CLI wizards and MCP mutation tools)

**Not yet imported:** `data_validation.py` (prepared utility, not integrated into any module)

## Testing

- **Unit:** `pytest tests/unit/test_db_health_disk.py tests/unit/test_log_rotation.py tests/unit/test_driver_utils.py tests/unit/test_pipeline_health.py`
- **Integration:** None yet
- **Manual:** `dango start` exercises database.py; `dango sync` exercises activity_log, sync_history, db_health, dbt_lock; `dango cleanup` exercises log_rotation, db_health

## Key Conventions

- **`dbt_lock` module/function name collision:** `__init__.py` exports a function `dbt_lock` that shadows the submodule `dango.utils.dbt_lock`. On Python 3.10, `patch("dango.utils.dbt_lock.DbtLock")` resolves to the function, not the module. Fix: `import dango.utils.dbt_lock; _mod = sys.modules["dango.utils.dbt_lock"]` then `patch.object(_mod, "DbtLock", ...)`. See [STANDARDS.md §7](../../STANDARDS.md#mocking-and-patching).

## Don't Modify

| File | Reason |
|------|--------|
| `activity_log.py` JSONL format | Web UI and CLI both parse this format; changes break log readers |
| `sync_history.py` JSON structure | Web UI displays sync history entries; field changes break the UI |
| `dbt_lock.py` lock file paths (`.dango/state/dbt.lock*`) | Other processes rely on these exact paths to detect lock state |
| `dbt_status.py` status file path (`.dango/dbt_model_status.json`) | Web UI reads this file directly for model status display |
