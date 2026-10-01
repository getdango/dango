"""tests/unit/test_mcp_docs.py

Tests for the MCP documentation tools (dango/cli/commands/mcp_docs.py) over real files and DuckDB in tmp_path.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
import yaml

from dango.cli.commands import mcp_docs
from dango.exceptions import DbtLockError

PLACEHOLDER = "TODO: Add description\n(Auto-generated - edit in dbt/models/[layer]/schema.yml)"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project with raw_sales.orders, staging + marts models, docs and a real warehouse."""
    (tmp_path / "data").mkdir()
    conn = duckdb.connect(str(tmp_path / "data" / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA raw_sales")
    conn.execute("CREATE TABLE raw_sales.orders (id INTEGER, region VARCHAR)")
    conn.execute("CREATE SCHEMA marts")
    conn.execute("CREATE TABLE marts.fct_sales (region VARCHAR, total INTEGER, extra INTEGER)")
    conn.close()
    models = tmp_path / "dbt" / "models"
    _write(models / "staging" / "stg_sales__orders.sql", "select 1 as id")
    _write(models / "marts" / "fct_sales.sql", "select 1 as region")
    _write(
        models / "marts" / "schema.yml",
        yaml.dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "fct_sales",
                        "description": "Sales by region",
                        "columns": [
                            {
                                "name": "region",
                                "description": "Region code",
                                "data_tests": ["not_null"],
                            },
                            {"name": "total", "description": PLACEHOLDER},
                            {"name": "ghost", "description": "not in warehouse"},
                        ],
                    }
                ],
            }
        ),
    )
    _write(
        models / "staging" / "sources_sales.yml",
        yaml.dump(
            {
                "version": 2,
                "sources": [
                    {
                        "name": "sales",
                        "description": "dbt source desc",
                        "schema": "raw_sales",
                        "tables": [
                            {
                                "name": "orders",
                                "description": "Raw orders",
                                "columns": [
                                    {"name": "id", "description": "Order id", "tests": ["unique"]},
                                    {"name": "region"},
                                ],
                            },
                            {"name": "other", "description": "Other"},
                        ],
                    }
                ],
            }
        ),
    )
    _write(
        tmp_path / ".dango" / "sources.yml",
        "version: '1.0'\nsources:\n"
        "  - name: sales\n    type: csv\n    description: Sales CSVs\n"
        "    csv:\n      directory: data/uploads/sales\n",
    )
    monkeypatch.setattr(mcp_docs, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_docs, "_git_warnings", lambda root: [])
    return tmp_path


@pytest.mark.unit
class TestGetTableSchema:
    def test_get_table_schema_missing_warehouse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_docs, "_get_project_root", lambda: tmp_path)
        result = mcp_docs.get_table_schema("some_table")
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

        monkeypatch.setattr(mcp_docs, "_get_project_root", lambda: tmp_path)
        result = mcp_docs.get_table_schema("events")

        assert result["schema"] == "raw_a"
        assert [c["name"] for c in result["columns"]] == ["a_only_col"]
        assert result["other_schemas"] == ["raw_b"]

    def test_get_table_schema_includes_descriptions(self, project: Path) -> None:
        result = mcp_docs.get_table_schema("fct_sales")
        assert result["schema"] == "marts"
        assert result["description"] == "Sales by region"
        cols = {c["name"]: c for c in result["columns"]}
        assert cols["region"]["description"] == "Region code"
        assert cols["region"]["data_tests"] == ["not_null"]
        assert cols["extra"]["description"] is None

    def test_get_table_schema_raw_table_descriptions(self, project: Path) -> None:
        result = mcp_docs.get_table_schema("orders", schema="raw_sales")
        assert result["description"] == "Raw orders"
        cols = {c["name"]: c for c in result["columns"]}
        assert cols["id"]["description"] == "Order id"
        assert cols["id"]["data_tests"] == ["unique"]  # legacy `tests:` key
        assert cols["region"]["description"] is None


@pytest.mark.unit
class TestModelAndSourceDocs:
    def test_get_model_docs_reports_undocumented_and_in_warehouse(self, project: Path) -> None:
        result = mcp_docs.get_model_docs("fct_sales")
        assert result["layer"] == "marts"
        assert result["docs_path"] == "dbt/models/marts/schema.yml"
        in_wh = {c["name"]: c["in_warehouse"] for c in result["columns"]}
        assert in_wh == {"region": True, "total": True, "ghost": False}
        assert result["undocumented_columns"] == ["extra"]

    def test_get_model_docs_no_warehouse_is_none(self, project: Path) -> None:
        (project / "data" / "warehouse.duckdb").unlink()
        result = mcp_docs.get_model_docs("fct_sales")
        assert all(c["in_warehouse"] is None for c in result["columns"])
        assert result["undocumented_columns"] == []

    def test_get_model_docs_unknown_model_hint(self, project: Path) -> None:
        result = mcp_docs.get_model_docs("fct_sale")
        assert "not found" in result["error"]
        assert "fct_sales" in result["error"]

    def test_get_source_docs(self, project: Path) -> None:
        result = mcp_docs.get_source_docs("sales")
        assert result["source_description"] == "Sales CSVs"
        assert result["dbt_source_description"] == "dbt source desc"
        orders = next(t for t in result["tables"] if t["name"] == "orders")
        assert orders["description"] == "Raw orders"
        assert orders["columns"][0]["description"] == "Order id"
        assert "error" in mcp_docs.get_source_docs("nope")


@pytest.mark.unit
class TestUpdateSourceTableDocs:
    def test_merges_and_preserves(self, project: Path) -> None:
        path = project / "dbt" / "models" / "staging" / "sources_sales.yml"
        r = mcp_docs.update_source_table_docs(
            "sales",
            "orders",
            description="New",
            columns=[{"name": "region", "description": "Region"}, {"name": "new_col"}],
        )
        assert r["status"] == "updated"
        data = yaml.safe_load(path.read_text())
        src = data["sources"][0]
        assert src["description"] == "dbt source desc" and data["version"] == 2
        assert src["tables"][1] == {"name": "other", "description": "Other"}
        orders = src["tables"][0]
        assert orders["description"] == "New"
        cols = {c["name"]: c for c in orders["columns"]}
        assert cols["id"] == {"name": "id", "description": "Order id", "tests": ["unique"]}
        assert cols["region"]["description"] == "Region"
        assert "new_col" in cols
        assert (
            mcp_docs.update_source_table_docs("sales", "orders", description="New")["status"]
            == "unchanged"
        )

    def test_errors_leave_file_untouched(self, project: Path) -> None:
        path = project / "dbt" / "models" / "staging" / "sources_sales.yml"
        before = path.read_text()
        assert (
            "not found"
            in mcp_docs.update_source_table_docs("sales", "ordres", description="x")["error"]
        )
        assert "error" in mcp_docs.update_source_table_docs("nosrc", "orders", description="x")
        assert "error" in mcp_docs.update_source_table_docs("sales", "orders")
        assert "error" in mcp_docs.update_source_table_docs("sales", "orders", columns=[{"x": 1}])
        path.write_text("sources: [unclosed\n")
        broken = path.read_text()
        assert (
            "not valid YAML"
            in mcp_docs.update_source_table_docs("sales", "orders", description="x")["error"]
        )
        assert path.read_text() == broken
        assert before != broken

    def test_regeneration_does_not_overwrite_edit(self, project: Path) -> None:
        from tests.unit.test_dbt_generator import _make_generator_mocked

        mcp_docs.update_source_table_docs("sales", "orders", description="Edited by agent")
        gen = _make_generator_mocked(project)
        source = MagicMock()
        source.name = "sales"
        source.source_type.value = "csv"
        gen._get_source_endpoints = MagicMock(return_value=["orders"])
        gen.get_table_schema = MagicMock(return_value=[{"name": "id", "type": "INTEGER"}])
        gen.infer_dedup_strategy = MagicMock(return_value=(None, []))
        gen.generate_staging_model = MagicMock(return_value="-- model sql")
        gen.generate_sources_yml = MagicMock(return_value="version: 2\nsources:\n")
        gen.generate_staging_schema_yml = MagicMock(return_value="version: 2\nmodels:\n")
        gen._enrich_columns_from_profiling = MagicMock()
        gen.generate_all_models(sources=[source], skip_customized=False, generate_schema_yml=True)
        gen.generate_sources_yml.assert_not_called()
        path = project / "dbt" / "models" / "staging" / "sources_sales.yml"
        assert (
            yaml.safe_load(path.read_text())["sources"][0]["tables"][0]["description"]
            == "Edited by agent"
        )


@pytest.mark.unit
class TestDocsCoverage:
    def test_docs_coverage_counts_placeholders(self, project: Path) -> None:
        result = mcp_docs.docs_coverage()
        row = next(m for m in result["models"] if m["model"] == "fct_sales")
        assert row["has_description"] is True
        assert row["columns_total"] == 4  # region, total, extra (warehouse) + ghost (documented)
        assert row["columns_described"] == 2  # region, ghost; total is a placeholder
        assert row["placeholders"] == 1
        assert "total" in row["undocumented_columns"] and "extra" in row["undocumented_columns"]
        stg = next(m for m in result["models"] if m["model"] == "stg_sales__orders")
        assert stg["has_description"] is False
        assert result["totals"]["models"] == 2

    def test_layer_filter(self, project: Path) -> None:
        models = mcp_docs.docs_coverage(layer="marts")["models"]
        assert [m["model"] for m in models] == ["fct_sales"]


@pytest.mark.unit
class TestGenerateDocs:
    def test_generate_docs_lock_busy(self, project: Path) -> None:
        with (
            patch("dango.utils.dbt_lock.DbtLock.acquire", side_effect=DbtLockError("busy")),
            patch("dango.transformation.generate_dbt_docs") as gen,
        ):
            result = mcp_docs.generate_docs()
        assert "Warehouse busy" in result["error"]
        gen.assert_not_called()

    def test_generate_docs_releases_lock_on_failure(self, project: Path) -> None:
        with patch(
            "dango.transformation.generate_dbt_docs", return_value=(False, "\x1b[31mboom\x1b[0m")
        ):
            result = mcp_docs.generate_docs()
        assert result == {"status": "failed", "output": "boom"}
        from dango.utils import DbtLock

        lock = DbtLock(project)
        assert lock.acquire(timeout=0)
        lock.release()

    def test_generate_docs_releases_lock_when_generation_raises(self, project: Path) -> None:
        with patch("dango.transformation.generate_dbt_docs", side_effect=RuntimeError("x")):
            assert mcp_docs.generate_docs()["status"] == "failed"
        from dango.utils import DbtLock

        lock = DbtLock(project)
        assert lock.acquire(timeout=0)
        lock.release()

    def test_generate_docs_success_truncates_tail(self, project: Path) -> None:
        with patch("dango.transformation.generate_dbt_docs", return_value=(True, "x" * 20000)):
            result = mcp_docs.generate_docs()
        assert result["status"] == "completed" and len(result["output"]) == 10000


@pytest.mark.unit
def test_dbt_subprocesses_do_not_inherit_stdin(tmp_path: Path) -> None:
    from dango import transformation

    (tmp_path / "dbt").mkdir()
    ok = MagicMock(returncode=0, stdout="", stderr="")
    for call in (
        lambda: transformation.generate_dbt_docs(tmp_path),
        lambda: transformation.run_dbt_models(tmp_path),
        lambda: transformation.run_dbt_snapshots(tmp_path),
    ):
        with (
            patch("subprocess.run", return_value=ok) as run,
            patch("dango.utils.dbt_status.update_model_status"),
        ):
            call()
        assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.unit
def test_docs_tools_registered() -> None:
    from dango.cli.commands import mcp_server

    names = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
    for tool in (
        "get_table_schema",
        "get_model_docs",
        "get_source_docs",
        "update_source_table_docs",
        "docs_coverage",
        "generate_docs",
    ):
        assert names.count(tool) == 1
