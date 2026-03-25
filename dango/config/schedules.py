"""dango/config/schedules.py

Schedule configuration models, validation, and reload logic.

Defines the ``schedules:`` section of ``.dango/schedules.yml`` as Pydantic
models, validates cron expressions and source references, and provides a
reload function that diffs YAML configs against running APScheduler jobs.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from dango.config.exceptions import ConfigValidationError
from dango.logging import get_logger

if TYPE_CHECKING:
    from dango.platform.scheduling.scheduler import SchedulerService

logger = get_logger(__name__)

__all__ = [
    "CRON_PRESETS",
    "ReloadResult",
    "ScheduleConfig",
    "ScheduleType",
    "SchedulesConfig",
    "get_schedule_job_id",
    "load_schedules_config",
    "log_startup_checks",
    "reload_schedules",
    "save_schedules_config",
    "validate_schedules",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CRON_PRESETS: dict[str, str] = {
    "every_15m": "*/15 * * * *",
    "every_hour": "0 * * * *",
    "every_6h": "0 */6 * * *",
    "daily": "0 6 * * *",
    "weekly": "0 6 * * 1",
}

_SCHEDULE_JOB_PREFIX = "schedule:"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_VALID_NOTIFY_ON = frozenset({"failure", "success", "stale"})


def get_schedule_job_id(schedule_name: str) -> str:
    """Return the APScheduler job ID for a schedule name.

    Downstream consumers (TASK-038 CRUD API, TASK-041 sync wiring) should use
    this instead of hard-coding the ``schedule:`` prefix.
    """
    return f"{_SCHEDULE_JOB_PREFIX}{schedule_name}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ScheduleType(str, Enum):
    """Type of scheduled job."""

    SYNC = "sync"
    DBT = "dbt"


class ScheduleConfig(BaseModel):
    """Configuration for a single scheduled job."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: ScheduleType = ScheduleType.SYNC
    cron: str
    sources: list[str] = []
    enabled: bool = True
    timezone: str | None = None
    start_date: datetime | None = None
    misfire_grace_time: int | None = None
    timeout_minutes: int | None = None
    notify_on: list[str] = []
    dbt_command: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            msg = (
                f"Schedule name must be lowercase alphanumeric + underscore, "
                f"starting with a letter. Got: {v!r}"
            )
            raise ValueError(msg)
        return v

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        resolved = CRON_PRESETS.get(v, v)
        from croniter import croniter

        if not croniter.is_valid(resolved):
            msg = f"Invalid cron expression: {v!r}"
            if v in CRON_PRESETS:
                msg += f" (resolved from preset to {resolved!r})"
            raise ValueError(msg)
        return resolved

    @field_validator("notify_on")
    @classmethod
    def _validate_notify_on(cls, v: list[str]) -> list[str]:
        invalid = set(v) - _VALID_NOTIFY_ON
        if invalid:
            msg = f"Invalid notify_on values: {sorted(invalid)}. Valid: {sorted(_VALID_NOTIFY_ON)}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _validate_type_fields(self) -> ScheduleConfig:
        if self.type == ScheduleType.SYNC and not self.sources:
            msg = "Sync schedules must specify at least one source"
            raise ValueError(msg)
        if self.type == ScheduleType.DBT and not self.dbt_command:
            msg = "dbt schedules must specify a dbt_command"
            raise ValueError(msg)
        return self

    def get_notify_on_dict(self) -> dict[str, bool] | None:
        """Convert ``notify_on`` list to dict for ``webhook.should_notify()`` interop.

        Returns ``None`` if ``notify_on`` is empty (use global defaults).
        """
        if not self.notify_on:
            return None
        return {
            "on_failure": "failure" in self.notify_on,
            "on_success": "success" in self.notify_on,
            "on_stale": "stale" in self.notify_on,
        }


class SchedulesConfig(BaseModel):
    """Top-level container for schedule definitions."""

    model_config = ConfigDict(frozen=True)

    schedules: list[ScheduleConfig] = []


class ReloadResult(BaseModel):
    """Result of a schedule reload operation.

    Note: ``unchanged`` is always empty in the current implementation because
    APScheduler 3.x has no cheap way to diff trigger parameters. All existing
    jobs that remain in config are treated as ``updated`` (remove + re-add).
    """

    model_config = ConfigDict(frozen=True)

    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    unchanged: list[str] = []


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_schedules_config(project_root: Path) -> SchedulesConfig:
    """Load schedule config from ``.dango/schedules.yml``.

    Returns an empty ``SchedulesConfig`` if the file is missing.

    Raises:
        ConfigValidationError: If the file exists but contains invalid data.
    """
    path = project_root / ".dango" / "schedules.yml"
    if not path.exists():
        return SchedulesConfig()

    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigValidationError(f"Invalid YAML in {path}:\n{e}") from e

    schedules_data = data.get("schedules")
    if schedules_data is None:
        return SchedulesConfig()

    try:
        return SchedulesConfig(schedules=schedules_data)
    except Exception as e:
        raise ConfigValidationError(f"Invalid schedule configuration in {path}:\n{e}") from e


def save_schedules_config(project_root: Path, config: SchedulesConfig) -> None:
    """Save schedule config to ``.dango/schedules.yml``.

    Serialises the config with ``mode="json"`` so datetimes become ISO strings.
    """
    path = project_root / ".dango" / "schedules.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "schedules": [s.model_dump(exclude_none=True, mode="json") for s in config.schedules]
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------


def validate_schedules(
    schedules: list[ScheduleConfig],
    source_names: set[str],
    average_durations: dict[str, float] | None = None,
) -> list[str]:
    """Cross-validate schedules against known sources.

    Returns a list of error/warning strings (empty = all good).

    Checks:
    1. Duplicate schedule names (error)
    2. Unknown source references (error)
    3. Cron interval < avg duration * 0.8 (warning, when durations provided)
    4. Overlapping crons for shared sources (warning)
    """
    issues: list[str] = []

    # 1. Duplicate names
    seen_names: dict[str, int] = {}
    for sched in schedules:
        seen_names[sched.name] = seen_names.get(sched.name, 0) + 1
    for name, count in seen_names.items():
        if count > 1:
            issues.append(f"Duplicate schedule name: {name!r} (appears {count} times)")

    # 2. Unknown sources
    for sched in schedules:
        for src in sched.sources:
            if src not in source_names:
                issues.append(f"Schedule {sched.name!r} references unknown source: {src!r}")

    # 3. Interval vs duration check
    if average_durations:
        for sched in schedules:
            if sched.type != ScheduleType.SYNC:
                continue
            interval = _get_cron_interval_seconds(sched.cron)
            for src in sched.sources:
                avg = average_durations.get(src)
                if avg is not None and interval < avg * 0.8:
                    issues.append(
                        f"Schedule {sched.name!r}: cron interval ({interval:.0f}s) "
                        f"is less than 80% of avg duration for {src!r} ({avg:.0f}s)"
                    )

    # 4. Overlapping crons for shared sources
    issues.extend(_detect_overlaps(schedules))

    return issues


def _get_cron_interval_seconds(cron_expr: str, sample_count: int = 5) -> float:
    """Return the minimum interval between consecutive cron runs in seconds.

    Samples ``sample_count`` consecutive intervals and returns the smallest one.
    This handles non-uniform crons like ``0 6,18 * * *`` (12h and 12h gap) or
    ``0 9-17 * * 1-5`` where sampling only two ticks might see a long weekend gap.
    """
    from croniter import croniter

    it = croniter(cron_expr)
    prev: float = it.get_next(float)
    min_interval = float("inf")
    for _ in range(sample_count):
        nxt: float = it.get_next(float)
        gap = nxt - prev
        if gap < min_interval:
            min_interval = gap
        prev = nxt
    return min_interval


def _detect_overlaps(
    schedules: list[ScheduleConfig],
    check_count: int = 10,
    proximity_seconds: int = 60,
) -> list[str]:
    """Detect schedules with overlapping cron times for shared sources."""
    from croniter import croniter

    # Build source→schedule mapping (only enabled sync schedules)
    source_schedules: dict[str, list[ScheduleConfig]] = {}
    for sched in schedules:
        if not sched.enabled or sched.type != ScheduleType.SYNC:
            continue
        for src in sched.sources:
            source_schedules.setdefault(src, []).append(sched)

    warnings: list[str] = []
    checked_pairs: set[tuple[str, ...]] = set()

    for src, scheds in source_schedules.items():
        if len(scheds) < 2:
            continue
        for i, s1 in enumerate(scheds):
            for s2 in scheds[i + 1 :]:
                pair_key = tuple(sorted([s1.name, s2.name]))
                if pair_key in checked_pairs:
                    continue
                checked_pairs.add(pair_key)

                it1 = croniter(s1.cron)
                it2 = croniter(s2.cron)
                times1 = [it1.get_next(float) for _ in range(check_count)]
                times2 = [it2.get_next(float) for _ in range(check_count)]

                for t1 in times1:
                    for t2 in times2:
                        if abs(t1 - t2) < proximity_seconds:
                            warnings.append(
                                f"Schedules {s1.name!r} and {s2.name!r} have "
                                f"overlapping cron times for shared source {src!r}"
                            )
                            break
                    else:
                        continue
                    break

    return warnings


# ---------------------------------------------------------------------------
# Startup logging
# ---------------------------------------------------------------------------


def log_startup_checks(
    schedules: list[ScheduleConfig],
    source_names: set[str],
    project_root: Path,
) -> None:
    """Log INFO for unscheduled sources and WARNING for cloud conflicts."""
    if not schedules:
        logger.info("no_schedules_configured")
        return

    # Unscheduled sources
    scheduled_sources: set[str] = set()
    for sched in schedules:
        scheduled_sources.update(sched.sources)

    unscheduled = source_names - scheduled_sources
    if unscheduled:
        logger.info(
            "unscheduled_sources",
            sources=sorted(unscheduled),
        )

    # Cloud conflict check
    try:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(project_root)
        cloud_cfg = loader.load_cloud_config()
        if cloud_cfg is not None and cloud_cfg.droplet_ip is not None:
            logger.warning(
                "cloud_schedule_conflict",
                message=(
                    "A cloud deployment is active. Local schedules may conflict "
                    "with cloud-side scheduling."
                ),
            )
    except Exception:  # noqa: BLE001
        logger.debug("cloud_config_check_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


def reload_schedules(
    scheduler: SchedulerService,
    new_schedules: list[ScheduleConfig],
    project_root: Path,
) -> ReloadResult:
    """Diff running jobs against config and apply changes.

    Only touches jobs with the ``schedule:`` ID prefix.

    Returns a ``ReloadResult`` summarizing what changed.
    """
    from apscheduler.triggers.cron import CronTrigger

    from dango.platform.scheduling.jobs import run_scheduled_dbt, run_scheduled_sync

    # Collect current schedule jobs
    existing_jobs: dict[str, Any] = {}
    for job in scheduler.get_jobs():
        if job.id.startswith(_SCHEDULE_JOB_PREFIX):
            existing_jobs[job.id] = job

    # Build desired state from enabled schedules
    desired: dict[str, ScheduleConfig] = {}
    for sched in new_schedules:
        if sched.enabled:
            desired[get_schedule_job_id(sched.name)] = sched

    existing_ids = set(existing_jobs.keys())
    desired_ids = set(desired.keys())

    added: list[str] = []
    removed: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []

    # Remove jobs no longer in config
    for job_id in existing_ids - desired_ids:
        scheduler.remove_job(job_id)
        name = job_id.removeprefix(_SCHEDULE_JOB_PREFIX)
        removed.append(name)

    # Add or update
    for job_id, sched in desired.items():
        trigger_kwargs: dict[str, Any] = {}
        if sched.timezone:
            trigger_kwargs["timezone"] = sched.timezone
        trigger = CronTrigger.from_crontab(sched.cron, **trigger_kwargs)

        job_kwargs: dict[str, Any] = {
            "id": job_id,
            "name": sched.name,
        }
        if sched.misfire_grace_time is not None:
            job_kwargs["misfire_grace_time"] = sched.misfire_grace_time
        if sched.start_date is not None:
            job_kwargs["next_run_time"] = sched.start_date

        func: Any
        func_kwargs: dict[str, Any]
        if sched.type == ScheduleType.SYNC:
            func = run_scheduled_sync
            func_kwargs = {
                "schedule_name": sched.name,
                "sources": list(sched.sources),
                "project_root": str(project_root),
            }
        else:
            func = run_scheduled_dbt
            func_kwargs = {
                "schedule_name": sched.name,
                "dbt_command": sched.dbt_command,
                "project_root": str(project_root),
            }

        if job_id in existing_ids:
            # Update: remove then re-add (APScheduler 3.x has no atomic trigger update)
            scheduler.remove_job(job_id)
            updated.append(sched.name)
        else:
            added.append(sched.name)

        scheduler.add_job(func, trigger, kwargs=func_kwargs, **job_kwargs)

    return ReloadResult(
        added=added,
        updated=updated,
        removed=removed,
        unchanged=unchanged,
    )
