"""dango/notebooks/marimo_process.py

Helpers that stop a Marimo server process (whole process group where safe).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from dango.utils.process import kill_process

logger = logging.getLogger(__name__)

# stop_marimo(): wait after SIGTERM, then after SIGKILL, for the process to go.
_STOP_TERM_TIMEOUT_SECONDS = 10.0
_STOP_KILL_TIMEOUT_SECONDS = 5.0


def _stop_unready_process(proc: subprocess.Popen[bytes]) -> None:
    """Stop a Marimo that never became ready, with its whole process group.

    Marimo starts with ``start_new_session=True``, so its pid is also its
    process group id: SIGTERM the group, then SIGKILL it after 5 s.  A
    process that was already reaped (``returncode`` set) is not signalled,
    since its pid and group id may since have been reused.
    """
    import signal

    def _signal_group(sig: int) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    try:
        _signal_group(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_group(signal.SIGKILL)
            proc.wait(timeout=5)
    except Exception:
        logger.warning("Could not stop unready Marimo process %s", proc.pid, exc_info=True)


def _process_gone(pid: int) -> bool:
    """True when *pid* no longer runs (absent, or a zombie awaiting reaping)."""
    import psutil

    try:
        return bool(psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until *pid* is gone or *timeout* seconds pass."""
    deadline = time.monotonic() + timeout
    while not _process_gone(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _stop_marimo_process(pid: int, start_time: float | None) -> str:
    """Stop the Marimo recorded as *pid*; return ``stopped``, ``survived`` or ``foreign``.

    Marimo is started with ``start_new_session=True``, so it leads its own
    process group (pgid == pid): SIGTERM the group, then SIGKILL it.  The
    group is signalled only when the pid file recorded the process start
    time (the caller has verified it still matches), the pid leads its own
    group, and that group is not ours.  Otherwise the plain process-tree
    kill is used.  A pid we may not signal (EPERM) belongs to someone else:
    ``foreign``.
    """
    import signal

    import psutil

    try:
        pgid = os.getpgid(pid)
        group_ok = start_time is not None and pgid == pid and pgid != os.getpgrp()
        if not group_ok:
            kill_process(
                pid, timeout=int(_STOP_TERM_TIMEOUT_SECONDS), expected_start_time=start_time
            )
            return "stopped" if _wait_gone(pid, _STOP_KILL_TIMEOUT_SECONDS) else "survived"

        for sig, timeout in (
            (signal.SIGTERM, _STOP_TERM_TIMEOUT_SECONDS),
            (signal.SIGKILL, _STOP_KILL_TIMEOUT_SECONDS),
        ):
            os.killpg(pid, sig)
            if _wait_gone(pid, timeout):
                return "stopped"
        return "survived"
    except ProcessLookupError:
        return "stopped"
    except (PermissionError, psutil.AccessDenied):
        return "foreign"
