"""dango/platform/docker.py

Handles Docker Compose operations for Dango services.
"""

import hashlib
import os
import subprocess
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.table import Table

from dango.exceptions import format_structured_error

console = Console()


def get_compose_project_name(project_root: Path | str) -> str:
    """Return the Docker Compose project name for the given project root.

    Deterministic name derived from path hash to avoid collisions between
    multiple Dango projects on the same machine or server.
    """
    path_hash = hashlib.md5(str(project_root).encode(), usedforsecurity=False).hexdigest()[:8]
    return f"dango-{path_hash}"


class ServiceStatus(str, Enum):
    """Service status"""

    RUNNING = "running"
    STOPPED = "stopped"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    UNKNOWN = "unknown"


class DockerManager:
    """Manages Docker Compose services"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.compose_file = project_root / "docker-compose.yml"

    @property
    def compose_project_name(self) -> str:
        """Deterministic project name derived from path to avoid collisions.

        NOTE: Containers started before this change used Docker's default
        naming (directory-based) and will be orphaned.  ``dango stop --all``
        cleans those up via ``docker ps --filter name=``.
        """
        return get_compose_project_name(self.project_root)

    def _compose_env(self) -> dict[str, str]:
        """Return env dict with COMPOSE_PROJECT_NAME set."""
        env = os.environ.copy()
        env["COMPOSE_PROJECT_NAME"] = self.compose_project_name
        return env

    def _metabase_image_exists(self) -> bool:
        """Check if the dango-metabase Docker image already exists locally."""
        try:
            result = subprocess.run(
                ["docker", "images", "--filter", "reference=dango-metabase", "-q"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_docker_available(self) -> bool:
        """Check if Docker is available"""
        try:
            result = subprocess.run(
                ["docker", "--version"], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_docker_daemon_running(self) -> bool:
        """Check if Docker daemon is running"""
        try:
            result = subprocess.run(["docker", "ps"], capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_compose_available(self) -> bool:
        """Check if Docker Compose is available"""
        try:
            # Try docker compose (v2)
            result = subprocess.run(
                ["docker", "compose", "version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return True

            # Fall back to docker-compose (v1)
            result = subprocess.run(
                ["docker-compose", "--version"], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def get_compose_command(self) -> list[str]:
        """Get the docker compose command (v2 or v1)"""
        # Try docker compose (v2) first
        try:
            result = subprocess.run(
                ["docker", "compose", "version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return ["docker", "compose"]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Fall back to docker-compose (v1)
        return ["docker-compose"]

    def start_services(self) -> bool:
        """
        Start Docker Compose services.

        Returns:
            True if successful, False otherwise
        """
        if not self.compose_file.exists():
            msg = format_structured_error(
                what_failed="docker-compose.yml not found",
                causes=[
                    "Not in a Dango project directory",
                    f"Expected location: {self.compose_file}",
                ],
                suggested_fix="Run 'dango init' to create a new project",
            )
            console.print(f"[red]Error:[/red]\n{msg}")
            return False

        if not self.is_docker_available():
            msg = format_structured_error(
                what_failed="Docker is not available",
                causes=[
                    "Docker Desktop not installed",
                    "Docker daemon not running",
                    "Docker CLI not in system PATH",
                ],
                suggested_fix="Install Docker from https://docs.docker.com/get-docker/ and ensure the daemon is running",
            )
            console.print(f"[red]Error:[/red]\n{msg}")
            return False

        if not self.is_compose_available():
            msg = format_structured_error(
                what_failed="Docker Compose is not available",
                causes=[
                    "Docker Compose plugin not installed",
                    "docker-compose (v1) not in PATH",
                ],
                suggested_fix="Install Docker Compose: https://docs.docker.com/compose/install/",
            )
            console.print(f"[red]Error:[/red]\n{msg}")
            return False

        console.print("Starting Dango services...")
        console.print()

        cmd = self.get_compose_command() + ["-f", str(self.compose_file), "up", "-d"]

        first_run = not self._metabase_image_exists()
        if first_run:
            console.print(
                "[cyan]Building Metabase image (first run, may take 5-10 minutes)...[/cyan]"
            )
        timeout = 600 if first_run else 120

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._compose_env(),
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] Services started successfully")
                console.print()
                self._print_service_urls()
                return True
            else:
                stderr_lower = result.stderr.lower()
                if "port" in stderr_lower and "already" in stderr_lower:
                    causes = [
                        "Another service is using the required port",
                        "A previous Dango instance is still running",
                    ]
                    fix = "Run 'dango stop' first, or check for conflicting services"
                else:
                    causes = [
                        "Docker image pull failed",
                        "Container configuration error",
                        "Insufficient disk space or memory",
                    ]
                    fix = "Check the error output above and Docker logs"
                msg = format_structured_error(
                    what_failed="Failed to start Docker services",
                    causes=causes,
                    suggested_fix=fix,
                )
                console.print(f"[red]Error:[/red]\n{msg}")
                if result.stderr:
                    console.print(f"\nFull output:\n{result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            causes = [
                "Docker image download is slow",
                "Insufficient system resources",
                "Docker daemon is unresponsive",
            ]
            if first_run:
                causes.insert(0, "First-run Metabase image build can take 5-10 minutes")
            msg = format_structured_error(
                what_failed="Timeout starting Docker services",
                causes=causes,
                suggested_fix="Check Docker status with 'docker ps' and retry",
            )
            console.print(f"[red]Error:[/red]\n{msg}")
            return False
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            return False

    def stop_services(self) -> bool:
        """
        Stop Docker Compose services.

        Returns:
            True if successful, False otherwise
        """
        if not self.compose_file.exists():
            console.print("[yellow]Warning:[/yellow] docker-compose.yml not found")
            return True  # Nothing to stop

        console.print("Stopping Dango services...")

        cmd = self.get_compose_command() + ["-f", str(self.compose_file), "down"]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=60,
                env=self._compose_env(),
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] Services stopped")
                self._warn_orphaned_containers()
                return True
            else:
                console.print("[red]Error:[/red] Failed to stop services")
                console.print(result.stderr)
                return False

        except subprocess.TimeoutExpired:
            console.print("[red]Error:[/red] Timeout stopping services")
            return False
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            return False

    def _warn_orphaned_containers(self) -> None:
        """Warn if Dango containers from a previous naming scheme are still running."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "--filter", "name=metabase", "--filter", "name=dbt"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                console.print(
                    "[yellow]⚠[/yellow]  Other Dango containers still running. "
                    "Run [cyan]dango stop --all[/cyan] to stop them."
                )
        except Exception:
            pass  # Best-effort, never block stop

    def stop_all_dango_containers(self, all_projects: bool = False) -> bool:
        """
        Stop Dango containers.

        By default, stops only containers belonging to this project (determined
        by the Docker Compose project label).

        Args:
            all_projects: If True, stop ALL Dango containers globally (from any
                project), using name-based filtering. This is useful when
                switching between test projects or cleaning up containers
                from a previous naming scheme.

        Returns:
            True if successful, False otherwise
        """
        if all_projects:
            console.print("Stopping all Dango containers (from any project)...")
        else:
            console.print(f"Stopping Dango containers for {self.compose_project_name}...")

        try:
            if all_projects:
                # Find all containers with Dango service names globally
                result = subprocess.run(
                    ["docker", "ps", "-q", "--filter", "name=metabase", "--filter", "name=dbt"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                # Find containers belonging to this project via compose project label
                result = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={self.compose_project_name}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            if result.returncode != 0:
                console.print("[yellow]⚠[/yellow]  Could not list Docker containers")
                return False

            container_ids = result.stdout.strip().split("\n")
            container_ids = [cid for cid in container_ids if cid]  # Filter empty strings

            if not container_ids:
                scope = "any project" if all_projects else "this project"
                console.print(f"[dim]No Dango containers found running for {scope}[/dim]")
                return True

            # Stop the containers
            console.print(f"Found {len(container_ids)} Dango container(s), stopping...")
            result = subprocess.run(
                ["docker", "stop"] + container_ids, capture_output=True, text=True, timeout=60
            )

            if result.returncode == 0:
                if all_projects:
                    console.print("[green]✓[/green] Stopped all Dango containers")
                else:
                    console.print("[green]✓[/green] Stopped Dango containers for this project")
                return True
            else:
                console.print("[yellow]⚠[/yellow]  Some containers may not have stopped")
                return False

        except subprocess.TimeoutExpired:
            console.print("[red]Error:[/red] Timeout stopping containers")
            return False
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow]  Error stopping containers: {e}")
            return False

    def get_service_status(self) -> dict[str, ServiceStatus]:
        """
        Get status of all services.

        Returns:
            Dict mapping service names to their status
        """
        if not self.compose_file.exists():
            return {}

        cmd = self.get_compose_command() + ["-f", str(self.compose_file), "ps", "--format", "json"]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=10,
                env=self._compose_env(),
            )

            if result.returncode != 0:
                return {}

            # Parse output
            import json

            statuses = {}

            # Output might be multiple JSON objects, one per line
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                try:
                    service_info = json.loads(line)
                    name = service_info.get("Service", service_info.get("Name", "unknown"))
                    state = service_info.get("State", "unknown")
                    health = service_info.get("Health", "")

                    # Map state to ServiceStatus
                    if state == "running":
                        if health == "unhealthy":
                            status = ServiceStatus.UNHEALTHY
                        elif health == "starting":
                            status = ServiceStatus.STARTING
                        else:
                            status = ServiceStatus.RUNNING
                    elif state in ["exited", "stopped"]:
                        status = ServiceStatus.STOPPED
                    else:
                        status = ServiceStatus.UNKNOWN

                    statuses[name] = status

                except json.JSONDecodeError:
                    continue

            return statuses

        except (subprocess.TimeoutExpired, Exception):
            return {}

    def print_status(self) -> None:
        """Print service status table"""
        statuses = self.get_service_status()

        if not statuses:
            console.print("[yellow]No services running[/yellow]")
            console.print()
            console.print("Run [cyan]dango start[/cyan] to start services")
            return

        table = Table(title="Dango Services", show_header=True, header_style="bold cyan")
        table.add_column("Service", style="bold")
        table.add_column("Status")
        table.add_column("URL")

        # Service URL mappings
        urls = {
            "nginx": "http://localhost:8800",
            "metabase": "http://localhost:3001 (or via nginx)",
            "dbt-docs": "http://localhost:8081 (or via nginx)",
            "prefect-server": "http://localhost:4200 (or via nginx)",
        }

        for service, status in statuses.items():
            # Color based on status
            if status == ServiceStatus.RUNNING:
                status_text = "[green]● Running[/green]"
            elif status == ServiceStatus.STOPPED:
                status_text = "[red]● Stopped[/red]"
            elif status == ServiceStatus.UNHEALTHY:
                status_text = "[yellow]● Unhealthy[/yellow]"
            elif status == ServiceStatus.STARTING:
                status_text = "[cyan]● Starting[/cyan]"
            else:
                status_text = "[dim]● Unknown[/dim]"

            url = urls.get(service, "-")

            table.add_row(service, status_text, url)

        console.print(table)

    def _print_service_urls(self) -> None:
        """Print service URLs (via FastAPI proxy)"""
        # Load config to get the configured port
        from dango.config.loader import ConfigLoader

        try:
            config_loader = ConfigLoader(self.project_root)
            config = config_loader.load_config()
            port = config.platform.port
        except Exception:
            # Fallback to default port if config can't be loaded
            port = 8800

        console.print("[bold cyan]Docker services started:[/bold cyan]")
        console.print()
        console.print(
            f"  Metabase: [link=http://localhost:{port}/metabase]http://localhost:{port}/metabase[/link]"
        )
        console.print(
            f"  dbt Docs: [link=http://localhost:{port}/dbt-docs]http://localhost:{port}/dbt-docs[/link]"
        )
        console.print()
        console.print("[dim]Note: Services may take 30-60s to become healthy[/dim]")
