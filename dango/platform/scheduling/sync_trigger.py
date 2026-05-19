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
    except Exception:
        # Non-OAuth errors during validation: continue (benefit of the doubt)
        pass

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

    # --- Stop Metabase on cloud to release DuckDB read lock ---
    # Metabase's JDBC driver acquires fcntl read locks even on :ro Docker
    # volumes.  These read locks block DuckDB write locks needed by dlt.
    _cloud_mode = os.environ.get("DANGO_CLOUD_MODE") == "true"
    if _cloud_mode:
        _progress("metabase_stop", "Pausing Metabase for sync")
        try:
            import hashlib
            import subprocess as _sp

            # Must match DockerManager.compose_project_name
            _proj_name = f"dango-{hashlib.md5(str(project_root).encode(), usedforsecurity=False).hexdigest()[:8]}"
            _env = {**os.environ, "COMPOSE_PROJECT_NAME": _proj_name}
            result = _sp.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(project_root / "docker-compose.yml"),
                    "stop",
                    "metabase",
                ],
                capture_output=True,
                timeout=60,
                env=_env,
            )
            if result.returncode != 0:
                logger.warning(
                    "metabase_stop_nonzero",
                    returncode=result.returncode,
                    stderr=result.stderr.decode(errors="replace"),
                )
            # Wait for Metabase to fully release DuckDB file locks
            time.sleep(3)
        except Exception:
            logger.warning("metabase_stop_before_sync_failed", exc_info=True)

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

        record_completion(db_path, record_id)
        _progress("completed", "Sync completed successfully", rows_loaded=rows_loaded)
        return {
            "record_id": record_id,
            "status": "success",
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
        if _cloud_mode:
            try:
                import hashlib
                import subprocess as _sp

                _proj_name = f"dango-{hashlib.md5(str(project_root).encode(), usedforsecurity=False).hexdigest()[:8]}"
                _env = {**os.environ, "COMPOSE_PROJECT_NAME": _proj_name}
                _sp.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(project_root / "docker-compose.yml"),
                        "start",
                        "metabase",
                    ],
                    env=_env,
                    capture_output=True,
                    timeout=120,
                )
            except Exception:
                logger.debug("metabase_start_after_sync_failed", exc_info=True)


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
