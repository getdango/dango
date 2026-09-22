"""tests/unit/test_pipeline_health.py

Tests for dango.utils.pipeline_health — materializes sync history, dbt test
results, and the source list into DuckDB tables (`_dango_meta` schema) for
the "Data Pipeline Health" dashboard (1.0.8-DASH-1).

Also exercises the rewritten DASHBOARD_QUERIES SQL in
dango/visualization/metabase.py by running it against a real DuckDB
connection populated by materialize_pipeline_health() — this is the
"unit test the SQL" half of the task's Tests requirement; the live
Metabase check is separate (see PR description).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from dango.utils.pipeline_health import SCHEMA, materialize_pipeline_health


def _write_project(
    project_root: Path,
    sources_yaml: str = "version: '1.0'\nsources: []\n",
) -> None:
    """Write the minimal .dango/project.yml + sources.yml get_config() needs."""
    dango_dir = project_root / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    (dango_dir / "project.yml").write_text(
        "project:\n  name: test\n  created_by: test\n  purpose: test\n"
    )
    (dango_dir / "sources.yml").write_text(sources_yaml)


def _write_history(project_root: Path, source_name: str, entries: list[dict]) -> None:
    history_dir = project_root / ".dango" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / f"{source_name}.json").write_text(json.dumps(entries))


def _write_run_results(project_root: Path, results: list[dict], generated_at: str) -> None:
    target_dir = project_root / "dbt" / "target"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "run_results.json").write_text(
        json.dumps({"metadata": {"generated_at": generated_at}, "results": results})
    )


def _query(project_root: Path, sql: str) -> list[tuple]:
    conn = duckdb.connect(str(project_root / "data" / "warehouse.duckdb"), read_only=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.mark.unit
class TestMaterializeFreshInstall:
    """A project with no sync/dbt history must materialize empty tables, never crash."""

    def test_fresh_install_creates_empty_tables(self, tmp_path: Path) -> None:
        _write_project(tmp_path)

        materialize_pipeline_health(tmp_path)

        for table in ("sync_history", "dbt_test_results", "source_overview"):
            assert _query(tmp_path, f"SELECT * FROM {SCHEMA}.{table}") == []

    def test_missing_project_config_does_not_raise(self, tmp_path: Path) -> None:
        """No .dango/project.yml at all (get_config will raise internally) — never propagates."""
        materialize_pipeline_health(tmp_path)  # should not raise

        assert _query(tmp_path, f"SELECT * FROM {SCHEMA}.source_overview") == []

    def test_idempotent_schema_creation(self, tmp_path: Path) -> None:
        """Calling twice in a row doesn't error (CREATE SCHEMA/TABLE IF NOT EXISTS)."""
        _write_project(tmp_path)

        materialize_pipeline_health(tmp_path)
        materialize_pipeline_health(tmp_path)  # should not raise


@pytest.mark.unit
class TestMaterializeSourceOverview:
    def test_reads_real_sources_yml(self, tmp_path: Path) -> None:
        _write_project(
            tmp_path,
            sources_yaml=(
                "version: '1.0'\n"
                "sources:\n"
                "  - name: orders\n"
                "    type: csv\n"
                "    enabled: true\n"
                "    csv:\n"
                "      directory: csv_data\n"
                "  - name: legacy\n"
                "    type: csv\n"
                "    enabled: false\n"
                "    csv:\n"
                "      directory: legacy_data\n"
            ),
        )

        materialize_pipeline_health(tmp_path)

        rows = _query(
            tmp_path,
            f"SELECT source_name, source_type, enabled FROM {SCHEMA}.source_overview "
            "ORDER BY source_name",
        )
        assert rows == [("legacy", "csv", False), ("orders", "csv", True)]

    def test_full_rewrite_drops_removed_sources(self, tmp_path: Path) -> None:
        """A source removed from sources.yml doesn't linger in the table."""
        _write_project(
            tmp_path,
            sources_yaml=(
                "version: '1.0'\nsources:\n  - name: a\n    type: csv\n    enabled: true\n"
                "    csv:\n      directory: a_data\n"
            ),
        )
        materialize_pipeline_health(tmp_path)
        assert _query(tmp_path, f"SELECT source_name FROM {SCHEMA}.source_overview") == [("a",)]

        _write_project(tmp_path, sources_yaml="version: '1.0'\nsources: []\n")
        materialize_pipeline_health(tmp_path)
        assert _query(tmp_path, f"SELECT source_name FROM {SCHEMA}.source_overview") == []


@pytest.mark.unit
class TestMaterializeSyncHistory:
    def test_reads_real_history_files(self, tmp_path: Path) -> None:
        _write_project(
            tmp_path,
            sources_yaml=(
                "version: '1.0'\nsources:\n  - name: orders\n    type: csv\n    enabled: true\n"
                "    csv:\n      directory: csv_data\n"
            ),
        )
        _write_history(
            tmp_path,
            "orders",
            [
                {
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "status": "success",
                    "duration_seconds": 1.5,
                    "rows_processed": 10,
                    "error_message": None,
                },
                {
                    "timestamp": "2026-08-02T00:00:00+00:00",
                    "status": "failed",
                    "duration_seconds": 0.5,
                    "rows_processed": 0,
                    "error_message": "boom",
                },
            ],
        )

        materialize_pipeline_health(tmp_path)

        rows = _query(
            tmp_path,
            f"SELECT source_name, status, rows_processed, error_message "
            f"FROM {SCHEMA}.sync_history ORDER BY sync_timestamp",
        )
        assert rows == [
            ("orders", "success", 10, None),
            ("orders", "failed", 0, "boom"),
        ]

    def test_entry_missing_timestamp_is_skipped(self, tmp_path: Path) -> None:
        """A malformed entry (no timestamp) is dropped, not a crash."""
        _write_project(
            tmp_path,
            sources_yaml=(
                "version: '1.0'\nsources:\n  - name: orders\n    type: csv\n    enabled: true\n"
                "    csv:\n      directory: csv_data\n"
            ),
        )
        _write_history(tmp_path, "orders", [{"status": "success", "rows_processed": 5}])

        materialize_pipeline_health(tmp_path)

        assert _query(tmp_path, f"SELECT * FROM {SCHEMA}.sync_history") == []


@pytest.mark.unit
class TestMaterializeDbtTestResults:
    def test_passing_and_failing_tests_classified(self, tmp_path: Path) -> None:
        _write_project(tmp_path)
        _write_run_results(
            tmp_path,
            results=[
                {"unique_id": "model.proj.stg_orders", "status": "success"},
                {"unique_id": "test.proj.not_null_orders_id", "status": "pass"},
                {"unique_id": "test.proj.not_null_orders_amount", "status": "success"},
                {"unique_id": "test.proj.accepted_values_orders_status", "status": "fail"},
                {"unique_id": "test.proj.unique_orders_id", "status": "skipped"},
            ],
            generated_at="2026-08-01T12:00:00Z",
        )

        materialize_pipeline_health(tmp_path)

        rows = _query(
            tmp_path,
            f"SELECT unique_id, status, passed FROM {SCHEMA}.dbt_test_results ORDER BY unique_id",
        )
        assert rows == [
            ("test.proj.accepted_values_orders_status", "fail", False),
            ("test.proj.not_null_orders_amount", "success", True),
            ("test.proj.not_null_orders_id", "pass", True),
            ("test.proj.unique_orders_id", "skipped", None),
        ]
        # model. node is excluded entirely — only test. nodes are kept
        assert _query(tmp_path, f"SELECT COUNT(*) FROM {SCHEMA}.dbt_test_results")[0][0] == 4

    def test_no_run_results_file_leaves_table_empty(self, tmp_path: Path) -> None:
        _write_project(tmp_path)

        materialize_pipeline_health(tmp_path)

        assert _query(tmp_path, f"SELECT * FROM {SCHEMA}.dbt_test_results") == []

    def test_malformed_run_results_does_not_raise(self, tmp_path: Path) -> None:
        _write_project(tmp_path)
        target_dir = tmp_path / "dbt" / "target"
        target_dir.mkdir(parents=True)
        (target_dir / "run_results.json").write_text("not json{{{")

        materialize_pipeline_health(tmp_path)  # should not raise


@pytest.mark.unit
class TestMaterializeLocking:
    def test_busy_lock_skips_without_raising(self, tmp_path: Path) -> None:
        """If another process holds the DbtLock, materialize skips silently."""
        from dango.utils.dbt_lock import DbtLock

        _write_project(tmp_path)

        holder = DbtLock(tmp_path, source="test", operation="hold lock")
        holder.acquire()
        try:
            materialize_pipeline_health(tmp_path)  # should not raise, should not hang
        finally:
            holder.release()

        # Schema/tables were never created since the lock was never acquired
        conn = duckdb.connect(str(tmp_path / "data" / "warehouse.duckdb"))
        try:
            schemas = [
                r[0]
                for r in conn.execute(
                    "SELECT schema_name FROM information_schema.schemata"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert SCHEMA not in schemas


@pytest.mark.unit
class TestDashboardQueriesSql:
    """Runs the real DASHBOARD_QUERIES SQL (metabase.py) against a DuckDB
    populated by materialize_pipeline_health() — the closest thing to a unit
    test for native SQL cards that only really run inside Metabase."""

    def _setup(self, tmp_path: Path) -> None:
        _write_project(
            tmp_path,
            sources_yaml=(
                "version: '1.0'\nsources:\n"
                "  - name: orders\n    type: csv\n    enabled: true\n"
                "    csv:\n      directory: csv_data\n"
            ),
        )
        now = datetime.now(timezone.utc)
        _write_history(
            tmp_path,
            "orders",
            [
                {
                    "timestamp": now.isoformat(),
                    "status": "success",
                    "duration_seconds": 1.0,
                    "rows_processed": 10,
                    "error_message": None,
                }
            ],
        )
        _write_run_results(
            tmp_path,
            results=[
                {"unique_id": "test.proj.not_null_orders_id", "status": "pass"},
                {"unique_id": "test.proj.not_null_orders_amount", "status": "fail"},
            ],
            generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        materialize_pipeline_health(tmp_path)

    def test_fresh_install_queries_do_not_error(self, tmp_path: Path) -> None:
        """Every DASHBOARD_QUERIES SQL string runs cleanly against empty tables."""
        from dango.visualization.metabase import DASHBOARD_QUERIES

        _write_project(tmp_path)
        materialize_pipeline_health(tmp_path)

        conn = duckdb.connect(str(tmp_path / "data" / "warehouse.duckdb"), read_only=True)
        try:
            for _key, query_def in DASHBOARD_QUERIES.items():
                conn.execute(query_def["sql"]).fetchall()  # must not raise
        finally:
            conn.close()

    def test_source_overview_shows_status_and_no_hardcoded_rows(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import DASHBOARD_QUERIES

        self._setup(tmp_path)
        rows = _query(tmp_path, DASHBOARD_QUERIES["source_overview"]["sql"])
        assert rows == [("orders", "csv", True, "success")]
        # The old hardcoded rows must be gone
        names = {r[0] for r in rows}
        assert "sample" not in names
        assert "demo" not in names

    def test_dbt_test_results_reflects_real_pass_fail(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import DASHBOARD_QUERIES

        self._setup(tmp_path)
        rows = _query(tmp_path, DASHBOARD_QUERIES["dbt_test_results"]["sql"])
        assert rows == [("Tests Failing", 1, 2)]

    def test_pipeline_health_score_is_not_hardcoded_100(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import DASHBOARD_QUERIES

        self._setup(tmp_path)
        rows = _query(tmp_path, DASHBOARD_QUERIES["pipeline_health_score"]["sql"])
        assert len(rows) == 1
        score, status, message = rows[0]
        # 1/1 source synced (100) + 1/2 tests passing (50), weighted 50/50 = 75
        assert score == 75.0
        assert status == "Needs Attention"
        assert "1/1 sources synced successfully" in message
        assert "1/2 dbt tests passing" in message

    def test_data_freshness_shows_real_timestamp_and_row_count(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import DASHBOARD_QUERIES

        self._setup(tmp_path)
        rows = _query(tmp_path, DASHBOARD_QUERIES["data_freshness"]["sql"])
        assert len(rows) == 1
        source_name, row_count, last_updated = rows[0]
        assert source_name == "orders"
        assert row_count == 10
        assert last_updated is not None

    def test_row_counts_trend_is_cumulative(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import DASHBOARD_QUERIES

        self._setup(tmp_path)
        rows = _query(tmp_path, DASHBOARD_QUERIES["row_counts_trend"]["sql"])
        totals = [r[1] for r in rows]
        # Cumulative sum never decreases and ends at >= the single sync's rows_processed
        assert totals == sorted(totals)
        assert totals[-1] == 10
