# scheduling/

## Purpose

APScheduler-based job scheduling for Dango data pipelines. Manages scheduled syncs and dbt runs with retry, timeout, cancellation, and execution history tracking. Used by both `dango start` (local) and `dango serve` (cloud).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` (64 lines) | Re-exports public API | `SchedulerService`, `ResilienceConfig`, `run_with_resilience`, history functions, job functions |
| `scheduler.py` (430 lines) | APScheduler wrapper with SQLite persistence, event listeners, cancellation | `SchedulerService` |
| `resilience.py` (211 lines) | Retry with backoff, timeout via thread kill, cancellation flags | `ResilienceConfig`, `run_with_resilience`, `_execute_with_timeout`, `_raise_in_thread` |
| `history.py` (499 lines) | Execution history tracking in scheduler SQLite DB | `record_start`, `record_completion`, `record_failure`, `record_cancellation`, `record_timeout`, `get_schedule_history`, `get_recent_history`, `get_average_duration`, `get_last_run`, `cleanup_old_records` |
| `jobs.py` (895 lines) | Module-level job functions (pickle-safe for APScheduler) | `configure_jobs`, `run_scheduled_sync`, `run_scheduled_dbt` |
| `sync_trigger.py` (147 lines) | Server-side manual sync runner invoked via SSH from `dango remote sync` | `main()`, `_run_sync()` |

## Key Conventions

- **Resilience callback kwargs contract:** Retry callbacks receive `job_id, attempt, max_retries, next_retry_delay, error`. Timeout callbacks receive `job_id, timeout_minutes`. Cancel callbacks receive `job_id`. All callers (TASK-039/041) must match these signatures.
- **CPython-specific timeout kill:** `_raise_in_thread()` uses `PyThreadState_SetAsyncExc` — only works for Python bytecode. Threads blocked in C extensions (DuckDB queries, network I/O) defer the exception until returning to bytecode. Use DuckDB query timeouts and DbtLock timeouts as additional backstops.
- **`_on_job_missed` only logs:** Missed runs do not create execution history records. This is a known v1 limitation — missed jobs are invisible in the history UI and excluded from average duration calculations.
- **Exception handler side-effect completeness:** When adding exception handlers that mirror existing ones (e.g., `JobTimeoutError` alongside `JobCancelledError`), copy ALL side-effect calls: broadcast + notify + log + record. Omitting any one creates silent inconsistencies.

For additional scheduling patterns, see [`platform/CLAUDE.md`](../CLAUDE.md) § Scheduling patterns:
- APScheduler dual-patch testing (mock both `SQLAlchemyJobStore` and `AsyncIOScheduler`)
- No atomic trigger update (remove + re-add)
- Cron interval estimation needs sampling (5+ intervals, return minimum)
- `dbt_lock` module/function collision workaround
- Job function signature coupling with `config/schedules.py`
- APScheduler is untyped (budget review time)

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new scheduled job type | `jobs.py`, `config/schedules.py` | `pytest tests/unit/test_scheduler.py tests/unit/test_sync_jobs.py` |
| Change retry/timeout defaults | `resilience.py` (`ResilienceConfig`) | `pytest tests/unit/test_scheduler_resilience.py` |
| Query execution history | `history.py` | `pytest tests/unit/test_execution_history.py` |
| Add a new history status | `history.py` (add constant + recording function) | `pytest tests/unit/test_execution_history.py` |
| Modify scheduler lifecycle | `scheduler.py` (`SchedulerService`) | `pytest tests/unit/test_scheduler.py` |
| Trigger manual sync from SSH | `sync_trigger.py` | `pytest tests/unit/test_sync_trigger.py` |

## Dependencies

**Imports from:**
- `apscheduler` — `AsyncIOScheduler`, `SQLAlchemyJobStore`, `ThreadPoolExecutor`, event types
- `sqlalchemy` — required by APScheduler job store
- `dango.config` — `ConfigLoader`, schedule config models, `reload_schedules`
- `dango.ingestion` — `run_sync` (via lazy import in `jobs.py`)
- `dango.transformation` — `run_dbt_models` (via lazy import in `jobs.py`)
- `dango.utils` — `DbtLock`, `activity_log`, `sync_history`
- `dango.exceptions` — `JobCancelledError`, `JobTimeoutError`
- `dango.logging` — `get_logger`

**Used by:**
- `dango.web.app` — `SchedulerService` lifecycle (startup/shutdown)
- `dango.web.routes.schedules` — schedule CRUD, trigger, cancel, history
- `dango.web.routes.health` — scheduler status in platform health
- `dango.cli.commands.schedule` — CLI schedule management

## Testing

- **Unit:** `pytest tests/unit/test_scheduler.py tests/unit/test_scheduler_resilience.py tests/unit/test_execution_history.py tests/unit/test_sync_jobs.py tests/unit/test_sync_trigger.py`
- **Manual:** `dango start` (starts scheduler), web UI `/schedules` page

## Don't Modify

| File | Reason |
|------|--------|
| `history.py` table schema | Existing scheduler.db files depend on this schema; migrations required for changes |
| `jobs.py` function names | APScheduler job store references functions by module path; renames break persisted jobs |
| `__init__.py` export list | `web/`, `cli/`, and `config/` depend on re-exported symbols |
