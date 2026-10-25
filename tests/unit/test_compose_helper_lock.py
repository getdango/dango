"""tests/unit/test_compose_helper_lock.py

The compose helper's lock directory rules, bounded waits and per-verb time budgets.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud.compose_helper import LOCKED_VERBS, VERB_BUDGET
from tests.factories.compose_helper_sh import Runner, Tree, make_tree, runner_session

pytestmark = pytest.mark.unit

VALID_ARGS: dict[str, list[str]] = {
    "restart": ["metabase"],
    "logs": ["metabase"],
    "reset-password": ["a@b.c"],
    "render": ["3000", "8081", "v0.59.1", "n"],
    "pin-project": ["abcd1234"],
    "pin-metabase": ["v0.99.1", "ab" * 32],
    "down-volumes": ["metabase"],
}


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def prepare_lock(tree: Tree) -> Path:
    tree.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tree.lock_dir.chmod(0o700)
    lock = tree.lock_dir / "lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    return lock


def hold(runner: Runner, lock: Path, seconds: int = 30) -> subprocess.Popen[str]:
    """Hold the flock on *lock* in the helper's environment; returns once it is held."""
    proc = runner.popen(
        ["sh", "-c", f'exec 9>>"$1"; flock 9; echo held; sleep {seconds}', "_", str(lock)]
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def test_first_locked_call_creates_dir_0700_and_file_0600(runner: Runner, tmp_path: Path) -> None:
    tree = make_tree(tmp_path, runner)
    assert tree.call("up", sudo=True).returncode == 0
    assert mode(tree.lock_dir) == 0o700 and mode(tree.lock_dir / "lock") == 0o600


@pytest.mark.parametrize("breakage", ["dir_0755", "dir_symlink", "file_0644", "file_symlink"])
def test_unsafe_lock_paths_are_refused(runner: Runner, tmp_path: Path, breakage: str) -> None:
    tree = make_tree(tmp_path, runner)
    if breakage == "dir_0755":
        prepare_lock(tree)
        tree.lock_dir.chmod(0o755)
    elif breakage == "dir_symlink":
        real = tmp_path / "elsewhere"
        real.mkdir(mode=0o700)
        tree.lock_dir.symlink_to(real)
    elif breakage == "file_0644":
        prepare_lock(tree).chmod(0o644)
    else:
        prepare_lock(tree)
        (tree.lock_dir / "lock").unlink()
        (tree.lock_dir / "lock").symlink_to(tmp_path / "other")
        (tmp_path / "other").touch(mode=0o600)
    result = tree.call("restart", "metabase", sudo=True)
    assert result.returncode == 64 and "unsafe" in result.stderr
    assert tree.calls("docker") == []


@pytest.mark.parametrize(("sudo", "wait"), [(True, 1), (False, 2)])
def test_busy_lock_exits_75_after_the_bounded_wait(
    runner: Runner, tmp_path: Path, sudo: bool, wait: int
) -> None:
    tree = make_tree(tmp_path, runner, sudo_wait=1, root_wait=2)
    holder = hold(runner, prepare_lock(tree))
    try:
        started = time.monotonic()
        result = tree.call("restart", "metabase", sudo=sudo)
        elapsed = time.monotonic() - started
    finally:
        holder.kill()
    assert result.returncode == 75 and "busy" in result.stderr
    assert wait - 0.2 <= elapsed < wait + 8
    assert tree.calls("docker") == []


def test_exactly_the_mutating_verbs_wait_for_the_lock(runner: Runner, tmp_path: Path) -> None:
    tree = make_tree(tmp_path, runner, root_wait=1)
    holder = hold(runner, prepare_lock(tree), seconds=60)
    try:
        busy = {
            verb
            for verb in VERB_BUDGET
            if tree.call(verb, *VALID_ARGS.get(verb, [])).returncode == 75
        }
    finally:
        holder.kill()
    assert busy == set(LOCKED_VERBS)


def pids_matching(runner: Runner, needle: str) -> int:
    """Count processes whose command line contains *needle* (bracketed so grep ignores itself)."""
    bracket = f"[{needle[0]}]{needle[1:]}"
    script = f'for f in /proc/[0-9]*/cmdline; do tr "\\0" " " < "$f" 2>/dev/null; echo; done | grep -c "{bracket}" || true'
    return int(runner.run(["sh", "-c", script]).stdout.strip() or 0)


@pytest.mark.parametrize("verb", [["ps"], ["start", "metabase"]])
def test_every_verb_runs_under_timeout_and_leaves_nothing_behind(
    runner: Runner, tmp_path: Path, verb: list[str]
) -> None:
    tree = make_tree(tmp_path, runner, sudo_wait=1, budgets={"ps": 1, "start": 1})
    sleeper = "31.37"
    tree.set_tool("docker", sleep=float(sleeper))
    started = time.monotonic()
    result = tree.call(*verb, sudo=True)
    elapsed = time.monotonic() - started
    assert result.returncode == 124 and "time budget" in result.stderr
    assert elapsed < 12
    assert pids_matching(runner, f"sleep {sleeper}") == 0


def test_a_waiting_helper_gives_up_and_leaves_no_process(runner: Runner, tmp_path: Path) -> None:
    tree = make_tree(tmp_path, runner, sudo_wait=1)
    holder = hold(runner, prepare_lock(tree))
    try:
        assert tree.call("up", sudo=True).returncode == 75
        assert pids_matching(runner, "dango-compose.sh") == 0
    finally:
        holder.kill()
