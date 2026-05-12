"""tests/unit/test_query_retry.py

Unit tests for BUG-232 (DuckDB IOException retry) and BUG-222 (clean SQL error message).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import duckdb
import pytest


class TestExecuteQueryRetry:
    """Tests for ``_execute_query()`` IOException retry logic."""

    def _make_result(self):
        """Create a mock DuckDB result."""
        result = MagicMock()
        result.description = [("id",), ("name",)]
        result.fetchmany.return_value = [(1, "a"), (2, "b")]
        return result

    @patch("time.sleep")
    def test_retry_succeeds_on_second_attempt(self, mock_sleep):
        from dango.web.routes.query import _execute_query

        result = self._make_result()
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = [duckdb.IOException("locked"), None]
        mock_conn.execute.side_effect = None
        mock_conn.execute.return_value = result

        # First call raises IOException, second succeeds
        call_count = 0

        def connect_side_effect(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise duckdb.IOException("IO Error: could not set lock")
            conn = MagicMock()
            conn.execute.return_value = result
            return conn

        with patch("duckdb.connect", side_effect=connect_side_effect):
            data = _execute_query("/tmp/test.db", "SELECT 1", 100)

        assert data["columns"] == ["id", "name"]
        assert data["row_count"] == 2
        mock_sleep.assert_called_once_with(0.1)

    @patch("time.sleep")
    def test_exhausted_retries_raises(self, mock_sleep):
        from dango.web.routes.query import _execute_query

        with patch(
            "duckdb.connect",
            side_effect=duckdb.IOException("IO Error: locked"),
        ):
            with pytest.raises(duckdb.IOException, match="locked"):
                _execute_query("/tmp/test.db", "SELECT 1", 100)

        assert mock_sleep.call_count == 2

    def test_non_ioexception_not_retried(self):
        from dango.web.routes.query import _execute_query

        with patch(
            "duckdb.connect",
            side_effect=duckdb.ProgrammingError("syntax error"),
        ):
            with pytest.raises(duckdb.ProgrammingError):
                _execute_query("/tmp/test.db", "SELECT 1", 100)


class TestSqlErrorMessage:
    """Tests for BUG-222: sqlglot error message sanitization."""

    def test_sqlglot_error_does_not_leak_details(self):
        from dango.web.routes.query import _validate_sql

        # Craft something that passes keyword check but fails sqlglot parse
        # (starts with SELECT but has invalid syntax)
        try:
            import importlib.util

            if importlib.util.find_spec("sqlglot") is None:
                pytest.skip("sqlglot not installed")

            with pytest.raises(ValueError) as exc_info:
                _validate_sql("SELECT ,,, FROM")
            msg = str(exc_info.value)
            # Must not contain sqlglot internal details
            assert "SqlglotError" not in msg
            assert "Expected" not in msg or "check your query" in msg
            assert "Invalid SQL syntax" in msg
        except ImportError:  # pragma: no cover
            pytest.skip("sqlglot not installed")
