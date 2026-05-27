"""tests/unit/test_dbt_generator.py

Tests for DbtModelGenerator nested table discovery (BUG-152).
"""

from __future__ import annotations

from pathlib import Path

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
