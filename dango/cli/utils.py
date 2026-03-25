"""dango/cli/utils.py

CLI display helpers and project context utilities.
"""

import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from dango.config import ProjectNotFoundError
from dango.config.helpers import find_project_root

console = Console()


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
        raise click.Abort() from e


def print_error(message: str) -> None:
    """Print error message."""
    console.print(f"[red]Error:[/red] {message}")


def print_success(message: str) -> None:
    """Print success message."""
    console.print(f"[green]✓[/green] {message}")


def print_info(message: str) -> None:
    """Print info message."""
    console.print(f"[blue]ℹ[/blue] {message}")


def print_warning(message: str) -> None:
    """Print warning message."""
    console.print(f"[yellow]⚠[/yellow] {message}")


def print_panel(content: str, title: str, border_style: str = "blue") -> None:
    """Print content in a panel."""
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


def get_git_branch(project_root: Path | None = None) -> str | None:
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


def load_cloud_config_with_ssh(ctx: click.Context) -> tuple:
    """Load CloudConfig and return a connected SSHManager.

    Returns:
        Tuple of (CloudConfig, connected SSHManager).  Caller must close SSH.

    Raises:
        SystemExit: If no deployment is configured or SSH connection fails.
    """
    from dango.config.loader import ConfigLoader
    from dango.platform.cloud.ssh import SSHManager

    project_root: Path = require_project_context(ctx)
    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()

    if cloud_cfg is None or cloud_cfg.droplet_ip is None:
        console.print(
            "[red]Error:[/red] No cloud deployment found. "
            "Run [bold]dango deploy[/bold] to provision a server first."
        )
        raise SystemExit(1)

    key_path = project_root / cloud_cfg.ssh_key_path
    ssh = SSHManager(key_path=key_path)

    try:
        ssh.connect(cloud_cfg.droplet_ip, username="root")
    except Exception as exc:
        console.print(f"[red]Error:[/red] SSH connection failed: {exc}")
        raise SystemExit(1) from exc

    return cloud_cfg, ssh


def check_git_branch_warning(project_root: Path | None = None) -> None:
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
        console.print(
            Panel(
                f"[yellow]⚠️  You're on the [bold]{branch}[/bold] branch.[/yellow]\n\n"
                "💡 Consider creating a feature branch for data changes:\n"
                "   [dim]git checkout -b data/update-sources[/dim]\n\n"
                "This makes it easier to review and rollback changes if needed.",
                title="Git Branch Reminder",
                border_style="yellow",
            )
        )
        console.print()
