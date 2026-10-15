"""tests/unit/test_marimo_stop.py

stop_marimo: process-group stop, pid identity checks, and pid file handling.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_M = "dango.notebooks.manager"
_PID = 12345
_STARTED = 1_700_000_000.0


def _stop_with(
    tmp_path: Path,
    *,
    record: str | None = None,
    actual_start: float = _STARTED,
    pgid: int = _PID,
    own_pgrp: int = 1,
    survives: bool = False,
    killpg_error: BaseException | None = None,
) -> dict[str, Any]:
    """Run ``stop_marimo`` with every process call faked (no real pid is signalled)."""
    import psutil

    from dango.notebooks.manager import get_marimo_pid_file_path, stop_marimo

    pid_file = get_marimo_pid_file_path(tmp_path)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(
        record if record is not None else json.dumps({"pid": _PID, "start_time": _STARTED})
    )
    sent: list[int] = []
    state = {"alive": True}

    def _running(pid: int, expected_start_time: float | None = None) -> bool:
        if not state["alive"]:
            return False
        return expected_start_time is None or abs(expected_start_time - actual_start) < 1

    def _killpg(group: int, sig: int) -> None:
        sent.append(sig)
        if killpg_error is not None:
            raise killpg_error
        if not survives:
            state["alive"] = False

    def _process(pid: int) -> MagicMock:
        if not state["alive"]:
            raise psutil.NoSuchProcess(pid)
        live = MagicMock()
        live.status.return_value = "running"
        live.create_time.return_value = actual_start
        return live

    kill = MagicMock(return_value=False)
    with (
        patch(f"{_M}.is_process_running", side_effect=_running),
        patch(f"{_M}.kill_process", kill),
        patch(f"{_M}.os.getpgid", return_value=pgid),
        patch(f"{_M}.os.getpgrp", return_value=own_pgrp),
        patch(f"{_M}.os.killpg", side_effect=_killpg),
        patch("psutil.Process", side_effect=_process),
        patch(f"{_M}._STOP_TERM_TIMEOUT_SECONDS", 0.3),
        patch(f"{_M}._STOP_KILL_TIMEOUT_SECONDS", 0.3),
    ):
        result = stop_marimo(tmp_path)
    return {"result": result, "sent": sent, "kill": kill, "pid_file": pid_file}


@pytest.mark.unit
def test_stop_marimo_keeps_pid_file_when_process_survives(tmp_path: Path) -> None:
    """A Marimo that survives SIGTERM and SIGKILL keeps its pid file; stop reports failure."""
    out = _stop_with(tmp_path, survives=True)

    assert out["result"] is False
    assert out["sent"] == [signal.SIGTERM, signal.SIGKILL]
    assert out["pid_file"].exists()


@pytest.mark.unit
def test_stop_marimo_removes_pid_file_once_group_is_gone(tmp_path: Path) -> None:
    """The process group gets SIGTERM; the pid file goes only after the process is gone."""
    out = _stop_with(tmp_path)

    assert out["result"] is True
    assert out["sent"] == [signal.SIGTERM]
    assert not out["pid_file"].exists()


@pytest.mark.unit
def test_stop_marimo_permission_error_is_not_raised(tmp_path: Path) -> None:
    """A pid owned by someone else (EPERM) is not ours: no exception, stale pid file removed."""
    out = _stop_with(tmp_path, killpg_error=PermissionError(1, "Operation not permitted"))

    assert out["result"] is False
    assert not out["pid_file"].exists()


@pytest.mark.unit
def test_stop_marimo_reused_pid_is_not_signalled(tmp_path: Path) -> None:
    """Same pid, different create_time: a different process. Nothing signalled, file removed."""
    out = _stop_with(tmp_path, actual_start=_STARTED + 500)

    assert out["result"] is False
    assert out["sent"] == []
    out["kill"].assert_not_called()
    assert not out["pid_file"].exists()


@pytest.mark.unit
def test_stop_marimo_never_signals_own_process_group(tmp_path: Path) -> None:
    """A recorded pid that leads our own process group is never group-signalled."""
    out = _stop_with(tmp_path, own_pgrp=_PID)

    assert out["sent"] == []


@pytest.mark.unit
def test_stop_marimo_never_signals_a_non_leader_group(tmp_path: Path) -> None:
    """A pid that is not its own group leader never gets a group signal."""
    out = _stop_with(tmp_path, pgid=_PID + 1)

    assert out["sent"] == []
    out["kill"].assert_called_once()
    assert out["kill"].call_args.args[0] == _PID


@pytest.mark.unit
def test_stop_marimo_old_format_pid_file_never_group_signals(tmp_path: Path) -> None:
    """A bare-integer pid file (identity unknown) uses the plain process kill, never killpg."""
    out = _stop_with(tmp_path, record=str(_PID))

    assert out["sent"] == []
    out["kill"].assert_called_once()
    assert out["kill"].call_args.args[0] == _PID


@pytest.mark.unit
def test_start_marimo_records_process_identity(tmp_path: Path) -> None:
    """The pid file records the start time so a reused pid can be told apart."""
    from dango.notebooks.manager import get_marimo_pid_file_path, start_marimo
    from dango.utils.process import read_pid_record

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    with (
        patch(f"{_M}.subprocess.Popen", return_value=proc),
        patch(f"{_M}._get_idle_timeout", return_value=7200),
        patch(f"{_M}.is_process_running", return_value=False),
        patch(f"{_M}._is_marimo_responding", return_value=True),
        patch(f"{_M}.time.sleep"),
        patch("dango.config.helpers.is_running_on_cloud", return_value=False),
        patch("dango.utils.process.get_process_start_time", return_value=_STARTED),
    ):
        assert start_marimo(tmp_path, port=7805) == 4242

    record = read_pid_record(get_marimo_pid_file_path(tmp_path))
    assert record is not None
    assert (record.pid, record.start_time) == (4242, _STARTED)
