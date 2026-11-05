"""dango/cli/commands/backup_notices.py

What the CLI tells the user about where backups are and what they do not hold.

- ``print_offsite_from_stdout``: the off-site line a server-side backup printed.
- ``backup_status_lines``: the same fact for ``dango remote status``, read from the server.
- ``print_secrets_reminder``: before ``deploy destroy``, ``remote resize`` and ``remote backup
  restore``, that ``.env`` and ``.dlt/secrets.toml`` are not in backups (names only).
- ``print_reenter``: after a download or restore, which secrets to re-enter.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from rich.markup import escape

from dango.cli import console
from dango.cli.commands import remote_report
from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud.backup_destination import (
    DOWNLOAD_HINT,
    OFFSITE_PREFIX,
    SERVER_PROJECT_DIR,
    destination_from_env,
    offsite_line,
    reenter_message,
    server_secret_names,
    validated_destination,
)
from dango.platform.cloud.project_files import read_project_file, write_project_file
from dango.utils.env_file import parse_env_file

SPACES_KEY_NAMES = ("SPACES_ACCESS_KEY", "SPACES_SECRET_KEY")
_NOT_OFFSITE = (f"{OFFSITE_PREFIX}NO", f"{OFFSITE_PREFIX}unverified")


class ServerEnvUnreadable(Exception):
    """The server ``.env`` could not be read (so nothing can be said about the destination)."""


def _printable(text: str) -> str:
    return "".join(c for c in text if c.isprintable())


def _hint_lines() -> list[str]:
    """Two short lines (a panel would wrap one long one) naming the command that saves a copy."""
    return ["  To keep a copy off this server, run:", f"    {escape(DOWNLOAD_HINT)}"]


def _print_hint() -> None:
    for text in _hint_lines():
        console.print(text, highlight=False)


def print_offsite_from_stdout(stdout: str) -> None:
    """Print the off-site line a server-side backup reported (and what to do when it is not)."""
    found = next(
        (_printable(ln) for ln in stdout.splitlines() if ln.startswith(OFFSITE_PREFIX)), None
    )
    if found is None:
        console.print(
            f"[yellow]{OFFSITE_PREFIX}unknown[/yellow] (this server's Dango did not report where "
            "the backup went; run [bold]dango remote upgrade[/bold])",
            highlight=False,
        )
        return
    if found.startswith(_NOT_OFFSITE):
        console.print(f"[yellow]{escape(found)}[/yellow]", highlight=False, soft_wrap=True)
        _print_hint()
    else:
        console.print(f"[green]{escape(found)}[/green]", highlight=False, soft_wrap=True)


def _server_cloud_yml_destination(ssh: Any) -> dict[str, str] | None:
    """The destination in the server's ``.dango/cloud.yml`` (the server's fallback), if any."""
    import yaml

    text = read_project_file(ssh, f"{SERVER_PROJECT_DIR}/.dango/cloud.yml")
    data = yaml.safe_load(text) if text.strip() else None
    if not isinstance(data, dict):
        return None
    spaces = data.get("spaces")
    if not isinstance(spaces, dict) or not spaces.get("bucket"):
        return None
    return validated_destination(
        spaces["bucket"], spaces.get("region") or data.get("region"), source="cloud.yml"
    )


def server_destination(ssh: Any) -> tuple[dict[str, str] | None, bool]:
    """``(destination, has_keys)`` the server's scheduled backup would use.

    Raises:
        ServerEnvUnreadable: If the server ``.env`` cannot be read.
        CloudProvisioningError: If it names an invalid destination.
    """
    try:
        env_text = read_project_file(ssh, f"{SERVER_PROJECT_DIR}/.env")
    except (CloudError, OSError) as exc:
        raise ServerEnvUnreadable(str(exc)) from exc
    values = parse_env_file(env_text)
    destination = destination_from_env(values)
    if destination is None:
        try:
            destination = _server_cloud_yml_destination(ssh)
        except Exception:
            destination = None
    return destination, all(values.get(k) for k in SPACES_KEY_NAMES)


def backup_status_lines(ssh: Any) -> list[str]:
    """Rich-markup lines for the ``Backups`` panel of ``dango remote status``."""
    try:
        destination, has_keys = server_destination(ssh)
    except ServerEnvUnreadable:
        return [f"  [yellow]{OFFSITE_PREFIX}unknown[/yellow] (could not read the server .env)"]
    except CloudProvisioningError as exc:
        return [
            f"  [yellow]{escape(OFFSITE_PREFIX)}NO:[/yellow] {escape(str(exc))}",
            *_hint_lines(),
        ]
    lines: list[str] = []
    if destination and has_keys:
        lines.append(f"  [green]{escape(offsite_line(destination['bucket']))}[/green]")
    elif destination:
        lines.append(
            f"  [yellow]{OFFSITE_PREFIX}NO:[/yellow] Spaces bucket {escape(destination['bucket'])} "
            "is set but SPACES_ACCESS_KEY / SPACES_SECRET_KEY are missing from the server .env"
        )
    else:
        lines.append(f"  [yellow]{escape(offsite_line(None))}[/yellow]")
    if not (destination and has_keys):
        lines.extend(_hint_lines())
    try:
        timer = ssh.exec_command("systemctl is-active dango-backup.timer", timeout=10)
        enabled = str(timer.stdout).strip() == "active"
    except (CloudError, OSError):
        return lines
    lines.append(
        "  Scheduled backups: enabled (daily at 02:00 UTC)"
        if enabled
        else "  Scheduled backups: not enabled (run [bold]dango remote backup enable[/bold])"
    )
    return lines


def secrets_reminder_lines(names: list[str] | None, *, byos: bool) -> list[str]:
    """Plain lines saying secrets are not in backups; *names* None = the server was not reachable."""
    lines = [
        "Before you continue: .env and .dlt/secrets.toml are NOT in backups by default.",
    ]
    if names is None:
        lines.append("  Could not read the server to list the secret names it holds.")
    elif names:
        lines.append(f"  Secrets on this server (names only): {', '.join(names)}")
    else:
        lines.append("  No secrets were found on this server.")
    lines.append("  Keep a copy of their values somewhere safe: you will have to re-enter them.")
    if byos:
        lines.append(
            "  Backups on a server of your own are lost with the server. "
            f"To keep a copy: {DOWNLOAD_HINT}"
        )
    return lines


def print_secrets_reminder(
    cloud_cfg: Any, project_root: Path | None = None, *, ssh: Any = None
) -> None:
    """Print the secrets reminder before a destructive command.

    Reads the secret names from the server (names only, never values) over *ssh*, or over a
    connection of its own when none is given; an unreachable server still gets the reminder.
    """
    names: list[str] | None = None
    own = None
    try:
        if ssh is None:
            from dango.cli.commands.remote_mgmt import _make_ssh_manager, _resolve_ssh_key_path

            if project_root is None or not _resolve_ssh_key_path(cloud_cfg, project_root).exists():
                raise OSError("no SSH key")
            own = ssh = _make_ssh_manager(cloud_cfg, project_root)
            ssh.connect(cloud_cfg.droplet_ip)
        names = server_secret_names(ssh)
    except Exception:
        names = None
    finally:
        if own is not None:
            with contextlib.suppress(Exception):
                own.disconnect()
    console.print()
    for number, text in enumerate(
        secrets_reminder_lines(names, byos=getattr(cloud_cfg, "provider", "") == "byos")
    ):
        style = "yellow" if number == 0 else "dim"
        console.print(f"[{style}]{escape(text)}[/{style}]", highlight=False, soft_wrap=True)
    console.print()


def print_reenter(names: list[str] | None, included: bool = False) -> None:
    """Say which secrets to re-enter after a backup is downloaded or restored."""
    if included:
        return
    console.print(f"[yellow]Note:[/yellow] {escape(reenter_message(names))}", highlight=False)


def prepare_destination(ssh: Any, cloud_cfg: Any) -> dict[str, str] | None:
    """Check what the backup timer needs and write the destination the server lacks.

    The timer reads the bucket, region and keys from the server ``.env``.  Returns the
    destination, or None when the server has none (backups then stay on the server).
    Exits with status 1, after saying why, when the ``.env`` cannot be read or written, the
    destination is not valid, or the keys and the destination do not match.
    """
    try:
        env_text = read_project_file(ssh, f"{SERVER_PROJECT_DIR}/.env")
    except CloudProvisioningError as exc:
        remote_report.failure("Could not read the server .env file", exc)
        raise SystemExit(1) from exc
    values = parse_env_file(env_text)
    to_write: dict[str, str] | None = None
    try:
        destination = destination_from_env(values)
        if destination is None and cloud_cfg.spaces is not None and cloud_cfg.provider != "byos":
            region = values.get("SPACES_REGION") or cloud_cfg.spaces.region or cloud_cfg.region
            destination = to_write = validated_destination(
                cloud_cfg.spaces.bucket, region, source=".env"
            )
    except CloudProvisioningError as exc:
        remote_report.failure("The Spaces destination is not valid", exc)
        raise SystemExit(1) from exc
    missing = [k for k in SPACES_KEY_NAMES if not values.get(k)]
    if destination is not None and missing:
        console.print(
            f"[red]Error:[/red] {'; '.join(f'{k} not found' for k in missing)} in server .env "
            "file. Add Spaces credentials before enabling scheduled backups."
        )
        raise SystemExit(1)
    if destination is None and len(missing) < len(SPACES_KEY_NAMES):
        console.print(
            "[red]Error:[/red] Spaces credentials are in the server .env file, but no bucket "
            "is configured. Set [bold]spaces.bucket[/bold] in [bold].dango/cloud.yml[/bold] "
            "and run this command again."
        )
        raise SystemExit(1)
    if to_write is not None:
        base = env_text if not env_text or env_text.endswith("\n") else env_text + "\n"
        wanted = {"SPACES_BUCKET": to_write["bucket"], "SPACES_REGION": to_write["region"]}
        added = "".join(f"{k}={v}\n" for k, v in wanted.items() if not values.get(k))
        try:
            write_project_file(ssh, f"{SERVER_PROJECT_DIR}/.env", base + added, mode=0o600)
        except CloudProvisioningError as exc:
            remote_report.failure("Could not write the Spaces destination to the server", exc)
            raise SystemExit(1) from exc
    return destination
