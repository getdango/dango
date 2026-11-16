"""tests/unit/test_marimo_start_lock_platform.py

start_lock() on POSIX (real fcntl) and on Windows (fake msvcrt, sys.platform patched).
"""

from __future__ import annotations

import errno
import os
import sys
import time
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from dango.notebooks.access_token import start_lock, token_file_path

_TIMEOUT_MESSAGE = "Timed out waiting for another Marimo start"


def _lock_file(root: Path) -> Path:
    return token_file_path(root).with_name("marimo.start.lock")


def _fake_msvcrt(monkeypatch: pytest.MonkeyPatch, locking: Callable[[int, int, int], None]) -> None:
    fake = types.ModuleType("msvcrt")
    fake.__dict__.update(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(sys, "platform", "win32")


def _track_lock_fd(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the fd start_lock opens for the lock file."""
    opened: list[int] = []
    real_open = os.open

    def _open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        if str(path).endswith("marimo.start.lock"):
            opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", _open)
    return opened


def _fd_is_closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


@pytest.mark.unit
def test_start_lock_posix_acquires(tmp_path: Path) -> None:
    import fcntl

    with start_lock(tmp_path, timeout=0.3):
        probe = os.open(_lock_file(tmp_path), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    # Released on exit: a second acquisition succeeds immediately.
    with start_lock(tmp_path, timeout=0.3):
        pass


@pytest.mark.unit
def test_start_lock_posix_times_out(tmp_path: Path) -> None:
    import fcntl

    _lock_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(_lock_file(tmp_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match=_TIMEOUT_MESSAGE):
            with start_lock(tmp_path, timeout=0.3):
                pytest.fail("lock must not be acquired while another fd holds it")
    finally:
        os.close(holder)


@pytest.mark.unit
def test_start_lock_posix_other_oserror_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    def _boom(fd: int, op: int) -> None:
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(fcntl, "flock", _boom)
    opened = _track_lock_fd(monkeypatch)
    with pytest.raises(OSError, match="disk on fire"):
        with start_lock(tmp_path, timeout=0.3):
            pytest.fail("must not acquire")
    assert len(opened) == 1 and _fd_is_closed(opened[0])


_NBLCK, _UNLCK = 2, 0


def _recording_locking(
    calls: list[tuple[int, int, int, bool]], fail_nblck: int = 0, fail_unlck: bool = False
) -> Callable[[int, int, int], None]:
    """Fake msvcrt.locking: records (fd, mode, nbytes, fd_already_closed); contention for the first N NBLCK calls."""
    state = {"nblck": 0}

    def _locking(fd: int, mode: int, nbytes: int) -> None:
        calls.append((fd, mode, nbytes, _fd_is_closed(fd)))
        if mode == _NBLCK:
            state["nblck"] += 1
            if state["nblck"] <= fail_nblck:
                raise OSError(errno.EACCES, "locked")
        elif fail_unlck:
            raise OSError(errno.EACCES, "not locked")

    return _locking


@pytest.mark.unit
def test_start_lock_windows_acquires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, int, bool]] = []
    _fake_msvcrt(monkeypatch, _recording_locking(calls))
    opened = _track_lock_fd(monkeypatch)
    with start_lock(tmp_path, timeout=0.3):
        assert calls == [(opened[0], _NBLCK, 1, False)]  # locked once, not yet unlocked
    # Unlocked exactly once, before the fd is closed.
    assert calls == [(opened[0], _NBLCK, 1, False), (opened[0], _UNLCK, 1, False)]
    assert _fd_is_closed(opened[0])


@pytest.mark.unit
def test_start_lock_windows_retries_then_acquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int, bool]] = []
    sleeps: list[float] = []
    _fake_msvcrt(monkeypatch, _recording_locking(calls, fail_nblck=2))
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with start_lock(tmp_path, timeout=30.0):
        pass
    assert [mode for _fd, mode, _n, _closed in calls] == [_NBLCK, _NBLCK, _NBLCK, _UNLCK]
    assert sleeps == [0.1, 0.1]


@pytest.mark.unit
def test_start_lock_windows_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, int, bool]] = []
    _fake_msvcrt(monkeypatch, _recording_locking(calls, fail_nblck=10**9))
    opened = _track_lock_fd(monkeypatch)
    with pytest.raises(RuntimeError, match=_TIMEOUT_MESSAGE):
        with start_lock(tmp_path, timeout=0.3):
            pytest.fail("must not acquire")
    assert len(opened) == 1 and _fd_is_closed(opened[0])
    # The lock was never acquired, so it is never unlocked.
    assert calls and all(mode == _NBLCK for _fd, mode, _n, _closed in calls)


@pytest.mark.unit
def test_start_lock_windows_unlocks_when_body_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int, bool]] = []
    _fake_msvcrt(monkeypatch, _recording_locking(calls))
    opened = _track_lock_fd(monkeypatch)
    with pytest.raises(ValueError, match="boom"):
        with start_lock(tmp_path, timeout=0.3):
            raise ValueError("boom")
    assert [mode for _fd, mode, _n, _closed in calls] == [_NBLCK, _UNLCK]
    assert _fd_is_closed(opened[0])


@pytest.mark.unit
def test_start_lock_windows_unlock_error_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int, bool]] = []
    _fake_msvcrt(monkeypatch, _recording_locking(calls, fail_unlck=True))
    opened = _track_lock_fd(monkeypatch)
    with start_lock(tmp_path, timeout=0.3):
        pass
    assert [mode for _fd, mode, _n, _closed in calls] == [_NBLCK, _UNLCK]
    assert _fd_is_closed(opened[0])
