"""dango/cli/commands/project.py

Project management commands (init, rename, info).
"""

import click

from dango.cli import console


@click.command()
@click.argument("project_name", required=False, default=".")
@click.option("--skip-wizard", is_flag=True, help="Skip interactive wizard, create blank project")
@click.option("--force", is_flag=True, help="Force initialization even if project exists")
@click.pass_context
def init(ctx: click.Context, project_name: str, skip_wizard: bool, force: bool) -> None:
    """
    Create a new Dango data project.

    PROJECT_NAME: Name of project directory (default: current directory '.')

    Examples:
      dango init my-analytics        Create new project in ./my-analytics/
      dango init .                   Initialize in current directory
      dango init my-project --skip-wizard  Create blank structure (no wizard)
    """
    from pathlib import Path

    from ..init import init_project

    console.print("🍡 [bold]Initializing Dango project...[/bold]")
    console.print()

    project_dir = Path(project_name)

    try:
        init_project(project_dir, skip_wizard=skip_wizard, force=force)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@click.command()
@click.argument("new_name")
@click.pass_context
def rename(ctx: click.Context, new_name: str) -> None:
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
    import re

    from dango.config import ConfigLoader
    from dango.platform.network import HostsManager, NetworkConfig, NginxManager

    from ..utils import require_project_context

    console.print("🍡 [bold]Renaming Dango Project...[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)

        # Validate new name
        if not re.match(r"^[a-z0-9\-]+$", new_name):
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
        if new_name in existing_projects and existing_projects[new_name]["project_path"] != str(
            project_root
        ):
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
        console.print("[green]✓[/green] Updated .dango/project.yml")

        # Step 2: Update routing.json
        old_project_info = net_config.get_project_info(old_name)
        if old_project_info:
            console.print("[cyan]2/6[/cyan] Updating routing table...")

            # Remove old entry
            net_config.unregister_project(old_name)

            # Register with new name
            net_config.register_project(
                new_name, project_root, old_project_info["backend_port"], new_domain
            )
            net_config.update_project_status(new_name, old_project_info["status"])
            console.print("[green]✓[/green] Updated routing registry")
        else:
            console.print(
                "[cyan]2/6[/cyan] [dim]No routing entry found (project not started yet)[/dim]"
            )

        # Step 3: Update nginx site config
        if old_project_info:
            console.print("[cyan]3/6[/cyan] Updating nginx configuration...")

            # Remove old site config
            nginx_manager.remove_project_config(old_name)

            # Create new site config
            nginx_manager.write_project_config(
                new_name, new_domain, old_project_info["backend_port"]
            )
            console.print("[green]✓[/green] Updated nginx site config")
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
                console.print("[green]✓[/green] nginx reloaded with new configuration")
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
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@click.command()
@click.pass_context
def info(ctx: click.Context) -> None:
    """
    Show project information and context.

    Displays:
      - Project name and purpose
      - Stakeholders
      - Data refresh schedule
      - Last sync time
      - Getting started guide
    """
    from rich.panel import Panel
    from rich.table import Table

    from dango.config import get_config

    from ..utils import require_project_context

    console.print("🍡 [bold]Project Info[/bold]")
    console.print()

    try:
        project_root = require_project_context(ctx)
        config = get_config(project_root)

        # Project details
        console.print(
            Panel(
                f"[bold]Name:[/bold] {config.project.name}\n"
                f"[bold]Created:[/bold] {config.project.created.strftime('%Y-%m-%d')}\n"
                f"[bold]Created by:[/bold] {config.project.created_by}\n\n"
                f"[bold]Purpose:[/bold]\n{config.project.purpose}",
                title="📋 Project Details",
                border_style="cyan",
            )
        )
        console.print()

        # Stakeholders
        if config.project.stakeholders:
            table = Table(title="Stakeholders", show_header=True, header_style="bold cyan")
            table.add_column("Name", style="bold")
            table.add_column("Role")
            table.add_column("Contact")

            for stakeholder in config.project.stakeholders:
                table.add_row(stakeholder.name, stakeholder.role, stakeholder.contact)

            console.print(table)
            console.print()

        # SLA and Limitations
        if config.project.sla or config.project.limitations:
            info_text = ""
            if config.project.sla:
                info_text += f"[bold]Data Freshness SLA:[/bold]\n{config.project.sla}\n\n"
            if config.project.limitations:
                info_text += f"[bold]Limitations:[/bold]\n{config.project.limitations}\n\n"

            console.print(
                Panel(info_text.strip(), title="ℹ️  Additional Info", border_style="yellow")
            )
            console.print()

        # Getting Started
        if config.project.getting_started:
            console.print(
                Panel(
                    config.project.getting_started, title="🚀 Getting Started", border_style="green"
                )
            )
            console.print()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise click.Abort() from e
