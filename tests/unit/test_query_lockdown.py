"""tests/unit/test_query_lockdown.py

Locked-down query connection: user SQL (web /api/query, MCP query) cannot touch files, URLs or settings.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest
import yaml

CANARY = "dango-c1a-canary-7f3e9b"
DEFAULT_LIMIT = "953.6 MiB"  # DuckDB's rendering of '1GB'
SMALL_LIMIT = "95.3 MiB"  # DuckDB's rendering of '100MB'
BIG_AGG = "select length(string_agg(i::varchar, ',')) from range(5000000) t(i)"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_project(root: Path, api: dict[str, Any] | None = None) -> None:
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "project": {"name": "Lockdown", "created_by": "t@example.com", "purpose": "test"}
    }
    if api is not None:
        data["api"] = api
    (root / ".dango" / "project.yml").write_text(yaml.safe_dump(data))
    (root / ".dango" / "sources.yml").write_text(yaml.safe_dump({"sources": []}))


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    """Project with a dbt-style warehouse, plus a canary file outside the project."""
    project = tmp_path / "proj"
    (project / "data").mkdir(parents=True)
    _write_project(project)
    db = project / "data" / "warehouse.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("create schema raw_x")
    conn.execute("create table raw_x.t as select * from range(3) r(a)")
    conn.execute("create schema staging")
    conn.execute('create view staging.v as select * from "warehouse"."raw_x"."t"')
    conn.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary.txt").write_text(CANARY + "\n")
    return {"project": project, "db": db, "outside": outside}


def _open(db: Path, **kwargs: Any) -> duckdb.DuckDBPyConnection:
    from dango.utils.query_connection import open_locked_query_connection

    return open_locked_query_connection(db, **kwargs)


def _read_attacks(outside: Path) -> dict[str, str]:
    canary = outside / "canary.txt"
    return {
        "read_text": f"select content from read_text('{canary}')",
        "read_csv": f"select * from read_csv('{canary}', header=false)",
        "read_blob": f"select content from read_blob('{canary}')",
        "glob": f"select file from glob('{outside}/*')",
        "url": "select * from read_csv('http://127.0.0.1:1/x.csv')",
    }


READ_ATTACKS = ["read_text", "read_csv", "read_blob", "glob", "url"]


# ---------------------------------------------------------------------------
# Connection level
# ---------------------------------------------------------------------------


def test_locked_connection_reads_tables_and_ctes(env: dict[str, Path]) -> None:
    conn = _open(env["db"])
    try:
        assert conn.execute("select count(*) from raw_x.t").fetchone() == (3,)
        cte = "with x as (select a * 2 as b from raw_x.t) select sum(b) from x"
        assert conn.execute(cte).fetchone() == (6,)
    finally:
        conn.close()


def test_locked_connection_resolves_catalog_qualified_view(env: dict[str, Path]) -> None:
    conn = _open(env["db"])
    try:
        assert conn.execute("select count(*) from staging.v").fetchone() == (3,)
    finally:
        conn.close()


def test_locked_connection_memory_limit_errors_cleanly(env: dict[str, Path]) -> None:
    small = _open(env["db"], memory_limit="100MB")
    try:
        with pytest.raises(duckdb.OutOfMemoryException):
            small.execute(BIG_AGG).fetchall()
    finally:
        small.close()
    default = _open(env["db"])
    try:
        assert default.execute(BIG_AGG).fetchone()[0] > 0
        assert default.execute("select current_setting('memory_limit')").fetchone() == (
            DEFAULT_LIMIT,
        )
    finally:
        default.close()


@pytest.mark.parametrize("attack", READ_ATTACKS)
def test_locked_connection_blocks_file_access(env: dict[str, Path], attack: str) -> None:
    conn = _open(env["db"])
    try:
        with pytest.raises(duckdb.PermissionException) as exc_info:
            conn.execute(_read_attacks(env["outside"])[attack]).fetchall()
        assert CANARY not in str(exc_info.value)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("copy", duckdb.PermissionException),
        ("export", duckdb.PermissionException),
        ("install", duckdb.PermissionException),
        ("load_path", duckdb.PermissionException),
        ("attach", duckdb.PermissionException),
        ("enable_external_access", duckdb.InvalidInputException),
        ("unlock_configuration", duckdb.InvalidInputException),
        ("raise_memory_limit", duckdb.InvalidInputException),
        ("create_table", duckdb.InvalidInputException),
    ],
)
def test_locked_connection_blocks_mutation_and_reconfig(
    env: dict[str, Path], name: str, expected: type[Exception]
) -> None:
    out = env["outside"]
    fake_ext = out / "x.duckdb_extension"
    fake_ext.write_bytes(b"not an extension")
    sql = {
        "copy": f"COPY (select 1) TO '{out}/copied.csv'",
        "export": f"EXPORT DATABASE '{out}/exported'",
        "install": "INSTALL httpfs",
        "load_path": f"LOAD '{fake_ext}'",
        "attach": f"ATTACH '{out}/other.duckdb' AS other",
        "enable_external_access": "SET enable_external_access=true",
        "unlock_configuration": "SET lock_configuration=false",
        "raise_memory_limit": "SET memory_limit='64GB'",
        "create_table": "CREATE TABLE t(x int)",
    }[name]
    conn = _open(env["db"])
    try:
        with pytest.raises(expected):
            conn.execute(sql)
    finally:
        conn.close()
    assert not (out / "copied.csv").exists()
    assert not (out / "exported").exists()
    assert not (out / "other.duckdb").exists()


def test_locked_connection_coexists_with_open_connection(env: dict[str, Path]) -> None:
    other = duckdb.connect(str(env["db"]), read_only=True)
    try:
        conn = _open(env["db"])
        try:
            assert conn.execute("select count(*) from raw_x.t").fetchone() == (3,)
        finally:
            conn.close()
    finally:
        other.close()


def test_locked_connection_inprocess_writer_conflict_is_not_retried(env: dict[str, Path]) -> None:
    """Same-process read-write handle: a binder conflict, raised at once (no IOException retry)."""
    from dango.cli.commands.mcp_helpers import _connect_readonly_with_retry
    from dango.web.routes.query import _execute_query

    writer = duckdb.connect(str(env["db"]))
    try:
        with pytest.raises(duckdb.BinderException):
            _open(env["db"])
        with patch("time.sleep") as sleep:
            with pytest.raises(duckdb.BinderException):
                _connect_readonly_with_retry(env["db"])
            with pytest.raises(duckdb.BinderException):
                _execute_query(str(env["db"]), "select 1", 10)
        sleep.assert_not_called()
    finally:
        writer.close()


def test_locked_connection_raises_ioexception_under_writer_lock(env: dict[str, Path]) -> None:
    from dango.cli.commands.mcp_helpers import _connect_readonly_with_retry

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time, duckdb\n"
            "c = duckdb.connect(sys.argv[1])\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n",
            str(env["db"]),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(duckdb.IOException):
            _open(env["db"])
        with patch("time.sleep") as sleep:
            with pytest.raises(duckdb.IOException):
                _connect_readonly_with_retry(env["db"])
        assert sleep.call_count == 2
    finally:
        holder.kill()
        holder.wait()


# ---------------------------------------------------------------------------
# Entry points: web /api/query and MCP query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attack", [*READ_ATTACKS, "copy"])
def test_web_query_blocks_file_access(env: dict[str, Path], attack: str) -> None:
    from dango.web.routes.query import _execute_query

    target = env["outside"] / "copied.csv"
    attacks = {**_read_attacks(env["outside"]), "copy": f"COPY (select 1) TO '{target}'"}
    with pytest.raises(duckdb.PermissionException) as exc_info:
        _execute_query(str(env["db"]), attacks[attack], 10)
    assert CANARY not in str(exc_info.value)
    assert not target.exists()


@pytest.fixture
def mcp_project(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    from dango.cli.commands import mcp_server

    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(env["project"]))
    monkeypatch.setattr(mcp_server, "_get_project_root", lambda: env["project"])
    return env


@pytest.mark.anyio
@pytest.mark.parametrize("attack", READ_ATTACKS)
async def test_mcp_query_blocks_file_access(mcp_project: dict[str, Path], attack: str) -> None:
    from dango.cli.commands import mcp_server

    result = await mcp_server.query(_read_attacks(mcp_project["outside"])[attack])
    assert "rows" not in result
    assert "Permission Error" in result["error"]
    assert CANARY not in repr(result)


def test_mcp_connection_blocks_copy(env: dict[str, Path]) -> None:
    """COPY is refused by the MCP validator first; the connection must refuse it too."""
    from dango.cli.commands.mcp_helpers import (
        _connect_readonly_with_retry,
        _execute_query_on_connection,
    )

    target = env["outside"] / "copied.csv"
    conn = _connect_readonly_with_retry(env["db"])
    try:
        with pytest.raises(duckdb.PermissionException):
            _execute_query_on_connection(conn, f"COPY (select 1) TO '{target}'", 10)
    finally:
        conn.close()
    assert not target.exists()


def _web_post(project: Path, db: Path, sql: str) -> Any:
    """POST /api/query through the real router (auth stubbed, real DuckDB)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dango.auth.models import Role, User
    from dango.web.routes.query import router

    app = FastAPI()
    app.state.project_root = project
    user = User(id="u1", email="e@x.com", password_hash="h", role=Role.EDITOR, is_active=True)

    @app.middleware("http")
    async def _user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        return await call_next(request)

    app.include_router(router)
    with (
        patch("dango.web.routes.query.get_duckdb_path", return_value=db),
        patch("dango.web.routes.query.log_auth_event", MagicMock()),
    ):
        return TestClient(app, raise_server_exceptions=False).post("/api/query", json={"sql": sql})


@pytest.mark.anyio
async def test_query_memory_limit_from_project_yml(mcp_project: dict[str, Path]) -> None:
    from pydantic import ValidationError

    from dango.cli.commands import mcp_server
    from dango.config.models import ApiConfig

    project, db = mcp_project["project"], mcp_project["db"]
    probe = "select current_setting('memory_limit') as m"

    # absent -> 1GB on both entry points
    assert _web_post(project, db, probe).json()["rows"] == [[DEFAULT_LIMIT]]
    assert (await mcp_server.query(probe))["rows"] == [[DEFAULT_LIMIT]]

    # set -> used on both entry points
    _write_project(project, api={"query_memory_limit": "100MB"})
    assert _web_post(project, db, probe).json()["rows"] == [[SMALL_LIMIT]]
    assert (await mcp_server.query(probe))["rows"] == [[SMALL_LIMIT]]

    # invalid -> config validation error (value is embedded in a SET statement)
    with pytest.raises(ValidationError):
        ApiConfig(query_memory_limit="1GB; select 1")


def test_web_query_out_of_memory_message_is_readable(env: dict[str, Path]) -> None:
    _write_project(env["project"], api={"query_memory_limit": "100MB"})
    resp = _web_post(env["project"], env["db"], BIG_AGG)
    assert resp.status_code == 400
    body = resp.json()
    assert set(body) == {"error_code", "message"}
    assert body["error_code"] == "DANGO-Q007"
    assert "more memory than the 100MB limit" in body["message"]
    assert "api.query_memory_limit" in body["message"]


@pytest.mark.anyio
async def test_query_memory_limit_falls_back_on_bad_config(
    mcp_project: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """An invalid api.query_memory_limit never fails the query: 1GB default plus a warning."""
    from dango.cli.commands import mcp_server

    project, db = mcp_project["project"], mcp_project["db"]
    _write_project(project, api={"query_memory_limit": "1GB; select 1"})
    probe = "select current_setting('memory_limit') as m"

    with patch("dango.web.routes.query.logger") as web_logger:
        assert _web_post(project, db, probe).json()["rows"] == [[DEFAULT_LIMIT]]
    assert any(
        c.args[0] == "query_memory_limit_config_unreadable"
        for c in web_logger.warning.call_args_list
    )

    with caplog.at_level("WARNING", logger="dango.cli.commands.mcp_helpers"):
        assert (await mcp_server.query(probe))["rows"] == [[DEFAULT_LIMIT]]
    assert "query_memory_limit_config_unreadable" in caplog.text


# ---------------------------------------------------------------------------
# Validators fail closed without sqlglot
# ---------------------------------------------------------------------------


def test_validators_fail_closed_without_sqlglot(monkeypatch: pytest.MonkeyPatch) -> None:
    from dango.cli.commands.mcp_helpers import _validate_select_only
    from dango.web.routes.query import _validate_sql

    monkeypatch.setitem(sys.modules, "sqlglot", None)
    monkeypatch.setitem(sys.modules, "sqlglot.expressions", None)
    sql = "SELECT * FROM read_text('/etc/hosts')"
    for validate in (_validate_sql, _validate_select_only):
        with pytest.raises(ValueError, match="SQL validation is unavailable"):
            validate(sql)
