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
    fake.__dict__.update(LK_NBLCK=2, locking=locking)
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


@pytest.mark.unit
def test_start_lock_windows_acquires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, int]] = []
    _fake_msvcrt(monkeypatch, lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)))
    opened = _track_lock_fd(monkeypatch)
    with start_lock(tmp_path, timeout=0.3):
        assert len(calls) == 1
    assert calls == [(opened[0], 2, 1)]
    assert _fd_is_closed(opened[0])


@pytest.mark.unit
def test_start_lock_windows_retries_then_acquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def _locking(fd: int, mode: int, nbytes: int) -> None:
        attempts.append(fd)
        if len(attempts) <= 2:
            raise OSError(errno.EACCES, "locked")

    sleeps: list[float] = []
    _fake_msvcrt(monkeypatch, _locking)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with start_lock(tmp_path, timeout=30.0):
        pass
    assert len(attempts) == 3
    assert sleeps == [0.1, 0.1]


@pytest.mark.unit
def test_start_lock_windows_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _locking(fd: int, mode: int, nbytes: int) -> None:
        raise OSError(errno.EACCES, "locked")

    _fake_msvcrt(monkeypatch, _locking)
    opened = _track_lock_fd(monkeypatch)
    with pytest.raises(RuntimeError, match=_TIMEOUT_MESSAGE):
        with start_lock(tmp_path, timeout=0.3):
            pytest.fail("must not acquire")
    assert len(opened) == 1 and _fd_is_closed(opened[0])
