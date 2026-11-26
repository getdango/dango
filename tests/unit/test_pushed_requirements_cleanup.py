"""tests/unit/test_pushed_requirements_cleanup.py

The root pip install of pushed requirements runs under a trap that removes exactly the private
``dango-req-*`` directory it was given, however the install ends (exit, failure, HUP/INT/TERM).
"""

from __future__ import annotations

import shlex
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dango.platform.cloud.pushed_requirements import install_pushed_requirements
from tests.factories.compose_helper_sh import Runner, runner_session

pytestmark = pytest.mark.unit

# Runs the issued command (its argv, as sshd's shell would) in its own session and signals it (the whole group, as sshd does when a
# channel closes, or only the shell) once the fake pip has started; prints the exit status.
_HARNESS = textwrap.dedent(
    """
    import os, shlex, signal, subprocess, sys, time
    cmd, sig, scope, ready = sys.argv[1:5]
    p = subprocess.Popen(shlex.split(cmd), start_new_session=True, stdout=subprocess.DEVNULL)
    end = time.monotonic() + 15
    while not os.path.exists(ready) and time.monotonic() < end:
        time.sleep(0.01)
    number = getattr(signal, "SIG" + sig)
    if scope == "group":
        os.killpg(p.pid, number)
    else:
        os.kill(p.pid, number)
    print(p.wait())
    """
)


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


def _install_command(project: Path, workdir: str, pip: str) -> str:
    """The pip command ``install_pushed_requirements`` issues, captured with a fake SSH."""
    (project / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    commands: list[str] = []

    def _exec(command: str, *args: object, **kwargs: object) -> Any:
        commands.append(command)
        out = f"{workdir}\n" if command.startswith("mktemp -d -t dango-req-") else ""
        return SimpleNamespace(stdout=out, stderr="", exit_code=0, success=True)

    def _write(path: str, text: str, mode: int = 0o644) -> None:
        Path(path).write_text(text, encoding="utf-8")

    ssh: Any = SimpleNamespace(exec_command=_exec, write_remote_file=_write)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    assert install_pushed_requirements(ssh, project, pip, []) == ["requirements.txt"]
    return next(c for c in commands if "install" in c and "-r" in c)


def _wrapper_script(command: str) -> str:
    return shlex.split(command)[2]


class Scene:
    """A temp project, a stage root, a fake pip and the command ``install_pushed_requirements`` issues."""

    def __init__(self, tmp_path: Path, *, rc: int = 0, sleep: int = 0) -> None:
        self.stage_root = tmp_path / "stage"
        self.stage_root.mkdir()
        self.workdir = self.stage_root / "dango-req-Ab3dE9fG01"
        self.ready = tmp_path / "ready"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.pip = bin_dir / "pip"
        self.pip.write_text(
            "#!/bin/sh\n"
            'echo "pip-out $*"\n'
            'echo "pip-err" >&2\n'
            f": > {shlex.quote(str(self.ready))}\n"
            f"sleep {sleep}\n"
            f"exit {rc}\n",
            encoding="utf-8",
        )
        self.pip.chmod(0o755)
        project = tmp_path / "project"
        project.mkdir()
        self.command = _install_command(project, str(self.workdir), str(self.pip))


def test_the_pip_install_runs_inside_a_removal_wrapper(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    words = shlex.split(scene.command)
    assert words[:2] == ["sh", "-c"], words
    assert words[3:5] == ["_", str(scene.workdir)], words
    assert words[5:] == [
        str(scene.pip),
        "install",
        "--no-input",
        "-r",
        f"{scene.workdir}/requirements.txt",
    ]
    for name in ("EXIT", "HUP", "INT", "TERM"):
        assert name in words[2], words[2]


def test_normal_exit_keeps_the_output_and_removes_the_directory(
    runner: Runner, tmp_path: Path
) -> None:
    scene = Scene(tmp_path)
    done = runner.run(["sh", "-c", scene.command])
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"pip-out install --no-input -r {scene.workdir}/requirements.txt\n"
    assert done.stderr == "pip-err\n"
    assert not scene.workdir.exists()


def test_a_failing_install_keeps_its_status_and_removes_the_directory(
    runner: Runner, tmp_path: Path
) -> None:
    scene = Scene(tmp_path, rc=3)
    done = runner.run(["sh", "-c", scene.command])
    assert done.returncode == 3, done.stderr
    assert done.stderr == "pip-err\n"
    assert not scene.workdir.exists()


@pytest.mark.parametrize(
    ("sig", "scope", "sleep", "status"),
    [
        ("TERM", "group", 30, 143),
        ("HUP", "group", 30, 129),
        ("INT", "group", 30, 130),
        ("TERM", "shell", 2, 143),
        ("HUP", "shell", 2, 129),
    ],
)
def test_a_signal_during_the_install_still_removes_the_directory(
    runner: Runner, tmp_path: Path, sig: str, scope: str, sleep: int, status: int
) -> None:
    scene = Scene(tmp_path, sleep=sleep)
    done = runner.run(
        ["python3", "-c", _HARNESS, scene.command, sig, scope, str(scene.ready)], timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(status)
    assert not scene.workdir.exists()


def _run_wrapper(runner: Runner, scene: Scene, path: str) -> Any:
    script = _wrapper_script(scene.command)
    return runner.run(["sh", "-c", script, "_", path, "sh", "-c", "exit 7"])


def test_only_the_exact_directory_is_removed_not_siblings(runner: Runner, tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    scene.workdir.mkdir(exist_ok=True)
    (scene.workdir / "requirements.txt").write_text("x\n", encoding="utf-8")
    siblings = [
        scene.stage_root / f"{scene.workdir.name}-other",
        scene.stage_root / f"{scene.workdir.name}X",
        scene.stage_root / "dango-req-OTHER",
    ]
    for sibling in siblings:
        sibling.mkdir()
    done = _run_wrapper(runner, scene, str(scene.workdir))
    assert done.returncode == 7, done.stderr
    assert not scene.workdir.exists()
    assert all(s.is_dir() for s in siblings)


def test_an_empty_path_removes_nothing_and_keeps_the_status(runner: Runner, tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    keep = scene.stage_root / "dango-req-KEEP"
    keep.mkdir()
    done = _run_wrapper(runner, scene, "")
    assert done.returncode == 7, done.stderr
    assert keep.is_dir()


def test_a_directory_without_the_dango_req_prefix_is_never_removed(
    runner: Runner, tmp_path: Path
) -> None:
    scene = Scene(tmp_path)
    other = scene.stage_root / "important"
    other.mkdir()
    done = _run_wrapper(runner, scene, str(other))
    assert done.returncode == 7, done.stderr
    assert other.is_dir()


def test_a_symlink_at_the_path_is_not_followed_or_removed(runner: Runner, tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data.txt").write_text("keep", encoding="utf-8")
    link = scene.stage_root / "dango-req-LINK"
    link.symlink_to(victim)
    done = _run_wrapper(runner, scene, str(link))
    assert done.returncode == 7, done.stderr
    assert (victim / "data.txt").read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()


def test_a_path_that_is_already_gone_is_not_an_error(runner: Runner, tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    done = _run_wrapper(runner, scene, str(scene.stage_root / "dango-req-GONE"))
    assert done.returncode == 7, done.stderr
    assert done.stderr == ""
