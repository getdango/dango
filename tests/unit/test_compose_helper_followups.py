"""tests/unit/test_compose_helper_followups.py

Compose helper: a signal at any point (before the pid is known, or while the workload
runs) stops the workload, never leaves it behind, and never makes the helper exit early
under ``set -eu``; the lock's lifetime caveat is documented next to the lock.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud.compose_helper import helper_script
from tests.factories.compose_helper_sh import Runner, Tree, make_tree, runner_session

pytestmark = pytest.mark.unit

SPAWN = 'timeout -k 10 "$t" "$@" </dev/null 9>&- & cp=$!'
GAP = 'timeout -k 10 "$t" "$@" </dev/null 9>&- & kill -TERM $$; cp=$!'
LONG_DOCKER = """#!/bin/sh
echo $$ >"$(dirname "$0")/../dpid"
exec sleep 30
"""
STUBBORN_DOCKER = """#!/bin/sh
trap '' TERM
echo $$ >"$(dirname "$0")/../dpid"
while :; do sleep 1; done
"""
ALIVE = (
    'for p in /proc/[0-9]*; do [ "$p" = "/proc/$$" ] && continue; '
    "c=$(tr '\\0' ' ' <$p/cmdline 2>/dev/null) || continue; "
    'case "$c" in *"$PAT"*) echo "$c";; esac; done'
)


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


def workload_tree(runner: Runner, tmp_path: Path, docker: str, *, gap: bool = False) -> Tree:
    tree = make_tree(tmp_path, runner)
    shim = tree.root / "shim" / "docker"
    shim.write_text(docker)
    shim.chmod(0o755)
    if gap:
        text = tree.script.read_text()
        assert text.count(SPAWN) == 1
        tree.script.write_text(text.replace(SPAWN, GAP))
    return tree


def workload_running(tree: Tree) -> bool:
    out = tree.runner.run(["sh", "-c", ALIVE], env={"PAT": str(tree.root / "shim" / "docker")})
    return out.stdout.strip() != ""


def reap(tree: Tree) -> None:
    pid = tree.root / "dpid"
    if pid.exists():
        tree.runner.run(
            [
                "sh",
                "-c",
                'kill -KILL "-$0" 2>/dev/null; kill -KILL "$0" 2>/dev/null; true',
                pid.read_text().strip(),
            ]
        )


def test_signal_in_the_gap_before_the_pid_is_recorded_stops_the_workload(
    runner: Runner, tmp_path: Path
) -> None:
    tree = workload_tree(runner, tmp_path, LONG_DOCKER, gap=True)
    try:
        done = tree.call("stop", "metabase", sudo=True, timeout=25)
        assert done.returncode in (143, 124), done.stderr
        assert not workload_running(tree)
    finally:
        reap(tree)


@pytest.mark.parametrize("round_", range(20))
def test_gap_signal_is_not_flaky(runner: Runner, tmp_path: Path, round_: int) -> None:
    tree = workload_tree(runner, tmp_path, LONG_DOCKER, gap=True)
    try:
        start = time.monotonic()
        done = tree.call("stop", "metabase", sudo=True, timeout=25)
        assert done.returncode in (143, 124), done.stderr
        assert time.monotonic() - start < 15
        assert not workload_running(tree)
    finally:
        reap(tree)


def test_helper_does_not_return_before_a_term_ignoring_workload_exits(
    runner: Runner, tmp_path: Path
) -> None:
    tree = workload_tree(runner, tmp_path, STUBBORN_DOCKER, gap=True)
    try:
        done = tree.call("stop", "metabase", sudo=True, timeout=40)
        assert done.returncode in (143, 124, 137), done.stderr
        assert not workload_running(tree)
    finally:
        reap(tree)


def test_signal_while_the_workload_runs_exits_with_a_signal_status_not_1(
    runner: Runner, tmp_path: Path
) -> None:
    tree = workload_tree(runner, tmp_path, LONG_DOCKER)
    pidfile = tmp_path / "helper.pid"
    proc = runner.popen(
        ["sh", "-c", f"echo $$ >{pidfile}; SUDO_UID=1000 exec sh {tree.script} stop metabase"]
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (tmp_path / "dpid").exists():
            time.sleep(0.05)
        runner.run(["kill", "-TERM", pidfile.read_text().strip()])
        proc.communicate(timeout=30)
        assert proc.returncode in (143, 124, 137)
        assert not workload_running(tree)
    finally:
        proc.kill()
        reap(tree)


def test_trap_only_records_the_signal() -> None:
    trap = next(line for line in helper_script().splitlines() if line.strip().startswith("trap '"))
    assert "kill" not in trap


def test_lock_comment_states_the_sigkill_caveat() -> None:
    lines = helper_script().splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("locked()"))
    comment = "\n".join(lines[max(0, at - 3) : at])
    assert "fd 9" in comment
    assert "SIGKILL" in comment
