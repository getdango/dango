"""dango/utils/process.py

Generic process utilities shared by platform/ and cli/.
"""

import psutil


def is_process_running(pid: int) -> bool:
    """
    Check if process with given PID is running.

    Args:
        pid: Process ID

    Returns:
        True if process is running, False otherwise
    """
    try:
        # Use psutil for cross-platform compatibility
        return psutil.pid_exists(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_process(pid: int, timeout: int = 10) -> bool:
    """
    Kill process and its children gracefully (SIGTERM), then forcefully (SIGKILL) if needed.

    Args:
        pid: Process ID to kill
        timeout: Seconds to wait for graceful shutdown before force kill

    Returns:
        True if process was killed, False if it didn't exist or couldn't be killed
    """
    if not is_process_running(pid):
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
            except psutil.NoSuchProcess:
                pass

        # Wait for processes to exit
        gone, alive = psutil.wait_procs([proc] + children, timeout=timeout)

        if proc in alive:
            # Process didn't exit gracefully, force kill
            try:
                proc.kill()
            except psutil.NoSuchProcess:
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
