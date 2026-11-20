"""tests/unit/test_duckdb_lock_sites.py

Every CLI, web and MCP place that opens the warehouse says "another Dango process is using it"
(not DuckDB's raw lock text) while a second process holds the warehouse; exit codes are unchanged.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

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
        assert result.exception is not None

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
