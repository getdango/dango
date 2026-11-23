"""dango/transformation/docs_locked.py

`dbt docs generate` under the DuckDB single-writer lock, for callers that run it outside any other
lock holder. Docs are non-critical: this never raises and a busy warehouse only yields a warning.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Seconds to wait for the lock (the wait the MCP ``generate_docs`` tool uses).
DOCS_LOCK_WAIT_SECONDS: float = 30.0


def generate_dbt_docs_locked(
    project_root: Path, *, source: str = "docs", timeout: float | None = None
) -> tuple[bool, str]:
    """Run ``generate_dbt_docs`` while holding the dbt lock.

    Returns ``(success, output)`` like ``generate_dbt_docs``. If the lock cannot be taken within
    ``timeout`` (default ``DOCS_LOCK_WAIT_SECONDS``) docs are not generated, one warning is logged
    and ``(False, message)`` is returned. Never raises.
    """
    from dango.exceptions import DbtLockError
    from dango.utils.dbt_lock import DbtLock

    wait = DOCS_LOCK_WAIT_SECONDS if timeout is None else timeout
    lock = None
    try:
        lock = DbtLock(project_root, source=source, operation="dbt docs generate")
        lock.acquire(timeout=wait)
    except DbtLockError:
        message = "dbt docs not generated: the warehouse is busy (another sync or dbt run)."
        logger.warning("dbt_docs_not_generated_warehouse_busy")
        return (False, message)
    except OSError as exc:
        logger.warning("dbt_docs_not_generated_lock_unavailable: %s", exc)
        return (False, f"dbt docs not generated: could not use the dbt lock ({exc})")

    try:
        from dango.transformation import generate_dbt_docs

        return generate_dbt_docs(project_root)
    except Exception as exc:  # docs are non-critical: report, never fail the caller
        logger.warning("dbt_docs_generation_error: %s", exc)
        return (False, f"dbt docs generate failed: {exc}")
    finally:
        lock.release()
