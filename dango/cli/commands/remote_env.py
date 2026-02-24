"""dango/cli/commands/remote_env.py

Remote environment variable management via SSH.

Command hierarchy::

    dango remote env (subgroup)
    ├── set K=V    — Set an environment variable on the remote server
    ├── get K      — Display a masked environment variable
    ├── list       — List all environment variables (masked)
    └── delete K   — Remove an environment variable
"""

from __future__ import annotations

from typing import Any

import click

from dango.cli import console

# Remote .env path on the server (set by TASK-026 server setup)
_REMOTE_ENV_PATH = "/srv/dango/project/.env"


# ---------------------------------------------------------------------------
# env subgroup
# ---------------------------------------------------------------------------


@click.group("env")
@click.pass_context
def env(ctx: click.Context) -> None:
    """Manage environment variables on the remote server.

    Commands:
      set K=V    Set an environment variable
      get K      Display a variable (value masked)
      list       List all variables (values masked)
      delete K   Remove a variable
    """
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# .env parsing helpers
# ---------------------------------------------------------------------------


def _parse_env_file(content: str) -> dict[str, str]:
    """Parse .env file content into a dict, preserving order.

    Handles ``KEY=VALUE``, ``KEY="VALUE"``, ``KEY='VALUE'``.
    Skips blank lines and ``#`` comments.
    """
    env_vars: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            env_vars[key] = value
    return env_vars


def _serialize_env_file(env_vars: dict[str, str]) -> str:
    """Serialize a dict back to .env format.

    Values containing spaces, quotes, or special characters are
    double-quoted.  Simple values are written bare.
    """
    lines: list[str] = []
    for key, value in env_vars.items():
        # Quote values that contain spaces, quotes, #, or shell-special chars
        needs_quoting = any(c in value for c in (" ", '"', "'", "#", "$", "\n", "\t"))
        if needs_quoting:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n" if lines else ""


def _read_remote_env(ssh: Any) -> dict[str, str]:
    """Read and parse the remote .env file. Returns empty dict if missing."""
    try:
        content = ssh.read_remote_file(_REMOTE_ENV_PATH)
        return _parse_env_file(content)
    except Exception:
        return {}


def _write_remote_env(ssh: Any, env_vars: dict[str, str]) -> None:
    """Write env vars to the remote .env file with mode 0o600."""
    content = _serialize_env_file(env_vars)
    ssh.write_remote_file(_REMOTE_ENV_PATH, content, mode=0o600)


# ---------------------------------------------------------------------------
# env set
# ---------------------------------------------------------------------------


@env.command("set")
@click.argument("key_value")
@click.pass_context
def env_set(ctx: click.Context, key_value: str) -> None:
    """Set an environment variable on the remote server.

    KEY_VALUE must be in KEY=VALUE format (e.g. ``MY_KEY=my_value``).
    Creates the .env file if it doesn't exist.

    Example:
      dango remote env set GOOGLE_CLIENT_ID=123456.apps.googleusercontent.com
    """
    if "=" not in key_value:
        console.print("[red]Error:[/red] Expected KEY=VALUE format (e.g. MY_KEY=my_value)")
        raise SystemExit(1)

    key, _, value = key_value.partition("=")
    key = key.strip()
    value = value.strip()
    if not key:
        console.print("[red]Error:[/red] Key cannot be empty.")
        raise SystemExit(1)

    from dango.cli.commands.remote import _ssh_connect_or_fail

    _cloud_cfg, ssh, _project_root = _ssh_connect_or_fail(ctx)
    try:
        env_vars = _read_remote_env(ssh)
        action = "Updated" if key in env_vars else "Set"
        env_vars[key] = value
        _write_remote_env(ssh, env_vars)
        console.print(f"[green]{action}[/green] {key}=***")
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to set env var: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# env get
# ---------------------------------------------------------------------------


@env.command("get")
@click.argument("key")
@click.pass_context
def env_get(ctx: click.Context, key: str) -> None:
    """Display an environment variable from the remote server (masked).

    Shows KEY=*** if the variable exists, or an error if not found.

    Example:
      dango remote env get GOOGLE_CLIENT_ID
    """
    from dango.cli.commands.remote import _ssh_connect_or_fail

    _cloud_cfg, ssh, _project_root = _ssh_connect_or_fail(ctx)
    try:
        env_vars = _read_remote_env(ssh)
        if key not in env_vars:
            console.print(f"[yellow]Not found:[/yellow] {key} is not set in remote .env")
            raise SystemExit(1)
        console.print(f"{key}=***")
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to read env var: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# env list
# ---------------------------------------------------------------------------


@env.command("list")
@click.pass_context
def env_list(ctx: click.Context) -> None:
    """List all environment variables on the remote server (masked).

    Shows all KEY=*** pairs from the remote .env file.

    Example:
      dango remote env list
    """
    from dango.cli.commands.remote import _ssh_connect_or_fail

    _cloud_cfg, ssh, _project_root = _ssh_connect_or_fail(ctx)
    try:
        env_vars = _read_remote_env(ssh)
        if not env_vars:
            console.print("[yellow]No environment variables set.[/yellow]")
            return
        for key in env_vars:
            console.print(f"{key}=***")
        console.print(f"\n[dim]{len(env_vars)} variable(s) total[/dim]")
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to list env vars: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


# ---------------------------------------------------------------------------
# env delete
# ---------------------------------------------------------------------------


@env.command("delete")
@click.argument("key")
@click.pass_context
def env_delete(ctx: click.Context, key: str) -> None:
    """Remove an environment variable from the remote server.

    Example:
      dango remote env delete GOOGLE_CLIENT_ID
    """
    from dango.cli.commands.remote import _ssh_connect_or_fail

    _cloud_cfg, ssh, _project_root = _ssh_connect_or_fail(ctx)
    try:
        env_vars = _read_remote_env(ssh)
        if key not in env_vars:
            console.print(f"[yellow]Not found:[/yellow] {key} is not set in remote .env")
            raise SystemExit(1)
        del env_vars[key]
        _write_remote_env(ssh, env_vars)
        console.print(f"[green]Deleted[/green] {key}")
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to delete env var: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()
