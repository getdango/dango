"""dango/cli/commands/remote_mgmt.py

Remote server management commands: status, logs, ssh, query.

These commands are registered on the ``remote`` group defined in
``remote.py`` via ``@remote.command()`` decorators.  The parent module
triggers registration by importing this module at the bottom of ``remote.py``.
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import click

from dango.cli import console
from dango.cli.commands.remote import remote

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_cloud_config_with_ip(ctx: click.Context) -> tuple[Any, Path]:
    """Load CloudConfig requiring droplet_ip, return (config, project_root).

    Raises:
        SystemExit: If no deployment or no droplet IP is configured.
    """
    from dango.cli.utils import require_project_context
    from dango.config.loader import ConfigLoader

    project_root: Path = require_project_context(ctx)
    loader = ConfigLoader(project_root)
    cloud_cfg = loader.load_cloud_config()

    if cloud_cfg is None or cloud_cfg.droplet_ip is None:
        console.print(
            "[red]Error:[/red] No cloud deployment found. "
            "Run [bold]dango deploy[/bold] to provision a server first."
        )
        raise SystemExit(1)

    return cloud_cfg, project_root


def _make_ssh_manager(cloud_cfg: Any, project_root: Path) -> Any:
    """Create SSHManager from CloudConfig, resolving ssh_key_path."""
    from dango.platform.cloud.ssh import SSHManager

    key_path = _resolve_ssh_key_path(cloud_cfg, project_root)
    return SSHManager(
        key_path=key_path,
        known_hosts_path=key_path.parent / "known_hosts",
    )


def _resolve_ssh_key_path(cloud_cfg: Any, project_root: Path) -> Path:
    """Resolve SSH key path from CloudConfig relative to project root."""
    key_path = Path(cloud_cfg.ssh_key_path)
    if not key_path.is_absolute():
        key_path = project_root / key_path
    return key_path


def _format_bytes(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


@remote.command("status")
@click.pass_context
def remote_status(ctx: click.Context) -> None:
    """Show server status, resource usage, and deployment info.

    Connects to the remote server via SSH and collects CPU, RAM, disk,
    service status, DuckDB size, sync history, and version information.

    Example:
      dango remote status
    """
    from rich.panel import Panel
    from rich.table import Table

    from dango.platform.cloud.provisioning import SIZE_TIERS
    from dango.platform.cloud.server_status import (
        check_latest_pypi_version,
        collect_server_status,
    )

    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)

    ssh = _make_ssh_manager(cloud_cfg, project_root)
    try:
        ssh.connect(cloud_cfg.droplet_ip)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to connect to server: {exc}")
        raise SystemExit(1) from exc

    try:
        status = collect_server_status(ssh, cloud_cfg)
    finally:
        ssh.disconnect()

    latest_version = check_latest_pypi_version()

    # --- Server info ---
    tier_info = ""
    for tier in SIZE_TIERS:
        if tier.slug == cloud_cfg.size:
            tier_info = f" ({tier.name}, ${tier.price_monthly}/mo)"
            break

    server_lines = [
        f"  IP: [bold]{cloud_cfg.droplet_ip}[/bold]",
        f"  Region: {cloud_cfg.region}",
        f"  Size: {cloud_cfg.size}{tier_info}",
    ]
    if cloud_cfg.domain:
        server_lines.append(f"  Domain: {cloud_cfg.domain}")
    console.print(Panel("\n".join(server_lines), title="Server", border_style="blue"))

    # --- Services ---
    if status.services:
        svc_table = Table(show_header=True, header_style="bold cyan", box=None)
        svc_table.add_column("Service", width=15)
        svc_table.add_column("Status", width=15)
        for svc in status.services:
            style = "green" if svc.status in ("running", "active") else "red"
            svc_table.add_row(svc.name, f"[{style}]{svc.status}[/{style}]")
        console.print(Panel(svc_table, title="Services", border_style="blue"))

    # --- Resources ---
    resource_lines = []
    if status.cpu_usage_pct is not None:
        resource_lines.append(f"  CPU: {status.cpu_usage_pct}%")
    if status.ram_total_mb and status.ram_used_mb is not None:
        ram_pct = round(100 * status.ram_used_mb / status.ram_total_mb, 1)
        resource_lines.append(
            f"  RAM: {status.ram_used_mb} MB / {status.ram_total_mb} MB ({ram_pct}%)"
        )
    if status.disk_total_mb and status.disk_used_mb is not None:
        disk_pct = round(100 * status.disk_used_mb / status.disk_total_mb, 1)
        avail = f", {status.disk_available_mb} MB free" if status.disk_available_mb else ""
        resource_lines.append(
            f"  Disk: {status.disk_used_mb} MB / {status.disk_total_mb} MB ({disk_pct}%){avail}"
        )
    if resource_lines:
        console.print(Panel("\n".join(resource_lines), title="Resources", border_style="blue"))

    # --- Data ---
    data_lines = []
    if status.duckdb_size_bytes is not None:
        data_lines.append(f"  DuckDB: {_format_bytes(status.duckdb_size_bytes)}")
    if status.last_sync_per_source:
        data_lines.append("  Last sync:")
        for source, ts in sorted(status.last_sync_per_source.items()):
            data_lines.append(f"    {source}: {ts}")
    else:
        data_lines.append("  Last sync: N/A")
    if data_lines:
        console.print(Panel("\n".join(data_lines), title="Data", border_style="blue"))

    # --- Versions ---
    version_lines = []
    installed = status.dango_version or "N/A"
    version_lines.append(f"  Installed: {installed}")
    if latest_version:
        version_lines.append(f"  Latest (PyPI): {latest_version}")
        if status.dango_version and status.dango_version != latest_version:
            version_lines.append("  [yellow]Update available[/yellow]")
    console.print(Panel("\n".join(version_lines), title="Versions", border_style="blue"))

    # --- Backup ---
    if status.last_backup:
        console.print(
            Panel(f"  Last backup: {status.last_backup}", title="Backup", border_style="blue")
        )
    else:
        console.print(
            Panel("  [yellow]No backups found[/yellow]", title="Backup", border_style="yellow")
        )


# ---------------------------------------------------------------------------
# logs command
# ---------------------------------------------------------------------------

_LOG_COMMANDS: dict[str, str] = {
    "dango": "journalctl -u dango-web --no-pager",
    "caddy": "journalctl -u caddy --no-pager",
    "metabase": "docker logs metabase",
}


@remote.command("logs")
@click.option(
    "--service",
    type=click.Choice(["dango", "caddy", "metabase"]),
    default="dango",
    help="Service to view logs for.",
)
@click.option("--tail", "tail_n", type=int, default=50, help="Number of log lines to show.")
@click.option("--follow", "-f", is_flag=True, help="Stream logs in real-time.")
@click.pass_context
def remote_logs(
    ctx: click.Context,
    service: str,
    tail_n: int,
    follow: bool,
) -> None:
    """View logs from a remote service.

    Connects via SSH and retrieves logs from the specified service.
    Use --follow/-f to stream logs in real-time (Ctrl+C to stop).

    Examples:
      dango remote logs
      dango remote logs --service caddy --tail 20
      dango remote logs -f
    """
    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)
    ssh = _make_ssh_manager(cloud_cfg, project_root)

    try:
        ssh.connect(cloud_cfg.droplet_ip)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to connect to server: {exc}")
        raise SystemExit(1) from exc

    base_cmd = _LOG_COMMANDS[service]

    if service == "metabase":
        cmd = f"{base_cmd} --tail {tail_n}"
        if follow:
            cmd += " -f"
    else:
        cmd = f"{base_cmd} -n {tail_n}"
        if follow:
            cmd += " -f"

    if follow:
        try:
            _stream_ssh_command(ssh, cmd)
        finally:
            ssh.disconnect()
    else:
        try:
            result = ssh.exec_command(cmd)
            if result.stdout:
                console.print(result.stdout)
            if result.stderr:
                console.print(f"[dim]{result.stderr}[/dim]")
        except Exception as exc:
            console.print(f"[red]Error:[/red] Failed to retrieve logs: {exc}")
            raise SystemExit(1) from exc
        finally:
            ssh.disconnect()


def _stream_ssh_command(ssh: Any, command: str) -> None:
    """Stream SSH command output until Ctrl+C or EOF."""
    transport = ssh.get_transport()
    channel = None
    try:
        channel = transport.open_session()
        channel.exec_command(command)
        while not channel.exit_status_ready():
            if channel.recv_ready():
                sys.stdout.buffer.write(channel.recv(4096))
                sys.stdout.buffer.flush()
            elif channel.recv_stderr_ready():
                sys.stderr.buffer.write(channel.recv_stderr(4096))
                sys.stderr.buffer.flush()
            else:
                time.sleep(0.1)
        # Drain remaining output
        while channel.recv_ready():
            sys.stdout.buffer.write(channel.recv(4096))
        while channel.recv_stderr_ready():
            sys.stderr.buffer.write(channel.recv_stderr(4096))
        sys.stdout.buffer.flush()
        sys.stderr.buffer.flush()
    except KeyboardInterrupt:
        console.print("\n[dim]Log streaming stopped.[/dim]")
    finally:
        if channel is not None:
            channel.close()


# ---------------------------------------------------------------------------
# ssh command
# ---------------------------------------------------------------------------


@remote.command("ssh")
@click.pass_context
def remote_ssh(ctx: click.Context) -> None:
    """Open an interactive SSH session to the remote server.

    Replaces the current process with an SSH connection, providing full
    TTY support including tab completion and escape sequences.

    Example:
      dango remote ssh
    """
    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)

    key_path = _resolve_ssh_key_path(cloud_cfg, project_root)
    known_hosts = key_path.parent / "known_hosts"

    if not key_path.exists():
        console.print(
            f"[red]Error:[/red] SSH key not found at [bold]{key_path}[/bold]. "
            "Re-provision or check your cloud config."
        )
        raise SystemExit(1)

    args = [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        f"root@{cloud_cfg.droplet_ip}",
    ]

    try:
        os.execvp("ssh", args)
    except FileNotFoundError:
        console.print(
            "[red]Error:[/red] SSH client not found. "
            "Ensure [bold]ssh[/bold] is installed and on your PATH."
        )
        raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# query command
# ---------------------------------------------------------------------------

_DUCKDB_PATH = "/srv/dango/project/data/warehouse.duckdb"
_VENV_PYTHON = "/srv/dango/venv/bin/python3"


@remote.command("query")
@click.argument("sql")
@click.option("--timeout", default=60, type=int, help="Query timeout in seconds.")
@click.pass_context
def remote_query(ctx: click.Context, sql: str, timeout: int) -> None:
    """Run a read-only SQL query against the remote DuckDB database.

    The query runs inside the server's Python venv with DuckDB opened in
    read-only mode, so INSERT/UPDATE/DELETE are rejected.

    Examples:
      dango remote query "SELECT count(*) FROM information_schema.tables"
      dango remote query "SELECT * FROM raw.my_table LIMIT 10" --timeout 120
    """
    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)
    ssh = _make_ssh_manager(cloud_cfg, project_root)

    try:
        ssh.connect(cloud_cfg.droplet_ip)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to connect to server: {exc}")
        raise SystemExit(1) from exc

    # SQL is passed as a shell-quoted argv argument to Python on the remote
    # server. shlex.quote handles local quoting; SSH passes through one
    # additional shell layer, but since the quoted argument contains no
    # unquoted metacharacters this is safe for standard SQL input.
    # DuckDB's read_only=True provides defense-in-depth against writes.
    escaped_sql = shlex.quote(sql)
    cmd = (
        f"{_VENV_PYTHON} -c "
        f"'import duckdb,sys; "
        f'db=duckdb.connect("{_DUCKDB_PATH}",read_only=True); '
        f"r=db.sql(sys.argv[1]); "
        f'r.show() if r.description else print("OK")\' '
        f"{escaped_sql}"
    )

    try:
        result = ssh.exec_command(cmd, timeout=timeout, check=False)
        if result.success:
            if result.stdout:
                console.print(result.stdout.rstrip())
        else:
            if result.stderr:
                console.print(f"[red]Error:[/red] {result.stderr.strip()}")
            else:
                console.print("[red]Error:[/red] Query failed with no error output.")
            raise SystemExit(1)
    except Exception as exc:
        if isinstance(exc, SystemExit):
            raise
        console.print(f"[red]Error:[/red] Query execution failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()
