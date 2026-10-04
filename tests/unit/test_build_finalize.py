"""tests/unit/test_build_finalize.py

Tests for dango/transformation/build_finalize.py: run_results summary and the
shared post-build steps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from dango.transformation.build_finalize import finalize_dbt_build, summarize_run_results

_UMS = "dango.utils.dbt_status.update_model_status"
_UPS = "dango.cli.schema_manager.update_model_schemas"
_MB_REFRESH = "dango.visualization.metabase.refresh_metabase_connection"
_MB_SYNC = "dango.visualization.metabase.sync_metabase_schema"


def _write_results(root: Path, results: list[dict[str, Any]]) -> None:
    target = root / "dbt" / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "run_results.json").write_text(
        json.dumps({"elapsed_time": 2.5, "args": {}, "results": results})
    )


@pytest.mark.unit
def test_summarize_run_results_counts_and_failed(tmp_path: Path) -> None:
    _write_results(
        tmp_path,
        [
            {
                "unique_id": "model.proj.stg_a",
                "status": "success",
                "message": "OK",
                "execution_time": 0.2,
                "failures": None,
            },
            {
                "unique_id": "model.proj.fct_b",
                "status": "error",
                "message": "x" * 5000,
                "execution_time": 0.1,
                "failures": None,
            },
            {
                "unique_id": "test.proj.not_null_fct_b_id.f00ba4",
                "status": "fail",
                "message": "Got 3 results",
                "execution_time": 0.1,
                "failures": 3,
            },
            {
                "unique_id": "test.proj.unique_stg_a_id.abc",
                "status": "pass",
                "message": None,
                "execution_time": 0.1,
                "failures": 0,
            },
        ],
    )
    out = summarize_run_results(tmp_path)
    assert out is not None
    assert out["elapsed_seconds"] == 2.5
    assert out["counts"]["success"] == 1
    assert out["counts"]["error"] == 1
    assert out["counts"]["fail"] == 1
    assert out["counts"]["pass"] == 1
    assert out["counts"]["warn"] == 0
    assert [(n["resource_type"], n["name"]) for n in out["failed"]] == [
        ("model", "fct_b"),
        ("test", "not_null_fct_b_id"),
    ]
    assert len(out["failed"][0]["message"]) == 2000
    assert out["failed"][1]["failures"] == 3


@pytest.mark.unit
def test_summarize_since_stale_vs_fresh(tmp_path: Path) -> None:
    import os
    import time

    _write_results(tmp_path, [{"unique_id": "model.p.a", "status": "success"}])
    path = tmp_path / "dbt" / "target" / "run_results.json"
    now = time.time()
    os.utime(path, (now - 100, now - 100))
    assert summarize_run_results(tmp_path, since=now) is None
    assert summarize_run_results(tmp_path) is not None  # no since: unchanged behaviour
    os.utime(path, (now + 1, now + 1))
    fresh = summarize_run_results(tmp_path, since=now)
    assert fresh is not None and fresh["counts"]["success"] == 1


@pytest.mark.unit
def test_summarize_missing_or_corrupt_returns_none(tmp_path: Path) -> None:
    assert summarize_run_results(tmp_path) is None
    target = tmp_path / "dbt" / "target"
    target.mkdir(parents=True)
    (target / "run_results.json").write_text("{not json")
    assert summarize_run_results(tmp_path) is None


@pytest.mark.unit
def test_finalize_failure_updates_status_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, schemas, refresh, sync = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(_UMS, status)
    monkeypatch.setattr(_UPS, schemas)
    monkeypatch.setattr(_MB_REFRESH, refresh)
    monkeypatch.setattr(_MB_SYNC, sync)
    out = finalize_dbt_build(tmp_path, success=False)
    status.assert_called_once_with(tmp_path)
    schemas.assert_not_called()
    refresh.assert_not_called()
    sync.assert_not_called()
    assert out["model_status"] == "updated"
    assert out["schema_sync"] == "skipped"
    assert out["metabase"] == "skipped"


@pytest.mark.unit
def test_finalize_success_runs_all_and_collects_nested_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = tmp_path / "dbt" / "models"
    (models / "marts" / "finance").mkdir(parents=True)
    (models / "marts" / "finance" / "x.sql").write_text("select 1")
    (models / "marts" / "_skip.sql").write_text("select 1")
    (models / "intermediate").mkdir()
    (models / "intermediate" / "int_y.sql").write_text("select 1")
    (models / "staging").mkdir()
    (models / "staging" / "stg_z.sql").write_text("select 1")
    schemas = MagicMock()
    sync = MagicMock(return_value=True)
    monkeypatch.setattr(_UMS, MagicMock())
    monkeypatch.setattr(_UPS, schemas)
    monkeypatch.setattr(_MB_REFRESH, MagicMock(return_value=(True, None, "sid")))
    monkeypatch.setattr(_MB_SYNC, sync)
    out = finalize_dbt_build(tmp_path, success=True)
    args = schemas.call_args.args
    assert args[0] == tmp_path
    assert sorted(args[1]) == ["int_y", "x"]
    sync.assert_called_once_with(tmp_path, existing_session_id="sid")
    assert out["schema_sync"] == "updated"
    assert out["metabase"] == "refreshed"
    assert out["metabase_schema_synced"] is True


@pytest.mark.unit
def test_finalize_metabase_not_running_and_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_UMS, MagicMock())
    monkeypatch.setattr(_UPS, MagicMock())
    refresh = MagicMock(return_value=(False, "down", None))
    monkeypatch.setattr(_MB_REFRESH, refresh)
    assert finalize_dbt_build(tmp_path, success=True)["metabase"] == "not_running"
    refresh.reset_mock()
    assert finalize_dbt_build(tmp_path, success=True, refresh_metabase=False)["metabase"] == (
        "skipped"
    )
    refresh.assert_not_called()


@pytest.mark.unit
def test_finalize_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "dbt" / "models" / "marts").mkdir(parents=True)
    (tmp_path / "dbt" / "models" / "marts" / "m.sql").write_text("select 1")
    boom = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(_UMS, boom)
    monkeypatch.setattr(_UPS, boom)
    monkeypatch.setattr(_MB_REFRESH, boom)
    out = finalize_dbt_build(tmp_path, success=True)
    assert out["model_status"] == "error"
    assert out["schema_sync"] == "error"
    assert out["metabase"] == "skipped"
