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
    ("Every hour", "1h"),
    ("Every 2 hours", "2h"),
    ("Every 3 hours", "3h"),
    ("Every 4 hours", "4h"),
    ("Every 6 hours", "6h"),
    ("Every 8 hours", "8h"),
    ("Every 12 hours", "12h"),
    ("Daily", "daily"),
    ("Weekly", "weekly"),
    ("Custom cron", "custom"),
]

_TYPE_CHOICES = [
    ("Sync & Transform (recommended)", "sync"),
    ("Sync only (no transform)", "sync_only"),
    ("Transform only (dbt)", "dbt"),
]

_DAY_CHOICES = [
    ("Monday", 1),
    ("Tuesday", 2),
    ("Wednesday", 3),
    ("Thursday", 4),
    ("Friday", 5),
    ("Saturday", 6),
    ("Sunday", 0),
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


def _get_next_runs(cron_expr: str, count: int = 3) -> list[str]:
    """Return next *count* run times as formatted strings."""
    try:
        from croniter import croniter

        it = croniter(cron_expr, datetime.now())
        return [it.get_next(datetime).strftime("%Y-%m-%d %H:%M") for _ in range(count)]
    except Exception:
        return []


def _get_local_timezone() -> str:
    """Return the local timezone name."""
    if time.daylight and time.localtime().tm_isdst > 0:
        return time.tzname[1]
    return time.tzname[0]


def _build_hourly_hours(interval: int, start_hour: int) -> list[int]:
    """Compute run hours for an interval starting at *start_hour*."""
    return sorted(h % 24 for h in range(start_hour, start_hour + 24, interval))


def _build_cron_interactive(selection: str) -> str | None:
    """Prompt for time specifics based on frequency *selection* and return cron.

    Returns ``None`` if the user cancels a prompt.
    """
    import inquirer

    if selection == "custom":
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
            return None
        cron_val: str = answers["cron"]
        return cron_val

    if selection == "weekly":
        # Day → hour → minute (coarse to fine)
        answers = inquirer.prompt(
            [
                inquirer.List(
                    "day",
                    message="Day of week",
                    choices=[label for label, _ in _DAY_CHOICES],
                    default="Monday",
                )
            ]
        )
        if answers is None:
            return None
        day_num = dict(_DAY_CHOICES)[answers["day"]]

        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "hour",
                    message="Hour (0-23)",
                    default="6",
                    validate=lambda _, x: x.isdigit() and 0 <= int(x) <= 23,
                )
            ]
        )
        if answers is None:
            return None
        hour = int(answers["hour"])

        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "minute",
                    message="Minute (0-59)",
                    default="0",
                    validate=lambda _, x: x.isdigit() and 0 <= int(x) <= 59,
                )
            ]
        )
        if answers is None:
            return None
        minute = int(answers["minute"])
        return f"{minute} {hour} * * {day_num}"

    if selection == "daily":
        # Hour → minute
        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "hour",
                    message="Hour (0-23)",
                    default="6",
                    validate=lambda _, x: x.isdigit() and 0 <= int(x) <= 23,
                )
            ]
        )
        if answers is None:
            return None
        hour = int(answers["hour"])

        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "minute",
                    message="Minute (0-59)",
                    default="0",
                    validate=lambda _, x: x.isdigit() and 0 <= int(x) <= 59,
                )
            ]
        )
        if answers is None:
            return None
        minute = int(answers["minute"])
        return f"{minute} {hour} * * *"

    # Hourly intervals: 1h, 2h, 3h, 4h, 6h, 8h, 12h
    interval = int(selection.rstrip("h"))

    # Minute → start_hour (minute first since it applies to every run)
    answers = inquirer.prompt(
        [
            inquirer.Text(
                "minute",
                message="Minute (0-59)",
                default="0",
                validate=lambda _, x: x.isdigit() and 0 <= int(x) <= 59,
            )
        ]
    )
    if answers is None:
        return None
    minute = int(answers["minute"])

    # Every hour: just minute, no start_hour needed
    if interval == 1:
        return f"{minute} * * * *"

    # Prompt start hour
    answers = inquirer.prompt(
        [
            inquirer.Text(
                "start_hour",
                message=f"Start hour (0-23, runs every {interval}h from this hour)",
                default="0",
                validate=lambda _, x: x.isdigit() and 0 <= int(x) <= 23,
            )
        ]
    )
    if answers is None:
        return None
    start_hour = int(answers["start_hour"])

    hours = _build_hourly_hours(interval, start_hour)
    hours_csv = ",".join(str(h) for h in hours)
    console.print(f"  Runs at hours: {hours_csv}")
    return f"{minute} {hours_csv} * * *"


def _show_next_runs(cron_expr: str) -> None:
    """Print next 3 scheduled run times."""
    runs = _get_next_runs(cron_expr, 3)
    if runs:
        console.print("[dim]Next run times:[/dim]")
        for i, r in enumerate(runs, 1):
            console.print(f"  {i}. {r}")


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


def _show_schedule_detail(project_root: Path, schedules: list[dict[str, Any]], name: str) -> None:
    """Show detailed status for a single schedule (BUG-041)."""
    result = _find_schedule(schedules, name)
    if result is None:
        console.print(f"[red]Error:[/red] Schedule '{name}' not found.")
        raise SystemExit(1)

    _, sched = result
    enabled = sched.get("enabled", True)

    console.print(f"[bold]Schedule:[/bold] {name}")
    console.print(f"  Type:     {sched.get('type', 'sync')}")
    console.print(f"  Cron:     {sched.get('cron', '?')}")
    console.print(f"  Enabled:  {'yes' if enabled else 'no'}")
    if sched.get("timezone"):
        console.print(f"  Timezone: {sched['timezone']}")
    if sched.get("sources"):
        console.print(f"  Sources:  {', '.join(sched['sources'])}")
    if sched.get("dbt_command"):
        console.print(f"  dbt cmd:  {sched['dbt_command']}")
    if sched.get("notify_on"):
        console.print(f"  Notify:   {', '.join(sched['notify_on'])}")

    # Next run
    if enabled:
        console.print(f"  Next run: {_get_next_run(sched.get('cron', ''))}")

    # Recent history from scheduler.db
    from dango.platform.scheduling.history import get_scheduler_db_path

    db_path = get_scheduler_db_path(project_root)
    if db_path.exists():
        from dango.platform.scheduling.history import get_schedule_history

        runs, _total = get_schedule_history(db_path, name, limit=5)
        if runs:
            console.print("\n[bold]Recent runs:[/bold]")
            for run in runs:
                status = run.get("status", "?")
                started = run.get("started_at", "?")
                color = "green" if status == "success" else "red"
                console.print(f"  {started} — [{color}]{status}[/{color}]")
        else:
            console.print("\n[dim]No run history yet.[/dim]")
    else:
        console.print("\n[dim]No run history yet.[/dim]")


@schedule.command("status")
@click.argument("name", required=False, default=None)
@click.pass_context
def schedule_status(ctx: click.Context, name: str | None = None) -> None:
    """Show scheduler status overview, or details for a single schedule."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    data = _load_schedules_yaml(project_root)
    schedules = data.get("schedules", [])

    if not schedules:
        console.print(
            "[dim]No schedules configured.[/dim] Run [bold]dango schedule add[/bold] to create one."
        )
        return

    # Per-schedule detail mode (BUG-041)
    if name is not None:
        _show_schedule_detail(project_root, schedules, name)
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
        errors, warnings = validate_schedules(sched_models, all_sources)
        for e in errors:
            console.print(f"[red]Error:[/red] {e}")
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

    # 1. Name (validate after prompt to avoid per-keystroke flicker)
    while True:
        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "name",
                    message="Schedule name (lowercase, alphanumeric + underscore)",
                ),
            ]
        )
        if answers is None:
            return
        name: str = answers["name"]
        if not _SLUG_RE.match(name):
            console.print(
                "[red]Invalid name.[/red] Must be lowercase, start with a letter, "
                "and contain only letters, digits, and underscores."
            )
            continue
        if name in existing_names:
            console.print(f"[red]Name '{name}' already exists.[/red] Choose a different name.")
            continue
        break

    # 2. Type (BUG-037: friendly labels, default to sync)
    type_labels = [label for label, _ in _TYPE_CHOICES]
    answers = inquirer.prompt([inquirer.List("type", message="Schedule type", choices=type_labels)])
    if answers is None:
        return
    sched_type: str = dict(_TYPE_CHOICES)[answers["type"]]

    # 3a. Sources (sync only) / 3b. dbt command
    sources: list[str] = []
    dbt_command: str | None = None
    if sched_type in ("sync", "sync_only"):
        try:
            from dango.config.loader import ConfigLoader

            loader = ConfigLoader(project_root)
            sources_cfg = loader.load_sources_config()
            available = [s.name for s in sources_cfg.sources if s.enabled]
        except Exception:
            available = []
        # BUG-038: auto-select if only one source
        if len(available) == 1:
            sources = [available[0]]
            console.print(f"[green]Auto-selected source: {available[0]}[/green]")
        elif available:
            while True:
                answers = inquirer.prompt(
                    [
                        inquirer.Checkbox(
                            "sources",
                            message="Select sources to sync (SPACE to toggle, ENTER to confirm)",
                            choices=available,
                        )
                    ]
                )
                if answers is None:
                    return
                sources = answers["sources"]
                if sources:
                    break
                console.print("[red]Please select at least one source.[/red]")
        if not sources:
            console.print("[red]Error:[/red] Sync schedules require at least one source.")
            return
    else:
        answers = inquirer.prompt(
            [
                inquirer.Text(
                    "dbt_command",
                    message="dbt command (e.g., run +model+, run --select tag:daily)",
                    default="run",
                )
            ]
        )
        if answers is None:
            return
        dbt_command = answers["dbt_command"]

    # 4. Frequency (BUG-039: time customization with preview)
    freq_labels = [label for label, _ in _FREQUENCY_CHOICES]
    answers = inquirer.prompt(
        [inquirer.List("frequency", message="Run frequency", choices=freq_labels)]
    )
    if answers is None:
        return
    selection: str = dict(_FREQUENCY_CHOICES)[answers["frequency"]]

    cron = _build_cron_interactive(selection)
    if cron is None:
        return

    _show_next_runs(cron)

    # 5. Timezone
    answers = inquirer.prompt(
        [inquirer.Text("timezone", message="Timezone", default=_get_local_timezone())]
    )
    if answers is None:
        return
    timezone_str: str = answers["timezone"]

    # 6. Notify on (BUG-040: skip if no webhooks configured)
    notify_on: list[str] = []
    try:
        from dango.platform.notifications.webhook import load_notification_config

        notif_cfg = load_notification_config(project_root)
        has_webhooks = notif_cfg is not None and len(notif_cfg.webhooks) > 0
    except Exception:
        has_webhooks = False

    if has_webhooks:
        answers = inquirer.prompt(
            [
                inquirer.Checkbox(
                    "notify_on",
                    message="Notify on (select events)",
                    choices=["failure", "success", "stale"],
                    default=["failure", "success", "stale"],
                )
            ]
        )
        if answers is None:
            return
        notify_on = answers["notify_on"]
    else:
        console.print(
            "[dim]Tip: Configure webhooks in .dango/schedules.yml to receive schedule alerts[/dim]"
        )

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
