"""tests/unit/test_notebook_locking.py

Tests for dango.notebooks.locking — file-level notebook locking via SQLite.
"""

from __future__ import annotations

import pytest

from dango.utils.dango_db import connect


@pytest.mark.unit
class TestAcquireLock:
    def test_acquire_succeeds(self, tmp_path):
        from dango.notebooks.locking import acquire_lock

        result = acquire_lock(tmp_path, "nb1", "alice")
        assert result is True

    def test_acquire_fails_if_locked_by_other(self, tmp_path):
        from dango.notebooks.locking import acquire_lock

        acquire_lock(tmp_path, "nb1", "alice")
        result = acquire_lock(tmp_path, "nb1", "bob")
        assert result is False

    def test_acquire_refreshes_own_lock(self, tmp_path):
        from dango.notebooks.locking import acquire_lock

        acquire_lock(tmp_path, "nb1", "alice")
        result = acquire_lock(tmp_path, "nb1", "alice")
        assert result is True

    def test_acquire_succeeds_after_expiry(self, tmp_path):
        from dango.notebooks.locking import acquire_lock

        acquire_lock(tmp_path, "nb1", "alice")

        # Manually expire the lock
        with connect(tmp_path) as conn:
            conn.execute(
                "UPDATE notebook_locks SET expires_at = datetime('now', '-1 minute') "
                "WHERE notebook_id = 'nb1'"
            )
            conn.commit()

        result = acquire_lock(tmp_path, "nb1", "bob")
        assert result is True


@pytest.mark.unit
class TestReleaseLock:
    def test_release_own_lock(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, release_lock

        acquire_lock(tmp_path, "nb1", "alice")
        result = release_lock(tmp_path, "nb1", "alice")
        assert result is True

    def test_release_fails_for_other_user(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, release_lock

        acquire_lock(tmp_path, "nb1", "alice")
        result = release_lock(tmp_path, "nb1", "bob")
        assert result is False

    def test_release_nonexistent(self, tmp_path):
        from dango.notebooks.locking import release_lock

        result = release_lock(tmp_path, "nb1", "alice")
        assert result is False


@pytest.mark.unit
class TestRefreshLock:
    def test_refresh_own_lock(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, refresh_lock

        acquire_lock(tmp_path, "nb1", "alice")
        result = refresh_lock(tmp_path, "nb1", "alice")
        assert result is True

    def test_refresh_fails_for_other(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, refresh_lock

        acquire_lock(tmp_path, "nb1", "alice")
        result = refresh_lock(tmp_path, "nb1", "bob")
        assert result is False


@pytest.mark.unit
class TestForceReleaseLock:
    def test_force_release(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, force_release_lock, is_locked

        acquire_lock(tmp_path, "nb1", "alice")
        result = force_release_lock(tmp_path, "nb1")
        assert result is True
        assert is_locked(tmp_path, "nb1") is False

    def test_force_release_nonexistent(self, tmp_path):
        from dango.notebooks.locking import force_release_lock

        result = force_release_lock(tmp_path, "nb1")
        assert result is False


@pytest.mark.unit
class TestIsLocked:
    def test_not_locked(self, tmp_path):
        from dango.notebooks.locking import is_locked

        assert is_locked(tmp_path, "nb1") is False

    def test_locked(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, is_locked

        acquire_lock(tmp_path, "nb1", "alice")
        assert is_locked(tmp_path, "nb1") is True


@pytest.mark.unit
class TestGetLockInfo:
    def test_returns_info(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, get_lock_info

        acquire_lock(tmp_path, "nb1", "alice")
        info = get_lock_info(tmp_path, "nb1")
        assert info is not None
        assert info["locked_by"] == "alice"
        assert "locked_at" in info
        assert "expires_at" in info

    def test_returns_none_when_unlocked(self, tmp_path):
        from dango.notebooks.locking import get_lock_info

        assert get_lock_info(tmp_path, "nb1") is None

    def test_timestamps_are_iso_utc(self, tmp_path):
        """Timestamps use ISO 8601 format with Z suffix for UTC."""
        from dango.notebooks.locking import acquire_lock, get_lock_info

        acquire_lock(tmp_path, "nb1", "alice")
        info = get_lock_info(tmp_path, "nb1")
        assert info is not None
        assert "T" in info["locked_at"]
        assert info["locked_at"].endswith("Z")
        assert "T" in info["expires_at"]
        assert info["expires_at"].endswith("Z")


@pytest.mark.unit
class TestCopyLockedNotebook:
    def test_copies_file_and_registers(self, tmp_path):
        notebooks_dir = tmp_path / "notebooks"
        notebooks_dir.mkdir(parents=True)
        (notebooks_dir / "nb1.py").write_text("# original")

        from dango.notebooks.locking import copy_locked_notebook

        result = copy_locked_notebook(tmp_path, "nb1", "bob")
        assert result.startswith("nb1_copy_")
        assert result.endswith(".py")
        assert (notebooks_dir / result).exists()
        assert (notebooks_dir / result).read_text() == "# original"

        # Check metadata was registered
        with connect(tmp_path) as conn:
            row = conn.execute(
                "SELECT * FROM notebook_metadata WHERE name LIKE 'nb1_copy_%'"
            ).fetchone()
            assert row is not None
            assert row["created_by"] == "bob"

    def test_raises_if_file_missing(self, tmp_path):
        (tmp_path / "notebooks").mkdir(parents=True)

        from dango.notebooks.locking import copy_locked_notebook

        with pytest.raises(FileNotFoundError):
            copy_locked_notebook(tmp_path, "nonexistent", "bob")


@pytest.mark.unit
class TestExpireStaleLocks:
    def test_expires_stale_lock(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, expire_stale_locks, is_locked

        acquire_lock(tmp_path, "nb1", "alice")

        # Set heartbeat to 3 minutes ago
        with connect(tmp_path) as conn:
            conn.execute(
                "UPDATE notebook_locks SET last_heartbeat_at = datetime('now', '-3 minutes') "
                "WHERE notebook_id = 'nb1'"
            )
            conn.commit()

        expired = expire_stale_locks(tmp_path, timeout_seconds=120)
        assert expired == 1
        assert is_locked(tmp_path, "nb1") is False

    def test_active_heartbeat_preserved(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, expire_stale_locks, is_locked

        acquire_lock(tmp_path, "nb1", "alice")

        expired = expire_stale_locks(tmp_path, timeout_seconds=120)
        assert expired == 0
        assert is_locked(tmp_path, "nb1") is True

    def test_null_heartbeat_preserved(self, tmp_path):
        from dango.notebooks.locking import expire_stale_locks, is_locked

        # Insert a lock without last_heartbeat_at (simulates pre-migration lock)
        with connect(tmp_path) as conn:
            conn.execute(
                "INSERT INTO notebook_locks (notebook_id, locked_by, locked_at, expires_at) "
                "VALUES ('nb1', 'alice', datetime('now'), datetime('now', '+15 minutes'))"
            )
            conn.commit()

        expired = expire_stale_locks(tmp_path, timeout_seconds=1)
        assert expired == 0
        assert is_locked(tmp_path, "nb1") is True

    def test_returns_count(self, tmp_path):
        from dango.notebooks.locking import acquire_lock, expire_stale_locks

        acquire_lock(tmp_path, "nb1", "alice")
        acquire_lock(tmp_path, "nb2", "bob")

        # Set both heartbeats to 3 minutes ago
        with connect(tmp_path) as conn:
            conn.execute(
                "UPDATE notebook_locks SET last_heartbeat_at = datetime('now', '-3 minutes')"
            )
            conn.commit()

        expired = expire_stale_locks(tmp_path, timeout_seconds=120)
        assert expired == 2
