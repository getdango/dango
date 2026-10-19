"""tests/unit/test_compose_helper_lifetime.py

Compose helper: the workload never outlives the lock, signal-handling order, and the
Metabase data volume ownership fix in the rendered entrypoint.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import server_compose as sc
from tests.factories.compose_helper_sh import Runner, Tree, make_tree, runner_session

pytestmark = pytest.mark.unit

# A docker that ignores TERM, as a compose run waiting on its container may.
_STUBBORN_DOCKER = """#!/bin/sh
trap '' TERM
echo $$ >"$(dirname "$0")/../dpid"
while :; do sleep 1; done
"""
SCAN = (
    'for p in /proc/[0-9]*; do [ "$p" = "/proc/$$" ] && continue; '
    "c=$(tr '\\0' ' ' <$p/cmdline 2>/dev/null) || continue; "
    'case "$c" in *"$PAT"*) echo "$c";; esac; done'
)


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


def stubborn_tree(runner: Runner, tmp_path: Path, budgets: dict[str, int] | None = None) -> Tree:
    tree = make_tree(tmp_path, runner, budgets=budgets)
    (tree.root / "shim" / "docker").write_text(_STUBBORN_DOCKER)
    (tree.root / "shim" / "docker").chmod(0o755)
    return tree


def docker_processes(tree: Tree) -> str:
    return tree.runner.run(
        ["sh", "-c", SCAN], env={"PAT": str(tree.root / "shim" / "docker")}
    ).stdout.strip()


def lock_is_free(tree: Tree) -> bool:
    return tree.runner.run(["flock", "-n", str(tree.lock_dir / "lock"), "true"]).returncode == 0


def reap(tree: Tree) -> None:
    pid = tree.root / "dpid"
    if pid.exists():
        tree.runner.run(
            ["sh", "-c", 'kill -KILL -- -"$0" "$0" 2>/dev/null; true', pid.read_text().strip()]
        )


def test_workload_is_killed_when_the_budget_expires(runner: Runner, tmp_path: Path) -> None:
    tree = stubborn_tree(runner, tmp_path, {"up": 1})
    try:
        assert tree.call("up", sudo=True, timeout=60).returncode in (124, 137)
        assert docker_processes(tree) == ""
    finally:
        reap(tree)


def test_lock_is_held_until_the_workload_is_gone_after_term(runner: Runner, tmp_path: Path) -> None:
    tree = stubborn_tree(runner, tmp_path)
    pidfile = tmp_path / "helper.pid"
    proc = runner.popen(["sh", "-c", f"echo $$ >{pidfile}; SUDO_UID=1000 exec sh {tree.script} up"])
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (tmp_path / "dpid").exists():
            time.sleep(0.1)
        assert (tmp_path / "dpid").exists(), "docker was never started"
        runner.run(["sh", "-c", 'kill -TERM "$0"', pidfile.read_text().strip()])
        time.sleep(1.5)
        assert docker_processes(tree) != "", "stubborn docker should still be running"
        assert not lock_is_free(tree)
        proc.wait(timeout=40)
        assert docker_processes(tree) == ""
    finally:
        reap(tree)
        proc.kill()
        proc.wait()


def test_trap_is_installed_before_the_job_starts_and_wait_has_no_pid_probe() -> None:
    run_fn = ch.helper_script().split("run() {", 1)[1].split("\n}", 1)[0]
    trap_at, job_at = run_fn.index("trap '"), run_fn.index("& cp=$!")
    assert trap_at < job_at
    assert "kill -0" not in run_fn
    assert "k=1" in run_fn and '[ "$k" = 1 ] || break' in run_fn


def test_rendered_entrypoint_chowns_the_metabase_volume_before_dropping_privileges() -> None:
    text = sc.render_entrypoint().decode()
    ids = f"{ch.METABASE_ID}:{ch.METABASE_ID}"
    assert f"chown -R {ids} /metabase-data" in text
    assert text.index("/metabase-data") < text.index("gosu")
