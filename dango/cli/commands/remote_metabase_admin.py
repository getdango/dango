"""dango/cli/commands/remote_metabase_admin.py

Remote Metabase admin repair: run ``dango metabase repair-admin`` on the cloud server.

Registered on the ``remote`` group defined in ``remote.py``; the parent module triggers
registration by importing this module at the bottom of ``remote.py``.
"""

from __future__ import annotations

import click
from rich.markup import escape

from dango.cli import console
from dango.cli.commands.remote import remote
from dango.cli.utils import safe_confirm

# Covers the repair's own worst case (about 570 s) with margin.
_REPAIR_TIMEOUT_SECONDS = 900


@remote.command("metabase-repair-admin")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def remote_metabase_repair_admin(ctx: click.Context, yes: bool) -> None:
    """Repair Metabase admin access on the remote cloud server.

    Runs ``dango metabase repair-admin`` on the server as the ``dango`` user
    (data-preserving: no Metabase data is changed, no volume is removed). Use it
    when ``dango serve`` reports that Metabase admin access needs repair, for
    example after a backup restore or a server migration.

    Example:

      dango remote metabase-repair-admin
    """
    from dango.cli.commands.remote_mgmt import (
        _load_cloud_config_with_ip,
        _make_ssh_manager,
    )
    from dango.platform.common.metabase_credential_state import cloud_repair_admin_command

    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)

    if not yes and not safe_confirm(
        "Metabase restarts for about a minute; no Metabase data is changed. Continue?",
        abort=True,
    ):
        return

    ssh = _make_ssh_manager(cloud_cfg, project_root)
    try:
        ssh.connect(cloud_cfg.droplet_ip)
        result = ssh.exec_command(
            cloud_repair_admin_command(), timeout=_REPAIR_TIMEOUT_SECONDS, check=False
        )
    except Exception as exc:
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise click.Abort() from None
    finally:
        try:
            ssh.disconnect()
        except Exception:  # noqa: BLE001
            pass

    if result.success:
        # Server output is untrusted text: never let rich read its brackets as markup.
        console.print(
            _last_line(result.stdout) or "Metabase admin access restored.",
            style="green",
            markup=False,
            highlight=False,
        )
        return

    reason = _last_line(result.stdout) or _last_line(result.stderr) or "No output from server."
    console.print("[red]Metabase admin repair failed:[/red]", escape(reason), highlight=False)
    raise click.Abort()


def _last_line(text: str | None) -> str:
    """Return the last non-empty line of server output (the repair's one-line outcome)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""
