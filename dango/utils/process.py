"""dango/utils/process.py

Generic process utilities shared by platform/ and cli/.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import psutil

# Tolerance (seconds) when comparing a recorded process start time against the
# live process's psutil create_time(). create_time() has sub-second precision, so an
# exact match is expected in practice — this tolerance exists only to absorb float
# round-tripping through JSON serialization, not to loosen the identity check.
_START_TIME_TOLERANCE_SECONDS = 1.0


@dataclass(frozen=True)
class PidRecord:
    """A PID plus the process identity info needed to detect PID reuse.

    ``start_time`` is ``psutil.Process(pid).create_time()`` at the moment the PID was
    recorded. ``None`` means identity is unknown — either the record predates this
    identity-check mechanism (an old-format bare-integer PID file) or the process
    could not be inspected when the record was written.
    """

    pid: int
    start_time: float | None = None


def get_process_start_time(pid: int) -> float | None:
    """Return the process's start time (``psutil.Process(pid).create_time()``).

    Returns None if the process doesn't exist or can't be inspected. The start time
    is monotonic and effectively unique per PID slot — it's the basis for detecting
    when the OS has reused a PID number for an unrelated process.
    """
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def write_pid_record(pid_file: Path, pid: int) -> None:
    """Write a PID file recording both the PID and the process's start time.

    Storing the start time alongside the bare PID lets a later reader verify that a
    PID number still refers to the same process it was recorded for, rather than
    trusting the number alone — see PidRecord / read_pid_record.
    """
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    start_time = get_process_start_time(pid)
    pid_file.write_text(json.dumps({"pid": pid, "start_time": start_time}))


def read_pid_record(pid_file: Path) -> PidRecord | None:
    """Read a PID file, tolerating both the current JSON format and the old
    bare-integer format written before identity verification existed.

    An old-format file (or any content that isn't the expected JSON shape) is
    treated as "identity unknown" (``start_time=None``) rather than raising — callers
    pass that through to ``is_process_running()``/``kill_process()``, which fall back
    to existence-only checking in that case. This keeps pre-fix projects working
    without a crash; it does not retroactively make old PID files safe against PID
    reuse, only new ones written by ``write_pid_record()``.
    """
    if not pid_file.exists():
        return None
    try:
        text = pid_file.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
        return PidRecord(pid=int(data["pid"]), start_time=data.get("start_time"))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Not JSON (or not the expected shape) — likely an old-format bare-integer
        # PID file. Fall back to parsing it as a plain int with unknown identity.
        try:
            return PidRecord(pid=int(text), start_time=None)
        except ValueError:
            return None


def ensure_std_fds() -> None:
    """Ensure file descriptors 0-2 (stdin/stdout/stderr) are open.

    When a process is daemonized (terminal closed), fd 0 may be closed.
    If open() then gets fd 0 for a log file, subprocess.Popen's
    dup2(devnull, 0) clobbers the log handle, crashing the child with
    "Bad file descriptor" at init_sys_streams.

    Call this before opening files that will be passed to subprocess.Popen.
    """
    for fd in range(3):
        try:
            os.fstat(fd)
        except OSError:
            os.open(os.devnull, os.O_RDWR)


def is_process_running(pid: int, expected_start_time: float | None = None) -> bool:
    """
    Check if process with given PID is running, and — when known — verify it's the
    same process that was originally recorded, not a different process the OS has
    since reused the PID number for.

    A bare PID existence check is not enough to safely act on a PID read from a file:
    once a process exits, the OS is free to reuse its PID number for an unrelated
    process. A stale PID file (e.g. from a project whose server crashed without
    cleanup) combined with PID reuse can make a completely different, unrelated
    process look like "the tracked process" — see 1.0.8-OPS-1. When
    expected_start_time is provided, this compares it against the live process's
    psutil.Process(pid).create_time(); a mismatch means the PID was reused and the
    originally-tracked process is gone, so this returns False even though *some*
    process currently holds that PID number.

    Args:
        pid: Process ID
        expected_start_time: create_time() recorded when the PID was originally
            written (see PidRecord/read_pid_record). None skips identity
            verification and checks existence only — used for old-format PID files
            that predate this check, where identity is genuinely unknown.

    Returns:
        True if process is running (and, when expected_start_time is given, its
        identity matches).
    """
    try:
        if not psutil.pid_exists(pid):
            return False
        if expected_start_time is None:
            return True
        actual_start_time = psutil.Process(pid).create_time()
        return abs(actual_start_time - expected_start_time) < _START_TIME_TOLERANCE_SECONDS
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def kill_process(pid: int, timeout: int = 10, expected_start_time: float | None = None) -> bool:
    """
    Kill process and its children gracefully (SIGTERM), then forcefully (SIGKILL) if needed.

    Args:
        pid: Process ID to kill
        timeout: Seconds to wait for graceful shutdown before force kill
        expected_start_time: Optional recorded start time to verify identity against
            before signaling — see is_process_running(). If the live process at
            `pid` doesn't match, this is a no-op (treated as "already stopped"), not
            an error, so a stale/reused PID is never signaled.

    Returns:
        True if process was killed, False if it didn't exist, its identity didn't
        match expected_start_time, or it couldn't be killed
    """
    if not is_process_running(pid, expected_start_time=expected_start_time):
        return False

    try:
        proc = psutil.Process(pid)

        # Get all child processes
        try:
            children = proc.children(recursive=True)
        except psutil.NoSuchProcess:
            return False

        # Try graceful shutdown (SIGTERM) on parent and children
        proc.terminate()
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:  # noqa: BLE001
                pass

        # Wait for processes to exit
        gone, alive = psutil.wait_procs([proc] + children, timeout=timeout)

        if proc in alive:
            # Process didn't exit gracefully, force kill
            try:
                proc.kill()
            except psutil.NoSuchProcess:  # noqa: BLE001
                pass

            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass

            # Wait one more time to confirm
            gone, alive = psutil.wait_procs([proc] + children, timeout=3)
            return proc not in alive

        return True

    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
