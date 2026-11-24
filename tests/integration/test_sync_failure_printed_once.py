"""tests/integration/test_sync_failure_printed_once.py

`dango sync` with a failed dbt transform (real CLI, real run_sync, real dbt + DuckDB, no Docker):
every fact of the failure is printed once, the raw dbt output is shown only when no reason is
known, and secrets and Rich markup in dbt's text are handled.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

import dango
from dango.cli.commands.source import sync
from dango.platform.scheduling import jobs
from tests.integration.dbt_outcome_support import (
    build_clock_ahead,
    make_csv_project,
    write_orders_csv,
)

pytestmark = pytest.mark.integration

CLEAN_CSV = "id,name\n1,a\n2,b\n3,c\n"
DUPLICATE_CSV = "id,name\n1,a\n2,b\n3,c\n3,dup\n"
SECRET = "hunter2hunter2"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    monkeypatch.setenv("DANGO_TELEMETRY", "false")
    monkeypatch.setenv("COLUMNS", "400")  # no wrapping: names are never split across lines
    monkeypatch.delenv("DLT_DATA_DIR", raising=False)


def _sync(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Result, str]:
    monkeypatch.chdir(root)
    result = CliRunner().invoke(sync, ["--yes"], obj={"project_root": root})
    return result, re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def _synced_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_csv_project(tmp_path / "proj", CLEAN_CSV)
    first, _ = _sync(root, monkeypatch)
    assert first.exit_code == 0, first.output
    return root


def _write_model(root: Path, name: str, sql: str) -> None:
    (root / "dbt" / "models" / "marts" / f"{name}.sql").write_text(
        sql, encoding="utf-8", newline="\n"
    )


def _fake_dbt(
    root: Path,
    *,
    stdout: str,
    returncode: int = 1,
    results: bytes | None = None,
    timeout: bool = False,
) -> Any:
    """A subprocess.run that fakes only the dbt build (everything else runs for real)."""
    real_run = subprocess.run

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if "build" not in cmd or "--project-dir" not in cmd:
            return real_run(cmd, *args, **kwargs)
        if timeout:
            raise subprocess.TimeoutExpired(cmd, 300)
        if results is not None:
            target = root / "dbt" / "target"
            target.mkdir(parents=True, exist_ok=True)
            (target / "run_results.json").write_bytes(results)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return patch("dango.transformation.subprocess.run", side_effect=fake_run)


def test_duplicate_key_failure_prints_each_fact_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    write_orders_csv(root, DUPLICATE_CSV)
    result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    # the headline, the failing test, the skipped models, the table warning: once each
    assert text.count("dbt transform failed") == 1
    assert text.count("source_unique_shop_shop_id") == 1
    assert text.lower().count("tests failed (1)") == 1
    assert (
        text.count("stg_shop__shop, order_count") == 1
    )  # (profiling log lines also name the table)
    assert text.count("order_count") == 1
    assert text.lower().count("models skipped because something they depend on failed") == 1
    assert text.count("Tables of models that were skipped or failed") == 1
    assert text.count("Error:") == 1
    # dbt's boilerplate is not repeated around the reason
    assert text.count("Possible causes") == 0
    # the one actionable hint stays, once, and says where the cause is
    assert text.count("Suggested fix:") == 1
    assert text.count("duplicate or blank key values") == 1
    assert "named below" not in text


def test_real_process_prints_each_fact_once_on_stdout_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal shows stdout and stderr together: the log line must not repeat the reason."""
    root = _synced_project(tmp_path, monkeypatch)
    write_orders_csv(root, DUPLICATE_CSV)
    package_parent = str(Path(dango.__file__).resolve().parent.parent)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([package_parent, os.environ.get("PYTHONPATH", "")]),
        "DANGO_LOG_LEVEL": "INFO",  # CI sets ERROR; the file log must hold the INFO reason record
    }
    proc = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", "sync", "--yes"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    text = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout + proc.stderr)
    assert text.count("source_unique_shop_shop_id") == 1
    assert text.lower().count("models skipped because something they depend on failed") == 1
    assert text.count("Possible causes") == 0
    log_file = (root / ".dango" / "logs" / "dango.log").read_text(encoding="utf-8")
    assert "source_unique_shop_shop_id" in log_file  # the reason is recorded in the log file


def test_known_reason_does_not_dump_the_raw_dbt_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    write_orders_csv(root, DUPLICATE_CSV)
    _, text = _sync(root, monkeypatch)
    assert "Running with dbt" not in text
    assert "Completed with" not in text
    assert "Done. PASS=" not in text
    assert "compiled code at" not in text


def test_errored_model_shows_dbts_error_once_and_names_the_model_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    _write_model(
        root, "broken_mart", "select cast('abc' as integer) as x from {{ ref('stg_shop__shop') }}\n"
    )
    result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    assert text.count("Conversion Error") == 1  # the cause stays visible
    assert text.count("broken_mart") == 1
    assert "Running with dbt" not in text


def test_compile_error_without_results_shows_the_raw_output_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    _write_model(root, "bad_ref", "select * from {{ ref('no_such_model') }}\n")
    with caplog.at_level(logging.INFO):
        result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    assert text.count("Compilation Error") == 1
    assert text.count("no_such_model") >= 1
    assert text.count("Possible causes") == 1
    assert text.count("dbt transform failed") == 1
    # the log file's reason names dbt's cause, not only that the run results explain nothing
    reasons = [
        r.getMessage() for r in caplog.records if "dbt_transform_failed_reason" in r.getMessage()
    ]
    assert len(reasons) == 1
    assert "Compilation Error: " in reasons[0] and "no_such_model" in reasons[0]


def test_coalesced_run_records_the_compile_error_cause_in_the_activity_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    _write_model(root, "bad_ref", "select * from {{ ref('no_such_model') }}\n")
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setattr(jobs, "_get_coalesce_seconds", lambda _root: 0)
    jobs._add_pending_dbt_source(root, "shop")
    assert jobs._run_coalesced_dbt(root) is False
    path = root / ".dango" / "logs" / "activity.jsonl"
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["level"] == "error"
    assert last["message"].startswith("Coalesced dbt failed for sources: shop. ")
    assert "Compilation Error: " in last["message"] and "no_such_model" in last["message"]
    assert "run results do not explain" not in last["message"]
    assert "\n" not in last["message"]


def test_timeout_shows_its_cause_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO), _fake_dbt(root, stdout="", timeout=True):
        result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    assert text.count("timed out after 5 minutes") == 1
    assert text.count("Possible causes") == 1
    # the terminal also shows log records of WARNING and above: they must not repeat the cause
    shown = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("timed out after 5 minutes" in m for m in shown)
    # the log file still records the reason once
    assert sum("timed out after 5 minutes" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize(
    "results", [None, b'{"results": [ not json'], ids=["missing", "unparseable"]
)
def test_unexplained_exit_shows_the_raw_output_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: bytes | None
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    (root / "dbt" / "target" / "run_results.json").unlink(missing_ok=True)
    with _fake_dbt(root, stdout="odd dbt line one\nodd dbt line two\n", results=results):
        result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    assert text.count("odd dbt line one") == 1
    assert text.count("odd dbt line two") == 1
    assert text.count("dbt transform failed") == 1


def test_unparseable_results_are_rejected_as_unparseable_when_the_clock_runs_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coarse file timestamps must not turn "unparseable" into "stale": the file is read."""
    root = _synced_project(tmp_path, monkeypatch)
    (root / "dbt" / "target" / "run_results.json").unlink(missing_ok=True)
    with (
        patch("dango.transformation.build_finalize.logger") as log,
        build_clock_ahead(),
        _fake_dbt(root, stdout="odd dbt line one\n", results=b'{"results": [ not json'),
    ):
        result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    assert [c.args[0] for c in log.warning.call_args_list] == ["run_results_summary_failed"]
    assert text.count("odd dbt line one") == 1


def test_failed_check_that_skipped_nothing_is_shown_once_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_csv_project(tmp_path / "proj", CLEAN_CSV, failing_leaf_test=True)
    result, text = _sync(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "dbt models executed successfully" in text
    assert text.lower().count("tests failed (1)") == 1
    assert text.count("accepted_values_order_count_n") == 1
    assert "dbt transform failed" not in text


def test_markup_and_secret_in_dbt_text_are_safe_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _synced_project(tmp_path, monkeypatch)
    (root / "dbt" / "target" / "run_results.json").unlink(missing_ok=True)
    stdout = f"odd [bold red]marked[/bold red] line password={SECRET}\n"
    with caplog.at_level(logging.DEBUG), _fake_dbt(root, stdout=stdout):
        result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    assert SECRET not in text
    assert SECRET not in caplog.text
    assert "[bold red]marked[/bold red]" in text  # printed literally, not interpreted
    for path in (root / ".dango" / "history").glob("*.json"):
        assert SECRET not in path.read_text(encoding="utf-8")
        assert SECRET not in json.dumps(json.loads(path.read_text(encoding="utf-8")))
    for path in (root / ".dango" / "logs").glob("*"):
        assert SECRET not in path.read_text(encoding="utf-8", errors="replace")
