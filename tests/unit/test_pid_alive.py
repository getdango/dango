"""tests/unit/test_pid_alive.py

pid_alive() and its three call sites never probe with os.kill(pid, 0) (not a probe on Windows).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pytest

from tests.factories.scaffold_project import make_scaffold

pytestmark = pytest.mark.unit


@pytest.fixture
def windows_like_kill(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """os.kill as on Windows: signal 0 is CTRL_C_EVENT, never a probe. Records every call."""
    calls: list[Any] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(os, "kill", fake_kill)
    return calls


def _pid_exists(monkeypatch: pytest.MonkeyPatch, answer: bool) -> list[int]:
    seen: list[int] = []
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: seen.append(pid) or answer)
    return seen


# helper -----------------------------------------------------------------------------------------


def test_helper_true_when_psutil_says_exists(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any]
) -> None:
    from dango.utils.process import pid_alive

    seen = _pid_exists(monkeypatch, True)
    assert pid_alive(4242) is True
    assert seen == [4242]
    assert windows_like_kill == []


def test_helper_false_when_psutil_says_gone(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any]
) -> None:
    from dango.utils.process import pid_alive

    _pid_exists(monkeypatch, False)
    assert pid_alive(4242) is False
    assert windows_like_kill == []


def test_helper_own_pid_alive() -> None:
    from dango.utils.process import pid_alive

    assert pid_alive(os.getpid()) is True


def test_helper_reaped_child_dead() -> None:
    from dango.utils.process import pid_alive

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert pid_alive(child.pid) is False


def test_helper_live_child_alive_then_dead() -> None:
    from dango.utils.process import pid_alive

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()
    assert pid_alive(child.pid) is False


def test_helper_unsignalable_pid_counts_as_alive() -> None:
    """EPERM (another user's process) is alive: psutil reports it as existing."""
    from dango.utils.process import pid_alive

    with patch("psutil.pid_exists", return_value=True):
        assert pid_alive(1) is True


@pytest.mark.parametrize("pid", [-1, 2**80])
def test_helper_invalid_pid_is_dead(pid: int) -> None:
    from dango.utils.process import pid_alive

    assert pid_alive(pid) is False


# call sites -------------------------------------------------------------------------------------


def _write_status(root: Path, pid: int) -> Path:
    from dango.platform.sync_process import get_sync_status_path

    path = get_sync_status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "phase": "data_load"}), encoding="utf-8")
    return path


def test_sync_status_live_pid_kept_on_windows_like_os(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any], tmp_path: Path
) -> None:
    from dango.platform.sync_process import cleanup_sync_status

    path = _write_status(tmp_path, 4242)
    _pid_exists(monkeypatch, True)
    cleanup_sync_status(tmp_path)
    assert path.exists()
    assert windows_like_kill == []


def test_sync_status_dead_pid_removed(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any], tmp_path: Path
) -> None:
    from dango.platform.sync_process import cleanup_sync_status

    path = _write_status(tmp_path, 4242)
    _pid_exists(monkeypatch, False)
    cleanup_sync_status(tmp_path)
    assert not path.exists()
    assert windows_like_kill == []


def _nginx(tmp_path: Path, pid_text: str) -> Any:
    from dango.platform.local.network import NginxManager

    manager = NginxManager(MagicMock())
    manager.nginx_pid = tmp_path / "nginx.pid"
    manager.nginx_pid.write_text(pid_text, encoding="utf-8")
    return manager


def test_nginx_running_on_windows_like_os(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any], tmp_path: Path
) -> None:
    _pid_exists(monkeypatch, True)
    assert _nginx(tmp_path, "4242\n").is_running() is True
    assert windows_like_kill == []


def test_nginx_not_running(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any], tmp_path: Path
) -> None:
    _pid_exists(monkeypatch, False)
    assert _nginx(tmp_path, "4242\n").is_running() is False
    assert windows_like_kill == []


def test_nginx_garbage_pid_file_is_not_running(tmp_path: Path) -> None:
    assert _nginx(tmp_path, "not-a-pid").is_running() is False


def _invoke_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stale_pid: int, alive: bool
) -> MagicMock:
    """Run ``dango start`` up to the stale-pid check; abort at config load. Returns remove_pid_file."""
    from click.testing import CliRunner

    from dango.cli.commands.platform import start

    _pid_exists(monkeypatch, alive)
    remove = MagicMock()
    loader = MagicMock()
    loader.return_value.load_config.side_effect = RuntimeError("stop here")
    with (
        patch("dango.cli.utils.check_v01x_project"),
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.cli.helpers.process_manager.read_pid_file", return_value=stale_pid),
        patch("dango.cli.helpers.process_manager.remove_pid_file", remove),
        patch("dango.config.ConfigLoader", loader),
    ):
        CliRunner().invoke(start, [], obj={})
    return remove


def test_start_stale_pid_removed_when_dead(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any], tmp_path: Path
) -> None:
    remove = _invoke_start(monkeypatch, tmp_path, 4242, alive=False)
    remove.assert_called_once_with(tmp_path)
    assert windows_like_kill == []


def test_start_pid_file_kept_when_alive(
    monkeypatch: pytest.MonkeyPatch, windows_like_kill: list[Any], tmp_path: Path
) -> None:
    remove = _invoke_start(monkeypatch, tmp_path, 4242, alive=True)
    remove.assert_not_called()
    assert windows_like_kill == []


@pytest.fixture(autouse=True)
def _project_scaffold(tmp_path: Path) -> None:
    """The fake project is tmp_path: give it the files `start`/`sync`/`run` look for."""
    make_scaffold(tmp_path)
