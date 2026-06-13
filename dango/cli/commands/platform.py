"""dango/cli/commands/platform.py

Platform lifecycle commands (start, stop, status) and port helpers.
"""

import click

from dango.cli import console
from dango.logging import get_logger

logger = get_logger(__name__)


def _check_duplicate_ports(platform_config: object) -> None:
    """Check for duplicate port configuration."""
    ports = {
        "Web UI": platform_config.port,
        "Metabase": platform_config.metabase_port,
        "dbt docs": platform_config.dbt_docs_port,
    }

    # Find duplicates
    port_to_services = {}
    for service, port in ports.items():
        if port not in port_to_services:
            port_to_services[port] = []
        port_to_services[port].append(service)

    # Check for conflicts
    duplicates = {
        port: services for port, services in port_to_services.items() if len(services) > 1
    }

    if duplicates:
        console.print("[red]✗ Duplicate port configuration detected:[/red]\n")

        for port, services in duplicates.items():
            console.print(f"  [yellow]Port {port}[/yellow] is configured for multiple services:")
            for service in services:
                console.print(f"    • {service}")

        console.print()
        console.print(
            "[bold]To fix:[/bold] Edit [cyan].dango/project.yml[/cyan] and use different ports\n"
        )
        console.print("  [dim]platform:[/dim]")
        console.print(f"    [cyan]port: {platform_config.port}[/cyan]           # Web UI")
        console.print(
            f"    [cyan]metabase_port: {platform_config.metabase_port + 1 if platform_config.metabase_port == platform_config.port else platform_config.metabase_port}[/cyan]  # Metabase"
        )
        console.print(
            f"    [cyan]dbt_docs_port: {platform_config.dbt_docs_port + 2 if platform_config.dbt_docs_port in [platform_config.port, platform_config.metabase_port] else platform_config.dbt_docs_port}[/cyan]  # dbt docs"
        )
        console.print()

        raise click.Abort()


def _check_docker_ports(platform_config: object) -> None:
    """Check if Docker service ports are available."""
    import socket
    import subprocess

    ports_to_check = [
        (platform_config.metabase_port, "Metabase", "metabase_port"),
        (platform_config.dbt_docs_port, "dbt docs", "dbt_docs_port"),
    ]

    conflicts = []

    for port, service_name, config_key in ports_to_check:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port_available = sock.connect_ex(("127.0.0.1", port)) != 0
        sock.close()

        if not port_available:
            # Port is in use - find what's using it
            process_info = None
            try:
                result = subprocess.run(
                    ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=2
                )

                if result.returncode == 0 and result.stdout.strip():
                    pid = result.stdout.strip().split("\n")[0]

                    # Get process command
                    cmd_result = subprocess.run(
                        ["ps", "-p", pid, "-o", "command="],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )

                    if cmd_result.returncode == 0:
                        process_info = cmd_result.stdout.strip()
                        if len(process_info) > 60:
                            process_info = process_info[:57] + "..."
            except Exception:
                logger.debug("port_process_lookup_failed", port=port, exc_info=True)

            conflicts.append((port, service_name, config_key, process_info))

    if conflicts:
        console.print("[red]✗ Port conflicts detected:[/red]\n")

        for port, service_name, _config_key, process_info in conflicts:
            console.print(f"  [yellow]Port {port}[/yellow] ({service_name}) is already in use")
            if process_info:
                console.print(f"    [dim]Used by: {process_info}[/dim]")

        console.print()
        console.print(
            "  [bold yellow]→ If you have a previous Dango instance running, "
            "run [cyan]dango stop[/cyan] first.[/bold yellow]\n"
        )
        console.print("[bold]Other options:[/bold]\n")
        console.print("[bold]Option 1:[/bold] Stop the conflicting process(es)")
        console.print(f"  [cyan]lsof -ti :{conflicts[0][0]} | xargs kill -9[/cyan]\n")

        console.print("[bold]Option 2:[/bold] Change ports in [cyan].dango/project.yml[/cyan]")
        console.print("  [dim]platform:[/dim]")
        for port, _service_name, config_key, _ in conflicts:
            console.print(f"    [cyan]{config_key}: {port + 1}[/cyan]  # Change from {port}")
        console.print()

        raise click.Abort()


@click.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.pass_context
def start(ctx: click.Context, yes: bool) -> None:
    """
    Start all Dango data platform services.

    Starts:
      - FastAPI backend (Web UI and API)
      - Metabase (BI dashboards)
      - dbt-docs (documentation server)
      - File watcher (if enabled)

    Access platform at http://localhost:<port> (default: 8800)
    Change port in .dango/project.yml under platform.port
    """
    from dango.config import ConfigLoader
    from dango.platform.common.startup import (
        check_duckdb_version_alignment,
        ensure_dbt_schemas,
        ensure_duckdb_driver,
        import_dashboards,
        rotate_logs,
        run_pending_migrations,
        setup_metabase_if_needed,
        start_docker_services,
    )

    from ..helpers.process_manager import start_fastapi_server
    from ..utils import check_v01x_project, require_project_context

    check_v01x_project()

    console.print("🍡 [bold]Starting Dango Platform...[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)

        # Detect stale PID file from a previous crash
        from ..helpers.process_manager import read_pid_file, remove_pid_file

        stale_pid = read_pid_file(project_root)
        if stale_pid is not None:
            import os

            try:
                os.kill(stale_pid, 0)
                # Process is alive — existing server running, port check will catch it
            except ProcessLookupError:
                # Process is dead (ESRCH) — stale PID file
                # Note: PermissionError (EPERM) means the process exists but is
                # owned by another user — let the port-conflict check handle it.
                console.print("  Server was stopped. Restarting...")
                remove_pid_file(project_root)
                console.print()

        # Guard rail: inform if this looks like a cloned project
        sources_file = project_root / ".dango" / "sources.yml"
        dango_db = project_root / ".dango" / "dango.db"
        warehouse = project_root / "data" / "warehouse.duckdb"
        if sources_file.exists() and not dango_db.exists() and not warehouse.exists():
            console.print(
                "[yellow]  This looks like a cloned project. "
                "Run `dango sync` to load data before starting.[/yellow]"
            )
            console.print()

        # Load project config
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        project_name = config.project.name
        platform_config = config.platform

        # Version alignment check — abort early if Python DuckDB ≠ driver major.minor
        # Must run BEFORE any DuckDB write operations (migrations, schema setup)
        # because write mode auto-migrates the file format irreversibly.
        try:
            check_duckdb_version_alignment()
        except Exception as version_exc:
            from dango.exceptions import VersionMismatchError

            if isinstance(version_exc, VersionMismatchError):
                console.print(f"[red]❌ DuckDB version mismatch:[/red] {version_exc}")
                raise click.Abort() from version_exc
            raise

        # Rotate JSONL logs (never-fail)
        rotate_logs(project_root)

        # Auto-migrate databases
        try:
            migration_results = run_pending_migrations(project_root)
            for migration_db_name, migration_applied in migration_results.items():
                if migration_applied:
                    console.print(
                        f"  Applied {len(migration_applied)} migration(s) to {migration_db_name}"
                    )
        except Exception as migration_exc:
            from dango.exceptions import MigrationError

            if isinstance(migration_exc, MigrationError):
                console.print(f"[red]Migration error:[/red] {migration_exc}")
                raise click.Abort() from migration_exc
            raise

        # Clean up stale dbt lock from crashed process
        from dango.platform.common.startup import cleanup_stale_dbt_lock

        if cleanup_stale_dbt_lock(project_root):
            console.print("[yellow]⚠[/yellow] Removed stale dbt lock from crashed process")

        # Ensure all dbt schemas exist (for Metabase visibility)
        ensure_dbt_schemas(project_root)

        # Get port from config
        port = platform_config.port
        base_url = f"http://localhost:{port}"

        # Check if port is available
        import socket
        import subprocess

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port_available = sock.connect_ex(("127.0.0.1", port)) != 0
        sock.close()

        if not port_available:
            # Port is in use - check if it's a zombie Dango process
            console.print(f"[yellow]⚠[/yellow]  Port {port} is already in use")
            console.print()

            # Try to find and kill zombie processes
            try:
                result = subprocess.run(
                    ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5
                )

                if result.returncode == 0 and result.stdout.strip():
                    pids = result.stdout.strip().split("\n")

                    # Check each process to see if it's a Dango process
                    dango_pids = []
                    other_pids = []

                    for proc_pid in pids:
                        try:
                            proc_pid = int(proc_pid.strip())

                            # Get process command line to verify it's Dango
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
                        console.print(
                            f"[dim]Found {len(dango_pids)} Dango process(es) using port {port}[/dim]"
                        )
                        console.print("[dim]Attempting to stop zombie Dango processes...[/dim]")
                        console.print()

                        from dango.utils.process import kill_process

                        killed_any = False
                        for proc_pid in dango_pids:
                            if kill_process(proc_pid, timeout=5):
                                killed_any = True
                                console.print(f"[green]✓[/green] Stopped Dango process {proc_pid}")

                        if killed_any:
                            console.print()
                            console.print(
                                f"[green]✓[/green] Cleared port {port}, retrying start..."
                            )
                            console.print()
                            # Recheck port availability
                            import time

                            time.sleep(1)  # Give processes time to clean up
                            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            port_available = sock.connect_ex(("127.0.0.1", port)) != 0
                            sock.close()

                            if not port_available:
                                console.print(
                                    f"[red]✗[/red] Port {port} is still in use after cleanup"
                                )
                                console.print()
                                console.print("[bold]Options:[/bold]")
                                console.print("  1. Wait a few seconds and try again")
                                console.print(
                                    "  2. Change Dango's port in [cyan].dango/project.yml[/cyan]"
                                )
                                console.print()
                                raise click.Abort()
                        else:
                            console.print(
                                f"[red]✗[/red] Could not automatically stop Dango processes on port {port}"
                            )
                            console.print()
                            console.print("[bold]Options:[/bold]")
                            console.print("  1. Wait a few seconds and try again")
                            console.print(
                                "  2. Change Dango's port in [cyan].dango/project.yml[/cyan]"
                            )
                            console.print()
                            raise click.Abort()

                    # Warn about non-Dango processes and refuse to continue
                    elif other_pids:
                        console.print(
                            f"[red]✗[/red] Port {port} is in use by non-Dango process(es):"
                        )
                        console.print()
                        for proc_pid, cmd_line in other_pids:
                            # Truncate long command lines
                            display_cmd = cmd_line if len(cmd_line) <= 60 else cmd_line[:57] + "..."
                            console.print(f"  [dim]PID {proc_pid}:[/dim] {display_cmd}")
                        console.print()
                        console.print("[yellow]⚠  Refusing to kill non-Dango processes.[/yellow]")
                        console.print()
                        console.print("[bold]Option 1: Kill the process manually[/bold]")
                        if len(other_pids) == 1:
                            console.print(f"  [cyan]kill {other_pids[0][0]}[/cyan]")
                        else:
                            pids_str = " ".join(str(pid) for pid, _ in other_pids)
                            console.print(f"  [cyan]kill {pids_str}[/cyan]")
                        console.print()
                        console.print("[bold]Option 2: Change Dango's port[/bold]")
                        console.print("  Edit [cyan].dango/project.yml[/cyan]:")
                        console.print("[dim]  platform:[/dim]")
                        console.print(
                            f"[dim]    port: 9000  # Change from {port} to any free port[/dim]"
                        )
                        console.print()
                        raise click.Abort()
                    else:
                        # No processes found (might have exited between checks)
                        console.print(f"[red]✗[/red] Port {port} is in use but process not found")
                        console.print()
                        console.print("[bold]Options:[/bold]")
                        console.print("  1. Wait a few seconds and try again")
                        console.print("  2. Change Dango's port in [cyan].dango/project.yml[/cyan]")
                        console.print()
                        raise click.Abort()
            except subprocess.TimeoutExpired:
                console.print(f"[red]✗[/red] Timeout checking port {port}")
                console.print()
                raise click.Abort() from None

        console.print(f"[dim]Using port {port} (change in .dango/project.yml if needed)[/dim]")
        console.print()

        # Check for duplicate port configuration
        _check_duplicate_ports(platform_config)

        # Auto-clean orphaned Docker containers from previous Dango session
        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "--filter", "name=metabase", "--filter", "name=dbt"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                container_ids = [c for c in result.stdout.strip().split("\n") if c]
                console.print(
                    f"[yellow]⚠[/yellow]  Found {len(container_ids)} orphaned Docker "
                    "container(s) from a previous session, cleaning up..."
                )
                from dango.platform import DockerManager

                manager = DockerManager(project_root)
                manager.stop_services()
                manager.stop_all_dango_containers()
                console.print()
        except Exception:
            logger.debug("orphan_docker_cleanup_failed")

        # Check Docker service ports (Metabase and dbt-docs)
        _check_docker_ports(platform_config)

        # Ensure DuckDB driver is present (download if missing)
        driver_path = project_root / "metabase-plugins" / "duckdb.metabase-driver.jar"
        if not driver_path.exists():
            console.print("[yellow]⚠ DuckDB driver not found, downloading now...[/yellow]")
            console.print()
        try:
            ensure_duckdb_driver(project_root)
            if not driver_path.exists():
                # Was missing but now downloaded
                pass
            else:
                driver_size_mb = driver_path.stat().st_size // 1024 // 1024
                console.print(f"[green]✓[/green] DuckDB driver ready ({driver_size_mb}MB)")
                console.print()
        except RuntimeError as e:
            console.print("[red]❌ Failed to download DuckDB driver[/red]")
            console.print()
            console.print("[bold]This is required for Metabase to connect to DuckDB.[/bold]")
            console.print()
            console.print("[bold]To fix:[/bold]")
            console.print("  1. Check your internet connection")
            console.print("  2. Try running '[cyan]dango start[/cyan]' again")
            console.print("  3. Or manually download from:")
            console.print("     https://github.com/motherduckdb/metabase_duckdb_driver/releases")
            console.print(f"     Save as: {driver_path}")
            console.print()
            raise click.Abort() from e

        # Start Docker services (Metabase, dbt-docs) — includes daemon check + port check
        console.print("[cyan]Starting Docker services...[/cyan]")
        try:
            start_docker_services(project_root)
            console.print("[green]✓[/green] Docker services started")
        except RuntimeError as e:
            err_msg = str(e)
            if "Docker daemon is not running" in err_msg:
                console.print("[red]❌ Error: Docker daemon is not running[/red]")
                console.print()
                console.print("Dango requires Docker to run Metabase and other services.")
                console.print()
                console.print("[bold]Please start Docker Desktop first:[/bold]")
                console.print("  1. Open Docker Desktop application")
                console.print("  2. Wait for it to fully start (whale icon in menu bar)")
                console.print("  3. Run '[cyan]dango start[/cyan]' again")
            elif "ports are still in use" in err_msg:
                console.print("[red]❌ Error: Required ports are still in use[/red]")
                console.print()
                console.print("[bold]Manual cleanup required:[/bold]")
                console.print("  lsof -ti:3000 | xargs kill -9")
                console.print("  lsof -ti:8081 | xargs kill -9")
            else:
                console.print("[red]❌ Docker services failed to start[/red]")
                console.print()
                console.print("Dango requires Docker services (Metabase + dbt-docs) to function.")
                console.print()
                console.print("[bold]Troubleshooting:[/bold]")
                console.print("  1. Check Docker logs: '[cyan]docker ps -a[/cyan]'")
                console.print("  2. Try again: '[cyan]dango start[/cyan]'")
            console.print()
            raise click.Abort() from e

        # Metabase auto-setup (first-time only)
        console.print()
        console.print("[cyan]Checking Metabase setup...[/cyan]")
        metabase_configured = False
        organization = getattr(config.project, "organization", None)
        credentials_file = project_root / ".dango" / "metabase.yml"
        if not credentials_file.exists():
            console.print("[dim]First time setup detected...[/dim]")
        try:
            setup_result = setup_metabase_if_needed(project_root, project_name, organization)
            if setup_result.get("already_configured"):
                console.print("[green]✓[/green] Metabase already configured")
                metabase_configured = True
            elif setup_result.get("success"):
                console.print("[green]✓[/green] Metabase configured automatically")
                if setup_result.get("collections_created"):
                    console.print(
                        f"[dim]  Collections: {', '.join(setup_result['collections_created'])}[/dim]"
                    )
                metabase_configured = True
            else:
                # Partial success (DuckDB connected, non-critical errors)
                console.print("[red]✗[/red] Metabase setup failed")
                if setup_result.get("errors"):
                    for error in setup_result["errors"]:
                        console.print(f"[red]  • {error}[/red]")
                console.print()
                console.print(
                    "[yellow]⚠ Metabase partially configured (DuckDB connected, but setup incomplete)[/yellow]"
                )
                console.print(
                    "[dim]  You can manually complete setup at http://localhost:3000[/dim]"
                )
                metabase_configured = True  # Allow platform to start
        except RuntimeError as e:
            # Metabase setup raised — stop Docker and show troubleshooting
            console.print()
            console.print("[red]❌ Critical error: Cannot connect Metabase to DuckDB[/red]")
            console.print()
            console.print("[yellow]Rolling back: Stopping Docker services...[/yellow]")
            from dango.platform import DockerManager

            DockerManager(project_root).stop_services()
            console.print()
            console.print("[bold]All services have been stopped.[/bold]")
            console.print()
            console.print("[bold]Troubleshooting:[/bold]")
            console.print("  1. Check if DuckDB file exists: data/warehouse.duckdb")
            console.print("  2. Verify DuckDB driver: metabase-plugins/duckdb.metabase-driver.jar")
            console.print(
                "  3. Check Metabase logs: '[cyan]docker logs $(docker ps -q -f name=metabase)[/cyan]'"
            )
            console.print("  4. Try again: '[cyan]dango start[/cyan]'")
            console.print()
            raise click.Abort() from e

        # Import dashboards (if any exist)
        try:
            import_result = import_dashboards(project_root)
            if import_result is not None:
                console.print()
                console.print("[cyan]Importing dashboards...[/cyan]")
                if import_result.get("imported"):
                    console.print(
                        f"[green]✓[/green] Imported {len(import_result['imported'])} dashboard(s)"
                    )
                elif import_result.get("skipped"):
                    console.print("[dim]✓ All dashboards already imported[/dim]")
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow]  Dashboard import failed: {e}")

        console.print()

        # Start FastAPI backend (critical - must succeed)
        console.print("[cyan]Starting Web UI backend...[/cyan]")
        fastapi_pid = None
        try:
            fastapi_pid = start_fastapi_server(project_root, host="127.0.0.1", port=port)
            console.print(f"[green]✓[/green] Web UI started (PID {fastapi_pid})")
            console.print()
        except RuntimeError as e:
            # FastAPI failed - roll back Docker services
            from dango.platform import DockerManager

            console.print(f"[red]❌ Web UI failed to start:[/red] {e}")
            console.print()
            console.print("[yellow]Rolling back: Stopping Docker services...[/yellow]")
            DockerManager(project_root).stop_services()
            console.print()
            console.print("[bold]All services have been stopped.[/bold]")
            console.print("Fix the issue above and run '[cyan]dango start[/cyan]' again.")
            console.print()
            raise click.Abort() from e

        # Start file watcher if auto-sync is enabled (non-critical - can continue without it)
        if platform_config.auto_sync:
            console.print("[cyan]Starting file watcher...[/cyan]")
            try:
                from dango.platform.watcher_lifecycle import (
                    kill_orphan_watchers,
                    start_file_watcher,
                    stop_file_watcher,
                )

                orphans_killed = kill_orphan_watchers(project_root)
                if orphans_killed:
                    console.print(f"[yellow]⚠[/yellow] Killed {orphans_killed} orphaned watcher(s)")
                stop_file_watcher(project_root)  # Clean up any orphaned watcher
                watcher_pid = start_file_watcher(project_root)
                console.print(f"[green]✓[/green] File watcher started (PID {watcher_pid})")

                # Show what's being watched
                watch_dirs = ", ".join(platform_config.watch_directories)
                console.print(f"[dim]  Watching: {watch_dirs}[/dim]")
                console.print(f"[dim]  Debounce: {platform_config.debounce_seconds}s[/dim]")

                if platform_config.auto_dbt:
                    console.print("[dim]  Auto-cascade: sync → dbt[/dim]")

                console.print()
            except RuntimeError as e:
                # File watcher is non-critical - platform still works without it
                console.print(f"[yellow]⚠[/yellow]  File watcher failed to start: {e}")
                from dango.exceptions import is_debug_mode

                if is_debug_mode():
                    import traceback

                    console.print(traceback.format_exc())
                console.print("[dim]Platform will work, but auto-sync is disabled.[/dim]")
                console.print("[dim]You can manually run 'dango sync' when files change.[/dim]")
                console.print()
        else:
            console.print("[dim]File watcher disabled (auto_sync=false)[/dim]")
            console.print()

        # Print success summary
        if metabase_configured:
            console.print("[green]🎉 Dango is running![/green]")
        else:
            console.print("[yellow]⚠ Dango is running (with issues)[/yellow]")
        console.print()
        console.print("[bold cyan]Access your platform:[/bold cyan]")
        console.print()
        console.print(f"  Dashboard:  [link={base_url}]{base_url}[/link]")
        if metabase_configured:
            console.print(f"  Metabase:   [link={base_url}/metabase]{base_url}/metabase[/link]")
        else:
            console.print(
                f"  Metabase:   [dim strikethrough]{base_url}/metabase[/dim strikethrough] [red](not configured)[/red]"
            )
        console.print(f"  dbt Docs:   [link={base_url}/dbt-docs]{base_url}/dbt-docs[/link]")
        console.print(f"  API:        [link={base_url}/api]{base_url}/api[/link]")
        console.print()
        console.print("[dim]💡 Change port in .dango/project.yml under platform.port[/dim]")
        console.print()

        # Password reminder — help users who forgot their admin credentials
        try:
            from dango.auth.admin import get_auth_db_path

            db_path = get_auth_db_path(project_root)
            if db_path.exists():
                console.print(
                    "[dim]🔑 Forgot password? Run 'dango auth reset-password <email>'[/dim]"
                )
                console.print()
        except Exception:
            pass  # auth module may not be initialized yet

        console.print("[dim]Run 'dango stop' to shut down services.[/dim]")

        # Open dashboard in browser after health check
        import time
        import webbrowser

        import requests

        console.print()
        console.print("[dim]Waiting for services to be ready...[/dim]")

        # Wait for both FastAPI and Metabase to be ready
        from dango.utils.process import is_process_running

        max_wait = 180
        fastapi_ready = False
        metabase_ready = False
        process_died = False

        for _i in range(max_wait):
            # Check if FastAPI process is still alive
            if fastapi_pid and not is_process_running(fastapi_pid):
                console.print("[red]⚠[/red]  Web UI process exited unexpectedly")
                process_died = True
                break

            try:
                if not fastapi_ready:
                    response = requests.get(f"{base_url}/api/health", timeout=1)
                    if response.status_code == 200:
                        fastapi_ready = True
                        console.print("[dim]  ✓ Web UI ready[/dim]")
            except Exception:
                pass

            try:
                if not metabase_ready:
                    metabase_response = requests.get("http://localhost:3000/api/health", timeout=1)
                    if metabase_response.status_code == 200:
                        metabase_ready = True
                        console.print("[dim]  ✓ Metabase ready[/dim]")
            except Exception:
                pass

            if fastapi_ready and metabase_ready:
                break

            time.sleep(1)

        if not process_died:
            if not fastapi_ready:
                console.print(
                    "[yellow]⚠[/yellow]  Web UI not ready within timeout (may still be starting)"
                )
            if not metabase_ready:
                console.print(
                    "[yellow]⚠[/yellow]  Metabase not ready within timeout (may still be starting)"
                )

        try:
            webbrowser.open(base_url)
            console.print("[dim]✨ Opening Dango in your browser...[/dim]")
        except Exception:
            logger.debug("browser_open_failed", exc_info=True)

    except click.Abort:
        # User cancelled or intentional abort - re-raise without extra cleanup
        # (cleanup already handled where abort was raised)
        raise
    except Exception as e:
        # Unexpected error - roll back everything
        console.print()
        console.print(f"[red]❌ Unexpected error:[/red] {e}")
        console.print()
        console.print("[yellow]Rolling back: Stopping all services...[/yellow]")

        # Try to clean up what we can
        try:
            # Stop FastAPI if it was started
            from dango.platform.watcher_lifecycle import stop_file_watcher

            from ..helpers.process_manager import stop_fastapi_server

            stop_fastapi_server(project_root, verbose=False)
            stop_file_watcher(project_root)

            # Stop Docker services
            from dango.platform import DockerManager

            DockerManager(project_root).stop_services()
        except Exception:
            logger.debug("rollback_cleanup_failed", exc_info=True)

        console.print()
        console.print("[bold]All services have been stopped.[/bold]")
        console.print()
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print("[dim]Stack trace:[/dim]")
            console.print(traceback.format_exc())
            console.print()
        raise click.Abort() from None


@click.command()
@click.option("--all", "stop_all", is_flag=True, help="Stop ALL Dango containers from any project")
@click.pass_context
def stop(ctx: click.Context, stop_all: bool) -> None:
    """
    Stop all Dango data platform services.

    Stops:
      - FastAPI backend (Web UI)
      - File watcher
      - Metabase (BI dashboards)
      - dbt-docs (documentation server)

    Use --all to stop containers from ALL projects (useful when switching between projects).
    """
    from pathlib import Path

    from dango.config import ConfigLoader
    from dango.platform import DockerManager
    from dango.platform.network import NetworkConfig

    from ..helpers.process_manager import stop_fastapi_server
    from ..utils import require_project_context

    console.print("🍡 [bold]Stopping Dango Platform...[/bold]")
    console.print()

    # Handle --all flag (doesn't require project context)
    if stop_all:
        # Create a dummy manager just to call the global cleanup method
        manager = DockerManager(Path.cwd())
        manager.stop_all_dango_containers()
        console.print()
        console.print("[green]✅ Stopped all Dango containers[/green]")
        console.print()
        return

    try:
        project_root = require_project_context(ctx)

        # Load project config
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        project_name = config.project.name

        console.print("[cyan]Stopping services...[/cyan]")

        # Stop file watcher
        from dango.platform.watcher_lifecycle import (
            get_watcher_status,
            kill_orphan_watchers,
            stop_file_watcher,
        )

        watcher_was_running = get_watcher_status(project_root)["running"]
        watcher_stopped = stop_file_watcher(project_root)
        if watcher_was_running and not watcher_stopped:
            console.print("[yellow]⚠[/yellow] Failed to stop file watcher")
        orphans_killed = kill_orphan_watchers(project_root)
        if orphans_killed:
            console.print(f"[yellow]⚠[/yellow] Killed {orphans_killed} orphaned watcher(s)")

        # Stop Marimo notebook server
        from dango.notebooks.manager import get_marimo_status, stop_idle_checker, stop_marimo

        stop_idle_checker()
        marimo_was_running = get_marimo_status(project_root)["running"]
        marimo_stopped = stop_marimo(project_root)
        if marimo_was_running and not marimo_stopped:
            console.print("[yellow]⚠[/yellow] Failed to stop Marimo")

        # Stop FastAPI backend
        stop_fastapi_server(project_root, verbose=False)

        # Stop Docker services
        manager = DockerManager(project_root)
        docker_success = manager.stop_services()
        if not docker_success:
            console.print(
                "[yellow]Warning:[/yellow] Some Docker services may not have stopped cleanly"
            )

        # Update project status in routing registry
        net_config = NetworkConfig()
        project_info = net_config.get_project_info(project_name)
        if project_info:
            net_config.update_project_status(project_name, "stopped")
            console.print(f"[dim]✓ Marked {project_name} as stopped[/dim]")

        console.print()
        console.print("[green]✅ All services stopped[/green]")
        console.print()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """
    Show Dango platform status.

    Displays:
      - Project information
      - Service health (running/stopped)
      - Access URLs
      - Network routing (if using shared nginx)
    """
    from rich.table import Table

    from dango.config import ConfigLoader
    from dango.platform import DockerManager
    from dango.platform.network import NetworkConfig, NginxManager

    from ..helpers.process_manager import get_fastapi_status
    from ..utils import require_project_context

    console.print("🍡 [bold]Dango Platform Status[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)

        # Load project config
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        project_name = config.project.name

        # Initialize network managers
        net_config = NetworkConfig()
        nginx_manager = NginxManager(net_config)

        # Get project network info
        project_info = net_config.get_project_info(project_name)

        # Display project info
        if project_info:
            console.print(f"[bold]Project:[/bold] {project_name}")
            if hasattr(config.project, "organization"):
                console.print(f"[bold]Organization:[/bold] {config.project.organization}")
            console.print(f"[bold]Status:[/bold] {project_info['status'].capitalize()}")
            console.print(f"[bold]URL:[/bold] http://{project_info['domain']}")
            console.print()
        else:
            console.print(f"[bold]Project:[/bold] {project_name}")
            console.print("[bold]Status:[/bold] Not registered (running in localhost mode)")
            console.print()

        # Get FastAPI status
        fastapi_status = get_fastapi_status(project_root)

        # Get file watcher status
        from dango.platform.watcher_lifecycle import get_watcher_status

        watcher_status = get_watcher_status(project_root)

        # Get Docker services status
        manager = DockerManager(project_root)
        docker_statuses = manager.get_service_status()

        # Determine base URL
        if project_info:
            base_url = f"http://{project_info['domain']}"
        elif fastapi_status["running"]:
            # Use the port from running FastAPI
            base_url = fastapi_status["url"]
        else:
            base_url = "http://localhost:8800"

        # Create status table
        table = Table(title="Services", show_header=True, header_style="bold cyan")
        table.add_column("Service", style="bold")
        table.add_column("Status")

        # Add nginx status (if using domain mode)
        if nginx_manager.is_running():
            table.add_row("nginx (shared, port 80)", "[green]● Running[/green]")

        # Add DuckDB (always embedded)
        table.add_row("DuckDB (embedded)", "[green]● Running[/green]")

        # Add file watcher
        if watcher_status["running"]:
            table.add_row(
                "File Watcher (auto-sync)",
                f"[green]● Running[/green] (PID {watcher_status['pid']})",
            )
        else:
            # Check if auto-sync is enabled
            config = config_loader.load_config()
            if config.platform.auto_sync:
                table.add_row("File Watcher (auto-sync)", "[red]● Stopped[/red]")
            else:
                table.add_row("File Watcher (auto-sync)", "[dim]● Disabled[/dim]")

        # Add Web UI / FastAPI — BUG-243: cloud uses systemd, local uses PID file
        from dango.config.helpers import is_running_on_cloud

        cloud_mode = is_running_on_cloud()
        if cloud_mode:
            import subprocess

            try:
                systemd_result = subprocess.run(
                    ["systemctl", "is-active", "dango-web"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                state = systemd_result.stdout.strip()
                if state == "active":
                    table.add_row("Web UI (systemd)", "[green]● Running[/green]")
                else:
                    table.add_row(
                        "Web UI (systemd)",
                        f"[red]● {state.capitalize() if state else 'Unknown'}[/red]",
                    )
            except Exception:
                logger.debug("systemd_status_check_failed", exc_info=True)
                table.add_row("Web UI (systemd)", "[yellow]● Unknown[/yellow]")
        elif fastapi_status["running"]:
            table.add_row(
                f"Web UI (port {fastapi_status['port']})",
                f"[green]● Running[/green] (PID {fastapi_status['pid']})",
            )
        else:
            table.add_row(f"Web UI (port {fastapi_status['port']})", "[red]● Stopped[/red]")

        # Add Metabase
        if docker_statuses and "metabase" in docker_statuses:
            svc_status = docker_statuses["metabase"]
            if svc_status.value == "running":
                status_text = "[green]● Running[/green]"
            else:
                status_text = (
                    f"[{svc_status.value}]● {svc_status.value.capitalize()}[/{svc_status.value}]"
                )
            table.add_row("Metabase (port 3000)", status_text)
        else:
            table.add_row("Metabase (port 3000)", "[red]● Stopped[/red]")

        console.print(table)
        console.print()

        # OAuth token health (stored metadata only — no API calls for fast output)
        try:
            from dango.oauth.storage import OAuthStorage

            oauth_storage = OAuthStorage(project_root)
            oauth_creds = oauth_storage.list()

            if oauth_creds:
                oauth_table = Table(
                    title="OAuth Tokens", show_header=True, header_style="bold cyan"
                )
                oauth_table.add_column("Source", style="bold")
                oauth_table.add_column("Status")
                oauth_table.add_column("Account", style="dim")

                has_warning = False
                for cred in oauth_creds:
                    if cred.is_expired():
                        token_status = "[red]Expired[/red]"
                        has_warning = True
                    elif cred.is_expiring_soon():
                        days_left = cred.days_until_expiry()
                        token_status = f"[yellow]Expires in {days_left}d[/yellow]"
                        has_warning = True
                    else:
                        token_status = "[green]Active[/green]"
                    oauth_table.add_row(cred.source_type, token_status, cred.account_info)

                console.print(oauth_table)
                if has_warning:
                    console.print(
                        "  [yellow]Run 'dango oauth check' for live token validation[/yellow]"
                    )
                console.print()
        except Exception:
            logger.debug("oauth_token_display_failed", exc_info=True)

        # Print quick start commands
        any_running = fastapi_status["running"] or bool(docker_statuses)
        if not any_running:
            console.print("[yellow]No services running[/yellow]")
            console.print()
            console.print("Start services with: [cyan]dango start[/cyan]")
        else:
            console.print("[green]✓ Platform is running[/green]")
            console.print()
            console.print("[bold]Access your platform:[/bold]")
            console.print(f"  Dashboard:  {base_url}")
            console.print(f"  Metabase:   {base_url}/metabase")
            console.print(f"  dbt Docs:   {base_url}/dbt-docs")
            console.print(f"  API:        {base_url}/api")
            console.print()

        # Show active routes if nginx is running
        if nginx_manager.is_running() and net_config.list_projects():
            console.print("[bold]Active Routes (shared nginx):[/bold]")
            for name, info in net_config.list_projects().items():
                if info["status"] == "running":
                    marker = " (this project)" if name == project_name else ""
                    console.print(
                        f"  [green]✓[/green] {info['domain']} → localhost:{info['backend_port']}{marker}"
                    )
                else:
                    console.print(
                        f"  [dim]○ {info['domain']} → localhost:{info['backend_port']} (stopped)[/dim]"
                    )
            console.print()

        if any_running:
            console.print("[dim]Run 'dango stop' to shut down services.[/dim]")

        # Show log file location if FastAPI was running
        if fastapi_status["log_file"].exists():
            console.print()
            console.print(f"[dim]Logs: {fastapi_status['log_file']}[/dim]")

        # Check for available updates (cached, non-blocking)
        try:
            from dango import __version__ as current_version
            from dango.cli.commands.upgrade import get_latest_version_cached

            latest_version = get_latest_version_cached(project_root)
            if latest_version:
                from packaging.version import Version

                if Version(latest_version) > Version(current_version):
                    console.print()
                    console.print(
                        f"[yellow]Update available:[/yellow] "
                        f"{current_version} → [bold]{latest_version}[/bold]  "
                        f"Run [cyan]dango upgrade[/cyan] to update."
                    )
        except Exception:  # noqa: BLE001
            logger.debug("version_check_failed", exc_info=True)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
