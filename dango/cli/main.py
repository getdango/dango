"""
Dango CLI - Main entry point

Provides command-line interface for managing Dango data projects.
"""

import click
from rich.console import Console

from dango import __version__
from dango.config.loader import check_unreferenced_custom_sources, format_unreferenced_sources_warning

# Enable hyperlinks in terminal output (for clickable URLs)
console = Console(force_terminal=True, legacy_windows=False)


@click.group()
@click.version_option(version=__version__, prog_name="dango")
@click.pass_context
def cli(ctx):
    """
    🍡 Dango - Open source data platform

    Professional analytics stack built with production-grade tools.

    Common commands:
      dango init       Create a new data project
      dango start      Start data platform services
      dango sync       Load data from all sources
      dango status     Show platform status

    For help on a specific command:
      dango <command> --help
    """
    ctx.ensure_object(dict)

    # Try to find project root for commands that need it
    # (init command doesn't need it, but most others do)
    try:
        from .utils import find_project_root
        ctx.obj["project_root"] = find_project_root()
    except:
        # Not in a project - that's OK for some commands like init
        ctx.obj["project_root"] = None


@cli.command()
@click.argument("project_name", required=False, default=".")
@click.option("--skip-wizard", is_flag=True, help="Skip interactive wizard, create blank project")
@click.option("--force", is_flag=True, help="Force initialization even if project exists")
@click.pass_context
def init(ctx, project_name, skip_wizard, force):
    """
    Create a new Dango data project.

    PROJECT_NAME: Name of project directory (default: current directory '.')

    Examples:
      dango init my-analytics        Create new project in ./my-analytics/
      dango init .                   Initialize in current directory
      dango init my-project --skip-wizard  Create blank structure (no wizard)
    """
    from pathlib import Path
    from .init import init_project

    console.print(f"🍡 [bold]Initializing Dango project...[/bold]")
    console.print()

    project_dir = Path(project_name)

    try:
        init_project(project_dir, skip_wizard=skip_wizard, force=force)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


def _check_duplicate_ports(platform_config):
    """Check for duplicate port configuration"""
    ports = {
        'Web UI': platform_config.port,
        'Metabase': platform_config.metabase_port,
        'dbt docs': platform_config.dbt_docs_port
    }

    # Find duplicates
    port_to_services = {}
    for service, port in ports.items():
        if port not in port_to_services:
            port_to_services[port] = []
        port_to_services[port].append(service)

    # Check for conflicts
    duplicates = {port: services for port, services in port_to_services.items() if len(services) > 1}

    if duplicates:
        console.print("[red]✗ Duplicate port configuration detected:[/red]\n")

        for port, services in duplicates.items():
            console.print(f"  [yellow]Port {port}[/yellow] is configured for multiple services:")
            for service in services:
                console.print(f"    • {service}")

        console.print()
        console.print("[bold]To fix:[/bold] Edit [cyan].dango/project.yml[/cyan] and use different ports\n")
        console.print("  [dim]platform:[/dim]")
        console.print(f"    [cyan]port: {platform_config.port}[/cyan]           # Web UI")
        console.print(f"    [cyan]metabase_port: {platform_config.metabase_port + 1 if platform_config.metabase_port == platform_config.port else platform_config.metabase_port}[/cyan]  # Metabase")
        console.print(f"    [cyan]dbt_docs_port: {platform_config.dbt_docs_port + 2 if platform_config.dbt_docs_port in [platform_config.port, platform_config.metabase_port] else platform_config.dbt_docs_port}[/cyan]  # dbt docs")
        console.print()

        raise click.Abort()


def _check_docker_ports(platform_config):
    """Check if Docker service ports are available"""
    import socket
    import subprocess

    ports_to_check = [
        (platform_config.metabase_port, "Metabase", "metabase_port"),
        (platform_config.dbt_docs_port, "dbt docs", "dbt_docs_port")
    ]

    conflicts = []

    for port, service_name, config_key in ports_to_check:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port_available = sock.connect_ex(('127.0.0.1', port)) != 0
        sock.close()

        if not port_available:
            # Port is in use - find what's using it
            process_info = None
            try:
                result = subprocess.run(
                    ['lsof', '-ti', f':{port}'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )

                if result.returncode == 0 and result.stdout.strip():
                    pid = result.stdout.strip().split('\n')[0]

                    # Get process command
                    cmd_result = subprocess.run(
                        ['ps', '-p', pid, '-o', 'command='],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )

                    if cmd_result.returncode == 0:
                        process_info = cmd_result.stdout.strip()
                        if len(process_info) > 60:
                            process_info = process_info[:57] + "..."
            except:
                pass

            conflicts.append((port, service_name, config_key, process_info))

    if conflicts:
        console.print("[red]✗ Port conflicts detected:[/red]\n")

        for port, service_name, config_key, process_info in conflicts:
            console.print(f"  [yellow]Port {port}[/yellow] ({service_name}) is already in use")
            if process_info:
                console.print(f"    [dim]Used by: {process_info}[/dim]")

        console.print()
        console.print("[bold]To fix, choose one option:[/bold]\n")
        console.print("[bold]Option 1:[/bold] Stop the conflicting process(es)")
        console.print(f"  [cyan]lsof -ti :{conflicts[0][0]} | xargs kill -9[/cyan]\n")

        console.print("[bold]Option 2:[/bold] Change ports in [cyan].dango/project.yml[/cyan]")
        console.print("  [dim]platform:[/dim]")
        for port, service_name, config_key, _ in conflicts:
            console.print(f"    [cyan]{config_key}: {port + 1}[/cyan]  # Change from {port}")
        console.print()

        raise click.Abort()


@cli.command()
@click.pass_context
def start(ctx):
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
    from pathlib import Path
    from .utils import require_project_context, start_fastapi_server
    from dango.platform import DockerManager
    from dango.config import ConfigLoader

    console.print("🍡 [bold]Starting Dango Platform...[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)

        # Load project config
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        project_name = config.project.name
        platform_config = config.platform

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
        port_available = sock.connect_ex(('127.0.0.1', port)) != 0
        sock.close()

        if not port_available:
            # Port is in use - check if it's a zombie Dango process
            console.print(f"[yellow]⚠[/yellow]  Port {port} is already in use")
            console.print()

            # Try to find and kill zombie processes
            try:
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

                            # Get process command line to verify it's Dango
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
                        console.print(f"[dim]Found {len(dango_pids)} Dango process(es) using port {port}[/dim]")
                        console.print("[dim]Attempting to stop zombie Dango processes...[/dim]")
                        console.print()

                        from .utils import kill_process
                        killed_any = False
                        for proc_pid in dango_pids:
                            if kill_process(proc_pid, timeout=5):
                                killed_any = True
                                console.print(f"[green]✓[/green] Stopped Dango process {proc_pid}")

                        if killed_any:
                            console.print()
                            console.print(f"[green]✓[/green] Cleared port {port}, retrying start...")
                            console.print()
                            # Recheck port availability
                            import time
                            time.sleep(1)  # Give processes time to clean up
                            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            port_available = sock.connect_ex(('127.0.0.1', port)) != 0
                            sock.close()

                            if not port_available:
                                console.print(f"[red]✗[/red] Port {port} is still in use after cleanup")
                                console.print()
                                console.print("[bold]Options:[/bold]")
                                console.print(f"  1. Wait a few seconds and try again")
                                console.print(f"  2. Change Dango's port in [cyan].dango/project.yml[/cyan]")
                                console.print()
                                raise click.Abort()
                        else:
                            console.print(f"[red]✗[/red] Could not automatically stop Dango processes on port {port}")
                            console.print()
                            console.print("[bold]Options:[/bold]")
                            console.print(f"  1. Wait a few seconds and try again")
                            console.print(f"  2. Change Dango's port in [cyan].dango/project.yml[/cyan]")
                            console.print()
                            raise click.Abort()

                    # Warn about non-Dango processes and refuse to continue
                    elif other_pids:
                        console.print(f"[red]✗[/red] Port {port} is in use by non-Dango process(es):")
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
                            pids_str = ' '.join(str(pid) for pid, _ in other_pids)
                            console.print(f"  [cyan]kill {pids_str}[/cyan]")
                        console.print()
                        console.print("[bold]Option 2: Change Dango's port[/bold]")
                        console.print(f"  Edit [cyan].dango/project.yml[/cyan]:")
                        console.print("[dim]  platform:[/dim]")
                        console.print(f"[dim]    port: 9000  # Change from {port} to any free port[/dim]")
                        console.print()
                        raise click.Abort()
                    else:
                        # No processes found (might have exited between checks)
                        console.print(f"[red]✗[/red] Port {port} is in use but process not found")
                        console.print()
                        console.print("[bold]Options:[/bold]")
                        console.print(f"  1. Wait a few seconds and try again")
                        console.print(f"  2. Change Dango's port in [cyan].dango/project.yml[/cyan]")
                        console.print()
                        raise click.Abort()
            except subprocess.TimeoutExpired:
                console.print(f"[red]✗[/red] Timeout checking port {port}")
                console.print()
                raise click.Abort()

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
        from .utils import check_port_in_use
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
                for docker_port, service_name in ports_still_in_use:
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
            import urllib.request
            import time
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
                    console.print(f"[green]✓[/green] Downloaded DuckDB driver ({driver_path.stat().st_size // 1024 // 1024}MB)")
                    console.print()
                    driver_downloaded = True
                    break
                except Exception as e:
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
                console.print("     https://github.com/motherduckdb/metabase_duckdb_driver/releases")
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
        if True:
            # Metabase auto-setup (first-time only)
            console.print()
            console.print("[cyan]Checking Metabase setup...[/cyan]")
            from dango.visualization.metabase import setup_metabase

            metabase_configured = False
            credentials_file = project_root / ".dango" / "metabase.yml"
            if not credentials_file.exists():
                # First time - run auto-setup
                console.print("[dim]First time setup detected...[/dim]")
                organization = getattr(config.project, 'organization', None)
                setup_result = setup_metabase(
                    project_root,
                    project_name,
                    organization
                )

                if setup_result.get("success"):
                    console.print("[green]✓[/green] Metabase configured automatically")
                    if setup_result.get("collections_created"):
                        console.print(f"[dim]  Collections: {', '.join(setup_result['collections_created'])}[/dim]")
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
                        console.print("  2. Verify DuckDB driver: metabase-plugins/duckdb.metabase-driver.jar")
                        console.print("  3. Check Metabase logs: '[cyan]docker logs $(docker ps -q -f name=metabase)[/cyan]'")
                        console.print("  4. Try again: '[cyan]dango start[/cyan]'")
                        console.print()
                        raise click.Abort()
                    else:
                        # DuckDB connected but other setup failed (collections, etc.)
                        # This is non-critical - can continue
                        console.print()
                        console.print("[yellow]⚠ Metabase partially configured (DuckDB connected, but setup incomplete)[/yellow]")
                        console.print("[dim]  You can manually complete setup at http://localhost:3000[/dim]")
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
                        console.print(f"[green]✓[/green] Imported {len(import_result['imported'])} dashboard(s)")
                    elif import_result.get("skipped"):
                        console.print(f"[dim]✓ All dashboards already imported[/dim]")
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
            raise click.Abort()

        # Start file watcher if auto-sync is enabled (non-critical - can continue without it)
        if platform_config.auto_sync:
            console.print("[cyan]Starting file watcher...[/cyan]")
            try:
                from .utils import start_file_watcher

                watcher_pid = start_file_watcher(project_root)
                console.print(f"[green]✓[/green] File watcher started (PID {watcher_pid})")

                # Show what's being watched
                watch_dirs = ", ".join(platform_config.watch_directories)
                console.print(f"[dim]  Watching: {watch_dirs}[/dim]")
                console.print(f"[dim]  Debounce: {platform_config.debounce_seconds}s[/dim]")

                if platform_config.auto_dbt:
                    console.print(f"[dim]  Auto-cascade: sync → dbt[/dim]")

                console.print()
            except RuntimeError as e:
                # File watcher is non-critical - platform still works without it
                console.print(f"[yellow]⚠[/yellow]  File watcher failed to start: {e}")
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
            console.print(f"  Metabase:   [dim strikethrough]{base_url}/metabase[/dim strikethrough] [red](not configured)[/red]")
        console.print(f"  dbt Docs:   [link={base_url}/dbt-docs]{base_url}/dbt-docs[/link]")
        console.print(f"  API:        [link={base_url}/api]{base_url}/api[/link]")
        console.print()
        console.print(f"[dim]💡 Change port in .dango/project.yml under platform.port[/dim]")
        console.print()
        console.print("[dim]Run 'dango stop' to shut down services.[/dim]")

        # Open dashboard in browser after health check
        import webbrowser
        import time
        import requests

        console.print()
        console.print("[dim]Waiting for services to be ready...[/dim]")

        # Wait for both FastAPI and Metabase to be ready
        max_wait = 15
        fastapi_ready = False
        metabase_ready = False

        for i in range(max_wait):
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
            from .utils import stop_fastapi_server, stop_file_watcher
            stop_fastapi_server(project_root, verbose=False)
            stop_file_watcher(project_root, verbose=False)

            # Stop Docker services
            if 'manager' in locals():
                manager.stop_services()
        except Exception:
            pass  # Best effort cleanup

        console.print()
        console.print("[bold]All services have been stopped.[/bold]")
        console.print()
        import traceback
        console.print("[dim]Stack trace:[/dim]")
        traceback.print_exc()
        console.print()
        raise click.Abort()


@cli.command()
@click.option('--all', 'stop_all', is_flag=True, help='Stop ALL Dango containers from any project')
@click.pass_context
def stop(ctx, stop_all):
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
    from .utils import require_project_context, stop_fastapi_server
    from dango.platform import DockerManager
    from dango.platform.network import NetworkConfig
    from dango.config import ConfigLoader

    console.print("🍡 [bold]Stopping Dango Platform...[/bold]")
    console.print()

    # Handle --all flag (doesn't require project context)
    if stop_all:
        from dango.platform import DockerManager
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
        from .utils import stop_file_watcher
        watcher_stopped = stop_file_watcher(project_root, verbose=True)

        if not watcher_stopped:
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
            console.print("[yellow]Warning:[/yellow] Some Docker services may not have stopped cleanly")
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
        raise click.Abort()


@cli.command()
@click.pass_context
def status(ctx):
    """
    Show Dango platform status.

    Displays:
      - Project information
      - Service health (running/stopped)
      - Access URLs
      - Network routing (if using shared nginx)
    """
    from pathlib import Path
    from rich.table import Table
    from .utils import require_project_context, get_fastapi_status
    from dango.platform import DockerManager
    from dango.platform.network import NetworkConfig, NginxManager
    from dango.config import ConfigLoader

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
            if hasattr(config.project, 'organization'):
                console.print(f"[bold]Organization:[/bold] {config.project.organization}")
            console.print(f"[bold]Status:[/bold] {project_info['status'].capitalize()}")
            console.print(f"[bold]URL:[/bold] http://{project_info['domain']}")
            console.print()
        else:
            console.print(f"[bold]Project:[/bold] {project_name}")
            console.print(f"[bold]Status:[/bold] Not registered (running in localhost mode)")
            console.print()

        # Get FastAPI status
        fastapi_status = get_fastapi_status(project_root)

        # Get file watcher status
        from .utils import get_watcher_status
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
            table.add_row("File Watcher (auto-sync)", f"[green]● Running[/green] (PID {watcher_status['pid']})")
        else:
            # Check if auto-sync is enabled
            config = config_loader.load_config()
            if config.platform.auto_sync:
                table.add_row("File Watcher (auto-sync)", "[red]● Stopped[/red]")
            else:
                table.add_row("File Watcher (auto-sync)", "[dim]● Disabled[/dim]")

        # Add Metabase
        if docker_statuses and "metabase" in docker_statuses:
            status = docker_statuses["metabase"]
            if status.value == "running":
                status_text = "[green]● Running[/green]"
            else:
                status_text = f"[{status.value}]● {status.value.capitalize()}[/{status.value}]"
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
                    console.print(f"  [green]✓[/green] {info['domain']} → localhost:{info['backend_port']}{marker}")
                else:
                    console.print(f"  [dim]○ {info['domain']} → localhost:{info['backend_port']} (stopped)[/dim]")
            console.print()

        if any_running:
            console.print("[dim]Run 'dango stop' to shut down services.[/dim]")

        # Show log file location if FastAPI was running
        if fastapi_status["log_file"].exists():
            console.print()
            console.print(f"[dim]Logs: {fastapi_status['log_file']}[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.command()
@click.argument("new_name")
@click.pass_context
def rename(ctx, new_name):
    """
    Rename the project and update its domain.

    NEW_NAME: New project name (will become <new_name>.dango)

    This command:
      - Updates project name in config
      - Updates domain in routing table
      - Updates nginx configuration
      - Updates /etc/hosts entry
      - Reloads nginx

    Example:
      dango rename my-new-analytics
      → Project renamed to 'my-new-analytics'
      → New URL: http://my-new-analytics.dango
    """
    from pathlib import Path
    import re
    from .utils import require_project_context
    from dango.platform.network import NetworkConfig, NginxManager, HostsManager
    from dango.config import ConfigLoader

    console.print("🍡 [bold]Renaming Dango Project...[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)

        # Validate new name
        if not re.match(r'^[a-z0-9\-]+$', new_name):
            console.print("[red]Error:[/red] Invalid project name")
            console.print("Project names must contain only lowercase letters, numbers, and hyphens")
            console.print("Example: my-analytics, client-reports, team-metrics")
            raise click.Abort()

        # Load current config
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        old_name = config.project.name

        if old_name == new_name:
            console.print(f"[yellow]Project is already named '{new_name}'[/yellow]")
            return

        # Initialize network managers
        net_config = NetworkConfig()
        nginx_manager = NginxManager(net_config)
        hosts_manager = HostsManager(net_config)

        # Check if new name conflicts with existing project
        existing_projects = net_config.list_projects()
        if new_name in existing_projects and existing_projects[new_name]["project_path"] != str(project_root):
            console.print(f"[red]Error:[/red] Project name '{new_name}' already in use")
            console.print(f"Used by: {existing_projects[new_name]['project_path']}")
            console.print()
            console.print("Choose a different name or unregister the other project:")
            console.print(f"  cd {existing_projects[new_name]['project_path']}")
            console.print("  dango unregister  # (future command)")
            raise click.Abort()

        old_domain = f"{old_name}.dango"
        new_domain = f"{new_name}.dango"

        console.print(f"Renaming project: [cyan]{old_name}[/cyan] → [cyan]{new_name}[/cyan]")
        console.print()

        # Step 1: Update config.yml
        console.print("[cyan]1/6[/cyan] Updating project configuration...")
        config.project.name = new_name
        config_loader.save_config(config)
        console.print(f"[green]✓[/green] Updated .dango/project.yml")

        # Step 2: Update routing.json
        old_project_info = net_config.get_project_info(old_name)
        if old_project_info:
            console.print("[cyan]2/6[/cyan] Updating routing table...")

            # Remove old entry
            net_config.unregister_project(old_name)

            # Register with new name
            net_config.register_project(
                new_name,
                project_root,
                old_project_info["backend_port"],
                new_domain
            )
            net_config.update_project_status(new_name, old_project_info["status"])
            console.print(f"[green]✓[/green] Updated routing registry")
        else:
            console.print("[cyan]2/6[/cyan] [dim]No routing entry found (project not started yet)[/dim]")

        # Step 3: Update nginx site config
        if old_project_info:
            console.print("[cyan]3/6[/cyan] Updating nginx configuration...")

            # Remove old site config
            nginx_manager.remove_project_config(old_name)

            # Create new site config
            nginx_manager.write_project_config(
                new_name,
                new_domain,
                old_project_info["backend_port"]
            )
            console.print(f"[green]✓[/green] Updated nginx site config")
        else:
            console.print("[cyan]3/6[/cyan] [dim]No nginx config found[/dim]")

        # Step 4: Update /etc/hosts
        console.print("[cyan]4/6[/cyan] Updating /etc/hosts...")

        # Remove old domain
        if old_project_info:
            success, message = hosts_manager.remove_domain(old_domain)
            if success:
                console.print(f"[green]✓[/green] Removed {old_domain}")
            else:
                console.print(f"[yellow]⚠[/yellow]  {message}")

        # Add new domain
        success, message = hosts_manager.add_domain(new_domain)
        if success:
            console.print(f"[green]✓[/green] Added {new_domain}")
        else:
            console.print(f"[yellow]⚠[/yellow]  {message}")
            console.print("[dim]You may need to manually update /etc/hosts:[/dim]")
            console.print(f"[dim]  Remove: 127.0.0.1  {old_domain}[/dim]")
            console.print(f"[dim]  Add:    127.0.0.1  {new_domain}[/dim]")

        # Step 5: Reload nginx
        if nginx_manager.is_running() and old_project_info:
            console.print("[cyan]5/6[/cyan] Reloading nginx...")
            success, message = nginx_manager.reload()
            if success:
                console.print(f"[green]✓[/green] nginx reloaded with new configuration")
            else:
                console.print(f"[yellow]⚠[/yellow]  Failed to reload nginx: {message}")
                console.print("[dim]You may need to restart nginx manually[/dim]")
        else:
            console.print("[cyan]5/6[/cyan] [dim]nginx not running, skip reload[/dim]")

        # Step 6: Summary
        console.print("[cyan]6/6[/cyan] Complete!")
        console.print()
        console.print("[green]✅ Project renamed successfully![/green]")
        console.print()
        console.print(f"[bold]Old:[/bold] {old_name} → http://{old_domain}")
        console.print(f"[bold]New:[/bold] {new_name} → http://{new_domain}")
        console.print()

        if old_project_info and old_project_info["status"] == "running":
            console.print("[yellow]Note:[/yellow] Project is currently running.")
            console.print("The new URL is active immediately:")
            console.print(f"  → http://{new_domain}")
        else:
            console.print("[dim]Start the project to use the new URL:[/dim]")
            console.print("[dim]  dango start[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        import traceback
        traceback.print_exc()
        raise click.Abort()


@cli.command()
@click.option("--source", help="Sync specific source only")
@click.option("--start-date", help="Start date for incremental loading (YYYY-MM-DD)")
@click.option("--end-date", help="End date for incremental loading (YYYY-MM-DD)")
@click.option("--full-refresh", is_flag=True, help="Drop existing data and reload from scratch")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without executing")
@click.pass_context
def sync(ctx, source, start_date, end_date, full_refresh, dry_run):
    """
    Load data from all sources (or specific source).

    Examples:
      dango sync                               Sync all enabled sources
      dango sync --source orders               Sync only 'orders' source
      dango sync --start-date 2024-01-01       Override start date
      dango sync --full-refresh                Reset state and reload all data
      dango sync --dry-run                     Preview what would be synced

    This command:
      1. Runs CSV loaders (incremental)
      2. Runs dlt pipelines (API sources)
      3. Optionally runs dbt models (transformations)
    """
    from pathlib import Path
    from datetime import datetime
    from .utils import require_project_context
    from dango.config import get_config
    from dango.ingestion import run_sync
    from dango.utils import DbtLock, DbtLockError

    console.print("🍡 [bold]Syncing data...[/bold]")
    console.print()

    try:
        # Get project context
        project_root = require_project_context(ctx)

        # Try to acquire lock before running sync (which includes dbt)
        try:
            lock = DbtLock(
                project_root=project_root,
                source="cli",
                operation=f"sync {source if source else 'all sources'}"
            )
            lock.acquire()
        except DbtLockError as e:
            console.print(f"[red]Error:[/red] {str(e)}")
            raise click.Abort()

        # Check git branch (gentle reminder if on main/master)
        from .utils import check_git_branch_warning
        check_git_branch_warning(project_root)

        # Load configuration
        config = get_config(project_root)

        # Check for unreferenced custom sources
        unreferenced = check_unreferenced_custom_sources(project_root, config.sources)
        if unreferenced:
            console.print(format_unreferenced_sources_warning(unreferenced))

        # Parse dates if provided
        start_date_obj = None
        end_date_obj = None

        if start_date:
            try:
                start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                console.print(f"[red]Error:[/red] Invalid start date format. Use YYYY-MM-DD")
                raise click.Abort()

        if end_date:
            try:
                end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                console.print(f"[red]Error:[/red] Invalid end date format. Use YYYY-MM-DD")
                raise click.Abort()

        # Get sources to sync
        if source:
            # Sync specific source
            source_config = config.sources.get_source(source)
            if not source_config:
                console.print(f"[red]Error:[/red] Source '{source}' not found in sources.yml")
                console.print("\nAvailable sources:")
                for s in config.sources.sources:
                    status = "✓ enabled" if s.enabled else "✗ disabled"
                    console.print(f"  • {s.name} ({s.type.value}) - {status}")
                raise click.Abort()

            sources_to_sync = [source_config]
            console.print(f"Syncing source: [bold]{source}[/bold]")
        else:
            # Sync all enabled sources
            sources_to_sync = config.sources.get_enabled_sources()
            if not sources_to_sync:
                console.print("[yellow]No enabled sources found in sources.yml[/yellow]")
                console.print("\nRun 'dango source add' to add a source")
                return

            console.print(f"Syncing {len(sources_to_sync)} enabled source(s)")

        if full_refresh:
            console.print("[yellow]⚠️  Full refresh mode: existing data will be dropped[/yellow]")

        console.print()

        # Dry run mode - show what would be synced without executing
        if dry_run:
            console.print("[bold cyan]Dry run mode - no changes will be made[/bold cyan]\n")
            console.print("Sources that would be synced:")
            for src in sources_to_sync:
                console.print(f"  • {src.name} ({src.type.value})")
                if src.type.value == "csv":
                    console.print(f"    Path: {src.csv.file_path if src.csv else 'N/A'}")
                elif src.type.value == "dlt_native":
                    console.print(f"    Module: {src.dlt_native.source_module if src.dlt_native else 'N/A'}")
                elif src.dlt_config:
                    console.print(f"    dlt source: {src.dlt_config.source_name}")

            console.print()
            console.print("[dim]Options:[/dim]")
            console.print(f"  • Full refresh: {'Yes' if full_refresh else 'No'}")
            console.print(f"  • Start date: {start_date or 'Default'}")
            console.print(f"  • End date: {end_date or 'Default'}")
            console.print()
            console.print("[dim]Run without --dry-run to execute sync[/dim]")
            return

        # Run sync
        summary = run_sync(
            project_root=project_root,
            sources=sources_to_sync,
            start_date=start_date_obj,
            end_date=end_date_obj,
            full_refresh=full_refresh,
        )

        # Trigger Metabase schema sync (if Metabase is running)
        if summary["failed_count"] == 0:
            console.print()
            console.print("[dim]Updating Metabase schema...[/dim]")
            from dango.visualization.metabase import sync_metabase_schema

            if sync_metabase_schema(project_root):
                console.print("[green]✓[/green] Metabase schema updated")
            else:
                # Silent skip if Metabase isn't configured or running
                console.print("[dim]ℹ Metabase not running (schema will sync automatically when started)[/dim]")

        # Display OAuth warnings at the very end (so users don't miss them)
        oauth_warnings = summary.get("oauth_warnings", [])
        if oauth_warnings:
            console.print()
            console.print("[yellow]" + "="*60 + "[/yellow]")
            console.print("[yellow]⚠️  OAuth Token Warnings:[/yellow]")
            console.print("[yellow]" + "="*60 + "[/yellow]")
            for warning in oauth_warnings:
                console.print(f"  • {warning['source_name']}: expires in {warning['days_left']} day(s) ({warning['expires_at']})")
                console.print(f"    [cyan]Re-authenticate:[/cyan] dango auth {warning['source_type']}")
            console.print()

        # Exit with error code if any sources failed
        if summary["failed_count"] > 0:
            lock.release()
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        lock.release()
        raise click.Abort()
    finally:
        # Always release the lock (if it was acquired)
        try:
            lock.release()
        except:
            pass


@cli.command()
@click.pass_context
def info(ctx):
    """
    Show project information and context.

    Displays:
      - Project name and purpose
      - Stakeholders
      - Data refresh schedule
      - Last sync time
      - Getting started guide
    """
    from pathlib import Path
    from rich.panel import Panel
    from rich.table import Table
    from .utils import require_project_context
    from dango.config import get_config

    console.print("🍡 [bold]Project Info[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        # Project details
        console.print(Panel(
            f"[bold]Name:[/bold] {config.project.name}\n"
            f"[bold]Created:[/bold] {config.project.created.strftime('%Y-%m-%d')}\n"
            f"[bold]Created by:[/bold] {config.project.created_by}\n\n"
            f"[bold]Purpose:[/bold]\n{config.project.purpose}",
            title="📋 Project Details",
            border_style="cyan"
        ))
        console.print()

        # Stakeholders
        if config.project.stakeholders:
            table = Table(title="Stakeholders", show_header=True, header_style="bold cyan")
            table.add_column("Name", style="bold")
            table.add_column("Role")
            table.add_column("Contact")

            for stakeholder in config.project.stakeholders:
                table.add_row(
                    stakeholder.name,
                    stakeholder.role,
                    stakeholder.contact
                )

            console.print(table)
            console.print()

        # SLA and Limitations
        if config.project.sla or config.project.limitations:
            info_text = ""
            if config.project.sla:
                info_text += f"[bold]Data Freshness SLA:[/bold]\n{config.project.sla}\n\n"
            if config.project.limitations:
                info_text += f"[bold]Limitations:[/bold]\n{config.project.limitations}\n\n"

            console.print(Panel(
                info_text.strip(),
                title="ℹ️  Additional Info",
                border_style="yellow"
            ))
            console.print()

        # Getting Started
        if config.project.getting_started:
            console.print(Panel(
                config.project.getting_started,
                title="🚀 Getting Started",
                border_style="green"
            ))
            console.print()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.group()
def source():
    """
    Manage data sources.

    Commands:
      dango source add      Add a new data source
      dango source list     List all sources
      dango source remove   Remove a source
    """
    pass


@source.command("add")
@click.pass_context
def source_add(ctx):
    """
    Add a new data source (interactive wizard).

    Supports 27+ sources across 9 categories:
      - Marketing & Analytics (7): Facebook Ads, Google Ads, Sheets, Analytics, etc.
      - Business & CRM (7): HubSpot, Salesforce, Zendesk, Jira, etc.
      - E-commerce & Payment (1): Stripe
      - Files & Storage (2): Notion, Email Inbox
      - Databases (1): MongoDB
      - Streaming (2): Kafka, Kinesis
      - Development (1): GitHub
      - Communication (1): Slack
      - Local & Custom (2): CSV, REST API
    """
    from dango.cli.source_wizard import add_source
    from .utils import check_git_branch_warning

    project_root = ctx.obj.get("project_root")
    if not project_root:
        console.print("[red]❌ Not in a dango project directory[/red]")
        return

    # Check git branch (gentle reminder if on main/master)
    check_git_branch_warning(project_root)

    # Run wizard
    success = add_source(project_root)

    if not success:
        console.print("\n[yellow]Source not added[/yellow]")
        raise click.Abort()


@source.command("list")
@click.option("--enabled-only", is_flag=True, help="Show only enabled sources")
@click.pass_context
def source_list(ctx, enabled_only):
    """
    List all configured data sources.

    Shows source name, type, status (enabled/disabled), and last sync time.

    Examples:
      dango source list               List all sources
      dango source list --enabled-only  List only enabled sources
    """
    from pathlib import Path
    from rich.table import Table
    from .utils import require_project_context
    from dango.config import get_config
    import duckdb
    from datetime import datetime

    console.print("🍡 [bold]Data Sources[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        # Check for unreferenced custom sources
        unreferenced = check_unreferenced_custom_sources(project_root, config.sources)
        if unreferenced:
            console.print(format_unreferenced_sources_warning(unreferenced))

        # Get sources
        sources = config.sources.sources

        if not sources:
            console.print("[yellow]No sources configured yet[/yellow]")
            console.print("\nRun '[cyan]dango source add[/cyan]' to add a source")
            return

        # Filter if needed
        if enabled_only:
            sources = [s for s in sources if s.enabled]
            if not sources:
                console.print("[yellow]No enabled sources found[/yellow]")
                return

        # Get last sync times from multiple sources:
        # 1. _dlt_loads table in each raw_{source_name} schema (for dlt-based sources)
        # 2. _dango_file_metadata table in main schema (for CSV sources)
        last_sync_times = {}
        duckdb_path = project_root / "data" / "warehouse.duckdb"
        if duckdb_path.exists():
            try:
                conn = duckdb.connect(str(duckdb_path), read_only=True)

                # Method 1: Check _dlt_loads tables for dlt-based sources
                # Each source has raw_{source_name}._dlt_loads with inserted_at timestamp
                for source in sources:
                    raw_schema = f"raw_{source.name}"
                    try:
                        # Check if _dlt_loads table exists for this source
                        result = conn.execute(f"""
                            SELECT MAX(inserted_at) as last_sync
                            FROM "{raw_schema}"._dlt_loads
                            WHERE status = 0
                        """).fetchone()
                        if result and result[0]:
                            # Convert to naive datetime if timezone-aware
                            last_sync_dt = result[0]
                            if hasattr(last_sync_dt, 'replace') and last_sync_dt.tzinfo:
                                last_sync_dt = last_sync_dt.replace(tzinfo=None)
                            last_sync_times[source.name] = last_sync_dt
                    except Exception:
                        # Table doesn't exist for this source, continue
                        pass

                # Method 2: Check CSV metadata table (may override with more recent time)
                tables = conn.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'main' AND table_name = '_dango_file_metadata'
                """).fetchall()

                if tables:
                    result = conn.execute("""
                        SELECT source_name, MAX(loaded_at) as last_sync
                        FROM _dango_file_metadata
                        WHERE status = 'loaded'
                        GROUP BY source_name
                    """).fetchall()

                    for source_name, last_sync in result:
                        # Only update if more recent than dlt load time
                        if source_name not in last_sync_times or last_sync > last_sync_times[source_name]:
                            last_sync_times[source_name] = last_sync

                conn.close()
            except Exception:
                # If we can't read metadata, just skip - last_sync will show "never"
                pass

        # Create table
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Name", style="white")
        table.add_column("Type", style="dim")
        table.add_column("Status", style="white")
        table.add_column("Last Sync", style="dim")

        for source in sources:
            # Status indicator
            if source.enabled:
                status = "[green]✓ enabled[/green]"
            else:
                status = "[dim]✗ disabled[/dim]"

            # Last sync time from metadata
            if source.name in last_sync_times:
                last_sync_dt = last_sync_times[source.name]
                # Format: "2 hours ago", "3 days ago", or full date if older
                now = datetime.now()
                diff = now - last_sync_dt

                if diff.days == 0:
                    if diff.seconds < 3600:
                        minutes = diff.seconds // 60
                        last_sync = f"{minutes}m ago" if minutes > 0 else "just now"
                    else:
                        hours = diff.seconds // 3600
                        last_sync = f"{hours}h ago"
                elif diff.days == 1:
                    last_sync = "yesterday"
                elif diff.days < 7:
                    last_sync = f"{diff.days}d ago"
                else:
                    last_sync = last_sync_dt.strftime("%Y-%m-%d")
            else:
                last_sync = "[dim]never[/dim]"

            table.add_row(
                source.name,
                source.type.value,
                status,
                last_sync
            )

        console.print(table)
        console.print()

        # Summary
        enabled_count = sum(1 for s in config.sources.sources if s.enabled)
        total_count = len(config.sources.sources)
        console.print(f"[dim]Total: {total_count} sources ({enabled_count} enabled, {total_count - enabled_count} disabled)[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@source.command("remove")
@click.argument("source_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def source_remove(ctx, source_name, yes):
    """
    Remove a data source.

    SOURCE_NAME: Name of source to remove

    Examples:
      dango source remove my_csv          Remove source (with confirmation)
      dango source remove my_csv --yes    Remove without confirmation
    """
    from pathlib import Path
    from rich.prompt import Confirm
    from .utils import require_project_context, check_git_branch_warning
    from dango.config import get_config

    console.print(f"🍡 [bold]Removing source: {source_name}[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        # Check git branch (gentle reminder if on main/master)
        check_git_branch_warning(project_root)

        config = get_config(project_root)

        # Check if source exists
        source = config.sources.get_source(source_name)
        if not source:
            console.print(f"[red]Error:[/red] Source '{source_name}' not found")
            console.print("\nAvailable sources:")
            for s in config.sources.sources:
                console.print(f"  • {s.name} ({s.type.value})")
            raise click.Abort()

        # Show source info
        console.print(f"[bold]Source Details:[/bold]")
        console.print(f"  Name: {source.name}")
        console.print(f"  Type: {source.type.value}")
        console.print(f"  Status: {'enabled' if source.enabled else 'disabled'}")
        console.print()

        # Confirm deletion
        if not yes:
            console.print("[yellow]⚠️  This will remove the source configuration[/yellow]")
            console.print("[dim]Note: This does NOT delete data from DuckDB[/dim]")
            console.print(f"[dim]      Use 'dango db clean' afterwards to remove data[/dim]\n")

            if not Confirm.ask(f"Remove source '{source_name}'?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        # Remove from sources.yml
        sources_file = project_root / ".dango" / "sources.yml"
        if not sources_file.exists():
            console.print("[red]Error:[/red] sources.yml not found")
            raise click.Abort()

        # Read YAML
        import yaml
        with open(sources_file, "r") as f:
            data = yaml.safe_load(f) or {}

        # Remove source
        if "sources" in data and isinstance(data["sources"], list):
            data["sources"] = [s for s in data["sources"] if s.get("name") != source_name]

            # Write back
            with open(sources_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

            console.print(f"[green]✅ Source '{source_name}' removed successfully[/green]")
            console.print()
            console.print("[yellow]⚠️  Important:[/yellow]")
            console.print(f"  • Source configuration removed from sources.yml")
            console.print(f"  • [bold]Data still exists[/bold] in DuckDB tables:")
            console.print(f"    - raw.{source_name}")
            console.print(f"    - staging.{source_name}")
            console.print(f"  • Environment variables in .env are unchanged")
            console.print()
            console.print("[dim]To clean up orphaned tables:[/dim]")
            console.print(f"  [cyan]dango db clean[/cyan]  # Removes tables without source config (including this one)")
            console.print()
            console.print("[dim]Or to check data before cleanup:[/dim]")
            console.print(f"  [cyan]dango db query \"SELECT COUNT(*) FROM raw.{source_name}\"[/cyan]")
            console.print()

        else:
            console.print("[red]Error:[/red] Invalid sources.yml format")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.group()
def config():
    """
    Manage Dango configuration.

    Commands:
      dango config validate   Validate configuration files
      dango config show       Show current configuration
    """
    pass


@config.command("validate")
@click.pass_context
def config_validate(ctx):
    """
    Validate all configuration files:
    - .dango/sources.yml (source configuration)
    - .dango/project.yml (project settings)
    - dbt/models/staging/*/sources.yml (dbt source documentation)
    """
    from pathlib import Path
    import yaml
    from .utils import require_project_context
    from dango.config import ConfigLoader

    console.print("🍡 [bold]Validating configuration files[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)
        loader = ConfigLoader(project_root)

        all_valid = True

        # 1. Validate main config files
        console.print("[cyan]Checking .dango/sources.yml and project.yml...[/cyan]")
        is_valid, errors = loader.validate_config()

        if is_valid:
            console.print("[green]✓[/green] Main config files valid")
        else:
            all_valid = False
            console.print("[red]✗[/red] Main config has errors:")
            for error in errors:
                console.print(f"    • {error}")

        console.print()

        # 2. Validate dbt sources.yml files
        console.print("[cyan]Checking dbt sources.yml files...[/cyan]")
        staging_dir = project_root / "dbt" / "models" / "staging"

        if not staging_dir.exists():
            console.print("[dim]  No staging models yet (run sync first)[/dim]")
        else:
            sources_files = list(staging_dir.glob("*/sources.yml"))

            if not sources_files:
                console.print("[dim]  No sources.yml files yet (run sync first)[/dim]")
            else:
                dbt_errors = []
                for sources_file in sources_files:
                    try:
                        with open(sources_file, 'r') as f:
                            data = yaml.safe_load(f)

                        # Basic validation
                        if not data:
                            dbt_errors.append(f"{sources_file.relative_to(project_root)}: Empty file")
                        elif not isinstance(data, dict):
                            dbt_errors.append(f"{sources_file.relative_to(project_root)}: Invalid structure (expected dict)")
                        elif 'sources' not in data:
                            dbt_errors.append(f"{sources_file.relative_to(project_root)}: Missing 'sources' key")
                        else:
                            # File is valid
                            console.print(f"[green]✓[/green] {sources_file.relative_to(project_root)}")

                    except yaml.YAMLError as e:
                        all_valid = False
                        line_num = getattr(e, 'problem_mark', None)
                        if line_num:
                            dbt_errors.append(
                                f"{sources_file.relative_to(project_root)}: "
                                f"YAML error at line {line_num.line + 1}, column {line_num.column + 1}"
                            )
                        else:
                            dbt_errors.append(f"{sources_file.relative_to(project_root)}: Invalid YAML")
                    except Exception as e:
                        all_valid = False
                        dbt_errors.append(f"{sources_file.relative_to(project_root)}: {str(e)}")

                if dbt_errors:
                    all_valid = False
                    console.print()
                    console.print("[red]✗[/red] dbt sources.yml errors:")
                    for error in dbt_errors:
                        console.print(f"    • {error}")

        console.print()

        # Summary
        if all_valid:
            console.print("[green]✅ All configuration files are valid[/green]")
        else:
            console.print("[red]❌ Configuration has errors[/red]")
            console.print()
            console.print("[dim]Fix errors and run 'dango config validate' again[/dim]")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@config.command("show")
@click.pass_context
def config_show(ctx):
    """
    Show current configuration.
    """
    from rich.syntax import Syntax
    from .utils import require_project_context
    from dango.config import ConfigLoader

    console.print("🍡 [bold]Current Configuration[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)
        loader = ConfigLoader(project_root)

        # Show project.yml
        if loader.project_file.exists():
            with open(loader.project_file, 'r') as f:
                project_yaml = f.read()

            console.print("[bold cyan]project.yml:[/bold cyan]")
            syntax = Syntax(project_yaml, "yaml", theme="monokai", line_numbers=True)
            console.print(syntax)
            console.print()

        # Show sources.yml
        if loader.sources_file.exists():
            with open(loader.sources_file, 'r') as f:
                sources_yaml = f.read()

            console.print("[bold cyan]sources.yml:[/bold cyan]")
            syntax = Syntax(sources_yaml, "yaml", theme="monokai", line_numbers=True)
            console.print(syntax)
        else:
            console.print("[dim]sources.yml not found (no sources configured yet)[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.group()
@click.pass_context
def db(ctx):
    """
    Manage DuckDB database.

    Commands:
      dango db status     Show database status and orphaned tables
      dango db clean      Remove orphaned tables
    """
    pass


@db.command("status")
@click.pass_context
def db_status(ctx):
    """
    Show database status including orphaned tables.

    Orphaned tables are tables that exist in DuckDB but have no corresponding
    source configuration in .dango/sources.yml
    """
    from pathlib import Path
    import duckdb
    from rich.table import Table
    from .utils import require_project_context
    from dango.config import get_config

    console.print(f"🍡 [bold]Database Status[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)
        duckdb_path = project_root / "data" / "warehouse.duckdb"

        if not duckdb_path.exists():
            console.print("[yellow]⚠️  No database found[/yellow]")
            console.print("Run 'dango sync' to create the database")
            return

        # Connect to database
        conn = duckdb.connect(str(duckdb_path), read_only=True)

        # Get all tables from relevant schemas (without row counts first)
        # Include: raw, raw_*, staging, intermediate, marts
        tables = conn.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ('raw', 'staging', 'intermediate', 'marts')
               OR table_schema LIKE 'raw_%'
            ORDER BY table_schema, table_name
        """).fetchall()

        # Get row counts for each table individually
        result = []
        for schema, table in tables:
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0]
                result.append((schema, table, f"{count:,} rows"))
            except Exception as e:
                result.append((schema, table, "Error"))

        # Build schema-to-table mapping from source configurations
        from .db_helpers import build_schema_table_mapping, is_table_configured
        schema_to_tables, source_to_schema = build_schema_table_mapping(config)

        # Build actual raw tables mapping from database
        # This is used to validate that staging tables have corresponding raw tables
        actual_raw_tables = {}
        for schema, table, size in result:
            if schema.startswith('raw_') and not table.startswith('_dlt_'):
                if schema not in actual_raw_tables:
                    actual_raw_tables[schema] = set()
                actual_raw_tables[schema].add(table)

        configured_tables = []
        orphaned_tables = []

        for schema, table, size in result:
            if is_table_configured(schema, table, schema_to_tables, source_to_schema, actual_raw_tables):
                if not table.startswith('_dlt_'):
                    configured_tables.append((schema, table, size, "✅"))
            else:
                orphaned_tables.append((schema, table, size))

        conn.close()

        # Display configured tables
        if configured_tables:
            table = Table(title="Configured Tables", show_header=True, header_style="bold cyan")
            table.add_column("Schema", style="cyan")
            table.add_column("Table", style="white")
            table.add_column("Size", justify="right")
            table.add_column("Status", justify="center")

            for schema, name, size, status in configured_tables:
                table.add_row(schema, name, size, status)

            console.print(table)
            console.print()

        # Display orphaned tables
        if orphaned_tables:
            table = Table(title="Orphaned Tables", show_header=True, header_style="bold yellow")
            table.add_column("Schema", style="yellow")
            table.add_column("Table", style="white")
            table.add_column("Size", justify="right")
            table.add_column("Status", justify="center")

            for schema, name, size in orphaned_tables:
                table.add_row(schema, name, size, "⚠️")

            console.print(table)
            console.print()
            console.print("[yellow]⚠️  Found orphaned tables (no source config)[/yellow]")
            console.print("Run 'dango db clean' to remove them")
        else:
            console.print("[green]✅ No orphaned tables found[/green]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@db.command("clean")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def db_clean(ctx, yes):
    """
    Remove orphaned tables from DuckDB.

    Orphaned tables are tables that exist in DuckDB but have no corresponding
    source configuration in .dango/sources.yml

    Examples:
      dango db clean          Remove orphaned tables (with confirmation)
      dango db clean --yes    Remove without confirmation
    """
    from pathlib import Path
    import duckdb
    from rich.prompt import Confirm
    from .utils import require_project_context
    from dango.config import get_config

    console.print(f"🍡 [bold]Clean Database[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)
        duckdb_path = project_root / "data" / "warehouse.duckdb"

        if not duckdb_path.exists():
            console.print("[yellow]⚠️  No database found[/yellow]")
            return

        # Connect to database
        conn = duckdb.connect(str(duckdb_path))

        # Get all tables from relevant schemas (without row counts first)
        # Include: raw, raw_*, staging, intermediate, marts
        tables = conn.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ('raw', 'staging', 'intermediate', 'marts')
               OR table_schema LIKE 'raw_%'
            ORDER BY table_schema, table_name
        """).fetchall()

        # Get row counts for each table individually
        result = []
        for schema, table in tables:
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0]
                result.append((schema, table, f"{count:,} rows"))
            except Exception as e:
                result.append((schema, table, "Error"))

        # Build schema-to-table mapping from source configurations
        from .db_helpers import build_schema_table_mapping, is_table_configured
        schema_to_tables, source_to_schema = build_schema_table_mapping(config)

        # Build actual raw tables mapping from database
        # This is used to validate that staging tables have corresponding raw tables
        actual_raw_tables = {}
        for schema, table, size in result:
            if schema.startswith('raw_') and not table.startswith('_dlt_'):
                if schema not in actual_raw_tables:
                    actual_raw_tables[schema] = set()
                actual_raw_tables[schema].add(table)

        # Find orphaned tables
        orphaned_tables = []

        for schema, table, size in result:
            if not is_table_configured(schema, table, schema_to_tables, source_to_schema, actual_raw_tables):
                orphaned_tables.append((schema, table, size))
            # Note: We do NOT clean intermediate or marts tables
            # These are custom models created by users with dango model add
            # and should not be automatically deleted

        if not orphaned_tables:
            console.print("[green]✅ No orphaned tables found[/green]")
            conn.close()
            return

        # Show orphaned tables
        console.print(f"[yellow]Found {len(orphaned_tables)} orphaned table(s):[/yellow]\n")
        for schema, table, size in orphaned_tables:
            console.print(f"  • {schema}.{table} ({size})")

        console.print()

        # Confirm deletion
        if not yes:
            if not Confirm.ask("Remove these orphaned tables?"):
                console.print("[yellow]Cancelled[/yellow]")
                conn.close()
                return

        # Drop orphaned tables
        dropped_count = 0
        orphaned_sources = set()  # Track orphaned source names for metadata cleanup

        for schema, table, size in orphaned_tables:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
                console.print(f"[green]✓[/green] Dropped {schema}.{table}")
                dropped_count += 1

                # Track orphaned source name
                if schema == 'raw':
                    # Single-resource source: table name is source name
                    orphaned_sources.add(table)
                elif schema.startswith('raw_'):
                    # Multi-resource source: schema name contains source name (raw_sourcename)
                    source_name = schema[4:]  # Remove 'raw_' prefix
                    orphaned_sources.add(source_name)

            except Exception as e:
                console.print(f"[red]✗[/red] Failed to drop {schema}.{table}: {e}")

        # Clean up metadata for orphaned sources (only if metadata table exists)
        if orphaned_sources:
            # Check if metadata table exists (only created for CSV sources)
            metadata_table_exists = conn.execute("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name = '_dango_file_metadata'
            """).fetchone()[0] > 0

            if metadata_table_exists:
                console.print()
                console.print("[dim]Cleaning metadata...[/dim]")

                metadata_cleaned = 0
                for source_name in orphaned_sources:
                    try:
                        # Count entries first
                        count = conn.execute("""
                            SELECT COUNT(*) FROM _dango_file_metadata
                            WHERE source_name = ?
                        """, [source_name]).fetchone()[0]

                        if count > 0:
                            # Delete entries
                            conn.execute("""
                                DELETE FROM _dango_file_metadata
                                WHERE source_name = ?
                            """, [source_name])

                            console.print(f"[green]✓[/green] Cleaned metadata for '{source_name}' ({count} entries)")
                            metadata_cleaned += 1
                    except Exception as e:
                        console.print(f"[yellow]⚠[/yellow] Could not clean metadata for '{source_name}': {e}")

        conn.close()

        console.print()
        console.print(f"[green]✅ Removed {dropped_count}/{len(orphaned_tables)} orphaned table(s)[/green]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.group()
@click.pass_context
def auth(ctx):
    """
    Authenticate with OAuth providers.

    Commands:
      dango auth google_sheets      Authenticate with Google Sheets
      dango auth google_analytics   Authenticate with Google Analytics (GA4)
      dango auth google_ads         Authenticate with Google Ads
      dango auth facebook_ads       Authenticate with Facebook Ads
    """
    pass


@auth.command("list")
@click.pass_context
def auth_list(ctx):
    """
    List all OAuth credentials

    Shows all configured OAuth credentials with account info, expiry status, and usage.
    """
    from .utils import require_project_context
    from dango.oauth.storage import OAuthStorage
    from rich.table import Table

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Get all OAuth credentials
        credentials = oauth_storage.list()

        if not credentials:
            console.print("\n[yellow]No OAuth credentials configured[/yellow]")
            console.print("\n[cyan]To authenticate:[/cyan]")
            console.print("  dango auth google_sheets")
            console.print("  dango auth google_analytics")
            console.print("  dango auth google_ads")
            console.print("  dango auth facebook_ads")
            return

        # Create table
        table = Table(title=f"OAuth Credentials ({len(credentials)})", show_header=True)
        table.add_column("Source Type", style="cyan")
        table.add_column("Provider", style="blue")
        table.add_column("Account", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="dim")

        for cred in credentials:
            # Determine status
            if cred.is_expired():
                status = "[red]EXPIRED[/red]"
            elif cred.is_expiring_soon():
                days_left = cred.days_until_expiry()
                status = f"[yellow]Expires in {days_left}d[/yellow]"
            else:
                status = "[green]Active[/green]"

            # Format created date
            created = cred.created_at.strftime("%Y-%m-%d") if cred.created_at else "Unknown"

            table.add_row(
                cred.source_type,
                cred.provider,
                cred.account_info,
                status,
                created
            )

        console.print("\n")
        console.print(table)
        console.print("\n[dim]To re-authenticate: dango auth <source_type>[/dim]")
        console.print("[dim]To remove: dango auth remove <source_type>[/dim]\n")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("status")
@click.pass_context
def auth_status(ctx):
    """
    Show OAuth credential expiry status

    Displays OAuth credentials that are expired or expiring soon.
    """
    from .utils import require_project_context
    from dango.oauth.storage import OAuthStorage

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Get all OAuth credentials
        credentials = oauth_storage.list()

        if not credentials:
            console.print("\n[yellow]No OAuth credentials configured[/yellow]\n")
            return

        # Find credentials that need attention
        expired = [c for c in credentials if c.is_expired()]
        expiring_soon = [c for c in credentials if c.is_expiring_soon() and not c.is_expired()]

        if not expired and not expiring_soon:
            console.print("\n[green]✓ All OAuth credentials are active[/green]\n")
            return

        # Show expired credentials
        if expired:
            console.print("\n[red]⚠️  Expired OAuth Credentials:[/red]")
            for cred in expired:
                console.print(f"  • {cred.account_info} ({cred.source_type})")
                console.print(f"    [dim]Expired: {cred.expires_at.strftime('%Y-%m-%d')}[/dim]")
                console.print(f"    [yellow]Re-authenticate: dango auth refresh {cred.source_type}[/yellow]\n")

        # Show expiring soon
        if expiring_soon:
            console.print("\n[yellow]⚠️  OAuth Credentials Expiring Soon:[/yellow]")
            for cred in expiring_soon:
                days_left = cred.days_until_expiry()
                console.print(f"  • {cred.account_info} ({cred.source_type})")
                console.print(f"    [dim]Expires: {cred.expires_at.strftime('%Y-%m-%d')} ({days_left} days)[/dim]")
                console.print(f"    [cyan]Re-authenticate: dango auth refresh {cred.source_type}[/cyan]\n")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("remove")
@click.argument("source_type")
@click.pass_context
def auth_remove(ctx, source_type):
    """
    Remove OAuth credential

    SOURCE_TYPE: Source type to remove credentials for (e.g., google_ads, facebook_ads)

    Example:
      dango auth remove google_ads
    """
    from .utils import require_project_context
    from dango.oauth.storage import OAuthStorage
    from rich.prompt import Confirm

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Check if credential exists
        cred = oauth_storage.get(source_type)
        if not cred:
            console.print(f"\n[red]✗ OAuth credentials for '{source_type}' not found[/red]")
            console.print("\n[cyan]To see all credentials:[/cyan] dango auth list\n")
            raise click.Abort()

        # Show info and confirm
        console.print(f"\n[yellow]⚠️  About to remove OAuth credentials:[/yellow]")
        console.print(f"  Source Type: {cred.source_type}")
        console.print(f"  Provider: {cred.provider}")
        console.print(f"  Account: {cred.account_info}\n")

        if not Confirm.ask("[red]Are you sure?[/red]", default=False):
            console.print("\n[yellow]Cancelled[/yellow]\n")
            return

        # Remove credential
        if oauth_storage.delete(source_type):
            console.print(f"\n[green]✓ OAuth credential removed successfully[/green]\n")
        else:
            console.print(f"\n[red]✗ Failed to remove OAuth credential[/red]\n")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("refresh")
@click.argument("oauth_name")
@click.pass_context
def auth_refresh(ctx, oauth_name):
    """
    Re-authenticate OAuth credential

    OAUTH_NAME: Name of OAuth credential to refresh (from dango auth list)

    Example:
      dango auth refresh facebook_ads_123456789
    """
    from .utils import require_project_context
    from dango.oauth.storage import OAuthStorage
    from dango.oauth import create_oauth_manager
    from dango.oauth.providers import GoogleOAuthProvider, FacebookOAuthProvider, ShopifyOAuthProvider

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Check if credential exists
        cred = oauth_storage.get(oauth_name)
        if not cred:
            console.print(f"\n[red]✗ OAuth credential '{oauth_name}' not found[/red]")
            console.print("\n[cyan]To see all credentials:[/cyan] dango auth list\n")
            raise click.Abort()

        # Show info
        console.print(f"\n🍡 [bold]Re-authenticating OAuth credential:[/bold]")
        console.print(f"  Source Type: {cred.source_type}")
        console.print(f"  Provider: {cred.provider}")
        console.print(f"  Account: {cred.account_info}\n")

        # Dispatch to appropriate provider
        oauth_manager = create_oauth_manager(project_root)
        new_oauth_name = None

        if cred.provider == "google":
            service = cred.metadata.get("service", "google_ads") if cred.metadata else "google_ads"
            google_provider = GoogleOAuthProvider(oauth_manager)
            new_oauth_name = google_provider.authenticate(service=service)

        elif cred.provider == "facebook_ads":
            facebook_provider = FacebookOAuthProvider(oauth_manager)
            new_oauth_name = facebook_provider.authenticate()

        elif cred.provider == "shopify":
            shopify_provider = ShopifyOAuthProvider(oauth_manager)
            new_oauth_name = shopify_provider.authenticate()
        else:
            console.print(f"\n[red]✗ Unsupported provider: {cred.provider}[/red]\n")
            raise click.Abort()

        if not new_oauth_name:
            console.print("\n[red]✗ Re-authentication failed[/red]\n")
            raise click.Abort()

        console.print(f"\n[green]✓ OAuth credential refreshed successfully[/green]")
        console.print(f"[dim]  New credential: {new_oauth_name}[/dim]\n")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("facebook_ads")
@click.pass_context
def auth_facebook_ads(ctx):
    """
    Authenticate with Facebook Ads using OAuth.

    This will guide you through:
    1. Getting a short-lived access token from Facebook Graph API Explorer
    2. Exchanging it for a long-lived token (60 days)
    3. Credentials saved to .dlt/secrets.toml

    The token will need to be refreshed every 60 days.
    """
    from pathlib import Path
    from .utils import require_project_context
    from dango.oauth import OAuthManager
    from dango.oauth.providers import FacebookOAuthProvider

    try:
        project_root = require_project_context(ctx)

        # Use new OAuth implementation
        oauth_manager = OAuthManager(project_root)
        provider = FacebookOAuthProvider(oauth_manager)

        # Start OAuth flow
        oauth_name = provider.authenticate()

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("google_sheets")
@click.pass_context
def auth_google_sheets(ctx):
    """
    Authenticate with Google Sheets using OAuth.

    This will guide you through the browser-based OAuth flow:
    1. Create OAuth credentials in Google Cloud Console
    2. Authorize Dango via browser
    3. Credentials saved to .dlt/secrets.toml
    """
    from pathlib import Path
    from .utils import require_project_context
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    try:
        project_root = require_project_context(ctx)

        oauth_manager = OAuthManager(project_root)
        provider = GoogleOAuthProvider(oauth_manager)
        oauth_name = provider.authenticate(service="google_sheets")

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("google_analytics")
@click.pass_context
def auth_google_analytics(ctx):
    """
    Authenticate with Google Analytics (GA4) using OAuth.

    This will guide you through the browser-based OAuth flow:
    1. Create OAuth credentials in Google Cloud Console
    2. Authorize Dango via browser
    3. Credentials saved to .dlt/secrets.toml
    """
    from pathlib import Path
    from .utils import require_project_context
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    try:
        project_root = require_project_context(ctx)

        oauth_manager = OAuthManager(project_root)
        provider = GoogleOAuthProvider(oauth_manager)
        oauth_name = provider.authenticate(service="google_analytics")

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("google_ads")
@click.pass_context
def auth_google_ads(ctx):
    """
    Authenticate with Google Ads using OAuth.

    This will guide you through the browser-based OAuth flow:
    1. Create OAuth credentials in Google Cloud Console
    2. Authorize Dango via browser
    3. Credentials saved to .dlt/secrets.toml
    """
    from pathlib import Path
    from .utils import require_project_context
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    try:
        project_root = require_project_context(ctx)

        oauth_manager = OAuthManager(project_root)
        provider = GoogleOAuthProvider(oauth_manager)
        oauth_name = provider.authenticate(service="google_ads")

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("check")
@click.pass_context
def auth_check(ctx):
    """
    Check OAuth configuration and credential status.

    Validates:
    - OAuth client credentials in .env
    - Saved OAuth tokens in .dlt/secrets.toml
    - Token expiry status

    Example:
      dango auth check
    """
    from pathlib import Path
    import os
    from .utils import require_project_context
    from dango.oauth.storage import OAuthStorage
    from dotenv import load_dotenv

    try:
        project_root = require_project_context(ctx)

        # Load .env
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        console.print("\n[bold cyan]OAuth Configuration Check[/bold cyan]\n")

        # Define providers and their required env vars
        providers = {
            "Google": {
                "env_vars": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
                "services": ["Google Ads", "Google Analytics", "Google Sheets"],
                "auth_cmd": "dango auth google_<sheets|analytics|ads>",
            },
            "Facebook": {
                "env_vars": ["FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"],
                "services": ["Facebook Ads"],
                "auth_cmd": "dango auth facebook_ads",
            },
        }

        # Check each provider's env vars
        console.print("[bold]1. OAuth Client Credentials (.env)[/bold]\n")

        all_configured = True
        for provider_name, config in providers.items():
            env_vars = config["env_vars"]
            configured = all(os.getenv(var) for var in env_vars)

            if configured:
                console.print(f"  [green]✓[/green] {provider_name}")
                for var in env_vars:
                    value = os.getenv(var, "")
                    masked = value[:8] + "..." if len(value) > 8 else "***"
                    console.print(f"    [dim]{var}: {masked}[/dim]")
            else:
                console.print(f"  [red]✗[/red] {provider_name}")
                for var in env_vars:
                    if os.getenv(var):
                        console.print(f"    [green]✓[/green] {var}: configured")
                    else:
                        console.print(f"    [red]✗[/red] {var}: [dim]missing[/dim]")
                console.print(f"    [dim]→ Add credentials to .env file[/dim]")
                all_configured = False

        # Check saved OAuth tokens
        console.print("\n[bold]2. Saved OAuth Tokens (.dlt/secrets.toml)[/bold]\n")

        oauth_storage = OAuthStorage(project_root)
        credentials = oauth_storage.list()

        if not credentials:
            console.print("  [yellow]No OAuth tokens saved yet[/yellow]")
            console.print("  [dim]→ Run: dango auth <provider> to authenticate[/dim]")
        else:
            for cred in credentials:
                if cred.is_expired():
                    status = "[red]EXPIRED[/red]"
                    action = f"[dim]→ Run: dango auth refresh {cred.source_type}[/dim]"
                elif cred.is_expiring_soon():
                    days_left = cred.days_until_expiry()
                    status = f"[yellow]Expires in {days_left}d[/yellow]"
                    action = f"[dim]→ Consider refreshing: dango auth refresh {cred.source_type}[/dim]"
                else:
                    status = "[green]Active[/green]"
                    action = ""

                console.print(f"  {status} {cred.account_info}")
                console.print(f"    [dim]Provider: {cred.provider} | Source: {cred.source_type}[/dim]")
                if action:
                    console.print(f"    {action}")

        # Summary and next steps
        console.print("\n[bold]3. Summary[/bold]\n")

        if all_configured and credentials:
            active_creds = [c for c in credentials if not c.is_expired()]
            if active_creds:
                console.print("  [green]✓ OAuth is fully configured[/green]")
                console.print("  [dim]You can add OAuth sources with: dango source add[/dim]")
            else:
                console.print("  [yellow]⚠️  OAuth credentials configured but tokens expired[/yellow]")
                console.print("  [dim]Re-authenticate with: dango auth refresh <name>[/dim]")
        elif all_configured:
            console.print("  [yellow]⚠️  OAuth credentials configured but not yet authenticated[/yellow]")
            console.print("  [dim]Authenticate with: dango auth <provider>[/dim]")
        else:
            console.print("  [yellow]⚠️  Some OAuth credentials missing[/yellow]")
            console.print("  [dim]Add missing credentials to .env file[/dim]")

        console.print("")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@auth.command("setup")
@click.argument("provider", type=click.Choice(["google", "facebook"], case_sensitive=False))
@click.pass_context
def auth_setup(ctx, provider):
    """
    Interactive OAuth setup wizard.

    Guides you through creating OAuth credentials for a provider.

    PROVIDER: The OAuth provider to set up (google, facebook)

    Examples:
      dango auth setup google
      dango auth setup facebook
    """
    from pathlib import Path
    import os
    from .utils import require_project_context
    from rich.panel import Panel
    from rich.prompt import Confirm
    from dotenv import load_dotenv, set_key
    import inquirer
    from inquirer import themes

    try:
        project_root = require_project_context(ctx)

        # Load existing .env
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        console.print(f"\n[bold cyan]OAuth Setup Wizard: {provider.title()}[/bold cyan]\n")

        # Provider-specific configuration
        provider_config = {
            "google": {
                "display_name": "Google",
                "env_vars": [
                    ("GOOGLE_CLIENT_ID", "OAuth Client ID"),
                    ("GOOGLE_CLIENT_SECRET", "OAuth Client Secret"),
                ],
                "setup_url": "https://console.cloud.google.com/apis/credentials",
                "setup_steps": [
                    "1. Go to Google Cloud Console → APIs & Services → Credentials",
                    "2. Click '+ CREATE CREDENTIALS' → 'OAuth client ID'",
                    "3. Application type: 'Web application'",
                    "4. Name: 'Dango Local' (or any name)",
                    "5. Authorized redirect URIs: Add 'http://localhost:8080/callback'",
                    "6. Click 'Create' and copy the Client ID and Client Secret",
                ],
                "services": ["Google Ads", "Google Analytics", "Google Sheets"],
            },
            "facebook": {
                "display_name": "Facebook",
                "env_vars": [
                    ("FACEBOOK_APP_ID", "App ID"),
                    ("FACEBOOK_APP_SECRET", "App Secret"),
                ],
                "setup_url": "https://developers.facebook.com/apps/",
                "setup_steps": [
                    "1. Go to Facebook Developers → My Apps → Create App",
                    "2. Select 'Business' app type",
                    "3. Add 'Marketing API' product",
                    "4. Go to Settings → Basic to get App ID and App Secret",
                    "5. Add 'http://localhost:8080/callback' to Valid OAuth Redirect URIs",
                ],
                "services": ["Facebook Ads"],
            },
        }

        config = provider_config[provider.lower()]

        # Show privacy message
        console.print(Panel(
            "[bold]Why create your own OAuth app?[/bold]\n\n"
            "• Your data flows directly: Provider → Your Machine → Local Database\n"
            "• Dango never touches your data (no intermediary servers)\n"
            "• You control the OAuth app and can revoke access anytime\n"
            "• No shared rate limits or quotas",
            title="Privacy First",
            border_style="green"
        ))

        # Check if already configured
        all_configured = all(os.getenv(var) for var, _ in config["env_vars"])
        if all_configured:
            console.print(f"\n[green]✓ {config['display_name']} OAuth credentials already configured[/green]")

            if not Confirm.ask("Update credentials anyway?", default=False):
                console.print("\n[dim]To authenticate, run:[/dim]")
                if provider.lower() == "google":
                    console.print("  dango auth google_sheets")
                    console.print("  dango auth google_analytics")
                    console.print("  dango auth google_ads")
                else:
                    console.print(f"  dango auth {provider.lower()}")
                return

        # Show setup steps
        console.print(f"\n[bold]Setup Steps for {config['display_name']}:[/bold]\n")
        for step in config["setup_steps"]:
            console.print(f"  {step}")

        console.print(f"\n[cyan]Setup URL:[/cyan] {config['setup_url']}\n")

        # Ask if ready to continue
        if not Confirm.ask("Ready to enter credentials?", default=True):
            console.print("\n[yellow]Setup cancelled[/yellow]")
            console.print(f"[dim]Run again when ready: dango auth setup {provider.lower()}[/dim]")
            return

        # Collect credentials
        console.print("\n[bold]Enter your OAuth credentials:[/bold]\n")

        credentials = {}
        for env_var, display_name in config["env_vars"]:
            current_value = os.getenv(env_var, "")
            if current_value:
                masked = current_value[:8] + "..." if len(current_value) > 8 else "***"
                console.print(f"  [dim]Current {display_name}: {masked}[/dim]")

            questions = [
                inquirer.Text(
                    env_var,
                    message=display_name,
                    default="" if not current_value else None,
                )
            ]
            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers or not answers[env_var]:
                if current_value:
                    console.print(f"  [dim]Keeping existing value[/dim]")
                    credentials[env_var] = current_value
                else:
                    console.print(f"\n[red]✗ {display_name} is required[/red]")
                    raise click.Abort()
            else:
                credentials[env_var] = answers[env_var]

        # Save to .env
        console.print("\n[dim]Saving credentials to .env...[/dim]")

        # Create .env if doesn't exist
        if not env_file.exists():
            env_file.touch()

        for env_var, value in credentials.items():
            set_key(str(env_file), env_var, value)

        console.print(f"\n[green]✓ {config['display_name']} OAuth credentials saved to .env[/green]")

        # Next steps
        console.print(f"\n[bold]Next Steps:[/bold]")
        console.print(f"  1. Authenticate: ", end="")
        if provider.lower() == "google":
            console.print("[cyan]dango auth <google_ads|google_analytics|google_sheets>[/cyan]")
        else:
            console.print(f"[cyan]dango auth {provider.lower()}[/cyan]")
        console.print(f"  2. Add a source: [cyan]dango source add[/cyan]")
        console.print("")

    except KeyboardInterrupt:
        console.print("\n[yellow]Setup cancelled[/yellow]")
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.group()
@click.pass_context
def model(ctx):
    """
    Manage dbt models.

    Commands:
      dango model add    Create a new intermediate or marts model
    """
    pass


@model.command("add")
@click.pass_context
def model_add(ctx):
    """
    Create a new dbt model (intermediate or marts layer).

    This interactive wizard helps you create:
    - Intermediate models: Reusable business logic
    - Marts models: Final business metrics

    Staging models are auto-generated during 'dango sync',
    so this wizard only handles intermediate and marts layers.

    Examples:
      dango model add    Run interactive wizard
    """
    from pathlib import Path
    from .utils import require_project_context, check_git_branch_warning
    from .model_wizard import add_model

    try:
        project_root = require_project_context(ctx)

        # Check git branch (gentle reminder if on main/master)
        check_git_branch_warning(project_root)

        model_path = add_model(project_root)

        if not model_path:
            raise click.Abort()

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        import traceback
        console.print(traceback.format_exc())
        raise click.Abort()


@model.command("remove")
@click.argument("model_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def model_remove(ctx, model_name, yes):
    """
    Remove a custom dbt model.

    Deletes the model SQL file. Does NOT drop the table from DuckDB -
    run 'dbt run' to rebuild without the removed model, or manually
    drop the table.

    Examples:
      dango model remove fct_daily_sales
      dango model remove int_orders --yes
    """
    from pathlib import Path
    from rich.prompt import Confirm
    from .utils import require_project_context

    console.print(f"🍡 [bold]Removing Model: {model_name}[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        dbt_dir = project_root / "dbt" / "models"

        # Find model file (check intermediate and marts)
        model_file = None
        layer = None

        for layer_name in ['intermediate', 'marts']:
            potential_path = dbt_dir / layer_name / f"{model_name}.sql"
            if potential_path.exists():
                model_file = potential_path
                layer = layer_name
                break

        if not model_file:
            console.print(f"[red]Error:[/red] Model '{model_name}' not found")
            console.print(f"[dim]Searched in dbt/models/intermediate/ and dbt/models/marts/[/dim]")
            raise click.Abort()

        # Show model details
        console.print(f"[bold]Model Details:[/bold]")
        console.print(f"  Name: {model_name}")
        console.print(f"  Layer: {layer}")
        console.print(f"  File: {model_file.relative_to(project_root)}")
        console.print()

        # Check if table exists in DuckDB
        import duckdb
        duckdb_path = project_root / "data" / "warehouse.duckdb"
        table_exists = False

        if duckdb_path.exists():
            try:
                conn = duckdb.connect(str(duckdb_path))
                result = conn.execute(f"""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = '{layer}' AND table_name = '{model_name}'
                """).fetchone()
                table_exists = result[0] > 0
                conn.close()
            except Exception:
                pass

        # Check for downstream dependencies
        downstream_models = []
        for layer_to_check in ['intermediate', 'marts']:
            layer_dir = dbt_dir / layer_to_check
            if layer_dir.exists():
                for sql_file in layer_dir.glob("*.sql"):
                    if sql_file.stem != model_name:  # Don't check self
                        try:
                            content = sql_file.read_text()
                            # Check if this file references the model being removed
                            if f"ref('{model_name}')" in content or f'ref("{model_name}")' in content:
                                downstream_models.append(f"{layer_to_check}.{sql_file.stem}")
                        except Exception:
                            pass

        # Warn about dependencies
        if downstream_models:
            console.print("[red]⚠️  WARNING: Other models depend on this model![/red]")
            console.print(f"\n[bold]Downstream dependencies:[/bold]")
            for dep in downstream_models:
                console.print(f"  • {dep}")
            console.print()
            console.print("[yellow]Removing this model will break downstream models.[/yellow]")
            console.print("[dim]Consider removing downstream models first, or updating them.[/dim]\n")

            if not yes:
                if not Confirm.ask(f"Continue removing '{model_name}' anyway?"):
                    console.print("[yellow]Cancelled[/yellow]")
                    return

        # Confirm deletion
        if not yes and not downstream_models:  # Skip if already confirmed above
            console.print("[yellow]⚠️  This will delete the model file[/yellow]")
            if table_exists:
                console.print(f"[dim]The table {layer}.{model_name} exists and will be offered for removal[/dim]\n")
            else:
                console.print(f"[dim]No table found in DuckDB (model may not have been run yet)[/dim]\n")

            if not Confirm.ask(f"Remove model '{model_name}'?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        # Delete model file
        model_file.unlink()
        console.print(f"[green]✓[/green] Deleted model file: {model_file.relative_to(project_root)}")

        # Handle table deletion if it exists
        if table_exists:
            console.print()
            if Confirm.ask(f"Also drop the table from DuckDB ({layer}.{model_name})?"):
                try:
                    conn = duckdb.connect(str(duckdb_path))
                    conn.execute(f"DROP TABLE IF EXISTS {layer}.{model_name}")
                    conn.close()
                    console.print(f"[green]✓[/green] Dropped table: {layer}.{model_name}")
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to drop table: {e}")
            else:
                console.print(f"[yellow]⚠[/yellow]  Table {layer}.{model_name} still exists in DuckDB")
                console.print(f"[dim]    Run 'cd dbt && dbt run' to rebuild project without this model[/dim]")

        console.print()
        console.print(f"[green]✅ Model '{model_name}' removed successfully[/green]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.command("run", context_settings=dict(ignore_unknown_options=True, allow_interspersed_args=False))
@click.argument('dbt_args', nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run(ctx, dbt_args):
    """
    Run dbt models (wrapper for dbt run).

    This command works from anywhere within your project directory
    and automatically finds the dbt project.

    Examples:
      dango run                           Run all models
      dango run --select my_model         Run specific model
      dango run --select my_model+        Run model and downstream
      dango run --select tag:marts        Run models with tag
      dango run --full-refresh            Full refresh of incremental models

    Any dbt run arguments are passed through to dbt.
    See 'dbt run --help' for all available options.
    """
    import subprocess
    from pathlib import Path
    from .utils import require_project_context
    from dango.utils import DbtLock, DbtLockError
    from dango.utils.dbt_status import update_model_status

    try:
        project_root = require_project_context(ctx)
        dbt_dir = project_root / "dbt"

        if not dbt_dir.exists():
            console.print("[red]Error:[/red] dbt directory not found")
            console.print(f"[dim]Expected: {dbt_dir}[/dim]")
            raise click.Abort()

        # Build dbt command
        cmd = ["dbt", "run", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]
        if dbt_args:
            cmd.extend(dbt_args)

        # Try to acquire lock before running dbt
        try:
            lock = DbtLock(
                project_root=project_root,
                source="cli",
                operation=f"dbt run {' '.join(dbt_args) if dbt_args else ''}"
            )
            lock.acquire()
        except DbtLockError as e:
            console.print(f"[red]Error:[/red] {str(e)}")
            raise click.Abort()

        console.print(f"[dim]Running: {' '.join(cmd)}[/dim]\n")

        # Run dbt command from dbt directory for correct path resolution
        result = subprocess.run(cmd, cwd=str(dbt_dir))

        if result.returncode != 0:
            console.print(f"\n[red]dbt run failed with exit code {result.returncode}[/red]")
            raise click.Abort()

        # Update persistent model status
        update_model_status(project_root)

        # Update schema.yml files for intermediate/marts models
        console.print("\n[dim]Updating schema.yml files...[/dim]")
        from dango.cli.schema_manager import update_model_schemas

        # Get list of all intermediate/marts models
        models_to_update = []
        for layer in ["intermediate", "marts"]:
            layer_dir = dbt_dir / "models" / layer
            if layer_dir.exists():
                for sql_file in layer_dir.glob("*.sql"):
                    if not sql_file.name.startswith("_"):
                        models_to_update.append(sql_file.stem)

        if models_to_update:
            update_model_schemas(project_root, models_to_update)

        # Refresh Metabase connection to see new/updated tables
        console.print("\n[dim]Refreshing Metabase connection...[/dim]")
        from dango.visualization.metabase import refresh_metabase_connection, sync_metabase_schema

        if refresh_metabase_connection(project_root):
            console.print("[green]✓ Metabase connection refreshed[/green]")
            # Also sync schema to discover new tables/schemas from dbt run
            if sync_metabase_schema(project_root):
                console.print("[green]✓ Metabase schema synced[/green]")
        else:
            console.print("[dim]ℹ Metabase not running (will sync when started)[/dim]")

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        lock.release()
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        lock.release()
        raise click.Abort()
    finally:
        # Always release the lock (if it was acquired)
        try:
            lock.release()
        except:
            pass


@cli.command("docs")
@click.pass_context
def docs(ctx):
    """
    Generate dbt documentation (wrapper for dbt docs generate).

    This command generates documentation for your dbt models, sources, and tests.
    After generation, view docs at http://localhost:{port}/dbt-docs (if platform is running).

    Examples:
      dango docs          Generate documentation
      dango start         Then view at http://localhost:{port}/dbt-docs
    """
    import subprocess
    from pathlib import Path
    from .utils import require_project_context
    from dango.config import ConfigLoader

    try:
        project_root = require_project_context(ctx)
        dbt_dir = project_root / "dbt"

        if not dbt_dir.exists():
            console.print("[red]Error:[/red] dbt directory not found")
            console.print(f"[dim]Expected: {dbt_dir}[/dim]")
            raise click.Abort()

        console.print("[dim]Generating dbt documentation...[/dim]\n")

        # Build dbt command
        cmd = ["dbt", "docs", "generate", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)]

        # Run dbt command from dbt directory for correct path resolution
        result = subprocess.run(cmd, cwd=str(dbt_dir))

        if result.returncode != 0:
            console.print(f"\n[red]dbt docs generate failed with exit code {result.returncode}[/red]")
            raise click.Abort()

        console.print()
        console.print("[green]✓ Documentation generated successfully[/green]")
        console.print()

        # Get platform port from config for proxy URL
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
        platform_port = config.platform.port
        dbt_docs_url = f"http://localhost:{platform_port}/dbt-docs"

        # Check if platform is running
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        platform_running = sock.connect_ex(('127.0.0.1', platform_port)) == 0
        sock.close()

        if platform_running:
            console.print(f"[bold]View documentation:[/bold] [cyan]{dbt_docs_url}[/cyan]")
        else:
            console.print("[dim]To view documentation:[/dim]")
            console.print(f"  1. Start platform: [cyan]dango start[/cyan]")
            console.print(f"  2. Open browser: [cyan]{dbt_docs_url}[/cyan]")

        console.print()

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.command("validate")
@click.pass_context
def validate(ctx):
    """
    Validate project configuration and setup.

    This command checks:
    - Project directory structure
    - Configuration files (project.yml, sources.yml)
    - Data source configurations
    - dbt setup (dbt_project.yml, profiles.yml, models)
    - Database connectivity (DuckDB)
    - Required dependencies (dlt, dbt, duckdb, etc.)
    - File permissions

    Run this command to ensure your project is properly configured
    before syncing data or running transformations.

    Examples:
      dango validate    Run all validation checks
    """
    from pathlib import Path
    from .utils import require_project_context
    from .validate import validate_project

    try:
        project_root = require_project_context(ctx)
        summary = validate_project(project_root)

        # Exit with error code if validation failed
        if not summary["is_valid"]:
            raise SystemExit(1)

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        raise click.Abort()
    except SystemExit:
        raise  # Re-raise SystemExit
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        import traceback
        console.print(traceback.format_exc())
        raise click.Abort()


@cli.command("generate")
@click.option("--models", is_flag=True, help="Generate dbt staging models")
@click.option("--all", "generate_all", is_flag=True, help="Generate all dbt artifacts (models + schema)")
@click.pass_context
def generate(ctx, models, generate_all):
    """
    Generate dbt models and artifacts from data sources.

    This command introspects your DuckDB warehouse and automatically generates:
    - Staging models (stg_*.sql) with deduplication logic
    - Schema definitions (schema.yml) with tests and documentation

    Examples:
      dango generate --models      Generate staging models only
      dango generate --all         Generate models + schema.yml

    Note: Run 'dango sync' first to load data into DuckDB
    """
    from pathlib import Path
    from rich.table import Table
    from .utils import require_project_context
    from dango.config import get_config
    from dango.transformation.generator import DbtModelGenerator

    console.print("🍡 [bold]Generating dbt Models[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        # Get enabled sources
        sources = config.sources.get_enabled_sources()

        if not sources:
            console.print("[yellow]No enabled sources found[/yellow]")
            console.print("\nRun 'dango source add' to add sources")
            return

        # Default to --all if no flag specified
        if not models and not generate_all:
            generate_all = True

        console.print(f"Generating models for {len(sources)} source(s)...")
        console.print()

        # Initialize generator
        generator = DbtModelGenerator(project_root)

        # Generate models (force regenerate all in manual generate command)
        summary = generator.generate_all_models(
            sources=sources,
            generate_schema_yml=generate_all,
            skip_customized=False  # Manual command always regenerates
        )

        # Display results
        if summary["generated"]:
            console.print("[green]✅ Generated Models:[/green]\n")

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Source", style="white")
            table.add_column("Columns", style="dim")
            table.add_column("Dedup Strategy", style="cyan")
            table.add_column("Files", style="dim")

            for item in summary["generated"]:
                source_name = item["source"]
                models = item.get("models", [])

                # For each model generated for this source
                for model in models:
                    files = "model"
                    if item.get("schema"):
                        files += " + schema"

                    table.add_row(
                        f"{source_name} ({model['endpoint']})",
                        str(model.get("columns", "N/A")),
                        model.get("dedup_strategy", "N/A"),
                        files
                    )

            console.print(table)
            console.print()

        if summary["skipped"]:
            console.print("[yellow]⚠️  Skipped:[/yellow]\n")
            for item in summary["skipped"]:
                console.print(f"  • {item['source']}: {item['reason']}")
            console.print()

        if summary["errors"]:
            console.print("[red]❌ Errors:[/red]\n")
            for item in summary["errors"]:
                console.print(f"  • {item['source']}: {item['error']}")
            console.print()

        # Summary
        console.print(f"[bold]Summary:[/bold]")
        console.print(f"  Generated: {len(summary['generated'])}")
        console.print(f"  Skipped: {len(summary['skipped'])}")
        console.print(f"  Errors: {len(summary['errors'])}")

        # Next steps
        if summary["generated"]:
            console.print()
            console.print("[cyan]Next steps:[/cyan]")
            console.print("  1. Review generated models in dbt/models/staging/")
            console.print("  2. Run: [bold]cd dbt && dbt run[/bold]")
            console.print("  3. Run: [bold]dbt test[/bold]")
            console.print("  4. View docs: [bold]dbt docs generate && dbt docs serve[/bold]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        import traceback
        console.print(traceback.format_exc())
        raise click.Abort()


@cli.group()
@click.pass_context
def dashboard(ctx):
    """
    Provision pre-built Metabase dashboards.

    Commands:
      dango dashboard provision    Create Data Pipeline Health dashboard
    """
    pass


@dashboard.command("provision")
@click.option("--url", default="http://localhost:3001", help="Metabase URL")
@click.option("--username", default="admin@example.com", help="Metabase admin username")
@click.option("--password", prompt=True, hide_input=True, help="Metabase admin password")
@click.pass_context
def dashboard_provision(ctx, url, username, password):
    """
    Provision Data Pipeline Health dashboard in Metabase.

    This creates a pre-built dashboard with:
    - Pipeline health score
    - Source sync status
    - Data freshness indicators
    - Row count trends
    - dbt test results

    The dashboard provides instant visibility into your data pipeline.

    Examples:
      dango dashboard provision                  # Use defaults (localhost:3001)
      dango dashboard provision --url http://metabase.local
    """
    from rich.panel import Panel
    from rich.table import Table
    from dango.visualization import provision_dashboard

    console.print("\n🍡 [bold]Provisioning Metabase Dashboard[/bold]\n")

    try:
        console.print(f"Connecting to Metabase at {url}...")
        console.print()

        # Provision dashboard
        with console.status("[cyan]Creating dashboard...[/cyan]", spinner="dots"):
            result = provision_dashboard(
                metabase_url=url,
                username=username,
                password=password
            )

        if result["success"]:
            console.print("[green]✅ Dashboard provisioned successfully![/green]\n")

            # Show dashboard info
            info_panel = Panel(
                f"[bold]Dashboard ID:[/bold] {result['dashboard_id']}\n"
                f"[bold]URL:[/bold] {result['dashboard_url']}\n"
                f"[bold]Cards Created:[/bold] {len(result['cards_created'])}",
                title="📊 Data Pipeline Health Dashboard",
                border_style="green"
            )
            console.print(info_panel)
            console.print()

            # Show created cards
            if result["cards_created"]:
                console.print("[bold]Created Visualizations:[/bold]\n")
                table = Table(show_header=False)
                table.add_column("Card", style="cyan")

                for card in result["cards_created"]:
                    table.add_row(f"✓ {card['name']}")

                console.print(table)
                console.print()

            # Show errors if any
            if result["errors"]:
                console.print("[yellow]⚠️  Warnings:[/yellow]")
                for error in result["errors"]:
                    console.print(f"  • {error}")
                console.print()

            # Next steps
            console.print("[cyan]Next steps:[/cyan]")
            console.print(f"  1. Open dashboard: {result['dashboard_url']}")
            console.print("  2. Customize visualizations as needed")
            console.print("  3. Share with your team")
            console.print()

        else:
            console.print("[red]❌ Dashboard provisioning failed[/red]\n")

            if result["errors"]:
                console.print("[red]Errors:[/red]")
                for error in result["errors"]:
                    console.print(f"  • {error}")
                console.print()

            console.print("[yellow]Troubleshooting:[/yellow]")
            console.print("  • Ensure Metabase is running: dango start")
            console.print(f"  • Check Metabase is accessible: {url}")
            console.print("  • Verify admin credentials are correct")
            console.print("  • Check DuckDB database is connected in Metabase")

            raise click.Abort()

    except KeyboardInterrupt:
        console.print("\n[yellow]Provisioning cancelled[/yellow]")
        raise click.Abort()
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        import traceback
        console.print(traceback.format_exc())
        raise click.Abort()


@cli.group()
@click.pass_context
def metabase(ctx):
    """
    Manage Metabase assets (dashboards, questions).

    Commands:
      dango metabase save     Export dashboards/questions to files
      dango metabase load     Import dashboards/questions from files
    """
    pass


@metabase.command("save")
@click.option("--all", "include_personal", is_flag=True, help="Include personal collections (default: only shared/team)")
@click.option("--collections", help="Specific collections to export (comma-separated)")
@click.pass_context
def metabase_save(ctx, include_personal, collections):
    """
    Save Metabase dashboards and questions to files.

    Exports to metabase/ directory in YAML format.
    By default, excludes personal collections (only exports shared/team assets).

    What this does:
    - Exports dashboards from Metabase to metabase/dashboards/ (YAML files)
    - Exports questions from Metabase to metabase/questions/ (YAML files)
    - Excludes personal collections by default
    - Files can optionally be committed to git for version control

    Workflow:
      1. Make changes in Metabase UI
      2. Run 'dango metabase save'
      3. (Optional) Commit to git: git add metabase/ && git commit -m "Update dashboards"

    Examples:
      dango metabase save                          # Export shared/team collections
      dango metabase save --all                    # Include personal collections
      dango metabase save --collections "Shared,Marketing"  # Specific collections
    """
    from pathlib import Path
    from .utils import require_project_context
    from dango.visualization.dashboard_manager import DashboardManager

    console.print("\n🍡 [bold]Saving Metabase Assets[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        # Parse collections if provided
        collection_list = None
        if collections:
            collection_list = [c.strip() for c in collections.split(",")]

        # Create dashboard manager
        manager = DashboardManager(project_root)

        with console.status("[cyan]Exporting dashboards and questions...[/cyan]", spinner="dots"):
            result = manager.save_to_files(
                include_personal=include_personal,
                collections=collection_list
            )

        if result["success"]:
            total = (
                len(result["exported_dashboards"]) +
                len(result["exported_questions"]) +
                len(result.get("exported_models", [])) +
                len(result.get("exported_metrics", [])) +
                len(result.get("exported_timelines", []))
            )
            console.print(f"[green]✅ Exported {total} item(s) to metabase/[/green]\n")

            if result["exported_dashboards"]:
                console.print("[bold]Dashboards:[/bold]")
                for item in result["exported_dashboards"]:
                    console.print(f"  ✓ {item['name']} ({item['cards']} cards) - {item['collection']}")
                console.print()

            if result["exported_questions"]:
                console.print("[bold]Questions:[/bold]")
                for item in result["exported_questions"]:
                    console.print(f"  ✓ {item['name']} ({item['type']}) - {item['collection']}")
                console.print()

            if result.get("exported_models"):
                console.print("[bold]Models:[/bold]")
                for item in result["exported_models"]:
                    console.print(f"  ✓ {item['name']} ({item['type']}) - {item['collection']}")
                console.print()

            if result.get("exported_metrics"):
                console.print("[bold]Metrics:[/bold]")
                for item in result["exported_metrics"]:
                    console.print(f"  ✓ {item['name']} - {item['collection']}")
                console.print()

            if result.get("exported_timelines"):
                console.print("[bold]Timelines:[/bold]")
                for item in result["exported_timelines"]:
                    console.print(f"  ✓ {item['name']} - {item['collection']}")
                console.print()

            if result["skipped_collections"]:
                console.print(f"[dim]⏭  Skipped {len(result['skipped_collections'])} personal collection(s)[/dim]")
                for name in result["skipped_collections"]:
                    console.print(f"[dim]    • {name}[/dim]")
                console.print()

            console.print("[bold cyan]Next steps:[/bold cyan]")
            console.print("  • Files saved to metabase/ directory")
            console.print("  • (Optional) Commit to git for version control:")
            console.print("    [dim]git add metabase/ && git commit -m \"Update dashboards\"[/dim]")

        else:
            console.print("[yellow]⚠️  No assets exported[/yellow]\n")
            if result["errors"]:
                for error in result["errors"]:
                    console.print(f"  [red]•[/red] {error}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@metabase.command("load")
@click.option("--overwrite", is_flag=True, help="Overwrite existing dashboards/questions (WARNING: destructive)")
@click.option("--dry-run", is_flag=True, help="Show what would be imported without actually importing")
@click.pass_context
def metabase_load(ctx, overwrite, dry_run):
    """
    Load Metabase dashboards and questions from files.

    Imports from metabase/ directory into Metabase.
    By default, skips existing items. Use --overwrite to replace existing.

    Behavior:
    - Default: Skip dashboards that already exist in Metabase (safe)
    - --overwrite: Replace existing dashboards with file versions (destructive)
    - --dry-run: Preview what would be imported without making changes

    WARNING: --overwrite will replace existing dashboards/questions in Metabase
             with versions from files. Uncommitted Metabase changes will be lost!

    Workflow:
      1. (If using git) Pull changes: git pull
      2. Import: dango metabase load
      3. Dashboards from files are now in Metabase

    Examples:
      dango metabase load                   # Import new items, skip existing
      dango metabase load --dry-run         # Preview what would be imported
      dango metabase load --overwrite       # Replace existing items (destructive)
    """
    from pathlib import Path
    from .utils import require_project_context
    from dango.visualization.dashboard_manager import DashboardManager

    console.print("\n🍡 [bold]Loading Metabase Assets[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        # Warning for overwrite mode
        if overwrite and not dry_run:
            console.print("[bold yellow]⚠️  WARNING: Overwrite Mode[/bold yellow]")
            console.print("This will replace existing dashboards/questions in Metabase")
            console.print("with versions from files. Unsaved Metabase changes will be lost!\n")

            if not click.confirm("Continue?"):
                console.print("[yellow]Cancelled[/yellow]")
                return

        # Create dashboard manager
        manager = DashboardManager(project_root)

        mode_str = "[dim](dry-run)[/dim]" if dry_run else "[dim](overwrite)[/dim]" if overwrite else "[dim](skip existing)[/dim]"
        with console.status(f"[cyan]Loading assets {mode_str}...[/cyan]", spinner="dots"):
            result = manager.load_from_files(
                overwrite=overwrite,
                dry_run=dry_run
            )

        if result["success"]:
            if dry_run:
                console.print("[bold cyan]Preview (dry-run mode):[/bold cyan]\n")

            total_imported = (
                len(result["imported_dashboards"]) +
                len(result["imported_questions"]) +
                len(result.get("imported_models", [])) +
                len(result.get("imported_metrics", [])) +
                len(result.get("imported_timelines", []))
            )
            total_skipped = len(result["skipped"])

            if total_imported > 0:
                console.print(f"[green]✅ {'Would import' if dry_run else 'Imported'} {total_imported} item(s)[/green]\n")

                if result["imported_dashboards"]:
                    console.print("[bold]Dashboards:[/bold]")
                    for item in result["imported_dashboards"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']} ({item['cards']} cards)")
                    console.print()

                if result["imported_questions"]:
                    console.print("[bold]Questions:[/bold]")
                    for item in result["imported_questions"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

                if result.get("imported_models"):
                    console.print("[bold]Models:[/bold]")
                    for item in result["imported_models"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

                if result.get("imported_metrics"):
                    console.print("[bold]Metrics:[/bold]")
                    for item in result["imported_metrics"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

                if result.get("imported_timelines"):
                    console.print("[bold]Timelines:[/bold]")
                    for item in result["imported_timelines"]:
                        status_icon = "?" if dry_run else "✓"
                        console.print(f"  {status_icon} {item['name']}")
                    console.print()

            if total_skipped > 0:
                console.print(f"[dim]⏭  Skipped {total_skipped} existing item(s)[/dim]")
                for item in result["skipped"]:
                    console.print(f"[dim]    • {item['name']} ({item['type']}) - {item['reason']}[/dim]")
                console.print()

            if result["would_overwrite"] and (overwrite or dry_run):
                console.print(f"[yellow]⚠️  {'Would overwrite' if dry_run else 'Overwrote'} {len(result['would_overwrite'])} existing item(s)[/yellow]")
                for item in result["would_overwrite"]:
                    console.print(f"[yellow]    • {item['name']} ({item['type']})[/yellow]")
                console.print()

            if dry_run and total_imported > 0:
                console.print("[bold cyan]To actually import:[/bold cyan]")
                console.print("  dango metabase load")

        else:
            console.print("[yellow]⚠️  Nothing to import[/yellow]\n")
            if result["errors"]:
                for error in result["errors"]:
                    console.print(f"  [red]•[/red] {error}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@metabase.command("refresh")
@click.pass_context
def metabase_refresh(ctx):
    """
    Refresh Metabase database connection to discover new schemas.

    Use this when you've created new schemas (e.g., marts) and want
    Metabase to discover them. This recreates the database connection.

    Examples:
      dango metabase refresh    # Refresh to discover new schemas
    """
    from pathlib import Path
    from .utils import require_project_context
    import requests
    import yaml
    import time

    console.print("\n🍡 [bold]Refreshing Metabase Connection[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        credentials_file = project_root / ".dango" / "metabase.yml"

        if not credentials_file.exists():
            console.print("[red]✗[/red] Metabase not configured. Run 'dango start' first.")
            raise click.Abort()

        # Load credentials
        with open(credentials_file, 'r') as f:
            credentials = yaml.safe_load(f)

        admin = credentials.get("admin", {})
        old_db = credentials.get("database", {})
        metabase_url = credentials.get("metabase_url", "http://localhost:3000")

        # Check if Metabase is running
        try:
            health_response = requests.get(f"{metabase_url}/api/health", timeout=2)
            if health_response.status_code != 200:
                console.print("[red]✗[/red] Metabase is not running. Start with 'dango start'.")
                raise click.Abort()
        except requests.exceptions.RequestException:
            console.print("[red]✗[/red] Cannot connect to Metabase. Start with 'dango start'.")
            raise click.Abort()

        console.print("[cyan]Step 1:[/cyan] Logging in to Metabase...")
        login_response = requests.post(
            f"{metabase_url}/api/session",
            json={"username": admin.get("email"), "password": admin.get("password")},
            timeout=10
        )

        if login_response.status_code != 200:
            console.print("[red]✗[/red] Failed to login to Metabase")
            raise click.Abort()

        session_id = login_response.json().get("id")
        headers = {"X-Metabase-Session": session_id}

        console.print("[green]✓[/green] Logged in")

        # Delete old database connection
        console.print(f"[cyan]Step 2:[/cyan] Removing old database connection (ID: {old_db.get('id')})...")
        delete_response = requests.delete(
            f"{metabase_url}/api/database/{old_db.get('id')}",
            headers=headers,
            timeout=10
        )

        if delete_response.status_code == 204:
            console.print("[green]✓[/green] Old connection removed")
        else:
            console.print(f"[yellow]⚠[/yellow] Could not remove old connection (status: {delete_response.status_code})")

        # Create new database connection
        console.print("[cyan]Step 3:[/cyan] Creating new database connection...")
        create_response = requests.post(
            f"{metabase_url}/api/database",
            headers=headers,
            json={
                "name": old_db.get("name", "DuckDB Analytics"),
                "engine": "duckdb",
                "details": {
                    "database_file": "/data/warehouse.duckdb",
                    "old_implicit_casting": True,
                    "read_only": False
                }
            },
            timeout=10
        )

        if create_response.status_code != 200:
            console.print(f"[red]✗[/red] Failed to create new connection: {create_response.text}")
            raise click.Abort()

        new_db_id = create_response.json().get("id")
        console.print(f"[green]✓[/green] New connection created (ID: {new_db_id})")

        # Update credentials file
        console.print("[cyan]Step 4:[/cyan] Updating configuration...")
        credentials["database"]["id"] = new_db_id

        with open(credentials_file, 'w') as f:
            yaml.dump(credentials, f, default_flow_style=False)

        console.print("[green]✓[/green] Configuration updated")

        # Wait for sync
        console.print("[cyan]Step 5:[/cyan] Waiting for schema sync...")
        time.sleep(3)

        # Check discovered tables
        metadata_response = requests.get(
            f"{metabase_url}/api/database/{new_db_id}/metadata",
            headers=headers,
            timeout=10
        )

        if metadata_response.status_code == 200:
            tables = metadata_response.json().get("tables", [])
            schemas = set(t.get("schema") for t in tables)

            console.print(f"[green]✓[/green] Discovery complete")
            console.print(f"\n[bold]Discovered schemas:[/bold] {', '.join(sorted(schemas))}")
            console.print(f"[bold]Total tables:[/bold] {len(tables)}\n")

            for schema in sorted(schemas):
                schema_tables = [t.get("name") for t in tables if t.get("schema") == schema]
                console.print(f"  [cyan]{schema}[/cyan]: {', '.join(schema_tables)}")

        console.print(f"\n[green]✨ Metabase connection refreshed successfully![/green]\n")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort()


@cli.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload (development)")
@click.pass_context
def web(ctx, host, port, reload):
    """
    Start the Web UI backend server.

    This starts a FastAPI server that provides:
    - REST API for source management
    - Real-time WebSocket updates
    - Service health monitoring

    The API documentation is available at:
      http://localhost:8080/api/docs

    Examples:
      dango web                      # Start on default port 8080
      dango web --port 3001         # Start on custom port
      dango web --reload            # Start with auto-reload (dev mode)
    """
    from pathlib import Path
    from .utils import require_project_context

    console.print("\n🍡 [bold]Starting Dango Web UI[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        console.print(f"[dim]Project root: {project_root}[/dim]")
        console.print(f"[dim]Server: http://{host}:{port}[/dim]")
        console.print(f"[dim]API docs: http://{host}:{port}/api/docs[/dim]")
        console.print()

        # Import and run uvicorn
        import uvicorn
        from dango.web import app as web_app

        # Set project root in app state
        web_app.app.state.project_root = project_root

        # Start server
        console.print("[green]Starting server...[/green]")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")

        uvicorn.run(
            web_app.app,
            host=host,
            port=port,
            reload=reload,
            log_level="info"
        )

    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Error starting server:[/red] {e}")
        import traceback
        console.print(traceback.format_exc())
        raise click.Abort()


def main():
    """Entry point for CLI"""
    cli(obj={})


if __name__ == "__main__":
    main()
