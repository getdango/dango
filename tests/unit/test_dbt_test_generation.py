"""tests/unit/test_dbt_test_generation.py

Tests for dbt test auto-generation in staging schema.yml.

Covers:
- get_table_schema() unique test inference for _id columns
- staging_schema.yml.j2 rendering of column tests
- _enrich_columns_from_profiling() adding not_null from profiling
- Post-sync enrichment of existing schema.yml
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.transformation.generator import DbtModelGenerator


@pytest.mark.unit
class TestGetTableSchemaUniqueTests:
    """get_table_schema adds unique test for _id suffix columns."""

    def _make_generator(self, tmp_path: Path) -> DbtModelGenerator:
        db_path = tmp_path / "data"
        db_path.mkdir()
        return DbtModelGenerator(tmp_path)

    def test_exact_id_gets_unique(self, tmp_path):
        """Column named 'id' should get unique test."""
        gen = self._make_generator(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("id", "VARCHAR", "NO"),
        ]
        with patch("duckdb.connect", return_value=mock_conn):
            (tmp_path / "data" / "warehouse.duckdb").touch()
            columns = gen.get_table_schema("test_table")

        assert len(columns) == 1
        assert "unique" in columns[0]["tests"]
        assert "not_null" in columns[0]["tests"]

    def test_id_suffix_no_unique(self, tmp_path):
        """Column ending in '_id' (e.g. customer_id) is a foreign key — no unique test."""
        gen = self._make_generator(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("customer_id", "VARCHAR", "YES"),
        ]
        with patch("duckdb.connect", return_value=mock_conn):
            (tmp_path / "data" / "warehouse.duckdb").touch()
            columns = gen.get_table_schema("test_table")

        assert len(columns) == 1
        assert "unique" not in columns[0]["tests"]

    def test_non_id_column_no_unique(self, tmp_path):
        """Regular column like 'name' should not get unique test."""
        gen = self._make_generator(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("name", "VARCHAR", "YES"),
        ]
        with patch("duckdb.connect", return_value=mock_conn):
            (tmp_path / "data" / "warehouse.duckdb").touch()
            columns = gen.get_table_schema("test_table")

        assert len(columns) == 1
        assert "unique" not in columns[0]["tests"]

    def test_uuid_gets_unique(self, tmp_path):
        """Column named 'uuid' should get unique test."""
        gen = self._make_generator(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("uuid", "VARCHAR", "YES"),
        ]
        with patch("duckdb.connect", return_value=mock_conn):
            (tmp_path / "data" / "warehouse.duckdb").touch()
            columns = gen.get_table_schema("test_table")

        assert "unique" in columns[0]["tests"]


@pytest.mark.unit
class TestStagingSchemaTemplate:
    """staging_schema.yml.j2 renders tests correctly."""

    def test_renders_tests_in_schema_yml(self, tmp_path):
        """Tests should appear in the generated staging schema YAML."""
        gen = DbtModelGenerator(tmp_path)
        source = MagicMock()
        source.name = "shopify"
        source.type.value = "shopify"

        models = [
            {
                "name": "stg_shopify__orders",
                "table_name": "orders",
                "schema_name": "raw_shopify",
                "columns": [
                    {"name": "order_id", "tests": ["unique", "not_null"]},
                    {"name": "customer_name", "tests": []},
                    {"name": "created_at", "tests": ["not_null"]},
                ],
            }
        ]

        result = gen.generate_staging_schema_yml(source, models)
        parsed = yaml.safe_load(result)

        assert parsed["version"] == 2
        model = parsed["models"][0]
        assert model["name"] == "stg_shopify__orders"

        cols = model["columns"]
        # order_id has both tests
        order_id_col = next(c for c in cols if c["name"] == "order_id")
        assert "unique" in order_id_col["tests"]
        assert "not_null" in order_id_col["tests"]

        # customer_name has no tests key (empty tests = no block rendered)
        customer_col = next(c for c in cols if c["name"] == "customer_name")
        assert "tests" not in customer_col

        # created_at has not_null
        created_col = next(c for c in cols if c["name"] == "created_at")
        assert created_col["tests"] == ["not_null"]

    def test_no_tests_means_no_tests_key(self, tmp_path):
        """Columns with no tests should not have a tests key in YAML."""
        gen = DbtModelGenerator(tmp_path)
        source = MagicMock()
        source.name = "csv"
        source.type.value = "csv"

        models = [
            {
                "name": "stg_csv__data",
                "table_name": "data",
                "schema_name": "raw_csv",
                "columns": [
                    {"name": "col1", "tests": []},
                    {"name": "col2"},
                ],
            }
        ]

        result = gen.generate_staging_schema_yml(source, models)
        parsed = yaml.safe_load(result)
        cols = parsed["models"][0]["columns"]
        for col in cols:
            assert "tests" not in col


@pytest.mark.unit
class TestEnrichColumnsFromProfiling:
    """_enrich_columns_from_profiling adds not_null from profiling data."""

    def test_adds_not_null_for_zero_null_columns(self, tmp_path):
        """Columns with 0% null rate should get not_null test."""
        gen = DbtModelGenerator(tmp_path)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("order_id",),
            ("status",),
        ]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        columns = [
            {"name": "order_id", "tests": ["unique"]},
            {"name": "status", "tests": []},
            {"name": "notes", "tests": []},
        ]

        with patch("dango.utils.dango_db.connect", return_value=mock_conn):
            gen._enrich_columns_from_profiling("shopify", "orders", columns)

        # order_id already had unique, now also has not_null
        assert "not_null" in columns[0]["tests"]
        assert "unique" in columns[0]["tests"]
        # status gets not_null
        assert "not_null" in columns[1]["tests"]
        # notes was not in profiling results — no change
        assert "not_null" not in columns[2]["tests"]

    def test_does_not_duplicate_not_null(self, tmp_path):
        """If not_null already exists, don't add it again."""
        gen = DbtModelGenerator(tmp_path)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("order_id",)]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        columns = [
            {"name": "order_id", "tests": ["not_null", "unique"]},
        ]

        with patch("dango.utils.dango_db.connect", return_value=mock_conn):
            gen._enrich_columns_from_profiling("shopify", "orders", columns)

        assert columns[0]["tests"].count("not_null") == 1

    def test_handles_db_error_gracefully(self, tmp_path):
        """Database errors should leave columns unchanged."""
        gen = DbtModelGenerator(tmp_path)

        columns = [{"name": "id", "tests": []}]

        with patch(
            "dango.utils.dango_db.connect",
            side_effect=Exception("DB error"),
        ):
            gen._enrich_columns_from_profiling("shopify", "orders", columns)

        assert columns == [{"name": "id", "tests": []}]


@pytest.mark.unit
class TestPostSyncEnrichStagingTests:
    """Post-sync _enrich_staging_tests updates existing schema.yml."""

    def test_enriches_existing_schema_yml(self, tmp_path):
        """Should add not_null to columns with 0% null rate."""
        from dango.utils.post_sync import _enrich_staging_tests

        # Create staging dir with a schema file
        staging_dir = tmp_path / "dbt" / "models" / "staging"
        staging_dir.mkdir(parents=True)

        schema_content = {
            "version": 2,
            "models": [
                {
                    "name": "stg_shopify__orders",
                    "columns": [
                        {"name": "order_id", "tests": ["unique"]},
                        {"name": "customer_name"},
                    ],
                }
            ],
        }
        header = "# Auto-generated by Dango\n"
        schema_file = staging_dir / "stg_shopify.yml"
        schema_file.write_text(header + yaml.dump(schema_content, sort_keys=False))

        # Mock connect to return profiling data
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("order_id",)]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("dango.utils.post_sync.connect", return_value=mock_conn):
            _enrich_staging_tests(tmp_path, ["shopify"])

        # Verify the file was updated
        updated = yaml.safe_load(schema_file.read_text())
        order_col = updated["models"][0]["columns"][0]
        assert "not_null" in order_col["tests"]
        assert "unique" in order_col["tests"]

    def test_skips_when_no_schema_file(self, tmp_path):
        """Should not fail when staging schema file doesn't exist."""
        from dango.utils.post_sync import _enrich_staging_tests

        staging_dir = tmp_path / "dbt" / "models" / "staging"
        staging_dir.mkdir(parents=True)

        # No schema file — should silently return
        _enrich_staging_tests(tmp_path, ["shopify"])

    def test_skips_when_no_zero_null_columns(self, tmp_path):
        """Should not modify schema when no columns have 0% null rate."""
        from dango.utils.post_sync import _enrich_staging_tests

        staging_dir = tmp_path / "dbt" / "models" / "staging"
        staging_dir.mkdir(parents=True)

        schema_content = {
            "version": 2,
            "models": [
                {
                    "name": "stg_shopify__orders",
                    "columns": [{"name": "order_id"}],
                }
            ],
        }
        schema_file = staging_dir / "stg_shopify.yml"
        original = yaml.dump(schema_content, sort_keys=False)
        schema_file.write_text(original)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("dango.utils.post_sync.connect", return_value=mock_conn):
            _enrich_staging_tests(tmp_path, ["shopify"])

        # File should be unchanged (no zero null columns found)
        assert schema_file.read_text() == original

    def test_preserves_header_comment(self, tmp_path):
        """Should preserve the auto-generated header comment including blank lines."""
        from dango.utils.post_sync import _enrich_staging_tests

        staging_dir = tmp_path / "dbt" / "models" / "staging"
        staging_dir.mkdir(parents=True)

        # Header with blank line between comments and body (matches real template)
        header = "# Auto-generated by Dango on 2026-01-01\n# Source: shopify\n\n"
        schema_content = {
            "version": 2,
            "models": [
                {
                    "name": "stg_shopify__orders",
                    "columns": [{"name": "order_id"}],
                }
            ],
        }
        schema_file = staging_dir / "stg_shopify.yml"
        schema_file.write_text(header + yaml.dump(schema_content, sort_keys=False))

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("order_id",)]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("dango.utils.post_sync.connect", return_value=mock_conn):
            _enrich_staging_tests(tmp_path, ["shopify"])

        content = schema_file.read_text()
        assert content.startswith("# Auto-generated by Dango on 2026-01-01\n# Source: shopify\n\n")
