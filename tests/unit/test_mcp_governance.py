"""tests/unit/test_mcp_governance.py

Tests for the MCP governance tools (dango/cli/commands/mcp_governance.py) and PII masking
in the MCP query() tool.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from dango.cli.commands import mcp_governance, mcp_server
from dango.governance.pii_detector import _cache_findings, get_pii_findings
from dango.governance.pii_overrides import get_pii_overrides
from dango.governance.pii_overrides import set_pii_override as _raw_set

FAKE = ["fake-a@example.invalid", "fake-b@example.invalid"]


def _finding(table: str, column: str, source: str = "test_source") -> dict[str, Any]:
    return {
        "source": source,
        "table_name": table,
        "column_name": column,
        "entity_type": "EMAIL_ADDRESS",
        "confidence": 0.9,
        "sample_count": 2,
        "scanned_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_governance, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "_get_project_root", lambda: tmp_path)
    (tmp_path / "data").mkdir()
    conn = duckdb.connect(str(tmp_path / "data" / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA marts")
    conn.execute("CREATE TABLE marts.fct_customers (id INTEGER, email VARCHAR)")
    conn.execute("INSERT INTO marts.fct_customers VALUES (1, ?), (2, ?)", FAKE)
    conn.execute("CREATE SCHEMA raw_test_source")
    conn.execute("CREATE TABLE raw_test_source.customers (id INTEGER, email VARCHAR)")
    conn.close()
    _cache_findings(tmp_path, "test_source", "customers", [_finding("customers", "email")])
    return tmp_path


@pytest.mark.unit
class TestQueryMasking:
    @pytest.mark.anyio
    async def test_query_masks_pii_by_default(self, project: Path) -> None:
        result = await mcp_server.query("select * from marts.fct_customers order by id")
        assert result["rows"] == [[1, "[PII masked]"], [2, "[PII masked]"]]
        assert result["pii_masking"]["masked_columns"] == ["email"]
        dumped = json.dumps(result)
        assert all(v not in dumped for v in FAKE)

    @pytest.mark.anyio
    async def test_query_masks_duplicate_email_columns(self, project: Path) -> None:
        result = await mcp_server.query(
            "select a.email, b.email from marts.fct_customers a "
            "join marts.fct_customers b using (id)"
        )
        assert result["pii_masking"]["masked_columns"] == ["email", "email"]
        assert all(v not in json.dumps(result) for v in FAKE)

    @pytest.mark.anyio
    async def test_query_alias_not_masked_documented(self, project: Path) -> None:
        result = await mcp_server.query("select email as contact from marts.fct_customers")
        assert result["pii_masking"]["masked_columns"] == []
        assert result["rows"][0][0] in FAKE
        assert "output column names" in result["pii_masking"]["note"]

    @pytest.mark.anyio
    async def test_query_unmasked_when_opted_out(self, project: Path) -> None:
        pyml = project / ".dango" / "project.yml"
        data = yaml.safe_load(pyml.read_text())
        data["api"] = {"mcp_mask_pii": False}
        pyml.write_text(yaml.safe_dump(data))
        result = await mcp_server.query("select * from marts.fct_customers order by id")
        assert "pii_masking" not in result
        assert result["rows"][0][1] in FAKE

    @pytest.mark.anyio
    async def test_query_masking_fails_closed_on_config_error(self, project: Path) -> None:
        (project / ".dango" / "project.yml").write_text("api: [not, a, mapping")
        result = await mcp_server.query("select * from marts.fct_customers order by id")
        assert result["rows"][0][1] == "[PII masked]"

    def test_default_config_roundtrip_omits_api_section(self, project: Path, sample_config) -> None:
        from dango.config.helpers import load_config, save_config

        save_config(load_config(project), project)
        assert "api" not in yaml.safe_load((project / ".dango" / "project.yml").read_text())
        assert load_config(project).api.mcp_mask_pii is True


@pytest.mark.unit
class TestDriftTools:
    def test_get_schema_drift_and_accept(self, project: Path) -> None:
        from dango.governance.schema_drift import detect_table_drift

        assert detect_table_drift(project, "test_source", "customers") == []  # baseline
        conn = duckdb.connect(str(project / "data" / "warehouse.duckdb"))
        conn.execute("ALTER TABLE raw_test_source.customers DROP COLUMN email")
        conn.close()
        events = detect_table_drift(project, "test_source", "customers")
        assert any(e["severity"] == "breaking" for e in events)

        result = mcp_governance.get_schema_drift(source="test_source")
        assert result["events"]
        assert [a["source"] for a in result["needs_attention"]] == ["test_source"]

        accepted = mcp_governance.accept_schema_drift("test_source")
        assert accepted["status"] == "accepted"
        assert mcp_governance.get_schema_drift()["needs_attention"] == []


@pytest.mark.unit
class TestPiiTools:
    def test_get_pii_findings_has_effective_status_and_no_samples(self, project: Path) -> None:
        _raw_set(project, "test_source", "customers", "email", "not_pii", set_by="cli")
        out = mcp_governance.get_pii_findings(source="test_source")
        assert out["findings"][0]["effective_status"] == "not_pii"
        assert not {"sample", "sample_values", "values"} & set(out["findings"][0])

    def test_set_pii_override_only_tightens(self, project: Path) -> None:
        ok = mcp_governance.set_pii_override("test_source", "customers", "id", "pii", "r")
        assert ok["status"] == "set"
        assert get_pii_overrides(project)[0]["set_by"] == "mcp"
        refused = mcp_governance.set_pii_override("test_source", "customers", "email", "not_pii")
        assert (
            "dango governance pii-set test_source customers email --status not_pii"
            in (refused["error"])
        )
        assert [o["column_name"] for o in get_pii_overrides(project)] == ["id"]

    def test_delete_pii_override_refuses_pii_allows_not_pii(self, project: Path) -> None:
        _raw_set(project, "test_source", "customers", "id", "pii", set_by="cli")
        _raw_set(project, "test_source", "customers", "email", "not_pii", set_by="cli")
        assert "error" in mcp_governance.delete_pii_override("test_source", "customers", "id")
        assert mcp_governance.delete_pii_override("test_source", "customers", "email") == {
            "status": "deleted"
        }
        assert mcp_governance.delete_pii_override("test_source", "customers", "zzz") == {
            "status": "not_found"
        }
        assert [o["column_name"] for o in get_pii_overrides(project)] == ["id"]
        assert mcp_governance.list_pii_overrides()["overrides"][0]["pii_status"] == "pii"

    def test_scan_pii_unavailable_analyzer(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dango.governance.pii_detector._get_analyzer", lambda: None)
        out = mcp_governance.scan_pii("test_source")
        assert out == {"error": "PII scanning unavailable: spaCy model could not be loaded"}
        assert len(get_pii_findings(project)) == 1  # cache untouched

    def test_scan_pii_typo_table_leaves_cache_intact(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dango.governance.pii_detector._get_analyzer", lambda: object())
        out = mcp_governance.scan_pii("test_source", "custmers")
        assert "not found" in out["error"]
        assert len(get_pii_findings(project, source="test_source")) == 1

    def test_unknown_source_rejected(self, project: Path) -> None:
        for out in (
            mcp_governance.get_schema_drift(source="nope"),
            mcp_governance.accept_schema_drift("nope"),
            mcp_governance.get_pii_findings(source="nope"),
            mcp_governance.scan_pii("nope"),
            mcp_governance.list_pii_overrides(source="nope"),
            mcp_governance.set_pii_override("nope", "t", "c"),
        ):
            assert "not found" in out["error"]


@pytest.mark.unit
def test_governance_tools_registered() -> None:
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {
        "get_schema_drift",
        "accept_schema_drift",
        "get_pii_findings",
        "scan_pii",
        "list_pii_overrides",
        "set_pii_override",
        "delete_pii_override",
    } <= names
