"""dango/cli/commands/schedule_webhook.py

Webhook management subcommands for ``dango schedule webhook``.

Command hierarchy::

    dango schedule webhook
    ├── list       — List configured webhooks
    ├── add        — Add a webhook via interactive prompts
    ├── remove     — Remove a webhook by name
    └── test       — Send a test payload to a webhook

Registered on the ``webhook`` group from ``schedule.py`` via import.
"""

from __future__ import annotations

from typing import Any

import click

from dango.cli import console
from dango.cli.commands.schedule import (
    _load_schedules_yaml,
    _save_schedules_yaml,
    webhook,
)

# ---------------------------------------------------------------------------
# webhook list
# ---------------------------------------------------------------------------


@webhook.command("list")
@click.pass_context
def webhook_list(ctx: click.Context) -> None:
    """List configured webhooks."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    notifications = data.get("notifications", {})
    webhooks: list[dict[str, Any]] = notifications.get("webhooks", [])

    if not webhooks:
        console.print(
            "[dim]No webhooks configured.[/dim] "
            "Run [bold]dango schedule webhook add[/bold] to create one."
        )
        return

    from rich.table import Table

    table = Table(title="Webhooks")
    table.add_column("Name", style="bold")
    table.add_column("URL")
    table.add_column("Format")

    for wh in webhooks:
        table.add_row(
            wh.get("name", "?"),
            wh.get("url", "?"),
            wh.get("format", "generic"),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# webhook add
# ---------------------------------------------------------------------------


@webhook.command("add")
@click.pass_context
def webhook_add(ctx: click.Context) -> None:
    """Add a webhook via interactive prompts."""
    import inquirer

    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    notifications: dict[str, Any] = data.setdefault("notifications", {})
    webhooks: list[dict[str, Any]] = notifications.setdefault("webhooks", [])

    answers = inquirer.prompt(
        [
            inquirer.Text("name", message="Webhook name"),
            inquirer.Text(
                "url",
                message="Webhook URL",
                validate=lambda _, x: x.startswith(("http://", "https://")),
            ),
            inquirer.List(
                "format",
                message="Payload format",
                choices=["generic", "slack"],
            ),
        ]
    )
    if answers is None:
        return

    entry: dict[str, str] = {
        "name": answers["name"],
        "url": answers["url"],
        "format": answers["format"],
    }
    webhooks.append(entry)
    _save_schedules_yaml(project_root, data)
    console.print(f"[green]Webhook '{answers['name']}' added.[/green]")


# ---------------------------------------------------------------------------
# webhook remove
# ---------------------------------------------------------------------------


@webhook.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def webhook_remove(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove a webhook by name."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    notifications = data.get("notifications", {})
    webhooks: list[dict[str, Any]] = notifications.get("webhooks", [])

    found_idx: int | None = None
    for i, wh in enumerate(webhooks):
        if wh.get("name") == name:
            found_idx = i
            break

    if found_idx is None:
        console.print(f"[red]Error:[/red] Webhook '{name}' not found.")
        raise SystemExit(1)

    if not yes:
        if not click.confirm(f"Remove webhook '{name}'?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    webhooks.pop(found_idx)
    _save_schedules_yaml(project_root, data)
    console.print(f"[green]Webhook '{name}' removed.[/green]")


# ---------------------------------------------------------------------------
# webhook test
# ---------------------------------------------------------------------------


@webhook.command("test")
@click.argument("name")
@click.pass_context
def webhook_test(ctx: click.Context, name: str) -> None:
    """Send a test payload to a webhook."""
    import httpx

    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    notifications = data.get("notifications", {})
    webhooks: list[dict[str, Any]] = notifications.get("webhooks", [])

    target: dict[str, Any] | None = None
    for wh in webhooks:
        if wh.get("name") == name:
            target = wh
            break

    if target is None:
        console.print(f"[red]Error:[/red] Webhook '{name}' not found.")
        raise SystemExit(1)

    url: str = target["url"]
    payload = {
        "event": "test",
        "schedule": "test_schedule",
        "sources": [],
        "message": "Test notification from Dango CLI",
    }

    console.print(f"Sending test payload to [bold]{url}[/bold]...")

    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
        if resp.status_code < 300:
            console.print(f"[green]Success![/green] Status: {resp.status_code}")
        else:
            console.print(f"[red]Failed.[/red] Status: {resp.status_code}")
    except httpx.HTTPError as exc:
        console.print(f"[red]Error:[/red] {exc}")
