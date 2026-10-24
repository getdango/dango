"""tests/unit/test_runner_isolation.py

The runner-isolation plan: step order (marker last), failure policy, id rules, marker record
parsing and trust, and that importing the modules has no side effects.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dango.platform import run_as
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as iso
from dango.platform.cloud import runner_scripts as rs

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
MODULES = ("runner_isolation", "runner_scripts", "runner_access_scripts")


def test_constants_agree_with_the_launcher() -> None:
    assert rs.RESERVED_IDS == tuple(sorted(run_as.RESERVED_UIDS)) == (999, 61000, 61001)
    assert rs.marker_path() == run_as.MARKER == iso.MARKER
    assert rs.RUN_USER == run_as.runner_user("script")
    assert rs.NB_USER == run_as.runner_user("nb")
    assert (rs.UID_MIN, rs.UID_MAX) == (1000, 60000)
    assert run_as.MARKER.startswith(rs.runner_dir() + "/")


def test_modules_stay_within_the_size_limit() -> None:
    for name in MODULES:
        path = REPO / "dango" / "platform" / "cloud" / f"{name}.py"
        assert len(path.read_text().splitlines()) <= 500, name


def test_importing_has_no_side_effects() -> None:
    code = (
        "import sys, dango.platform.cloud\n"
        "events = []\n"
        "watched = ('subprocess.Popen', 'os.system', 'os.exec', 'os.spawn', 'os.mkdir',\n"
        "           'os.remove', 'os.rename', 'os.rmdir', 'shutil.copyfile', 'socket.connect',\n"
        "           'os.chmod', 'os.chown')\n"
        "def hook(event, args):\n"
        "    if event in watched:\n"
        "        events.append(event)\n"
        "    elif event == 'open' and len(args) > 1 and args[1] and any(c in str(args[1]) for c in 'wax+'):\n"
        "        events.append(('open', args[0], args[1]))\n"
        "sys.addaudithook(hook)\n"
        "import dango.platform.cloud.runner_isolation, dango.platform.cloud.runner_scripts\n"
        "import dango.platform.cloud.runner_access_scripts\n"
        "assert not events, events\n"
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr


def test_step_order_puts_the_marker_last_after_verify() -> None:
    names = [s.name for s in iso.build_steps()]
    assert names[-1] == "marker" and names[-2] == "verify"
    assert names.count("marker") == 1
    for before, after in [
        ("runner_base_dir", "users"),
        ("users", "runner_dirs"),
        ("runner_dirs", "project_permissions"),
        ("users", "sudoers"),
        ("project_permissions", "verify"),
        ("notebooks_acl", "verify"),
        ("sudoers", "verify"),
        ("nft_rules", "verify"),
        ("nftables_package", "nft_rules"),
    ]:
        assert names.index(before) < names.index(after), (before, after)


def test_failure_policy_is_declared_per_step() -> None:
    steps = {s.name: s for s in iso.build_steps()}
    warn = {n for n, s in steps.items() if s.on_failure == "warn"}
    assert warn == {"acl_package", "nftables_package", "nft_rules"}
    assert steps["nft_rules"].requires == ("nftables_package",)
    assert steps["marker"].requires == ("verify",)
    for name in ("users", "sudoers", "project_permissions", "notebooks_acl", "verify", "marker"):
        assert steps[name].on_failure == "fatal"
    assert {n for n, s in steps.items() if s.run_as == "service"} == {
        "project_permissions",
        "notebooks_acl",
    }


class Exec:
    def __init__(self, failing: dict[str, str] | None = None) -> None:
        self.failing = failing or {}
        self.ran: list[str] = []

    def __call__(self, step: iso.Step) -> iso.StepResult:
        self.ran.append(step.name)
        if step.name in self.failing:
            return iso.StepResult(False, self.failing[step.name])
        return iso.StepResult(True)


def test_all_steps_succeeding_enables_with_restricted_loopback() -> None:
    ex = Exec()
    out = iso.run_steps(iso.build_steps(), ex)
    assert out.enabled and out.loopback == "restricted" and out.warnings == ()
    assert ex.ran[-1] == "marker" and out.failed is None


def test_nft_rule_failure_only_warns() -> None:
    ex = Exec({"nft_rules": "unit failed"})
    out = iso.run_steps(iso.build_steps(), ex)
    assert out.enabled and out.loopback == "unrestricted"
    assert any("loopback: unrestricted" in w and "unit failed" in w for w in out.warnings)
    assert ex.ran[-1] == "marker"


def test_nft_package_failure_means_no_rules_and_a_warning() -> None:
    ex = Exec({"nftables_package": "apt failed"})
    out = iso.run_steps(iso.build_steps(), ex)
    assert out.enabled and out.loopback == "unrestricted"
    assert "nft_rules" not in ex.ran
    assert any("loopback: unrestricted" in w for w in out.warnings)
    assert any("nftables package" in w for w in out.warnings)


def test_acl_package_failure_alone_does_not_block() -> None:
    out = iso.run_steps(iso.build_steps(), Exec({"acl_package": "apt failed"}))
    assert out.enabled and any("acl package" in w for w in out.warnings)


@pytest.mark.parametrize(
    "name",
    [
        "runner_base_dir",
        "users",
        "runner_dirs",
        "sudoers",
        "project_permissions",
        "notebooks_acl",
        "verify",
        "marker",
    ],
)
def test_fatal_failures_stop_before_the_marker(name: str) -> None:
    ex = Exec({name: "boom"})
    out = iso.run_steps(iso.build_steps(), ex)
    assert not out.enabled and out.failed == name and out.detail == "boom"
    assert ex.ran[-1] == name
    if name != "marker":
        assert "marker" not in ex.ran


def test_acl_failure_for_the_notebook_role_is_fatal_even_when_everything_else_passes() -> None:
    ex = Exec({"notebooks_acl": "setfacl: Operation not supported"})
    out = iso.run_steps(iso.build_steps(), ex)
    assert not out.enabled and "marker" not in ex.ran and "verify" not in ex.ran


def test_ids_rules() -> None:
    iso.assert_runner_ids({"dango-run": (1001, 1001), "dango-nb": (1002, 1002)})
    iso.assert_runner_ids({"a": (1000, 1000), "b": (59999, 59999)})
    bad = [
        {"a": (999, 1001)},
        {"a": (1001, 999)},
        {"a": (61000, 1001)},
        {"a": (1001, 61001)},
        {"a": (60000, 1001)},
        {"a": (0, 1001)},
        {"a": (500, 500)},
        {"a": (1001, 1001), "b": (1001, 1002)},
    ]
    for case in bad:
        with pytest.raises(ValueError):
            iso.assert_runner_ids(case)


def test_ids_rules_match_the_launcher_rule() -> None:
    for uid in (0, 999, 1000, 1001, 59999, 60000, 61000, 61001):
        try:
            run_as.assert_runner_uid(uid)
            launcher_ok = True
        except ValueError:
            launcher_ok = False
        try:
            iso.assert_runner_ids({"x": (uid, 1001)})
            plan_ok = True
        except ValueError:
            plan_ok = False
        assert launcher_ok == plan_ok, uid


def test_parse_record() -> None:
    rec = iso.parse_record('{"schema": 1, "loopback": "restricted"}\n')
    assert rec == iso.RunnerRecord(1, "restricted")
    assert iso.parse_record('{"schema": 3, "loopback": "unrestricted"}').schema == 3
    for text in (
        "",
        "[]",
        "nope",
        '{"schema": 0, "loopback": "restricted"}',
        '{"schema": true, "loopback": "restricted"}',
        '{"schema": 1, "loopback": "maybe"}',
        '{"loopback": "restricted"}',
    ):
        with pytest.raises(ValueError):
            iso.parse_record(text)


def _write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def test_read_record_trusts_only_a_clean_regular_file(tmp_path: Path) -> None:
    good = '{"schema": 1, "loopback": "unrestricted"}\n'
    me = os.getuid()
    path = _write(tmp_path / "active", good)
    assert iso.read_record(str(path), trusted_uid=me) == iso.RunnerRecord(1, "unrestricted")
    assert iso.read_record(str(path), trusted_uid=me + 1) is None
    assert iso.read_record(str(tmp_path / "missing"), trusted_uid=me) is None
    assert iso.read_record(str(tmp_path), trusted_uid=me) is None
    link = tmp_path / "link"
    link.symlink_to(path)
    assert iso.read_record(str(link), trusted_uid=me) is None
    for mode in (0o664, 0o646):
        assert iso.read_record(str(_write(tmp_path / "w", good, mode)), trusted_uid=me) is None
    assert iso.read_record(str(_write(tmp_path / "bad", "{")), trusted_uid=me) is None


def test_record_and_launcher_trust_the_same_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    me = os.getuid()
    monkeypatch.setattr(run_as, "_ROOT_UID", me)
    path = _write(tmp_path / "active", '{"schema": 1, "loopback": "restricted"}')
    monkeypatch.setattr(run_as, "MARKER", str(path))
    assert run_as.uses_isolation() and iso.read_record(str(path), trusted_uid=me)
    path.chmod(0o666)
    assert not run_as.uses_isolation() and iso.read_record(str(path), trusted_uid=me) is None


def test_access_module_exports_the_scripts_the_plan_uses() -> None:
    assert access.project_dir() == "/srv/dango/project"
    assert callable(access.notebooks_acl_script)
