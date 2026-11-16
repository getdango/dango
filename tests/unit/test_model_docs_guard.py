"""tests/unit/test_model_docs_guard.py

Docs written through the model service / MCP docs tools may not carry Jinja or SQL-bearing test arguments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dango.transformation.model_service import ModelServiceError, create_model, update_model

pytestmark = pytest.mark.unit

STG = "stg_orders__orders"
MARTS_SQL = "select id, amount from {{ ref('stg_orders__orders') }}"
FILE_READ = "length((select content from read_text('/etc/passwd'))) > 0"

BAD_DESCRIPTIONS = [
    "{{ env_var('API_KEY') }}",
    "{{ env_var('API_KEY', 'x') }}",
    "{% if true %}x{% endif %}",
    "{# hidden #}",
    "{{ doc('orders') }}",
]

BAD_COLUMNS: list[list[dict[str, Any]]] = [
    [{"name": "id", "description": "{{ env_var('API_KEY') }}"}],
    [{"name": "id", "meta": {"owner": "{{ env_var('API_KEY') }}"}}],
    [{"name": "id", "data_tests": [{"not_null": {"config": {"where": FILE_READ}}}]}],
    [{"name": "id", "data_tests": [{"not_null": {"where": FILE_READ}}]}],
    [{"name": "id", "tests": [{"unique": {"config": {"store_failures": True}}}]}],
    [{"name": "id", "data_tests": [{"not_null": {"config": {"error_if": "> (select 1)"}}}]}],
    [{"name": "id", "data_tests": [{"not_null": {"config": {"fail_calc": "count(*)"}}}]}],
    [{"name": "id", "data_tests": [{"accepted_values": {"values": [1], "quote": False}}]}],
    [{"name": "id", "data_tests": [{"accepted_values": {"values": ["a') or (1=1"]}}]}],
    [
        {
            "name": "id",
            "data_tests": [{"accepted_values": {"arguments": {"values": ["x"], "quote": False}}}],
        }
    ],
    [{"name": "id", "data_tests": [{"relationships": {"to": "read_text('x')", "field": "id"}}]}],
    [{"name": "id", "data_tests": [{"relationships": {"to": "ref('a')", "field": "id or 1=1"}}]}],
    [{"name": "id", "data_tests": [{"dbt_utils.expression_is_true": {"expression": "1=1"}}]}],
    [{"name": "id", "data_tests": ["expression_is_true"]}],
    [{"name": "id", "data_tests": "not_null"}],
    [{"name": "id", "constraints": [{"type": "check", "expression": FILE_READ}]}],
    [{"name": "id", "config": {"meta": {}}}],
    [{"name": "id", "data_type": "int); select 1; --"}],
]

GOOD_COLUMNS: list[list[dict[str, Any]]] = [
    [{"name": "id", "description": "Order id", "data_tests": ["unique", "not_null"]}],
    [{"name": "id", "tests": ["unique"]}],
    [{"name": "id", "data_tests": [{"not_null": {"config": {"severity": "warn"}}}]}],
    [{"name": "id", "data_tests": [{"accepted_values": {"values": ["a", "b", 3]}}]}],
    [{"name": "id", "data_tests": [{"accepted_values": {"arguments": {"values": ["a"]}}}]}],
    [
        {
            "name": "id",
            "data_tests": [{"relationships": {"to": "ref('stg_orders__orders')", "field": "id"}}],
        }
    ],
    [{"name": "id", "data_tests": [{"relationships": {"to": "source('s', 't')", "field": "id"}}]}],
    [{"name": "id", "data_type": "decimal(18, 2)", "meta": {"owner": "finance"}, "tags": ["a"]}],
    [{"name": "id", "quote": True}],
]


def _check(description: str | None, columns: list[dict[str, Any]] | None) -> list[str]:
    from dango.transformation.model_docs_guard import check_docs_input

    return check_docs_input(description, columns)


@pytest.mark.parametrize("description", BAD_DESCRIPTIONS)
def test_rejects_jinja_description(description: str) -> None:
    assert _check(description, None)


@pytest.mark.parametrize("columns", BAD_COLUMNS)
def test_rejects_columns(columns: list[dict[str, Any]]) -> None:
    assert _check(None, columns)


@pytest.mark.parametrize("columns", GOOD_COLUMNS)
def test_accepts_columns(columns: list[dict[str, Any]]) -> None:
    assert _check("Plain description with {braces} and 'quotes'", columns) == []


# ---- service / MCP level ---------------------------------------------------


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write(tmp_path, f"dbt/models/staging/{STG}.sql", "select 1 as id, 2 as amount")
    _write(tmp_path, "dbt/models/marts/fct_a.sql", MARTS_SQL)

    def _no_parse(root: Path) -> tuple[bool, str]:
        raise AssertionError("dbt parse gate reached for rejected docs")

    monkeypatch.setattr("dango.transformation.model_service.parse_project", _no_parse)
    return tmp_path


def test_create_model_rejects_env_var_description(proj: Path) -> None:
    before = _tree(proj)
    with pytest.raises(ModelServiceError):
        create_model(proj, "fct_b", "marts", sql=MARTS_SQL, description="{{ env_var('K') }}")
    assert _tree(proj) == before


def test_update_model_rejects_where_test(proj: Path) -> None:
    before = _tree(proj)
    cols = [{"name": "id", "data_tests": [{"not_null": {"config": {"where": FILE_READ}}}]}]
    with pytest.raises(ModelServiceError):
        update_model(proj, "fct_a", columns=cols)
    with pytest.raises(ModelServiceError):
        update_model(proj, "fct_a", description="{{ env_var('K') }}")
    assert _tree(proj) == before


def test_mcp_update_source_table_docs_rejects(proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dango.cli.commands import mcp_docs

    src = {
        "version": 2,
        "sources": [{"name": "sales", "tables": [{"name": "orders", "columns": [{"name": "id"}]}]}],
    }
    _write(proj, "dbt/models/staging/sources_sales.yml", yaml.dump(src))
    monkeypatch.setattr(mcp_docs, "_get_project_root", lambda: proj)
    before = _tree(proj)
    r = mcp_docs.update_source_table_docs("sales", "orders", description="{{ env_var('K') }}")
    assert "error" in r
    cols = [{"name": "id", "data_tests": [{"not_null": {"config": {"where": FILE_READ}}}]}]
    r = mcp_docs.update_source_table_docs("sales", "orders", columns=cols)
    assert "error" in r and r["errors"]
    assert _tree(proj) == before
