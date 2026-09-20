"""tests/unit/test_dlt_runner_lock_retry.py

Tests for dango.ingestion.dlt_runner._connect_with_lock_retry() — the shared
Metabase-lock-conflict retry used by every DuckDB write call site outside the
dlt-pipeline path (which _load_with_lock() already covers). 1.0.8-AQ.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestConnectWithLockRetry:
    """Tests for _connect_with_lock_retry()."""

    def test_connect_with_lock_retry_succeeds_first_attempt(self) -> None:
        """No retry/sleep/warning when duckdb.connect() succeeds immediately."""
        from dango.ingestion.dlt_runner import _connect_with_lock_retry

        mock_conn = MagicMock()

        with (
            patch("duckdb.connect", return_value=mock_conn) as mock_connect,
            patch("dango.ingestion.dlt_runner.time.sleep") as mock_sleep,
            patch("dango.ingestion.dlt_runner._logging") as mock_logging,
        ):
            result = _connect_with_lock_retry(Path("/fake/warehouse.duckdb"), "my_source", "op")

        assert result is mock_conn
        mock_connect.assert_called_once_with("/fake/warehouse.duckdb")
        mock_sleep.assert_not_called()
        mock_logging.getLogger.return_value.warning.assert_not_called()

    def test_connect_with_lock_retry_retries_on_lock_error_then_succeeds(self) -> None:
        """One retry on a lock-conflict error, then success; sleeps 10s once, warns once."""
        from dango.ingestion.dlt_runner import _connect_with_lock_retry

        mock_conn = MagicMock()
        lock_error = Exception("IO Error: Could not set lock on file ...")

        with (
            patch("duckdb.connect", side_effect=[lock_error, mock_conn]) as mock_connect,
            patch("dango.ingestion.dlt_runner.time.sleep") as mock_sleep,
            patch("dango.ingestion.dlt_runner._logging") as mock_logging,
        ):
            result = _connect_with_lock_retry(Path("/fake/warehouse.duckdb"), "my_source", "op")

        assert result is mock_conn
        assert mock_connect.call_count == 2
        mock_sleep.assert_called_once_with(10)
        mock_logging.getLogger.return_value.warning.assert_called_once()

    def test_connect_with_lock_retry_exhausts_after_five_attempts(self) -> None:
        """After 5 failed attempts, the lock-conflict exception propagates."""
        from dango.ingestion.dlt_runner import _connect_with_lock_retry

        lock_error = Exception(
            'IO Error: Could not set lock on file "warehouse.duckdb": '
            "Conflicting lock is held in java (PID 3435)"
        )

        with (
            patch("duckdb.connect", side_effect=lock_error) as mock_connect,
            patch("dango.ingestion.dlt_runner.time.sleep") as mock_sleep,
        ):
            with pytest.raises(Exception, match="Conflicting lock"):
                _connect_with_lock_retry(Path("/fake/warehouse.duckdb"), "my_source", "op")

        assert mock_connect.call_count == 5
        # 4 retries between 5 attempts
        assert mock_sleep.call_count == 4

    def test_connect_with_lock_retry_does_not_retry_non_lock_errors(self) -> None:
        """A non-lock error propagates immediately, with no retry."""
        from dango.ingestion.dlt_runner import _connect_with_lock_retry

        other_error = Exception("file not found")

        with (
            patch("duckdb.connect", side_effect=other_error) as mock_connect,
            patch("dango.ingestion.dlt_runner.time.sleep") as mock_sleep,
        ):
            with pytest.raises(Exception, match="file not found"):
                _connect_with_lock_retry(Path("/fake/warehouse.duckdb"), "my_source", "op")

        mock_connect.assert_called_once()
        mock_sleep.assert_not_called()
