"""dango/cli/commands/remote_backup.py

Backup management subgroup for ``dango remote backup``.

Command hierarchy::

    dango remote backup                   — On-demand backup to Spaces
    dango remote backup list              — List server + Spaces backups
    dango remote backup enable            — Enable systemd backup timer
    dango remote backup disable           — Disable systemd backup timer
    dango remote backup download NAME     — Download from Spaces to local
    dango remote backup restore SOURCE    — Restore from Spaces backup

Registered as a subgroup of ``remote`` in ``remote.py`` via
``remote.add_command(backup_group)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from dango.cli import console
from dango.exceptions import format_structured_error

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_cloud_config_with_ssh_or_fail(ctx: click.Context) -> tuple[Any, Any]:
    """Load CloudConfig and return a connected SSHManager.  Caller must close SSH."""
    from dango.cli.utils import load_cloud_config_with_ssh

    return load_cloud_config_with_ssh(ctx)


def _load_spaces_client_or_fail(ctx: click.Context) -> tuple[Any, Any]:
    """Load CloudConfig and return a SpacesClient.

    Returns:
        Tuple of (CloudConfig, SpacesClient).

    Raises:
        SystemExit: If Spaces is not configured.
    """
    from dango.cli.utils import require_project_context
    from dango.config.loader import ConfigLoader
    from dango.platform.cloud.spaces import SpacesClient

    project_root: Path = require_project_context(ctx)
    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()

    if cloud_cfg is None or cloud_cfg.droplet_ip is None:
        console.print(
            "[red]Error:[/red] No cloud deployment found. "
            "Run [bold]dango deploy[/bold] to provision a server first."
        )
        raise SystemExit(1)

    if cloud_cfg.spaces is None:
        if cloud_cfg.provider == "byos":
            console.print(
                "[red]Error:[/red] Spaces backups require DigitalOcean. "
                "Use [bold]dango remote backup[/bold] (on-demand via SSH) or "
                "[bold]dango remote backup download[/bold] for BYOS deployments."
            )
        else:
            console.print(
                "[red]Error:[/red] Spaces not configured. "
                "Set [bold]spaces.bucket[/bold] in [bold].dango/cloud.yml[/bold]."
            )
        raise SystemExit(1)

    region = cloud_cfg.spaces.region or cloud_cfg.region

    try:
        client = SpacesClient(
            bucket=cloud_cfg.spaces.bucket,
            region=region,
            access_key_env=cloud_cfg.spaces.access_key_env,
            secret_key_env=cloud_cfg.spaces.secret_key_env,
        )
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to create Spaces client: {exc}")
        raise SystemExit(1) from exc

    return cloud_cfg, client


# ---------------------------------------------------------------------------
# backup group
# ---------------------------------------------------------------------------


@click.group("backup", invoke_without_command=True)
@click.pass_context
def backup_group(ctx: click.Context) -> None:
    """Manage remote server backups.

    Without a subcommand, triggers an on-demand backup on the server.

    Commands:
      list       List local and Spaces backups
      enable     Enable daily scheduled backups
      disable    Disable daily scheduled backups
      download   Download a backup from Spaces
      restore    Restore from a Spaces backup
    """
    if ctx.invoked_subcommand is not None:
        return

    # On-demand backup: run scheduled_backup module on server via SSH
    from rich.status import Status

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        with Status("[bold blue]Running on-demand backup on server...", console=console):
            result = ssh.exec_command(
                "/srv/dango/venv/bin/python -m dango.platform.cloud.scheduled_backup",
                timeout=900,
            )

        if result.success:
            console.print("[green]Backup completed successfully.[/green]")
            if result.stdout.strip():
                console.print(result.stdout.strip())
        else:
            msg = format_structured_error(
                what_failed="Remote backup failed",
                causes=[
                    "Insufficient disk space on server",
                    "SSH connection dropped",
                    "Spaces credentials invalid",
                ],
                suggested_fix="Check server disk with 'dango remote status' and verify Spaces config",
            )
            console.print(f"[red]Error:[/red]\n{msg}")
            if result.stderr.strip():
                console.print(f"\nServer output:\n{result.stderr.strip()}")
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# backup list
# ---------------------------------------------------------------------------


@backup_group.command("list")
@click.pass_context
def backup_list(ctx: click.Context) -> None:
    """List backups on the server and in Spaces.

    Shows both local server backups and remote Spaces backups in a table.

    Example:
      dango remote backup list
    """
    from rich.table import Table

    from dango.platform.cloud.backup import list_local_backups

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        # List local backups
        local_backups = list_local_backups(ssh)

        # List Spaces backups (if configured)
        spaces_backups: list[dict[str, Any]] = []
        if cloud_cfg.spaces is not None:
            try:
                from dango.platform.cloud.spaces import SpacesClient

                region = cloud_cfg.spaces.region or cloud_cfg.region
                client = SpacesClient(
                    bucket=cloud_cfg.spaces.bucket,
                    region=region,
                    access_key_env=cloud_cfg.spaces.access_key_env,
                    secret_key_env=cloud_cfg.spaces.secret_key_env,
                )
                objects = client.list_objects(prefix="backups/")
                for obj in objects:
                    key = obj.get("Key", "")
                    if key.endswith(".tar.gz"):
                        name = key.rsplit("/", 1)[-1]
                        size = obj.get("Size", 0)
                        spaces_backups.append({"name": name, "key": key, "size_bytes": size})
            except Exception:
                spaces_backups = []

        if not local_backups and not spaces_backups:
            console.print("[yellow]No backups found.[/yellow]")
            return

        table = Table(title="Backups", show_header=True, header_style="bold cyan")
        table.add_column("Source", width=10)
        table.add_column("Name")
        table.add_column("Size", justify="right")

        for b in local_backups:
            size_mb = b["size_bytes"] / (1024 * 1024) if b["size_bytes"] else 0
            table.add_row("server", b["name"], f"{size_mb:.1f} MB")

        for b in spaces_backups:
            size_mb = b["size_bytes"] / (1024 * 1024) if b["size_bytes"] else 0
            table.add_row("spaces", b["name"], f"{size_mb:.1f} MB")

        console.print(table)
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# backup enable
# ---------------------------------------------------------------------------


@backup_group.command("enable")
@click.pass_context
def backup_enable(ctx: click.Context) -> None:
    """Enable daily scheduled backups via systemd timer.

    Requires Spaces to be configured in ``.dango/cloud.yml`` and
    credentials (``SPACES_ACCESS_KEY``, ``SPACES_SECRET_KEY``) in the
    server's ``.env`` file.

    Example:
      dango remote backup enable
    """
    from dango.platform.cloud._server_templates import (
        SYSTEMD_BACKUP_SERVICE,
        SYSTEMD_BACKUP_TIMER,
    )

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        # Verify Spaces credentials exist on server
        env_check = ssh.exec_command(
            "grep -q SPACES_ACCESS_KEY /srv/dango/project/.env 2>/dev/null"
        )
        if not env_check.success:
            console.print(
                "[red]Error:[/red] SPACES_ACCESS_KEY not found in server .env file. "
                "Add Spaces credentials before enabling scheduled backups."
            )
            raise SystemExit(1)

        # Write systemd unit files
        ssh.write_remote_file(
            "/etc/systemd/system/dango-backup.service",
            SYSTEMD_BACKUP_SERVICE,
            mode=0o644,
        )
        ssh.write_remote_file(
            "/etc/systemd/system/dango-backup.timer",
            SYSTEMD_BACKUP_TIMER,
            mode=0o644,
        )

        # Enable and start timer
        result = ssh.exec_command(
            "systemctl daemon-reload && systemctl enable --now dango-backup.timer",
            timeout=30,
        )
        if not result.success:
            console.print(
                f"[red]Error:[/red] Failed to enable backup timer: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            raise SystemExit(1)

        console.print("[green]Scheduled backups enabled.[/green] Daily at 02:00 UTC.")
        console.print("  Timer: dango-backup.timer")
        console.print("  Service: dango-backup.service")
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# backup disable
# ---------------------------------------------------------------------------


@backup_group.command("disable")
@click.pass_context
def backup_disable(ctx: click.Context) -> None:
    """Disable daily scheduled backups.

    Example:
      dango remote backup disable
    """
    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        ssh.exec_command(
            "systemctl disable --now dango-backup.timer 2>/dev/null || true",
            timeout=30,
        )
        console.print("[green]Scheduled backups disabled.[/green]")
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# backup download
# ---------------------------------------------------------------------------


@backup_group.command("download")
@click.argument("name")
@click.option(
    "-o",
    "--output",
    default=None,
    type=click.Path(),
    help="Local path to save the backup. Defaults to current directory.",
)
@click.pass_context
def backup_download(ctx: click.Context, name: str, output: str | None) -> None:
    """Download a backup archive from Spaces.

    NAME is the backup filename (e.g. ``backup-20260224-143000.tar.gz``).

    Examples:
      dango remote backup download backup-20260224-143000.tar.gz
      dango remote backup download backup-20260224-143000.tar.gz -o ./my-backup.tar.gz
    """
    from rich.status import Status

    cloud_cfg, client = _load_spaces_client_or_fail(ctx)

    key = f"backups/{name}"
    if output is None:
        output = name

    output_path = Path(output)

    try:
        with Status(f"[bold blue]Downloading {name}...", console=console):
            data = client.download(key)
            output_path.write_bytes(data)

        size_mb = len(data) / (1024 * 1024)
        console.print(
            f"[green]Downloaded.[/green] Saved to [bold]{output_path}[/bold] ({size_mb:.1f} MB)"
        )
    except Exception as exc:
        msg = format_structured_error(
            what_failed=f"Download failed for {name}",
            causes=[
                "Spaces credentials expired or invalid",
                "Backup file does not exist in Spaces",
                "Network connectivity issue",
            ],
            suggested_fix="Verify Spaces config and run 'dango remote backup list' to check available backups",
        )
        console.print(f"[red]Error:[/red]\n{msg}")
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# backup restore
# ---------------------------------------------------------------------------


@backup_group.command("restore")
@click.argument("source")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def backup_restore(ctx: click.Context, source: str, yes: bool) -> None:
    """Restore the server from a Spaces backup.

    SOURCE is the backup name (e.g. ``backup-20260224-143000.tar.gz``).

    This downloads the backup to the server, then restores it.  Current
    data will be overwritten.

    Examples:
      dango remote backup restore backup-20260224-143000.tar.gz
    """
    from rich.status import Status

    if not yes:
        if not click.confirm(
            f"This will restore the server from Spaces backup '{source}'. "
            "Current data will be overwritten. Continue?"
        ):
            console.print("[yellow]Restore cancelled.[/yellow]")
            return

    # Validate source to prevent shell injection (passed to remote Python command)
    if not all(c.isalnum() or c in "-_." for c in source):
        console.print(
            "[red]Error:[/red] Invalid backup name. "
            "Use only alphanumeric characters, hyphens, underscores, and dots."
        )
        raise SystemExit(1)

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        key = f"backups/{source}"

        with Status("[bold blue]Restoring from Spaces backup...", console=console):
            # Run restore on the server
            result = ssh.exec_command(
                f'{VENV_PYTHON} -c "'
                "from dango.platform.cloud.scheduled_backup import restore_from_spaces; "
                "from dango.platform.cloud.scheduled_backup import _load_spaces_config; "
                f"restore_from_spaces(_load_spaces_config(), '{key}')\"",
                timeout=900,
            )

        if result.success:
            console.print(f"[green]Restore complete.[/green] Restored from: {source}")
        else:
            msg = format_structured_error(
                what_failed=f"Restore failed from backup '{source}'",
                causes=[
                    "Backup archive is corrupt or incomplete",
                    "Insufficient disk space on server",
                    "Services failed to restart after restore",
                ],
                suggested_fix="Check server disk with 'dango remote status' and try a different backup",
            )
            console.print(f"[red]Error:[/red]\n{msg}")
            if result.stderr.strip():
                console.print(f"\nServer output:\n{result.stderr.strip()}")
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


#: Path to venv Python on the remote server.
VENV_PYTHON = "/srv/dango/venv/bin/python"
