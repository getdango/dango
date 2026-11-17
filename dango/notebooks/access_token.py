"""dango/notebooks/access_token.py

Per-start access token for the local Marimo server (file mode 0600, never in argv).
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO
from urllib.parse import urlencode

from dango.utils.private_files import apply_private_mode


def token_file_path(project_root: Path) -> Path:
    """Path of the token file Marimo reads at startup."""
    return project_root / ".dango" / "state" / "marimo.token"


def create_token_file(project_root: Path) -> Path:
    """Write a fresh random token to the token file and return its path.

    The new file is created exclusively (``O_EXCL``, no symlink following,
    mode 0600) next to the target and atomically renamed over it, so a
    planted symlink or an older file is replaced rather than written through.
    """
    path = token_file_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(secrets.token_urlsafe(32))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read_token(project_root: Path) -> str | None:
    """Return the current token, or ``None`` when Marimo runs without one."""
    try:
        token = token_file_path(project_root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def remove_token_file(project_root: Path) -> None:
    """Delete the token file (a stopped Marimo no longer needs it)."""
    token_file_path(project_root).unlink(missing_ok=True)


def marimo_open_url(
    project_root: Path, port: int, notebook_file: str | None = None, *, with_token: bool = True
) -> str:
    """URL to open local Marimo in a browser; carries the token so Marimo sets its cookie.

    ``with_token=False`` yields the bare URL (for users who may not execute notebooks).
    """
    params: dict[str, str] = {}
    if notebook_file:
        params["file"] = notebook_file
    token = read_token(project_root) if with_token else None
    if token:
        params["access_token"] = token
    query = f"?{urlencode(params, safe='/.')}" if params else ""
    return f"http://localhost:{port}/{query}"


def open_private_log(path: Path) -> IO[str]:
    """Open *path* for writing as a fresh 0600 file (truncated, symlinks not followed)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    apply_private_mode(fd, 0o600)  # an existing file keeps its old mode otherwise
    return os.fdopen(fd, "w", encoding="utf-8", newline="\n")


def _try_lock_exclusive(fd: int) -> bool:
    """Try to take an exclusive lock on *fd* without blocking.

    Returns True when the lock is held, False when another holder has it.
    Any other ``OSError`` propagates. The lock is released when *fd* is closed.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release_lock(fd: int) -> None:
    """Unlock *fd* before it is closed. Only Windows needs this (errors are ignored).

    POSIX ``flock`` locks go away with the descriptor; a Windows ``msvcrt`` byte-range
    lock should be released explicitly (mirrors ``DbtLock.release``).
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextmanager
def start_lock(project_root: Path, timeout: float = 90.0) -> Iterator[None]:
    """Serialize Marimo starts (``flock`` on a never-unlinked file).

    A second caller waits up to *timeout* seconds, then proceeds to see the
    running instance (or fails like any other start). On Windows the lock is
    a one-byte ``msvcrt`` lock, unlocked explicitly before the file is closed.
    """
    lock_path = token_file_path(project_root).with_name("marimo.start.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock_exclusive(fd):
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for another Marimo start") from None
            time.sleep(0.1)
        locked = True
        yield
    finally:
        if locked:
            _release_lock(fd)
        os.close(fd)
