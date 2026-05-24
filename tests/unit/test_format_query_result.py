"""tests/unit/test_format_query_result.py

Tests for the _format_query_result helper in dango/cli/commands/remote_mgmt.py.
"""

from __future__ import annotations

import pytest

from dango.cli.commands.remote_mgmt import _format_query_result


@pytest.mark.unit
class TestFormatQueryResult:
    """Tests for _format_query_result helper."""

    def test_basic_table(self):
        """Formats a JSON result as a readable ASCII table."""
        raw = '{"columns": ["name", "age"], "rows": [["Alice", 30]], "row_count": 1, "truncated": false}'
        result = _format_query_result(raw)
        assert "name" in result
        assert "Alice" in result
        assert "30" in result
        assert "1 row(s)" in result

    def test_truncation_warning(self):
        """Shows warning when results are truncated."""
        raw = '{"columns": ["x"], "rows": [[1]], "row_count": 1, "truncated": true, "warning": "Results truncated to 10000 rows"}'
        result = _format_query_result(raw)
        assert "truncated" in result.lower()

    def test_empty_columns(self):
        """Returns OK message when no columns."""
        raw = '{"columns": [], "rows": [], "row_count": 0, "truncated": false}'
        result = _format_query_result(raw)
        assert "OK" in result

    def test_error_response(self):
        """Returns error message from error JSON."""
        raw = '{"error": "something went wrong"}'
        result = _format_query_result(raw)
        assert "something went wrong" in result

    def test_invalid_json_passthrough(self):
        """Non-JSON input is returned as-is."""
        raw = "not json at all"
        assert _format_query_result(raw) == raw
