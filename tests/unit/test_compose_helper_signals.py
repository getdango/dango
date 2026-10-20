"""tests/unit/test_compose_helper_signals.py

Compose helper `run()`: a signal at any moment reaches the workload and the real exit status survives,
checked in the script text and by signalling the real script under dash many times.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud import compose_helper as ch
from tests.factories.compose_helper_sh import Runner, Tree, make_tree, runner_session

pytestmark = pytest.mark.unit

ITERATIONS = 24


def run_fn() -> str:
    return ch.helper_script().split("run() {", 1)[1].split("\n}", 1)[0]


def test_trap_forwards_the_signal_itself_so_no_window_remains_before_wait() -> None:
    trap = next(
        line for line in run_fn().splitlines() if "trap '" in line and "HUP INT TERM" in line
    )
    assert "k=1;" in trap
    assert "sg=1;" in trap  # whatever the signal left in the process group is ended afterwards
    assert '[ -z "$cp" ] || kill -TERM "$cp" 2>/dev/null || :' in trap


def test_second_wait_is_omitted_once_the_child_is_reaped() -> None:
    text = run_fn()
    assert '[ "$k" = 1 ] && [ -e "/proc/$cp" ] || break' in text
    assert '[ "$k" = 1 ] || break' not in text


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


def slow_docker_tree(runner: Runner, tmp_path: Path) -> Tree:
    tree = make_tree(tmp_path, runner)
    tree.set_tool("docker", sleep=60)
    return tree


# The fake docker sleeps 60 s as a child of its shell; nothing may be left of it once the helper is gone.
LEFTOVER = (
    'for p in /proc/[0-9]*; do c=$(tr "\\0" " " <$p/cmdline 2>/dev/null) || continue; '
    'case "$c" in "sleep 60 ") echo "$p";; esac; done'
)


def start_helper(tree: Tree, tmp_path: Path) -> tuple[subprocess.Popen[str], Path, Path]:
    """Run the helper under an outer shell that records its exit status in a file, so the tests
    do not depend on `docker exec` noticing a process that exits within milliseconds."""
    pidfile, rcfile = tmp_path / "helper.pid", tmp_path / "helper.rc"
    pidfile.unlink(missing_ok=True)
    rcfile.unlink(missing_ok=True)
    inner = f"echo $$ >{pidfile}; SUDO_UID=1000 exec sh {tree.script} up"
    proc = tree.runner.popen(["sh", "-c", f"sh -c {shlex.quote(inner)}; echo $? >{rcfile}"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
        time.sleep(0.002)  # the file exists before the pid is written into it
    return proc, pidfile, rcfile


def wait_status(rcfile: Path, limit: float = 25) -> int:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if rcfile.exists() and rcfile.read_text().strip():
            return int(rcfile.read_text().strip())
        time.sleep(0.01)
    raise AssertionError("the helper did not exit")


def signalled_run(
    tree: Tree, tmp_path: Path, delay: float, signals: tuple[str, ...] = ("TERM",)
) -> tuple[int, float]:
    start = time.monotonic()
    proc, pidfile, rcfile = start_helper(tree, tmp_path)
    try:
        time.sleep(delay)
        pid = pidfile.read_text().strip()
        for sig in signals:
            tree.runner.run(["sh", "-c", f'kill -{sig} "$0" 2>/dev/null; true', pid])
        code = wait_status(rcfile)
        took = time.monotonic() - start
        assert tree.runner.run(["sh", "-c", LEFTOVER]).stdout.strip() == "", "workload left behind"
        return code, took
    finally:
        proc.kill()
        proc.wait()


def test_term_at_any_moment_ends_the_workload_promptly_with_status_143(
    runner: Runner, tmp_path: Path
) -> None:
    tree = slow_docker_tree(runner, tmp_path)
    for i in range(ITERATIONS):
        code, took = signalled_run(tree, tmp_path, delay=0.013 * i)
        assert code == 143, (i, code)
        assert took < 25, (i, took)


def test_hup_and_int_behave_like_term(runner: Runner, tmp_path: Path) -> None:
    tree = slow_docker_tree(runner, tmp_path)
    for i, sig in enumerate(["HUP", "INT"] * 6):
        code, took = signalled_run(tree, tmp_path, delay=0.02 * i, signals=(sig,))
        assert code in (129, 130, 143), (sig, code)
        assert took < 25


def test_a_second_signal_does_not_replace_the_real_status_with_127(
    runner: Runner, tmp_path: Path
) -> None:
    tree = slow_docker_tree(runner, tmp_path)
    for i in range(ITERATIONS):
        code, _took = signalled_run(tree, tmp_path, delay=0.01 * i, signals=("TERM", "TERM"))
        assert code in (143, 0), (i, code)
        assert code != 127


def test_budget_expiry_keeps_status_124_or_137(runner: Runner, tmp_path: Path) -> None:
    tree = make_tree(tmp_path, runner, budgets={"up": 1})
    tree.set_tool("docker", sleep=30)
    assert tree.call("up", sudo=True, timeout=60).returncode in (124, 137)
