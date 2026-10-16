"""tests/unit/test_marimo_readiness.py

Marimo startup readiness: cloud base-url probe, bounded wait, cleanup on timeout.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.utils.process import read_pid_record

_M = "dango.notebooks.manager"
_CLOUD = "dango.config.helpers.is_running_on_cloud"


def _serve(routes: dict[str, tuple[int, bytes]]) -> Iterator[int]:
    """Run a loopback HTTP server answering only *routes*; yield its port."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, body = routes.get(self.path.split("?")[0], (404, b'{"detail":"Not Found"}'))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def cloud_marimo_port() -> Iterator[int]:
    """A server shaped like Marimo started with ``--base-url /notebooks/marimo``."""
    yield from _serve({"/notebooks/marimo/health": (200, b'{"status":"healthy"}')})


@pytest.fixture
def local_marimo_port() -> Iterator[int]:
    """A server shaped like Marimo started without a base url."""
    yield from _serve({"/": (200, b"<html><title>marimo</title></html>")})


class _StubProc:
    """Stand-in for ``subprocess.Popen``: alive until terminated, killed, or released."""

    def __init__(self, *, ignore_terminate: bool = False) -> None:
        self.pid = 4242
        self.released = threading.Event()
        self.terminated = False
        self.killed = False
        self.group_signals: list[int] = []
        self.returncode: int | None = None
        self._ignore_terminate = ignore_terminate

    def _dead(self) -> bool:
        return self.killed or (self.terminated and not self._ignore_terminate)

    def poll(self) -> int | None:
        if self._dead() or self.released.is_set():
            self.returncode = -15
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self._dead():
            self.returncode = -15
            return -15
        raise subprocess.TimeoutExpired(cmd="marimo", timeout=timeout or 0)

    def group_signal(self, pgid: int, sig: int) -> None:
        """Fake ``os.killpg``: record the signal and apply it to this process."""
        assert pgid == self.pid
        self.group_signals.append(sig)
        if sig == signal.SIGTERM:
            self.terminated = True
        elif sig == signal.SIGKILL:
            self.killed = True


def _run_start(
    project_root: Path,
    port: int,
    proc: _StubProc,
    *,
    cloud: bool,
    killpg: Any = None,
) -> dict[str, Any]:
    """Call ``start_marimo`` in a worker thread with a hard join timeout.

    The worker is always released afterwards (the stub process "exits"), so a
    readiness loop that never ends cannot hang or leak beyond the test.
    ``os.getpgid``/``os.killpg`` are always faked: no real process is signalled.
    """
    from dango.notebooks.manager import start_marimo

    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["pid"] = start_marimo(project_root, port=port)
        except Exception as exc:
            outcome["error"] = exc

    with (
        patch(f"{_M}.subprocess.Popen", return_value=proc),
        patch(f"{_M}._get_idle_timeout", return_value=7200),
        patch(f"{_M}.is_process_running", return_value=False),
        patch(_CLOUD, return_value=cloud),
        patch(f"{_M}.os.getpgid", side_effect=lambda pid: pid),
        patch(f"{_M}.os.getpgrp", return_value=1),
        patch(f"{_M}.os.killpg", side_effect=killpg or proc.group_signal),
    ):
        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout=10)
        outcome["finished_in_time"] = not worker.is_alive()
        proc.released.set()
        worker.join(timeout=5)
    return outcome


@pytest.mark.unit
def test_cloud_readiness_probe_uses_base_path(tmp_path: Path, cloud_marimo_port: int) -> None:
    """Cloud Marimo only answers under its base url; startup must still complete."""
    from dango.notebooks.manager import get_marimo_pid_file_path, get_marimo_status

    proc = _StubProc()
    outcome = _run_start(tmp_path, cloud_marimo_port, proc, cloud=True)

    assert outcome["finished_in_time"], "start_marimo never saw the cloud Marimo as ready"
    assert outcome.get("pid") == 4242, outcome
    pid_file = get_marimo_pid_file_path(tmp_path)
    assert read_pid_record(pid_file).pid == 4242
    assert not proc.terminated

    with patch(f"{_M}.is_process_running", return_value=True):
        assert get_marimo_status(tmp_path)["running"] is True


@pytest.mark.unit
def test_local_readiness_probe_unchanged(
    tmp_path: Path, local_marimo_port: int, cloud_marimo_port: int
) -> None:
    """Local mode keeps the root probe with the "marimo" body check."""
    from dango.notebooks.manager import _is_marimo_responding, get_marimo_pid_file_path

    assert _is_marimo_responding(local_marimo_port) is True
    # A base-url-only server is not Marimo for the default (root) probe.
    assert _is_marimo_responding(cloud_marimo_port) is False

    proc = _StubProc()
    outcome = _run_start(tmp_path, local_marimo_port, proc, cloud=False)

    assert outcome["finished_in_time"]
    assert outcome.get("pid") == 4242, outcome
    assert read_pid_record(get_marimo_pid_file_path(tmp_path)).pid == 4242


@pytest.mark.unit
@pytest.mark.parametrize("ignore_terminate", [False, True], ids=["terminate", "kill"])
def test_start_marimo_times_out_and_cleans_up(tmp_path: Path, ignore_terminate: bool) -> None:
    """A Marimo that never becomes ready is stopped, no pid file, RuntimeError, no leaked thread."""
    from dango.notebooks.manager import get_marimo_pid_file_path

    proc = _StubProc(ignore_terminate=ignore_terminate)
    with (
        patch(f"{_M}._MARIMO_START_TIMEOUT_SECONDS", 2),
        patch(f"{_M}._is_marimo_responding", return_value=False),
    ):
        outcome = _run_start(tmp_path, 7805, proc, cloud=True)

    assert outcome["finished_in_time"], "readiness wait did not stop at the deadline"
    error = outcome.get("error")
    assert isinstance(error, RuntimeError), outcome
    assert "did not become ready" in str(error)
    assert "marimo.log" in str(error)
    assert proc.terminated is True
    assert proc.killed is ignore_terminate
    expected = [signal.SIGTERM, signal.SIGKILL] if ignore_terminate else [signal.SIGTERM]
    assert proc.group_signals == expected, "the whole process group must be signalled"
    assert not get_marimo_pid_file_path(tmp_path).exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("message", "logged"),
    [
        ("Marimo did not become ready within 60 s. Check logs at /x/marimo.log", True),
        ("Marimo failed to start. Check logs at /x/marimo.log", True),
        ("Marimo is already running (PID 1).\nStop it with 'dango stop'", False),
    ],
    ids=["timeout-logged", "early-exit-logged", "already-running-silent"],
)
def test_background_start_logs_start_failures(tmp_path: Path, message: str, logged: bool) -> None:
    """The background starter surfaces start failures but stays quiet on the start race."""
    from dango.web.routes import notebooks

    logger = MagicMock()
    with (
        patch.object(notebooks, "create_snapshot", return_value=None),
        patch.object(notebooks, "start_marimo", side_effect=RuntimeError(message)),
        patch.object(notebooks, "logger", logger),
    ):
        asyncio.run(notebooks._start_marimo_background(tmp_path, "a@example.com"))

    assert logger.warning.called is logged


@pytest.mark.unit
def test_start_marimo_timeout_tolerates_vanished_process_group(tmp_path: Path) -> None:
    """A process group that is already gone does not mask the readiness timeout."""
    from dango.notebooks.manager import get_marimo_pid_file_path

    proc = _StubProc()

    def _gone(pgid: int, sig: int) -> None:
        proc.group_signals.append(sig)
        raise ProcessLookupError

    with (
        patch(f"{_M}._MARIMO_START_TIMEOUT_SECONDS", 2),
        patch(f"{_M}._is_marimo_responding", return_value=False),
    ):
        outcome = _run_start(tmp_path, 7805, proc, cloud=True, killpg=_gone)

    assert outcome["finished_in_time"]
    assert "did not become ready" in str(outcome.get("error")), outcome
    assert signal.SIGTERM in proc.group_signals
    assert not get_marimo_pid_file_path(tmp_path).exists()


@pytest.mark.unit
def test_start_marimo_stops_process_when_wait_loop_raises(tmp_path: Path) -> None:
    """Any exception escaping the readiness wait stops the spawned process group."""
    from dango.notebooks.manager import get_marimo_pid_file_path

    proc = _StubProc()
    with patch(f"{_M}._is_marimo_responding", side_effect=OSError("probe blew up")):
        outcome = _run_start(tmp_path, 7805, proc, cloud=True)

    assert outcome["finished_in_time"]
    assert isinstance(outcome.get("error"), RuntimeError), outcome
    assert proc.group_signals[:1] == [signal.SIGTERM]
    assert proc.terminated is True
    assert not get_marimo_pid_file_path(tmp_path).exists()


@pytest.mark.unit
def test_start_marimo_early_exit_sends_no_group_signal(tmp_path: Path) -> None:
    """A Marimo that already exited (reaped) is not signalled: its pgid may be reused."""
    proc = _StubProc()
    proc.released.set()  # exits before the first readiness check
    outcome = _run_start(tmp_path, 7805, proc, cloud=True)

    assert "failed to start" in str(outcome.get("error")), outcome
    assert proc.group_signals == []
