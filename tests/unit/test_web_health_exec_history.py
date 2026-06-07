"""tests/unit/test_web_health_exec_history.py

M3: execution_history (scheduler.db) failures surfaced in health data.
Tests that get_platform_health_data() checks execution_history for
schedule-level failures and adds them to failed_syncs.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _run_health(
    tmp: Path,
    recs: list[dict[str, Any]],
    src_cfg: list[dict[str, Any]] | None = None,
    sync_hist: list[dict[str, Any]] | None = None,
    create_db: bool = True,
) -> dict[str, Any]:
    """Run get_platform_health_data with all non-scheduler deps stubbed."""
    from dango.web.helpers import get_platform_health_data

    h, d = "dango.web.helpers", "dango.utils.db_health"
    stub_db = {
        "size_gb": 0,
        "size_mb": 0,
        "tables": 0,
        "status": "new",
        "raw_tables": 0,
        "staging_tables": 0,
        "marts_tables": 0,
    }
    stub_disk = {
        "free_gb": 50,
        "total_gb": 100,
        "used_gb": 50,
        "used_pct": 50.0,
        "status": "healthy",
    }
    if create_db:
        (tmp / ".dango").mkdir(parents=True, exist_ok=True)
        (tmp / ".dango" / "scheduler.db").touch()
    targets = {
        f"{h}.get_project_root": tmp,
        f"{h}.get_duckdb_path": tmp / "w.db",
        f"{d}.check_duckdb_health": stub_db,
        f"{d}.get_disk_usage_summary": stub_disk,
        f"{d}.get_component_disk_usage": {},
        f"{d}.get_duckdb_capacity": {},
        f"{h}.load_sources_config": src_cfg or [],
        "dango.platform.scheduling.history.get_recent_history": recs,
    }
    if sync_hist is not None:
        targets[f"{h}.load_sync_history"] = sync_hist
    with ExitStack() as es:
        for k, v in targets.items():
            es.enter_context(patch(k, MagicMock(return_value=v)))
        return asyncio.run(get_platform_health_data())


def _rec(
    status: str = "failed",
    sources: list[str] | None = None,
    started_at: str | None = None,
    error: str = "err",
) -> dict[str, Any]:
    """Build a minimal execution_history record."""
    return {
        "schedule_name": "s",
        "sources": sources or ["src"],
        "started_at": started_at or datetime.now(tz=timezone.utc).isoformat(),
        "status": status,
        "error": error,
    }


@pytest.mark.unit
class TestExecutionHistoryInHealth:
    """M3: execution_history failures surfaced in health data."""

    @pytest.mark.parametrize(
        "status,sources",
        [("failed", ["hubspot"]), ("timeout", ["fb"])],
    )
    def test_failed_and_timeout_surface(self, tmp_path, status, sources):
        r = _run_health(tmp_path, [_rec(status=status, sources=sources)])
        assert r["failed_syncs"][0]["source"] == sources[0]

    def test_dedup_with_sync_history(self, tmp_path):
        now = datetime.now(tz=timezone.utc).isoformat()
        r = _run_health(
            tmp_path,
            [_rec(sources=["hubspot", "stripe"])],
            src_cfg=[{"name": "hubspot", "type": "hubspot", "enabled": True}],
            sync_hist=[{"timestamp": now, "status": "failed", "error_message": "e"}],
        )
        srcs = [f["source"] for f in r["failed_syncs"]]
        assert srcs.count("hubspot") == 1 and "stripe" in srcs

    def test_old_failure_ignored(self, tmp_path):
        r = _run_health(tmp_path, [_rec(started_at="2020-01-01T00:00:00+00:00")])
        assert r["failed_syncs"] == []

    def test_success_hides_older_failure(self, tmp_path):
        now = datetime.now(tz=timezone.utc).isoformat()
        r = _run_health(
            tmp_path,
            [_rec(status="success", started_at=now), _rec(started_at=now)],
        )
        assert r["failed_syncs"] == []

    def test_missing_scheduler_db(self, tmp_path):
        assert _run_health(tmp_path, [], create_db=False)["failed_syncs"] == []

    def test_sources_as_json_string(self, tmp_path):
        """Handle sources as raw JSON string (defensive, in case _row_to_dict skipped)."""
        rec = _rec()
        rec["sources"] = json.dumps(["stripe"])  # raw JSON string, not list
        r = _run_health(tmp_path, [rec])
        assert r["failed_syncs"][0]["source"] == "stripe"
