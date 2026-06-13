"""tests/unit/test_watcher_lifecycle.py

Tests for kill_orphan_watchers in dango.platform.local.watcher_lifecycle.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from dango.platform.local.watcher_lifecycle import kill_orphan_watchers


def _make_proc(pid: int, cmdline: list[str]) -> MagicMock:
    """Create a mock process for psutil.process_iter."""
    proc = MagicMock()
    proc.pid = pid
    proc.info = {"pid": pid, "cmdline": cmdline}
    return proc


@pytest.mark.unit
class TestKillOrphanWatchers:
    def test_no_matching_processes_returns_zero(self, tmp_path: Path) -> None:
        with patch("psutil.process_iter", return_value=[]):
            assert kill_orphan_watchers(tmp_path) == 0

    def test_matching_process_killed(self, tmp_path: Path) -> None:
        resolved = str(tmp_path.resolve())
        proc = _make_proc(12345, ["python3", "watcher_runner.py", resolved])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch(
                "dango.platform.local.watcher_lifecycle.kill_process", return_value=True
            ) as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 1
        mock_kill.assert_called_once_with(12345)

    def test_different_project_root_not_killed(self, tmp_path: Path) -> None:
        other_root = "/some/other/project"
        proc = _make_proc(12345, ["python3", "watcher_runner.py", other_root])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()

    def test_empty_cmdline_skipped(self, tmp_path: Path) -> None:
        """Process with empty cmdline is skipped gracefully."""
        proc = _make_proc(12345, [])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()

    def test_skips_current_process(self, tmp_path: Path) -> None:
        resolved = str(tmp_path.resolve())
        proc = _make_proc(99999, ["python3", "watcher_runner.py", resolved])
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()

    def test_nosuchprocess_skipped(self, tmp_path: Path) -> None:
        """NoSuchProcess during iteration is handled gracefully."""
        proc = MagicMock()
        proc.pid = 12345
        type(proc).info = property(
            lambda self: (_ for _ in ()).throw(psutil.NoSuchProcess(pid=12345))
        )
        with (
            patch(
                "psutil.process_iter",
                return_value=[proc],
            ),
            patch("dango.platform.local.watcher_lifecycle.kill_process") as mock_kill,
            patch("os.getpid", return_value=99999),
        ):
            result = kill_orphan_watchers(tmp_path)
        assert result == 0
        mock_kill.assert_not_called()
