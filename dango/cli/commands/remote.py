"""dango/cli/commands/remote.py

Remote server management commands for Dango cloud deployments.

Command hierarchy::

    dango remote (group)
    ├── env (subgroup)
    │   ├── set K=V    — Set an environment variable
    │   ├── get K      — Display a variable (masked)
    │   ├── list       — List all variables (masked)
    │   └── delete K   — Remove a variable
    └── firewall (subgroup)
        ├── list       — Show current firewall rules
        ├── allow-ip   — Restrict ports 80/443 to a specific IP
        └── allow-all  — Revert ports 80/443 to public access

All commands require an active cloud deployment (``droplet_id`` and
``firewall_id`` set in ``.dango/cloud.yml``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from dango.cli import console

# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group()
@click.pass_context
def remote(ctx: click.Context) -> None:
    """Manage the remote Dango cloud server.

    Commands:
      dango remote firewall list        Show current firewall rules
      dango remote firewall allow-ip    Restrict web to a specific IP
      dango remote firewall allow-all   Revert web access to public
    """
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# firewall subgroup
# ---------------------------------------------------------------------------


@remote.group("firewall")
@click.pass_context
def firewall(ctx: click.Context) -> None:
    """Manage the cloud server firewall.

    Commands:
      list       Show current inbound/outbound rules
      allow-ip   Restrict ports 80/443 to a specific IP or CIDR
      allow-all  Revert ports 80/443 to public (allow all traffic)
    """
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_cloud_config_or_fail(ctx: click.Context) -> tuple[Any, str]:
    """Load CloudConfig and return (config, firewall_id), or exit with an error.

    Returns:
        Tuple of (CloudConfig, firewall_id string).

    Raises:
        SystemExit: If no deployment or no firewall is configured.
    """
    from dango.cli.utils import require_project_context
    from dango.config.loader import ConfigLoader

    project_root: Path = require_project_context(ctx)
    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()

    if cloud_cfg is None or cloud_cfg.droplet_id is None:
        console.print(
            "[red]Error:[/red] No cloud deployment found. "
            "Run [bold]dango deploy[/bold] to provision a server first."
        )
        raise SystemExit(1)

    if cloud_cfg.firewall_id is None:
        console.print(
            "[red]Error:[/red] No firewall configured for this deployment. "
            "Re-provision or manually set [bold]firewall_id[/bold] in "
            "[bold].dango/cloud.yml[/bold]."
        )
        raise SystemExit(1)

    return cloud_cfg, cloud_cfg.firewall_id


def _ssh_connect_or_fail(ctx: click.Context) -> tuple[Any, Any, Path]:
    """Load cloud config, connect SSH as ``dango`` user, return context.

    Returns:
        Tuple of (CloudConfig, connected SSHManager, project_root Path).
        Caller **must** call ``ssh.disconnect()`` when done.

    Raises:
        SystemExit: If no deployment or SSH connection fails.
    """
    from dango.cli.utils import require_project_context
    from dango.config.loader import ConfigLoader
    from dango.platform.cloud.ssh import SSHManager

    project_root: Path = require_project_context(ctx)
    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()

    if cloud_cfg is None or cloud_cfg.droplet_id is None:
        console.print(
            "[red]Error:[/red] No cloud deployment found. "
            "Run [bold]dango deploy[/bold] to provision a server first."
        )
        raise SystemExit(1)

    server_ip = cloud_cfg.droplet_ip
    if server_ip is None:
        console.print("[red]Error:[/red] No server IP in cloud config.")
        raise SystemExit(1)

    key_path = Path(cloud_cfg.ssh_key_path) if cloud_cfg.ssh_key_path else None
    ssh = SSHManager(key_path=key_path)
    try:
        ssh.connect(server_ip, username="dango")
    except Exception as exc:
        console.print(f"[red]Error:[/red] SSH connection failed: {exc}")
        raise SystemExit(1) from exc

    return cloud_cfg, ssh, project_root


def _make_client() -> Any:
    """Create and return a DigitalOceanClient instance."""
    from dango.platform.cloud.digitalocean import DigitalOceanClient

    return DigitalOceanClient()


# ---------------------------------------------------------------------------
# firewall list
# ---------------------------------------------------------------------------


@firewall.command("list")
@click.pass_context
def firewall_list(ctx: click.Context) -> None:
    """Show current inbound and outbound firewall rules.

    Displays all firewall rules in a table, separated by direction.
    Requires an active deployment with a configured firewall.

    Example:
      dango remote firewall list
    """
    from rich.table import Table

    from dango.platform.cloud.firewall import format_firewall_rules, get_firewall_rules

    _cloud_cfg, firewall_id = _load_cloud_config_or_fail(ctx)
    client = _make_client()

    try:
        fw = get_firewall_rules(client, firewall_id)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to retrieve firewall rules: {exc}")
        raise SystemExit(1) from exc

    rows = format_firewall_rules(fw)

    if not rows:
        console.print("[yellow]No firewall rules configured.[/yellow]")
        return

    table = Table(
        title=f"Firewall: {fw.get('name', firewall_id)}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Direction", style="dim", width=10)
    table.add_column("Protocol", width=10)
    table.add_column("Ports", width=12)
    table.add_column("Sources / Destinations")

    for row in rows:
        direction = row["direction"]
        style = "green" if direction == "inbound" else "blue"
        table.add_row(
            f"[{style}]{direction}[/{style}]",
            row["protocol"],
            row["ports"],
            row["sources_or_destinations"],
        )

    console.print(table)


# ---------------------------------------------------------------------------
# firewall allow-ip
# ---------------------------------------------------------------------------


@firewall.command("allow-ip")
@click.argument("ip_address")
@click.pass_context
def firewall_allow_ip(ctx: click.Context, ip_address: str) -> None:
    """Restrict ports 80 and 443 to IP_ADDRESS.

    IP_ADDRESS can be a bare IPv4 address (e.g. 203.0.113.42) or a CIDR
    range (e.g. 203.0.113.0/24).  Bare IPs are treated as /32 (single host).

    SSH (port 22) is always left open to allow continued server access.

    Examples:
      dango remote firewall allow-ip 203.0.113.42
      dango remote firewall allow-ip 203.0.113.0/24
    """
    from dango.platform.cloud.firewall import add_allowed_ip

    _cloud_cfg, firewall_id = _load_cloud_config_or_fail(ctx)
    client = _make_client()

    try:
        fw = add_allowed_ip(client, firewall_id, ip_address)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc

    console.print(
        f"[green]Firewall updated.[/green] "
        f"Ports 80/443 now restricted to [bold]{ip_address}[/bold]."
    )
    console.print(f"  Firewall: {fw.get('name', firewall_id)}")


# ---------------------------------------------------------------------------
# firewall allow-all
# ---------------------------------------------------------------------------


@firewall.command("allow-all")
@click.pass_context
def firewall_allow_all(ctx: click.Context) -> None:
    """Revert ports 80 and 443 to allow all public traffic.

    Removes any IP restrictions on HTTP/HTTPS access.  SSH (port 22) is
    always left open to allow continued server access.

    Example:
      dango remote firewall allow-all
    """
    from dango.platform.cloud.firewall import allow_all_web

    _cloud_cfg, firewall_id = _load_cloud_config_or_fail(ctx)
    client = _make_client()

    try:
        fw = allow_all_web(client, firewall_id)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc

    console.print("[green]Firewall updated.[/green] Ports 80/443 are now open to all traffic.")
    console.print(f"  Firewall: {fw.get('name', firewall_id)}")


# ---------------------------------------------------------------------------
# Register subgroups from separate modules
# ---------------------------------------------------------------------------

from dango.cli.commands.remote_env import env  # noqa: E402

remote.add_command(env)
