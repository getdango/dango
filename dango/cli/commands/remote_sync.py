"""dango/cli/commands/remote_sync.py

Remote sync command: trigger data syncs on a cloud server via SSH.

Registered on the ``remote`` group defined in ``remote.py`` via a
``@remote.command()`` decorator.  The parent module triggers registration
by importing this module at the bottom of ``remote.py``.
"""

from __future__ import annotations

import json
import shlex

import click
from rich.status import Status

from dango.cli import console
from dango.cli.commands.remote import remote
from dango.exceptions import CloudSSHError
from dango.platform.cloud.remote_launch import build_background_launch

_VENV_PYTHON = "/srv/dango/venv/bin/python3"
_PROJECT_ROOT = "/srv/dango/project"


@remote.command("sync")
@click.argument("source")
@click.option("--full-refresh", is_flag=True, help="Run a full refresh sync.")
@click.option(
    "--backfill",
    default=None,
    help="Backfill duration (e.g. 7d, 2w, 1m).",
)
@click.option(
    "--wait",
    is_flag=True,
    help="Wait for sync to complete and show result.",
)
@click.pass_context
def remote_sync(
    ctx: click.Context,
    source: str,
    full_refresh: bool,
    backfill: str | None,
    wait: bool,
) -> None:
    """Trigger a data sync on the remote cloud server.

    Runs the sync via SSH using the server's Python venv and Dango
    installation.

    Examples:

      dango remote sync my_source

      dango remote sync my_source --full-refresh --wait

      dango remote sync my_source --backfill 7d --wait
    """
    from dango.cli.commands.remote_mgmt import (
        _load_cloud_config_with_ip,
        _make_ssh_manager,
    )
    from dango.validation import parse_backfill_duration

    # Validate backfill locally before SSH
    backfill_days: int | None = None
    if backfill is not None:
        try:
            backfill_days = parse_backfill_duration(backfill)
        except ValueError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1) from None

    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)
    ssh = _make_ssh_manager(cloud_cfg, project_root)

    try:
        ssh.connect(cloud_cfg.droplet_ip)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to connect to server: {exc}")
        raise SystemExit(1) from exc

    # Build the JSON payload for the server-side runner
    payload: dict[str, object] = {
        "sources": [source],
        "full_refresh": full_refresh,
        "project_root": _PROJECT_ROOT,
    }
    if backfill_days is not None:
        payload["backfill_days"] = backfill_days

    escaped_payload = shlex.quote(json.dumps(payload))
    # Run as dango user (not root) to avoid file ownership pollution,
    # and set DANGO_CLOUD_MODE so sync_trigger stops Metabase before writing.
    # cd to project dir first — SSH session starts in /root/ which dango can't access.
    cd_prefix = f"cd {_PROJECT_ROOT} &&"
    sudo_cmd = (
        f"sudo -u dango -H env DANGO_CLOUD_MODE=true"
        f" {_VENV_PYTHON} -m dango.platform.scheduling.sync_trigger {escaped_payload}"
    )
    cmd = f"{cd_prefix} {sudo_cmd}"

    try:
        if wait:
            with Status(f"Syncing [bold]{source}[/bold]...", console=console):
                result = ssh.exec_command(cmd, timeout=3600, check=False)

            if result.success and result.stdout:
                try:
                    data = json.loads(result.stdout.strip())
                except json.JSONDecodeError:
                    console.print(result.stdout.rstrip())
                    return

                status = data.get("status", "unknown")
                duration = data.get("duration_seconds", 0)

                if status == "success":
                    console.print(
                        f"[green]Sync completed[/green] for [bold]{source}[/bold] in {duration}s."
                    )
                else:
                    error = data.get("error", "Unknown error")
                    console.print(f"[red]Sync failed[/red] for [bold]{source}[/bold]: {error}")
                    raise SystemExit(1)
            else:
                stderr = result.stderr.strip() if result.stderr else "No output"
                console.print(f"[red]Error:[/red] {stderr}")
                raise SystemExit(1)
        else:
            # Fire-and-forget: only the sync command is backgrounded; the directory check
            # and a short liveness check run in the foreground (see remote_launch).
            script = build_background_launch(_PROJECT_ROOT, sudo_cmd)
            result = ssh.exec_command(f"sh -c {shlex.quote(script)}", timeout=30, check=False)
            if not result.success:
                stderr = result.stderr.strip() if result.stderr else "Could not start sync"
                console.print(f"[red]Error:[/red] {stderr}")
                raise SystemExit(1)
            console.print(
                f"[green]Sync triggered[/green] for [bold]{source}[/bold] "
                f"(running in background on server)."
            )
    except CloudSSHError as exc:
        console.print(f"[red]Error:[/red] SSH command failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()
