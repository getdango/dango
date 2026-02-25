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
| `db_health.py` | Disk space checks and DuckDB health monitoring | `check_disk_space()`, `check_duckdb_health()`, `get_disk_usage_summary()`, `print_health_summary()` (imports `DiskSpaceError`, `DuckDBHealthError` from `dango.exceptions`) |
| `dbt_lock.py` | Cross-process file lock for dbt operations (fcntl/msvcrt) | `DbtLock`, `dbt_lock()` context manager (imports `DbtLockError` from `dango.exceptions`) |
| `dbt_status.py` | Persistent dbt model status from run_results.json | `update_model_status()`, `get_model_statuses()` |
| `data_validation.py` | Schema/data integrity checks against DuckDB | `validate_cursor_field()`, `detect_schema_changes()`, `validate_data_completeness()`, `print_validation_report()` |
| `env_file.py` | .env file parsing and serialization | `parse_env_file()`, `serialize_env_file()` |

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

## Dependencies

**Imports from:**
- `duckdb` — database operations in database.py, db_health.py, data_validation.py
- `psutil` — process existence checks in dbt_lock.py
- `rich` — console output in db_health.py, data_validation.py
- `shutil` — disk usage in db_health.py
- `fcntl` / `msvcrt` — platform-specific file locking in dbt_lock.py

**Used by:**
- `dango/web/app.py` — dbt_status, sync_history, activity_log, db_health, dbt_lock
- `dango/ingestion/dlt_runner.py` — activity_log, sync_history, db_health
- `dango/cli/main.py` — database, dbt_lock, dbt_status
- `dango/platform/watcher_runner.py` — dbt_lock, dbt_status
- `dango/transformation/__init__.py` — dbt_status

**Not yet imported:** `data_validation.py` (prepared utility, not integrated into any module)

## Testing

- **Unit:** None yet (will be `tests/unit/test_utils.py`)
- **Integration:** None yet
- **Manual:** `dango start` exercises database.py; `dango sync` exercises activity_log, sync_history, db_health, dbt_lock

## Don't Modify

| File | Reason |
|------|--------|
| `activity_log.py` JSONL format | Web UI and CLI both parse this format; changes break log readers |
| `sync_history.py` JSON structure | Web UI displays sync history entries; field changes break the UI |
| `dbt_lock.py` lock file paths (`.dango/state/dbt.lock*`) | Other processes rely on these exact paths to detect lock state |
| `dbt_status.py` status file path (`.dango/dbt_model_status.json`) | Web UI reads this file directly for model status display |
