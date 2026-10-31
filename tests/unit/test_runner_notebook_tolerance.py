"""tests/unit/test_runner_notebook_tolerance.py

A path a notebook runner plants under ``notebooks/`` (unreadable directory, dangling link, FIFO,
deep tree, many entries) never blocks the permission re-apply and never gets access; skipped
directories are reported; restored subdirectories such as ``__marimo__`` get the notebook ACL.
Real ``dash`` and ``find`` run against a temp tree with recording fake ``setfacl`` and ``id``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud import runner_reapply
from dango.platform.cloud import runner_scripts as rs
from tests.factories.runner_isolation_fakes import RunnerServer

pytestmark = pytest.mark.unit

DASH = "dash"
FOREIGN_UID = "4242"
ID_SHIM = (
    '[ -n "$FAKE_UID" ] && [ "$1" = -u ] && { echo "$FAKE_UID"; exit 0; }\nexec /usr/bin/id "$@"\n'
)


class Tree:
    """A temp server tree with ``notebooks/`` and recording fake tools first on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.write_text("")
        self.srv = root / "srv" / "dango"
        self.nb = self.srv / "project" / "notebooks"
        self.nb.mkdir(parents=True)
        self.tool("setfacl", 'echo "setfacl|$(pwd -P)|$*" >>"$LOG"\n')
        self.tool("id", ID_SHIM)

    def tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run(self, script: str, *, foreign: bool = False) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "LOG": str(self.log),
            "HOME": str(self.root),
        }
        if foreign:
            env["FAKE_UID"] = FOREIGN_UID
        return subprocess.run(
            [DASH, "-c", script], capture_output=True, text=True, env=env, cwd=self.root
        )

    def acl(self, *, foreign: bool = False) -> subprocess.CompletedProcess[str]:
        return self.run(access.notebooks_acl_script(srv_dir=str(self.srv)), foreign=foreign)

    def report(self, *, foreign: bool = False) -> subprocess.CompletedProcess[str]:
        return self.run(access.notebooks_report_script(srv_dir=str(self.srv)), foreign=foreign)

    def targets(self) -> set[str]:
        """Real paths ``setfacl -P`` was handed (relative names resolved against its cwd)."""
        found: set[str] = set()
        for line in self.log.read_text().splitlines():
            _, cwd, args = line.split("|", 2)
            words = args.split()
            if "-P" not in words:
                continue
            for name in words[3:]:
                found.add(os.path.realpath(os.path.join(cwd, name.removeprefix("./"))))
        return found

    def file_targets(self) -> set[str]:
        return {t for t in self.targets() if t.endswith(".py")}

    def dir_targets(self) -> set[str]:
        return {t for t in self.targets() if not t.endswith(".py")}


def _unlock(root: Path) -> None:
    """Make every directory under *root* traversable again so the temp tree can be removed."""
    stack = [root]
    while stack:
        current = stack.pop()
        if current.is_symlink() or not current.is_dir():
            continue
        current.chmod(0o700)
        stack.extend(current.iterdir())


@pytest.fixture
def tree(tmp_path: Path) -> Iterator[Tree]:
    yield Tree(tmp_path)
    _unlock(tmp_path)


def _trap(parent: Path, name: str = "trapB") -> Path:
    trap = parent / name
    trap.mkdir()
    (trap / "inside.py").write_text("x")
    trap.chmod(0o000)
    return trap


def _real(path: Path) -> str:
    return os.path.realpath(path)


# --- AF-12: a planted unreadable directory does not block the re-apply ---------------------------


def test_acl_script_is_valid_dash() -> None:
    for text in (access.notebooks_acl_script(), access.notebooks_report_script()):
        assert subprocess.run([DASH, "-n", "-c", text], capture_output=True).returncode == 0


def test_runner_owned_unreadable_directory_does_not_fail_the_acl_step(tree: Tree) -> None:
    (tree.nb / "a.py").write_text("x")
    trap = _trap(tree.nb)
    res = tree.acl(foreign=True)  # every entry belongs to someone else, as a runner's would
    assert res.returncode == 0, res.stderr
    assert "Permission denied" not in res.stderr
    assert stat.S_IMODE(trap.stat().st_mode) == 0  # left exactly as the runner made it
    assert not any("trapB" in t for t in tree.targets())
    assert tree.file_targets() == set()  # runner-owned files are not the service user's to change


def test_unreadable_directory_the_service_user_owns_is_made_traversable_and_processed(
    tree: Tree,
) -> None:
    (tree.nb / "a.py").write_text("x")
    trap = _trap(tree.nb)  # owned by the current user: the service user in production
    res = tree.acl()
    assert res.returncode == 0, res.stderr
    assert stat.S_IMODE(trap.stat().st_mode) & 0o700 == 0o700
    assert tree.file_targets() == {_real(tree.nb / "a.py"), _real(trap / "inside.py")}
    assert not stat.S_IMODE(trap.stat().st_mode) & 0o077  # nobody else gained anything


def test_hostile_tree_matrix_never_blocks_and_only_touches_real_python_files(tree: Tree) -> None:
    (tree.nb / "a.py").write_text("x")
    (tree.nb / "dangling.py").symlink_to(tree.root / "missing")
    (tree.nb / "loop").symlink_to(tree.nb)
    (tree.nb / "outside").symlink_to(tree.root)
    os.mkfifo(tree.nb / "pipe.py")
    _trap(tree.nb, "trap000")
    deep = tree.nb
    for i in range(40):
        deep = deep / f"d{i}"
        deep.mkdir()
    (deep / "deep.py").write_text("x")
    many = tree.nb / "many"
    many.mkdir()
    for i in range(300):
        (many / f"f{i}.py").write_text("x")
        (many / f"f{i}.txt").write_text("x")
    for i in range(40):
        (many / f"dir{i}").mkdir()
    res = tree.acl()
    assert res.returncode == 0, res.stderr
    expected = {
        _real(tree.nb / "a.py"),
        _real(deep / "deep.py"),
        _real(tree.nb / "trap000" / "inside.py"),
    }
    expected |= {_real(many / f"f{i}.py") for i in range(300)}
    assert tree.file_targets() == expected
    assert not any(
        "dangling" in t or "pipe" in t or t.startswith(_real(tree.root / "missing"))
        for t in tree.targets()
    )
    assert tree.report().returncode == 0  # all of it readable now, nothing left to report


def test_a_setfacl_failure_is_still_fatal_next_to_an_unreadable_directory(tree: Tree) -> None:
    (tree.nb / "a.py").write_text("x")
    _trap(tree.nb)
    tree.tool("setfacl", "exit 1\n")
    assert tree.acl(foreign=True).returncode != 0
    assert tree.acl().returncode != 0


def test_the_acl_step_still_refuses_a_symlinked_notebooks_directory(tree: Tree) -> None:
    tree.nb.rmdir()
    tree.nb.symlink_to(tree.root)
    res = tree.acl()
    assert res.returncode == 3 and tree.log.read_text() == ""


def test_report_names_runner_made_unreadable_directories_on_one_clean_line(tree: Tree) -> None:
    (tree.nb / "fine").mkdir()
    _trap(tree.nb, "trapB")
    _trap(tree.nb, "esc\x1b[31m\nlast")
    res = tree.report(foreign=True)
    assert res.returncode == access.UNREADABLE_EXIT
    last = res.stderr.strip().splitlines()[-1]
    assert "notebooks/trapB" in last and "fine" not in last
    assert re.fullmatch(r"[ -~]+", last), repr(last)  # printable ASCII only: no escape sequences
    assert tree.acl().returncode == 0  # the service user repairs what it owns itself
    assert tree.report().returncode == 0  # so nothing is left to report


def test_report_is_bounded_for_many_unreadable_directories(tree: Tree) -> None:
    for i in range(60):
        _trap(tree.nb, f"t{i:02d}")
    res = tree.report(foreign=True)
    assert res.returncode == access.UNREADABLE_EXIT
    last = res.stderr.strip().splitlines()[-1]
    assert last.count("notebooks/") <= 5 and "more" in last


def test_report_does_not_follow_links_or_change_anything(tree: Tree) -> None:
    (tree.nb / "loop").symlink_to(tree.nb)
    _trap(tree.nb)
    res = tree.report(foreign=True)
    assert res.returncode == access.UNREADABLE_EXIT
    assert tree.log.read_text() == ""


def test_acl_scripts_use_only_no_follow_walks_and_no_deletion_in_text() -> None:
    for text in (access.notebooks_acl_script(), access.notebooks_report_script()):
        assert "find -P" in text and "-prune" in text
        assert "find -L" not in text and "-follow" not in text and "rm " not in text
    acl = access.notebooks_acl_script()
    assert "-execdir setfacl -P" in acl and "chmod u+rwx" in acl
    assert "-user" in acl  # only what the service user owns is changed


# --- AF-11: restored subdirectories get the notebook ACL -----------------------------------------


def test_restored_marimo_cache_directory_gets_the_notebook_acl(tree: Tree) -> None:
    (tree.nb / "__marimo__" / "cache").mkdir(parents=True)
    (tree.nb / "sub").mkdir()
    res = tree.acl()
    assert res.returncode == 0, res.stderr
    assert {
        _real(tree.nb),
        _real(tree.nb / "__marimo__"),
        _real(tree.nb / "__marimo__" / "cache"),
        _real(tree.nb / "sub"),
    } <= tree.dir_targets() | {_real(tree.nb)}
    dir_calls = [
        line.split("|", 2)[2]
        for line in tree.log.read_text().splitlines()
        if "-P" in line.split("|", 2)[2].split() and not line.endswith(".py")
    ]
    assert dir_calls and all(call.startswith("-P -m " + access.NOTEBOOKS_ACL) for call in dir_calls)


# --- the report step is part of every plan; a skipped directory is a warning, not a failure -----


def _steps(plan: list[ri.Step]) -> dict[str, ri.Step]:
    return {s.name: s for s in plan}


@pytest.mark.parametrize("plan", [ri.build_steps(), runner_reapply.reapply_steps()])
def test_both_plans_report_skipped_notebook_directories_after_the_acl_step(
    plan: list[ri.Step],
) -> None:
    names = [s.name for s in plan]
    assert names.index("notebooks_report") == names.index("notebooks_acl") + 1
    step = _steps(plan)["notebooks_report"]
    assert step.run_as == "service" and step.on_failure == "warn"
    assert step.warning == access.NOTEBOOKS_SKIPPED_WARNING


def test_a_skipped_directory_leaves_isolation_enabled_with_a_warning() -> None:
    def execute(step: ri.Step) -> ri.StepResult:
        if step.name == "notebooks_report":
            return ri.StepResult(False, "unreadable: notebooks/trapB")
        return ri.StepResult(True)

    outcome = ri.run_steps(ri.build_steps(), execute)
    assert outcome.enabled and "marker" in outcome.attempted
    assert len(outcome.warnings) == 1 and "notebooks/trapB" in outcome.warnings[0]
    assert access.NOTEBOOKS_SKIPPED_WARNING in outcome.warnings[0]


def _isolated_server(rfail: dict[str, int]) -> RunnerServer:
    srv = RunnerServer(version="1.0.14", marker=True, in_group=False)
    srv.rfail = rfail
    srv.record = json.dumps({"dango": "1.0.14", "docker_access": "helper"})
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    return srv


def test_reapply_reports_a_skipped_directory_without_calling_it_a_failure() -> None:
    srv = _isolated_server({"notebooks_report": access.UNREADABLE_EXIT})
    warnings: list[str] = []
    msg = runner_reapply.reapply(srv, warnings)
    assert msg == "Runner permissions re-applied"
    assert len(warnings) == 1 and warnings[0].startswith(access.NOTEBOOKS_SKIPPED_WARNING)
    assert not warnings[0].startswith(runner_reapply.REAPPLY_MARK)
    assert srv.runner_events()[-1] == "verify"


def test_reapply_local_returns_the_skipped_directory_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = Path(rs.marker_path(str(tmp_path)))
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")

    def execute(step: ri.Step) -> ri.StepResult:
        if step.name == "notebooks_report":
            return ri.StepResult(False, "unreadable: notebooks/trapB")
        return ri.StepResult(True)

    monkeypatch.setattr(runner_reapply, "_local_executor", lambda: execute)
    notes = runner_reapply.reapply_local(str(tmp_path))
    assert len(notes) == 1 and "notebooks/trapB" in notes[0]
