"""tests/unit/test_schema_manager_preserve.py

Regression tests: dango run's schema.yml sync must not delete user-written dbt tests, config or other keys.
"""

from pathlib import Path

import pytest
import yaml

from dango.cli.schema_manager import SchemaManager

pytestmark = pytest.mark.unit

_TODO = "TODO: Add description\n(Auto-generated - edit in dbt/models/[layer]/schema.yml)"


def _mgr(tmp_path: Path) -> SchemaManager:
    return SchemaManager(tmp_path, tmp_path / "warehouse.duckdb")


def test_merge_preserves_column_tests() -> None:
    existing = {
        "version": 2,
        "models": [
            {
                "name": "fct_a",
                "description": "A",
                "config": {"tags": ["finance"]},
                "data_tests": [{"unique": {"column_name": "id"}}],
                "columns": [
                    {"name": "id", "description": "id col", "data_tests": ["unique", "not_null"]},
                    {"name": "gone", "description": "removed one"},
                ],
            }
        ],
    }
    actual = [{"name": "id", "type": "INTEGER"}, {"name": "new_col", "type": "VARCHAR"}]
    schema, changes = _mgr(Path("/nonexistent"))._merge_schema("fct_a", actual, existing, "marts")
    model = schema["models"][0]
    assert model["config"] == {"tags": ["finance"]}
    assert model["data_tests"] == [{"unique": {"column_name": "id"}}]
    assert model["columns"][0] == {
        "name": "id",
        "description": "id col",
        "data_tests": ["unique", "not_null"],
    }
    assert model["columns"][1] == {"name": "new_col", "description": _TODO}
    assert changes["removed_columns"] == [{"name": "gone", "description": "removed one"}]
    assert changes["added_columns"] == ["new_col"]


def test_merge_preserves_other_models_and_top_level_keys() -> None:
    existing = {
        "version": 2,
        "sources": [{"name": "s"}],
        "exposures": [{"name": "e"}],
        "models": [{"name": "other", "config": {"x": 1}}, {"name": "fct_a", "columns": []}],
    }
    schema, _ = _mgr(Path("/nonexistent"))._merge_schema(
        "fct_a", [{"name": "id", "type": "INTEGER"}], existing, "marts"
    )
    assert schema["sources"] == [{"name": "s"}]
    assert schema["exposures"] == [{"name": "e"}]
    assert schema["models"][0] == {"name": "other", "config": {"x": 1}}


def test_merge_keeps_description_key_for_existing_column_without_one() -> None:
    existing = {"models": [{"name": "m", "columns": [{"name": "id", "meta": {"a": 1}}]}]}
    schema, _ = _mgr(Path("/nonexistent"))._merge_schema(
        "m", [{"name": "id", "type": "INTEGER"}], existing, "marts"
    )
    assert schema["models"][0]["columns"][0] == {"name": "id", "description": "", "meta": {"a": 1}}


def test_update_schema_uses_nested_docs_file_and_nested_model(tmp_path: Path) -> None:
    models = tmp_path / "dbt" / "models" / "marts" / "finance"
    models.mkdir(parents=True)
    (models / "fct_a.sql").write_text("select 1")
    docs = models / "docs.yml"
    docs.write_text(yaml.dump({"version": 2, "models": [{"name": "fct_a", "columns": []}]}))
    mgr = _mgr(tmp_path)
    assert mgr._get_model_layer("fct_a") == "marts"
    mgr._introspect_model_columns = lambda n, layer: [{"name": "id", "type": "INTEGER"}]  # type: ignore[method-assign]
    mgr.update_schemas_for_models(["fct_a"])
    data = yaml.safe_load(docs.read_text())
    assert data["models"][0]["columns"][0]["name"] == "id"
    assert not (tmp_path / "dbt" / "models" / "marts" / "schema.yml").exists()
