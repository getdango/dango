"""dango/platform/common/metabase_lifecycle.py

Shared Metabase stop/start helpers for DuckDB write-lock management.

DuckDB allows only one writer process. Metabase's JDBC driver acquires file
locks that block DuckDB writes — even with a ``:ro`` Docker mount. Stopping
Metabase before write operations and restarting it afterward is the actual
prevention mechanism.

This module extracts the ~15-line stop/start pattern that was duplicated in
four call sites into a single pair of functions.

See also: ``dango/config/helpers.py:is_cloud_mode()`` (cloud detection).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: Caller timeouts (seconds). Through the compose helper each must exceed its lock wait
#: plus the verb's budget (stop 30 + 45, start 30 + 90), so a timeout never leaves a
#: queued helper call behind.
STOP_TIMEOUT = 90
START_TIMEOUT = 150


def stop_metabase_for_writes(project_root: Path, force: bool = False) -> bool:
    """Stop Metabase container to prevent DuckDB write lock conflicts.

    Normally cloud-only (``force=False``). Pass ``force=True`` to bypass that gate —
    used as a last-resort local/CI fallback by
    ``_connect_with_metabase_stopped()`` in dlt_runner.py after
    ``_connect_with_lock_retry()``'s own retries are exhausted.

    Returns ``True`` if Metabase was stopped, ``False`` if not applicable or if the
    stop failed (logged, not raised).
    """
    if not force and os.environ.get("DANGO_CLOUD_MODE") != "true":
        return False

    try:
        from dango.platform.compose_exec import run_compose

        result = run_compose(project_root, "stop", "metabase", timeout=STOP_TIMEOUT)
        if result.returncode != 0:
            logger.warning(
                "metabase_stop_nonzero",
                extra={
                    "returncode": result.returncode,
                    "stderr": result.stderr.decode(errors="replace"),
                },
            )
        # Wait for Metabase to fully release DuckDB file locks
        time.sleep(3)
        return True
    except Exception:
        logger.warning("metabase_stop_before_writes_failed", exc_info=True)
        return False


def start_metabase_after_writes(project_root: Path) -> bool:
    """Restart Metabase container after write operations complete.

    Only call this when ``stop_metabase_for_writes()`` returned ``True``.
    The cloud-mode guard is intentionally omitted here — if the stop succeeded,
    the start must be attempted regardless of env-var state (which could
    theoretically change between stop and start in edge cases).

    Returns ``True`` if Metabase was started, ``False`` if the start failed
    (logged, not raised).
    """
    try:
        from dango.platform.compose_exec import run_compose

        run_compose(project_root, "start", "metabase", timeout=START_TIMEOUT)
        return True
    except Exception:
        logger.warning("metabase_start_after_writes_failed", exc_info=True)
        return False
