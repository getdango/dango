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
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def stop_metabase_for_writes(project_root: Path) -> bool:
    """Stop Metabase container on cloud to prevent DuckDB write lock conflicts.

    Returns ``True`` if Metabase was stopped (cloud mode), ``False`` if not
    cloud mode or if the stop failed (logged, not raised).
    """
    if os.environ.get("DANGO_CLOUD_MODE") != "true":
        return False

    try:
        from dango.platform.docker import get_compose_project_name

        proj_name = get_compose_project_name(project_root)
        env = {**os.environ, "COMPOSE_PROJECT_NAME": proj_name}
        result = subprocess.run(
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
            env=env,
        )
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

    Returns ``True`` if Metabase was started (cloud mode), ``False`` if not
    cloud mode or if the start failed (logged, not raised).
    """
    if os.environ.get("DANGO_CLOUD_MODE") != "true":
        return False

    try:
        from dango.platform.docker import get_compose_project_name

        proj_name = get_compose_project_name(project_root)
        env = {**os.environ, "COMPOSE_PROJECT_NAME": proj_name}
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(project_root / "docker-compose.yml"),
                "start",
                "metabase",
            ],
            capture_output=True,
            timeout=120,
            env=env,
        )
        return True
    except Exception:
        logger.warning("metabase_start_after_writes_failed", exc_info=True)
        return False
