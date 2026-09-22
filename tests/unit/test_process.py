"""tests/unit/test_process.py

Tests for dango.utils.process — generic process utilities.
"""

import json
from unittest.mock import MagicMock, patch

import psutil
import pytest

from dango.utils.process import (
    PidRecord,
    get_process_start_time,
    is_process_running,
    kill_process,
    read_pid_record,
    write_pid_record,
)


def _set_psutil_exceptions(mock_psutil):
    """Wire real psutil exception classes onto a mock psutil module."""
    mock_psutil.NoSuchProcess = psutil.NoSuchProcess
    mock_psutil.AccessDenied = psutil.AccessDenied
    mock_psutil.ZombieProcess = psutil.ZombieProcess


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
class TestIsProcessRunningIdentity:
    """1.0.8-OPS-1: identity verification via expected_start_time, mocked."""

    @patch("dango.utils.process.psutil")
    def test_matching_start_time_returns_true(self, mock_psutil):
        mock_psutil.pid_exists.return_value = True
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 1000000.5
        mock_psutil.Process.return_value = mock_proc

        assert is_process_running(1234, expected_start_time=1000000.5) is True

    @patch("dango.utils.process.psutil")
    def test_mismatched_start_time_returns_false(self, mock_psutil):
        """Simulates PID reuse: the PID exists, but belongs to a different process
        than the one originally recorded (different create_time())."""
        mock_psutil.pid_exists.return_value = True
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 2000000.0  # unrelated process's start time
        mock_psutil.Process.return_value = mock_proc

        assert is_process_running(1234, expected_start_time=1000000.5) is False

    @patch("dango.utils.process.psutil")
    def test_pid_gone_returns_false_even_with_expected_start_time(self, mock_psutil):
        mock_psutil.pid_exists.return_value = False
        assert is_process_running(1234, expected_start_time=1000000.5) is False

    @patch("dango.utils.process.psutil")
    def test_none_expected_start_time_skips_identity_check(self, mock_psutil):
        """Old-format PID files (start_time unknown) fall back to existence-only."""
        mock_psutil.pid_exists.return_value = True
        assert is_process_running(1234, expected_start_time=None) is True
        # create_time() must never be consulted when identity is unknown
        mock_psutil.Process.assert_not_called()

    @patch("dango.utils.process.psutil")
    def test_no_such_process_on_create_time_returns_false(self, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_psutil.pid_exists.return_value = True
        mock_psutil.Process.side_effect = psutil.NoSuchProcess(1234)
        assert is_process_running(1234, expected_start_time=1000000.5) is False


@pytest.mark.unit
class TestIsProcessRunningIdentityLive:
    """Live verification (no mocking) using this test process's own real PID —
    covers both the match case (own live process) and the mismatch case
    (simulated PID reuse via a deliberately wrong recorded start_time)."""

    def test_own_process_start_time_matches(self):
        import os

        own_pid = os.getpid()
        real_start_time = psutil.Process(own_pid).create_time()

        assert is_process_running(own_pid, expected_start_time=real_start_time) is True

    def test_wrong_start_time_treated_as_pid_reuse(self):
        import os

        own_pid = os.getpid()
        real_start_time = psutil.Process(own_pid).create_time()
        # A start_time far from the real one simulates the PID having been
        # reassigned by the OS to a different process since it was recorded.
        bogus_start_time = real_start_time - 100000.0

        assert is_process_running(own_pid, expected_start_time=bogus_start_time) is False


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

    def test_mismatched_identity_never_signals(self):
        """1.0.8-OPS-1: kill_process() must not signal a PID whose identity doesn't
        match — the core regression this fix exists to prevent. Live, unmocked: uses
        this test process's own real PID with a deliberately wrong recorded
        start_time (simulated PID reuse). is_process_running()'s real identity check
        must reject it, so kill_process() returns False having never called
        proc.terminate()/proc.kill() on anything.
        """
        import os

        own_pid = os.getpid()
        real_start_time = psutil.Process(own_pid).create_time()
        bogus_start_time = real_start_time - 100000.0

        # Stand in for psutil.Process so create_time() deterministically reports the
        # real start time (as the live process actually would) — the test is about
        # the mismatch against `bogus_start_time`, not about mocking away reality.
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = real_start_time
        with patch(
            "dango.utils.process.psutil.Process", return_value=mock_proc
        ) as mock_process_ctor:
            result = kill_process(own_pid, expected_start_time=bogus_start_time)

        assert result is False
        # Process(pid) is consulted once — purely to compare create_time() for
        # identity — but since it doesn't match, terminate()/kill() must never fire.
        mock_process_ctor.assert_called_once_with(own_pid)
        mock_proc.terminate.assert_not_called()
        mock_proc.kill.assert_not_called()

    @patch("dango.utils.process.psutil")
    def test_matching_identity_signals_normally(self, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_psutil.pid_exists.return_value = True
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 555.0
        mock_proc.children.return_value = []
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.wait_procs.return_value = ([mock_proc], [])

        result = kill_process(42, expected_start_time=555.0)

        assert result is True
        mock_proc.terminate.assert_called_once()


@pytest.mark.unit
class TestGetProcessStartTime:
    @patch("dango.utils.process.psutil")
    def test_returns_create_time_for_live_process(self, mock_psutil):
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 123456.0
        mock_psutil.Process.return_value = mock_proc
        assert get_process_start_time(42) == 123456.0

    @patch("dango.utils.process.psutil")
    def test_no_such_process_returns_none(self, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_psutil.Process.side_effect = psutil.NoSuchProcess(42)
        assert get_process_start_time(42) is None

    @patch("dango.utils.process.psutil")
    def test_access_denied_returns_none(self, mock_psutil):
        _set_psutil_exceptions(mock_psutil)
        mock_psutil.Process.side_effect = psutil.AccessDenied(42)
        assert get_process_start_time(42) is None

    def test_live_own_process(self):
        """No mocking — confirms real psutil integration for the current process."""
        import os

        start_time = get_process_start_time(os.getpid())
        assert start_time is not None
        assert start_time > 0


@pytest.mark.unit
class TestWritePidRecordAndReadPidRecord:
    @patch("dango.utils.process.get_process_start_time", return_value=987.5)
    def test_round_trip(self, _mock_start_time, tmp_path):
        pid_file = tmp_path / "some.pid"
        write_pid_record(pid_file, 4242)

        record = read_pid_record(pid_file)
        assert record == PidRecord(pid=4242, start_time=987.5)

    def test_old_format_bare_integer_handled_gracefully(self, tmp_path):
        """A pre-1.0.8-OPS-1 PID file (bare integer, no JSON) must not crash —
        identity is unknown, not an error."""
        pid_file = tmp_path / "some.pid"
        pid_file.write_text("9876")

        record = read_pid_record(pid_file)
        assert record == PidRecord(pid=9876, start_time=None)

    def test_missing_file_returns_none(self, tmp_path):
        assert read_pid_record(tmp_path / "nope.pid") is None

    def test_garbage_content_returns_none(self, tmp_path):
        pid_file = tmp_path / "some.pid"
        pid_file.write_text("not-json-not-int")
        assert read_pid_record(pid_file) is None

    def test_empty_file_returns_none(self, tmp_path):
        pid_file = tmp_path / "some.pid"
        pid_file.write_text("")
        assert read_pid_record(pid_file) is None

    def test_json_missing_pid_key_returns_none(self, tmp_path):
        pid_file = tmp_path / "some.pid"
        pid_file.write_text(json.dumps({"start_time": 1.0}))
        assert read_pid_record(pid_file) is None

    @patch("dango.utils.process.get_process_start_time", return_value=None)
    def test_write_when_process_uninspectable_records_none(self, _mock_start_time, tmp_path):
        pid_file = tmp_path / "some.pid"
        write_pid_record(pid_file, 111)
        record = read_pid_record(pid_file)
        assert record == PidRecord(pid=111, start_time=None)

    def test_creates_parent_dirs(self, tmp_path):
        pid_file = tmp_path / "nested" / "dir" / "some.pid"
        with patch("dango.utils.process.get_process_start_time", return_value=1.0):
            write_pid_record(pid_file, 1)
        assert pid_file.exists()
