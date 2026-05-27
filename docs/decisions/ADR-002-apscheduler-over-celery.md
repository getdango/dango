# ADR-002: APScheduler over Celery

## Status
Accepted

## Context
Dango needs a job scheduler to orchestrate periodic data pipeline tasks: dlt syncs, dbt transformations, and dbt tests. Jobs must persist across process restarts, handle missed schedules on recovery, and prevent overlapping runs (critical because DuckDB enforces single-writer-process — see ADR-003).

The scheduler runs inside the Dango web server (FastAPI with asyncio) and must not block HTTP request handling.

## Decision
Use APScheduler 3.x (`AsyncIOScheduler`) with a SQLite job store, `ThreadPoolExecutor(20)`, and default job settings of `coalesce=True`, `max_instances=1`, `misfire_grace_time=None`.

## Rationale
VAL-005 validated APScheduler 3.11.2 with 16/16 tests passing:

- **Non-blocking:** `AsyncIOScheduler` integrates with FastAPI's asyncio loop. HTTP requests remain responsive while background jobs execute in the thread pool.
- **Persistence:** SQLAlchemy job store backed by SQLite survives process restarts. Jobs are recovered automatically on startup.
- **Missed schedule handling:** `coalesce=True` merges multiple missed runs into one execution (prevents flood on restart). `misfire_grace_time=None` (unlimited; Dango-configured) ensures missed jobs always recover on startup regardless of how long the scheduler was offline — appropriate for local-first deployments where Dango may be stopped for days.
- **Overlap prevention:** `max_instances=1` ensures the same job never runs concurrently. This directly enforces DuckDB's single-writer constraint — a sync job that takes longer than its interval simply skips the next trigger.
- **Dynamic management:** Jobs can be added, listed, and removed at runtime via API, enabling user-configurable schedules.
- **Event system:** `EVENT_JOB_EXECUTED` and `EVENT_JOB_ERROR` listeners provide execution history tracking.

## Alternatives Considered
- **Celery:** The standard Python task queue, but requires an external message broker (Redis or RabbitMQ). This adds a process to manage and monitor, which conflicts with Dango's single-server, minimal-dependency deployment model. Celery is designed for distributed task processing across workers — overkill for a single-instance scheduler.
- **APScheduler 4.x:** A complete rewrite with a new API, but still in alpha (not production-ready as of testing date). The 3.x branch is stable, well-documented, and meets all requirements.
- **System cron:** Simple and reliable, but provides no programmatic control, no persistence of job metadata, and no integration with the application's event system. Users would need SSH access to manage schedules.

## Consequences
- Job functions must be defined at module level (e.g., `dango.orchestration.jobs`) because the SQLite job store uses pickle serialization. Nested functions, lambdas, and closures will fail to serialize.
- Jobs execute in threads (`ThreadPoolExecutor`), not async coroutines. This is appropriate for Dango's I/O-bound sync jobs (API calls, database writes) but means CPU-bound work would block a thread.
- Thread pool size of 20 is sufficient for Dango's workload. Most jobs are I/O-bound and complete quickly. If a deployment has more than 20 concurrent jobs, the pool size can be increased.
- Tied to APScheduler 3.x API. If a future version needs features only in 4.x, migration would require rewriting scheduler integration code.

## Revision History

| Date | Change |
|------|--------|
| 2026-05-05 | Changed `misfire_grace_time` from `3600` to `None` (BUG-167). Local-first deployments may be offline for days; unlimited grace time ensures missed jobs always recover on startup. |
