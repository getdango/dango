"""dango/cli/commands/schedule.py

Schedule management CLI commands (list, status, add, remove, enable, disable).
Operates directly on ``.dango/schedules.yml``.  Webhook subcommands in
``schedule_webhook.py``, registered via bottom-of-file import.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import yaml

from dango.cli import console
from dango.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_FREQUENCY_CHOICES = [
    ("Every hour", "0 * * * *"),
    ("Every 6 hours", "0 */6 * * *"),
    ("Daily (6 AM)", "0 6 * * *"),
    ("Weekly (Monday 6 AM)", "0 6 * * 1"),
    ("Custom cron", "custom"),
]


def _load_schedules_yaml(project_root: Path) -> dict[str, Any]:
    """Load raw YAML from ``.dango/schedules.yml``, or empty dict if missing."""
    path = project_root / ".dango" / "schedules.yml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    return data


def _save_schedules_yaml(project_root: Path, data: dict[str, Any]) -> None:
    """Atomic write via ``ConfigLoader.save_yaml()``.  Preserves both sections."""
    from dango.config.loader import ConfigLoader

    loader = ConfigLoader(project_root)
    loader.save_yaml(data, project_root / ".dango" / "schedules.yml")


def _find_schedule(schedules: list[dict[str, Any]], name: str) -> tuple[int, dict[str, Any]] | None:
    """Return (index, schedule_dict) for a schedule by name, or ``None``."""
    for i, sched in enumerate(schedules):
        if sched.get("name") == name:
            return i, sched
    return None


def _get_next_run(cron_expr: str) -> str:
    """Compute next run time from a cron expression using croniter."""
    try:
        from croniter import croniter

        it = croniter(cron_expr, datetime.now())
        next_dt: datetime = it.get_next(datetime)
        return next_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _get_local_timezone() -> str:
    """Return the local timezone name."""
    if time.daylight and time.localtime().tm_isdst > 0:
        return time.tzname[1]
    return time.tzname[0]


def _toggle_schedule(ctx: click.Context, name: str, *, enable: bool) -> None:
    """Shared logic for enable/disable commands."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    schedules = data.get("schedules", [])

    result = _find_schedule(schedules, name)
    if result is None:
        console.print(f"[red]Error:[/red] Schedule '{name}' not found.")
        raise SystemExit(1)

    _, sched = result
    current = sched.get("enabled", True)
    action = "enabled" if enable else "disabled"

    if current == enable:
        console.print(f"[blue]Schedule '{name}' is already {action}.[/blue]")
        return

    sched["enabled"] = enable
    _save_schedules_yaml(project_root, data)
    console.print(f"[green]Schedule '{name}' {action}.[/green]")


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group()
@click.pass_context
def schedule(ctx: click.Context) -> None:
    """Manage data sync schedules.

    Configure when and how data sources are synchronized.
    """
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# schedule list
# ---------------------------------------------------------------------------


@schedule.command("list")
@click.pass_context
def schedule_list(ctx: click.Context) -> None:
    """List all configured schedules."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    schedules = data.get("schedules", [])

    if not schedules:
        console.print(
            "[dim]No schedules configured.[/dim] Run [bold]dango schedule add[/bold] to create one."
        )
        return

    from rich.table import Table

    table = Table(title="Schedules")
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Cron")
    table.add_column("Sources")
    table.add_column("Enabled")
    table.add_column("Next Run")

    for sched in schedules:
        sources = sched.get("sources", [])
        enabled = sched.get("enabled", True)
        sources_str = ", ".join(sources) if sources else "—"
        if len(sources_str) > 40:
            sources_str = sources_str[:37] + "..."
        table.add_row(
            sched.get("name", "?"),
            sched.get("type", "sync"),
            sched.get("cron", "?"),
            sources_str,
            "[green]yes[/green]" if enabled else "[red]no[/red]",
            _get_next_run(sched.get("cron", "")) if enabled else "—",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# schedule status
# ---------------------------------------------------------------------------


@schedule.command("status")
@click.pass_context
def schedule_status(ctx: click.Context) -> None:
    """Show scheduler status overview."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    schedules = data.get("schedules", [])

    if not schedules:
        console.print(
            "[dim]No schedules configured.[/dim] Run [bold]dango schedule add[/bold] to create one."
        )
        return

    # 1. Next scheduled run (earliest across enabled schedules)
    from croniter import croniter

    now = datetime.now()
    earliest_name: str | None = None
    earliest_dt: datetime | None = None
    for sched in schedules:
        if not sched.get("enabled", True):
            continue
        try:
            it = croniter(sched.get("cron", ""), now)
            nxt: datetime = it.get_next(datetime)
            if earliest_dt is None or nxt < earliest_dt:
                earliest_dt = nxt
                earliest_name = sched.get("name", "?")
        except Exception:
            logger.debug("croniter_parse_failed", schedule=sched.get("name"))
            continue

    if earliest_dt is not None:
        console.print(
            f"[bold]Next run:[/bold] {earliest_name} at {earliest_dt.strftime('%Y-%m-%d %H:%M')}"
        )
    else:
        console.print("[bold]Next run:[/bold] [dim]no enabled schedules[/dim]")

    # 2. Last run (from scheduler.db if exists)
    from dango.platform.scheduling.history import get_scheduler_db_path

    db_path = get_scheduler_db_path(project_root)
    if db_path.exists():
        from dango.platform.scheduling.history import get_recent_history

        recent = get_recent_history(db_path, limit=1)
        if recent:
            last = recent[0]
            console.print(
                f"[bold]Last run:[/bold] {last.get('schedule_name', '?')} — "
                f"{last.get('status', '?')} at {last.get('started_at', '?')}"
            )
        else:
            console.print("[bold]Last run:[/bold] [dim]never[/dim]")
    else:
        console.print("[bold]Last run:[/bold] [dim]never[/dim]")

    # 3. Unscheduled sources
    try:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root)
        sources_cfg = loader.load_sources_config()
        all_sources = {s.name for s in sources_cfg.sources if s.enabled}
    except Exception:
        logger.debug("sources_config_load_failed")
        all_sources = set()

    scheduled_sources: set[str] = set()
    for sched in schedules:
        scheduled_sources.update(sched.get("sources", []))

    unscheduled = sorted(all_sources - scheduled_sources)
    if unscheduled:
        console.print(
            f"[bold]Unscheduled sources:[/bold] [yellow]{', '.join(unscheduled)}[/yellow]"
        )

    # 4. Validation warnings
    from dango.config.schedules import ScheduleConfig, validate_schedules

    try:
        sched_models = [ScheduleConfig(**s) for s in schedules]
        warnings = validate_schedules(sched_models, all_sources)
        for w in warnings:
            console.print(f"[yellow]Warning:[/yellow] {w}")
    except Exception:
        logger.debug("schedule_validation_failed")


# ---------------------------------------------------------------------------
# schedule add (interactive wizard)
# ---------------------------------------------------------------------------


@schedule.command("add")
@click.pass_context
def schedule_add(ctx: click.Context) -> None:
    """Add a new schedule via interactive wizard."""
    import inquirer

    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    schedules = data.setdefault("schedules", [])
    existing_names = {s.get("name") for s in schedules}

    # 1. Name
    answers = inquirer.prompt(
        [
            inquirer.Text(
                "name",
                message="Schedule name (lowercase, alphanumeric + underscore)",
                validate=lambda _, x: bool(_SLUG_RE.match(x)) and x not in existing_names,
            ),
        ]
    )
    if answers is None:
        return
    name: str = answers["name"]

    # 2. Type
    answers = inquirer.prompt(
        [inquirer.List("type", message="Schedule type", choices=["sync", "dbt"])]
    )
    if answers is None:
        return
    sched_type: str = answers["type"]

    # 3a. Sources (sync only) / 3b. dbt command
    sources: list[str] = []
    dbt_command: str | None = None
    if sched_type == "sync":
        try:
            from dango.config.loader import ConfigLoader

            loader = ConfigLoader(project_root)
            sources_cfg = loader.load_sources_config()
            available = [s.name for s in sources_cfg.sources if s.enabled]
        except Exception:
            available = []
        if available:
            answers = inquirer.prompt(
                [inquirer.Checkbox("sources", message="Select sources to sync", choices=available)]
            )
            if answers is None:
                return
            sources = answers["sources"]
        if not sources:
            console.print("[red]Error:[/red] Sync schedules require at least one source.")
            return
    else:
        answers = inquirer.prompt(
            [inquirer.Text("dbt_command", message="dbt command", default="run")]
        )
        if answers is None:
            return
        dbt_command = answers["dbt_command"]

    # 4. Frequency
    answers = inquirer.prompt(
        [
            inquirer.List(
                "frequency",
                message="Run frequency",
                choices=[label for label, _ in _FREQUENCY_CHOICES],
            )
        ]
    )
    if answers is None:
        return
    cron: str = dict(_FREQUENCY_CHOICES)[answers["frequency"]]

    if cron == "custom":
        from croniter import croniter

        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "cron",
                    message="Cron expression (5-part)",
                    validate=lambda _, x: croniter.is_valid(x),
                )
            ]
        )
        if answers is None:
            return
        cron = answers["cron"]

    # 5. Timezone
    answers = inquirer.prompt(
        [inquirer.Text("timezone", message="Timezone", default=_get_local_timezone())]
    )
    if answers is None:
        return
    timezone_str: str = answers["timezone"]

    # 6. Notify on
    answers = inquirer.prompt(
        [
            inquirer.Checkbox(
                "notify_on",
                message="Notify on (select events)",
                choices=["failure", "success", "stale"],
                default=["failure"],
            )
        ]
    )
    if answers is None:
        return
    notify_on: list[str] = answers["notify_on"]

    # Build + save
    entry: dict[str, Any] = {
        "name": name,
        "type": sched_type,
        "cron": cron,
        "enabled": True,
        "timezone": timezone_str,
    }
    if sources:
        entry["sources"] = sources
    if dbt_command:
        entry["dbt_command"] = dbt_command
    if notify_on:
        entry["notify_on"] = notify_on

    console.print()
    console.print("[bold]Schedule summary:[/bold]")
    console.print(f"  Name: {name}  Type: {sched_type}  Cron: {cron}")
    if sources:
        console.print(f"  Sources: {', '.join(sources)}")
    if dbt_command:
        console.print(f"  dbt cmd: {dbt_command}")
    console.print(f"  Timezone: {timezone_str}")
    if notify_on:
        console.print(f"  Notify on: {', '.join(notify_on)}")
    console.print()

    schedules.append(entry)
    _save_schedules_yaml(project_root, data)
    console.print(f"[green]Schedule '{name}' added.[/green]")


# ---------------------------------------------------------------------------
# schedule remove
# ---------------------------------------------------------------------------


@schedule.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def schedule_remove(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove a schedule by name."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    schedules = data.get("schedules", [])

    result = _find_schedule(schedules, name)
    if result is None:
        console.print(f"[red]Error:[/red] Schedule '{name}' not found.")
        raise SystemExit(1)

    idx, sched = result
    console.print(f"Schedule: [bold]{name}[/bold]")
    console.print(f"  Type: {sched.get('type', 'sync')}  Cron: {sched.get('cron', '?')}")

    if not yes:
        if not click.confirm("Remove this schedule?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    schedules.pop(idx)
    data["schedules"] = schedules
    _save_schedules_yaml(project_root, data)
    console.print(f"[green]Schedule '{name}' removed.[/green]")


# ---------------------------------------------------------------------------
# schedule enable / disable
# ---------------------------------------------------------------------------


@schedule.command("enable")
@click.argument("name")
@click.pass_context
def schedule_enable(ctx: click.Context, name: str) -> None:
    """Enable a disabled schedule."""
    _toggle_schedule(ctx, name, enable=True)


@schedule.command("disable")
@click.argument("name")
@click.pass_context
def schedule_disable(ctx: click.Context, name: str) -> None:
    """Disable an active schedule."""
    _toggle_schedule(ctx, name, enable=False)


# ---------------------------------------------------------------------------
# Webhook subgroup shell + cross-file registration
# ---------------------------------------------------------------------------


@schedule.group()
@click.pass_context
def webhook(ctx: click.Context) -> None:
    """Manage webhook notifications for schedules."""
    ctx.ensure_object(dict)


# Register webhook subcommands from separate module (follows remote.py pattern)
import dango.cli.commands.schedule_webhook as _schedule_webhook  # noqa: E402, F401
