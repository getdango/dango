"""dango/utils/pipeline_health.py

Materializes pipeline-health state (sync history, dbt test results, and the
configured source list) into real DuckDB tables under the ``_dango_meta``
schema of the project's warehouse.

Why this exists: Metabase's Docker container only mounts ``./data:/data:ro``
(see ``templates/docker-compose.yml.j2``) — it has no access to
``.dango/history/*.json``, ``.dango/dbt_model_status.json``, or
``dbt/target/run_results.json``. The "Data Pipeline Health" dashboard
(``dango/visualization/metabase.py``, ``DASHBOARD_QUERIES``) is provisioned
as native SQL cards that Metabase executes itself against the DuckDB
warehouse — it cannot read those JSON files directly. This module is the
bridge: it reads the JSON state Dango already tracks and writes a small,
denormalized copy into DuckDB tables Metabase *can* see, in the ``_dango_meta``
schema (following the ``_dango_*`` naming precedent already anticipated in
``dango/visualization/CLAUDE.md``'s "Don't Modify" notes for
``DASHBOARD_QUERIES``).

Design choice — Python step, not a dbt model:
    A new dbt model using ``read_json_auto()`` was considered (and confirmed
    to parse the sync-history JSON shape correctly). It was rejected for
    ``dbt_test_results`` specifically: ``dbt/target/run_results.json`` is
    overwritten by dbt at the *end* of a whole dbt invocation (see
    ``dlt_runner.py``'s backup/restore of ``run_results.json`` around the
    ``generate_dbt_docs()`` call). A dbt model added to that same invocation
    would run *before* the invocation's own results are written, so it would
    always read the *previous* run's test results, not the current one — a
    permanent one-cycle lag. Reading sync history and sources.yml via a dbt
    model has no such problem, but keeping one mechanism for all three
    matches how ``_profile_dbt_models()`` in ``utils/post_sync.py`` already
    reads ``run_results.json`` directly with plain Python *after* dbt has
    fully finished (which is the correct time to read it).

Called from two places, both safe to call repeatedly (each run fully
replaces the materialized tables' contents):
    - ``utils/post_sync.py`` (``dispatch_post_sync_hooks``), after every
      ``dango sync`` — keeps the tables fresh with the latest sync + dbt run.
    - ``cli/commands/dashboard.py`` (``dashboard_provision``), immediately
      before creating dashboard cards — guarantees the tables exist (even if
      empty, e.g. fresh install with no sync history) so provisioning never
      fails, and that a manual re-provision always reflects current state.

Never raises — mirrors the "best-effort" contract of the other post-sync
hooks in ``utils/post_sync.py``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from dango.logging import get_logger

logger = get_logger(__name__)

SCHEMA = "_dango_meta"

# dbt test-node statuses that count as "passed". Different dbt/adapter
# versions have been observed to report either the TestStatus vocabulary
# ("pass"/"fail"/"warn"/"error"/"skipped") or the generic RunStatus
# vocabulary ("success"/"error"/"skipped") for test nodes — verified against
# a real project's run_results.json where dbt-duckdb reported "success" for
# passing tests, not "pass". Both are treated as passing here.
_PASSING_TEST_STATUSES = {"pass", "success", "warn"}
_FAILING_TEST_STATUSES = {"fail", "error"}
# Anything else (e.g. "skipped") is excluded from pass/fail counts entirely.


def _ensure_schema(conn: Any) -> None:
    """Create the ``_dango_meta`` schema and tables if they don't exist yet.

    Safe to call on a fresh project that has never synced — this is what
    lets ``dango dashboard provision`` succeed with an honest empty state
    instead of erroring on missing tables.
    """
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_history (
            source_name VARCHAR,
            sync_timestamp TIMESTAMP,
            status VARCHAR,
            duration_seconds DOUBLE,
            rows_processed BIGINT,
            error_message VARCHAR
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.dbt_test_results (
            unique_id VARCHAR,
            status VARCHAR,
            passed BOOLEAN,
            message VARCHAR,
            execution_time DOUBLE,
            run_generated_at TIMESTAMP
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.source_overview (
            source_name VARCHAR,
            source_type VARCHAR,
            enabled BOOLEAN
        )
        """
    )


def _materialize_sync_history(conn: Any, project_root: Path, source_names: list[str]) -> None:
    """Rewrite ``_dango_meta.sync_history`` from ``.dango/history/<source>.json``.

    Reads the full retained history (up to the 100-entry cap enforced by
    ``save_sync_history_entry``) for every currently-configured source, not
    just the default ``load_sync_history()`` limit of 10 — the sync-history
    and row-count-trend cards need up to 30 days of entries.
    """
    from dango.utils.sync_history import load_sync_history

    rows: list[tuple[Any, ...]] = []
    for source_name in source_names:
        try:
            entries = load_sync_history(project_root, source_name, limit=100)
        except Exception:
            logger.warning("pipeline_health_sync_history_read_error", source=source_name)
            continue

        for entry in entries:
            timestamp = entry.get("timestamp")
            if not timestamp:
                continue
            try:
                parsed_ts = datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                logger.debug(
                    "pipeline_health_sync_history_bad_timestamp",
                    source=source_name,
                    timestamp=timestamp,
                )
                continue

            rows.append(
                (
                    source_name,
                    parsed_ts,
                    entry.get("status"),
                    entry.get("duration_seconds"),
                    entry.get("rows_processed"),
                    entry.get("error_message"),
                )
            )

    conn.execute(f"DELETE FROM {SCHEMA}.sync_history")
    if rows:
        conn.executemany(
            f"INSERT INTO {SCHEMA}.sync_history "
            "(source_name, sync_timestamp, status, duration_seconds, rows_processed, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def _materialize_dbt_test_results(conn: Any, project_root: Path) -> None:
    """Rewrite ``_dango_meta.dbt_test_results`` from ``dbt/target/run_results.json``.

    Only test nodes (``unique_id`` starting with ``"test."``) are kept —
    models/seeds/operations are out of scope for this table. If the file
    doesn't exist yet (no dbt run has completed), the table is left empty.
    """
    run_results_path = project_root / "dbt" / "target" / "run_results.json"
    if not run_results_path.exists():
        conn.execute(f"DELETE FROM {SCHEMA}.dbt_test_results")
        return

    try:
        run_results = json.loads(run_results_path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("pipeline_health_run_results_read_error", exc_info=True)
        return

    generated_at_raw = run_results.get("metadata", {}).get("generated_at")
    generated_at: datetime | None = None
    if generated_at_raw:
        try:
            generated_at = datetime.fromisoformat(generated_at_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            generated_at = None

    rows: list[tuple[Any, ...]] = []
    for result in run_results.get("results", []):
        unique_id = result.get("unique_id", "")
        if not unique_id.startswith("test."):
            continue

        status = result.get("status")
        if status in _PASSING_TEST_STATUSES:
            passed: bool | None = True
        elif status in _FAILING_TEST_STATUSES:
            passed = False
        else:
            passed = None  # e.g. "skipped" — excluded from pass/fail counts

        rows.append(
            (
                unique_id,
                status,
                passed,
                result.get("message"),
                result.get("execution_time"),
                generated_at,
            )
        )

    conn.execute(f"DELETE FROM {SCHEMA}.dbt_test_results")
    if rows:
        conn.executemany(
            f"INSERT INTO {SCHEMA}.dbt_test_results "
            "(unique_id, status, passed, message, execution_time, run_generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def _materialize_source_overview(conn: Any, project_root: Path) -> list[str]:
    """Rewrite ``_dango_meta.source_overview`` from the project's ``sources.yml``.

    Returns the list of configured source names, so the caller can reuse it
    for the sync-history materialization step without loading config twice.
    """
    from dango.config.helpers import get_config

    try:
        config = get_config(project_root)
        sources = config.sources.sources
    except Exception:
        logger.warning("pipeline_health_config_read_error", exc_info=True)
        conn.execute(f"DELETE FROM {SCHEMA}.source_overview")
        return []

    rows = [(s.name, s.type.value, s.enabled) for s in sources]

    conn.execute(f"DELETE FROM {SCHEMA}.source_overview")
    if rows:
        conn.executemany(
            f"INSERT INTO {SCHEMA}.source_overview (source_name, source_type, enabled) "
            "VALUES (?, ?, ?)",
            rows,
        )

    return [s.name for s in sources]


def materialize_pipeline_health(project_root: Path) -> None:
    """Materialize sync history, dbt test results, and the source list into
    DuckDB tables under the ``_dango_meta`` schema, so the "Data Pipeline
    Health" dashboard's native SQL cards (``DASHBOARD_QUERIES`` in
    ``visualization/metabase.py``) can show real data.

    Each call fully replaces the three tables' contents (DELETE + INSERT) —
    data volumes are small (a handful of sources, up to 100 history entries
    each, a few hundred dbt test nodes) so a full rewrite is simpler and
    cheaper than incremental upserts, and guarantees no stale rows survive a
    source being removed from ``sources.yml``.

    Never raises — best-effort, matching the contract of the other
    post-sync hooks in ``utils/post_sync.py``. Safe to call on a project
    that has never synced (creates empty tables) and safe to call
    repeatedly (e.g. once per sync, and again at ``dango dashboard
    provision`` time).

    DuckDB is single-writer (VAL-003) — this opens a read-write connection,
    so (unlike the read-only ``duckdb.connect(..., config={"access_mode":
    "read_only"})`` calls elsewhere in ``post_sync.py``) it must serialize
    against other writers via ``DbtLock``, the same mechanism
    ``dlt_runner.py``'s dbt-run step and every other ``run_dbt_models()``
    caller uses (see PR #464, which fixed the one caller that had skipped
    it). Uses a short 15s timeout rather than the usual 300s default: this
    is a quick metadata write (a handful of small DELETE+INSERT statements),
    not a real dbt run, and a multi-minute stall here would be a bad user
    experience for `dango dashboard provision` specifically. A timeout just
    means this call's data goes stale until the next successful sync or
    provision — never fatal, per the never-raises contract above.
    """
    import duckdb  # lazy import, matches dlt_runner.py / post_sync.py pattern

    from dango.exceptions import DbtLockError
    from dango.utils.dbt_lock import DbtLock

    db_path = project_root / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    lock = DbtLock(project_root, source="pipeline-health", operation="materialize dashboard state")
    try:
        lock.acquire(timeout=15)
    except DbtLockError:
        logger.info("pipeline_health_lock_busy_skipped")
        return

    conn = None
    try:
        conn = duckdb.connect(str(db_path))
        _ensure_schema(conn)
        source_names = _materialize_source_overview(conn, project_root)
        _materialize_sync_history(conn, project_root, source_names)
        _materialize_dbt_test_results(conn, project_root)
        logger.info("pipeline_health_materialized", source_count=len(source_names))
    except Exception:
        logger.warning("pipeline_health_materialize_error", exc_info=True)
    finally:
        if conn is not None:
            conn.close()
        if lock._acquired:
            lock.release()
