"""dango/cli/commands/remote_power.py

Remote power state: ``dango remote power-on`` and the power-state lookups used by
``remote status`` and ``remote resize``.

An interrupted ``remote resize`` can leave the droplet powered off, where SSH-based
commands cannot reach it.  These commands use the DigitalOcean API instead.
"""

from __future__ import annotations

from typing import Any

import click

from dango.cli import console
from dango.cli.commands.remote import remote
from dango.cli.utils import safe_confirm

POWER_ON_HINT = "dango remote power-on"


def lookup_power_state(cloud_cfg: Any) -> str | None:
    """Best-effort DigitalOcean power state, or ``None`` (BYOS, no token, API failure)."""
    if getattr(cloud_cfg, "provider", None) == "byos" or not getattr(cloud_cfg, "droplet_id", None):
        return None
    try:
        from dango.cli.commands.remote import _make_client
        from dango.platform.cloud.resize import get_power_state

        return get_power_state(_make_client(), cloud_cfg.droplet_id)
    except Exception:
        return None


def refuse_if_powered_off(cloud_cfg: Any) -> None:
    """Raise ``click.UsageError`` with the recovery command if the droplet is powered off."""
    if lookup_power_state(cloud_cfg) == "off":
        raise click.UsageError(
            f"Server is powered off (likely an interrupted resize). Run: {POWER_ON_HINT}"
        )


def _adopt_live_size(ssh: Any, cloud_cfg: Any, project_root: Any, live_size: str) -> None:
    """A resize finished while the client was gone: record the new size and refresh profiles."""
    from dango.platform.cloud.provisioning import save_provisioning_metadata
    from dango.platform.cloud.resize import regenerate_dbt_profiles

    console.print(
        f"[yellow]The server was resized to {live_size} (cloud.yml had {cloud_cfg.size}); "
        "updating cloud.yml and dbt profiles.[/yellow]"
    )
    try:
        regenerate_dbt_profiles(ssh, live_size, cloud_cfg.dbt_overrides)
    except Exception as exc:
        console.print(f"[yellow]profiles.yml was not regenerated: {exc}[/yellow]")
    save_provisioning_metadata(
        project_root,
        droplet_id=cloud_cfg.droplet_id,
        droplet_ip=cloud_cfg.droplet_ip,
        region=cloud_cfg.region,
        size=live_size,
    )


@remote.command("power-on")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remote_power_on(ctx: click.Context, yes: bool) -> None:
    """Power on the server if it is off (for example after an interrupted resize).

    Uses the DigitalOcean API, so it works while SSH cannot connect. Starts the
    services and checks health once the server is reachable.
    """
    from rich.status import Status

    from dango.cli.commands.remote import _connect_ssh, _make_client, _require_cloud_deployment
    from dango.platform.cloud.backup import start_services, verify_health
    from dango.platform.cloud.resize import get_power_state, power_on_and_wait

    cloud_cfg, project_root = _require_cloud_deployment(ctx)
    if cloud_cfg.provider == "byos":
        console.print(
            "[red]Error:[/red] Power control is not available for BYOS deployments. "
            "Use your hosting provider."
        )
        raise click.Abort()

    try:
        client = _make_client()
        state = get_power_state(client, cloud_cfg.droplet_id)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Could not read the server's power state: {exc}")
        raise click.Abort() from exc

    console.print(f"Server power state: [bold]{state}[/bold]")
    if state == "active":
        console.print("[green]Server is already powered on.[/green]")
        return
    if state != "off":
        console.print(f"[red]Error:[/red] Cannot power on a server in state '{state}'.")
        raise click.Abort()

    if not yes and not safe_confirm("Power on the server now?"):
        console.print("[yellow]Cancelled.[/yellow]")
        return

    try:
        with Status("[bold blue]Powering on...", console=console):
            power_on_and_wait(client, cloud_cfg.droplet_id, cloud_cfg.droplet_ip)
        live_size = str(client.get_droplet(cloud_cfg.droplet_id).get("size_slug", ""))
        ssh = _connect_ssh(cloud_cfg, project_root)
        try:
            if live_size and live_size != cloud_cfg.size:
                _adopt_live_size(ssh, cloud_cfg, project_root, live_size)
            start_services(ssh)
            healthy = verify_health(ssh)
        finally:
            ssh.disconnect()
    except Exception as exc:
        console.print(f"[red]Error:[/red] Power-on failed: {exc}")
        raise click.Abort() from exc

    if healthy:
        console.print("[green]Server is powered on and healthy.[/green]")
    else:
        console.print(
            "[yellow]Server is powered on, but the health check did not pass.[/yellow] "
            "Check [bold]dango remote status[/bold]."
        )
