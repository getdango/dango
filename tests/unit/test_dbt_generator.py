"""tests/unit/test_dbt_generator.py

Tests for DbtModelGenerator nested table discovery (BUG-152) and
sources/stg yml protection (P0-2).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest


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
