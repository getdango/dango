"""tests/unit/test_query_validation.py

Tests for SQL validation in dango/web/routes/query.py.
"""

from __future__ import annotations

import pytest

from dango.web.routes.query import _validate_sql


@pytest.mark.unit
class TestValidateSQL:
    """Tests for _validate_sql()."""

    def test_valid_select_passes(self) -> None:
        """Simple SELECT should not raise."""
        _validate_sql("SELECT 1")

    def test_valid_select_with_from_passes(self) -> None:
        """SELECT with FROM clause should not raise."""
        _validate_sql("SELECT * FROM my_table")

    def test_valid_with_cte_passes(self) -> None:
        """WITH (CTE) query should not raise."""
        _validate_sql("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_drop_table_rejected(self) -> None:
        """DROP TABLE should be rejected."""
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_sql("DROP TABLE users")

    def test_insert_rejected(self) -> None:
        """INSERT should be rejected."""
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_sql("INSERT INTO users VALUES (1, 'test')")

    def test_invalid_sql_syntax_raises(self) -> None:
        """SQL with syntax errors that starts with SELECT should raise syntax error."""
        with pytest.raises(ValueError, match="Invalid SQL syntax"):
            _validate_sql("SELECT * FROM WHERE")

    def test_non_select_typo_rejected(self) -> None:
        """SQL that doesn't start with SELECT/WITH is rejected as non-SELECT."""
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_sql("SELCT * FORM users")

    def test_multiple_statements_rejected(self) -> None:
        """Multiple SQL statements should be rejected."""
        with pytest.raises(ValueError, match="Multiple SQL statements"):
            _validate_sql("SELECT 1; SELECT 2")
