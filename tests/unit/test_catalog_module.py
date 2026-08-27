"""tests/unit/test_catalog_module.py

Tests for the dango/catalog/ module extraction — verify no FastAPI import and
public API exports.
"""

from __future__ import annotations


def test_schema_module_no_fastapi_import() -> None:
    """Verify dango.catalog.schema has no FastAPI import."""
    import dango.catalog.schema

    assert "fastapi" not in dango.catalog.schema.__dict__


def test_profiling_module_no_fastapi_import() -> None:
    """Verify dango.catalog.profiling has no FastAPI import."""
    import dango.catalog.profiling

    assert "fastapi" not in dango.catalog.profiling.__dict__


def test_lineage_module_no_fastapi_import() -> None:
    """Verify dango.catalog.lineage has no FastAPI import."""
    import dango.catalog.lineage

    assert "fastapi" not in dango.catalog.lineage.__dict__


def test_manifest_module_no_fastapi_import() -> None:
    """Verify dango.catalog.manifest has no FastAPI import."""
    import dango.catalog.manifest

    assert "fastapi" not in dango.catalog.manifest.__dict__


def test_models_module_no_fastapi_import() -> None:
    """Verify dango.catalog.models has no FastAPI import."""
    import dango.catalog.models

    assert "fastapi" not in dango.catalog.models.__dict__


def test_get_column_schema_empty_db(tmp_path) -> None:
    """Test _get_column_schema with no tables."""

    import duckdb

    from dango.catalog.schema import _get_column_schema

    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA raw_test")
    conn.execute("CREATE TABLE raw_test.empty_table (id INT, name VARCHAR)")
    conn.close()

    result = _get_column_schema(db_path, "test", "empty_table")

    assert len(result) == 2
    assert result[0]["name"] == "id"
    assert result[0]["type"] == "INTEGER"
    assert result[1]["name"] == "name"
    assert result[1]["type"] == "VARCHAR"


def test_catalog_init_exports() -> None:
    """Verify dango.catalog.__init__ exports the public API."""
    from dango.catalog import (
        get_column_schema,
        get_impact,
        get_lineage,
        get_models,
        search_catalog,
    )

    assert callable(get_lineage)
    assert callable(get_impact)
    assert callable(get_models)
    assert callable(search_catalog)
    assert callable(get_column_schema)


def test_public_api_aliases_exist() -> None:
    """Verify all 5 public API aliases are defined in __init__.py."""
    import dango.catalog

    assert hasattr(dango.catalog, "get_lineage")
    assert hasattr(dango.catalog, "get_impact")
    assert hasattr(dango.catalog, "get_models")
    assert hasattr(dango.catalog, "search_catalog")
    assert hasattr(dango.catalog, "get_column_schema")
