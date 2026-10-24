"""tests/unit/test_marimo_run_as.py

Marimo launch, stop and snapshot through the restricted-user launcher.  Everything
is gated on ``run_as.uses_isolation()``: with no marker the legacy behaviour holds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pytest

from dango.notebooks import manager, marimo_process
from dango.notebooks.manager import get_marimo_pid_file_path, stop_marimo
from dango.utils.process import read_pid_record

_ISO = "dango.platform.run_as.uses_isolation"
_CLOUD = "dango.config.helpers.is_running_on_cloud"
_PID = 12345
_STARTED = 1_700_000_000.0
_SECRET = "s3cret-value-must-not-leak"


class _Proc:
    pid = os.getpid()
    returncode: int | None = None

    def poll(self) -> int | None:
        return None


def _start(
    tmp_path: Path, *, isolated: bool, cloud: bool = True, snapshot_path: Path | None = None
) -> tuple[MagicMock, MagicMock, Any]:
    """Run the start path with Popen faked; return ``(popen, unready_stop, pid)``."""
    (tmp_path / ".env").write_text(f"SECRET={_SECRET}\n")
    with (
        patch(_ISO, return_value=isolated),
        patch(_CLOUD, return_value=cloud),
        patch.dict(os.environ, {"LEAKY": _SECRET}),
        patch(f"{manager.__name__}.subprocess.Popen", return_value=_Proc()) as popen,
        patch(f"{manager.__name__}._is_marimo_responding", return_value=True),
        patch(f"{manager.__name__}.time.sleep"),
        patch(f"{manager.__name__}._stop_unready_process") as unready,
    ):
        pid = manager.start_marimo(tmp_path, port=7805, snapshot_path=snapshot_path)
    return popen, unready, pid


def test_isolated_launch_uses_sudo_and_minimal_env(tmp_path: Path) -> None:
    snap = Path("/srv/dango/runner/snapshots/warehouse_a_1.duckdb")
    popen, _, _ = _start(tmp_path, isolated=True, snapshot_path=snap)
    argv = popen.call_args[0][0]
    env = popen.call_args.kwargs["env"]
    assert argv[:5] == ["/usr/bin/sudo", "-n", "-u", "dango-nb", "-H"]
    assert "dango.runtime.isolated_run" in argv
    assert argv[argv.index("--role") + 1] == "nb"
    assert "--no-token" in argv
    assert env["DANGO_NOTEBOOK_DB_PATH"] == str(snap)
    assert "DANGO_NOTEBOOK_DB_PATH" in next(a for a in argv if a.startswith("--preserve-env="))
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["stderr"] == subprocess.STDOUT


def _leaks(argv: list[str], env: dict[str, str]) -> bool:
    """True when the secret from ``.env`` or the process environment is visible."""
    return _SECRET in "".join(env.values()) or _SECRET in " ".join(argv) or "LEAKY" in env


def test_isolated_env_has_no_env_file_or_process_values(tmp_path: Path) -> None:
    popen, _, _ = _start(tmp_path, isolated=True)
    assert not _leaks(popen.call_args[0][0], popen.call_args.kwargs["env"])
    assert "SECRET" not in popen.call_args.kwargs["env"]


def test_secret_check_detects_a_leak_positive_control(tmp_path: Path) -> None:
    """The leak check fires for the legacy env (it copies ``os.environ``) and for a
    value that came from the ``.env`` file."""
    legacy_popen, _, _ = _start(tmp_path, isolated=False, cloud=True)
    assert _leaks(legacy_popen.call_args[0][0], legacy_popen.call_args.kwargs["env"])
    env_text = (tmp_path / ".env").read_text()
    from_file = dict(line.split("=", 1) for line in env_text.splitlines())
    assert _leaks([], {"SECRET": from_file["SECRET"]})
    assert _leaks(["--flag", _SECRET], {})


def test_isolated_pid_record_marks_runner_and_stays_readable(tmp_path: Path) -> None:
    _, _, pid = _start(tmp_path, isolated=True)
    pid_file = get_marimo_pid_file_path(tmp_path)
    data = json.loads(pid_file.read_text())
    assert data["runner"] == "nb" and data["pid"] == pid
    record = read_pid_record(pid_file)
    assert record is not None and record.pid == pid
    assert marimo_process.is_runner_record(pid_file)


def test_legacy_launch_is_unchanged(tmp_path: Path) -> None:
    snap = tmp_path / "snap.duckdb"
    popen, _, pid = _start(tmp_path, isolated=False, snapshot_path=snap)
    argv = popen.call_args[0][0]
    assert argv == [
        sys.executable,
        "-m",
        "marimo",
        "edit",
        "--headless",
        "--port",
        "7805",
        "--host",
        "127.0.0.1",
        "--timeout",
        str(manager._IDLE_TIMEOUT_CLOUD // 60),
        "--skip-update-check",
        "--no-token",
        "--base-url",
        "/notebooks/marimo",
        str(tmp_path / "notebooks"),
    ]
    expected = {**os.environ, "LEAKY": _SECRET, "DANGO_NOTEBOOK_DB_PATH": str(snap)}
    assert popen.call_args.kwargs["env"] == expected
    assert json.loads(get_marimo_pid_file_path(tmp_path).read_text()).get("runner") is None
    assert not marimo_process.is_runner_record(get_marimo_pid_file_path(tmp_path))


def test_isolated_unready_cleanup_is_flagged_runner_owned(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n")

    class _Dead(_Proc):
        def poll(self) -> int | None:
            return 1

    with (
        patch(_ISO, return_value=True),
        patch(_CLOUD, return_value=True),
        patch(f"{manager.__name__}.subprocess.Popen", return_value=_Dead()),
        patch(f"{manager.__name__}.time.sleep"),
        patch(f"{manager.__name__}._stop_unready_process") as unready,
        pytest.raises(RuntimeError),
    ):
        manager.start_marimo(tmp_path, port=7805)
    assert unready.call_args.kwargs == {"runner_owned": True}


def test_unready_stop_under_isolation_uses_stop_all() -> None:
    proc = MagicMock()
    proc.pid = _PID
    proc.returncode = None
    with (
        patch("dango.platform.run_as.stop_all", return_value=[]) as stop_all,
        patch("dango.notebooks.marimo_process.os.killpg") as killpg,
    ):
        marimo_process._stop_unready_process(proc, runner_owned=True)
    stop_all.assert_called_once_with("nb")
    killpg.assert_not_called()
    proc.terminate.assert_not_called()


# --- stop -------------------------------------------------------------------


def _runner_record(tmp_path: Path, *, runner: bool = True) -> Path:
    pid_file = get_marimo_pid_file_path(tmp_path)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"pid": _PID, "start_time": _STARTED}
    if runner:
        body["runner"] = "nb"
    pid_file.write_text(json.dumps(body))
    token = tmp_path / ".dango" / "state" / "marimo.token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("tok")
    return pid_file


@pytest.fixture
def fake_host() -> Iterator[dict[str, Any]]:
    """The recorded sudo pid is alive and ``killpg`` is denied (a different uid)."""
    state: dict[str, Any] = {"alive": True, "reused": False, "killpg_calls": 0, "order": []}

    def _killpg(*_a: Any) -> None:
        state["killpg_calls"] += 1
        raise PermissionError(1, "Operation not permitted")

    with (
        patch(f"{manager.__name__}.is_process_running", side_effect=lambda *a, **k: state["alive"]),
        patch(
            "dango.notebooks.marimo_process.is_process_running",
            side_effect=lambda *a, **k: state["alive"] and not state["reused"],
            create=True,
        ),
        patch("dango.notebooks.marimo_process.os.killpg", side_effect=_killpg),
        patch("dango.notebooks.marimo_process.os.getpgid", return_value=_PID),
        patch("dango.notebooks.marimo_process.os.getpgrp", return_value=1),
        patch(
            "dango.notebooks.marimo_process._process_gone", side_effect=lambda p: not state["alive"]
        ),
        patch("dango.notebooks.marimo_process.time.sleep"),
    ):
        yield state


def test_eperm_on_runner_record_keeps_pid_file_and_token(
    tmp_path: Path, fake_host: dict[str, Any]
) -> None:
    pid_file = _runner_record(tmp_path)
    with patch("dango.platform.run_as.stop_all", return_value=[777]) as stop_all:
        assert stop_marimo(tmp_path) is False
    assert pid_file.exists() and (tmp_path / ".dango/state/marimo.token").exists()
    assert fake_host["killpg_calls"] == 0
    assert stop_all.called


def test_runner_stop_deletes_files_only_after_stop_all(
    tmp_path: Path, fake_host: dict[str, Any]
) -> None:
    pid_file = _runner_record(tmp_path)
    seen: list[bool] = []

    def _stop_all(role: str, *, pgid: int | None = None, **_k: Any) -> list[int]:
        assert role == "nb" and pgid is None  # no group signalling of the sudo leader
        seen.append(pid_file.exists())
        if pgid is None:
            fake_host["alive"] = False
        return []

    with patch("dango.platform.run_as.stop_all", side_effect=_stop_all):
        assert stop_marimo(tmp_path) is True
    assert seen and all(seen)  # pid file still present while stop_all ran
    assert not pid_file.exists() and not (tmp_path / ".dango/state/marimo.token").exists()


def test_runner_stop_with_survivors_keeps_files_and_warns(
    tmp_path: Path, fake_host: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    pid_file = _runner_record(tmp_path)
    with (
        patch("dango.platform.run_as.stop_all", return_value=[4321]),
        caplog.at_level("WARNING"),
    ):
        assert stop_marimo(tmp_path) is False
    assert pid_file.exists() and (tmp_path / ".dango/state/marimo.token").exists()
    assert any("Marimo" in r.getMessage() for r in caplog.records)


def test_runner_stop_failure_to_run_sudo_keeps_files(
    tmp_path: Path, fake_host: dict[str, Any]
) -> None:
    pid_file = _runner_record(tmp_path)
    with patch("dango.platform.run_as.stop_all", side_effect=FileNotFoundError("sudo")):
        assert stop_marimo(tmp_path) is False
    assert pid_file.exists()


def test_runner_record_with_dead_sudo_still_requires_stop_all(
    tmp_path: Path, fake_host: dict[str, Any]
) -> None:
    pid_file = _runner_record(tmp_path)
    fake_host["alive"] = False
    with patch("dango.platform.run_as.stop_all", return_value=[55]):
        assert stop_marimo(tmp_path) is False
    assert pid_file.exists()


def test_old_record_without_field_still_takes_legacy_path(
    tmp_path: Path, fake_host: dict[str, Any]
) -> None:
    pid_file = _runner_record(tmp_path, runner=False)
    with patch("dango.platform.run_as.stop_all") as stop_all:
        assert stop_marimo(tmp_path) is False  # legacy EPERM: foreign
    stop_all.assert_not_called()
    assert not pid_file.exists()  # unchanged legacy behaviour for non-runner records
    pid_file.write_text("4242")
    assert not marimo_process.is_runner_record(pid_file)


def test_reused_pid_is_treated_as_gone_but_stop_all_still_runs(
    tmp_path: Path, fake_host: dict[str, Any]
) -> None:
    pid_file = _runner_record(tmp_path)
    fake_host["reused"] = True  # the pid now belongs to an unrelated live process
    with patch("dango.platform.run_as.stop_all", return_value=[]) as stop_all:
        assert stop_marimo(tmp_path) is True
    stop_all.assert_called_once_with("nb")
    assert not pid_file.exists() and not (tmp_path / ".dango/state/marimo.token").exists()


def test_reused_pid_still_keeps_files_when_runner_processes_remain(
    tmp_path: Path, fake_host: dict[str, Any]
) -> None:
    pid_file = _runner_record(tmp_path)
    fake_host["reused"] = True
    with patch("dango.platform.run_as.stop_all", return_value=[9]):
        assert stop_marimo(tmp_path) is False
    assert pid_file.exists() and (tmp_path / ".dango/state/marimo.token").exists()


@pytest.fixture
def leader() -> Iterator[subprocess.Popen[bytes]]:
    """A live stand-in for the ``sudo`` leader of its own group.

    ``sudo kill`` is replaced by a plain kill that does not reap, so after the
    stop the leader is a zombie (as a real exited ``sudo`` child would be), and
    the group stays visible until someone reaps it.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )

    def _kill(*_a: Any, **_k: Any) -> bool:
        os.kill(child.pid, 9)
        return True

    with (
        patch("dango.platform.run_as._sudo_kill", side_effect=_kill),
        patch("dango.platform.run_as.runner_pids", return_value=[]),
        patch("dango.platform.run_as._group_alive", side_effect=psutil.pid_exists),
    ):
        yield child
    child.kill()
    child.wait()


def test_stop_with_unreaped_leader_is_fast(tmp_path: Path, leader: subprocess.Popen[bytes]) -> None:
    marimo_process.write_runner_record(get_marimo_pid_file_path(tmp_path), leader.pid)
    started = time.monotonic()
    assert stop_marimo(tmp_path) is True
    assert time.monotonic() - started < 1.0


def test_stop_reaps_the_leader_when_the_popen_is_held(
    tmp_path: Path, leader: subprocess.Popen[bytes]
) -> None:
    marimo_process.write_runner_record(get_marimo_pid_file_path(tmp_path), leader.pid, leader)
    assert stop_marimo(tmp_path) is True
    assert leader.returncode is not None  # reaped: no zombie left behind


def test_runner_record_is_written_in_one_atomic_replace(tmp_path: Path) -> None:
    pid_file = get_marimo_pid_file_path(tmp_path)
    seen: list[bool] = []
    real_replace = os.replace

    def _replace(src: Any, dst: Any) -> None:
        seen.append(Path(dst).exists())  # nothing is visible before the replace
        real_replace(src, dst)

    with patch("dango.notebooks.marimo_process.os.replace", side_effect=_replace) as replace:
        marimo_process.write_runner_record(pid_file, os.getpid())
    replace.assert_called_once()
    assert seen == [False]
    data = json.loads(pid_file.read_text())
    assert data["runner"] == "nb" and data["pid"] == os.getpid()
    assert isinstance(data["start_time"], float)
    assert [p.name for p in pid_file.parent.iterdir()] == [pid_file.name]
