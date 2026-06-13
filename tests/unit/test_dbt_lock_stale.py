"""tests/unit/test_dbt_lock_stale.py

Tests for stale dbt lock cleanup (startup helper + DbtLock._cleanup_stale_lock).
"""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.platform.common.startup import cleanup_stale_dbt_lock

# Work around dbt_lock module/function name collision (STANDARDS.md §7)
# The import statement resolves to the function re-exported by __init__.py,
# so we must force-import the module first, then grab it from sys.modules.
importlib.import_module("dango.utils.dbt_lock")
_dbt_lock_mod = sys.modules["dango.utils.dbt_lock"]
DbtLock = _dbt_lock_mod.DbtLock


def _write_stale_lock(project_root: Path, pid: int) -> None:
    """Create stale lock files with the given PID."""
    state_dir = project_root / ".dango" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "dbt.lock").write_text("")
    (state_dir / "dbt.lock.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "source": "cli",
                "operation": "dbt run",
                "started_at": "2026-01-01T00:00:00+00:00",
                "hostname": "test",
            }
        )
    )


@pytest.mark.unit
class TestCleanupStaleDbtLock:
    def test_stale_lock_from_dead_pid_removed(self, tmp_path: Path) -> None:
        _write_stale_lock(tmp_path, pid=99999)
        with patch("psutil.pid_exists", return_value=False):
            result = cleanup_stale_dbt_lock(tmp_path)
        assert result is True
        assert not (tmp_path / ".dango" / "state" / "dbt.lock").exists()
        assert not (tmp_path / ".dango" / "state" / "dbt.lock.json").exists()

    def test_no_lock_files_returns_false(self, tmp_path: Path) -> None:
        result = cleanup_stale_dbt_lock(tmp_path)
        assert result is False

    def test_lock_from_running_pid_preserved(self, tmp_path: Path) -> None:
        _write_stale_lock(tmp_path, pid=12345)
        with patch("psutil.pid_exists", return_value=True):
            result = cleanup_stale_dbt_lock(tmp_path)
        assert result is False
        assert (tmp_path / ".dango" / "state" / "dbt.lock").exists()
        assert (tmp_path / ".dango" / "state" / "dbt.lock.json").exists()

    def test_corrupt_json_returns_false(self, tmp_path: Path) -> None:
        """Corrupt JSON in lock info file should not crash."""
        state_dir = tmp_path / ".dango" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "dbt.lock.json").write_text("{invalid json")
        result = cleanup_stale_dbt_lock(tmp_path)
        assert result is False


@pytest.mark.unit
class TestDbtLockCleanupStaleIntegration:
    def test_acquire_succeeds_after_stale_lock_cleanup(self, tmp_path: Path) -> None:
        """DbtLock.acquire() succeeds when stale lock files exist from a dead process."""
        _write_stale_lock(tmp_path, pid=99999)

        _mod = sys.modules["dango.utils.dbt_lock"]
        with patch.object(_mod.DbtLock, "_is_process_running", return_value=False):
            lock = DbtLock(tmp_path, source="test", operation="test op")
            acquired = lock.acquire(timeout=0)
            assert acquired is True
            lock.release()

    def test_acquire_blocked_by_running_pid(self, tmp_path: Path) -> None:
        """DbtLock.acquire() raises DbtLockError when lock is held by running process."""
        from dango.exceptions import DbtLockError as DbtLockErr

        # Create a real lock first
        holder = DbtLock(tmp_path, source="holder", operation="hold")
        holder.acquire(timeout=0)

        try:
            contender = DbtLock(tmp_path, source="contender", operation="contend")
            with pytest.raises(DbtLockErr, match="Another sync operation"):
                contender.acquire(timeout=0)
        finally:
            holder.release()


@pytest.mark.unit
class TestDbtLockCleanupLogging:
    def test_cleanup_stale_lock_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_cleanup_stale_lock() logs a warning when removing stale files."""
        _write_stale_lock(tmp_path, pid=99999)

        _mod = sys.modules["dango.utils.dbt_lock"]
        import logging

        with (
            caplog.at_level(logging.WARNING, logger="dango.utils.dbt_lock"),
            patch.object(_mod.DbtLock, "_is_process_running", return_value=False),
        ):
            lock = DbtLock(tmp_path, source="test", operation="test op")
            cleaned = lock._cleanup_stale_lock()

        assert cleaned is True
        assert "Removed stale dbt lock" in caplog.text
        assert "99999" in caplog.text
