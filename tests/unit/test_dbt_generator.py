"""tests/unit/test_dbt_generator.py

Tests for DbtModelGenerator nested table discovery (BUG-152) and
sources/stg yml protection (P0-2).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
import yaml


@pytest.mark.unit
class TestFindDltNestedTable:
    """Tests for DbtModelGenerator._find_dlt_nested_table."""

    def _make_generator(self, tmp_path: Path) -> object:
        from dango.transformation.generator import DbtModelGenerator

        gen = DbtModelGenerator(tmp_path)
        return gen

    def _create_db_with_tables(self, db_path: Path, schema: str, tables: list[str]) -> None:
        conn = duckdb.connect(str(db_path))
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        for table in tables:
            conn.execute(f'CREATE TABLE "{schema}"."{table}" (id INTEGER)')
        conn.close()

    def test_find_dlt_nested_table_found(self, tmp_path: Path) -> None:
        gen = self._make_generator(tmp_path)
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        self._create_db_with_tables(
            db_path, "raw_chess", ["players_profiles__streaming_platforms", "games"]
        )

        result = gen._find_dlt_nested_table("players_profiles_streaming_platforms", "raw_chess")
        assert result == "players_profiles__streaming_platforms"

    def test_find_dlt_nested_table_not_found(self, tmp_path: Path) -> None:
        gen = self._make_generator(tmp_path)
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        self._create_db_with_tables(db_path, "raw_chess", ["games", "players"])

        result = gen._find_dlt_nested_table("nonexistent_table", "raw_chess")
        assert result is None

    def test_find_dlt_nested_table_no_db(self, tmp_path: Path) -> None:
        gen = self._make_generator(tmp_path)
        result = gen._find_dlt_nested_table("some_table", "raw_chess")
        assert result is None

    def test_find_dlt_nested_table_multiple_underscores(self, tmp_path: Path) -> None:
        """Table with triple underscore should also be findable."""
        gen = self._make_generator(tmp_path)
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        self._create_db_with_tables(db_path, "raw_src", ["parent__child__grandchild"])

        result = gen._find_dlt_nested_table("parent_child_grandchild", "raw_src")
        assert result == "parent__child__grandchild"


def _make_generator_mocked(tmp_path: Path):
    """Create a DbtModelGenerator with DuckDB connection mocked out."""
    with patch("duckdb.connect"):
        from dango.transformation.generator import DbtModelGenerator

        gen = DbtModelGenerator(tmp_path)

    gen.staging_dir.mkdir(parents=True, exist_ok=True)
    return gen


@pytest.mark.unit
class TestSourcesYmlProtection:
    """Test that sources_*.yml is not overwritten when it already exists (P0-2)."""

    def _setup_source_mocks(self, gen, source_name: str = "my_source"):
        """Set up common mocks for a source going through generate_all_models."""
        source = MagicMock()
        source.name = source_name
        source.source_type = MagicMock()
        source.source_type.value = "csv"

        gen._get_source_endpoints = MagicMock(return_value=["orders"])
        gen.get_table_schema = MagicMock(return_value=[{"name": "id", "type": "INTEGER"}])
        gen.infer_dedup_strategy = MagicMock(return_value=(None, []))
        gen.generate_staging_model = MagicMock(return_value="-- model sql")
        gen.generate_sources_yml = MagicMock(return_value="version: 2\nsources:\n")
        gen.generate_staging_schema_yml = MagicMock(return_value="version: 2\nmodels:\n")
        gen._enrich_columns_from_profiling = MagicMock()
        return source

    def test_sources_yml_not_overwritten_when_exists(self, tmp_path: Path) -> None:
        """Existing sources_*.yml should NOT be overwritten by generate."""
        gen = _make_generator_mocked(tmp_path)
        source = self._setup_source_mocks(gen)

        # Create an existing sources file with custom content
        sources_file = gen.staging_dir / f"sources_{source.name}.yml"
        custom_content = "# User-customized sources config\nversion: 2\n"
        sources_file.write_text(custom_content)

        gen.generate_all_models(
            sources=[source],
            skip_customized=False,
            generate_schema_yml=True,
        )

        # Verify the file was NOT overwritten
        assert sources_file.read_text() == custom_content
        # generate_sources_yml should NOT have been called
        gen.generate_sources_yml.assert_not_called()

    def test_sources_yml_created_when_missing(self, tmp_path: Path) -> None:
        """sources_*.yml should be created when it doesn't exist."""
        gen = _make_generator_mocked(tmp_path)
        source = self._setup_source_mocks(gen, "new_source")

        gen.generate_all_models(
            sources=[source],
            skip_customized=False,
            generate_schema_yml=True,
        )

        sources_file = gen.staging_dir / "sources_new_source.yml"
        assert sources_file.exists()
        assert sources_file.read_text() == "version: 2\nsources:\n"

    def test_stg_yml_protection_still_works(self, tmp_path: Path) -> None:
        """Existing stg_*.yml should also NOT be overwritten (regression check)."""
        gen = _make_generator_mocked(tmp_path)
        source = self._setup_source_mocks(gen)

        # Create an existing stg file
        stg_file = gen.staging_dir / f"stg_{source.name}.yml"
        custom_stg = "# Custom staging schema\nversion: 2\n"
        stg_file.write_text(custom_stg)

        gen.generate_all_models(
            sources=[source],
            skip_customized=False,
            generate_schema_yml=True,
        )

        # stg file should be unchanged
        assert stg_file.read_text() == custom_stg


@pytest.mark.unit
class TestSourcesYmlColumnDescriptions:
    """Test that generate_sources_yml emits column descriptions when present."""

    def test_sources_yml_includes_column_descriptions(self, tmp_path: Path) -> None:
        """When columns have descriptions, they appear in the generated sources YAML."""
        from dango.transformation.generator import DbtModelGenerator

        gen = DbtModelGenerator(tmp_path)
        source = MagicMock()
        source.name = "stripe"
        source.type = MagicMock()
        source.type.value = "stripe"

        tables = [
            {
                "name": "charge",
                "columns": [
                    {"name": "id", "tests": [], "description": "Unique identifier for the charge"},
                    {"name": "amount", "tests": [], "description": ""},
                    {
                        "name": "status",
                        "tests": ["not_null"],
                        "description": "Charge status: succeeded, pending, or failed",
                    },
                ],
                "staging_columns": [],
            }
        ]

        result = gen.generate_sources_yml(source, "raw_stripe", tables)

        # Validate YAML is valid (won't raise exception)
        parsed = yaml.safe_load(result)
        assert parsed is not None

        # Validate descriptions appear in output
        assert (
            "description: Unique identifier for the charge" in result
            or 'description: "Unique identifier for the charge"' in result
        )
        assert "description: Charge status:" in result or 'description: "Charge status:' in result
        # Template has source + table descriptions, columns add 2 more (empty one is skipped)
        assert result.count("description:") == 4

    def test_full_enrichment_integration_stripe(self, tmp_path: Path) -> None:
        """Integration test: enrichment with real Stripe registry data produces descriptions in output."""
        from dango.config.models import DataSource, SourceType
        from dango.ingestion.sources.registry import get_source_metadata
        from dango.transformation.generator import DbtModelGenerator

        gen = DbtModelGenerator(tmp_path)
        gen.staging_dir.mkdir(parents=True, exist_ok=True)

        # Create a real DataSource for Stripe
        source = DataSource(
            name="payments",
            type=SourceType.STRIPE,
        )

        # Create sample tables matching actual Stripe source
        tables_for_yml = [
            {
                "name": "charge",
                "columns": [
                    {
                        "name": "id",
                        "type": "VARCHAR",
                        "nullable": False,
                        "tests": [],
                        "description": "id column",  # auto-generated placeholder
                    },
                    {
                        "name": "amount",
                        "type": "INTEGER",
                        "nullable": True,
                        "tests": [],
                        "description": "amount column",  # auto-generated placeholder
                    },
                ],
                "staging_columns": [],
            }
        ]

        # Simulate the enrichment that happens in generate_all_models
        source_reg = get_source_metadata(source.type.value) or {}
        col_descs = source_reg.get("column_descriptions", {})
        for table_entry in tables_for_yml:
            table_descs = col_descs.get(table_entry["name"], {})
            for col in table_entry["columns"]:
                if col["name"] in table_descs:
                    col["description"] = table_descs[col["name"]]

        # Now generate YAML with enriched descriptions
        result = gen.generate_sources_yml(source, "raw_stripe", tables_for_yml)

        # Verify registry descriptions appear in output (not auto-generated placeholders)
        assert "Unique identifier for the charge" in result
        assert "Amount charged in the smallest currency unit" in result
        # Verify auto-generated placeholders were replaced
        assert "id column" not in result
        assert "amount column" not in result

        # Verify valid YAML was generated
        parsed = yaml.safe_load(result)
        assert parsed is not None
        assert parsed["sources"][0]["tables"][0]["columns"][0]["description"] == (
            "Unique identifier for the charge"
        )

    def test_unknown_column_names_trigger_warning(self, tmp_path: Path) -> None:
        """Registry with unknown column names should warn during enrichment."""
        import warnings

        from dango.transformation.generator import DbtModelGenerator

        gen = DbtModelGenerator(tmp_path)
        gen.staging_dir.mkdir(parents=True, exist_ok=True)
        source = MagicMock()
        source.name = "my_stripe"
        source.type = MagicMock()
        source.type.value = "stripe"

        # Mock the registry metadata for stripe source
        mock_metadata = {
            "column_descriptions": {
                "orders": {
                    "id": "Order ID",
                    "nonexistent_col": "This column doesn't exist",
                }
            }
        }

        # Mock the source endpoints and tables
        gen._get_source_endpoints = MagicMock(return_value=["orders"])
        gen.get_table_schema = MagicMock(
            return_value=[
                {
                    "name": "id",
                    "type": "INTEGER",
                    "nullable": False,
                    "tests": [],
                    "description": "",
                }
            ]
        )
        gen.infer_dedup_strategy = MagicMock(return_value=(None, []))
        gen.generate_staging_model = MagicMock(return_value="-- model")
        gen.generate_sources_yml = MagicMock(return_value="version: 2\n")
        gen.generate_staging_schema_yml = MagicMock(return_value="version: 2\n")
        gen._enrich_columns_from_profiling = MagicMock()

        # Patch get_source_metadata to return our mock metadata
        with patch(
            "dango.ingestion.sources.registry.get_source_metadata",
            return_value=mock_metadata,
        ):
            # Capture warnings
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                gen.generate_all_models(
                    sources=[source],
                    skip_customized=False,
                    generate_schema_yml=True,
                )

                # Verify warning was raised
                assert len(w) > 0
                assert any("nonexistent_col" in str(warning.message) for warning in w)
