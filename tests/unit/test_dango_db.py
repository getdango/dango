"""tests/unit/test_dango_db.py

Tests for the dango metadata database (dango/utils/dango_db.py).
"""

from __future__ import annotations

import pytest

from dango.utils.dango_db import get_connection, get_dango_db_path

EXPECTED_TABLES = {
    "profiling_stats",
    "schema_baselines",
    "drift_events",
    "pii_findings",
    "notebook_locks",
    "notebook_metadata",
    "metric_history",
    "metric_results",
}


@pytest.mark.unit
class TestDangoDb:
    """Unit tests for dango.db connection and schema management."""

    def test_get_dango_db_path(self, tmp_path):
        """get_dango_db_path returns the correct path."""
        path = get_dango_db_path(tmp_path)
        assert path == tmp_path / ".dango" / "dango.db"

    def test_get_connection_creates_directory_and_file(self, tmp_path):
        """get_connection creates .dango/ dir and dango.db file."""
        conn = get_connection(tmp_path)
        try:
            db_path = get_dango_db_path(tmp_path)
            assert db_path.parent.is_dir()
            assert db_path.is_file()
        finally:
            conn.close()

    def test_all_tables_exist(self, tmp_path):
        """All 8 tables are created on first connection."""
        conn = get_connection(tmp_path)
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = {row["name"] for row in cursor.fetchall()}
            assert EXPECTED_TABLES.issubset(tables)
        finally:
            conn.close()

    def test_get_connection_idempotent(self, tmp_path):
        """Calling get_connection twice does not error."""
        conn1 = get_connection(tmp_path)
        conn1.close()
        conn2 = get_connection(tmp_path)
        try:
            cursor = conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row["name"] for row in cursor.fetchall()}
            assert EXPECTED_TABLES.issubset(tables)
        finally:
            conn2.close()

    def test_wal_mode_enabled(self, tmp_path):
        """WAL journal mode is enabled."""
        conn = get_connection(tmp_path)
        try:
            cursor = conn.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()[0]
            assert mode == "wal"
        finally:
            conn.close()
