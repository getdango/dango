"""tests/unit/test_process_group_sites.py

Call sites of the process-group helpers: simulated Windows behaviour and unchanged POSIX signals.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest

pytestmark = pytest.mark.unit

_M = "dango.notebooks.marimo_process"


@pytest.fixture
def no_unix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the Unix-only group/signal attributes and the pwd module, as on Windows."""
    for name in ("killpg", "getpgid", "getpgrp"):
        monkeypatch.delattr(os, name, raising=False)
    for name in ("SIGKILL", "SIGALRM", "alarm"):
        monkeypatch.delattr(signal, name, raising=False)
    monkeypatch.setitem(sys.modules, "pwd", None)


def _proc(pid: int = 4242, returncode: int | None = None) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = pid
    proc.returncode = returncode
    return proc


# marimo_process ---------------------------------------------------------------------------------


def test_stop_marimo_process_without_group_support_uses_the_tree_kill(no_unix: None) -> None:
    from dango.notebooks.marimo_process import _stop_marimo_process

    kill = MagicMock(return_value=True)
    with patch(f"{_M}.kill_process", kill), patch(f"{_M}._wait_gone", return_value=True):
        assert _stop_marimo_process(4242, 12.5) == "stopped"
    assert kill.call_args.args == (4242,)
    assert kill.call_args.kwargs["expected_start_time"] == 12.5


def test_stop_unready_process_without_group_support_uses_the_tree_kill(no_unix: None) -> None:
    from dango.notebooks.marimo_process import _stop_unready_process

    proc = _proc()
    kill = MagicMock(return_value=True)
    with patch(f"{_M}.kill_process", kill):
        _stop_unready_process(proc)
    assert kill.call_args.args == (4242,)
    proc.wait.assert_called()


def test_stop_unready_process_already_reaped_is_not_signalled(no_unix: None) -> None:
    from dango.notebooks.marimo_process import _stop_unready_process

    kill = MagicMock()
    with patch(f"{_M}.kill_process", kill):
        _stop_unready_process(_proc(returncode=0))
    kill.assert_not_called()


def test_stop_unready_process_posix_sends_term_then_kill_to_the_group() -> None:
    from dango.notebooks.marimo_process import _stop_unready_process

    proc = _proc()
    proc.wait.side_effect = [subprocess.TimeoutExpired("marimo", 5), 0]
    with patch("os.killpg") as killpg:
        _stop_unready_process(proc)
    assert [c.args for c in killpg.call_args_list] == [
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
    ]


def test_stop_marimo_process_posix_sends_term_then_kill_to_the_group() -> None:
    from dango.notebooks.marimo_process import _stop_marimo_process

    with (
        patch("os.getpgid", return_value=4242),
        patch("os.getpgrp", return_value=1),
        patch("os.killpg") as killpg,
        patch(f"{_M}._wait_gone", side_effect=[False, True]),
    ):
        assert _stop_marimo_process(4242, 12.5) == "stopped"
    assert [c.args for c in killpg.call_args_list] == [
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
    ]


# script_launch and run_as -----------------------------------------------------------------------


@pytest.mark.parametrize("exists", [True, False])
def test_script_group_present_without_killpg(no_unix: None, exists: bool) -> None:
    from dango.platform.script_launch import _group_present

    with patch("psutil.pid_exists", return_value=exists):
        assert _group_present(4321) is exists


def test_script_group_present_posix_probes_with_signal_zero() -> None:
    from dango.platform.script_launch import _group_present

    with patch("os.killpg", side_effect=PermissionError) as killpg:
        assert _group_present(4321) is True
    killpg.assert_called_once_with(4321, 0)


@pytest.mark.parametrize("exists", [True, False])
def test_run_as_group_alive_without_killpg(no_unix: None, exists: bool) -> None:
    from dango.platform import run_as

    with patch("psutil.pid_exists", return_value=exists):
        assert run_as._group_alive(4321) is exists


def test_run_as_group_alive_posix_probes_with_signal_zero() -> None:
    from dango.platform import run_as

    with patch("os.killpg", side_effect=ProcessLookupError) as killpg:
        assert run_as._group_alive(4321) is False
    killpg.assert_called_once_with(4321, 0)


def test_run_as_runner_user_lookup_without_pwd_is_an_absent_user(no_unix: None) -> None:
    from dango.platform import run_as

    with pytest.raises(KeyError):
        run_as.runner_uid("script")
    assert run_as.runner_pids("script") == []


def test_windows_takes_the_legacy_path_without_group_or_pwd_calls(
    no_unix: None, tmp_path: Path
) -> None:
    from dango.platform import run_as

    with patch("dango.config.helpers.is_running_on_cloud", return_value=False):
        assert run_as.uses_isolation() is False
        assert run_as.require_isolation_ready() == "local"
        launch = run_as.build_launch(
            "script", ["a.py"], project_root=tmp_path, legacy_argv=["python", "a.py"]
        )
    assert launch.isolated is False
    assert launch.argv == ["python", "a.py"]


# dlt_runner and web helpers ---------------------------------------------------------------------


def _runner_timeout(func: Any, minutes: int) -> Any:
    from dango.ingestion.dlt_runner import DltPipelineRunner

    return DltPipelineRunner._run_with_timeout(MagicMock(), func, minutes)


def test_sync_timeout_without_alarm_runs_the_function_unbounded(no_unix: None) -> None:
    assert _runner_timeout(lambda: "ok", 1) == "ok"


def test_sync_timeout_posix_arms_and_cancels_the_alarm() -> None:
    with patch("signal.alarm") as alarm:
        assert _runner_timeout(lambda: "ok", 2) == "ok"
    assert [c.args for c in alarm.call_args_list] == [(120,), (0,), (0,)]


def _make_db(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE TABLE raw.src AS SELECT range AS i FROM range(3)")
    con.close()
    return path


def test_source_row_count_without_alarm(no_unix: None, tmp_path: Path) -> None:
    from dango.web.helpers import get_source_row_count

    with patch("dango.web.helpers.get_duckdb_path", return_value=_make_db(tmp_path)):
        assert get_source_row_count("src") == 3


def test_source_row_count_posix_arms_a_two_second_alarm(tmp_path: Path) -> None:
    from dango.web.helpers import get_source_row_count

    with (
        patch("dango.web.helpers.get_duckdb_path", return_value=_make_db(tmp_path)),
        patch("signal.alarm") as alarm,
    ):
        assert get_source_row_count("src") == 3
    assert [c.args for c in alarm.call_args_list] == [(2,), (0,)]


# detach -----------------------------------------------------------------------------------------

_SENTINEL = {"creationflags": 0x208}
_HELPER = "dango.utils.process_group.detach_kwargs"


def _started_popen() -> MagicMock:
    popen = MagicMock()
    popen.return_value.pid = 99
    popen.return_value.poll.return_value = None
    return popen


def _assert_sentinel(popen: MagicMock) -> None:
    kwargs = popen.call_args.kwargs
    assert kwargs["creationflags"] == 0x208
    assert "start_new_session" not in kwargs


def test_web_server_detach_kwargs_come_from_the_helper(tmp_path: Path) -> None:
    from dango.cli.helpers.process_manager import start_fastapi_server

    (tmp_path / ".dango").mkdir()
    popen = _started_popen()
    with (
        patch("dango.cli.helpers.process_manager.subprocess.Popen", popen),
        patch("dango.cli.helpers.process_manager.time.sleep"),
        patch("dango.cli.helpers.process_manager.check_port_in_use", return_value=False),
        patch("dango.cli.helpers.process_manager.is_process_running", return_value=False),
        patch(_HELPER, return_value=_SENTINEL),
    ):
        start_fastapi_server(tmp_path)
    _assert_sentinel(popen)


def test_watcher_detach_kwargs_come_from_the_helper(tmp_path: Path) -> None:
    from dango.platform.local.watcher_lifecycle import start_file_watcher

    (tmp_path / ".dango").mkdir()
    popen = _started_popen()
    with (
        patch("dango.platform.local.watcher_lifecycle.subprocess.Popen", popen),
        patch("dango.platform.local.watcher_lifecycle.time.sleep"),
        patch("dango.platform.local.watcher_lifecycle.is_process_running", return_value=False),
        patch(_HELPER, return_value=_SENTINEL),
    ):
        start_file_watcher(tmp_path)
    _assert_sentinel(popen)


def test_marimo_detach_kwargs_come_from_the_helper(tmp_path: Path) -> None:
    from dango.notebooks import manager

    popen = _started_popen()
    popen.return_value.pid = os.getpid()
    with (
        patch(f"{manager.__name__}.subprocess.Popen", popen),
        patch(f"{manager.__name__}._is_marimo_responding", return_value=True),
        patch(f"{manager.__name__}.time.sleep"),
        patch("dango.platform.run_as.uses_isolation", return_value=False),
        patch("dango.platform.run_as.isolation_state", return_value="local"),
        patch("dango.config.helpers.is_running_on_cloud", return_value=False),
        patch(_HELPER, return_value=_SENTINEL),
    ):
        manager.start_marimo(tmp_path, port=7805)
    _assert_sentinel(popen)


def test_runner_script_detach_kwargs_come_from_the_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.platform import run_as, script_launch

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "scripts.yml").write_text(
        "scripts:\n  - path: hello.py\n", encoding="utf-8"
    )
    monkeypatch.setattr(run_as, "uses_isolation", lambda: True)
    popen = _started_popen()
    with patch("subprocess.Popen", popen), patch(_HELPER, return_value=_SENTINEL):
        script_launch.start_script(tmp_path, "hello.py", scripts / "hello.py")
    _assert_sentinel(popen)
