"""dango/cli/commands/platform.py

Platform lifecycle commands (start, stop, status) and port helpers.
"""

import click

from dango.cli import console


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
                pass

            conflicts.append((port, service_name, config_key, process_info))

    if conflicts:
        console.print("[red]✗ Port conflicts detected:[/red]\n")

        for port, service_name, _config_key, process_info in conflicts:
            console.print(f"  [yellow]Port {port}[/yellow] ({service_name}) is already in use")
            if process_info:
                console.print(f"    [dim]Used by: {process_info}[/dim]")

        console.print()
        console.print("[bold]To fix, choose one option:[/bold]\n")
        console.print("[bold]Option 1:[/bold] Stop the conflicting process(es)")
        console.print(f"  [cyan]lsof -ti :{conflicts[0][0]} | xargs kill -9[/cyan]\n")

        console.print("[bold]Option 2:[/bold] Change ports in [cyan].dango/project.yml[/cyan]")
        console.print("  [dim]platform:[/dim]")
        for port, _service_name, config_key, _ in conflicts:
            console.print(f"    [cyan]{config_key}: {port + 1}[/cyan]  # Change from {port}")
        console.print()

        raise click.Abort()


@click.command()
@click.pass_context
def start(ctx: click.Context) -> None:
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
    from dango.platform import DockerManager

    from ..helpers.process_manager import start_fastapi_server
    from ..utils import require_project_context

    console.print("🍡 [bold]Starting Dango Platform...[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)

        # Load project config
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        project_name = config.project.name
        platform_config = config.platform

        # Auto-migrate databases
        try:
            from dango.migrations import apply_all_pending

            migration_results = apply_all_pending(project_root)
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

        # Ensure all dbt schemas exist (for Metabase visibility)
        from dango.utils.database import ensure_dbt_schemas

        duckdb_path = project_root / "data" / "warehouse.duckdb"
        ensure_dbt_schemas(duckdb_path)

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

        # Initialize Docker manager FIRST so we can stop services before port check
        manager = DockerManager(project_root)

        # Clean up any zombie containers from previous failed runs
        # This must happen BEFORE port checks to allow ports to be freed
        console.print("[dim]Stopping any existing Dango services...[/dim]")
        manager.stop_services()
        console.print()

        # Check Docker service ports (Metabase and dbt-docs) AFTER stopping services
        _check_docker_ports(platform_config)

        # Pre-flight check: Docker daemon must be running
        if not manager.is_docker_daemon_running():
            console.print("[red]❌ Error: Docker daemon is not running[/red]")
            console.print()
            console.print("Dango requires Docker to run Metabase and other services.")
            console.print()
            console.print("[bold]Please start Docker Desktop first:[/bold]")
            console.print("  1. Open Docker Desktop application")
            console.print("  2. Wait for it to fully start (whale icon in menu bar)")
            console.print("  3. Run '[cyan]dango start[/cyan]' again")
            console.print()
            raise click.Abort()

        # Pre-flight check: Required Docker ports must be free
        from ..helpers.port_manager import check_port_in_use

        required_docker_ports = {
            3000: "Metabase",
            8081: "dbt-docs",
        }

        ports_in_use = []
        for docker_port, service_name in required_docker_ports.items():
            if check_port_in_use(docker_port):
                ports_in_use.append((docker_port, service_name))

        if ports_in_use:
            console.print("[yellow]⚠ Required ports are occupied by existing containers[/yellow]")
            console.print()
            for docker_port, service_name in ports_in_use:
                console.print(f"  Port {docker_port} ({service_name}) is in use")
            console.print()

            # Try to automatically stop ALL Dango containers (from any project)
            console.print("[dim]Attempting to stop Dango containers from other projects...[/dim]")
            manager.stop_all_dango_containers()
            console.print()

            # Recheck ports
            ports_still_in_use = []
            for docker_port, service_name in required_docker_ports.items():
                if check_port_in_use(docker_port):
                    ports_still_in_use.append((docker_port, service_name))

            if ports_still_in_use:
                # Ports still occupied after cleanup - abort
                console.print("[red]❌ Error: Ports still in use after cleanup[/red]")
                console.print()
                for docker_port, service_name in ports_still_in_use:
                    console.print(f"  Port {docker_port} ({service_name}) is still occupied")
                console.print()
                console.print("[bold]Manual cleanup required:[/bold]")
                for docker_port, _service_name in ports_still_in_use:
                    console.print(f"  lsof -ti:{docker_port} | xargs kill -9")
                console.print()
                raise click.Abort()
            else:
                console.print("[green]✓[/green] Ports cleared, continuing with startup...")
                console.print()

        # Pre-flight check: Ensure DuckDB driver is downloaded
        driver_path = project_root / "metabase-plugins" / "duckdb.metabase-driver.jar"
        if not driver_path.exists():
            console.print("[yellow]⚠ DuckDB driver not found, downloading now...[/yellow]")
            console.print()

            # Try to download the driver
            import time
            import urllib.request

            driver_url = "https://github.com/motherduckdb/metabase_duckdb_driver/releases/download/1.4.1.0/duckdb.metabase-driver.jar"

            driver_path.parent.mkdir(exist_ok=True)
            driver_downloaded = False

            # Retry same URL 3 times (network issues are transient)
            for attempt in range(3):
                try:
                    if attempt > 0:
                        console.print(f"[dim]Retry {attempt}/2...[/dim]")
                        time.sleep(2)  # Wait before retry
                    urllib.request.urlretrieve(driver_url, driver_path)
                    console.print(
                        f"[green]✓[/green] Downloaded DuckDB driver ({driver_path.stat().st_size // 1024 // 1024}MB)"
                    )
                    console.print()
                    driver_downloaded = True
                    break
                except Exception:
                    if attempt == 2:  # Last attempt failed
                        break
                    continue

            if not driver_downloaded:
                console.print("[red]❌ Failed to download DuckDB driver[/red]")
                console.print()
                console.print("[bold]This is required for Metabase to connect to DuckDB.[/bold]")
                console.print()
                console.print("[bold]To fix:[/bold]")
                console.print("  1. Check your internet connection")
                console.print("  2. Try running '[cyan]dango start[/cyan]' again")
                console.print("  3. Or manually download from:")
                console.print(
                    "     https://github.com/motherduckdb/metabase_duckdb_driver/releases"
                )
                console.print(f"     Save as: {driver_path}")
                console.print()
                raise click.Abort()

        # Start Docker services (Metabase, dbt-docs)
        console.print("[cyan]Starting Docker services...[/cyan]")
        docker_success = manager.start_services()

        if not docker_success:
            console.print("[red]❌ Docker services failed to start[/red]")
            console.print()

            # Clean up any partially-created containers
            console.print("[yellow]Cleaning up partial containers...[/yellow]")
            manager.stop_services()
            console.print()

            console.print("Dango requires Docker services (Metabase + dbt-docs) to function.")
            console.print()
            console.print("[bold]Troubleshooting:[/bold]")
            console.print("  1. Check Docker logs: '[cyan]docker ps -a[/cyan]'")
            console.print("  2. Try again: '[cyan]dango start[/cyan]'")
            console.print()
            raise click.Abort()

        # Docker services started successfully
        # Metabase auto-setup (first-time only)
        console.print()
        console.print("[cyan]Checking Metabase setup...[/cyan]")
        from dango.visualization.metabase import setup_metabase

        metabase_configured = False
        credentials_file = project_root / ".dango" / "metabase.yml"
        if not credentials_file.exists():
            # First time - run auto-setup
            console.print("[dim]First time setup detected...[/dim]")
            organization = getattr(config.project, "organization", None)
            setup_result = setup_metabase(project_root, project_name, organization)

            if setup_result.get("success"):
                console.print("[green]✓[/green] Metabase configured automatically")
                if setup_result.get("collections_created"):
                    console.print(
                        f"[dim]  Collections: {', '.join(setup_result['collections_created'])}[/dim]"
                    )
                metabase_configured = True
            else:
                # Metabase setup failed
                console.print("[red]✗[/red] Metabase setup failed")
                if setup_result.get("errors"):
                    for error in setup_result["errors"]:
                        console.print(f"[red]  • {error}[/red]")

                # Check if DuckDB connection failed (critical)
                if not setup_result.get("duckdb_connected"):
                    # DuckDB connection is critical - abort and rollback
                    console.print()
                    console.print("[red]❌ Critical error: Cannot connect Metabase to DuckDB[/red]")
                    console.print()
                    console.print("[yellow]Rolling back: Stopping Docker services...[/yellow]")
                    manager.stop_services()
                    console.print()
                    console.print("[bold]All services have been stopped.[/bold]")
                    console.print()
                    console.print("[bold]Troubleshooting:[/bold]")
                    console.print("  1. Check if DuckDB file exists: data/warehouse.duckdb")
                    console.print(
                        "  2. Verify DuckDB driver: metabase-plugins/duckdb.metabase-driver.jar"
                    )
                    console.print(
                        "  3. Check Metabase logs: '[cyan]docker logs $(docker ps -q -f name=metabase)[/cyan]'"
                    )
                    console.print("  4. Try again: '[cyan]dango start[/cyan]'")
                    console.print()
                    raise click.Abort()
                else:
                    # DuckDB connected but other setup failed (collections, etc.)
                    # This is non-critical - can continue
                    console.print()
                    console.print(
                        "[yellow]⚠ Metabase partially configured (DuckDB connected, but setup incomplete)[/yellow]"
                    )
                    console.print(
                        "[dim]  You can manually complete setup at http://localhost:3000[/dim]"
                    )
                    metabase_configured = True  # Allow platform to start
        else:
            console.print("[green]✓[/green] Metabase already configured")
            metabase_configured = True

        # Import dashboards (if any exist)
        dashboards_dir = project_root / "dashboards"
        if dashboards_dir.exists() and list(dashboards_dir.glob("*.yml")):
            console.print()
            console.print("[cyan]Importing dashboards...[/cyan]")
            from dango.visualization.dashboard_manager import import_dashboards

            try:
                import_result = import_dashboards(project_root)
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
            console.print(f"[red]❌ Web UI failed to start:[/red] {e}")
            console.print()
            console.print("[yellow]Rolling back: Stopping Docker services...[/yellow]")
            manager.stop_services()
            console.print()
            console.print("[bold]All services have been stopped.[/bold]")
            console.print("Fix the issue above and run '[cyan]dango start[/cyan]' again.")
            console.print()
            raise click.Abort() from e

        # Start file watcher if auto-sync is enabled (non-critical - can continue without it)
        if platform_config.auto_sync:
            console.print("[cyan]Starting file watcher...[/cyan]")
            try:
                from dango.platform.watcher_lifecycle import start_file_watcher

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
        console.print("[dim]Run 'dango stop' to shut down services.[/dim]")

        # Open dashboard in browser after health check
        import time
        import webbrowser

        import requests

        console.print()
        console.print("[dim]Waiting for services to be ready...[/dim]")

        # Wait for both FastAPI and Metabase to be ready
        max_wait = 15
        fastapi_ready = False
        metabase_ready = False

        for _i in range(max_wait):
            try:
                # Check FastAPI
                if not fastapi_ready:
                    response = requests.get(f"{base_url}/api/status", timeout=1)
                    if response.status_code == 200:
                        fastapi_ready = True
                        console.print("[dim]  ✓ Dashboard ready[/dim]")

                # Check Metabase
                if not metabase_ready:
                    metabase_response = requests.get("http://localhost:3000/api/health", timeout=1)
                    if metabase_response.status_code == 200:
                        metabase_ready = True
                        console.print("[dim]  ✓ Metabase ready[/dim]")

                # Both ready? Break
                if fastapi_ready and metabase_ready:
                    break

            except Exception:
                pass

            time.sleep(1)

        try:
            webbrowser.open(base_url)
            console.print("[dim]✨ Opening dashboard in your browser...[/dim]")
        except Exception:
            pass  # Silently fail if browser can't be opened

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
            if "manager" in locals():
                manager.stop_services()
        except Exception:
            pass  # Best effort cleanup

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

        # Stop file watcher first
        console.print("[cyan]Stopping file watcher...[/cyan]")
        from dango.platform.watcher_lifecycle import get_watcher_status, stop_file_watcher

        watcher_was_running = get_watcher_status(project_root)["running"]
        watcher_stopped = stop_file_watcher(project_root)

        if watcher_stopped:
            console.print("[green]✓[/green] File watcher stopped")
        elif watcher_was_running:
            console.print("[yellow]⚠[/yellow] Failed to stop file watcher")
        else:
            console.print("[dim]File watcher was not running[/dim]")

        console.print()

        # Stop FastAPI backend
        console.print("[cyan]Stopping Web UI backend...[/cyan]")
        fastapi_stopped = stop_fastapi_server(project_root, verbose=True)

        if not fastapi_stopped:
            console.print("[dim]Web UI was not running[/dim]")

        console.print()

        # Stop Docker services
        console.print("[cyan]Stopping Docker services...[/cyan]")
        manager = DockerManager(project_root)
        docker_success = manager.stop_services()

        if not docker_success:
            console.print(
                "[yellow]Warning:[/yellow] Some Docker services may not have stopped cleanly"
            )
            console.print()

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
            console.print(f"  dbt Docs:   {base_url}/docs")
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

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
