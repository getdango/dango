"""tests/unit/test_remote_launch.py

Executes the script from build_background_launch in a real local shell (sh, bash, dash if present).
Covers a missing directory, a long job, fast success, early failure and a missing binary.
No SSH or remote host is involved; helper processes are stopped with os.kill.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dango.platform.cloud.remote_launch import build_background_launch

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell required"),
]

SHELLS = [s for s in ("sh", "bash", "dash") if shutil.which(s)]


def _run(shell: str, script: str) -> tuple[subprocess.CompletedProcess[str], float]:
    start = time.monotonic()
    proc = subprocess.run(
        [shell, "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    return proc, time.monotonic() - start


@pytest.fixture(params=SHELLS)
def shell(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_missing_directory_fails(shell: str, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    missing = tmp_path / "does not exist"
    proc, _ = _run(shell, build_background_launch(str(missing), f"touch {marker}", 1))
    assert proc.returncode == 2
    assert f"cannot cd to {missing}" in proc.stderr.splitlines()
    assert not marker.exists()


def test_long_job_reports_started_quickly(shell: str, tmp_path: Path) -> None:
    script = build_background_launch(str(tmp_path), "sleep 8", 2)
    pid = None
    try:
        proc, elapsed = _run(shell, script)
        assert proc.returncode == 0
        assert proc.stdout.startswith("started pid=")
        pid = int(proc.stdout.strip().split("pid=")[1])
        # capture_output reads until EOF: returning early proves the pipe was released
        assert elapsed < 6
        os.kill(pid, 0)  # still alive
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_fast_success_reports_started(shell: str, tmp_path: Path) -> None:
    proc, _ = _run(shell, build_background_launch(str(tmp_path), "true", 1))
    assert proc.returncode == 0
    assert "started (finished within 1s)" in proc.stdout


def test_early_failure_reported(shell: str, tmp_path: Path) -> None:
    proc, _ = _run(shell, build_background_launch(str(tmp_path), "sh -c 'exit 7'", 1))
    assert proc.returncode == 3
    assert "command exited early with status 7" in proc.stderr


def test_missing_binary_reported(shell: str, tmp_path: Path) -> None:
    proc, _ = _run(shell, build_background_launch(str(tmp_path), "no_such_binary_xyz_t10", 1))
    assert proc.returncode == 3
    assert "command exited early with status 127" in proc.stderr


def test_command_runs_in_project_dir_and_directory_is_quoted(shell: str, tmp_path: Path) -> None:
    project = tmp_path / "dir with $pace; touch pwned"
    project.mkdir()
    script = build_background_launch(str(project), "sh -c 'pwd > here.txt'", 1)
    proc, _ = _run(shell, script)
    assert proc.returncode == 0
    assert Path((project / "here.txt").read_text().strip()).resolve() == project.resolve()
    assert not (tmp_path / "pwned").exists() and not (project / "pwned").exists()


def test_self_killed_job_does_not_leak_command_text(shell: str, tmp_path: Path) -> None:
    secret = "SECRET_PAYLOAD_xyz"
    cmd = f"sh -c 'echo {secret}; kill -9 $$'"
    proc, _ = _run(shell, build_background_launch(str(tmp_path), cmd, 1))
    assert proc.returncode == 3
    assert "command exited early with status 137" in proc.stderr
    assert secret not in proc.stderr


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_old_command_shape_documents_the_defect(tmp_path: Path) -> None:
    """The pre-fix `cd X && nohup job &` shape: failures invisible, pipe held for the job."""
    bad, _ = _run("bash", "cd /nonexistent_dir_t10 && nohup sleep 1 > /dev/null 2>&1 &")
    assert bad.returncode == 0  # reported as success despite the failed cd
    proc, elapsed = _run("bash", f"cd {tmp_path} && nohup sleep 4 > /dev/null 2>&1 &")
    assert proc.returncode == 0
    assert elapsed >= 3.5  # the whole job held the channel
