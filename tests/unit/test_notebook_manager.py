"""tests/unit/test_notebook_manager.py

Tests for dango.notebooks.manager — Marimo process lifecycle and idle shutdown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.utils.dango_db import _schema_initialized, connect


@pytest.mark.unit
class TestGetMarimoPidFilePath:
    def test_returns_correct_path(self, tmp_path):
        result = self._call(tmp_path)
        assert result == tmp_path / ".dango" / "marimo.pid"

    def _call(self, project_root):
        from dango.notebooks.manager import get_marimo_pid_file_path

        return get_marimo_pid_file_path(project_root)


@pytest.mark.unit
class TestStartMarimo:
    @patch("dango.notebooks.manager._get_idle_timeout", return_value=7200)
    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    @patch("dango.notebooks.manager.subprocess.Popen")
    def test_start_creates_pid_file(self, mock_popen, mock_running, mock_timeout, tmp_path):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notebooks").mkdir(parents=True, exist_ok=True)

        with patch("dango.notebooks.manager.time.sleep"):
            pid = self._call(tmp_path, port=7805)

        assert pid == 12345
        pid_file = tmp_path / ".dango" / "marimo.pid"
        assert pid_file.exists()
        assert pid_file.read_text() == "12345"

    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    def test_start_raises_if_already_running(self, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999")

        with pytest.raises(RuntimeError, match="already running"):
            self._call(tmp_path, port=7805)

    @patch("dango.notebooks.manager._get_idle_timeout", return_value=7200)
    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    @patch("dango.notebooks.manager.subprocess.Popen")
    def test_start_cleans_stale_pid(self, mock_popen, mock_running, mock_timeout, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999")

        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        with patch("dango.notebooks.manager.time.sleep"):
            pid = self._call(tmp_path, port=7805)

        assert pid == 11111

    @patch("dango.notebooks.manager._get_idle_timeout", return_value=7200)
    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    @patch("dango.notebooks.manager.subprocess.Popen")
    def test_start_raises_if_process_exits_immediately(
        self, mock_popen, mock_running, mock_timeout, tmp_path
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 1  # Exited with error
        mock_popen.return_value = mock_proc

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)

        with patch("dango.notebooks.manager.time.sleep"):
            with pytest.raises(RuntimeError, match="failed to start"):
                self._call(tmp_path, port=7805)

    def _call(self, project_root, port=None):
        from dango.notebooks.manager import start_marimo

        return start_marimo(project_root, port=port)


@pytest.mark.unit
class TestStopMarimo:
    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    @patch("dango.notebooks.manager.kill_process", return_value=True)
    def test_stop_returns_true(self, mock_kill, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("12345")

        from dango.notebooks.manager import stop_marimo

        result = stop_marimo(tmp_path)
        assert result is True
        mock_kill.assert_called_once_with(12345, timeout=10)

    def test_stop_returns_false_no_pid_file(self, tmp_path):
        from dango.notebooks.manager import stop_marimo

        result = stop_marimo(tmp_path)
        assert result is False

    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    @patch("dango.notebooks.manager.kill_process", return_value=False)
    def test_stop_returns_false_if_kill_fails(self, mock_kill, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("12345")

        from dango.notebooks.manager import stop_marimo

        result = stop_marimo(tmp_path)
        assert result is False

    @patch("dango.notebooks.manager.is_process_running", return_value=False)
    def test_stop_cleans_stale_pid(self, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999")

        from dango.notebooks.manager import stop_marimo

        result = stop_marimo(tmp_path)
        assert result is False
        assert not pid_file.exists()


@pytest.mark.unit
class TestGetMarimoStatus:
    def test_status_not_running(self, tmp_path):
        from dango.notebooks.manager import get_marimo_status

        status = get_marimo_status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None

    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    def test_status_running(self, mock_running, tmp_path):
        pid_file = tmp_path / ".dango" / "marimo.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("12345")

        from dango.notebooks.manager import get_marimo_status

        with patch("dango.config.loader.ConfigLoader") as mock_loader:
            mock_config = MagicMock()
            mock_config.platform.marimo_port = 7805
            mock_loader.return_value.load_config.return_value = mock_config

            status = get_marimo_status(tmp_path)

        assert status["running"] is True
        assert status["pid"] == 12345
        assert status["port"] == 7805


def _init_db(tmp_path):
    """Initialize dango.db schema for tests."""
    _schema_initialized.clear()
    with connect(tmp_path):
        pass


def _seed_lock(tmp_path, notebook_id="nb1", user="alice"):
    """Insert a non-expired lock row."""
    with connect(tmp_path) as conn:
        conn.execute(
            "INSERT INTO notebook_locks (notebook_id, locked_by, locked_at, expires_at) "
            "VALUES (?, ?, datetime('now'), datetime('now', '+15 minutes'))",
            (notebook_id, user),
        )
        conn.commit()


@pytest.mark.unit
class TestHasActiveLocks:
    def test_true_when_locks_exist(self, tmp_path):
        _init_db(tmp_path)
        _seed_lock(tmp_path)

        from dango.notebooks.manager import _has_active_locks

        assert _has_active_locks(tmp_path) is True

    def test_false_when_no_locks(self, tmp_path):
        _init_db(tmp_path)

        from dango.notebooks.manager import _has_active_locks

        assert _has_active_locks(tmp_path) is False

    def test_false_when_all_expired(self, tmp_path):
        _init_db(tmp_path)
        with connect(tmp_path) as conn:
            conn.execute(
                "INSERT INTO notebook_locks (notebook_id, locked_by, locked_at, expires_at) "
                "VALUES ('nb1', 'alice', datetime('now'), datetime('now', '-1 minute'))"
            )
            conn.commit()

        from dango.notebooks.manager import _has_active_locks

        assert _has_active_locks(tmp_path) is False


@pytest.mark.unit
class TestReleaseAllLocks:
    def test_clears_all_locks(self, tmp_path):
        _init_db(tmp_path)
        _seed_lock(tmp_path, "nb1", "alice")
        _seed_lock(tmp_path, "nb2", "bob")

        from dango.notebooks.manager import _release_all_locks

        _release_all_locks(tmp_path)

        with connect(tmp_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS cnt FROM notebook_locks").fetchone()["cnt"]
        assert count == 0


@pytest.mark.unit
class TestStartIdleChecker:
    def test_creates_task(self, tmp_path):
        import dango.notebooks.manager as mgr

        _init_db(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            mgr._idle_checker_task = None
            loop.run_until_complete(asyncio.sleep(0))

            # Run start_idle_checker inside the loop
            async def _run():
                mgr.start_idle_checker(tmp_path)
                assert mgr._idle_checker_task is not None
                assert not mgr._idle_checker_task.done()
                mgr._idle_checker_task.cancel()
                try:
                    await mgr._idle_checker_task
                except asyncio.CancelledError:
                    pass

            loop.run_until_complete(_run())
        finally:
            mgr._idle_checker_task = None
            loop.close()


@pytest.mark.unit
class TestStopIdleChecker:
    def test_cancels_task(self):
        import dango.notebooks.manager as mgr

        loop = asyncio.new_event_loop()
        try:

            async def _run():
                task = loop.create_task(asyncio.sleep(999))
                mgr._idle_checker_task = task
                mgr.stop_idle_checker()
                assert mgr._idle_checker_task is None
                # Let the cancellation propagate
                await asyncio.sleep(0)
                assert task.cancelled()

            loop.run_until_complete(_run())
        finally:
            mgr._idle_checker_task = None
            loop.close()

    def test_noop_when_no_task(self):
        import dango.notebooks.manager as mgr

        mgr._idle_checker_task = None
        mgr.stop_idle_checker()
        assert mgr._idle_checker_task is None


@pytest.mark.unit
class TestGetIdleTimeout:
    """Tests for _get_idle_timeout() deployment mode logic."""

    @patch("dango.config.helpers.is_cloud_mode", return_value=False)
    def test_local_mode_returns_7200(self, mock_cloud: MagicMock) -> None:
        from dango.notebooks.manager import _get_idle_timeout

        result = _get_idle_timeout(Path("/fake"))
        assert result == 7200
        mock_cloud.assert_called_once_with(Path("/fake"))

    @patch("dango.config.helpers.is_cloud_mode", return_value=True)
    def test_cloud_mode_returns_3600(self, mock_cloud: MagicMock) -> None:
        from dango.notebooks.manager import _get_idle_timeout

        result = _get_idle_timeout(Path("/fake"))
        assert result == 3600


@pytest.mark.unit
class TestStartMarimoSnapshotPath:
    """Tests for snapshot_path env var in start_marimo()."""

    @patch("dango.notebooks.manager._get_idle_timeout", return_value=7200)
    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    def test_with_snapshot_path_sets_env_var(
        self, mock_running: MagicMock, mock_timeout: MagicMock, tmp_path: Path
    ) -> None:
        from dango.notebooks.manager import start_marimo

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notebooks").mkdir(parents=True, exist_ok=True)

        snapshot = tmp_path / ".dango" / "snapshots" / "warehouse_cli_20260101_120000.duckdb"
        snapshot.parent.mkdir(parents=True)
        snapshot.touch()

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            with patch("dango.notebooks.manager.time.sleep"):
                result = start_marimo(tmp_path, port=7805, snapshot_path=snapshot)

            assert result == 12345
            call_kwargs = mock_popen.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["DANGO_NOTEBOOK_DB_PATH"] == str(snapshot)

    @patch("dango.notebooks.manager._get_idle_timeout", return_value=7200)
    @patch("dango.notebooks.manager.is_process_running", return_value=True)
    def test_without_snapshot_no_env_var(
        self, mock_running: MagicMock, mock_timeout: MagicMock, tmp_path: Path
    ) -> None:
        from dango.notebooks.manager import start_marimo

        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notebooks").mkdir(parents=True, exist_ok=True)

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            with patch("dango.notebooks.manager.time.sleep"):
                start_marimo(tmp_path, port=7805)

            call_kwargs = mock_popen.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert "DANGO_NOTEBOOK_DB_PATH" not in env


@pytest.mark.unit
class TestBroadcastIdleWarning:
    """Tests for _broadcast_idle_warning() WebSocket notification."""

    def test_broadcasts_warning_event(self) -> None:
        from dango.notebooks.manager import _broadcast_idle_warning

        mock_broadcast = AsyncMock()
        mock_ws = MagicMock()
        mock_ws.broadcast = mock_broadcast

        with patch(
            "dango.web.routes.websocket.ws_manager",
            mock_ws,
        ):
            asyncio.run(_broadcast_idle_warning(300))

        mock_broadcast.assert_called_once()
        event = mock_broadcast.call_args[0][0]
        assert event["event"] == "notebook_idle_warning"
        assert event["remaining_seconds"] == 300
        assert "5 minutes" in event["message"]

    def test_warning_shows_at_least_1_minute(self) -> None:
        from dango.notebooks.manager import _broadcast_idle_warning

        mock_broadcast = AsyncMock()
        mock_ws = MagicMock()
        mock_ws.broadcast = mock_broadcast

        with patch(
            "dango.web.routes.websocket.ws_manager",
            mock_ws,
        ):
            asyncio.run(_broadcast_idle_warning(30))

        event = mock_broadcast.call_args[0][0]
        assert "1 minutes" in event["message"]
        assert event["remaining_seconds"] == 30

    def test_swallows_exception_when_no_web_server(self) -> None:
        from dango.notebooks.manager import _broadcast_idle_warning

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock(side_effect=RuntimeError("no web server"))

        with patch(
            "dango.web.routes.websocket.ws_manager",
            mock_ws,
        ):
            # Should not raise
            asyncio.run(_broadcast_idle_warning(300))


@pytest.mark.unit
class TestIdleTimeoutModuleLevel:
    """Verify module-level _IDLE_TIMEOUT is > 900 (smoke test compat)."""

    def test_idle_timeout_above_900(self) -> None:
        from dango.notebooks.manager import _IDLE_TIMEOUT

        assert _IDLE_TIMEOUT > 900
