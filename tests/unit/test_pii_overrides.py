"""tests/unit/test_pii_overrides.py

Unit tests for PII override CRUD (dango/governance/pii_overrides.py).
YAML-based storage with SQLite migration support.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from dango.governance.pii_overrides import (
    _overrides_path,
    delete_pii_override,
    get_overrides_for_table,
    get_pii_overrides,
    set_pii_override,
)


@pytest.mark.unit
class TestSetPiiOverride:
    """Tests for set_pii_override."""

    def test_set_creates_override_and_yaml(self, tmp_path: Path) -> None:
        set_pii_override(
            tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com", "chess notation"
        )
        # YAML file should exist
        path = _overrides_path(tmp_path)
        assert path.exists()
        data = yaml.safe_load(path.read_text())
        assert len(data["overrides"]) == 1
        assert data["overrides"][0]["source"] == "chess"
        assert data["overrides"][0]["table"] == "games"
        assert data["overrides"][0]["column"] == "pgn"
        assert data["overrides"][0]["status"] == "not_pii"

        overrides = get_overrides_for_table(tmp_path, "chess", "games")
        assert overrides == {"pgn": "not_pii"}

    def test_set_upserts_on_conflict(self, tmp_path: Path) -> None:
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "chess", "games", "pgn", "pii", "admin@test.com")
        overrides = get_overrides_for_table(tmp_path, "chess", "games")
        assert overrides == {"pgn": "pii"}
        # Should still be just one entry
        all_overrides = get_pii_overrides(tmp_path)
        assert len(all_overrides) == 1

    def test_set_rejects_invalid_status(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="pii_status must be"):
            set_pii_override(tmp_path, "chess", "games", "pgn", "maybe", "admin@test.com")


@pytest.mark.unit
class TestGetOverridesForTable:
    """Tests for get_overrides_for_table."""

    def test_empty_when_no_overrides(self, tmp_path: Path) -> None:
        assert get_overrides_for_table(tmp_path, "chess", "games") == {}

    def test_returns_only_matching_table(self, tmp_path: Path) -> None:
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "chess", "players", "name", "pii", "admin@test.com")
        assert get_overrides_for_table(tmp_path, "chess", "games") == {"pgn": "not_pii"}
        assert get_overrides_for_table(tmp_path, "chess", "players") == {"name": "pii"}

    def test_returns_empty_on_error(self, tmp_path: Path) -> None:
        # Point at a non-existent path that will fail
        result = get_overrides_for_table(Path("/nonexistent/path"), "x", "y")
        assert result == {}


@pytest.mark.unit
class TestGetPiiOverrides:
    """Tests for get_pii_overrides."""

    def test_list_all(self, tmp_path: Path) -> None:
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "shopify", "orders", "email", "pii", "admin@test.com")
        result = get_pii_overrides(tmp_path)
        assert len(result) == 2

    def test_filter_by_source(self, tmp_path: Path) -> None:
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "shopify", "orders", "email", "pii", "admin@test.com")
        result = get_pii_overrides(tmp_path, source="chess")
        assert len(result) == 1
        assert result[0]["source"] == "chess"

    def test_returns_all_fields_no_id(self, tmp_path: Path) -> None:
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
        assert "id" not in o

    def test_sorted_by_updated_at_desc(self, tmp_path: Path) -> None:
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        set_pii_override(tmp_path, "shopify", "orders", "email", "pii", "admin@test.com")
        result = get_pii_overrides(tmp_path)
        assert result[0]["updated_at"] >= result[1]["updated_at"]


@pytest.mark.unit
class TestDeletePiiOverride:
    """Tests for delete_pii_override."""

    def test_delete_existing(self, tmp_path: Path) -> None:
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")
        assert delete_pii_override(tmp_path, "chess", "games", "pgn") is True
        assert get_overrides_for_table(tmp_path, "chess", "games") == {}

    def test_delete_nonexistent_returns_false(self, tmp_path: Path) -> None:
        assert delete_pii_override(tmp_path, "chess", "games", "pgn") is False


@pytest.mark.unit
class TestMigrationFromSqlite:
    """Tests for SQLite-to-YAML migration."""

    def test_migrates_sqlite_to_yaml_on_first_read(self, tmp_path: Path) -> None:
        """When YAML doesn't exist but SQLite has data, migration occurs."""
        from dango.utils.dango_db import connect

        with connect(tmp_path) as conn:
            conn.execute(
                "INSERT INTO pii_overrides "
                "(source, table_name, column_name, pii_status, set_by, reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "chess",
                    "games",
                    "pgn",
                    "not_pii",
                    "admin@test.com",
                    "chess notation",
                    "2026-05-06T12:00:00+00:00",
                ),
            )
            conn.commit()

        # YAML should not exist yet
        assert not _overrides_path(tmp_path).exists()

        # Reading triggers migration
        overrides = get_overrides_for_table(tmp_path, "chess", "games")
        assert overrides == {"pgn": "not_pii"}

        # YAML should now exist
        assert _overrides_path(tmp_path).exists()

    def test_no_migration_if_yaml_exists(self, tmp_path: Path) -> None:
        """When YAML already exists, SQLite is not consulted."""
        # Create YAML first
        set_pii_override(tmp_path, "chess", "games", "pgn", "not_pii", "admin@test.com")

        # Even if SQLite has different data, YAML wins
        with patch("dango.governance.pii_overrides._maybe_migrate_from_sqlite") as mock_migrate:
            result = get_overrides_for_table(tmp_path, "chess", "games")
            mock_migrate.assert_not_called()
        assert result == {"pgn": "not_pii"}

    def test_no_migration_if_sqlite_empty(self, tmp_path: Path) -> None:
        """When SQLite has no overrides, no YAML is created."""
        from dango.utils.dango_db import connect

        # Create the SQLite DB with empty table
        with connect(tmp_path):
            pass

        result = get_overrides_for_table(tmp_path, "chess", "games")
        assert result == {}
        assert not _overrides_path(tmp_path).exists()

    def test_migration_failure_returns_empty(self, tmp_path: Path) -> None:
        """When migration fails, returns empty gracefully."""
        with patch(
            "dango.governance.pii_overrides._maybe_migrate_from_sqlite",
            side_effect=Exception("migration boom"),
        ):
            # Should not raise, just return empty
            result = get_overrides_for_table(tmp_path, "chess", "games")
            assert result == {}
