"""dango/platform/scheduling/sync_trigger.py

Server-side manual sync runner invoked via SSH from ``dango remote sync``.

Usage (on the remote server)::

    /srv/dango/venv/bin/python -m dango.platform.scheduling.sync_trigger \
        '{"sources":["x"],"full_refresh":false}'

Prints a JSON result line to stdout on completion::

    {"record_id": 1, "status": "success", "duration_seconds": 42.1}
"""

from __future__ import annotations

import json
import sys
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


def run_manual_sync(
    project_root: Path,
    sources: list[str],
    full_refresh: bool = False,
    backfill_days: int | None = None,
) -> dict[str, Any]:
    """Execute a manual sync with execution history tracking.

    Args:
        project_root: Dango project root directory.
        sources: List of source names to sync.
        full_refresh: Whether to do a full refresh sync.
        backfill_days: Number of days to backfill, or ``None``.

    Returns:
        Dict with ``record_id``, ``status``, and ``duration_seconds``.
    """
    from dango.config.helpers import load_config
    from dango.ingestion import run_sync
    from dango.utils import DbtLock, DbtLockError

    db_path = get_scheduler_db_path(project_root)
    record_id = record_start(db_path, "manual", sources=sources)
    start_time = time.time()

    lock = None
    try:
        lock = DbtLock(
            project_root=project_root,
            source="manual",
            operation=f"sync:{','.join(sources)}",
        )
        lock.acquire()
    except DbtLockError as exc:
        record_failure(db_path, record_id, f"Lock unavailable: {exc}")
        duration = round(time.time() - start_time, 1)
        return {
            "record_id": record_id,
            "status": "failed",
            "duration_seconds": duration,
            "error": f"Lock unavailable: {exc}",
        }

    try:
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
            return {
                "record_id": record_id,
                "status": "failed",
                "duration_seconds": duration,
                "error": msg,
            }

        start_date = None
        if backfill_days is not None:
            start_date = datetime.now(tz=timezone.utc) - timedelta(days=backfill_days)

        run_sync(
            project_root=project_root,
            sources=resolved,
            full_refresh=full_refresh,
            start_date=start_date,
        )

        record_completion(db_path, record_id)
        duration = round(time.time() - start_time, 1)
        return {
            "record_id": record_id,
            "status": "success",
            "duration_seconds": duration,
        }
    except Exception as exc:
        logger.warning("manual_sync_failed", error=str(exc), exc_info=True)
        record_failure(db_path, record_id, str(exc))
        duration = round(time.time() - start_time, 1)
        return {
            "record_id": record_id,
            "status": "failed",
            "duration_seconds": duration,
            "error": str(exc),
        }
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m dango.platform.scheduling.sync_trigger '<json>'", file=sys.stderr)
        sys.exit(1)

    args: dict[str, Any] = json.loads(sys.argv[1])
    project_root = Path(args.get("project_root", "/srv/dango/project"))
    result = run_manual_sync(
        project_root=project_root,
        sources=args["sources"],
        full_refresh=args.get("full_refresh", False),
        backfill_days=args.get("backfill_days"),
    )
    print(json.dumps(result))
