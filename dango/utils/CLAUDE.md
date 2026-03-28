# utils/

## Purpose

Shared utilities for process management, activity logging, sync history tracking, DuckDB schema initialization, database health monitoring, cross-process dbt locking, and persistent dbt model status.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports from activity_log, sync_history, database, dbt_lock | Re-exports `log_activity`, `get_activity_log_file`, `save_sync_history_entry`, `load_sync_history`, `get_sync_history_file`, `ensure_dbt_schemas`, `DbtLock`, `DbtLockError`, `dbt_lock` |
| `process.py` | Generic process utilities (shared by platform/ and cli/) | `is_process_running()`, `kill_process()` |
| `activity_log.py` | JSONL activity logging to `.dango/logs/activity.jsonl` | `log_activity()`, `get_activity_log_file()` |
| `sync_history.py` | Per-source sync history (JSON, last 100 entries) | `save_sync_history_entry()`, `load_sync_history()`, `get_sync_history_file()` |
| `database.py` | DuckDB schema initialization (raw, staging, intermediate, marts) | `ensure_dbt_schemas()` |
| `db_health.py` | Disk space checks, DuckDB health monitoring, and per-component disk breakdown with 60-second TTL cache | `check_disk_space()`, `check_duckdb_health()`, `get_disk_usage_summary()`, `get_component_disk_usage()`, `print_health_summary()` (imports `DiskSpaceError`, `DuckDBHealthError` from `dango.exceptions`) |
| `dbt_lock.py` | Cross-process file lock for dbt operations (fcntl/msvcrt) | `DbtLock`, `dbt_lock()` context manager (imports `DbtLockError` from `dango.exceptions`) |
| `log_rotation.py` | JSONL log rotation with gzip compression and retention management. Rotates audit.jsonl and activity.jsonl when >5 MB or >1 day old. All public functions follow the never-fail contract. | `rotate_jsonl_log()`, `cleanup_old_archives()`, `get_log_disk_usage()` |
| `dbt_status.py` | Persistent dbt model status from run_results.json | `update_model_status()`, `get_model_statuses()` |
| `data_validation.py` | Schema/data integrity checks against DuckDB | `validate_cursor_field()`, `detect_schema_changes()`, `validate_data_completeness()`, `print_validation_report()` |
| `env_file.py` | .env file parsing and serialization | `parse_env_file()`, `serialize_env_file()` |
| `dango_db.py` (~179 lines) | SQLite context manager for `.dango/dango.db` + schema init | `connect()`, `get_connection()` |
| `post_sync.py` (~486 lines) | Post-sync hook dispatcher | `dispatch_post_sync_hooks()`, `_run_profiling()`, `_run_drift_detection()`, `_run_pii_scan()`, `_run_analysis()` |
| `git_info.py` | Git repository info and deployment guardrails | `GitInfo`, `GitGuardrailResult`, `collect_git_info()`, `check_git_guardrails()` |

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

## Dependencies

**Imports from:**
- `duckdb` — database operations in database.py, db_health.py, data_validation.py
- `psutil` — process existence checks in dbt_lock.py
- `rich` — console output in db_health.py, data_validation.py
- `shutil` — disk usage in db_health.py
- `fcntl` / `msvcrt` — platform-specific file locking in dbt_lock.py

**Used by:**
- `dango/web/helpers.py` — db_health, sync_history, activity_log, dbt_status
- `dango/web/routes/sync.py` — dbt_lock
- `dango/ingestion/dlt_runner.py` — activity_log, sync_history, db_health, post_sync (dispatch_post_sync_hooks)
- `dango/cli/commands/platform.py` — process (kill_process)
- `dango/cli/commands/cleanup.py` — db_health, log_rotation
- `dango/platform/local/watcher_runner.py` — dbt_lock, dbt_status
- `dango/platform/common/startup.py` — log_rotation
- `dango/transformation/__init__.py` — dbt_status
- `dango/governance/` — dango_db (connect)
- `dango/notebooks/` — dango_db (connect)
- `dango/analysis/` — dango_db (connect)

**Not yet imported:** `data_validation.py` (prepared utility, not integrated into any module)

## Testing

- **Unit:** `pytest tests/unit/test_db_health_disk.py tests/unit/test_log_rotation.py`
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
