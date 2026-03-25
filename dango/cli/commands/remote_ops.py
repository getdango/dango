"""dango/cli/commands/remote_ops.py

Remote operations: resize, migrate, and upgrade commands.

These commands are registered on the ``remote`` group defined in
``remote.py`` via ``@remote.command()`` decorators.  The parent module
triggers registration by importing this module at the bottom of ``remote.py``.
"""

from __future__ import annotations

import click

from dango.cli import console
from dango.cli.commands.remote import remote

# ---------------------------------------------------------------------------
# dango remote upgrade
# ---------------------------------------------------------------------------


@remote.command("upgrade")
@click.option(
    "--version",
    "target_version",
    default=None,
    help="Specific version to install (e.g. 1.2.3). Defaults to latest on PyPI.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option("--skip-backup", is_flag=True, help="Skip the pre-upgrade backup.")
@click.pass_context
def remote_upgrade(
    ctx: click.Context,
    target_version: str | None,
    yes: bool,
    skip_backup: bool,
) -> None:
    """Upgrade Dango on the remote server."""
    from rich.status import Status

    from dango.cli.commands.remote import _load_cloud_config_with_ssh_or_fail
    from dango.platform.cloud.upgrade import (
        UpgradeResult,
        check_versions,
        upgrade_dango,
        validate_version_string,
    )

    # Validate version early (before SSH connection)
    if target_version is not None:
        try:
            validate_version_string(target_version)
        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1) from exc

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        # Show current vs target version
        console.print("[bold]Checking versions...[/bold]")
        current, latest = check_versions(ssh)

        display_target = target_version or latest or "unknown"
        console.print(f"  Current version: [cyan]{current or 'unknown'}[/cyan]")
        console.print(f"  Target version:  [cyan]{display_target}[/cyan]")

        if current == display_target:
            console.print("\n[green]Already at target version — no upgrade needed.[/green]")
            return

        if not latest and target_version is None:
            console.print(
                "\n[red]Error:[/red] Could not determine latest version from PyPI. "
                "Specify a version with --version."
            )
            raise SystemExit(1)

        # Confirm
        if not yes:
            console.print()
            if skip_backup:
                console.print("[yellow]Warning:[/yellow] Pre-upgrade backup will be skipped.")
            if not click.confirm("Proceed with upgrade?"):
                console.print("[yellow]Upgrade cancelled.[/yellow]")
                return

        # Execute upgrade
        with Status("[bold blue]Upgrading...", console=console) as status:

            def _on_progress(step: str, step_status: str) -> None:
                if step_status == "running":
                    labels: dict[str, str] = {
                        "check_version": "Checking current version...",
                        "check_pypi": "Checking latest PyPI version...",
                        "backup": "Creating pre-upgrade backup...",
                        "stop_services": "Stopping services...",
                        "pip_install": "Installing new version...",
                        "migrations": "Running migrations...",
                        "docker_rebuild": "Rebuilding Docker images...",
                        "start_services": "Starting services...",
                        "verify_health": "Verifying health...",
                    }
                    status.update(f"[bold blue]{labels.get(step, step)}")

            result: UpgradeResult = upgrade_dango(
                ssh,
                version=target_version,
                skip_backup=skip_backup,
                on_progress=_on_progress,
            )

        # Show results
        console.print()
        if result.health_check_passed:
            console.print("[green]Upgrade complete.[/green]")
        else:
            console.print("[yellow]Upgrade complete with warnings.[/yellow]")

        console.print(
            f"  Version: [cyan]{result.old_version}[/cyan] → [cyan]{result.new_version}[/cyan]"
        )
        console.print(f"  Duration: {result.duration_seconds}s")
        if result.backup_path:
            console.print(f"  Backup: {result.backup_path}")
        if result.migrations_run:
            console.print("  Migrations: applied")
        if result.docker_rebuilt:
            console.print("  Docker: rebuilt")

        for warning in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {warning}")

        if not result.health_check_passed:
            console.print(
                "\n[yellow]Health check failed.[/yellow] "
                "Run [bold]dango remote rollback[/bold] to restore the previous version."
            )

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] Upgrade failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# dango remote resize
# ---------------------------------------------------------------------------


@remote.command("resize")
@click.argument("size", required=False, default=None)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remote_resize(ctx: click.Context, size: str | None, yes: bool) -> None:
    """Resize the remote server (change CPU/RAM).

    If SIZE is omitted, shows current spec and available tiers.
    If SIZE is provided, resizes the droplet to that size slug.
    """
    from rich.status import Status
    from rich.table import Table

    from dango.cli.commands.remote import (
        _load_cloud_config_with_ssh_or_fail,
        _make_client,
        _require_cloud_deployment,
    )
    from dango.platform.cloud.provisioning import (
        SIZE_TIERS,
        get_size_tier,
    )
    from dango.platform.cloud.resize import (
        ResizeResult,
        get_disk_warning,
        resize_droplet,
        validate_size_slug,
    )

    cloud_cfg, project_root = _require_cloud_deployment(ctx)

    if cloud_cfg.provider == "byos":
        console.print(
            "[red]Error:[/red] Resize is not available for BYOS deployments. "
            "Manage server sizing through your hosting provider."
        )
        raise SystemExit(1)

    if size is None:
        # Info mode: show current spec and available tiers
        current_tier = get_size_tier(cloud_cfg.size)
        console.print("[bold]Current server spec:[/bold]")
        if current_tier:
            console.print(
                f"  {current_tier.name} ({current_tier.slug}) — "
                f"{current_tier.vcpus} vCPU, {current_tier.ram_gb} GB RAM, "
                f"{current_tier.disk_gb} GB disk, ${current_tier.price_monthly}/mo"
            )
        else:
            console.print(f"  {cloud_cfg.size} (custom size)")

        console.print("\n[bold]Available tiers:[/bold]")
        table = Table(show_header=True)
        table.add_column("Name")
        table.add_column("Slug")
        table.add_column("vCPUs")
        table.add_column("RAM (GB)")
        table.add_column("Disk (GB)")
        table.add_column("Price/mo")
        for tier in SIZE_TIERS:
            marker = " ←" if tier.slug == cloud_cfg.size else ""
            table.add_row(
                f"{tier.name}{marker}",
                tier.slug,
                str(tier.vcpus),
                str(tier.ram_gb),
                str(tier.disk_gb),
                f"${tier.price_monthly}",
            )
        console.print(table)
        console.print("\nTo resize: [bold]dango remote resize <SIZE_SLUG>[/bold]")
        return

    # Resize mode: validate and execute
    try:
        validate_size_slug(size)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc

    # Show current vs new comparison
    current_tier = get_size_tier(cloud_cfg.size)
    new_tier = get_size_tier(size)

    console.print("[bold]Resize plan:[/bold]")
    if current_tier:
        console.print(
            f"  Current: {current_tier.name} ({current_tier.slug}) — "
            f"{current_tier.vcpus} vCPU, {current_tier.ram_gb} GB RAM, "
            f"${current_tier.price_monthly}/mo"
        )
    else:
        console.print(f"  Current: {cloud_cfg.size}")
    if new_tier:
        console.print(
            f"  New:     {new_tier.name} ({new_tier.slug}) — "
            f"{new_tier.vcpus} vCPU, {new_tier.ram_gb} GB RAM, "
            f"${new_tier.price_monthly}/mo"
        )
    else:
        console.print(f"  New:     {size}")

    # Disk warning
    disk_warning = get_disk_warning(cloud_cfg.size, size)
    if disk_warning:
        console.print(f"\n  [yellow]Warning:[/yellow] {disk_warning}")

    console.print(
        "\n[yellow]Note:[/yellow] The server will be powered off during resize. "
        "Expect 1-3 minutes of downtime."
    )

    if not yes:
        if not click.confirm("\nProceed with resize?"):
            console.print("[yellow]Resize cancelled.[/yellow]")
            return

    # Connect SSH + DO client
    _, ssh = _load_cloud_config_with_ssh_or_fail(ctx)
    client = _make_client()

    try:
        with Status("[bold blue]Resizing...", console=console) as status:

            def _on_progress(step: str, step_status: str) -> None:
                if step_status == "running":
                    labels: dict[str, str] = {
                        "backup": "Creating pre-resize backup...",
                        "power_off": "Powering off droplet...",
                        "resize": "Resizing droplet...",
                        "power_on": "Powering on droplet...",
                        "wait_active": "Waiting for droplet...",
                        "wait_ssh": "Waiting for SSH...",
                        "dbt_profiles": "Regenerating dbt profiles...",
                        "start_services": "Starting services...",
                        "verify_health": "Verifying health...",
                    }
                    status.update(f"[bold blue]{labels.get(step, step)}")

            result: ResizeResult = resize_droplet(
                client,
                ssh,
                cloud_cfg.droplet_id,
                size,
                dbt_overrides=cloud_cfg.dbt_overrides,
                on_progress=_on_progress,
                project_root=project_root,
                region=cloud_cfg.region,
            )

        # Show results
        console.print()
        console.print("[green]Resize complete.[/green]")
        console.print(f"  Size: [cyan]{result.old_size}[/cyan] → [cyan]{result.new_size}[/cyan]")
        console.print(f"  Duration: {result.duration_seconds}s")
        if result.backup_path:
            console.print(f"  Backup: {result.backup_path}")
        if result.dbt_profiles_regenerated:
            console.print("  dbt profiles.yml: regenerated")

        for warning in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {warning}")

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] Resize failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# dango remote migrate
# ---------------------------------------------------------------------------


@remote.command("migrate")
@click.option("--size", required=True, help="Size slug for the new server.")
@click.option(
    "--region",
    default=None,
    help="Region slug for the new server. Defaults to current region.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remote_migrate(
    ctx: click.Context,
    size: str,
    region: str | None,
    yes: bool,
) -> None:
    """Migrate to a new server (new droplet for disk/region changes).

    Creates a new server, transfers data via Spaces, and destroys the old
    server after verification. Requires Spaces to be configured.
    """
    from rich.status import Status

    from dango.cli.commands.remote import (
        _connect_ssh,
        _make_client,
        _require_cloud_deployment,
    )
    from dango.platform.cloud.migrate import migrate_server
    from dango.platform.cloud.provisioning import (
        get_region_info,
        get_size_tier,
    )
    from dango.platform.cloud.resize import validate_size_slug

    cloud_cfg, project_root = _require_cloud_deployment(ctx)

    if cloud_cfg.provider == "byos":
        console.print("[red]Error:[/red] Migration is not available for BYOS deployments.")
        raise SystemExit(1)

    # Validate size
    try:
        validate_size_slug(size)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc

    # Require Spaces
    if cloud_cfg.spaces is None:
        console.print(
            "[red]Error:[/red] Migration requires Spaces for data transfer.\n"
            "Configure Spaces first with: [bold]dango remote backup enable[/bold]"
        )
        raise SystemExit(1)

    effective_region = region or cloud_cfg.region

    # Show plan
    console.print("[bold]Migration plan:[/bold]")
    current_tier = get_size_tier(cloud_cfg.size)
    new_tier = get_size_tier(size)

    if current_tier:
        console.print(
            f"  Current: {current_tier.name} ({current_tier.slug}) — "
            f"{current_tier.vcpus} vCPU, {current_tier.ram_gb} GB RAM, "
            f"${current_tier.price_monthly}/mo"
        )
    else:
        console.print(f"  Current: {cloud_cfg.size}")

    if new_tier:
        console.print(
            f"  New:     {new_tier.name} ({new_tier.slug}) — "
            f"{new_tier.vcpus} vCPU, {new_tier.ram_gb} GB RAM, "
            f"${new_tier.price_monthly}/mo"
        )
    else:
        console.print(f"  New:     {size}")

    console.print(f"  Region:  {cloud_cfg.region} → {effective_region}")
    region_info = get_region_info(effective_region)
    if region_info:
        extra = " (GDPR)" if region_info.gdpr else ""
        console.print(f"           {region_info.city}, {region_info.country}{extra}")

    console.print(
        "\n[yellow]Warning:[/yellow] This creates a new server and destroys the old one.\n"
        "  Downtime: ~5-15 minutes."
    )

    if cloud_cfg.domain:
        console.print(
            f"\n[yellow]Note:[/yellow] Domain [bold]{cloud_cfg.domain}[/bold] will be "
            "configured on the new server. Update your DNS A record to the new IP."
        )

    if not yes:
        if not click.confirm("\nProceed with migration?"):
            console.print("[yellow]Migration cancelled.[/yellow]")
            return

    # Connect to old server
    client = _make_client()
    old_ssh = _connect_ssh(cloud_cfg, project_root)

    try:
        with Status("[bold blue]Migrating...", console=console) as status:

            def _on_progress(step: str, step_status: str) -> None:
                if step_status == "running":
                    labels: dict[str, str] = {
                        "backup": "Creating backup on old server...",
                        "upload_spaces": "Uploading backup to Spaces...",
                        "provision": "Provisioning new server...",
                        "setup_server": "Setting up new server...",
                        "copy_secrets": "Copying secrets...",
                        "download_spaces": "Downloading backup on new server...",
                        "restore": "Restoring data on new server...",
                        "domain": "Configuring domain...",
                        "firewall": "Updating firewall...",
                        "verify_health": "Verifying health...",
                        "cleanup": "Cleaning up old server...",
                    }
                    status.update(f"[bold blue]{labels.get(step, step)}")

            result = migrate_server(
                client,
                old_ssh,
                cloud_cfg,
                size,
                effective_region,
                project_root=project_root,
                on_progress=_on_progress,
            )

        # Show results
        console.print()
        if result.old_droplet_destroyed:
            console.print("[green]Migration complete.[/green]")
        else:
            console.print("[yellow]Migration complete with warnings.[/yellow]")

        console.print(f"  New droplet: {result.new_droplet_id} ({result.new_droplet_ip})")
        console.print(f"  Region: {result.new_region}")
        console.print(f"  Size: {result.new_size}")
        console.print(f"  Duration: {result.duration_seconds}s")

        if result.dns_updated and cloud_cfg.domain:
            console.print(
                f"\n[yellow]Action required:[/yellow] Update DNS A record for "
                f"[bold]{cloud_cfg.domain}[/bold] to [bold]{result.new_droplet_ip}[/bold]"
            )

        for warning in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {warning}")

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] Migration failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        old_ssh.disconnect()
