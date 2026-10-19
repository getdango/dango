"""tests/unit/test_compose_helper_review.py

Compose helper hardening: container-id refusal, signal handling, lock directory race,
install failure handling, private metabase directory and lock fd inheritance.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud import compose_helper as ch
from tests.factories.compose_helper_sh import Runner, Tree, make_tree, runner_session

pytestmark = pytest.mark.unit

_FAKE_GETENT = """#!/bin/sh
d=$(dirname "$0")
if [ -f "$d/hit.$1.$2" ]; then cat "$d/hit.$1.$2"; exit 0; fi
exit 2
"""
RENDER = ["render", "3000", "8081", "v0.59.1", "n"]
SCAN = (
    'for p in /proc/[0-9]*; do [ "$p" = "/proc/$$" ] && continue; '
    "c=$(tr '\\0' ' ' <$p/cmdline 2>/dev/null) || continue; "
    'case "$c" in *"$PAT"*) echo "$c";; esac; done'
)


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


@pytest.fixture
def tree(runner: Runner, tmp_path: Path) -> Tree:
    return make_tree(tmp_path, runner)


def host_has(tree: Tree, database: str, key: str, line: str) -> None:
    shim = tree.root / "shim"
    (shim / "getent").write_text(_FAKE_GETENT)
    (shim / "getent").chmod(0o755)
    (shim / f"hit.{database}.{key}").write_text(line + "\n")


CLASHES = [
    ("passwd", "61000", "x:x:61000:61000::/:/bin/false"),
    ("passwd", "61001", "x:x:61001:100::/:/bin/false"),
    ("group", "61000", "x:x:61000:"),
    ("group", "61001", "x:x:61001:"),
    ("group", "docker", "docker:x:61000:"),
]


@pytest.mark.parametrize(("database", "key", "line"), CLASHES)
@pytest.mark.parametrize("verb", [["self-test"], RENDER], ids=["self-test", "render"])
def test_self_test_and_render_refuse_when_the_host_uses_a_container_id(
    tree: Tree, database: str, key: str, line: str, verb: list[str]
) -> None:
    host_has(tree, database, key, line)
    result = tree.call(*verb, sudo=True)
    assert result.returncode == 67 and "container" in result.stderr
    assert tree.calls("python") == [] and tree.calls("docker") == []


def test_other_docker_group_ids_do_not_block_self_test(tree: Tree) -> None:
    host_has(tree, "group", "docker", "docker:x:999:")
    result = tree.call("self-test", sudo=True)
    assert result.returncode == 0, result.stderr


def kill_when_running(tree: Tree, verb: str) -> subprocess.Popen[str]:
    tree.set_tool("docker", sleep=20)
    pidfile = tree.root / "helper.pid"
    proc = tree.runner.popen(
        ["sh", "-c", f"echo $$ >{pidfile}; SUDO_UID=1000 exec sh {tree.script} {verb}"]
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not tree.logs("docker", "argv"):
        time.sleep(0.1)
    assert tree.logs("docker", "argv"), "docker was never started"
    tree.runner.run(["sh", "-c", 'kill -TERM "$0"', pidfile.read_text().strip()])
    return proc


def leftovers(tree: Tree, pattern: str) -> str:
    return tree.runner.run(["sh", "-c", SCAN], env={"PAT": pattern}).stdout.strip()


def test_killing_the_helper_leaves_no_timeout_flock_or_docker(tree: Tree) -> None:
    proc = kill_when_running(tree, "up")
    time.sleep(1.5)
    try:
        assert leftovers(tree, str(tree.lock_dir / "lock")) == ""
        assert leftovers(tree, str(tree.root / "shim" / "docker")) == ""
    finally:
        proc.wait(timeout=60)


def test_signal_exit_status_is_propagated(tree: Tree) -> None:
    tree.set_tool("docker", rc=7)
    assert tree.call("up", sudo=True).returncode == 7


def test_lock_directory_created_by_a_racing_caller_is_accepted(tree: Tree) -> None:
    (tree.root / "shim" / "mkdir").write_text('#!/bin/sh\n/bin/mkdir "$@"\nexit 1\n')
    (tree.root / "shim" / "mkdir").chmod(0o755)
    assert not tree.lock_dir.exists()
    assert tree.call("up", sudo=True).returncode == 0
    assert tree.lock_dir.is_dir()


def test_concurrent_first_calls_both_succeed(tree: Tree) -> None:
    procs = [tree.runner.popen(["sh", str(tree.script), "stop"]) for _ in range(2)]
    for proc in procs:
        proc.wait(timeout=60)
        assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""


def test_a_failing_chown_stops_the_install(tree: Tree) -> None:
    shim = tree.root / "shim"
    for name, body in (
        ("install", '#!/bin/sh\nfor a; do :; done\n/bin/mkdir -p "$a"\n'),
        ("chown", "#!/bin/sh\nexit 1\n"),
        ("chmod", "#!/bin/sh\nexit 0\n"),
        ("mv", f"#!/bin/sh\necho moved >>{tree.root}/moved\n"),
    ):
        (shim / name).write_text(body)
        (shim / name).chmod(0o755)
    text = ch.install_helper_script().replace("/usr/local/sbin", str(tree.root / "sbin"))
    script = tree.root / "install.sh"
    script.write_text(f"PATH={shim}:$PATH\nset -e\n{text}echo finished\n")
    result = tree.runner.run(["sh", str(script)])
    assert result.returncode != 0 and "finished" not in result.stdout
    assert not (tree.root / "moved").exists()


def test_metabase_directory_must_be_exactly_private(tree: Tree) -> None:
    (tree.srv / "metabase").chmod(0o755)
    result = tree.call("ps", sudo=True)
    assert result.returncode == 64 and "metabase" in result.stderr
    assert tree.calls("docker") == []
    (tree.srv / "metabase").chmod(0o700)
    assert tree.call("ps", sudo=True).returncode == 0


def test_the_lock_is_not_inherited_by_docker(tree: Tree) -> None:
    (tree.root / "shim" / "docker").write_text(
        f'#!/bin/sh\nfor f in /proc/$$/fd/*; do readlink "$f"; done >{tree.root}/fds\nexit 0\n'
    )
    assert tree.call("up", sudo=True).returncode == 0
    assert str(tree.lock_dir / "lock") not in (tree.root / "fds").read_text()
