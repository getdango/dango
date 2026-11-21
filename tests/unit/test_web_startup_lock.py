"""tests/unit/test_web_startup_lock.py

`dango web` startup: waiting for the dbt lock is announced and bounded, an unwritable lock folder
fails at once, an existing or busy warehouse never stops web, Ctrl+C is neutral, --reload works.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from click.testing import CliRunner, Result

from dango.cli.main import cli
from dango.utils.dbt_lock import DbtLock
from tests.factories.scaffold_project import build_fresh_project, working_copy

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STARTUP = "dango.platform.common.startup"
_PROLOGUE = "dango.platform.common.startup_prologue"
_WAITING = "Waiting for another Dango process"


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fresh_project(tmp_path_factory)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    for name in ("DANGO_PROJECT_ROOT", "DANGO_ADMIN_EMAIL"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)


@pytest.fixture
def project(fresh_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = working_copy(fresh_project, tmp_path)
    monkeypatch.chdir(root)
    return root


def _web(*args: str) -> tuple[Result, MagicMock]:
    with patch("uvicorn.run") as run:
        result = CliRunner().invoke(cli, ["web", *args])
    return result, run


def _text(result: Result) -> str:
    return " ".join(_ANSI.sub("", result.output).split())


def _drop_warehouse(project: Path) -> None:
    (project / "data" / "warehouse.duckdb").unlink()


@pytest.fixture
def warehouse_holder(project: Path) -> Iterator[Any]:
    """Start a second process that keeps warehouse.duckdb open read-write; stop it afterwards."""
    procs: list[subprocess.Popen[str]] = []

    def start() -> None:
        code = (
            "import duckdb, sys, time\n"
            "c = duckdb.connect(sys.argv[1])\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", code, str(project / "data" / "warehouse.duckdb")],
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"

    yield start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=30)
        if proc.stdout is not None:
            proc.stdout.close()


# --- the dbt lock wait: announced, bounded, accurate ----------------------------------------


def test_web_says_it_is_waiting_for_a_busy_lock_and_then_proceeds(project: Path) -> None:
    _drop_warehouse(project)
    holder = DbtLock(project, source="sync", operation="dbt run stg_orders")
    assert holder.acquire(timeout=0)
    release = threading.Timer(3.0, holder.release)
    release.start()
    started = time.monotonic()
    try:
        result, run = _web()
    finally:
        release.join()
    output = _text(result)
    assert result.exit_code == 0, output
    assert run.call_count == 1
    assert time.monotonic() - started >= 2.5
    assert output.count(_WAITING) == 1
    assert f"PID {os.getpid()}" in output
    assert "dbt run stg_orders" in output
    assert "Ctrl+C" in output


def test_web_does_not_announce_a_wait_when_the_lock_is_free(project: Path) -> None:
    _drop_warehouse(project)
    result, run = _web()
    assert result.exit_code == 0, result.output
    assert run.call_count == 1
    assert _WAITING not in _text(result)


def test_web_gives_up_on_a_lock_that_stays_busy_and_names_the_holder(project: Path) -> None:
    _drop_warehouse(project)
    holder = DbtLock(project, source="web", operation="dbt run stg_orders")
    assert holder.acquire(timeout=0)
    try:
        with patch(f"{_PROLOGUE}.LOCK_WAIT_TIMEOUT_SECONDS", 1):
            result, run = _web()
    finally:
        holder.release()
    output = _text(result)
    assert result.exit_code == 1
    assert run.call_count == 0
    assert _WAITING in output
    assert "dbt run stg_orders" in output
    assert "Sync queue timeout" not in output
    assert "Another sync" not in output


def test_an_unwritable_lock_folder_fails_at_once_with_a_clear_message(project: Path) -> None:
    _drop_warehouse(project)
    started = time.monotonic()
    with (
        patch("tempfile.mkstemp", side_effect=PermissionError(13, "Permission denied")),
        patch.object(DbtLock, "acquire", side_effect=AssertionError("must not wait")),
    ):
        result, run = _web()
    output = _text(result)
    assert result.exit_code == 1
    assert run.call_count == 0
    assert time.monotonic() - started < 2
    assert ".dango" in output and "Permission denied" in output
    assert _WAITING not in output
    assert "AssertionError" not in output


# --- an existing or busy warehouse never stops web ------------------------------------------


def test_web_does_not_write_when_the_schemas_already_exist(project: Path) -> None:
    with patch(f"{_STARTUP}.ensure_dbt_schemas") as schemas:
        result, run = _web()
    assert result.exit_code == 0, result.output
    assert run.call_count == 1
    schemas.assert_not_called()


def test_web_starts_when_another_process_holds_the_warehouse_with_schemas(
    project: Path, warehouse_holder: Any
) -> None:
    warehouse_holder()
    result, run = _web()
    output = _text(result)
    assert result.exit_code == 0, output
    assert run.call_count == 1
    assert "in use by another Dango process" in output
    assert "Traceback" not in output


def test_web_starts_when_another_process_holds_a_warehouse_without_schemas(
    project: Path, warehouse_holder: Any
) -> None:
    warehouse = project / "data" / "warehouse.duckdb"
    warehouse.unlink()
    duckdb.connect(str(warehouse)).close()
    warehouse_holder()
    result, run = _web()
    output = _text(result)
    assert result.exit_code == 0, output
    assert run.call_count == 1
    assert "in use by another Dango process" in output


def test_web_still_aborts_on_a_corrupt_warehouse(project: Path) -> None:
    (project / "data" / "warehouse.duckdb").write_bytes(b"this is not a duckdb file" * 100)
    result, run = _web()
    output = _text(result)
    assert result.exit_code == 1
    assert run.call_count == 0
    assert "Schema setup failed" in output
    assert "Traceback" not in output


# --- Ctrl+C and --reload --------------------------------------------------------------------


def test_ctrl_c_during_the_startup_checks_is_neutral_and_non_zero(project: Path) -> None:
    with patch(f"{_STARTUP}.run_pending_migrations", side_effect=KeyboardInterrupt):
        result, run = _web()
    output = _text(result)
    assert result.exit_code != 0
    assert run.call_count == 0
    assert "Cancelled" in output
    assert "Server stopped" not in output


def test_ctrl_c_while_serving_still_reports_the_server_stopped(project: Path) -> None:
    with patch("uvicorn.run", side_effect=KeyboardInterrupt):
        result = CliRunner().invoke(cli, ["web"])
    assert result.exit_code == 0
    assert "Server stopped" in _text(result)


def test_reload_gives_uvicorn_an_import_string(project: Path) -> None:
    result, run = _web("--reload")
    assert result.exit_code == 0, result.output
    args, kwargs = run.call_args
    assert args[0] == "dango.web.app:app"
    assert kwargs["reload"] is True


def test_without_reload_uvicorn_gets_the_app_object(project: Path) -> None:
    from dango.web import app as web_app

    result, run = _web()
    assert result.exit_code == 0, result.output
    args, kwargs = run.call_args
    assert args[0] is web_app.app
    assert kwargs["reload"] is False
