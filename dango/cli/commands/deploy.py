"""dango/cli/commands/deploy.py

Deploy group and destroy command for Dango cloud deployments.

Command hierarchy::

    dango deploy             — Interactive deployment wizard (default)
    dango deploy --reconnect — Reconnect to existing server
    dango deploy destroy     — Tear down cloud infrastructure
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import click

from dango.cli import console

# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option("--non-interactive", is_flag=True, help="Non-interactive mode (requires flags/env).")
@click.option("--reconnect", is_flag=True, help="Reconnect to an existing server.")
@click.option("--ip", type=str, default=None, help="Server IP for --reconnect.")
@click.option("--region", type=str, default=None, help="DO region slug.")
@click.option("--size", type=str, default=None, help="Droplet size slug.")
@click.option("--domain", type=str, default=None, help="Custom domain for HTTPS.")
@click.option("--admin-email", type=str, default=None, help="Admin user email.")
@click.option(
    "--admin-password", type=str, default=None, help="Admin password (or DANGO_ADMIN_PASSWORD env)."
)
@click.option("--skip-backups", is_flag=True, help="Skip automated backup setup.")
@click.option("--skip-initial-sync", is_flag=True, help="Skip initial data sync.")
@click.pass_context
def deploy(  # noqa: PLR0913
    ctx: click.Context,
    non_interactive: bool,
    reconnect: bool,
    ip: str | None,
    region: str | None,
    size: str | None,
    domain: str | None,
    admin_email: str | None,
    admin_password: str | None,
    skip_backups: bool,
    skip_initial_sync: bool,
) -> None:
    """Deploy and manage Dango cloud infrastructure.

    Run without a subcommand to start the interactive deployment wizard.

    \b
    Examples:
      dango deploy                     Interactive wizard
      dango deploy --non-interactive   All params via flags/env
      dango deploy --reconnect --ip X  Reconnect to existing server
      dango deploy destroy             Tear down cloud infrastructure
    """
    ctx.ensure_object(dict)

    if ctx.invoked_subcommand is not None:
        return

    from dango.cli.utils import require_project_context

    project_root: Path = require_project_context(ctx)

    # --- Existing deployment guard ---
    if not reconnect:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root)
        cloud_cfg = loader.load_cloud_config()
        if cloud_cfg is not None and cloud_cfg.droplet_id is not None:
            console.print(
                "[yellow]A cloud deployment already exists.[/yellow] "
                "Did you mean [bold]dango remote push[/bold]?"
            )
            console.print("  To destroy and redeploy: [bold]dango deploy destroy[/bold] first.")
            raise SystemExit(1)

    # --- Reconnect mode ---
    if reconnect:
        _handle_reconnect(project_root, ip)
        return

    # --- Wizard or non-interactive ---
    from dango.cli.commands.deploy_provision import run_provisioning
    from dango.cli.commands.deploy_wizard import run_non_interactive, run_wizard

    if non_interactive:
        config = run_non_interactive(
            project_root,
            region=region,
            size=size,
            domain=domain,
            admin_email=admin_email,
            admin_password=admin_password,
            skip_backups=skip_backups,
            skip_initial_sync=skip_initial_sync,
        )
    else:
        config = run_wizard(project_root)

    result = run_provisioning(project_root, config)

    # --- Success output ---
    console.print("\n[bold green]Deployment complete![/bold green]")
    console.print(f"  URL:    {result.url}")
    console.print(f"  IP:     {result.droplet_ip}")
    console.print(f"  Admin:  {config.admin_email}")
    if result.warnings:
        for w in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {w}")
    console.print("\n  Next: Visit the URL above and log in with your admin credentials.")
    if not config.skip_initial_sync:
        console.print("  Initial data sync is running in the background.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _handle_reconnect(project_root: Path, ip: str | None) -> None:
    """Reconnect to an existing Dango server and write cloud.yml."""
    from dango.config.loader import ConfigLoader
    from dango.config.models import CloudConfig
    from dango.platform.cloud.ssh import SSHManager

    if not ip:
        console.print("[red]Error:[/red] --ip is required with --reconnect.")
        raise SystemExit(1)

    key_path = project_root / ".dango" / "cloud_key"
    if not key_path.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {key_path}")
        console.print("  Cannot reconnect without the original SSH key.")
        raise SystemExit(1)

    ssh = SSHManager(key_path=key_path)
    try:
        ssh.connect(ip, username="root")
    except Exception as exc:
        console.print(f"[red]Error:[/red] SSH connection failed: {exc}")
        raise SystemExit(1) from exc

    try:
        result = ssh.exec_command("cat /srv/dango/project/.dango/project.yml 2>/dev/null | head -5")
        if not result.success or not result.stdout.strip():
            console.print("[red]Error:[/red] Server does not appear to be a Dango deployment.")
            raise SystemExit(1)
    finally:
        ssh.disconnect()

    # Write minimal cloud.yml
    loader = ConfigLoader(project_root)
    config = CloudConfig(droplet_ip=ip)
    loader.save_cloud_config(config)

    console.print(f"[green]Reconnected[/green] to server at {ip}.")
    console.print("  Run [bold]dango remote status[/bold] for details.")


def _load_deploy_config(ctx: click.Context) -> tuple[Any, Path]:
    """Load CloudConfig for deploy commands, return (config, project_root).

    Raises:
        SystemExit: If no cloud deployment found.
    """
    from dango.cli.utils import require_project_context
    from dango.config.loader import ConfigLoader

    project_root: Path = require_project_context(ctx)
    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()

    if cloud_cfg is None or cloud_cfg.droplet_id is None:
        console.print("[red]Error:[/red] No cloud deployment found. Nothing to destroy.")
        raise SystemExit(1)

    return cloud_cfg, project_root


def _compute_ssh_fingerprint(public_key_path: Path) -> str | None:
    """Compute MD5 fingerprint of an SSH public key (DO format).

    Returns colon-separated hex digest or None if the key cannot be read.
    """
    try:
        import base64

        content = public_key_path.read_text().strip()
        parts = content.split()
        if len(parts) < 2:
            return None
        key_data = base64.b64decode(parts[1])
        digest = hashlib.md5(key_data).hexdigest()  # noqa: S324 — MD5 for fingerprint matching only
        return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# destroy command
# ---------------------------------------------------------------------------


@deploy.command("destroy")
@click.option("--force", is_flag=True, help="Skip confirmation and backup prompts.")
@click.option("--keep-spaces", is_flag=True, help="Keep the Spaces bucket and its contents.")
@click.option("--keep-ssh-key", is_flag=True, help="Keep the SSH key on DigitalOcean.")
@click.pass_context
def deploy_destroy(
    ctx: click.Context,
    force: bool,
    keep_spaces: bool,
    keep_ssh_key: bool,
) -> None:
    """Tear down all cloud infrastructure for this project.

    Deletes the Droplet, firewall, SSH key (from DO), and Spaces bucket.
    Local SSH keys and project files are never deleted.

    Use --force to skip confirmation prompts.  Use --keep-spaces or
    --keep-ssh-key to preserve specific resources.

    Examples:
      dango deploy destroy
      dango deploy destroy --force
      dango deploy destroy --keep-spaces --keep-ssh-key
    """
    from rich.panel import Panel

    cloud_cfg, project_root = _load_deploy_config(ctx)

    # --- Show destruction summary ---
    summary_lines = [
        f"  Droplet: [bold]{cloud_cfg.droplet_id}[/bold] at {cloud_cfg.droplet_ip or 'unknown'}"
        f" ({cloud_cfg.size}, {cloud_cfg.region})",
    ]
    if cloud_cfg.firewall_id:
        summary_lines.append(f"  Firewall: {cloud_cfg.firewall_id}")

    ssh_action = "keep" if keep_ssh_key else "delete"
    summary_lines.append(f"  SSH key (DO): will {ssh_action}")

    if cloud_cfg.spaces:
        spaces_action = "keep" if keep_spaces else "delete (all contents + bucket)"
        summary_lines.append(f"  Spaces bucket: {cloud_cfg.spaces.bucket} — will {spaces_action}")

    console.print(
        Panel(
            "\n".join(summary_lines),
            title="[red]Destruction Summary[/red]",
            border_style="red",
        )
    )

    # --- Offer backup download (unless --force) ---
    if not force and cloud_cfg.droplet_ip:
        _offer_backup_download(cloud_cfg, project_root)

    # --- Type-to-confirm (unless --force) ---
    if not force:
        ip = cloud_cfg.droplet_ip or str(cloud_cfg.droplet_id)
        confirm = click.prompt(
            f"\nType the droplet IP ({ip}) to confirm destruction",
            default="",
            show_default=False,
        )
        if confirm != ip:
            console.print("[yellow]Aborted.[/yellow] Input did not match.")
            raise SystemExit(1)

    # --- Delete resources ---
    from dango.platform.cloud.digitalocean import DigitalOceanClient

    client = DigitalOceanClient()
    errors: list[str] = []

    # 1. Delete droplet
    try:
        client.delete_droplet(cloud_cfg.droplet_id)
        console.print("[green]Deleted[/green] droplet.")
    except Exception as exc:
        errors.append(f"Droplet: {exc}")
        console.print(f"[red]Failed[/red] to delete droplet: {exc}")

    # 2. Delete firewall
    if cloud_cfg.firewall_id:
        try:
            client.delete_firewall(cloud_cfg.firewall_id)
            console.print("[green]Deleted[/green] firewall.")
        except Exception as exc:
            errors.append(f"Firewall: {exc}")
            console.print(f"[red]Failed[/red] to delete firewall: {exc}")

    # 3. Delete SSH key from DO
    if not keep_ssh_key:
        _delete_ssh_key(client, cloud_cfg, project_root, errors)

    # 4. Delete Spaces bucket
    if cloud_cfg.spaces and not keep_spaces:
        _delete_spaces_bucket(cloud_cfg, errors)

    # --- Clean local config ---
    cloud_yml = project_root / ".dango" / "cloud.yml"
    if cloud_yml.exists():
        try:
            cloud_yml.unlink()
            console.print("[green]Removed[/green] .dango/cloud.yml")
        except OSError as exc:
            errors.append(f"Config cleanup: {exc}")

    # --- Report results ---
    key_path = _resolve_key_path(cloud_cfg, project_root)
    if key_path.exists():
        console.print(f"\n[dim]Local SSH key kept at: {key_path}[/dim]")

    if errors:
        console.print(f"\n[yellow]Completed with {len(errors)} error(s):[/yellow]")
        for err in errors:
            console.print(f"  - {err}")
    else:
        console.print("\n[green]All cloud resources destroyed successfully.[/green]")


def _resolve_key_path(cloud_cfg: Any, project_root: Path) -> Path:
    """Resolve SSH key path from config."""
    from dango.cli.commands.remote_mgmt import _resolve_ssh_key_path

    return _resolve_ssh_key_path(cloud_cfg, project_root)


def _offer_backup_download(cloud_cfg: Any, project_root: Path) -> None:
    """Offer to download the latest backup before destroying."""
    from dango.platform.cloud.ssh import SSHManager

    key_path = _resolve_key_path(cloud_cfg, project_root)
    if not key_path.exists():
        return

    ssh = SSHManager(
        key_path=key_path,
        known_hosts_path=key_path.parent / "known_hosts",
    )

    try:
        ssh.connect(cloud_cfg.droplet_ip)
    except Exception:  # noqa: BLE001
        console.print("[yellow]Warning:[/yellow] Could not connect to server to check for backups.")
        return

    try:
        result = ssh.exec_command("ls -t /srv/dango/backups/deploy/ 2>/dev/null | head -1")
        if not result.success or not result.stdout.strip():
            console.print(
                "[yellow]Warning:[/yellow] No backups found on server. "
                "Consider backing up your data before proceeding."
            )
            return

        latest_backup = result.stdout.strip()
        if "/" in latest_backup or "\\" in latest_backup or not latest_backup:
            console.print(
                "[yellow]Warning:[/yellow] Unexpected backup filename — skipping download."
            )
            return
        if click.confirm(f"Download latest backup ({latest_backup}) before destroying?"):
            remote_path = f"/srv/dango/backups/deploy/{latest_backup}"
            local_path = project_root / f"dango-backup-{latest_backup}"
            try:
                ssh.download_file(remote_path, local_path)
                console.print(f"[green]Downloaded[/green] backup to {local_path}")
            except Exception as exc:
                console.print(f"[yellow]Warning:[/yellow] Failed to download backup: {exc}")
    finally:
        ssh.disconnect()


def _delete_ssh_key(
    client: Any,
    cloud_cfg: Any,
    project_root: Path,
    errors: list[str],
) -> None:
    """Delete SSH key from DigitalOcean."""
    if cloud_cfg.ssh_key_id:
        try:
            client.delete_ssh_key(cloud_cfg.ssh_key_id)
            console.print("[green]Deleted[/green] SSH key from DigitalOcean.")
        except Exception as exc:
            errors.append(f"SSH key: {exc}")
            console.print(f"[red]Failed[/red] to delete SSH key: {exc}")
        return

    # Fallback: find key by fingerprint
    key_path = _resolve_key_path(cloud_cfg, project_root)
    pub_path = Path(f"{key_path}.pub")
    if not pub_path.exists():
        console.print("[dim]No SSH key ID or public key found — skipping key deletion.[/dim]")
        return

    fingerprint = _compute_ssh_fingerprint(pub_path)
    if not fingerprint:
        console.print("[dim]Could not compute SSH key fingerprint — skipping key deletion.[/dim]")
        return

    try:
        keys = client.list_ssh_keys()
        for key in keys:
            if key.get("fingerprint") == fingerprint:
                client.delete_ssh_key(key["id"])
                console.print(
                    "[green]Deleted[/green] SSH key from DigitalOcean (matched by fingerprint)."
                )
                return
        console.print(
            "[dim]SSH key not found on DigitalOcean — may have been deleted already.[/dim]"
        )
    except Exception as exc:
        errors.append(f"SSH key: {exc}")
        console.print(f"[red]Failed[/red] to delete SSH key: {exc}")


def _delete_spaces_bucket(cloud_cfg: Any, errors: list[str]) -> None:
    """Delete all objects in the Spaces bucket, then delete the bucket."""
    from dango.platform.cloud.spaces import SpacesClient

    spaces_cfg = cloud_cfg.spaces
    try:
        spaces = SpacesClient(
            bucket=spaces_cfg.bucket,
            region=spaces_cfg.region or cloud_cfg.region,
            access_key_env=spaces_cfg.access_key_env,
            secret_key_env=spaces_cfg.secret_key_env,
        )

        objects = spaces.list_objects()
        if objects:
            console.print(f"  Deleting {len(objects)} object(s) from Spaces...")
            for obj in objects:
                spaces.delete(obj["Key"])

        spaces.delete_bucket()
        console.print("[green]Deleted[/green] Spaces bucket.")
    except Exception as exc:
        errors.append(f"Spaces: {exc}")
        console.print(f"[red]Failed[/red] to delete Spaces bucket: {exc}")
