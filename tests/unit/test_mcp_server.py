"""tests/unit/test_mcp_server.py

Tests for the `dango mcp` command group (dango/cli/commands/mcp_server.py):
the read-only MCP tool functions, group registration, and the
_get_project_root()/_check_version_compatibility() helpers (1.0.8-OPS-4).
See tests/unit/test_mcp_setup.py for `mcp setup`/`status`/`remove`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dango.cli.commands import mcp_server
from dango.cli.commands.mcp_server import mcp_group


@pytest.mark.unit
class TestMcpGroupRegistration:
    """CLI wiring smoke tests."""

    def test_mcp_help_shows_subcommands(self) -> None:
        """``dango mcp --help`` lists run/setup/status."""
        runner = CliRunner()
        result = runner.invoke(mcp_group, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "setup" in result.output
        assert "status" in result.output


@pytest.mark.unit
class TestQueryTool:
    """query() — SELECT-only guard, row-limit cap, length cap, timeout. Async tool: see
    dango/cli/CLAUDE.md's async-tool note — call directly with `await` under anyio."""

    @pytest.mark.anyio
    async def test_query_rejects_non_select(self) -> None:
        result = await mcp_server.query("DELETE FROM foo")
        assert "error" in result
        assert "SELECT" in result["error"]

    @pytest.mark.anyio
    async def test_query_rejects_multi_statement(self) -> None:
        result = await mcp_server.query("SELECT 1; DROP TABLE foo")
        assert "error" in result

    @pytest.mark.anyio
    async def test_query_allows_with_cte(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A WITH ... SELECT CTE is a legitimate read-only query, not just bare SELECT."""
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: Path("/nonexistent"))
        result = await mcp_server.query("WITH t AS (SELECT 1 AS x) SELECT * FROM t")
        # Should get past validation to the "no warehouse found" branch, not a SELECT-only error.
        assert result.get("error") != "Only SELECT queries are allowed"

    @pytest.mark.anyio
    async def test_query_rejects_oversized_sql(self) -> None:
        """SQL longer than the 102,400-char cap (matches web/routes/query.py) is rejected
        before ever touching the project root or the warehouse."""
        oversized = "SELECT " + "1, " * 60_000 + "1"
        assert len(oversized) > mcp_server._MAX_QUERY_SQL_LENGTH
        result = await mcp_server.query(oversized)
        assert "too long" in result["error"]

    @pytest.mark.anyio
    async def test_query_row_limit_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """row_limit above 500 is capped at 500, even against a table with more rows."""
        import duckdb

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "warehouse.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE t AS SELECT * FROM range(600) AS r(x)")
        conn.close()

        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        result = await mcp_server.query("SELECT * FROM t", row_limit=10_000)

        assert result["row_count"] == 500
        assert result["truncated"] is True

    @pytest.mark.anyio
    async def test_query_times_out_and_interrupts_the_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An execution that outruns the timeout returns a clean error, AND actually
        calls conn.interrupt() to cancel the in-flight DuckDB query — positive control
        for the bug where asyncio.wait_for(asyncio.to_thread(...)) only stopped
        *waiting*, silently leaving the query running to completion in a background
        thread against the shared default executor."""
        import time as time_module
        from unittest.mock import MagicMock

        import duckdb

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "warehouse.duckdb"
        duckdb.connect(str(db_path)).close()  # just needs to exist and be openable

        real_conn = duckdb.connect(str(db_path), read_only=True)

        class _ConnSpy:
            """duckdb's connection type is a C extension — its attributes can't
            be monkeypatched directly (they're read-only on the instance), so
            wrap it to make .interrupt() spyable while still delegating to the
            real connection."""

            def __init__(self, real: object) -> None:
                self._real = real
                self.interrupt = MagicMock(wraps=real.interrupt)

            def close(self) -> None:
                self._real.close()

        conn_spy = _ConnSpy(real_conn)

        def _slow_execute(conn: object, sql: str, row_limit: int) -> dict:
            time_module.sleep(0.3)
            return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr(mcp_server, "_connect_readonly_with_retry", lambda db_path: conn_spy)
        monkeypatch.setattr(mcp_server, "_execute_query_on_connection", _slow_execute)
        monkeypatch.setattr(mcp_server, "_get_query_timeout_seconds", lambda project_root: 0.05)

        result = await mcp_server.query("SELECT 1")

        assert "timed out" in result["error"]
        conn_spy.interrupt.assert_called_once()

    def test_query_timeout_honors_project_config(self, tmp_path: Path, sample_config) -> None:
        """_get_query_timeout_seconds reads api.query_timeout_seconds from project.yml
        rather than always using the hardcoded 30s default — positive control for the
        gap where the MCP tool ignored the same per-project setting
        web/routes/query.py's _get_api_config honors."""
        from dango.cli.commands.mcp_helpers import _get_query_timeout_seconds
        from dango.config.helpers import save_config

        sample_config.api.query_timeout_seconds = 120
        save_config(sample_config, tmp_path)

        assert _get_query_timeout_seconds(tmp_path) == 120

    def test_query_timeout_falls_back_to_default_when_unconfigured(self, tmp_path: Path) -> None:
        """No project at all -> falls back to the 30s default rather than raising."""
        from dango.cli.commands.mcp_helpers import _get_query_timeout_seconds

        assert _get_query_timeout_seconds(tmp_path) == 30


@pytest.mark.unit
class TestGetTableSchemaAndModelTools:
    def test_get_model_sql_missing_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        result = mcp_server.get_model_sql("stg_stripe__customers")
        assert "error" in result
        assert "No manifest found" in result["error"]

    def test_list_models_missing_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        result = mcp_server.list_models()
        assert result == [{"error": "No manifest found. Run dango run first."}]

    def test_get_table_schema_missing_warehouse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        result = mcp_server.get_table_schema("some_table")
        assert "error" in result
        assert "No warehouse found" in result["error"]

    def test_get_table_schema_ambiguous_name_filters_to_one_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A table name that exists in two schemas must not have its columns merged:
        positive control for the bug where every matching table's columns were
        concatenated into one list under a single (misleading) schema name."""
        import duckdb

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "warehouse.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE SCHEMA raw_a")
        conn.execute("CREATE SCHEMA raw_b")
        conn.execute("CREATE TABLE raw_a.events (a_only_col INTEGER)")
        conn.execute("CREATE TABLE raw_b.events (b_only_col INTEGER, another_b_col INTEGER)")
        conn.close()

        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        result = mcp_server.get_table_schema("events")

        assert result["schema"] == "raw_a"
        assert [c["name"] for c in result["columns"]] == ["a_only_col"]
        assert result["other_schemas"] == ["raw_b"]


@pytest.mark.unit
class TestGetLineage:
    def test_referenced_by_resolves_for_versioned_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the manual unique_id reconstruction bug: dbt's versioned
        models (dbt-core 1.5+) have a real unique_id like 'model.pkg.orders.v2', which
        f"model.{package_name}.{model_name}" doesn't produce — that silently made
        referenced_by empty even when a real dependent exists."""
        manifest = {
            "nodes": {
                "model.pkg.orders.v2": {
                    "name": "orders",
                    "package_name": "pkg",
                    "resource_type": "model",
                    "original_file_path": "models/marts/orders.sql",
                    "depends_on": {"nodes": []},
                },
                "model.pkg.fct_order_summary": {
                    "name": "fct_order_summary",
                    "package_name": "pkg",
                    "resource_type": "model",
                    "depends_on": {"nodes": ["model.pkg.orders.v2"]},
                },
            },
            "sources": {},
        }
        dbt_dir = tmp_path / "dbt" / "target"
        dbt_dir.mkdir(parents=True)
        (dbt_dir / "manifest.json").write_text(json.dumps(manifest))

        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        result = mcp_server.get_lineage(model_name="orders")

        assert result["referenced_by"] == ["model.pkg.fct_order_summary"]


@pytest.mark.unit
class TestListSources:
    @pytest.mark.anyio
    async def test_list_sources_empty_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No sources configured -> returns empty list, no crash."""
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr("dango.web.helpers.load_sources_config", lambda: [], raising=True)
        result = await mcp_server.list_sources()
        assert result == []


@pytest.mark.unit
class TestGetSyncHistory:
    def test_get_sync_history_no_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not in a Dango project -> RuntimeError propagates (matches every other tool;
        fastmcp converts this into a tool-call error for the client, not a crash)."""

        def _raise() -> Path:
            raise RuntimeError("Not inside a Dango project.")

        monkeypatch.setattr(mcp_server, "_get_project_root", _raise)
        with pytest.raises(RuntimeError, match="Not inside a Dango project"):
            mcp_server.get_sync_history()

    def test_get_sync_history_single_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr(
            "dango.utils.sync_history.load_sync_history",
            lambda project_root, source_name, limit: [
                {"timestamp": "2026-01-01T00:00:00Z", "status": "success", "rows_processed": 10}
            ],
        )
        result = mcp_server.get_sync_history(source_name="my_source")
        assert result == [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "status": "success",
                "rows_processed": 10,
                "source": "my_source",
            }
        ]


@pytest.mark.unit
class TestInferLayer:
    def test_infer_layer_staging(self) -> None:
        assert mcp_server._infer_layer("stg_stripe__customers") == "staging"

    def test_infer_layer_intermediate(self) -> None:
        assert mcp_server._infer_layer("int_orders_enriched") == "intermediate"

    def test_infer_layer_marts(self) -> None:
        assert mcp_server._infer_layer("fct_orders") == "marts"
        assert mcp_server._infer_layer("dim_customers") == "marts"

    def test_infer_layer_other(self) -> None:
        assert mcp_server._infer_layer("some_other_model") == "other"


@pytest.mark.unit
class TestValidateSelectOnly:
    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            mcp_server._validate_select_only("   ")

    def test_rejects_delete(self) -> None:
        with pytest.raises(ValueError):
            mcp_server._validate_select_only("DELETE FROM foo")

    def test_rejects_multi_statement(self) -> None:
        with pytest.raises(ValueError):
            mcp_server._validate_select_only("SELECT 1; SELECT 2")

    def test_allows_select(self) -> None:
        mcp_server._validate_select_only("SELECT * FROM foo")

    def test_allows_with_cte(self) -> None:
        mcp_server._validate_select_only("WITH t AS (SELECT 1) SELECT * FROM t")


@pytest.mark.unit
class TestGetProjectRoot:
    """_get_project_root() — DANGO_PROJECT_ROOT env var takes precedence over
    cwd-walking (1.0.8-OPS-4: the fix for MCP servers being long-lived
    processes where cwd-at-spawn-time may not match the intended project)."""

    def test_prefers_env_var(self, tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """DANGO_PROJECT_ROOT set to a real project -> returned directly,
        without ever falling through to cwd-based resolution."""
        from dango.cli.commands.mcp_helpers import _get_project_root

        monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_project_dir))
        monkeypatch.setattr(
            "dango.config.helpers.find_project_root",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fall back to cwd")),
        )

        assert _get_project_root() == tmp_project_dir

    def test_env_var_invalid_path_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DANGO_PROJECT_ROOT pointing at a path with no .dango/project.yml ->
        RuntimeError naming the bad path, not a silent fallback to cwd."""
        from dango.cli.commands.mcp_helpers import _get_project_root

        bad_path = tmp_path / "not-a-project"
        bad_path.mkdir()
        monkeypatch.setenv("DANGO_PROJECT_ROOT", str(bad_path))

        with pytest.raises(RuntimeError, match=str(bad_path)):
            _get_project_root()

    def test_falls_back_to_cwd_when_env_var_unset(
        self, tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No DANGO_PROJECT_ROOT -> existing cwd-walking behavior, unchanged
        (regression check for every manually-invoked `dango mcp run` and any
        config predating this change)."""
        from dango.cli.commands.mcp_helpers import _get_project_root

        monkeypatch.delenv("DANGO_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_project_dir)

        assert _get_project_root() == tmp_project_dir


@pytest.mark.unit
class TestCheckVersionCompatibility:
    """_check_version_compatibility() — startup-only warning when the running
    dango differs from the project's own recorded version."""

    def test_version_mismatch_returns_warning(
        self, tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.cli.commands.mcp_helpers import _check_version_compatibility
        from dango.config.helpers import get_config, save_config

        config = get_config(tmp_project_dir)
        config.project.dango_version = "1.0.7"
        save_config(config, tmp_project_dir)
        monkeypatch.setattr("dango.__version__", "1.0.8")

        warning = _check_version_compatibility(tmp_project_dir)

        assert warning is not None
        assert "1.0.7" in warning
        assert "1.0.8" in warning

    def test_version_match_returns_none(
        self, tmp_project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.cli.commands.mcp_helpers import _check_version_compatibility
        from dango.config.helpers import get_config, save_config

        config = get_config(tmp_project_dir)
        config.project.dango_version = "1.0.8"
        save_config(config, tmp_project_dir)
        monkeypatch.setattr("dango.__version__", "1.0.8")

        assert _check_version_compatibility(tmp_project_dir) is None

    def test_no_recorded_version_returns_none(self, tmp_project_dir: Path) -> None:
        """A project with no recorded dango_version at all (e.g. created
        before this field existed) -> nothing to compare against, no warning."""
        from dango.cli.commands.mcp_helpers import _check_version_compatibility

        assert _check_version_compatibility(tmp_project_dir) is None
