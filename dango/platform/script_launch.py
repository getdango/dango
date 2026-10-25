"""dango/platform/script_launch.py

Shared launch, timeout and stop helper for user scripts (web runs and scheduled runs).

With the runner marker absent, a script is started exactly as before: the server
interpreter, a copy of ``os.environ`` and no new process group.  With the marker
present it is started through :mod:`dango.platform.run_as` as the restricted
``dango-run`` user with an environment built from nothing plus the names the
script declares in ``.dango/scripts.yml``.  A script that declares
``warehouse: write`` must write the warehouse and the lock under ``.dango/``, so it
runs as the ``dango`` user with only its declared values and is reported as
unisolated.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dango.platform import run_as

__all__ = [
    "RunnerProc",
    "ScriptHandle",
    "ScriptStart",
    "communicate_with_timeout",
    "start_script",
]

_ROLE = "script"
_PIPES: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}


class RunnerProc:
    """A script started through the runner: a ``Popen`` whose leader is ``sudo``.

    Signals must go through the runner (the script runs as another uid), so
    ``stop`` replaces ``terminate``/``kill``.  It blocks for up to its grace
    period: call it from a worker thread, never from the event loop.
    """

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        """Pid of the ``sudo`` leader (also the process group id)."""
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        """Exit code of the leader, or ``None`` while it runs."""
        return self._proc.returncode

    def poll(self) -> int | None:
        """Exit code if the leader has finished (reaping it), else ``None``."""
        return self._proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the leader to exit."""
        return self._proc.wait(timeout)

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        """Collect ``(stdout, stderr)``; raises ``TimeoutExpired`` after *timeout*."""
        out, err = self._proc.communicate(timeout=timeout)
        return out or "", err or ""

    def stop(self, grace: float = 5.0) -> bool:
        """TERM then KILL the whole group; True if processes are still left.

        A leader that already exited is reaped first (``poll``), and a reaper
        thread collects it as soon as the signal lands, so an unreaped ``sudo``
        zombie never keeps the group looking alive for the whole grace period.
        """
        if self._proc.poll() is not None and not _group_present(self._proc.pid):
            return False
        threading.Thread(target=self._proc.wait, daemon=True).start()
        return run_as.stop_group(_ROLE, self._proc.pid, grace=grace)


#: What ``_running_processes`` holds: a plain ``Popen`` (legacy, unisolated) or a runner handle.
ScriptHandle = subprocess.Popen[str] | RunnerProc


@dataclass(frozen=True)
class ScriptStart:
    """A started script and how it was started."""

    proc: ScriptHandle
    #: Runs as the restricted runner user.
    isolated: bool
    #: Isolation is on but this script (``warehouse: write``) runs as ``dango``.
    unisolated: bool = False


def _group_present(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _unisolated_env(
    project_root: Path, declared: dict[str, str], run_id: str | None
) -> dict[str, str]:
    """Environment for a ``warehouse: write`` script: base plus declared, never ``os.environ``.

    ``HOME`` and ``DLT_DATA_DIR`` (the launcher's own, non-secret settings) are
    carried over so the script finds the dango user's home and the dlt state.
    """
    env = run_as.build_env(_ROLE, project_root, declared, run_id=run_id, tz=os.environ.get("TZ"))
    env["HOME"] = os.environ.get("HOME", env["HOME"])
    env.pop("DANGO_WORKSPACE", None)
    if "DLT_DATA_DIR" in os.environ:
        env["DLT_DATA_DIR"] = os.environ["DLT_DATA_DIR"]
    return env


def start_script(
    project_root: Path,
    script_rel: str,
    script_path: Path,
    *,
    run_id: str | None = None,
    log: Callable[[str], None] | None = None,
) -> ScriptStart:
    """Start a script; *script_rel* is its path relative to ``scripts/``.

    Raises:
        IsolationRequiredError: On a cloud server whose isolation is missing (D2).
        OSError: If the process cannot be started (as ``subprocess.Popen`` does).
    """
    state = run_as.require_isolation_ready()
    if state != "on":
        env = os.environ.copy()
        env["DANGO_PROJECT_ROOT"] = str(project_root)
        if run_id is not None:
            env["DANGO_SCRIPT_RUN_ID"] = run_id
        proc = subprocess.Popen(
            [sys.executable, str(script_path)], cwd=str(project_root), env=env, **_PIPES
        )
        return ScriptStart(proc, isolated=False, unisolated=state == "disabled")

    from dango.config.scripts import load_scripts_config

    config = load_scripts_config(project_root)
    names = config.env_for(script_rel)
    legacy_argv = [sys.executable, str(script_path)]
    if config.warehouse_for(script_rel) == "write":
        declared, missing = run_as.resolve_declared(project_root, names)
        _report(log, missing, "warehouse: write script runs as the dango user (unisolated)")
        env = _unisolated_env(project_root, declared, run_id)
        proc = subprocess.Popen(legacy_argv, cwd=str(project_root), env=env, **_PIPES)
        return ScriptStart(proc, isolated=False, unisolated=True)

    launch = run_as.build_launch(
        _ROLE,
        [f"scripts/{script_rel}"],
        project_root=project_root,
        names=names,
        legacy_argv=legacy_argv,
        run_id=run_id,
        tz=os.environ.get("TZ"),
    )
    _report(log, launch.missing)
    proc = subprocess.Popen(
        launch.argv, cwd=str(project_root), env=launch.env, start_new_session=True, **_PIPES
    )
    return ScriptStart(RunnerProc(proc), isolated=True)


def _report(log: Callable[[str], None] | None, missing: tuple[str, ...], note: str = "") -> None:
    if log is None:
        return
    for name in missing:
        log(f"declared but not set: {name}")
    if note:
        log(note)


def communicate_with_timeout(proc: ScriptHandle, timeout: float) -> tuple[str, str, bool]:
    """Collect output within *timeout* seconds; ``(stdout, stderr, timed_out)``.

    On timeout a plain ``Popen`` gets SIGTERM, then SIGKILL after 5 s (as before);
    a runner script is stopped through the runner.  Blocking: call from a thread.
    """
    try:
        out, err = proc.communicate(timeout=timeout)
        return out, err, False
    except subprocess.TimeoutExpired:
        pass
    if isinstance(proc, RunnerProc):
        proc.stop()
        out, err = proc.communicate()
        return out, err, True
    proc.terminate()
    try:
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return out, err, True
