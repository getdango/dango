"""dango/cli/commands/remote.py

Remote server management commands for Dango cloud deployments.

Command hierarchy::

    dango remote (group)
    ├── status              — Show server status and resource usage
    ├── logs                — View service logs (with optional streaming)
    ├── ssh                 — Open interactive SSH session
    ├── query               — Run read-only SQL against remote DuckDB
    ├── push                — Push local files and rebuild
    ├── rollback            — Restore from a backup
    ├── env (subgroup)
    │   ├── set K=V         — Set an environment variable
    │   ├── get K           — Display a variable (masked)
    │   ├── list            — List all variables (masked)
    │   └── delete K        — Remove a variable
    ├── firewall (subgroup)
    │   ├── list            — Show current firewall rules
    │   ├── allow-ip        — Restrict ports 80/443 to a specific IP
    │   └── allow-all       — Revert ports 80/443 to public access
    ├── domain (subgroup)
    │   ├── set             — Configure HTTPS with Let's Encrypt
    │   └── remove          — Revert to IP-only HTTP
    └── backup (subgroup)   — See remote_backup.py

All commands require an active cloud deployment (``droplet_id`` set in
``.dango/cloud.yml``).  Firewall commands additionally require ``firewall_id``.

Management commands (status, logs, ssh, query) are defined in
``remote_mgmt.py`` and registered on the ``remote`` group via import.
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
      dango remote status                  Show server status
      dango remote logs                    View service logs
      dango remote ssh                     Open interactive SSH session
      dango remote query "SQL"             Run read-only SQL query
      dango remote push                    Push local files and rebuild
      dango remote rollback                Restore from a backup
      dango remote firewall list           Show current firewall rules
      dango remote firewall allow-ip       Restrict web to a specific IP
      dango remote firewall allow-all      Revert web access to public
      dango remote domain set DOMAIN       Configure HTTPS with Let's Encrypt
      dango remote domain remove           Revert to IP-only HTTP
      dango remote backup                  On-demand backup
      dango remote backup list             List backups
      dango remote backup enable           Enable scheduled backups
      dango remote backup disable          Disable scheduled backups
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
# Internal helpers (firewall-specific)
# ---------------------------------------------------------------------------


def _require_cloud_deployment(ctx: click.Context) -> tuple[Any, Path]:
    """Load CloudConfig and return (config, project_root), or exit with an error.

    Only requires ``droplet_id`` — does NOT check for ``firewall_id``.

    Returns:
        Tuple of (CloudConfig, project_root Path).

    Raises:
        SystemExit: If no cloud deployment is configured.
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

    return cloud_cfg, project_root


def _load_cloud_config_or_fail(ctx: click.Context) -> tuple[Any, str]:
    """Load CloudConfig and return (config, firewall_id), or exit with an error.

    Requires both ``droplet_id`` and ``firewall_id``.

    Returns:
        Tuple of (CloudConfig, firewall_id string).

    Raises:
        SystemExit: If no deployment or no firewall is configured.
    """
    cloud_cfg, _project_root = _require_cloud_deployment(ctx)

    if cloud_cfg.firewall_id is None:
        console.print(
            "[red]Error:[/red] No firewall configured for this deployment. "
            "Re-provision or manually set [bold]firewall_id[/bold] in "
            "[bold].dango/cloud.yml[/bold]."
        )
        raise SystemExit(1)

    return cloud_cfg, cloud_cfg.firewall_id


def _load_cloud_config_with_ssh_or_fail(ctx: click.Context) -> tuple[Any, Any]:
    """Load CloudConfig and return a connected SSHManager.  Caller must close SSH."""
    from dango.cli.utils import load_cloud_config_with_ssh

    return load_cloud_config_with_ssh(ctx)


def _ssh_connect_or_fail(ctx: click.Context) -> tuple[Any, Any, Path]:
    """Load cloud config, connect SSH as ``dango`` user, return context.

    Unlike ``_load_cloud_config_with_ssh_or_fail`` (which connects as root
    for system operations), this connects as the ``dango`` service user
    for project-level file operations (e.g. ``.env`` management).

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

    if cloud_cfg is None or cloud_cfg.droplet_id is None or cloud_cfg.droplet_ip is None:
        console.print(
            "[red]Error:[/red] No cloud deployment found. "
            "Run [bold]dango deploy[/bold] to provision a server first."
        )
        raise SystemExit(1)

    key_path = project_root / cloud_cfg.ssh_key_path
    ssh = SSHManager(key_path=key_path)
    try:
        ssh.connect(cloud_cfg.droplet_ip, username="dango")
    except Exception as exc:
        console.print(f"[red]Error:[/red] SSH connection failed: {exc}")
        raise SystemExit(1) from exc

    return cloud_cfg, ssh, project_root


def _make_client() -> Any:
    """Create and return a DigitalOceanClient instance."""
    from dango.platform.cloud.digitalocean import DigitalOceanClient

    return DigitalOceanClient()


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@remote.command("rollback")
@click.option(
    "--backup",
    default=None,
    help="Path to a specific backup archive. Defaults to the most recent.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remote_rollback(ctx: click.Context, backup: str | None, yes: bool) -> None:
    """Restore the remote server from a backup.

    Stops services, extracts the backup archive over the project directory,
    restores Metabase data, then restarts services and verifies health.

    By default, uses the most recent backup.  Use ``--backup`` to specify
    a particular archive path on the server.

    Examples:
      dango remote rollback
      dango remote rollback --backup /srv/dango/backups/deploy/backup-20260224-143000.tar.gz
    """
    from rich.status import Status

    from dango.platform.cloud.backup import rollback

    if not yes:
        if not click.confirm(
            "This will restore the server from a backup. "
            "Current data will be overwritten. Continue?"
        ):
            console.print("[yellow]Rollback cancelled.[/yellow]")
            return

    cloud_cfg, ssh = _load_cloud_config_with_ssh_or_fail(ctx)

    try:
        with Status("[bold blue]Restoring from backup...", console=console) as status:

            def _on_progress(step: str, step_status: str) -> None:
                if step_status == "running":
                    labels = {
                        "find_backup": "Finding backup...",
                        "stop_services": "Stopping services...",
                        "read_manifest": "Reading manifest...",
                        "extract_archive": "Extracting archive...",
                        "restore_files": "Restoring files...",
                        "restore_metabase": "Restoring Metabase data...",
                        "fix_ownership": "Fixing file ownership...",
                        "start_services": "Starting services...",
                        "verify_health": "Verifying health...",
                    }
                    status.update(f"[bold blue]{labels.get(step, step)}")

            result = rollback(ssh, backup_path=backup, on_progress=_on_progress)

        console.print("\n[green]Rollback complete.[/green]")
        console.print(f"  Restored from: [bold]{result.restored_from}[/bold]")
        console.print(f"  Duration: {result.duration_seconds}s")
        if result.health_check_passed:
            console.print("  Health check: [green]passed[/green]")
        else:
            console.print("  Health check: [yellow]did not pass (services may need time)[/yellow]")
        for warning in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {warning}")
    except Exception as exc:
        console.print(f"[red]Error:[/red] Rollback failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@remote.command("push")
@click.option("--dry-run", is_flag=True, help="Show changes without applying.")
@click.option("--force", is_flag=True, help="Override an existing deploy lock.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remote_push(ctx: click.Context, dry_run: bool, force: bool, yes: bool) -> None:
    """Push local project files to the remote server and rebuild.

    Syncs config and dbt files, creates a pre-deploy backup, runs
    ``dbt compile`` and selectively rebuilds changed models.

    Use ``--dry-run`` to preview changes without applying them.

    Examples:
      dango remote push
      dango remote push --dry-run
      dango remote push --force --yes
    """
    from rich.status import Status

    from dango.platform.cloud.deployer import push_deploy

    cloud_cfg, project_root = _require_cloud_deployment(ctx)

    if cloud_cfg.droplet_ip is None:
        console.print("[red]Error:[/red] No droplet IP found in cloud.yml.")
        raise SystemExit(1)

    if not dry_run and not yes:
        if not click.confirm(
            "This will push local files to the remote server and rebuild. Continue?"
        ):
            console.print("[yellow]Push cancelled.[/yellow]")
            return

    ssh = _connect_ssh(cloud_cfg, project_root)

    try:
        with Status("[bold blue]Deploying...", console=console) as status:

            def _on_progress(step: str, step_status: str) -> None:
                if step_status == "running":
                    labels = {
                        "acquire_lock": "Acquiring deploy lock...",
                        "create_backup": "Creating pre-deploy backup...",
                        "stop_web": "Stopping web service...",
                        "sync_files": "Syncing files...",
                        "detect_changes": "Detecting changes...",
                        "upload_config": "Uploading config files...",
                        "sync_dbt": "Syncing dbt directories...",
                        "fix_ownership": "Fixing file ownership...",
                        "validate_sources": "Validating sources...",
                        "dbt_deps": "Installing dbt packages...",
                        "dbt_compile": "Compiling dbt project...",
                        "dbt_run": "Running dbt models...",
                        "start_web": "Starting web service...",
                    }
                    status.update(f"[bold blue]{labels.get(step, step)}")

            result = push_deploy(
                ssh,
                project_root,
                cloud_cfg.droplet_ip,
                dry_run=dry_run,
                force=force,
                on_progress=_on_progress,
            )

        # --- Summary ---
        if dry_run:
            console.print("\n[bold cyan]Dry-run summary:[/bold cyan]")
        else:
            console.print("\n[green]Push complete.[/green]")

        sr = result.sync_result
        console.print(f"  Files synced: {len(sr.synced_files)}")
        if sr.added_models:
            console.print(f"  New models: {', '.join(sr.added_models)}")
        if sr.changed_models:
            console.print(f"  Changed models: {', '.join(sr.changed_models)}")
        if sr.removed_models:
            console.print(f"  Removed models: {', '.join(sr.removed_models)}")
        if sr.packages_changed:
            console.print("  packages.yml: [yellow]changed[/yellow]")

        if not dry_run:
            if result.dbt_deps_run:
                console.print("  dbt deps: [green]ran[/green]")
            if result.dbt_compile_success:
                console.print("  dbt compile: [green]success[/green]")
            if result.models_rebuilt:
                console.print(f"  Models rebuilt: {', '.join(result.models_rebuilt)}")
            console.print(f"  Duration: {result.duration_seconds}s")

        for warning in result.warnings:
            console.print(f"  [yellow]Warning:[/yellow] {warning}")

    except Exception as exc:
        console.print(f"[red]Error:[/red] Push failed: {exc}")
        if not dry_run:
            console.print(
                "[dim]A pre-deploy backup may have been created. "
                "Use [bold]dango remote rollback[/bold] to check and restore.[/dim]"
            )
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


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


# domain subgroup
# ---------------------------------------------------------------------------


@remote.group("domain")
@click.pass_context
def domain(ctx: click.Context) -> None:
    """Manage the custom domain and HTTPS for this deployment.

    Commands:
      set DOMAIN   Configure HTTPS with automatic Let's Encrypt certificates
      remove       Revert to IP-only HTTP access
    """
    ctx.ensure_object(dict)


def _connect_ssh(cloud_cfg: Any, project_root: Path) -> Any:
    """Create and connect an SSHManager for the deployment."""
    from dango.platform.cloud.ssh import SSHManager

    key_path = project_root / cloud_cfg.ssh_key_path
    ssh = SSHManager(key_path=key_path)
    ssh.connect(cloud_cfg.droplet_ip, username="root")
    return ssh


# ---------------------------------------------------------------------------
# domain set
# ---------------------------------------------------------------------------


@domain.command("set")
@click.argument("domain_name")
@click.pass_context
def domain_set(ctx: click.Context, domain_name: str) -> None:
    """Configure HTTPS for DOMAIN_NAME with automatic Let's Encrypt.

    Caddy acquires and renews TLS certificates automatically.  DNS must
    point DOMAIN_NAME to the droplet IP for certificate issuance to
    succeed.  If DNS hasn't propagated yet, a warning is shown but the
    configuration is still applied (Caddy retries automatically).

    Example:
      dango remote domain set app.example.com
    """
    from dango.platform.cloud.domain import set_domain

    cloud_cfg, project_root = _require_cloud_deployment(ctx)

    if cloud_cfg.droplet_ip is None:
        console.print("[red]Error:[/red] No droplet IP found in cloud.yml.")
        raise SystemExit(1)

    ssh = _connect_ssh(cloud_cfg, project_root)
    try:
        result = set_domain(ssh, project_root, domain_name)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()

    if result["dns_ok"]:
        console.print(f"[green]DNS OK:[/green] {result['dns_message']}")
    else:
        console.print(f"[yellow]DNS warning:[/yellow] {result['dns_message']}")

    if result["caddyfile_updated"]:
        console.print(
            f"[green]HTTPS configured.[/green] "
            f"Caddy will serve [bold]{domain_name}[/bold] with automatic TLS."
        )
    else:
        console.print(f"Caddyfile already configured for [bold]{domain_name}[/bold].")


# ---------------------------------------------------------------------------
# domain remove
# ---------------------------------------------------------------------------


@domain.command("remove")
@click.pass_context
def domain_remove(ctx: click.Context) -> None:
    """Revert to IP-only HTTP access.

    Removes the domain configuration and rewrites the Caddyfile for
    plain HTTP on port 80.  The domain is cleared from cloud.yml.

    Example:
      dango remote domain remove
    """
    from dango.platform.cloud.domain import remove_domain

    cloud_cfg, project_root = _require_cloud_deployment(ctx)

    if cloud_cfg.droplet_ip is None:
        console.print("[red]Error:[/red] No droplet IP found in cloud.yml.")
        raise SystemExit(1)

    ssh = _connect_ssh(cloud_cfg, project_root)
    try:
        result = remove_domain(ssh, project_root)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()

    prev = result.get("previous_domain")
    if prev:
        console.print(f"[green]Domain removed.[/green] Was: [bold]{prev}[/bold]")
    else:
        console.print("[yellow]No domain was configured.[/yellow]")

    if result["caddyfile_updated"]:
        console.print("Caddyfile reverted to HTTP-only (port 80).")
    else:
        console.print("Caddyfile was already HTTP-only.")


# ---------------------------------------------------------------------------
# Register subgroups from separate modules
# ---------------------------------------------------------------------------

import dango.cli.commands.remote_mgmt as _remote_mgmt  # noqa: E402, F401
from dango.cli.commands.remote_backup import backup_group  # noqa: E402
from dango.cli.commands.remote_env import env  # noqa: E402

remote.add_command(backup_group)
remote.add_command(env)
