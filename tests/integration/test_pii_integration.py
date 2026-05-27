"""tests/integration/test_pii_integration.py

Integration tests for PII detection using real DuckDB and SQLite.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from dango.governance.pii_detector import (
    get_pii_findings,
    scan_sources_for_pii,
    scan_table_for_pii,
)
from dango.utils.dango_db import _schema_initialized, connect
from dango.utils.post_sync import dispatch_post_sync_hooks

_PII = "dango.governance.pii_detector"


def _create_test_warehouse(tmp_path: Path) -> Path:
    """Create a real DuckDB warehouse with test data including PII-like values.

    Returns:
        Path to the DuckDB file.
    """
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA raw_testshop")
    conn.execute("""
        CREATE TABLE raw_testshop.customers (
            id INTEGER NOT NULL,
            email VARCHAR,
            phone VARCHAR,
            total DOUBLE
        )
    """)
    conn.execute("""
        INSERT INTO raw_testshop.customers VALUES
        (1, 'alice@example.com', '555-123-4567', 10.50),
        (2, 'bob@example.com', '555-987-6543', 25.00)
    """)

    # dlt internal table (should be excluded)
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


def _mock_analyzer_with_findings() -> MagicMock:
    """Create a mock analyzer that returns findings for email-like strings."""
    mock_analyzer = MagicMock()

    def analyze(text, entities, language):
        results = []
        if "@" in text:
            r = MagicMock()
            r.entity_type = "EMAIL_ADDRESS"
            r.score = 0.95
            results.append(r)
        return results

    mock_analyzer.analyze.side_effect = analyze
    return mock_analyzer


@pytest.mark.integration
class TestPiiScanIntegration:
    """Integration tests for PII scanning with real databases."""

    def test_scan_table_creates_findings(self, tmp_path: Path) -> None:
        """Scanning a table with PII-like data produces findings."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)
        mock_analyzer = _mock_analyzer_with_findings()

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            findings = scan_table_for_pii(tmp_path, "testshop", "customers")

        # Should find EMAIL_ADDRESS in the email column
        email_findings = [f for f in findings if f["entity_type"] == "EMAIL_ADDRESS"]
        assert len(email_findings) >= 1
        assert email_findings[0]["column_name"] == "email"

    def test_findings_cached_in_sqlite(self, tmp_path: Path) -> None:
        """Scan results are persisted to the pii_findings SQLite table."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)
        mock_analyzer = _mock_analyzer_with_findings()

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            scan_table_for_pii(tmp_path, "testshop", "customers")

        # Query cached findings
        findings = get_pii_findings(tmp_path)
        assert len(findings) >= 1

    def test_repeat_scan_replaces_old(self, tmp_path: Path) -> None:
        """Second scan replaces findings from the first scan."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)
        mock_analyzer = _mock_analyzer_with_findings()

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            scan_table_for_pii(tmp_path, "testshop", "customers")
            first_count = len(get_pii_findings(tmp_path, source="testshop"))

            # Scan again — should replace, not duplicate
            scan_table_for_pii(tmp_path, "testshop", "customers")
            second_count = len(get_pii_findings(tmp_path, source="testshop"))

        assert second_count == first_count

    def test_non_string_columns_skipped(self, tmp_path: Path) -> None:
        """Non-string columns (INTEGER, DOUBLE) are not scanned."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = []

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            scan_table_for_pii(tmp_path, "testshop", "customers")

        # Analyzer should only be called for string columns (email, phone)
        # not for id (INTEGER) or total (DOUBLE)
        for call in mock_analyzer.analyze.call_args_list:
            text = call.kwargs.get("text") or call.args[0] if call.args else None
            if text:
                # Should never see numeric values
                assert not text.replace(".", "").isdigit()


@pytest.mark.integration
class TestPiiHookBoundaryIntegration:
    """Integration tests for the full hook boundary path."""

    def test_dispatch_to_pii_scan(self, tmp_path: Path) -> None:
        """dispatch_post_sync_hooks → _run_pii_scan → scan_sources_for_pii."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)
        mock_analyzer = _mock_analyzer_with_findings()

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            dispatch_post_sync_hooks(tmp_path, ["testshop"])

        # Verify findings were recorded in SQLite
        with connect(tmp_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pii_findings WHERE source = 'testshop'"
            ).fetchone()[0]

        assert count >= 1


@pytest.mark.integration
class TestPiiFindingsQuery:
    """Integration tests for PII findings queries."""

    def test_query_with_filters(self, tmp_path: Path) -> None:
        """Query findings with source and table filters."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)
        mock_analyzer = _mock_analyzer_with_findings()

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            scan_sources_for_pii(tmp_path, ["testshop"])

        # Query all
        all_findings = get_pii_findings(tmp_path)
        assert len(all_findings) >= 1

        # Query with source filter
        filtered = get_pii_findings(tmp_path, source="testshop")
        assert len(filtered) >= 1

        # Query with table filter
        table_filtered = get_pii_findings(tmp_path, source="testshop", table_name="customers")
        assert len(table_filtered) >= 1

        # Query with limit
        limited = get_pii_findings(tmp_path, limit=1)
        assert len(limited) == 1

        # Newest first
        assert limited[0]["id"] == all_findings[0]["id"]
