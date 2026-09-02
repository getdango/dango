"""tests/unit/test_mcp_server.py

Tests for the `dango mcp` command group (dango/cli/commands/mcp_server.py):
the read-only MCP tool functions and the setup/status CLI subcommands.
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
class TestMcpSetup:
    """dango mcp setup — LLM client config detection + writing."""

    def test_mcp_setup_writes_claude_code_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ~/.claude/ exists, setup writes settings.json with dango in mcpServers."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        settings_path = claude_dir / "settings.json"
        assert settings_path.exists()
        written = json.loads(settings_path.read_text())
        assert written["mcpServers"]["dango"]["args"] == ["mcp", "run"]

    def test_mcp_setup_preserves_existing_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing unrelated keys in settings.json are not clobbered."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"theme": "dark"}))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        written = json.loads((claude_dir / "settings.json").read_text())
        assert written["theme"] == "dark"
        assert written["mcpServers"]["dango"]["args"] == ["mcp", "run"]

    def test_mcp_setup_no_clients(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No LLM client dirs exist -> helpful message, no error, no files written."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        assert "No LLM clients detected" in result.output
        assert not (tmp_path / ".claude").exists()


@pytest.mark.unit
class TestMcpStatus:
    """dango mcp status — verification output."""

    def test_mcp_status_no_clients(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["status"])

        assert result.exit_code == 0
        assert "No LLM clients detected" in result.output

    def test_mcp_status_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"mcpServers": {"dango": {"command": "dango", "args": ["mcp", "run"]}}})
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["status"])

        assert result.exit_code == 0
        assert "dango MCP configured" in result.output


@pytest.mark.unit
class TestQueryTool:
    """query() — SELECT-only guard, row-limit cap."""

    def test_query_rejects_non_select(self) -> None:
        result = mcp_server.query("DELETE FROM foo")
        assert "error" in result
        assert "SELECT" in result["error"]

    def test_query_rejects_multi_statement(self) -> None:
        result = mcp_server.query("SELECT 1; DROP TABLE foo")
        assert "error" in result

    def test_query_allows_with_cte(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A WITH ... SELECT CTE is a legitimate read-only query, not just bare SELECT."""
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: Path("/nonexistent"))
        result = mcp_server.query("WITH t AS (SELECT 1 AS x) SELECT * FROM t")
        # Should get past validation to the "no warehouse found" branch, not a SELECT-only error.
        assert result.get("error") != "Only SELECT queries are allowed"

    def test_query_row_limit_capped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """row_limit above 500 is capped at 500, even against a table with more rows."""
        import duckdb

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "warehouse.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE t AS SELECT * FROM range(600) AS r(x)")
        conn.close()

        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        result = mcp_server.query("SELECT * FROM t", row_limit=10_000)

        assert result["row_count"] == 500
        assert result["truncated"] is True


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


@pytest.mark.unit
class TestListSources:
    def test_list_sources_empty_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No sources configured -> returns empty list, no crash."""
        monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr("dango.web.helpers.load_sources_config", lambda: [], raising=True)
        result = mcp_server.list_sources()
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
