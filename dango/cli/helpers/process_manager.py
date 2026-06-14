"""dango/cli/helpers/process_manager.py

FastAPI server process management and PID file helpers.
"""

import subprocess
import time
from pathlib import Path

from rich.console import Console

from dango.utils.process import is_process_running, kill_process

from .port_manager import check_port_in_use, get_process_using_port

console = Console()


def get_pid_file_path(project_root: Path) -> Path:
    """Get path to PID file for FastAPI server."""
    return project_root / ".dango" / "web.pid"


def write_pid_file(project_root: Path, pid: int) -> None:
    """
    Write PID to file.

    Args:
        project_root: Project root directory
        pid: Process ID to write
    """
    pid_file = get_pid_file_path(project_root)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid))


def read_pid_file(project_root: Path) -> int | None:
    """
    Read PID from file.

    Args:
        project_root: Project root directory

    Returns:
        PID if file exists and valid, None otherwise
    """
    pid_file = get_pid_file_path(project_root)

    if not pid_file.exists():
        return None

    try:
        pid_str = pid_file.read_text().strip()
        return int(pid_str)
    except (ValueError, OSError):
        return None


def remove_pid_file(project_root: Path) -> None:
    """
    Remove PID file.

    Args:
        project_root: Project root directory
    """
    pid_file = get_pid_file_path(project_root)
    try:
        if pid_file.exists():
            pid_file.unlink()
    except OSError:
        pass


def start_fastapi_server(project_root: Path, host: str = "0.0.0.0", port: int = 8080) -> int | None:
    """
    Start FastAPI server in background.

    Args:
        project_root: Project root directory
        host: Host to bind to
        port: Port to bind to

    Returns:
        PID of started process, or None if failed

    Raises:
        RuntimeError: If port is already in use or server fails to start
    """
    import sys

    # Check if we already have a PID file first (more informative error)
    existing_pid = read_pid_file(project_root)
    if existing_pid and is_process_running(existing_pid):
        raise RuntimeError(
            f"FastAPI server is already running (PID {existing_pid}).\n"
            f"Stop it with 'dango stop' or check status with 'dango status'"
        )

    # Clean up stale PID file if exists
    if existing_pid:
        remove_pid_file(project_root)

    # Check if port is already in use (by something else)
    if check_port_in_use(port):
        existing_pid_on_port = get_process_using_port(port)
        if existing_pid_on_port:
            raise RuntimeError(
                f"Port {port} is already in use by another process (PID {existing_pid_on_port}).\n"
                f"Kill it with: kill {existing_pid_on_port}\n"
                f"Or use a different port with: dango web --port <other_port>"
            )
        else:
            raise RuntimeError(
                f"Port {port} is already in use.\n"
                f"Find and stop the process using it, or use a different port."
            )

    # Start server as subprocess

    # Log file for server output
    log_file = project_root / ".dango" / "web.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Open log file
        log_handle = open(log_file, "w")  # noqa: SIM115

        # Pass project root via env var so the uvicorn worker can resolve it
        import os

        env = {**os.environ, "DANGO_PROJECT_ROOT": str(project_root)}

        # Start uvicorn server
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "dango.web.app:app",
                "--host",
                host,
                "--port",
                str(port),
                "--log-level",
                "info",
            ],
            cwd=project_root,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # Detach from parent session
        )

        # Give server a moment to start
        time.sleep(2)

        # Check if process is still running
        if proc.poll() is not None:
            # Process exited immediately, something went wrong
            log_handle.close()
            raise RuntimeError(f"FastAPI server failed to start. Check logs at {log_file}")

        # Write PID file
        write_pid_file(project_root, proc.pid)

        # Don't close log_handle - let subprocess write to it

        return proc.pid

    except Exception as e:
        if "log_handle" in locals():
            log_handle.close()
        raise RuntimeError(f"Failed to start FastAPI server: {e}") from e


def stop_fastapi_server(project_root: Path, verbose: bool = True) -> bool:
    """
    Stop FastAPI server.

    Args:
        project_root: Project root directory
        verbose: Print status messages

    Returns:
        True if server was stopped, False if it wasn't running
    """
    from dango.config import ConfigLoader

    # Try to stop using PID file first
    pid = read_pid_file(project_root)

    if pid is not None:
        if not is_process_running(pid):
            if verbose:
                console.print(
                    f"[yellow]⚠[/yellow] FastAPI server PID {pid} is not running (stale PID file)"
                )
            remove_pid_file(project_root)
        else:
            if verbose:
                console.print(f"Stopping FastAPI server (PID {pid})...")

            # Try to kill the process
            success = kill_process(pid, timeout=10)

            # Clean up PID file
            remove_pid_file(project_root)

            if success:
                if verbose:
                    console.print("[green]✓[/green] FastAPI server stopped")
                return True
            else:
                if verbose:
                    console.print(f"[yellow]⚠[/yellow] Failed to stop process {pid}")
                # Continue to try port-based cleanup below

    # If PID file is missing or process couldn't be killed, try to find by port
    try:
        # Load config to get the port
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        port = config.platform.port

        # Find processes using this port
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5
        )

        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")

            # Check each process to see if it's a Dango process
            dango_pids = []
            other_pids = []

            for proc_pid_str in pids:
                try:
                    proc_pid = int(proc_pid_str.strip())

                    # Get process command line
                    cmd_result = subprocess.run(
                        ["ps", "-p", str(proc_pid), "-o", "command="],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )

                    if cmd_result.returncode == 0:
                        cmd_line = cmd_result.stdout.strip()

                        # Check if it's a Dango uvicorn process
                        if "uvicorn" in cmd_line and "dango.web.app" in cmd_line:
                            dango_pids.append(proc_pid)
                        else:
                            other_pids.append((proc_pid, cmd_line))
                except (ValueError, Exception):
                    continue

            # Only auto-kill Dango processes
            if dango_pids:
                if verbose:
                    if pid is None:
                        console.print(
                            f"[blue]ℹ[/blue] Found Dango process(es) using port {port} (no PID file)"
                        )
                    console.print(f"Stopping Dango process(es) on port {port}...")

                killed_any = False
                for proc_pid in dango_pids:
                    if kill_process(proc_pid, timeout=5):
                        killed_any = True
                        if verbose:
                            console.print(f"  ✓ Killed Dango process {proc_pid}")

                if killed_any:
                    if verbose:
                        console.print("[green]✓[/green] FastAPI server stopped")
                    return True

            # Warn about non-Dango processes
            if other_pids:
                if verbose:
                    console.print(
                        f"[yellow]⚠[/yellow] Port {port} is in use by non-Dango process(es):"
                    )
                    for proc_pid, cmd_line in other_pids:
                        # Truncate long command lines
                        display_cmd = cmd_line if len(cmd_line) <= 60 else cmd_line[:57] + "..."
                        console.print(f"  PID {proc_pid}: {display_cmd}")
                    console.print()
                    console.print("[yellow]Refusing to kill non-Dango processes.[/yellow]")
                    console.print(
                        "[dim]Please manually stop these processes or change Dango's port.[/dim]"
                    )
                return False

            # No processes found (might have exited between lsof and ps)
            if not dango_pids and not other_pids:
                if verbose and pid is None:
                    console.print("[blue]ℹ[/blue] No FastAPI server PID file found")
                return False
        else:
            if verbose and pid is None:
                console.print("[blue]ℹ[/blue] No FastAPI server PID file found")
            return False

    except Exception as e:
        if verbose:
            console.print(f"[yellow]Warning:[/yellow] Could not check port: {e}")
        return False

    return False


def get_fastapi_status(project_root: Path) -> dict:
    """
    Get FastAPI server status.

    Args:
        project_root: Project root directory

    Returns:
        Dict with status info:
            - running: bool
            - pid: Optional[int]
            - port: int
            - url: Optional[str]
            - log_file: Path
    """
    pid = read_pid_file(project_root)
    log_file = project_root / ".dango" / "web.log"

    # Read port from project config (default 8800)
    port = 8800
    try:
        from dango.config import ConfigLoader

        config = ConfigLoader(project_root).load_config()
        port = config.platform.port
    except Exception:
        pass

    status: dict = {"running": False, "pid": None, "port": port, "url": None, "log_file": log_file}

    if pid and is_process_running(pid):
        status["running"] = True
        status["pid"] = pid
        status["url"] = f"http://localhost:{port}"

    return status
