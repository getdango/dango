"""tests/unit/test_pii_overrides.py

Unit tests for PII override CRUD (dango/governance/pii_overrides.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.governance.pii_overrides import (
    delete_pii_override,
    get_overrides_for_table,
    get_pii_overrides,
    set_pii_override,
)
from dango.utils.dango_db import connect


def _init_db(tmp_path: Path) -> None:
    """Ensure schema is created so pii_overrides table exists."""
    with connect(tmp_path):
        pass  # schema auto-created on first connect


@pytest.mark.unit
class TestSetPiiOverride:
    """Tests for set_pii_override."""

    def test_set_creates_override(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        set_pii_override(
            tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com", "chess notation"
        )
        overrides = get_overrides_for_table(tmp_path, "chess", "games")
        assert overrides == {"pgn": "not_pii"}

    def test_set_upserts_on_conflict(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "chess", "games", "pgn", "pii", "admin@test.com")
        overrides = get_overrides_for_table(tmp_path, "chess", "games")
        assert overrides == {"pgn": "pii"}

    def test_set_rejects_invalid_status(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        with pytest.raises(ValueError, match="pii_status must be"):
            set_pii_override(tmp_path, "chess", "games", "pgn", "maybe", "admin@test.com")


@pytest.mark.unit
class TestGetOverridesForTable:
    """Tests for get_overrides_for_table."""

    def test_empty_when_no_overrides(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        assert get_overrides_for_table(tmp_path, "chess", "games") == {}

    def test_returns_only_matching_table(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "chess", "players", "name", "pii", "admin@test.com")
        assert get_overrides_for_table(tmp_path, "chess", "games") == {"pgn": "not_pii"}
        assert get_overrides_for_table(tmp_path, "chess", "players") == {"name": "pii"}

    def test_returns_empty_on_db_error(self, tmp_path: Path) -> None:
        # Point at a non-existent path that will fail
        result = get_overrides_for_table(Path("/nonexistent/path"), "x", "y")
        assert result == {}


@pytest.mark.unit
class TestGetPiiOverrides:
    """Tests for get_pii_overrides."""

    def test_list_all(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "shopify", "orders", "email", "pii", "admin@test.com")
        result = get_pii_overrides(tmp_path)
        assert len(result) == 2

    def test_filter_by_source(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "shopify", "orders", "email", "pii", "admin@test.com")
        result = get_pii_overrides(tmp_path, source="chess")
        assert len(result) == 1
        assert result[0]["source"] == "chess"

    def test_returns_all_fields(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        set_pii_override(
            tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com", "chess notation"
        )
        result = get_pii_overrides(tmp_path)
        assert len(result) == 1
        o = result[0]
        assert o["source"] == "chess"
        assert o["table_name"] == "games"
        assert o["column_name"] == "pgn"
        assert o["pii_status"] == "not_pii"
        assert o["set_by"] == "admin@test.com"
        assert o["reason"] == "chess notation"
        assert o["updated_at"] is not None
        assert o["id"] is not None


@pytest.mark.unit
class TestDeletePiiOverride:
    """Tests for delete_pii_override."""

    def test_delete_existing(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        assert delete_pii_override(tmp_path, "chess", "games", "pgn") is True
        assert get_overrides_for_table(tmp_path, "chess", "games") == {}

    def test_delete_nonexistent_returns_false(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        assert delete_pii_override(tmp_path, "chess", "games", "pgn") is False
