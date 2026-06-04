"""dango/platform/scheduling/sync_trigger.py

Server-side manual sync runner invoked via SSH from ``dango remote sync``,
and subprocess entrypoint for process-isolated syncs from the web UI and
scheduler.

Usage (on the remote server)::

    /srv/dango/venv/bin/python -m dango.platform.scheduling.sync_trigger \
        '{"sources":["x"],"full_refresh":false}'

Usage (subprocess from web UI / scheduler)::

    python -m dango.platform.scheduling.sync_trigger \
        '{"sources":["x"],"full_refresh":false,"write_progress":true,"source_label":"ui"}'

Prints a JSON result line to stdout on completion::

    {"record_id": 1, "status": "success", "duration_seconds": 42.1}
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dango.logging import get_logger
from dango.platform.scheduling.history import (
    get_scheduler_db_path,
    record_completion,
    record_failure,
    record_start,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Progress file helpers
# ---------------------------------------------------------------------------


def _write_status(state_dir: Path, sync_id: str | None = None, **fields: Any) -> None:
    """Atomically write sync status to state_dir/sync_status_{sync_id}.json.

    Uses write-to-temp + fsync + os.replace() for crash-safe atomic updates.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    filename = f"sync_status_{sync_id}.json" if sync_id else "sync_status.json"
    status_path = state_dir / filename

    data = {
        "pid": os.getpid(),
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        **fields,
    }

    # Write to temp file in same directory, then atomic rename
    tmp_f = tempfile.NamedTemporaryFile(
        mode="w",
        dir=state_dir,
        prefix=".sync_status_",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(data, tmp_f)
        tmp_f.flush()
        os.fsync(tmp_f.fileno())
        tmp_f.close()
        os.replace(tmp_f.name, status_path)
    except Exception:
        # Clean up temp file on failure
        tmp_f.close()
        try:
            os.unlink(tmp_f.name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Main sync function
# ---------------------------------------------------------------------------


def run_manual_sync(
    project_root: Path,
    sources: list[str],
    full_refresh: bool = False,
    backfill_days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    write_progress: bool = False,
    source_label: str = "manual",
    skip_dbt: bool = False,
    max_lock_wait: int = 0,
    sync_id: str | None = None,
    record_id: int | None = None,
) -> dict[str, Any]:
    """Execute a manual sync with execution history tracking.

    Args:
        project_root: Dango project root directory.
        sources: List of source names to sync.
        full_refresh: Whether to do a full refresh sync.
        backfill_days: Number of days to backfill, or ``None``.
        start_date: ISO date string for start_date filter.
        end_date: ISO date string for end_date filter.
        write_progress: If True, write progress to .dango/state/sync_status.json.
        source_label: Label for the sync trigger (e.g., "ui", "scheduler", "manual").
        skip_dbt: If True, skip dbt run after data load.
        max_lock_wait: Max seconds to wait for lock (0 = fail immediately).
        sync_id: Unique identifier for the status file (avoids concurrent clobber).
        record_id: Existing execution history record ID to reuse (avoids double records).

    Returns:
        Dict with ``record_id``, ``status``, ``duration_seconds``, and optionally
        ``rows_loaded``.
    """
    from dango.config.helpers import load_config
    from dango.ingestion import run_sync
    from dango.utils import DbtLock, DbtLockError

    state_dir = project_root / ".dango" / "state"
    db_path = get_scheduler_db_path(project_root)
    if record_id is None:
        record_id = record_start(db_path, source_label, sources=sources)
    start_time = time.time()

    def _progress(phase: str, message: str, **extra: Any) -> None:
        if write_progress:
            _write_status(
                state_dir,
                sync_id=sync_id,
                phase=phase,
                message=message,
                sources=sources,
                source_label=source_label,
                started_at=datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(),
                elapsed_seconds=round(time.time() - start_time, 1),
                **extra,
            )

    _progress("starting", "Initializing sync")

    # --- OAuth validation (before lock) ---
    try:
        from dango.exceptions import OAuthTokenExpiredError, OAuthTokenRevokedError
        from dango.oauth.validation import validate_before_sync

        config = load_config(project_root)
        for name in sources:
            src = config.sources.get_source(name)
            if src is not None:
                validate_before_sync(src.type.value, project_root)
    except (OAuthTokenExpiredError, OAuthTokenRevokedError) as oauth_err:
        error_msg = f"OAuth validation failed: {oauth_err.user_message}"
        record_failure(db_path, record_id, error_msg)
        duration = round(time.time() - start_time, 1)
        _progress("failed", error_msg, error=error_msg)
        return {
            "record_id": record_id,
            "status": "failed",
            "duration_seconds": duration,
            "error": error_msg,
        }
    except Exception as e:
        # Non-OAuth errors during validation: continue (benefit of the doubt)
        logger.warning("pre_sync_validation_error", error=str(e), exc_info=True)

    # --- Lock acquisition with retry ---
    _progress("lock_waiting", "Waiting for lock")
    lock = None
    try:
        lock = DbtLock(
            project_root=project_root,
            source=source_label,
            operation=f"sync:{','.join(sources)}",
        )

        acquired = False
        wait_start = time.time()
        while not acquired:
            try:
                lock.acquire()
                acquired = True
            except DbtLockError as exc:
                elapsed_wait = time.time() - wait_start
                if elapsed_wait >= max_lock_wait:
                    error_msg = f"Lock unavailable: {exc}"
                    record_failure(db_path, record_id, error_msg)
                    duration = round(time.time() - start_time, 1)
                    _progress("failed", error_msg, error=error_msg)
                    return {
                        "record_id": record_id,
                        "status": "failed",
                        "duration_seconds": duration,
                        "error": error_msg,
                    }
                time.sleep(5)
    except DbtLockError as exc:
        error_msg = f"Lock unavailable: {exc}"
        record_failure(db_path, record_id, error_msg)
        duration = round(time.time() - start_time, 1)
        _progress("failed", error_msg, error=error_msg)
        return {
            "record_id": record_id,
            "status": "failed",
            "duration_seconds": duration,
            "error": error_msg,
        }

    # --- Stop Metabase on cloud to prevent DuckDB lock conflicts ---
    from dango.platform.common.metabase_lifecycle import stop_metabase_for_writes

    _metabase_should_stop = os.environ.get("DANGO_CLOUD_MODE") == "true"
    if _metabase_should_stop:
        _progress("metabase_stop", "Pausing Metabase for sync")
    _metabase_was_stopped = stop_metabase_for_writes(project_root)

    try:
        # Reload config (may have been loaded above for OAuth, but safe to reload)
        config = load_config(project_root)
        resolved = []
        for name in sources:
            src = config.sources.get_source(name)
            if src is not None:
                resolved.append(src)

        if not resolved:
            msg = f"No valid sources found for: {', '.join(sources)}"
            logger.warning("manual_sync_no_sources", source_names=sources)
            # Record failure in sync history so UI shows "failed" not "never synced"
            from dango.utils.sync_history import save_sync_history_entry

            for name in sources:
                save_sync_history_entry(
                    project_root,
                    name,
                    {
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                        "status": "failed",
                        "duration_seconds": 0,
                        "rows_processed": 0,
                        "error_message": msg,
                    },
                )
            record_failure(db_path, record_id, msg)
            duration = round(time.time() - start_time, 1)
            _progress("failed", msg, error=msg)
            return {
                "record_id": record_id,
                "status": "failed",
                "duration_seconds": duration,
                "error": msg,
            }

        # Parse date params
        start_date_obj = None
        if backfill_days is not None:
            start_date_obj = datetime.now(tz=timezone.utc) - timedelta(days=backfill_days)
        elif start_date is not None:
            start_date_obj = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)

        end_date_obj = None
        if end_date is not None:
            end_date_obj = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

        _progress("data_load", "Loading data from source")

        _dbt_failed = False

        def _sync_progress_cb(phase: str, message: str) -> None:
            nonlocal _dbt_failed
            if phase == "dbt_failed":
                _dbt_failed = True
            _progress(phase, message)

        sync_result = run_sync(
            project_root=project_root,
            sources=resolved,
            full_refresh=full_refresh,
            start_date=start_date_obj,
            end_date=end_date_obj,
            skip_dbt=skip_dbt,
            progress_callback=_sync_progress_cb,
        )

        # Extract rows loaded from sync result
        rows_loaded = 0
        if isinstance(sync_result, dict):
            rows_loaded = sum(
                r.get("rows_loaded", 0)
                for r in sync_result.get("results", [])
                if isinstance(r, dict)
            )

        duration = round(time.time() - start_time, 1)

        # Check for failures: dbt failed OR any source failed
        failed_sources = (
            sync_result.get("failed_sources", []) if isinstance(sync_result, dict) else []
        )

        if _dbt_failed:
            error_msg = "dbt models failed"
            record_failure(db_path, record_id, error_msg)
            _progress("failed", error_msg, rows_loaded=rows_loaded, dbt_error=True)
            return {
                "record_id": record_id,
                "status": "failed",
                "duration_seconds": duration,
                "error": error_msg,
                "rows_loaded": rows_loaded,
            }

        if failed_sources:
            error_msg = "; ".join(f["error"] for f in failed_sources if isinstance(f, dict))
            record_failure(db_path, record_id, error_msg)
            _progress("failed", f"Sync failed: {error_msg}", error=error_msg)
            return {
                "record_id": record_id,
                "status": "failed",
                "duration_seconds": duration,
                "error": error_msg,
                "rows_loaded": rows_loaded,
            }

        record_completion(db_path, record_id)
        if skip_dbt:
            _progress("data_loaded", "Data loaded (dbt deferred)", rows_loaded=rows_loaded)
        else:
            _progress("completed", "Sync completed successfully", rows_loaded=rows_loaded)
        return {
            "record_id": record_id,
            "status": "data_loaded" if skip_dbt else "success",
            "duration_seconds": duration,
            "rows_loaded": rows_loaded,
        }
    except Exception as exc:
        logger.warning("manual_sync_failed", error=str(exc), exc_info=True)
        error_msg = str(exc)
        record_failure(db_path, record_id, error_msg)
        duration = round(time.time() - start_time, 1)
        _progress("failed", f"Sync failed: {error_msg}", error=error_msg)
        return {
            "record_id": record_id,
            "status": "failed",
            "duration_seconds": duration,
            "error": error_msg,
        }
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        # --- Restart Metabase on cloud ---
        if _metabase_was_stopped:
            from dango.platform.common.metabase_lifecycle import start_metabase_after_writes

            start_metabase_after_writes(project_root)
            try:
                # Trigger Metabase schema scan so new tables appear immediately
                _trigger_metabase_schema_scan(project_root)
            except Exception:
                logger.debug("metabase_schema_scan_after_sync_failed", exc_info=True)


def _trigger_metabase_schema_scan(project_root: Path) -> None:
    """Wait for Metabase health, then trigger a schema sync via API.

    Best-effort — failures are logged but do not affect the sync result.
    """
    import time

    import requests
    import yaml

    metabase_url = "http://localhost:3000"

    # Wait for Metabase to become healthy (up to 60 seconds)
    for _ in range(12):
        try:
            resp = requests.get(f"{metabase_url}/api/health", timeout=3)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        logger.debug("metabase_schema_scan_skipped", reason="health_timeout")
        return

    # Load credentials from metabase.yml
    mb_yml = project_root / ".dango" / "metabase.yml"
    if not mb_yml.exists():
        logger.debug("metabase_schema_scan_skipped", reason="no_metabase_yml")
        return

    try:
        with open(mb_yml) as f:
            creds = yaml.safe_load(f)
        admin = creds.get("admin", {})
        email, password = admin.get("email"), admin.get("password")
        db_id = creds.get("database", {}).get("id")
        if not email or not password or not db_id:
            logger.debug("metabase_schema_scan_skipped", reason="missing_credentials")
            return

        # Login to get session
        login_resp = requests.post(
            f"{metabase_url}/api/session",
            json={"username": email, "password": password},
            timeout=10,
        )
        if login_resp.status_code != 200:
            logger.debug("metabase_schema_scan_skipped", reason="login_failed")
            return

        session_id = login_resp.json().get("id")
        if not session_id:
            return

        # Trigger schema sync
        requests.post(
            f"{metabase_url}/api/database/{db_id}/sync_schema",
            headers={"X-Metabase-Session": session_id},
            timeout=10,
        )
        logger.debug("metabase_schema_scan_triggered", database_id=db_id)
    except Exception:
        logger.debug("metabase_schema_scan_failed", exc_info=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python -m dango.platform.scheduling.sync_trigger '<json>'",
            file=sys.stderr,
        )
        sys.exit(1)

    args: dict[str, Any] = json.loads(sys.argv[1])
    project_root = Path(args.get("project_root", "/srv/dango/project"))
    result = run_manual_sync(
        project_root=project_root,
        sources=args["sources"],
        full_refresh=args.get("full_refresh", False),
        backfill_days=args.get("backfill_days"),
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
        write_progress=args.get("write_progress", False),
        source_label=args.get("source_label", "manual"),
        skip_dbt=args.get("skip_dbt", False),
        max_lock_wait=args.get("max_lock_wait", 0),
        sync_id=args.get("sync_id"),
        record_id=args.get("record_id"),
    )
    print(json.dumps(result))
