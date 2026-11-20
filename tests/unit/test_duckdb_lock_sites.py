"""tests/unit/test_duckdb_lock_sites.py

Every CLI, web and MCP place that opens the warehouse says "another Dango process is using it"
(not DuckDB's raw lock text) while a second process holds the warehouse; exit codes are unchanged.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from click.testing import CliRunner

from tests.factories.duckdb_holder import hold_warehouse, make_warehouse

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RAW = ("Could not set lock", "Conflicting lock", "duckdb.org", "IOException", "Traceback")


def _flat(text: str) -> str:
    return " ".join(_ANSI.sub("", text).split())


def _assert_friendly(text: str) -> None:
    flat = _flat(text)
    assert "Another Dango process is using the warehouse" in flat, text
    assert "dango status" in flat, text
    for raw in _RAW:
        assert raw not in flat, text


@pytest.fixture
def project(tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project whose warehouse is held open by another process for the whole test."""
    db = make_warehouse(tmp_project_dir / "data" / "warehouse.duckdb")
    monkeypatch.chdir(tmp_project_dir)
    monkeypatch.setenv("COLUMNS", "300")
    with hold_warehouse(db):
        yield tmp_project_dir


def _run(args: list[str]) -> Any:
    from dango.cli.main import cli

    result = CliRunner().invoke(cli, args, catch_exceptions=True)
    return SimpleNamespace(
        exit_code=result.exit_code, output=result.output, exception=result.exception
    )


class TestCliSites:
    def test_db_status(self, project: Path) -> None:
        result = _run(["db", "status"])
        assert result.exit_code == 1
        _assert_friendly(result.output)

    def test_db_clean(self, project: Path) -> None:
        result = _run(["db", "clean", "--yes"])
        assert result.exit_code == 1
        _assert_friendly(result.output)

    def test_validate_reports_the_lock_in_its_database_check(self, project: Path) -> None:
        result = _run(["validate"])
        assert result.exit_code == 1  # the check still fails: exit code unchanged
        _assert_friendly(result.output)

    def test_source_inspect_state(self, project: Path) -> None:
        result = _run(["source", "inspect-state", "test_csv"])
        assert result.exit_code == 0
        _assert_friendly(result.output)

    def test_uncaught_lock_error_in_any_command_is_friendly(self, project: Path) -> None:
        # `snapshot add` opens the warehouse without a handler of its own.
        result = _run(["snapshot", "add"])
        assert result.exit_code == 1
        _assert_friendly(result.output)

    def test_debug_mode_still_raises(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DANGO_DEBUG", "1")
        result = _run(["snapshot", "add"])
        assert result.exit_code == 1
        assert isinstance(result.exception, duckdb.IOException)
        assert "Another Dango process" not in result.output

    def test_non_lock_errors_escape_the_group_unchanged(self, project: Path) -> None:
        with patch("duckdb.connect", side_effect=RuntimeError("disk exploded")):
            result = _run(["snapshot", "add"])
        assert result.exit_code == 1
        assert isinstance(result.exception, RuntimeError)
        assert "disk exploded" in str(result.exception)
        assert "Another Dango process" not in result.output

    def test_a_crash_while_handling_a_lock_error_keeps_its_traceback(self, project: Path) -> None:
        import click

        from dango.cli.main import cli

        @click.command("wp33-probe")
        def probe() -> None:
            try:
                duckdb.connect(str(project / "data" / "warehouse.duckdb"))
            except duckdb.IOException:
                raise NameError("bug while handling the lock error") from None

        cli.add_command(probe)
        try:
            result = _run(["wp33-probe"])
        finally:
            cli.commands.pop("wp33-probe", None)
        assert isinstance(result.exception, NameError)
        assert "Another Dango process" not in result.output

    def test_source_list_says_busy_not_never(self, project: Path) -> None:
        result = _run(["source", "list"])
        assert result.exit_code == 0
        flat = _flat(result.output)
        assert "busy" in flat
        assert "never" not in flat
        assert "another Dango process" in flat  # one-line note under the table

    def test_other_errors_keep_their_text(self, project: Path) -> None:
        with patch("duckdb.connect", side_effect=RuntimeError("disk exploded")):
            result = _run(["db", "status"])
        assert result.exit_code == 1
        assert "disk exploded" in _flat(result.output)
        assert "Another Dango process" not in result.output


class TestSyncSites:
    @pytest.fixture
    def real_text(self, tmp_path: Path) -> str:
        from tests.factories.duckdb_holder import real_lock_error

        err, _pid = real_lock_error(make_warehouse(tmp_path / "w" / "warehouse.duckdb"))
        return str(err)

    def test_sync_failure_report_is_friendly(
        self, real_text: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit.test_cli_failure_output import _failed_summary, _invoke_sync

        monkeypatch.setattr("dango.cli.scaffold.enforce_scaffold", lambda *args, **kwargs: None)
        result = _invoke_sync(["--yes"], MagicMock(return_value=_failed_summary(real_text)))
        assert result.exit_code == 1
        assert "bad_src" in result.output
        _assert_friendly(result.output)

    def test_runner_messages_are_friendly(self, real_text: str, tmp_path: Path) -> None:
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = object.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        shown = runner._rich_err(Exception(f"Failed to check DuckDB health: {real_text}"))
        assert "Failed to check DuckDB health:" in shown
        _assert_friendly(shown)

    def test_failure_guidance_block_is_friendly(self, real_text: str, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        from dango.ingestion import dlt_runner

        runner = object.__new__(dlt_runner.DltPipelineRunner)
        runner.project_root = tmp_path
        buf = StringIO()
        with patch.object(dlt_runner, "console", Console(file=buf, width=300)):
            runner._print_friendly_error(Exception(real_text), "my_source")
        output = buf.getvalue()
        assert "DuckDB Lock Error" in output  # the existing causes / how-to-fix guidance stays
        assert "Possible causes" in output
        _assert_friendly(output)

    def test_sync_summary_is_friendly(self, real_text: str, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        from dango.ingestion import dlt_runner
        from tests.unit.test_data_fixes import _make_source

        runner = MagicMock()
        runner.run_source.return_value = {"status": "failed", "error": real_text}
        buf = StringIO()
        with (
            patch.object(dlt_runner, "DltPipelineRunner", return_value=runner),
            patch.object(dlt_runner, "console", Console(file=buf, width=300)),
        ):
            dlt_runner.run_sync(tmp_path, [_make_source("my_source", "stripe")], skip_dbt=True)
        assert "my_source" in buf.getvalue()
        _assert_friendly(buf.getvalue())


class TestWebSite:
    def test_query_endpoint(self, project: Path) -> None:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient

        from dango.auth.models import Role, User
        from dango.exceptions import DangoError
        from dango.web.routes import query as query_module

        app = FastAPI()
        app.state.project_root = project

        @app.exception_handler(DangoError)
        async def _dango_error(request: Request, exc: DangoError) -> JSONResponse:
            return JSONResponse(status_code=403, content={"message": "forbidden"})

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = User(email="ed@example.com", role=Role.EDITOR)
            request.state.auth_method = "session"
            return await call_next(request)

        app.include_router(query_module.router)
        warehouse = project / "data" / "warehouse.duckdb"
        with (
            patch("dango.web.routes.query.get_duckdb_path", return_value=warehouse),
            patch("dango.web.routes.query._get_api_config", return_value=(100, 30)),
            patch("dango.web.routes.query.log_auth_event"),
        ):
            resp = TestClient(app, raise_server_exceptions=False).post(
                "/api/query", json={"sql": "SELECT 1"}
            )
        assert resp.status_code == 400  # status and error code unchanged
        body = resp.json()
        assert body["error_code"] == "DANGO-Q005"
        _assert_friendly(body["message"])


class TestMcpSites:
    @pytest.fixture(autouse=True)
    def _root(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from dango.cli.commands import mcp_docs, mcp_governance, mcp_helpers, mcp_server

        for module in (mcp_docs, mcp_governance, mcp_helpers, mcp_server):
            monkeypatch.setattr(module, "_get_project_root", lambda: project)
        (project / "dbt" / "target").mkdir(parents=True, exist_ok=True)

    def test_get_catalog(self) -> None:
        from dango.cli.commands import mcp_server

        _assert_friendly(mcp_server.get_catalog()["error"])

    def test_query(self) -> None:
        from dango.cli.commands import mcp_server

        result = asyncio.run(mcp_server.query("SELECT 1"))
        _assert_friendly(result["error"])

    def test_get_table_schema(self) -> None:
        from dango.cli.commands import mcp_docs

        _assert_friendly(mcp_docs.get_table_schema("test_csv")["error"])

    def test_scan_pii(self) -> None:
        from dango.cli.commands import mcp_governance

        result = mcp_governance.scan_pii("test_csv")
        _assert_friendly(result["error"])

    def test_warehouse_health(self) -> None:
        from dango.cli.commands import mcp_debug

        _assert_friendly(mcp_debug.get_warehouse_health()["error"])

    def test_other_errors_keep_their_text(self) -> None:
        from dango.cli.commands import mcp_server

        with patch("duckdb.connect", side_effect=RuntimeError("disk exploded")):
            result = mcp_server.get_catalog()
        assert result == {"error": "disk exploded"}


class TestDbtRunAndDocs:
    """dbt's own subprocess hits the lock; Dango adds the plain message after its failure line."""

    @pytest.fixture
    def held_fresh_project(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Iterator[Path]:
        from tests.factories.scaffold_project import build_fresh_project, working_copy

        root = working_copy(build_fresh_project(tmp_path_factory), tmp_path)
        monkeypatch.chdir(root)
        monkeypatch.setenv("COLUMNS", "300")
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        with hold_warehouse(root / "data" / "warehouse.duckdb"):
            yield root

    def test_run(self, held_fresh_project: Path) -> None:
        result = _run(["run"])
        assert result.exit_code == 1  # exit code unchanged
        flat = _flat(result.output)
        assert "dbt build failed with exit code" in flat
        assert flat.index("dbt build failed") < flat.index("Another Dango process")
        assert "dango status" in flat

    def test_docs(self, held_fresh_project: Path) -> None:
        result = _run(["docs"])
        assert result.exit_code == 1
        flat = _flat(result.output)
        assert "dbt docs generate failed with exit code" in flat
        assert flat.index("failed with exit code") < flat.index("Another Dango process")


class TestDbtLogTail:
    def _log(self, dbt_dir: Path) -> Path:
        path = dbt_dir / "logs" / "dbt.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_only_the_new_part_of_the_log_counts(self, tmp_path: Path) -> None:
        from dango.utils.dbt_log_lock import lock_message_since, log_size

        log = self._log(tmp_path)
        log.write_text(
            'IO Error: Could not set lock on file "x.duckdb": Conflicting lock is held in '
            "/usr/bin/python (PID 11) by user a\n",
            encoding="utf-8",
        )
        offset = log_size(tmp_path)
        assert lock_message_since(tmp_path, offset) is None  # the old run's text is not ours
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                'IO Error: Could not set lock on file "x.duckdb": Conflicting lock is held in '
                "/usr/bin/python (PID 4242) by user a\n"
            )
        message = lock_message_since(tmp_path, offset)
        assert message is not None
        assert "Another Dango process is using the warehouse (PID 4242)" in message

    def test_no_lock_text_no_message(self, tmp_path: Path) -> None:
        from dango.utils.dbt_log_lock import lock_message_since, log_size

        log = self._log(tmp_path)
        log.write_text("start\n", encoding="utf-8")
        offset = log_size(tmp_path)
        with log.open("a", encoding="utf-8") as handle:
            handle.write("Compilation Error in model x\n")
        assert lock_message_since(tmp_path, offset) is None

    def test_missing_log_and_rotated_log(self, tmp_path: Path) -> None:
        from dango.utils.dbt_log_lock import lock_message_since, log_size

        assert log_size(tmp_path) == 0
        assert lock_message_since(tmp_path, 0) is None
        log = self._log(tmp_path)
        log.write_text('Could not set lock on file "x.duckdb"\n', encoding="utf-8")
        assert lock_message_since(tmp_path, 10**9) is not None  # log was rotated: read it all

    def test_undecodable_bytes_do_not_raise(self, tmp_path: Path) -> None:
        from dango.utils.dbt_log_lock import lock_message_since

        self._log(tmp_path).write_bytes(b"\xff\xfe junk \x00")
        assert lock_message_since(tmp_path, 0) is None


class TestRealLockScript:
    def test_script_captures_the_real_message_and_checks_the_recogniser(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "check_duckdb_lock_text.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "REAL DUCKDB MESSAGE" in result.stdout
        assert "Another Dango process is using the warehouse" in result.stdout

    def test_every_platform_smoke_leg_runs_the_script(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[2] / ".github" / "workflows" / "platform-smoke.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        smoke_job = text.split("\n  installer:", 1)[0]
        assert "scripts/check_duckdb_lock_text.py" in smoke_job
        assert "continue-on-error" not in smoke_job
