"""tests/unit/test_file_source_partial_consumers.py

WP-67: what the CLI, the scheduler/web sync runner, the MCP tool and the dbt/hook steps do with a partial file source.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.ingestion.dlt_runner import run_sync
from dango.platform.sync_process import read_sync_status
from dango.utils.sync_history import load_sync_history
from tests.factories.partial_file_source import make_source, write_good, write_latin1
from tests.factories.scaffold_project import build_fresh_project, working_copy

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fresh_project(tmp_path_factory)


@pytest.fixture
def project(fresh_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The scaffold with source `demo` (csv, folder `inputs/`): a.csv, b.csv good, c.csv Latin-1."""
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    root = working_copy(fresh_project, tmp_path)
    write_good(root / "inputs" / "b.csv", ["5,b"])
    (root / "inputs" / "a.csv").write_text("id,item\n1,a\n", encoding="utf-8")
    write_latin1(root / "inputs" / "c.csv")
    return root


def _dango(root: Path, *args: str) -> tuple[int, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "COLUMNS": "400",
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, _ANSI.sub("", proc.stdout + proc.stderr)


def test_cli_sync_with_one_bad_file_exits_1_and_names_it(project: Path) -> None:
    code, output = _dango(project, "sync", "--yes")

    assert code == 1, output
    assert "Failed (1)" in output and "c.csv" in output
    assert "Sync failed for 1 source(s)" in output
    assert "Succeeded" not in output and "Synced demo" not in output
    assert "Running dbt models" not in output
    last = load_sync_history(project, "demo", limit=1)[0]
    assert last["status"] == "partial" and "c.csv" in last["error_message"]


def test_the_scheduler_and_web_runner_record_the_sync_as_failed(project: Path) -> None:
    from dango.platform.scheduling.sync_trigger import run_manual_sync

    result = run_manual_sync(
        project, ["demo"], write_progress=True, sync_id="wp67", skip_dbt=True, source_label="test"
    )

    assert result["status"] == "failed"
    assert "c.csv" in result["error"]
    status = read_sync_status(project, "wp67")
    assert status is not None and status["phase"] == "failed" and "c.csv" in status["error"]


def test_mcp_run_sync_reports_the_failed_file(project: Path, monkeypatch) -> None:
    from dango.cli.commands import mcp_operations

    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)

    with patch(
        "dango.visualization.metabase.refresh_metabase_connection", return_value=(False, "x", None)
    ):
        result = mcp_operations.run_sync("demo")

    assert result["status"] == "failed", result
    assert "c.csv" in json.dumps(result["failed_sources"])
    assert result["partial_sources"] == ["demo"]


def test_dbt_and_post_sync_hooks_skip_the_partial_source(tmp_path: Path) -> None:
    good_dir, bad_dir = tmp_path / "good", tmp_path / "bad"
    good_dir.mkdir()
    bad_dir.mkdir()
    write_good(good_dir / "g.csv", ["1,g"])
    write_good(bad_dir / "a.csv", ["1,a"])
    good, bad = make_source("csv", good_dir, "good"), make_source("csv", bad_dir, "bad")
    assert run_sync(tmp_path, [good, bad], skip_dbt=True)["failed_count"] == 0
    write_good(bad_dir / "b.csv", ["2,b"])
    write_latin1(bad_dir / "c.csv")

    run_dbt = MagicMock(return_value=(True, ""))
    hooks = MagicMock(return_value={})
    with (
        patch("dango.transformation.run_dbt_models", run_dbt),
        patch("dango.transformation.docs_locked.generate_dbt_docs_locked", return_value=(True, "")),
        patch("dango.transformation.generator.DbtModelGenerator"),
        patch("dango.governance.schema_drift.detect_drift_for_sources", return_value=[]),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes", return_value=False
        ),
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(False, "x", None),
        ),
        patch("dango.utils.post_sync.dispatch_post_sync_hooks", hooks),
    ):
        summary = run_sync(tmp_path, [good, bad])

    assert summary["success_sources"] == ["good"]
    assert [f["name"] for f in summary["failed_sources"]] == ["bad"]
    assert summary["partial_sources"] == ["bad"]
    assert run_dbt.call_args.kwargs["select"] == "source:good+"
    assert hooks.call_args.kwargs["sources"] == ["good"]
