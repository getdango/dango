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
from dango.cli.utils import safe_confirm
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
@click.argument("name", required=False)
@click.option(
    "-o",
    "--output",
    default=None,
    type=click.Path(),
    help="Local path to save the backup. Defaults to current directory.",
)
@click.option(
    "--from-server",
    is_flag=True,
    help="Download the latest backup directly from the server via SSH instead of Spaces.",
)
@click.pass_context
def backup_download(
    ctx: click.Context, name: str | None, output: str | None, from_server: bool
) -> None:
    """Download a backup archive from Spaces or directly from the server.

    NAME is the backup filename (e.g. ``backup-20260224-143000.tar.gz``).
    Required unless ``--from-server`` is used.

    Examples:
      dango remote backup download backup-20260224-143000.tar.gz
      dango remote backup download backup-20260224-143000.tar.gz -o ./my-backup.tar.gz
      dango remote backup download --from-server -o ./latest-backup.tar.gz
    """
    from rich.status import Status

    if from_server:
        _backup_download_from_server(ctx, output)
        return

    if not name:
        console.print("[red]Error:[/red] NAME is required unless --from-server is used.")
        raise SystemExit(1)

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


def _backup_download_from_server(ctx: click.Context, output: str | None) -> None:
    """Create a backup on the server and download it via SFTP."""
    import json

    from rich.status import Status

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        with Status("[bold blue]Creating backup on server...", console=console):
            create_result = ssh.exec_command(
                f'{VENV_PYTHON} -c "'
                "from dango.platform.cloud.scheduled_backup import _create_local_archive; "
                "import json; "
                "path, manifest, warnings = _create_local_archive('on-demand'); "
                "print(json.dumps({'path': str(path), 'warnings': warnings}))\"",
                timeout=600,
            )

        if not create_result.success:
            console.print(
                f"[red]Error:[/red] Failed to create backup on server:\n"
                f"{create_result.stderr.strip() or create_result.stdout.strip()}"
            )
            raise SystemExit(1)

        try:
            info = json.loads(create_result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            console.print("[red]Error:[/red] Could not parse backup path from server response.")
            raise SystemExit(1) from None

        archive_path = info["path"]
        archive_name = archive_path.rsplit("/", 1)[-1]
        local_name = output or archive_name
        local_path = Path(local_name)

        # Warn if server disk >50%
        disk_result = ssh.exec_command("df -m /srv/dango | tail -1")
        if disk_result.success:
            parts = disk_result.stdout.split()
            if len(parts) >= 5:
                try:
                    usage_pct = int(parts[4].rstrip("%"))
                    if usage_pct > 50:
                        console.print(
                            f"[yellow]Warning:[/yellow] Server disk is {usage_pct}% full."
                        )
                except (ValueError, IndexError):
                    pass

        with Status(f"[bold blue]Downloading {archive_name}...", console=console):
            ssh.download_file(archive_path, local_path)

        size_mb = local_path.stat().st_size / (1024 * 1024)
        console.print(
            f"[green]Downloaded.[/green] Saved to [bold]{local_path}[/bold] ({size_mb:.1f} MB)"
        )

        # Show warnings
        for w in info.get("warnings", []):
            console.print(f"  [yellow]Warning:[/yellow] {w}")
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# backup restore
# ---------------------------------------------------------------------------


@backup_group.command("restore")
@click.argument("source")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option(
    "--from-local", is_flag=True, help="Restore from a local backup file instead of Spaces."
)
@click.pass_context
def backup_restore(ctx: click.Context, source: str, yes: bool, from_local: bool) -> None:
    """Restore the server from a Spaces or local backup.

    SOURCE is the backup name (e.g. ``backup-20260224-143000.tar.gz``)
    or a local file path when using ``--from-local``.

    This downloads the backup to the server, then restores it.  Current
    data will be overwritten.

    Examples:
      dango remote backup restore backup-20260224-143000.tar.gz
      dango remote backup restore ./my-backup.tar.gz --from-local
    """
    from rich.status import Status

    if from_local:
        _backup_restore_from_local(ctx, source, yes)
        return

    if not yes:
        if not safe_confirm(
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


def _backup_restore_from_local(ctx: click.Context, source: str, yes: bool) -> None:
    """Restore from a local backup file by uploading it to the server."""
    from rich.status import Status

    local_path = Path(source).resolve()
    if not local_path.is_file():
        console.print(f"[red]Error:[/red] Local file not found: {local_path}")
        raise SystemExit(1)

    if not local_path.name.endswith(".tar.gz"):
        console.print("[red]Error:[/red] Backup file must be a .tar.gz archive.")
        raise SystemExit(1)

    if not yes:
        if not safe_confirm(
            f"This will upload '{local_path.name}' and restore the server from it. "
            "Current data will be overwritten. Continue?"
        ):
            console.print("[yellow]Restore cancelled.[/yellow]")
            return

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)
    remote_tmp = f"/tmp/{local_path.name}"

    try:
        with Status(f"[bold blue]Uploading {local_path.name}...", console=console):
            ssh.upload_file(local_path, remote_tmp)

        console.print(f"[green]Uploaded.[/green] Restoring from {local_path.name}...")

        from dango.platform.cloud.backup import restore_from_archive

        with Status("[bold blue]Restoring from backup...", console=console):
            result = restore_from_archive(ssh, remote_tmp)

        if result.health_check_passed:
            console.print(f"[green]Restore complete.[/green] Restored from: {local_path.name}")
        else:
            console.print(
                f"[yellow]Restore finished[/yellow] from {local_path.name}, "
                "but health check did not pass. Check 'dango remote status'."
            )
            for w in result.warnings:
                console.print(f"  [yellow]Warning:[/yellow] {w}")
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        # Clean up remote temp file (best-effort — SSH may already be dead)
        try:
            ssh.exec_command(f"rm -f {remote_tmp}")
        except Exception:  # noqa: BLE001
            pass
        ssh.disconnect()


# ---------------------------------------------------------------------------
# backup verify-metabase
# ---------------------------------------------------------------------------


@backup_group.command("verify-metabase")
@click.argument("source", required=False)
@click.pass_context
def backup_verify_metabase(ctx: click.Context, source: str | None) -> None:
    """Verify Metabase backup integrity in a Spaces archive or on the live server.

    With SOURCE: downloads the archive from Spaces and checks for Metabase
    H2 database files without full extraction.

    Without SOURCE: checks the live Metabase /api/health endpoint via SSH.

    Examples:
      dango remote backup verify-metabase backup-20260224-143000.tar.gz
      dango remote backup verify-metabase
    """
    if source:
        # Offline mode: download archive from Spaces, check members
        _verify_metabase_archive(ctx, source)
    else:
        # Live mode: SSH to server, check /api/health
        _verify_metabase_live(ctx)


def _verify_metabase_archive(ctx: click.Context, source: str) -> None:
    """Verify Metabase H2 files exist in a Spaces backup archive."""
    import tarfile
    import tempfile

    from rich.status import Status

    cloud_cfg, client = _load_spaces_client_or_fail(ctx)
    key = f"backups/{source}"

    try:
        with Status(f"[bold blue]Checking {source} for Metabase data...", console=console):
            data = client.download(key)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Could not download from Spaces: {exc}")
        raise click.Abort() from exc

    # Save to temp file for tarfile inspection
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        has_mv = False
        has_trace = False
        with tarfile.open(tmp_path, mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.name.endswith("metabase/metabase.db.mv.db") and member.size > 0:
                    has_mv = True
                if member.name.endswith("metabase/metabase.db.trace.db") and member.size > 0:
                    has_trace = True

        if has_mv and has_trace:
            console.print(
                "[green]PASS[/green]: Archive contains Metabase H2 database files "
                "(metabase.db.mv.db + metabase.db.trace.db)."
            )
        elif has_mv:
            console.print(
                "[yellow]PARTIAL[/yellow]: Archive contains metabase.db.mv.db "
                "but is missing metabase.db.trace.db."
            )
        elif has_trace:
            console.print(
                "[yellow]PARTIAL[/yellow]: Archive contains metabase.db.trace.db "
                "but is missing metabase.db.mv.db."
            )
        else:
            console.print("[red]FAIL[/red]: Archive does not contain Metabase H2 database files.")
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _verify_metabase_live(ctx: click.Context) -> None:
    """Check Metabase health endpoint via SSH."""
    from rich.status import Status

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        with Status("[bold blue]Checking live Metabase health...", console=console):
            result = ssh.exec_command("curl -sf http://localhost:8800/api/health", timeout=15)

        if result.success:
            console.print("[green]PASS[/green]: Metabase is responding on /api/health.")
        else:
            console.print(
                "[red]FAIL[/red]: Metabase is not responding. "
                "Check 'dango remote status' for more details."
            )
    except Exception as exc:
        console.print(f"[red]Error:[/red] Could not reach server: {exc}")
        raise click.Abort() from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# backup config
# ---------------------------------------------------------------------------


@backup_group.command("config")
@click.pass_context
def backup_config(ctx: click.Context) -> None:
    """Display the current backup configuration from cloud.yml.

    Shows Spaces bucket, retention settings, and secrets inclusion status.
    If the ``backup:`` key is not configured, shows defaults.

    Example:
      dango remote backup config
    """
    from dango.cli.utils import require_project_context
    from dango.config.loader import ConfigLoader

    project_root = require_project_context(ctx)
    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()

    if cloud_cfg is None:
        console.print("[red]Error:[/red] No cloud configuration found.")
        raise click.Abort()

    spaces_bucket = cloud_cfg.spaces.bucket if cloud_cfg.spaces else "Not configured"

    console.print("[bold]Backup Configuration[/bold]")
    console.print(f"  Spaces bucket:     {spaces_bucket}")

    if cloud_cfg.backup is None:
        console.print("  [dim](backup: key not set — using defaults)[/dim]")
        console.print("  Include secrets:   No (default)")
        console.print("  On-server retention: 1 (default)")
        console.print("  Spaces retention:")
        console.print("    Daily:   7 (default)")
        console.print("    Weekly:  4 (default)")
        console.print("    Monthly: 0 (default)")
    else:
        include = "Yes" if cloud_cfg.backup.include_secrets else "No"
        console.print(f"  Include secrets:   {include}")
        console.print(f"  On-server retention: {cloud_cfg.backup.on_server_retention}")
        console.print("  Spaces retention:")
        sr = cloud_cfg.backup.spaces_retention
        console.print(f"    Daily:   {sr.daily}")
        console.print(f"    Weekly:  {sr.weekly}")
        console.print(f"    Monthly: {sr.monthly}")


#: Path to venv Python on the remote server.
VENV_PYTHON = "/srv/dango/venv/bin/python"
