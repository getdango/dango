"""dango/cli/commands/remote_auth.py

Remote user management via SSH.

Command hierarchy::

    dango remote auth (subgroup)
    ├── add-user <email> --role <viewer|editor|admin>
    ├── list-users
    ├── remove-user <email>
    └── reset-password <email>

Runs ``dango auth`` subcommands on the remote server over SSH, following
the same pattern as ``remote_env.py`` and ``remote_mgmt.py``.
"""

from __future__ import annotations

import shlex
from typing import Any

import click

from dango.cli import console

# Remote paths on the cloud server (set by server setup)
_DANGO_CLI = "/srv/dango/venv/bin/dango"
_PROJECT_ROOT = "/srv/dango/project"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect_ssh_or_fail(ctx: click.Context) -> tuple[Any, Any]:
    """Load cloud config, connect SSH as root, return (cloud_cfg, ssh).

    Uses root because auth commands need read/write access to the auth
    database at ``.dango/auth.db``.

    Raises:
        SystemExit: If no deployment or SSH connection fails.
    """
    from dango.cli.commands.remote_mgmt import _load_cloud_config_with_ip, _make_ssh_manager

    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)
    ssh = _make_ssh_manager(cloud_cfg, project_root)
    try:
        ssh.connect(cloud_cfg.droplet_ip)
    except Exception as exc:
        console.print(
            f"[red]Error:[/red] Cannot connect to server: {exc}\n"
            "Check [bold]dango remote status[/bold] for connectivity."
        )
        raise SystemExit(1) from exc

    return cloud_cfg, ssh


def _run_remote_auth_cmd(
    ssh: Any,
    args: str,
    *,
    pipe_yes: bool = False,
    timeout: int = 30,
) -> bool:
    """Execute a ``dango auth`` subcommand on the remote server.

    Args:
        ssh: Connected SSHManager instance.
        args: Arguments to pass after ``dango auth`` (already shell-safe).
        pipe_yes: If True, prefix with ``yes |`` to auto-confirm prompts.
        timeout: Command timeout in seconds.

    Returns:
        True on success, False on failure (error already printed).
    """
    cmd = f"cd {_PROJECT_ROOT} && {_DANGO_CLI} auth {args}"
    if pipe_yes:
        cmd = f"yes | {cmd}"

    result = ssh.exec_command(cmd, timeout=timeout, check=False)
    if result.success:
        if result.stdout:
            console.print(result.stdout.rstrip())
        return True

    stderr = result.stderr.strip() if result.stderr else ""
    stdout = result.stdout.strip() if result.stdout else ""
    msg = stderr or stdout or "Command failed with no output."
    console.print(f"[red]Error:[/red] {msg}")
    return False


# ---------------------------------------------------------------------------
# auth subgroup
# ---------------------------------------------------------------------------


@click.group("auth")
@click.pass_context
def auth_group(ctx: click.Context) -> None:
    """Manage users on the remote server.

    Commands:
      add-user        Create a new user
      list-users      List all users
      remove-user     Remove a user
      reset-password  Reset a user's password
    """
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# add-user
# ---------------------------------------------------------------------------


@auth_group.command("add-user")
@click.argument("email")
@click.option(
    "--role",
    type=click.Choice(["admin", "editor", "viewer"]),
    default="viewer",
    help="Role for the new user.",
)
@click.pass_context
def auth_add_user(ctx: click.Context, email: str, role: str) -> None:
    """Create a new user on the remote server with a temporary password.

    The user will be prompted to change their password on first login.

    Example:
      dango remote auth add-user alice@example.com --role editor
    """
    _cloud_cfg, ssh = _connect_ssh_or_fail(ctx)
    try:
        safe_email = shlex.quote(email)
        args = f"add-user {safe_email} --role {role} --password"
        if not _run_remote_auth_cmd(ssh, args):
            raise SystemExit(1)
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# list-users
# ---------------------------------------------------------------------------


@auth_group.command("list-users")
@click.pass_context
def auth_list_users(ctx: click.Context) -> None:
    """List all users on the remote server.

    Example:
      dango remote auth list-users
    """
    _cloud_cfg, ssh = _connect_ssh_or_fail(ctx)
    try:
        if not _run_remote_auth_cmd(ssh, "list-users"):
            raise SystemExit(1)
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# remove-user
# ---------------------------------------------------------------------------


@auth_group.command("remove-user")
@click.argument("email")
@click.pass_context
def auth_remove_user(ctx: click.Context, email: str) -> None:
    """Remove a user from the remote server.

    This permanently deletes the user and cannot be undone.

    Example:
      dango remote auth remove-user alice@example.com
    """
    _cloud_cfg, ssh = _connect_ssh_or_fail(ctx)
    try:
        safe_email = shlex.quote(email)
        args = f"delete-user {safe_email}"
        if not _run_remote_auth_cmd(ssh, args, pipe_yes=True):
            raise SystemExit(1)
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# reset-password
# ---------------------------------------------------------------------------


@auth_group.command("reset-password")
@click.argument("email")
@click.pass_context
def auth_reset_password(ctx: click.Context, email: str) -> None:
    """Reset a user's password on the remote server.

    Generates a new temporary password. The user must change it on next login.

    Example:
      dango remote auth reset-password alice@example.com
    """
    _cloud_cfg, ssh = _connect_ssh_or_fail(ctx)
    try:
        safe_email = shlex.quote(email)
        args = f"reset-password {safe_email}"
        if not _run_remote_auth_cmd(ssh, args):
            raise SystemExit(1)
    finally:
        ssh.disconnect()
