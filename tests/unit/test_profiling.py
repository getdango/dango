"""tests/unit/test_profiling.py

Unit tests for the profiling engine in dango/utils/post_sync.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.utils.post_sync import _is_numeric, _is_string, _run_profiling, profile_table

# ---------------------------------------------------------------------------
# Type classification
# ---------------------------------------------------------------------------

_ALL_NUMERIC = [
    "INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT",
    "DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC",
    "UBIGINT", "UINTEGER", "USMALLINT", "UTINYINT", "INT1", "INT2", "INT4", "INT8",
]  # fmt: skip


@pytest.mark.unit
class TestTypeClassification:
    """Tests for _is_numeric and _is_string helpers."""

    @pytest.mark.parametrize("dtype", _ALL_NUMERIC)
    def test_numeric_types(self, dtype: str) -> None:
        """All known numeric types return True."""
        assert _is_numeric(dtype) is True

    def test_decimal_with_precision(self) -> None:
        """DECIMAL(10,2) is numeric after stripping precision."""
        assert _is_numeric("DECIMAL(10,2)") is True

    def test_numeric_case_insensitive(self) -> None:
        """Lowercase 'integer' is numeric."""
        assert _is_numeric("integer") is True

    def test_non_numeric(self) -> None:
        """VARCHAR, BOOLEAN, TIMESTAMP, DATE, STRUCT are not numeric."""
        for t in ("VARCHAR", "BOOLEAN", "TIMESTAMP", "DATE", "STRUCT"):
            assert _is_numeric(t) is False

    @pytest.mark.parametrize("dtype", ["VARCHAR", "TEXT", "CHAR", "BLOB", "UUID", "STRING"])
    def test_string_types(self, dtype: str) -> None:
        """All known string types return True."""
        assert _is_string(dtype) is True

    def test_varchar_with_length(self) -> None:
        """VARCHAR(255) is string after stripping length."""
        assert _is_string("VARCHAR(255)") is True

    def test_string_case_insensitive(self) -> None:
        """Lowercase 'varchar' is string."""
        assert _is_string("varchar") is True

    def test_non_string(self) -> None:
        """INTEGER, BOOLEAN, TIMESTAMP are not string."""
        for t in ("INTEGER", "BOOLEAN", "TIMESTAMP"):
            assert _is_string(t) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_warehouse(tmp_path: Path) -> None:
    """Create data/warehouse.duckdb in *tmp_path*."""
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    (db_dir / "warehouse.duckdb").touch()


def _mock_sqlite(mock_connect: MagicMock) -> MagicMock:
    """Configure *mock_connect* (for dango_db.connect) and return the mock conn."""
    sqlite_conn = MagicMock()
    mock_connect.return_value.__enter__ = MagicMock(return_value=sqlite_conn)
    mock_connect.return_value.__exit__ = MagicMock(return_value=False)
    return sqlite_conn


def _mock_duckdb(columns, total_rows, agg_results, sample_results) -> MagicMock:
    """Build a mock DuckDB connection that dispatches queries by SQL content."""
    mock_conn = MagicMock()

    def execute(sql: str) -> MagicMock:
        r = MagicMock()
        if "information_schema.columns" in sql:
            r.fetchall.return_value = columns
        elif sql.strip().startswith("SELECT COUNT(*)") and "AS" not in sql:
            r.fetchone.return_value = (total_rows,)
        elif "SELECT DISTINCT" in sql:
            for col, samples in sample_results.items():
                if f'"{col}"' in sql:
                    r.fetchall.return_value = samples
                    return r
            r.fetchall.return_value = []
        else:
            for col, agg in agg_results.items():
                if f'"{col}"' in sql:
                    r.fetchone.return_value = agg
                    return r
            r.fetchone.return_value = None
        return r

    mock_conn.execute.side_effect = execute
    return mock_conn


# ---------------------------------------------------------------------------
# profile_table
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProfileTable:
    """Tests for profile_table function."""

    @patch("dango.utils.post_sync.connect")
    @patch("duckdb.connect")
    def test_numeric_column_stats(self, mock_dc, mock_sc, tmp_path):
        """Numeric columns get min/max/mean stats."""
        _make_warehouse(tmp_path)
        mock_dc.return_value = _mock_duckdb(
            [("price", "DOUBLE", "NO")],
            100,
            {"price": (5, 90, "1.0", "99.0", "50.0")},
            {"price": [("10.5",), ("20.0",)]},
        )
        _mock_sqlite(mock_sc)
        stats = profile_table(tmp_path, "shopify", "orders")
        assert stats["price"]["null_count"] == "5"
        assert stats["price"]["null_pct"] == "5.0"
        assert stats["price"]["distinct_count"] == "90"
        assert stats["price"]["min"] == "1.0"
        assert stats["price"]["max"] == "99.0"
        assert stats["price"]["mean"] == "50.0"
        assert stats["price"]["sample_values"] is not None

    @patch("dango.utils.post_sync.connect")
    @patch("duckdb.connect")
    def test_string_column_stats(self, mock_dc, mock_sc, tmp_path):
        """String columns get min_length/max_length stats."""
        _make_warehouse(tmp_path)
        mock_dc.return_value = _mock_duckdb(
            [("email", "VARCHAR", "YES")],
            50,
            {"email": (3, 45, "5", "80")},
            {"email": [("alice@x.com",)]},
        )
        _mock_sqlite(mock_sc)
        stats = profile_table(tmp_path, "shopify", "customers")
        assert stats["email"]["null_count"] == "3"
        assert stats["email"]["min_length"] == "5"
        assert stats["email"]["max_length"] == "80"

    @patch("dango.utils.post_sync.connect")
    @patch("duckdb.connect")
    def test_zero_row_table(self, mock_dc, mock_sc, tmp_path):
        """Zero-row table returns null_pct=0.0."""
        _make_warehouse(tmp_path)
        mock_dc.return_value = _mock_duckdb(
            [("id", "INTEGER", "NO")],
            0,
            {"id": (0, 0, None, None, None)},
            {"id": []},
        )
        _mock_sqlite(mock_sc)
        stats = profile_table(tmp_path, "shopify", "empty_table")
        assert stats["id"]["null_count"] == "0"
        assert stats["id"]["null_pct"] == "0.0"
        assert stats["id"]["min"] is None

    @patch("dango.utils.post_sync.connect")
    @patch("duckdb.connect")
    def test_all_null_column(self, mock_dc, mock_sc, tmp_path):
        """Column where all values are null returns null_pct=100.0."""
        _make_warehouse(tmp_path)
        mock_dc.return_value = _mock_duckdb(
            [("notes", "VARCHAR", "YES")],
            10,
            {"notes": (10, 0, None, None)},
            {"notes": []},
        )
        _mock_sqlite(mock_sc)
        stats = profile_table(tmp_path, "shopify", "orders")
        assert stats["notes"]["null_pct"] == "100.0"
        assert stats["notes"]["distinct_count"] == "0"

    @patch("dango.utils.post_sync.connect")
    @patch("duckdb.connect")
    def test_sample_values_capped_at_five(self, mock_dc, mock_sc, tmp_path):
        """Sample values query returns up to 5 values."""
        _make_warehouse(tmp_path)
        mock_dc.return_value = _mock_duckdb(
            [("name", "VARCHAR", "NO")],
            100,
            {"name": (0, 100, "2", "50")},
            {"name": [("a",), ("b",), ("c",), ("d",), ("e",)]},
        )
        _mock_sqlite(mock_sc)
        stats = profile_table(tmp_path, "shopify", "products")
        assert len(json.loads(stats["name"]["sample_values"])) == 5

    @patch("dango.utils.post_sync.connect")
    @patch("duckdb.connect")
    def test_results_cached_in_sqlite(self, mock_dc, mock_sc, tmp_path):
        """Profiling results are written to SQLite via connect()."""
        _make_warehouse(tmp_path)
        mock_dc.return_value = _mock_duckdb(
            [("id", "INTEGER", "NO")],
            10,
            {"id": (0, 10, "1", "10", "5.5")},
            {"id": [("1",)]},
        )
        sqlite_conn = _mock_sqlite(mock_sc)
        profile_table(tmp_path, "shopify", "orders")
        insert_calls = [c for c in sqlite_conn.execute.call_args_list if "INSERT" in str(c)]
        assert len(insert_calls) > 0
        sqlite_conn.commit.assert_called_once()

    @patch("dango.utils.post_sync.connect")
    @patch("duckdb.connect")
    def test_per_column_error_isolation(self, mock_dc, mock_sc, tmp_path):
        """One column failing does not block profiling of other columns."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()

        def execute(sql):
            r = MagicMock()
            if "information_schema.columns" in sql:
                r.fetchall.return_value = [
                    ("good_col", "INTEGER", "NO"),
                    ("bad_col", "INTEGER", "NO"),
                ]
            elif sql.strip().startswith("SELECT COUNT(*)") and "AS" not in sql:
                r.fetchone.return_value = (10,)
            elif '"bad_col"' in sql:
                raise RuntimeError("simulated")
            elif "SELECT DISTINCT" in sql:
                r.fetchall.return_value = [("1",)]
            else:
                r.fetchone.return_value = (0, 10, "1", "10", "5.5")
            return r

        mock_conn.execute.side_effect = execute
        mock_dc.return_value = mock_conn
        _mock_sqlite(mock_sc)
        stats = profile_table(tmp_path, "shopify", "orders")
        assert "good_col" in stats
        assert "bad_col" not in stats

    @patch("duckdb.connect")
    def test_empty_table_returns_empty(self, mock_dc, tmp_path):
        """Table with no columns returns empty dict."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_dc.return_value = mock_conn
        assert profile_table(tmp_path, "shopify", "no_columns") == {}


# ---------------------------------------------------------------------------
# _run_profiling
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunProfiling:
    """Tests for _run_profiling post-sync hook."""

    @patch("dango.utils.post_sync.profile_table")
    @patch("duckdb.connect")
    def test_profiles_discovered_tables(self, mock_dc, mock_profile, tmp_path):
        """Profile_table is called for each discovered user table."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",), ("customers",)]
        mock_dc.return_value = mock_conn
        mock_profile.return_value = {}
        _run_profiling(tmp_path, ["shopify"])
        assert mock_profile.call_count == 2
        mock_profile.assert_any_call(tmp_path, "shopify", "orders")
        mock_profile.assert_any_call(tmp_path, "shopify", "customers")

    @patch("dango.utils.post_sync.profile_table")
    @patch("duckdb.connect")
    def test_excludes_dlt_tables(self, mock_dc, mock_profile, tmp_path):
        """dlt internal tables are excluded by the SQL filter."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",)]
        mock_dc.return_value = mock_conn
        mock_profile.return_value = {}
        _run_profiling(tmp_path, ["shopify"])
        sql_arg = mock_conn.execute.call_args[0][0]
        assert "_dlt_%" in sql_arg
        assert "spreadsheet" in sql_arg

    def test_missing_warehouse_graceful(self, tmp_path):
        """Missing warehouse.duckdb causes graceful early return."""
        _run_profiling(tmp_path, ["shopify"])

    @patch("dango.utils.post_sync.profile_table")
    @patch("duckdb.connect")
    def test_per_source_error_isolation(self, mock_dc, mock_profile, tmp_path):
        """One source failing does not block others."""
        _make_warehouse(tmp_path)
        call_idx = 0

        def connect_se(path, read_only=False):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                raise RuntimeError("bad source")
            conn = MagicMock()
            conn.execute.return_value.fetchall.return_value = [("orders",)]
            return conn

        mock_dc.side_effect = connect_se
        mock_profile.return_value = {}
        _run_profiling(tmp_path, ["bad_source", "good_source"])
        mock_profile.assert_called_once_with(tmp_path, "good_source", "orders")

    @patch("dango.utils.post_sync.profile_table")
    @patch("duckdb.connect")
    def test_per_table_error_isolation(self, mock_dc, mock_profile, tmp_path):
        """One table failing does not block profiling of other tables."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("bad_table",), ("good_table",)]
        mock_dc.return_value = mock_conn
        mock_profile.side_effect = [RuntimeError("table error"), {}]
        _run_profiling(tmp_path, ["shopify"])
        assert mock_profile.call_count == 2
        mock_profile.assert_any_call(tmp_path, "shopify", "good_table")
