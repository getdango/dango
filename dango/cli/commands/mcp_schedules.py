"""dango/cli/commands/mcp_schedules.py

MCP schedule tools: create, inspect, modify, enable/disable, remove, and activate schedules.

Import discipline mirrors mcp_mutations.py: at module top level only stdlib,
`_get_project_root`/`_git_warnings` from mcp_helpers, and the `mcp` instance.
Every other dango.* import is lazy inside function bodies. The MCP server
speaks JSON-RPC over stdio, so nothing here may write to stdout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root, _git_warnings
from dango.cli.commands.mcp_server import mcp

# ── Helpers ───────────────────────────────────────────────────────────────────


def _next_run_iso(sched: Any) -> str | None:
    """Next fire time for an enabled schedule, computed with the SAME trigger
    construction reload_schedules() uses, so it matches what APScheduler will do."""
    if not sched.enabled:
        return None
    try:
        from datetime import datetime

        from apscheduler.triggers.cron import CronTrigger

        trigger_kwargs: dict[str, Any] = {}
        if sched.timezone:
            trigger_kwargs["timezone"] = sched.timezone
        trigger = CronTrigger.from_crontab(sched.cron, **trigger_kwargs)
        nxt = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
        return nxt.isoformat() if nxt is not None else None
    except Exception:  # noqa: BLE001 -- display helper, never fail the tool
        return None


def _schedule_dict(sched: Any) -> dict[str, Any]:
    return {
        "name": sched.name,
        "type": sched.type.value,
        "cron": sched.cron,
        "timezone": sched.timezone,
        "sources": list(sched.sources),
        "enabled": sched.enabled,
        "dbt_command": sched.dbt_command,
        "script_path": sched.script_path,
        "timeout_minutes": sched.timeout_minutes,
        "next_run": _next_run_iso(sched),
    }


def _server_running(project_root: Path) -> bool:
    """True only if THIS project's own web server (per .dango/web.pid) is alive.
    Never trust 'something is listening on the port' alone — another Dango project
    may own the default port 8800."""
    try:
        from dango.cli.helpers.process_manager import read_pid_record_for_project
        from dango.utils.process import is_process_running

        record = read_pid_record_for_project(project_root)
        if record is None:
            return False
        return is_process_running(record.pid, expected_start_time=record.start_time)
    except Exception:  # noqa: BLE001
        return False


def _activate(project_root: Path) -> dict[str, Any]:
    """Reload this project's running scheduler. Never raises, never prints.

    Returns {"activation": "reloaded" | "server_not_running" | "reload_failed",
             "activation_detail": str}
    """
    if not _server_running(project_root):
        return {
            "activation": "server_not_running",
            "activation_detail": "Saved. Dango is not running for this project; the schedule "
            "will be activated on the next `dango start`.",
        }
    try:
        import requests

        from dango.config import ConfigLoader

        port = ConfigLoader(project_root).load_config().platform.port
        resp = requests.post(
            f"http://localhost:{port}/api/internal/schedules/reload",
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001
        return {"activation": "reload_failed", "activation_detail": f"Reload request failed: {e}"}
    if resp.status_code == 200:
        return {"activation": "reloaded", "activation_detail": "Running scheduler reloaded."}
    return {
        "activation": "reload_failed",
        "activation_detail": f"Scheduler reload returned HTTP {resp.status_code}; restart "
        "`dango start` to apply.",
    }


def _load_source_names(project_root: Path) -> set[str]:
    """Names of configured sources. Raises on a missing/invalid project config."""
    from dango.config.helpers import load_config

    return {s.name for s in load_config(project_root).sources.sources}


def _check_timezone(timezone: str) -> str | None:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception:  # noqa: BLE001
        return f"Unknown timezone: '{timezone}'"
    return None


def _check_sources(project_root: Path, sources: list[str]) -> str | None:
    """Agent-friendly unknown-source error (validate_schedules is the backstop)."""
    known = _load_source_names(project_root)
    for s in sources:
        if s not in known:
            return f"Source '{s}' not found in sources.yml. Add it first with add_source()."
    return None


def _save_validated(
    project_root: Path, schedules: list[Any], name: str
) -> tuple[str | None, list[str]]:
    """Cross-validate the proposed full list, then save.

    Only errors that concern `name` (the error text contains repr(name)) block the
    write; unrelated pre-existing errors are returned as warnings so one broken
    schedule never traps the user. Returns (error_or_None, warnings).
    """
    from dango.config.schedules import SchedulesConfig, save_schedules_config, validate_schedules

    errors, _warn = validate_schedules(schedules, _load_source_names(project_root))
    mine = [e for e in errors if repr(name) in e]
    if mine:
        return "; ".join(mine), []
    save_schedules_config(project_root, SchedulesConfig(schedules=schedules))
    return None, [e for e in errors if e not in mine]


def _success(
    project_root: Path, status: str, sched: Any, warnings: list[str] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if sched is not None:
        result["schedule"] = _schedule_dict(sched)
        result["schedule_name"] = sched.name
    result.update(_activate(project_root))
    if warnings:
        result["warnings"] = warnings
    if git_warning := _git_warnings(project_root):
        result["git_warning"] = git_warning
    return result


def _rebuild(old: Any, **changes: Any) -> Any:
    """Re-run ScheduleConfig validation (frozen model; model_copy skips validators)."""
    from dango.config.schedules import ScheduleConfig

    return ScheduleConfig(**{**old.model_dump(), **changes})


def _find(schedules: list[Any], name: str) -> int | None:
    for i, s in enumerate(schedules):
        if s.name == name:
            return i
    return None


def _not_found(name: str) -> dict[str, Any]:
    return {"error": f"Schedule '{name}' not found"}


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def list_schedules() -> dict[str, Any]:
    """List all schedules with their settings and next run time.

    `next_run` is computed from the cron + timezone and is only live if
    `server_running` is true and the schedule is `enabled`. `scheduler` is the
    running scheduler's own status (running, job_count, next_run_time) when this
    project's server is up, otherwise null.

    Returns dict with: schedules, server_running, scheduler.
    """
    from dango.config.exceptions import ConfigValidationError
    from dango.config.schedules import load_schedules_config

    project_root = _get_project_root()
    try:
        schedules = load_schedules_config(project_root).schedules
    except ConfigValidationError as e:
        return {"error": str(e)}

    running = _server_running(project_root)
    scheduler: dict[str, Any] | None = None
    if running:
        try:
            import requests

            from dango.config import ConfigLoader

            port = ConfigLoader(project_root).load_config().platform.port
            resp = requests.get(f"http://localhost:{port}/api/health/platform", timeout=3)
            sched_info = resp.json().get("scheduler")
            scheduler = sched_info if isinstance(sched_info, dict) else None
        except Exception:  # noqa: BLE001
            scheduler = None
    return {
        "schedules": [_schedule_dict(s) for s in schedules],
        "server_running": running,
        "scheduler": scheduler,
    }


@mcp.tool()
def add_schedule(
    schedule_name: str,
    cron: str,
    sources: list[str],
    timezone: str = "UTC",
    skip_dbt: bool = False,
) -> dict[str, Any]:
    """Add a new sync schedule.

    Args:
        schedule_name: Unique name for this schedule (lowercase_with_underscores)
        cron: Cron expression (e.g. "0 7 * * *" for 7am daily).
              Common presets: "0 * * * *" (hourly), "0 7 * * *" (daily 7am),
              "0 7 * * 1-5" (weekdays 7am)
        sources: List of source names to sync on this schedule
        timezone: Timezone for the cron (e.g. "Asia/Singapore", "US/Eastern"). Default UTC.
        skip_dbt: If True, sync only — do not run dbt transforms after sync.

    Returns dict with: status, schedule (incl. next_run), activation
    (reloaded / server_not_running / reload_failed), activation_detail, git_warning.
    """
    from pydantic import ValidationError

    from dango.config.exceptions import ConfigError
    from dango.config.schedules import ScheduleConfig, ScheduleType, load_schedules_config

    project_root = _get_project_root()
    try:
        schedules = list(load_schedules_config(project_root).schedules)
        if _find(schedules, schedule_name) is not None:
            return {"error": f"Schedule '{schedule_name}' already exists"}
        if tz_err := _check_timezone(timezone):
            return {"error": tz_err}
        if src_err := _check_sources(project_root, sources):
            return {"error": src_err}
        new = ScheduleConfig(
            name=schedule_name,
            cron=cron,
            sources=sources,
            timezone=timezone,
            type=ScheduleType.SYNC_ONLY if skip_dbt else ScheduleType.SYNC,
            enabled=True,
        )
        err, warnings = _save_validated(project_root, [*schedules, new], schedule_name)
    except (ValidationError, ValueError, ConfigError) as e:
        return {"error": str(e)}
    if err:
        return {"error": err}
    return _success(project_root, "created", new, warnings)


@mcp.tool()
def update_schedule(
    schedule_name: str,
    cron: str | None = None,
    sources: list[str] | None = None,
    timezone: str | None = None,
    skip_dbt: bool | None = None,
) -> dict[str, Any]:
    """Change an existing schedule's cron, sources, timezone, or dbt behaviour.

    Only the arguments you pass are changed; everything else is kept.

    Args:
        schedule_name: Name of the schedule to update
        cron: New cron expression
        sources: New list of source names
        timezone: New timezone (e.g. "Asia/Singapore")
        skip_dbt: True = sync only (no dbt after sync); False = sync then dbt

    Returns dict with: status, schedule (incl. next_run), activation,
    activation_detail, git_warning.
    """
    from pydantic import ValidationError

    from dango.config.exceptions import ConfigError
    from dango.config.schedules import ScheduleType, load_schedules_config

    project_root = _get_project_root()
    try:
        schedules = list(load_schedules_config(project_root).schedules)
        idx = _find(schedules, schedule_name)
        if idx is None:
            return _not_found(schedule_name)
        if cron is None and sources is None and timezone is None and skip_dbt is None:
            return {"error": "Nothing to update"}
        old = schedules[idx]
        changes: dict[str, Any] = {}
        if cron is not None:
            changes["cron"] = cron
        if sources is not None:
            if src_err := _check_sources(project_root, sources):
                return {"error": src_err}
            changes["sources"] = sources
        if timezone is not None:
            if tz_err := _check_timezone(timezone):
                return {"error": tz_err}
            changes["timezone"] = timezone
        if skip_dbt is not None:
            if old.type not in (ScheduleType.SYNC, ScheduleType.SYNC_ONLY):
                return {"error": "skip_dbt only applies to sync schedules"}
            changes["type"] = ScheduleType.SYNC_ONLY if skip_dbt else ScheduleType.SYNC
        new = _rebuild(old, **changes)
        schedules[idx] = new
        err, warnings = _save_validated(project_root, schedules, schedule_name)
    except (ValidationError, ValueError, ConfigError) as e:
        return {"error": str(e)}
    if err:
        return {"error": err}
    return _success(project_root, "updated", new, warnings)


@mcp.tool()
def set_schedule_enabled(schedule_name: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a schedule without deleting it.

    Disabling always works, even if another schedule in the file is broken.

    Args:
        schedule_name: Name of the schedule
        enabled: True to enable, False to disable

    Returns dict with: status (enabled / disabled / unchanged), schedule,
    activation, activation_detail, git_warning.
    """
    from pydantic import ValidationError

    from dango.config.exceptions import ConfigError
    from dango.config.schedules import SchedulesConfig, load_schedules_config, save_schedules_config

    project_root = _get_project_root()
    try:
        schedules = list(load_schedules_config(project_root).schedules)
        idx = _find(schedules, schedule_name)
        if idx is None:
            return _not_found(schedule_name)
        old = schedules[idx]
        if old.enabled == enabled:
            return {
                "status": "unchanged",
                "schedule": _schedule_dict(old),
                **_activate(project_root),
            }
        new = _rebuild(old, enabled=enabled)
        schedules[idx] = new
        warnings: list[str] = []
        if enabled:
            err, warnings = _save_validated(project_root, schedules, schedule_name)
            if err:
                return {"error": err}
        else:
            save_schedules_config(project_root, SchedulesConfig(schedules=schedules))
    except (ValidationError, ValueError, ConfigError) as e:
        return {"error": str(e)}
    return _success(project_root, "enabled" if enabled else "disabled", new, warnings)


@mcp.tool()
def remove_schedule(schedule_name: str) -> dict[str, Any]:
    """Permanently remove a schedule. Always works, even if other schedules are broken.

    Args:
        schedule_name: Name of the schedule to remove

    Returns dict with: status, schedule_name, activation, activation_detail, git_warning.
    """
    from dango.config.exceptions import ConfigError
    from dango.config.schedules import SchedulesConfig, load_schedules_config, save_schedules_config

    project_root = _get_project_root()
    try:
        schedules = list(load_schedules_config(project_root).schedules)
        idx = _find(schedules, schedule_name)
        if idx is None:
            return _not_found(schedule_name)
        del schedules[idx]
        save_schedules_config(project_root, SchedulesConfig(schedules=schedules))
    except ConfigError as e:
        return {"error": str(e)}
    result: dict[str, Any] = {"status": "removed", "schedule_name": schedule_name}
    result.update(_activate(project_root))
    if git_warning := _git_warnings(project_root):
        result["git_warning"] = git_warning
    return result


@mcp.tool()
def reload_schedules() -> dict[str, Any]:
    """Re-read schedules.yml and apply it to this project's running scheduler.

    Mutation tools already do this automatically; use this after editing the file by hand.

    Returns dict with: status, activation, activation_detail.
    """
    from dango.config.exceptions import ConfigError
    from dango.config.schedules import load_schedules_config

    project_root = _get_project_root()
    try:
        load_schedules_config(project_root)
    except ConfigError as e:
        return {"error": str(e)}
    return {"status": "ok", **_activate(project_root)}
