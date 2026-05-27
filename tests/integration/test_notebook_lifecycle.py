"""tests/integration/test_notebook_lifecycle.py

Integration tests for notebook locking lifecycle with real SQLite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.notebooks.locking import (
    acquire_lock,
    copy_locked_notebook,
    force_release_lock,
    get_lock_info,
    is_locked,
    refresh_lock,
    release_lock,
)
from dango.utils.dango_db import _schema_initialized, connect


def _clear_schema_cache() -> None:
    """Clear the dango_db schema initialization cache for test isolation."""
    _schema_initialized.clear()


def _seed_notebook_file(project_root: Path, name: str) -> None:
    """Create a notebook file on disk and register in metadata."""
    nb_dir = project_root / "notebooks"
    nb_dir.mkdir(parents=True, exist_ok=True)
    (nb_dir / f"{name}.py").write_text(f"# {name}\n")

    with connect(project_root) as conn:
        conn.execute(
            "INSERT INTO notebook_metadata (id, name, description, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
            (name, name, f"Test notebook {name}", "test@test.com"),
        )
        conn.commit()


@pytest.mark.integration
class TestNotebookLifecycleIntegration:
    """Integration tests for multi-step notebook locking flows with real SQLite."""

    def test_full_lifecycle(self, tmp_path: Path) -> None:
        """acquire -> refresh -> get_lock_info (has locked_at) -> release -> not locked."""
        _clear_schema_cache()
        _seed_notebook_file(tmp_path, "nb1")

        # Acquire
        assert acquire_lock(tmp_path, "nb1", "alice@test.com") is True
        assert is_locked(tmp_path, "nb1") is True

        # Refresh
        assert refresh_lock(tmp_path, "nb1", "alice@test.com") is True

        # Lock info includes locked_at
        info = get_lock_info(tmp_path, "nb1")
        assert info is not None
        assert info["locked_by"] == "alice@test.com"
        assert info["locked_at"] is not None

        # Release
        assert release_lock(tmp_path, "nb1", "alice@test.com") is True
        assert is_locked(tmp_path, "nb1") is False

    def test_lock_contention(self, tmp_path: Path) -> None:
        """alice acquires -> bob fails -> alice releases -> bob succeeds."""
        _clear_schema_cache()
        _seed_notebook_file(tmp_path, "nb2")

        assert acquire_lock(tmp_path, "nb2", "alice@test.com") is True
        assert acquire_lock(tmp_path, "nb2", "bob@test.com") is False

        assert release_lock(tmp_path, "nb2", "alice@test.com") is True
        assert acquire_lock(tmp_path, "nb2", "bob@test.com") is True

        info = get_lock_info(tmp_path, "nb2")
        assert info is not None
        assert info["locked_by"] == "bob@test.com"

    def test_force_release(self, tmp_path: Path) -> None:
        """alice acquires -> force_release -> not locked -> bob acquires."""
        _clear_schema_cache()
        _seed_notebook_file(tmp_path, "nb3")

        assert acquire_lock(tmp_path, "nb3", "alice@test.com") is True
        assert force_release_lock(tmp_path, "nb3") is True
        assert is_locked(tmp_path, "nb3") is False
        assert acquire_lock(tmp_path, "nb3", "bob@test.com") is True

    def test_expired_lock_allows_acquisition(self, tmp_path: Path) -> None:
        """alice acquires -> manually expire via SQL -> bob acquires."""
        _clear_schema_cache()
        _seed_notebook_file(tmp_path, "nb4")

        assert acquire_lock(tmp_path, "nb4", "alice@test.com") is True

        # Manually expire the lock
        with connect(tmp_path) as conn:
            conn.execute(
                "UPDATE notebook_locks "
                "SET expires_at = datetime('now', '-1 minute') "
                "WHERE notebook_id = 'nb4'"
            )
            conn.commit()

        # Bob can acquire (expired lock gets cleaned)
        assert acquire_lock(tmp_path, "nb4", "bob@test.com") is True

        info = get_lock_info(tmp_path, "nb4")
        assert info is not None
        assert info["locked_by"] == "bob@test.com"

    def test_copy_locked_notebook(self, tmp_path: Path) -> None:
        """alice locks nb -> bob copies -> copy exists with metadata, original lock unaffected."""
        _clear_schema_cache()
        _seed_notebook_file(tmp_path, "nb5")

        assert acquire_lock(tmp_path, "nb5", "alice@test.com") is True

        copy_filename = copy_locked_notebook(tmp_path, "nb5", "bob@test.com")
        assert copy_filename.startswith("nb5_copy_")
        assert copy_filename.endswith(".py")

        # Copy file exists on disk
        copy_path = tmp_path / "notebooks" / copy_filename
        assert copy_path.exists()

        # Copy has metadata
        copy_stem = copy_filename.removesuffix(".py")
        with connect(tmp_path) as conn:
            row = conn.execute(
                "SELECT created_by FROM notebook_metadata WHERE name = ?",
                (copy_stem,),
            ).fetchone()
        assert row is not None
        assert row["created_by"] == "bob@test.com"

        # Original lock unaffected
        info = get_lock_info(tmp_path, "nb5")
        assert info is not None
        assert info["locked_by"] == "alice@test.com"
