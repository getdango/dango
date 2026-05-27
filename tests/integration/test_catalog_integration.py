"""tests/integration/test_catalog_integration.py

Integration tests for catalog profiling and lineage using real DuckDB and SQLite databases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dango.utils.dango_db import _schema_initialized, connect
from dango.utils.post_sync import (
    _run_profiling,
    dispatch_post_sync_hooks,
    profile_table,
)
from dango.web.routes.catalog import _build_lineage_dag, _get_impact_tree


def _create_test_warehouse(tmp_path: Path) -> Path:
    """Create a real DuckDB warehouse with test data.

    Returns:
        Path to the DuckDB file.
    """
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA raw_testshop")
    conn.execute("""
        CREATE TABLE raw_testshop.orders (
            id INTEGER NOT NULL,
            total DOUBLE,
            email VARCHAR,
            is_paid BOOLEAN,
            created_at TIMESTAMP
        )
    """)
    conn.execute("""
        INSERT INTO raw_testshop.orders VALUES
        (1, 10.50, 'alice@example.com', true, '2026-01-01 00:00:00'),
        (2, 25.00, 'bob@example.com', true, '2026-01-02 00:00:00'),
        (3, NULL, NULL, false, '2026-01-03 00:00:00'),
        (4, 100.00, 'alice@example.com', true, '2026-01-04 00:00:00'),
        (5, 0.99, 'carol@example.com', NULL, NULL)
    """)

    # Create a dlt internal table that should be excluded
    conn.execute("""
        CREATE TABLE raw_testshop._dlt_loads (
            load_id VARCHAR,
            status INTEGER
        )
    """)

    conn.close()
    return db_path


def _clear_schema_cache() -> None:
    """Clear the dango_db schema initialization cache for test isolation."""
    _schema_initialized.clear()


@pytest.mark.integration
class TestProfileTableIntegration:
    """Integration tests for profile_table with real DuckDB and SQLite."""

    def test_numeric_stats_correct(self, tmp_path: Path) -> None:
        """Numeric column stats are numerically accurate."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        stats = profile_table(tmp_path, "testshop", "orders")

        # id: INTEGER, 5 rows, no nulls
        assert stats["id"]["null_count"] == "0"
        assert stats["id"]["null_pct"] == "0.0"
        assert stats["id"]["distinct_count"] == "5"
        assert stats["id"]["min"] == "1"
        assert stats["id"]["max"] == "5"

        # total: DOUBLE, 1 null out of 5
        assert stats["total"]["null_count"] == "1"
        assert stats["total"]["null_pct"] == "20.0"
        assert stats["total"]["distinct_count"] == "4"

    def test_string_stats_correct(self, tmp_path: Path) -> None:
        """String column stats (min_length, max_length) are correct."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        stats = profile_table(tmp_path, "testshop", "orders")

        # email: VARCHAR, 1 null out of 5 (only row 3)
        assert stats["email"]["null_count"] == "1"
        assert "min_length" in stats["email"]
        assert "max_length" in stats["email"]
        # Non-null emails: alice@example.com (17), bob@example.com (15), carol@example.com (17)
        assert stats["email"]["min_length"] == "15"
        assert stats["email"]["max_length"] == "17"

    def test_sample_values_present(self, tmp_path: Path) -> None:
        """Sample values are JSON-encoded and present."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        stats = profile_table(tmp_path, "testshop", "orders")

        samples = json.loads(stats["id"]["sample_values"])
        assert isinstance(samples, list)
        assert len(samples) <= 5
        assert len(samples) > 0

    def test_stats_cached_in_sqlite(self, tmp_path: Path) -> None:
        """Stats are actually written to .dango/dango.db."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        profile_table(tmp_path, "testshop", "orders")

        # Verify in SQLite
        with connect(tmp_path) as conn:
            rows = conn.execute(
                "SELECT column_name, stat_type, stat_value "
                "FROM profiling_stats "
                "WHERE source = 'testshop' AND table_name = 'orders'"
            ).fetchall()

        assert len(rows) > 0
        stat_map: dict[str, dict[str, str | None]] = {}
        for row in rows:
            col = row[0]
            if col not in stat_map:
                stat_map[col] = {}
            stat_map[col][row[1]] = row[2]

        assert "id" in stat_map
        assert stat_map["id"]["null_count"] == "0"

    def test_zero_row_table(self, tmp_path: Path) -> None:
        """Zero-row table is handled gracefully."""
        _clear_schema_cache()
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)

        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE SCHEMA raw_empty")
        conn.execute("CREATE TABLE raw_empty.items (id INTEGER, name VARCHAR)")
        conn.close()

        stats = profile_table(tmp_path, "empty", "items")

        assert stats["id"]["null_count"] == "0"
        assert stats["id"]["null_pct"] == "0.0"
        assert stats["name"]["null_count"] == "0"


@pytest.mark.integration
class TestRunProfilingIntegration:
    """Integration test for the full hook boundary path."""

    def test_dispatch_to_profile_table(self, tmp_path: Path) -> None:
        """dispatch_post_sync_hooks → _run_profiling → profile_table end-to-end."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        dispatch_post_sync_hooks(tmp_path, ["testshop"])

        # Verify profiling stats were cached
        with connect(tmp_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM profiling_stats "
                "WHERE source = 'testshop' AND table_name = 'orders'"
            ).fetchone()[0]

        assert count > 0

    def test_dlt_tables_excluded(self, tmp_path: Path) -> None:
        """dlt internal tables are not profiled."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        _run_profiling(tmp_path, ["testshop"])

        with connect(tmp_path) as conn:
            dlt_rows = conn.execute(
                "SELECT COUNT(*) FROM profiling_stats "
                "WHERE source = 'testshop' AND table_name = '_dlt_loads'"
            ).fetchone()[0]

        assert dlt_rows == 0


@pytest.mark.integration
class TestProfilingPerformance:
    """Performance test for profiling a larger table."""

    def test_profile_large_table(self, tmp_path: Path) -> None:
        """Profile a table with 100k rows in reasonable time."""
        _clear_schema_cache()
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)

        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE SCHEMA raw_perf")
        conn.execute("""
            CREATE TABLE raw_perf.big_table AS
            SELECT
                i AS id,
                random() * 1000 AS amount,
                'user_' || (i % 100)::VARCHAR AS username
            FROM generate_series(1, 100000) t(i)
        """)
        conn.close()

        import time

        start = time.monotonic()
        stats = profile_table(tmp_path, "perf", "big_table")
        elapsed = time.monotonic() - start

        assert "id" in stats
        assert "amount" in stats
        assert "username" in stats
        # Should complete well under 5 seconds on any reasonable hardware
        assert elapsed < 5.0


def _create_test_manifest(tmp_path: Path) -> Path:
    """Write a realistic manifest.json with sources, models, tests, and deps.

    Returns:
        Path to the manifest file.
    """
    manifest: dict[str, Any] = {
        "nodes": {
            "model.shop.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "schema": "analytics",
                "config": {"materialized": "view"},
                "description": "Staging orders",
                "depends_on": {"nodes": ["source.shop.raw.orders"]},
                "columns": {
                    "id": {"name": "id", "description": "Order ID"},
                    "total": {"name": "total", "description": ""},
                },
            },
            "model.shop.fct_revenue": {
                "resource_type": "model",
                "name": "fct_revenue",
                "schema": "analytics",
                "config": {"materialized": "table"},
                "description": "",
                "depends_on": {"nodes": ["model.shop.stg_orders"]},
                "columns": {},
            },
            "test.shop.not_null_stg_orders_id": {
                "resource_type": "test",
                "name": "not_null_stg_orders_id",
                "depends_on": {"nodes": ["model.shop.stg_orders"]},
            },
        },
        "sources": {
            "source.shop.raw.orders": {
                "name": "orders",
                "schema": "raw_shop",
                "description": "Raw orders table",
                "columns": {},
                "resource_type": "source",
            },
        },
    }
    manifest_dir = tmp_path / "dbt" / "target"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


@pytest.mark.integration
class TestLineageIntegration:
    """Integration tests for lineage DAG and impact tree with real manifest."""

    def test_build_lineage_dag_from_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_build_lineage_dag returns correct nodes + edges from manifest."""
        _create_test_manifest(tmp_path)
        monkeypatch.setattr(
            "dango.web.routes.catalog.get_dbt_manifest",
            lambda: json.loads((tmp_path / "dbt" / "target" / "manifest.json").read_text()),
        )

        dag = _build_lineage_dag()

        assert dag is not None
        assert len(dag["nodes"]) == 3  # 1 source + 2 models
        assert len(dag["edges"]) == 2  # source→stg, stg→fct

        names = {n["name"] for n in dag["nodes"]}
        assert names == {"orders", "stg_orders", "fct_revenue"}

        # Verify reverse lookup
        stg = next(n for n in dag["nodes"] if n["name"] == "stg_orders")
        assert "model.shop.fct_revenue" in stg["depended_on_by"]
        assert stg["test_count"] == 1
        assert stg["columns_documented"] == 1  # id has desc, total doesn't
        assert stg["columns_total"] == 2

    def test_impact_tree_from_manifest(
        self,
        tmp_path: Path,
    ) -> None:
        """_get_impact_tree returns correct downstream tree."""
        _create_test_manifest(tmp_path)
        manifest: dict[str, Any] = json.loads(
            (tmp_path / "dbt" / "target" / "manifest.json").read_text()
        )

        # Build combined node lookup and reverse map
        all_nodes: dict[str, dict[str, Any]] = {}
        reverse_map: dict[str, list[str]] = {}
        for uid, node in manifest["nodes"].items():
            all_nodes[uid] = node
            if node.get("resource_type") == "model":
                for dep in node.get("depends_on", {}).get("nodes", []):
                    reverse_map.setdefault(dep, []).append(uid)
        for uid, src in manifest["sources"].items():
            all_nodes[uid] = src

        tree = _get_impact_tree(reverse_map, "source.shop.raw.orders", all_nodes)

        assert tree["name"] == "orders"
        assert tree["total_downstream_count"] == 2
        assert len(tree["children"]) == 1
        assert tree["children"][0]["name"] == "stg_orders"
