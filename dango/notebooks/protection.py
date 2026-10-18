"""dango/notebooks/protection.py

Make sure a running local Marimo requires an access token (restart a pre-token one).
"""

from __future__ import annotations

import logging
from pathlib import Path

from dango.notebooks.access_token import remove_token_file, token_file_path

logger = logging.getLogger(__name__)


def ensure_marimo_protected(project_root: Path) -> bool:
    """Restart a local Marimo that Dango started without a token; clean stale token files.

    Idempotent.  Returns ``True`` only when a tokenless Marimo was stopped, so
    the caller's next ``start_marimo`` brings it back up with a token.

    - Cloud: never touched (it runs ``--no-token`` behind authenticated proxies).
    - A pid record whose process is gone: its token file is removed (covers a
      Marimo that exited on its own idle timeout).  No pid record at all is
      left alone: ``start_marimo`` writes the record only after readiness.
    - A running process is stopped only when the pid record carries a start
      time (so it is verifiably the process Dango started) and no token file
      exists.  ``stop_marimo`` repeats the identity check before signalling.
    """
    from dango.config.helpers import is_running_on_cloud
    from dango.notebooks import manager

    if is_running_on_cloud():
        return False
    record = manager.read_pid_record(manager.get_marimo_pid_file_path(project_root))
    if record is None:
        return False
    if not manager.is_process_running(record.pid, expected_start_time=record.start_time):
        remove_token_file(project_root)
        return False
    if record.start_time is None or token_file_path(project_root).exists():
        return False
    logger.warning("Restarting Marimo (PID %d): it was started without an access token", record.pid)
    return manager.stop_marimo(project_root)
