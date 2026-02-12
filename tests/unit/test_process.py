"""tests/unit/test_process.py

Tests for dango.utils.process — generic process utilities.
"""

from unittest.mock import MagicMock, patch

import psutil
import pytest

from dango.utils.process import is_process_running, kill_process


def _set_psutil_exceptions(mock_psutil):
    """Wire real psutil exception classes onto a mock psutil module."""
    mock_psutil.NoSuchProcess = psutil.NoSuchProcess
    mock_psutil.AccessDenied = psutil.AccessDenied


@pytest.mark.unit
class TestIsProcessRunning:
    @patch("dango.utils.process.psutil")
    def test_running_process_returns_true(self, mock_psutil):
        mock_psutil.pid_exists.return_value = True
        assert is_process_running(1234) is True
        mock_psutil.pid_exists.assert_called_once_with(1234)

    @patch("dango.utils.process.psutil")
    def test_non_running_process_returns_false(self, mock_psutil):
        mock_psutil.pid_exists.return_value = False
        assert is_process_running(9999) is False

    @patch("dango.utils.process.psutil")
    def test_no_such_process_returns_false(self, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_psutil.pid_exists.side_effect = psutil.NoSuchProcess(9999)
        assert is_process_running(9999) is False

    @patch("dango.utils.process.psutil")
    def test_access_denied_returns_false(self, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_psutil.pid_exists.side_effect = psutil.AccessDenied(9999)
        assert is_process_running(9999) is False


@pytest.mark.unit
class TestKillProcess:
    @patch("dango.utils.process.is_process_running", return_value=False)
    def test_not_running_returns_false(self, _mock_running):
        assert kill_process(1234) is False

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_graceful_sigterm_no_children(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.wait_procs.return_value = ([mock_proc], [])

        assert kill_process(42) is True
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_graceful_sigterm_with_children(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_proc = MagicMock()
        child1 = MagicMock()
        child2 = MagicMock()
        mock_proc.children.return_value = [child1, child2]
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.wait_procs.return_value = ([mock_proc, child1, child2], [])

        assert kill_process(42) is True
        mock_proc.terminate.assert_called_once()
        child1.terminate.assert_called_once()
        child2.terminate.assert_called_once()

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_sigkill_fallback(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_psutil.Process.return_value = mock_proc
        # First wait: proc still alive; second wait: proc gone
        mock_psutil.wait_procs.side_effect = [
            ([], [mock_proc]),
            ([mock_proc], []),
        ]

        assert kill_process(42) is True
        mock_proc.kill.assert_called()

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_both_sigterm_and_sigkill_fail(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_psutil.Process.return_value = mock_proc
        # Both waits: proc still alive
        mock_psutil.wait_procs.side_effect = [
            ([], [mock_proc]),
            ([], [mock_proc]),
        ]

        assert kill_process(42) is False

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_child_disappears_during_terminate(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_proc = MagicMock()
        child = MagicMock()
        child.terminate.side_effect = psutil.NoSuchProcess(999)
        mock_proc.children.return_value = [child]
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.wait_procs.return_value = ([mock_proc, child], [])

        assert kill_process(42) is True

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_children_raises_no_such_process(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_proc = MagicMock()
        mock_proc.children.side_effect = psutil.NoSuchProcess(42)
        mock_psutil.Process.return_value = mock_proc

        assert kill_process(42) is False

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_process_access_denied(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_psutil.Process.side_effect = psutil.AccessDenied(42)

        assert kill_process(42) is False

    @patch("dango.utils.process.psutil")
    @patch("dango.utils.process.is_process_running", return_value=True)
    def test_custom_timeout_forwarded(self, _mock_running, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.wait_procs.return_value = ([mock_proc], [])

        kill_process(42, timeout=30)
        mock_psutil.wait_procs.assert_called_once_with([mock_proc], timeout=30)
