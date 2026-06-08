"""tests/unit/test_schema_manager.py

Tests for schema manager description generation.
"""

import pytest

from dango.cli.schema_manager import SchemaManager


@pytest.mark.unit
class TestDefaultDescription:
    """Test auto-generated descriptions for new int/marts models."""

    def test_marts_model_description(self, tmp_path):
        mgr = SchemaManager(tmp_path, tmp_path / "warehouse.duckdb")
        columns = [{"name": "id", "type": "INTEGER"}]
        schema, _ = mgr._merge_schema("investment_desk_portfolio_summary", columns, None, "marts")
        model = schema["models"][0]
        assert model["description"] == "Marts model: Investment desk portfolio summary"

    def test_intermediate_model_strips_int_prefix(self, tmp_path):
        mgr = SchemaManager(tmp_path, tmp_path / "warehouse.duckdb")
        columns = [{"name": "id", "type": "INTEGER"}]
        schema, _ = mgr._merge_schema(
            "int_investment_desk_latest_prices", columns, None, "intermediate"
        )
        model = schema["models"][0]
        assert model["description"] == "Intermediate model: Investment desk latest prices"

    def test_intermediate_model_strips_intermediate_prefix(self, tmp_path):
        mgr = SchemaManager(tmp_path, tmp_path / "warehouse.duckdb")
        columns = [{"name": "id", "type": "INTEGER"}]
        schema, _ = mgr._merge_schema("intermediate_foo_bar", columns, None, "intermediate")
        model = schema["models"][0]
        assert model["description"] == "Intermediate model: Foo bar"

    def test_existing_model_preserves_description(self, tmp_path):
        mgr = SchemaManager(tmp_path, tmp_path / "warehouse.duckdb")
        columns = [{"name": "id", "type": "INTEGER"}]
        existing = {
            "version": 2,
            "models": [
                {
                    "name": "my_model",
                    "description": "Custom user description",
                    "columns": [{"name": "id"}],
                }
            ],
        }
        schema, _ = mgr._merge_schema("my_model", columns, existing, "marts")
        model = schema["models"][0]
        assert model["description"] == "Custom user description"

    def test_existing_empty_description_preserved(self, tmp_path):
        """Empty description is treated as user choice — not overwritten."""
        mgr = SchemaManager(tmp_path, tmp_path / "warehouse.duckdb")
        columns = [{"name": "id", "type": "INTEGER"}]
        existing = {
            "version": 2,
            "models": [
                {
                    "name": "my_model",
                    "description": "",
                    "columns": [{"name": "id"}],
                }
            ],
        }
        schema, _ = mgr._merge_schema("my_model", columns, existing, "marts")
        model = schema["models"][0]
        assert model["description"] == ""
