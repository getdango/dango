"""
Docker Service Management

Handles Docker Compose operations for Dango services.
"""

import subprocess
from pathlib import Path
from typing import Optional, Dict, List
from enum import Enum

from rich.console import Console
from rich.table import Table

console = Console()


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

    def is_docker_available(self) -> bool:
        """Check if Docker is available"""
        try:
            result = subprocess.run(
                ["docker", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_docker_daemon_running(self) -> bool:
        """Check if Docker daemon is running"""
        try:
            result = subprocess.run(
                ["docker", "ps"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_compose_available(self) -> bool:
        """Check if Docker Compose is available"""
        try:
            # Try docker compose (v2)
            result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return True

            # Fall back to docker-compose (v1)
            result = subprocess.run(
                ["docker-compose", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def get_compose_command(self) -> List[str]:
        """Get the docker compose command (v2 or v1)"""
        # Try docker compose (v2) first
        try:
            result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=5
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
            console.print("[red]Error:[/red] docker-compose.yml not found")
            console.print(f"Expected location: {self.compose_file}")
            return False

        if not self.is_docker_available():
            console.print("[red]Error:[/red] Docker is not available")
            console.print("Please install Docker: https://docs.docker.com/get-docker/")
            return False

        if not self.is_compose_available():
            console.print("[red]Error:[/red] Docker Compose is not available")
            return False

        console.print("Starting Dango services...")
        console.print()

        cmd = self.get_compose_command() + [
            "-f", str(self.compose_file),
            "up", "-d"
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] Services started successfully")
                console.print()
                self._print_service_urls()
                return True
            else:
                console.print(f"[red]Error:[/red] Failed to start services")
                console.print(result.stderr)
                return False

        except subprocess.TimeoutExpired:
            console.print("[red]Error:[/red] Timeout starting services")
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

        cmd = self.get_compose_command() + [
            "-f", str(self.compose_file),
            "down"
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] Services stopped")
                return True
            else:
                console.print(f"[red]Error:[/red] Failed to stop services")
                console.print(result.stderr)
                return False

        except subprocess.TimeoutExpired:
            console.print("[red]Error:[/red] Timeout stopping services")
            return False
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            return False

    def stop_all_dango_containers(self) -> bool:
        """
        Stop ALL Dango containers globally (from any project).

        This is useful when switching between test projects or when
        containers from a previous project are still running on required ports.

        Returns:
            True if successful, False otherwise
        """
        console.print("Stopping all Dango containers (from any project)...")

        try:
            # Find all containers with Dango service names (metabase, dbt-docs)
            # These are created by docker-compose with predictable naming
            result = subprocess.run(
                ["docker", "ps", "-q", "--filter", "name=metabase", "--filter", "name=dbt"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                console.print("[yellow]⚠[/yellow]  Could not list Docker containers")
                return False

            container_ids = result.stdout.strip().split('\n')
            container_ids = [cid for cid in container_ids if cid]  # Filter empty strings

            if not container_ids:
                console.print("[dim]No Dango containers found running[/dim]")
                return True

            # Stop the containers
            console.print(f"Found {len(container_ids)} Dango container(s), stopping...")
            result = subprocess.run(
                ["docker", "stop"] + container_ids,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] Stopped all Dango containers")
                return True
            else:
                console.print(f"[yellow]⚠[/yellow]  Some containers may not have stopped")
                return False

        except subprocess.TimeoutExpired:
            console.print("[red]Error:[/red] Timeout stopping containers")
            return False
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow]  Error stopping containers: {e}")
            return False

    def get_service_status(self) -> Dict[str, ServiceStatus]:
        """
        Get status of all services.

        Returns:
            Dict mapping service names to their status
        """
        if not self.compose_file.exists():
            return {}

        cmd = self.get_compose_command() + [
            "-f", str(self.compose_file),
            "ps", "--format", "json"
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {}

            # Parse output
            import json
            statuses = {}

            # Output might be multiple JSON objects, one per line
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue

                try:
                    service_info = json.loads(line)
                    name = service_info.get('Service', service_info.get('Name', 'unknown'))
                    state = service_info.get('State', 'unknown')
                    health = service_info.get('Health', '')

                    # Map state to ServiceStatus
                    if state == 'running':
                        if health == 'unhealthy':
                            status = ServiceStatus.UNHEALTHY
                        elif health == 'starting':
                            status = ServiceStatus.STARTING
                        else:
                            status = ServiceStatus.RUNNING
                    elif state in ['exited', 'stopped']:
                        status = ServiceStatus.STOPPED
                    else:
                        status = ServiceStatus.UNKNOWN

                    statuses[name] = status

                except json.JSONDecodeError:
                    continue

            return statuses

        except (subprocess.TimeoutExpired, Exception):
            return {}

    def print_status(self):
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
            "nginx": "http://dango.local or http://localhost",
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

    def _print_service_urls(self):
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
        console.print(f"  Metabase: [link=http://localhost:{port}/metabase]http://localhost:{port}/metabase[/link]")
        console.print(f"  dbt Docs: [link=http://localhost:{port}/dbt-docs]http://localhost:{port}/dbt-docs[/link]")
        console.print()
        console.print("[dim]Note: Services may take 30-60s to become healthy[/dim]")
