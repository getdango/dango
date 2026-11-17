"""tests/unit/test_store_cut_order.py

Error and tool output is redacted before it is cut to a length limit, so a secret straddling
the cut is never left partly visible in stored or broadcast text.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.config.models import SourceType

pytestmark = pytest.mark.unit

SECRET = "sk_live_ABCDEFGHIJKLMNOP"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(f"STRIPE_KEY={SECRET}\n", encoding="utf-8")
    return tmp_path


def _straddling(cut: int) -> str:
    """Text whose known secret begins 8 characters before the cut."""
    return "x" * (cut - 8) + SECRET + " tail"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_crash_record_redacts_before_cutting(project: Path) -> None:
    from dango.platform.sync_process import _handle_crash

    _handle_crash(project, ["src"], 1, None, _straddling(1000))
    activity = _read(project / ".dango" / "logs" / "activity.jsonl")
    assert "sk_live" not in activity
    history = _read(project / ".dango" / "history" / "src.json")
    assert "sk_live" not in history

    _handle_crash(project, ["src2"], 1, None, _straddling(2000))
    assert "sk_live" not in _read(project / ".dango" / "history" / "src2.json")


def test_dbt_transform_error_redacts_before_cutting(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from dango.ingestion import dlt_runner
    from dango.utils.sync_history import save_sync_history_entry

    save_sync_history_entry(project, "good", {"status": "success"})
    ok = {"status": "success", "source": "good", "rows_loaded": 1}
    src = MagicMock()
    src.name = "good"
    src.enabled = True
    src.type = SourceType.HUBSPOT
    with (
        patch.object(dlt_runner.DltPipelineRunner, "run_source", return_value=ok),
        patch("dango.transformation.run_dbt_models", return_value=(False, _straddling(500))),
        patch("dango.transformation.generator.DbtModelGenerator"),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes", return_value=False
        ),
        patch("dango.utils.dbt_lock.DbtLock"),
        patch("dango.utils.post_sync.dispatch_post_sync_hooks", return_value=None),
        patch.object(dlt_runner, "_connect_with_lock_retry"),
        patch.object(dlt_runner, "console"),
    ):
        summary = dlt_runner.run_sync(project, [src])
    assert summary["transform_status"] == "failed"
    assert "sk_live" not in json.dumps(summary["transform_error"])
    assert "sk_live" not in _read(project / ".dango" / "history" / "good.json")
    assert "dbt_transform_failed" in caplog.text
    assert "sk_live" not in caplog.text


def test_dbt_snapshot_failure_log_redacts_before_cutting(project: Path) -> None:
    from dango.utils import post_sync

    (project / "dbt" / "snapshots").mkdir(parents=True)
    (project / "dbt" / "snapshots" / "s.sql").write_text("select 1", encoding="utf-8")
    with (
        patch("dango.transformation.run_dbt_snapshots", return_value=(False, _straddling(500))),
        patch.object(post_sync, "logger") as log,
    ):
        post_sync._run_dbt_snapshots(project)
    assert "sk_live" not in json.dumps(log.error.call_args.kwargs, default=str)


def test_dbt_model_route_redacts_before_cutting(project: Path) -> None:
    from dango.web.routes import dbt as dbt_route

    failed = subprocess.CompletedProcess([], 1, stdout="", stderr=_straddling(200))
    broadcast = AsyncMock()
    with (
        patch.object(dbt_route, "get_project_root", return_value=project),
        patch("dango.utils.DbtLock"),
        patch("dango.transformation._get_dbt_executable", return_value="dbt"),
        patch("dango.transformation._dbt_telemetry_env", return_value={}),
        patch.object(dbt_route.subprocess, "run", return_value=failed),
        patch.object(dbt_route.ws_manager, "broadcast", broadcast),
    ):
        asyncio.run(dbt_route.run_dbt_model_task("m", False))
    sent: Any = [c.args[0] for c in broadcast.call_args_list]
    assert "sk_live" not in json.dumps(sent)
    assert "sk_live" not in _read(project / ".dango" / "logs" / "activity.jsonl")


def test_dbt_model_route_exception_is_redacted(project: Path) -> None:
    from dango.web.routes import dbt as dbt_route

    broadcast = AsyncMock()
    with (
        patch.object(dbt_route, "get_project_root", return_value=project),
        patch("dango.utils.DbtLock"),
        patch("dango.transformation._get_dbt_executable", return_value="dbt"),
        patch("dango.transformation._dbt_telemetry_env", return_value={}),
        patch.object(dbt_route.subprocess, "run", side_effect=RuntimeError(_straddling(200))),
        patch.object(dbt_route.ws_manager, "broadcast", broadcast),
    ):
        asyncio.run(dbt_route.run_dbt_model_task("m", False))
    sent: Any = [c.args[0] for c in broadcast.call_args_list]
    assert "sk_live" not in json.dumps(sent)
    assert "sk_live" not in _read(project / ".dango" / "logs" / "activity.jsonl")
