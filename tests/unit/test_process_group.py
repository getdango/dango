"""tests/unit/test_process_group.py

dango.utils.process_group: process-group signalling and detach kwargs, on POSIX and with the Unix-only
os/signal attributes removed (as on Windows).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from dango.utils import process_group as pg

pytestmark = pytest.mark.unit


@pytest.fixture
def no_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the Unix-only group/signal attributes, as on Windows."""
    for name in ("killpg", "getpgid", "getpgrp"):
        monkeypatch.delattr(os, name, raising=False)
    monkeypatch.delattr(signal, "SIGKILL", raising=False)


@pytest.fixture
def child_in_own_group() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


class TestPosix:
    def test_group_signal_available(self) -> None:
        assert pg.group_signal_available() is True

    def test_signal_group_passes_the_exact_arguments_to_killpg(self) -> None:
        with patch("os.killpg") as killpg:
            pg.signal_group(123, signal.SIGTERM)
        killpg.assert_called_once_with(123, signal.SIGTERM)

    def test_group_alive_probes_with_signal_zero(self) -> None:
        with patch("os.killpg") as killpg:
            assert pg.group_alive(123) is True
        killpg.assert_called_once_with(123, 0)

    def test_group_alive_false_when_group_is_gone(self) -> None:
        with patch("os.killpg", side_effect=ProcessLookupError):
            assert pg.group_alive(123) is False

    def test_group_alive_true_when_group_exists_but_is_not_ours(self) -> None:
        with patch("os.killpg", side_effect=PermissionError):
            assert pg.group_alive(123) is True

    def test_sigkill_is_sigkill(self) -> None:
        assert pg.sigkill() == signal.SIGKILL

    def test_leads_own_group(self) -> None:
        with (
            patch("os.getpgid", return_value=77),
            patch("os.getpgrp", return_value=1),
        ):
            assert pg.leads_own_group(77) is True

    @pytest.mark.parametrize(("pgid", "own"), [(5, 1), (77, 77)])
    def test_does_not_lead_own_group(self, pgid: int, own: int) -> None:
        with (
            patch("os.getpgid", return_value=pgid),
            patch("os.getpgrp", return_value=own),
        ):
            assert pg.leads_own_group(77) is False

    def test_leads_own_group_propagates_lookup_error(self) -> None:
        with patch("os.getpgid", side_effect=ProcessLookupError):
            with pytest.raises(ProcessLookupError):
                pg.leads_own_group(77)

    def test_real_child_group_is_terminated_and_no_longer_alive(
        self, child_in_own_group: subprocess.Popen[bytes]
    ) -> None:
        proc = child_in_own_group
        assert pg.group_alive(proc.pid) is True
        assert pg.leads_own_group(proc.pid) is True
        pg.signal_group(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
        deadline = time.monotonic() + 10
        while pg.group_alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pg.group_alive(proc.pid) is False


class TestWithoutUnixAttributes:
    def test_group_signal_not_available(self, no_unix: None) -> None:
        assert pg.group_signal_available() is False

    def test_signal_group_refuses(self, no_unix: None) -> None:
        with pytest.raises(NotImplementedError):
            pg.signal_group(123, signal.SIGTERM)

    def test_sigkill_falls_back_to_sigterm(self, no_unix: None) -> None:
        assert pg.sigkill() == signal.SIGTERM

    def test_leads_own_group_is_false(self, no_unix: None) -> None:
        assert pg.leads_own_group(os.getpid()) is False

    @pytest.mark.parametrize("exists", [True, False])
    def test_group_alive_follows_the_leader_process(self, no_unix: None, exists: bool) -> None:
        with patch("psutil.pid_exists", return_value=exists) as pid_exists:
            assert pg.group_alive(4321) is exists
        pid_exists.assert_called_once_with(4321)


class TestDetachKwargs:
    def test_posix_starts_a_new_session(self) -> None:
        assert pg.detach_kwargs() == {"start_new_session": True}

    def test_windows_keeps_the_current_behaviour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert pg.detach_kwargs() == {"start_new_session": True}

    def test_returns_a_fresh_dict(self) -> None:
        first = pg.detach_kwargs()
        first["x"] = 1
        assert pg.detach_kwargs() == {"start_new_session": True}


def test_signal_group_does_not_swallow_lookup_errors() -> None:
    with patch("os.killpg", MagicMock(side_effect=ProcessLookupError)):
        with pytest.raises(ProcessLookupError):
            pg.signal_group(123, signal.SIGTERM)
