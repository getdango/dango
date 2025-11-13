"""
CLI Utilities

Helper functions for CLI operations.
"""

import subprocess
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dango.config import ConfigLoader, ProjectNotFoundError

console = Console()


def find_project_root(start_path: Optional[Path] = None) -> Path:
    """
    Find Dango project root directory.

    Args:
        start_path: Starting directory (defaults to current directory)

    Returns:
        Project root path

    Raises:
        ProjectNotFoundError: If not in a Dango project
    """
    loader = ConfigLoader(start_path)
    root = loader.find_project_root(start_path)

    if root is None:
        raise ProjectNotFoundError(
            "Not in a Dango project.\n"
            "Run 'dango init' to create a new project."
        )

    return root


def require_project_context(ctx: click.Context) -> Path:
    """
    Ensure command is run in a Dango project.

    Args:
        ctx: Click context

    Returns:
        Project root path

    Raises:
        click.ClickException: If not in a project
    """
    try:
        return find_project_root()
    except ProjectNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


def print_error(message: str):
    """Print error message"""
    console.print(f"[red]Error:[/red] {message}")


def print_success(message: str):
    """Print success message"""
    console.print(f"[green]✓[/green] {message}")


def print_info(message: str):
    """Print info message"""
    console.print(f"[blue]ℹ[/blue] {message}")


def print_warning(message: str):
    """Print warning message"""
    console.print(f"[yellow]⚠[/yellow] {message}")


def print_panel(content: str, title: str, border_style: str = "blue"):
    """Print content in a panel"""
    console.print(Panel(content, title=title, border_style=border_style))


def confirm(message: str, default: bool = False) -> bool:
    """
    Ask for confirmation.

    Args:
        message: Confirmation message
        default: Default value if user just presses Enter

    Returns:
        True if confirmed, False otherwise
    """
    return click.confirm(message, default=default)


def get_git_branch(project_root: Optional[Path] = None) -> Optional[str]:
    """
    Get current git branch name.

    Args:
        project_root: Project root directory (defaults to current directory)

    Returns:
        Branch name if in git repo, None otherwise
    """
    try:
        cwd = str(project_root) if project_root else None
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return None


def check_git_branch_warning(project_root: Optional[Path] = None) -> None:
    """
    Check if on main/master branch and show gentle warning.

    This is a friendly reminder to work on feature branches,
    not a hard blocker. Users can proceed if they choose.

    Args:
        project_root: Project root directory (defaults to current directory)
    """
    branch = get_git_branch(project_root)

    if branch in ["main", "master"]:
        console.print()
        console.print(Panel(
            "[yellow]⚠️  You're on the [bold]{}[/bold] branch.[/yellow]\n\n"
            "💡 Consider creating a feature branch for data changes:\n"
            "   [dim]git checkout -b data/update-sources[/dim]\n\n"
            "This makes it easier to review and rollback changes if needed.".format(branch),
            title="Git Branch Reminder",
            border_style="yellow",
        ))
        console.print()


# ==============================================================================
# Process/PID Management
# ==============================================================================

import os
import signal
import psutil
import time


def get_pid_file_path(project_root: Path) -> Path:
    """Get path to PID file for FastAPI server"""
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


def read_pid_file(project_root: Path) -> Optional[int]:
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


def check_port_in_use(port: int) -> bool:
    """
    Check if a port is already in use.

    Args:
        port: Port number to check

    Returns:
        True if port is in use, False otherwise
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Set SO_REUSEADDR to handle TIME_WAIT state
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def get_process_using_port(port: int) -> Optional[int]:
    """
    Get PID of process using a specific port.

    Args:
        port: Port number

    Returns:
        PID of process using the port, or None
    """
    try:
        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status == 'LISTEN':
                return conn.pid
    except (psutil.AccessDenied, AttributeError):
        pass

    return None


def start_fastapi_server(project_root: Path, host: str = "0.0.0.0", port: int = 8080) -> Optional[int]:
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
    import sys
    from pathlib import Path

    # Log file for server output
    log_file = project_root / ".dango" / "web.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Open log file
        log_handle = open(log_file, 'w')

        # Start uvicorn server
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "dango.web.app:app",
                "--host", host,
                "--port", str(port),
                "--log-level", "info"
            ],
            cwd=project_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True  # Detach from parent session
        )

        # Give server a moment to start
        time.sleep(2)

        # Check if process is still running
        if proc.poll() is not None:
            # Process exited immediately, something went wrong
            log_handle.close()
            raise RuntimeError(
                f"FastAPI server failed to start. Check logs at {log_file}"
            )

        # Write PID file
        write_pid_file(project_root, proc.pid)

        # Don't close log_handle - let subprocess write to it

        return proc.pid

    except Exception as e:
        if 'log_handle' in locals():
            log_handle.close()
        raise RuntimeError(f"Failed to start FastAPI server: {e}")


def stop_fastapi_server(project_root: Path, verbose: bool = True) -> bool:
    """
    Stop FastAPI server.

    Args:
        project_root: Project root directory
        verbose: Print status messages

    Returns:
        True if server was stopped, False if it wasn't running
    """
    import subprocess
    from dango.config import ConfigLoader

    # Try to stop using PID file first
    pid = read_pid_file(project_root)

    if pid is not None:
        if not is_process_running(pid):
            if verbose:
                print_warning(f"FastAPI server PID {pid} is not running (stale PID file)")
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
                    print_success("FastAPI server stopped")
                return True
            else:
                if verbose:
                    print_warning(f"Failed to stop process {pid}")
                # Continue to try port-based cleanup below

    # If PID file is missing or process couldn't be killed, try to find by port
    try:
        # Load config to get the port
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        port = config.platform.port

        # Find processes using this port
        result = subprocess.run(
            ['lsof', '-ti', f':{port}'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split('\n')

            # Check each process to see if it's a Dango process
            dango_pids = []
            other_pids = []

            for proc_pid in pids:
                try:
                    proc_pid = int(proc_pid.strip())

                    # Get process command line
                    cmd_result = subprocess.run(
                        ['ps', '-p', str(proc_pid), '-o', 'command='],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )

                    if cmd_result.returncode == 0:
                        cmd_line = cmd_result.stdout.strip()

                        # Check if it's a Dango uvicorn process
                        if 'uvicorn' in cmd_line and 'dango.web.app' in cmd_line:
                            dango_pids.append(proc_pid)
                        else:
                            other_pids.append((proc_pid, cmd_line))
                except (ValueError, Exception):
                    continue

            # Only auto-kill Dango processes
            if dango_pids:
                if verbose:
                    if pid is None:
                        print_info(f"Found Dango process(es) using port {port} (no PID file)")
                    console.print(f"Stopping Dango process(es) on port {port}...")

                killed_any = False
                for proc_pid in dango_pids:
                    if kill_process(proc_pid, timeout=5):
                        killed_any = True
                        if verbose:
                            console.print(f"  ✓ Killed Dango process {proc_pid}")

                if killed_any:
                    if verbose:
                        print_success("FastAPI server stopped")
                    return True

            # Warn about non-Dango processes
            if other_pids:
                if verbose:
                    print_warning(f"Port {port} is in use by non-Dango process(es):")
                    for proc_pid, cmd_line in other_pids:
                        # Truncate long command lines
                        display_cmd = cmd_line if len(cmd_line) <= 60 else cmd_line[:57] + "..."
                        console.print(f"  PID {proc_pid}: {display_cmd}")
                    console.print()
                    console.print(f"[yellow]Refusing to kill non-Dango processes.[/yellow]")
                    console.print(f"[dim]Please manually stop these processes or change Dango's port.[/dim]")
                return False

            # No processes found (might have exited between lsof and ps)
            if not dango_pids and not other_pids:
                if verbose and pid is None:
                    print_info("No FastAPI server PID file found")
                return False
        else:
            if verbose and pid is None:
                print_info("No FastAPI server PID file found")
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
    port = 8080  # Default port

    status = {
        "running": False,
        "pid": None,
        "port": port,
        "url": None,
        "log_file": log_file
    }

    if pid and is_process_running(pid):
        status["running"] = True
        status["pid"] = pid
        status["url"] = f"http://localhost:{port}"

    return status


# ==============================================================================
# File Watcher Management
# ==============================================================================


def get_watcher_pid_file_path(project_root: Path) -> Path:
    """Get path to PID file for file watcher"""
    return project_root / ".dango" / "watcher.pid"


def start_file_watcher(project_root: Path) -> Optional[int]:
    """
    Start file watcher in background.

    Args:
        project_root: Project root directory

    Returns:
        PID of started process, or None if failed

    Raises:
        RuntimeError: If watcher is already running or fails to start
    """
    import sys

    # Check if we already have a PID file first
    pid_file = get_watcher_pid_file_path(project_root)
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            if is_process_running(existing_pid):
                raise RuntimeError(
                    f"File watcher is already running (PID {existing_pid}).\n"
                    f"Stop it with 'dango stop'"
                )
            else:
                # Stale PID file, remove it
                pid_file.unlink()
        except (ValueError, OSError):
            # Invalid PID file, remove it
            pid_file.unlink()

    # Log file for watcher output
    log_file = project_root / ".dango" / "watcher.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Open log file
        log_handle = open(log_file, 'w')

        # Get path to watcher_runner.py
        import dango.platform
        platform_dir = Path(dango.platform.__file__).parent
        watcher_runner = platform_dir / "watcher_runner.py"

        # Start watcher runner
        proc = subprocess.Popen(
            [
                sys.executable,
                str(watcher_runner),
                str(project_root)
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True  # Detach from parent session
        )

        # Give watcher a moment to start
        time.sleep(1)

        # Check if process is still running
        if proc.poll() is not None:
            # Process exited immediately, something went wrong
            log_handle.close()
            raise RuntimeError(
                f"File watcher failed to start. Check logs at {log_file}"
            )

        # Write PID file
        pid_file.write_text(str(proc.pid))

        # Don't close log_handle - let subprocess write to it

        return proc.pid

    except Exception as e:
        if 'log_handle' in locals():
            log_handle.close()
        raise RuntimeError(f"Failed to start file watcher: {e}")


def stop_file_watcher(project_root: Path, verbose: bool = True) -> bool:
    """
    Stop file watcher.

    Args:
        project_root: Project root directory
        verbose: Print status messages

    Returns:
        True if watcher was stopped, False if it wasn't running
    """
    pid_file = get_watcher_pid_file_path(project_root)

    if not pid_file.exists():
        if verbose:
            print_info("No file watcher PID file found")
        return False

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        if verbose:
            print_warning("Invalid file watcher PID file")
        pid_file.unlink()
        return False

    if not is_process_running(pid):
        if verbose:
            print_warning(f"File watcher PID {pid} is not running (stale PID file)")
        pid_file.unlink()
        return False

    if verbose:
        console.print(f"Stopping file watcher (PID {pid})...")

    # Try to kill the process
    success = kill_process(pid, timeout=10)

    # Clean up PID file
    try:
        pid_file.unlink()
    except OSError:
        pass

    if success:
        if verbose:
            print_success("File watcher stopped")
        return True
    else:
        if verbose:
            print_warning(f"Failed to stop process {pid}")
        return False


def get_watcher_status(project_root: Path) -> dict:
    """
    Get file watcher status.

    Args:
        project_root: Project root directory

    Returns:
        Dict with status info:
            - running: bool
            - pid: Optional[int]
            - log_file: Path
    """
    pid_file = get_watcher_pid_file_path(project_root)
    log_file = project_root / ".dango" / "watcher.log"

    status = {
        "running": False,
        "pid": None,
        "log_file": log_file
    }

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if is_process_running(pid):
                status["running"] = True
                status["pid"] = pid
        except (ValueError, OSError):
            pass

    return status
