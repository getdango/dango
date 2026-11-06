"""dango/cli/commands/remote_ops.py

Remote operations: resize, upgrade, and the switched-off migrate command.

These commands are registered on the ``remote`` group defined in
``remote.py`` via ``@remote.command()`` decorators.  The parent module
triggers registration by importing this module at the bottom of ``remote.py``.
"""

from __future__ import annotations

import click

from dango.cli import console
from dango.cli.commands import backup_notices
from dango.cli.commands import remote_report as report
from dango.cli.commands.remote import remote
from dango.cli.commands.remote_power import refuse_if_powered_off
from dango.cli.utils import safe_confirm

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
@click.option("--force", is_flag=True, help="Allow downgrades to an older version.")
@click.pass_context
def remote_upgrade(
    ctx: click.Context,
    target_version: str | None,
    yes: bool,
    skip_backup: bool,
    force: bool,
) -> None:
    """Upgrade Dango on the remote server."""
    from rich.status import Status

    from dango.cli.commands.remote import _load_cloud_config_with_ssh_or_fail
    from dango.cli.utils import require_project_context
    from dango.platform.cloud.runtime_ownership import at_target_restart_plan
    from dango.platform.cloud.upgrade import (
        PROGRESS_LABELS,
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
            console.print(f"[red]Error:[/red] {report.literal(exc)}")
            raise SystemExit(1) from exc

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        console.print("[bold]Checking versions...[/bold]")
        current, latest = check_versions(ssh)

        display_target = target_version or latest or "unknown"
        console.print(f"  Current version: [cyan]{current or 'unknown'}[/cyan]")
        console.print(f"  Target version:  [cyan]{display_target}[/cyan]")

        if at_target := current == display_target:  # post-install steps still run, no pip
            console.print("\n[green]Already at target version — no upgrade needed.[/green]")

        if not at_target and not latest and target_version is None:
            console.print(
                "\n[red]Error:[/red] Could not determine latest version from PyPI. "
                "Specify a version with --version."
            )
            ctx.exit(1)

        # At target, only a venv rebuild restarts services: ask first (plain steps need no prompt).
        if at_target and not yes and (plan := at_target_restart_plan(ssh)):
            console.print(f"\n{plan}")
            if not safe_confirm("Rebuild the venv now?", default=False):
                console.print("[yellow]Cancelled.[/yellow] Nothing changed; use --yes to rebuild.")
                return

        # Confirm
        if not yes and not at_target:
            console.print()
            if skip_backup:
                console.print("[yellow]Warning:[/yellow] Pre-upgrade backup will be skipped.")
            if not safe_confirm("Proceed with upgrade?"):
                console.print("[yellow]Upgrade cancelled.[/yellow]")
                return

        with Status("[bold blue]Upgrading...", console=console) as status:

            def _on_progress(step: str, step_status: str) -> None:
                if step_status == "running":
                    status.update(f"[bold blue]{PROGRESS_LABELS.get(step, step)}")

            result: UpgradeResult = upgrade_dango(
                ssh,
                version=target_version,
                skip_backup=skip_backup,
                force=force,
                on_progress=_on_progress,
                local_project_root=require_project_context(ctx),
                byos=getattr(cloud_cfg, "provider", None) == "byos",
            )

        done = "Server configuration checked" if at_target else "Upgrade complete"
        report.upgrade_result(result, done)

    except SystemExit:
        raise
    except Exception as exc:
        report.failure("Upgrade failed", exc)
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
        ctx.exit(1)

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
        console.print(f"[red]Error:[/red] {report.literal(exc)}")
        raise SystemExit(1) from exc

    refuse_if_powered_off(cloud_cfg)  # an interrupted resize leaves the droplet off
    current_tier, new_tier = get_size_tier(cloud_cfg.size), get_size_tier(size)

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

    backup_notices.print_secrets_reminder(cloud_cfg, project_root)
    if not yes:
        if not safe_confirm("\nProceed with resize?"):
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
            report.line(result.backup_path, "  Backup: ")
        if result.dbt_profiles_regenerated:
            console.print("  dbt profiles.yml: regenerated")

        for warning in result.warnings:
            report.warning(warning)

    except SystemExit:
        raise
    except Exception as exc:
        report.failure("Resize failed", exc)
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# dango remote migrate
# ---------------------------------------------------------------------------


MIGRATE_OFF_MESSAGE = (
    "Error: dango remote migrate is switched off in 1.0.14.\n"
    "To change the server size, use: dango remote resize\n"
    "To move to another server or region:\n"
    "  (1) dango remote backup download --from-server -o ./backup.tar.gz\n"
    "  (2) dango deploy destroy\n"
    "  (3) dango deploy\n"
    "  (4) dango remote backup restore ./backup.tar.gz --from-local\n"
    "Then re-enter secrets (they are not in backups by default) and update DNS."
)


@remote.command(
    "migrate",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--size", default=None, help="Ignored: the command is switched off.")
@click.option("--region", default=None, help="Ignored: the command is switched off.")
@click.option("--yes", "-y", is_flag=True, help="Ignored: the command is switched off.")
@click.pass_context
def remote_migrate(
    ctx: click.Context,
    size: str | None,
    region: str | None,
    yes: bool,
) -> None:
    """Migrate is switched off in 1.0.14 (it refuses and prints the manual steps).

    Use ``dango remote resize`` to change the server size. To move servers,
    back up, destroy, redeploy and restore: the refusal message lists the steps.
    """
    # Refuse before loading config or touching the network.
    console.print(MIGRATE_OFF_MESSAGE, markup=False, highlight=False)
    ctx.exit(1)
