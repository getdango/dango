"""tests/unit/test_compose_exec_lifetime.py

V22: a dango-side caller that gives up leaves no helper process behind.  Runs the real
helper text (patched paths, fake docker) behind a fake ``sudo`` through the wrapper.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform import compose_exec as ce
from dango.platform.cloud import compose_helper as ch
from tests.factories.compose_helper_sh import Runner, Tree, make_tree, runner_session

pytestmark = pytest.mark.unit

_STUBBORN_DOCKER = """#!/bin/sh
trap '' TERM
while :; do sleep 1; done
"""
FAKE_SUDO = """#!/bin/sh
# sudo -n HELPER verb...: run the patched helper with the uid sudo would record
shift; shift
exec env SUDO_UID=1000 sh "$DANGO_TEST_HELPER" "$@"
"""
SCAN = (
    'for p in /proc/[0-9]*; do [ "$p" = "/proc/$$" ] && continue; '
    "c=$(tr '\\0' ' ' <$p/cmdline 2>/dev/null) || continue; "
    'case "$c" in *"$PAT"*) echo "$c";; esac; done'
)


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        if r.container is not None:
            pytest.skip("needs the host's processes")
        yield r


@pytest.fixture
def tree(runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tree:
    t = make_tree(tmp_path, runner, sudo_wait=1, root_wait=2, budgets={"stop": 1})
    (t.root / "shim" / "sudo").write_text(FAKE_SUDO)
    (t.root / "shim" / "sudo").chmod(0o755)
    marker = tmp_path / "active"
    marker.write_text("")
    marker.chmod(0o644)
    monkeypatch.setattr(ce, "MARKER", str(marker))
    monkeypatch.setattr(ce, "_ROOT_UID", os.getuid())
    monkeypatch.setenv("PATH", f"{t.root / 'shim'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("DANGO_TEST_HELPER", str(t.script))
    return t


def leftovers(tree: Tree) -> str:
    out = ""
    for pat in (str(tree.script), str(tree.root / "shim" / "docker")):
        out += subprocess.run(
            ["sh", "-c", SCAN],
            env={"PAT": pat, "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    return out.strip()


def test_busy_lock_returns_without_a_queued_helper(tree: Tree, tmp_path: Path) -> None:
    tree.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = tree.lock_dir / "lock"
    lock.touch(mode=0o600)
    holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "30"])
    try:
        start = time.monotonic()
        with pytest.raises(ce.ComposeBusy):
            ce.run_compose(tmp_path, "stop", "metabase", timeout=1)
        assert time.monotonic() - start < 20
        assert leftovers(tree) == ""
    finally:
        holder.kill()
        holder.wait()


def test_stubborn_workload_is_gone_when_the_wrapper_returns(tree: Tree, tmp_path: Path) -> None:
    (tree.root / "shim" / "docker").write_text(_STUBBORN_DOCKER)
    (tree.root / "shim" / "docker").chmod(0o755)
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ce.run_compose(tmp_path, "stop", "metabase", timeout=1)
    assert time.monotonic() - start < ce.effective_timeout("stop", 1, helper=True)
    assert leftovers(tree) == ""


@pytest.mark.parametrize("verb", sorted(ch.VERB_BUDGET))
def test_every_helper_verb_ends_before_its_caller_gives_up(verb: str) -> None:
    wait = ch.SUDO_LOCK_WAIT if verb in ch.LOCKED_VERBS else 0
    worst_case_helper = wait + ch.VERB_BUDGET[verb] + 10  # timeout -k 10 grace
    assert ce.effective_timeout(verb, 1, helper=True) > worst_case_helper
