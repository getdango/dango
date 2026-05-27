"""tests/unit/test_analyze_error.py

Unit tests for BUG-195: DuckDB lock error detection in ``_analyze_error()``.
Ensures lock-related keywords are caught before the connection handler.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def analyze_error():
    """Return the ``_analyze_error`` function from dlt_runner."""
    from dango.ingestion.dlt_runner import DltPipelineRunner

    runner = DltPipelineRunner.__new__(DltPipelineRunner)
    return runner._analyze_error


class TestLockErrorDetection:
    """Lock error keywords must be detected before connection handler."""

    @pytest.mark.parametrize(
        "error_msg",
        [
            "Could not set lock on file: database is locked",
            "IO Error: database is locked by another process",
            "Cannot open database: file already open by another connection",
            "another process is using the database",
            "could not set lock on DuckDB file",
            "write lock held by another process",
        ],
    )
    def test_lock_keywords_detected(self, analyze_error, error_msg):
        result = analyze_error(Exception(error_msg), "test_source")
        assert "DuckDB Lock Error" in result
        assert "Close any open notebooks" in result or "close" in result.lower()

    def test_lock_not_confused_with_connection(self, analyze_error):
        """Lock errors containing 'connection' should NOT trigger connection handler."""
        error = Exception("file already open by another connection")
        result = analyze_error(error, "test_source")
        assert "DuckDB Lock Error" in result
        assert "Cannot reach API" not in result

    @pytest.mark.parametrize(
        "error_msg",
        [
            "connection refused by server",
            "network timeout while connecting",
            "dns resolution failed",
        ],
    )
    def test_connection_errors_still_work(self, analyze_error, error_msg):
        result = analyze_error(Exception(error_msg), "test_source")
        assert "Connection Error" in result

    @pytest.mark.parametrize(
        "error_msg",
        [
            "clock skew detected",
            "blocked by firewall",
        ],
    )
    def test_no_false_positive_on_lock_substring(self, analyze_error, error_msg):
        """Words containing 'lock' as a substring should NOT trigger lock handler."""
        result = analyze_error(Exception(error_msg), "test_source")
        assert "DuckDB Lock Error" not in result

    def test_no_false_positive_on_generic_error(self, analyze_error):
        result = analyze_error(Exception("something went wrong"), "test_source")
        assert "DuckDB Lock Error" not in result
        assert "Connection Error" not in result
