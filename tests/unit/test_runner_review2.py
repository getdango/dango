"""tests/unit/test_runner_review2.py

Second review round of the runner-isolation wiring: the firewall rules load on nftables 1.0.2 and
let replies through, a runner cannot persist code (cron, at, linger, user units, autostart), the
re-apply at the end of a deploy runs before the Metabase first-run trigger, and the smaller
follow-ups (restore warnings, relative PATH entries, special files in pushed directories, the
restart notice, the guard).  Shell text is run under dash against a temp tree with fake tools.
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.cli.commands import deploy_provision as dp
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as iso
from dango.platform.cloud import runner_persistence as rp
from dango.platform.cloud import runner_scripts as rs
from dango.platform.cloud.upgrade_guard import GUARD_ACTION
from tests.factories.runner_isolation_fakes import RunnerServer
from tests.unit.test_runner_hardening import Box

pytestmark = pytest.mark.unit

DASH = shutil.which("dash") or "/bin/sh"
BOTH = 'meta skuid { "dango-run", "dango-nb" }'


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# --- N1: rules nftables 1.0.2 can load ---------------------------------------------------------


def _wildcard_in_set(text: str) -> bool:
    """True when a ``{ ... }`` set on a rule line contains a ``*`` (rejected by nftables 1.0.2)."""
    for line in text.splitlines():
        if line.lstrip().startswith(("table ", "chain ", "}", "#")):
            continue
        if any("*" in group for group in re.findall(r"\{[^}]*\}", line)):
            return True
    return False


def test_the_set_parser_detects_the_rejected_form() -> None:
    bad = f'  {BOTH} oifname {{ "docker0", "br-*" }} reject\n'
    assert _wildcard_in_set(bad)
    assert not _wildcard_in_set(f'  {BOTH} oifname "br-*" reject\n')


def test_nft_ruleset_has_no_wildcard_inside_a_set() -> None:
    assert not _wildcard_in_set(rs.NFT_RULESET)


def test_nft_bridge_rules_are_two_separate_rules() -> None:
    lines = [ln.strip() for ln in rs.NFT_RULESET.splitlines()]
    assert f'{BOTH} oifname "docker0" reject' in lines
    assert f'{BOTH} oifname "br-*" reject' in lines
    assert not any('"docker0", "br-*"' in ln for ln in lines)


# --- N2: replies of allowed flows ---------------------------------------------------------------


def test_established_and_related_traffic_is_the_first_rule() -> None:
    lines = [ln.strip() for ln in rs.NFT_RULESET.splitlines()]
    policy = next(i for i, ln in enumerate(lines) if "policy accept" in ln)
    assert lines[policy + 1] == "ct state established,related accept"
    assert [ln for ln in lines if "established" in ln] == ["ct state established,related accept"]


# --- N3: a runner cannot persist code -------------------------------------------------------------


def _etc(box: Box) -> Path:
    etc = box.root / "etc"
    etc.mkdir()
    return etc


def test_persistence_step_follows_users_and_is_fatal_root_and_free_of_project_paths() -> None:
    steps = {s.name: s for s in iso.build_steps()}
    names = [s.name for s in iso.build_steps()]
    assert names.index("users") < names.index("persistence") < names.index("project_permissions")
    assert names.index("persistence") < names.index("verify")
    step = steps["persistence"]
    assert step.run_as == "root" and step.on_failure == "fatal"
    assert "/srv/dango/project" not in step.script and "$P" not in step.script


def test_deny_lists_are_created_with_both_runners(box: Box) -> None:
    etc = _etc(box)
    res = box.run(rp.deny_lists_script(str(etc)))
    assert res.returncode == 0, res.stderr
    for name in ("cron.deny", "at.deny"):
        assert (etc / name).read_text().splitlines() == ["dango-run", "dango-nb"]
        assert stat.S_IMODE((etc / name).stat().st_mode) == 0o644


def test_deny_lists_are_idempotent_and_keep_existing_entries(box: Box) -> None:
    etc = _etc(box)
    (etc / "at.deny").write_text("daemon\nbin\nsys")  # no trailing newline
    (etc / "cron.deny").write_text("guest\ndango-nb\n")  # one runner already listed
    script = rp.deny_lists_script(str(etc))
    for _ in range(3):
        assert box.run(script).returncode == 0
    assert (etc / "at.deny").read_text().splitlines() == [
        "daemon",
        "bin",
        "sys",
        "dango-run",
        "dango-nb",
    ]
    assert (etc / "cron.deny").read_text().splitlines() == ["guest", "dango-nb", "dango-run"]


def test_deny_lists_refuse_a_symlink_and_leave_its_target_alone(box: Box) -> None:
    etc = _etc(box)
    victim = box.root / "victim"
    victim.write_text("keep\n")
    (etc / "cron.deny").symlink_to(victim)
    res = box.run(rp.deny_lists_script(str(etc)))
    assert res.returncode == 3 and "symlink" in res.stderr
    assert victim.read_text() == "keep\n"


def _persist_tools(box: Box, home: Path, jobs: str = "") -> None:
    box.tool("getent", f'echo "$2:x:1001:1001::{home}:/usr/sbin/nologin"\n')
    box.tool("crontab", 'echo "crontab $*" >>"$LOG"\n')
    box.tool("loginctl", 'echo "loginctl $*" >>"$LOG"\n')
    box.tool("atrm", 'echo "atrm $*" >>"$LOG"\n')
    box.tool("atq", f'printf "{jobs}"\n')
    box.tool("id", "echo 1001\n")
    box.tool("systemctl", 'echo "systemctl $*" >>"$LOG"\nexit 3\n')
    box.tool("pgrep", "exit 1\n")
    box.tool("pkill", "exit 0\n")
    box.tool("sleep", "exit 0\n")


def _paths(box: Box) -> rp.PersistencePaths:
    return rp.PersistencePaths(
        linger_dir=str(box.root / "linger"),
        cron_spool=str(box.root / "crontabs"),
        at_spool=str(box.root / "atjobs"),
    )


def test_quiesce_text_clears_cron_at_linger_and_user_units() -> None:
    text = access.quiesce_script(stop_web=True)
    for needle in (
        'crontab -r -u "$u"',
        "atq",
        "atrm",
        "disable-linger",
        "/var/lib/systemd/linger",
        "autostart",
        "systemd",
        "find -P",
    ):
        assert needle in text, needle
    assert not re.search(r"chown|chmod|rm -rf \"?\$home", text)
    # the job cleanup happens before and after the kill loop
    assert text.count('crontab -r -u "$u"') == 2
    assert (
        text.index('crontab -r -u "$u"') < text.index("pkill") < text.rindex('crontab -r -u "$u"')
    )


def test_quiesce_removes_crontabs_at_jobs_and_linger_for_both_runners(box: Box) -> None:
    home = box.root / "home"
    home.mkdir()
    paths = _paths(box)
    (box.root / "linger").mkdir()
    for u in ("dango-run", "dango-nb", "other"):
        (box.root / "linger" / u).write_text("")
    (box.root / "crontabs").mkdir()
    (box.root / "crontabs" / "dango-nb").write_text("* * * * * x\n")
    _persist_tools(box, home, jobs="7\\tMon Oct 25 10:00:00 2026 a dango-nb\\n8\\tMon a other\\n")
    res = box.run(access.quiesce_script(stop_web=False, paths=paths))
    assert res.returncode == 0, res.stderr
    calls = box.calls()
    assert "crontab -r -u dango-run" in calls and "crontab -r -u dango-nb" in calls
    assert "atrm 7" in calls and "atrm 8" not in calls
    assert "loginctl disable-linger dango-nb" in calls
    assert sorted(p.name for p in (box.root / "linger").iterdir()) == ["other"]
    assert not (box.root / "crontabs" / "dango-nb").exists()


def test_quiesce_removes_user_units_and_autostart_without_following_links(box: Box) -> None:
    home = box.root / "home"
    outside = box.root / "outside"
    for d in (
        home / ".config" / "autostart",
        home / ".config" / "systemd" / "user",
        outside / "kept",
    ):
        d.mkdir(parents=True)
    (home / ".config" / "autostart" / "x.desktop").write_text("x")
    (home / ".config" / "marimo").mkdir()
    (home / ".config" / "marimo" / "marimo.toml").write_text("x")
    (home / ".local" / "share" / "systemd" / "user").mkdir(parents=True)
    (outside / "kept" / "f").write_text("x")
    _persist_tools(box, home)
    res = box.run(access.quiesce_script(stop_web=False, paths=_paths(box)))
    assert res.returncode == 0, res.stderr
    assert not (home / ".config" / "autostart").exists()
    assert not (home / ".config" / "systemd").exists()
    assert not (home / ".local" / "share" / "systemd").exists()
    assert (home / ".config" / "marimo" / "marimo.toml").exists()


def test_quiesce_does_not_follow_a_planted_config_link(box: Box) -> None:
    home = box.root / "home"
    home.mkdir()
    victim = box.root / "victim"
    (victim / "systemd" / "user").mkdir(parents=True)
    (victim / "autostart").mkdir()
    (victim / "systemd" / "user" / "keep.service").write_text("x")
    (home / ".config").symlink_to(victim)
    _persist_tools(box, home)
    res = box.run(access.quiesce_script(stop_web=False, paths=_paths(box)))
    assert res.returncode == 0, res.stderr
    assert (victim / "systemd" / "user" / "keep.service").exists()
    assert (victim / "autostart").is_dir()


def test_quiesce_removes_a_planted_link_inside_config_without_touching_its_target(box: Box) -> None:
    home = box.root / "home"
    (home / ".config").mkdir(parents=True)
    victim = box.root / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    (home / ".config" / "systemd").symlink_to(victim)
    _persist_tools(box, home)
    assert box.run(access.quiesce_script(stop_web=False, paths=_paths(box))).returncode == 0
    assert not (home / ".config" / "systemd").is_symlink()
    assert (victim / "keep").exists()


def test_every_root_cleanup_line_uses_fixed_paths_or_variables() -> None:
    text = access.quiesce_script(stop_web=True)
    assert "/srv/dango/project" not in text and "$P/" not in text
    assert "rm -rf" not in text.replace("-exec rm -rf {} +", "")


def _check_run(box: Box, etc: Path, paths: rp.PersistencePaths) -> Any:
    head = 'set -u\nbad=0\nfail() { echo "verify: $*" >&2; bad=1; }\n'
    return box.run(head + rp.persistence_checks(str(etc), paths) + '\nexit "$bad"\n')


def ssh_tools(box: Box) -> None:
    """Fakes for the ssh and user-manager checks: sshd denies both runners, user@ units masked."""
    box.tool("id", "echo 1001\n")
    box.tool("sshd", 'echo "denyusers dango-run dango-nb"\n')
    box.tool("systemctl", 'case "$1" in is-enabled) echo masked;; esac\nexit 0\n')


def _clean_state(
    box: Box, crontab_out: str = "You (u) are not allowed to use this program"
) -> tuple[Path, Path]:
    etc = _etc(box)
    for name in ("cron.deny", "at.deny"):
        (etc / name).write_text("dango-run\ndango-nb\n")
    home = box.root / "home"
    home.mkdir()
    box.tool("getent", f'echo "$2:x:1001:1001::{home}:/usr/sbin/nologin"\n')
    box.tool("runuser", 'shift 3\n"$@"\n')
    box.tool("crontab", f'echo "{crontab_out}" >&2\nexit 1\n')
    box.tool("at", "cat >/dev/null\necho 'You do not have permission to use at.' >&2\nexit 1\n")
    box.tool("atq", "exit 0\n")
    ssh_tools(box)
    return etc, home


def test_verify_passes_on_a_denied_runner(box: Box) -> None:
    etc, home = _clean_state(box)
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 0, res.stderr


def test_verify_fails_when_a_runner_can_use_crontab(box: Box) -> None:
    etc, home = _clean_state(box, crontab_out="no crontab for dango-nb")
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 1 and "can use crontab" in res.stderr


def test_verify_fails_when_a_runner_can_schedule_at_jobs(box: Box) -> None:
    etc, home = _clean_state(box)
    box.tool("at", "cat >/dev/null\necho job >&2\nexit 0\n")
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 1 and "can schedule at jobs" in res.stderr


@pytest.mark.parametrize("name", ["cron.deny", "at.deny"])
def test_verify_fails_when_a_runner_is_missing_from_a_deny_list(box: Box, name: str) -> None:
    etc, home = _clean_state(box)
    (etc / name).write_text("dango-run\n")
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 1 and f"not in {etc / name}" in res.stderr


def test_verify_fails_on_a_crontab_file_linger_or_user_unit(box: Box) -> None:
    etc, home = _clean_state(box)
    paths = _paths(box)
    (box.root / "crontabs").mkdir()
    (box.root / "crontabs" / "dango-nb").write_text("x")
    assert "has a crontab" in _check_run(box, etc, paths).stderr
    (box.root / "crontabs" / "dango-nb").unlink()
    (box.root / "linger").mkdir()
    (box.root / "linger" / "dango-run").write_text("")
    assert "linger" in _check_run(box, etc, paths).stderr
    (box.root / "linger" / "dango-run").unlink()
    (home / ".config" / "autostart").mkdir(parents=True)
    res = _check_run(box, etc, paths)
    assert res.returncode == 1 and "autostart" in res.stderr


def test_verify_script_contains_the_persistence_checks() -> None:
    text = access.verify_script()
    assert rp.persistence_checks().strip().splitlines()[0] in text
    assert "crontab -l" in text and "not allowed" in text and "at.deny" in text


# --- M-a: re-apply before the Metabase first-run trigger ---------------------------------------


def test_deploy_reapplies_permissions_before_the_metabase_trigger_on_both_paths() -> None:
    for fn in (dp.run_provisioning, dp.run_byos_setup):
        text = inspect.getsource(fn)
        call = text.index("_trigger_metabase_setup(")
        assert text.index("runner_reapply.reapply(") < call, fn.__name__
        assert text.index("runner_reapply.reapply(") < text.index("_health_check("), fn.__name__
        # the last thing before the trigger is not a service stop: the trigger restarts web itself
        assert "stop_web=True" in text


# --- L-a: restore warnings are shown -----------------------------------------------------------


def test_restore_from_spaces_tells_the_user_when_permissions_were_not_reapplied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.factories.backup_shell import run_spaces_restore

    project = tmp_path / "project"
    project.mkdir()
    top = "backup-20260101-000000"
    (tmp_path / top / ".dango").mkdir(parents=True)
    (tmp_path / top / ".dango" / "auth.db").write_text("x")
    with patch(
        "dango.platform.cloud.runner_reapply.reapply_local",
        return_value=["Runner permissions were not re-applied (verify: boom)."],
    ):
        run_spaces_restore(tmp_path, project, top)
    out = capsys.readouterr()
    assert "Runner permissions were not re-applied" in out.out + out.err


# --- L-b: relative PATH entries ----------------------------------------------------------------


def test_service_user_scripts_drop_relative_and_empty_path_entries(box: Box) -> None:
    snippet = rs.SAFE_PATH
    res = box.run(snippet + 'echo "$PATH"', PATH=f".::{box.bin}:rel/bin:/usr/bin:/bin:")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == f"{box.bin}:/usr/bin:/bin"
    for script in (access.project_permission_script(), access.notebooks_acl_script()):
        assert snippet in script
        assert script.index(snippet) < script.index("setfacl")


def test_notebooks_acl_runs_with_a_relative_path_entry(box: Box) -> None:
    (box.proj / "notebooks").mkdir()
    (box.proj / "notebooks" / "a.py").write_text("x")
    res = box.run(
        access.notebooks_acl_script(srv_dir=str(box.srv)),
        PATH=f".::{box.bin}:{os.environ['PATH']}",
    )
    assert res.returncode == 0, res.stderr
    assert any(c.startswith("setfacl") for c in box.calls())


# --- L-c: special files in pushed directories --------------------------------------------------


def test_pushed_directories_leave_out_fifos_and_keep_regular_files_and_links(
    tmp_path: Path,
) -> None:
    from dango.platform.cloud.rsync_transport import _normalised_copy

    src = tmp_path / "scripts"
    (src / "sub").mkdir(parents=True)
    (src / "a.py").write_text("x")
    (src / "sub" / "b.py").write_text("x")
    os.mkfifo(src / "pipe")
    os.symlink("a.py", src / "link")
    staged = _normalised_copy(src, tmp_path / "out")
    names = sorted(p.name for p in staged.rglob("*"))
    assert names == ["a.py", "b.py", "link", "sub"]
    assert (staged / "link").is_symlink()


# --- L-d: restart notice ------------------------------------------------------------------------


def test_status_tells_the_user_when_web_was_stopped_and_started_again() -> None:
    srv = RunnerServer(version="1.0.14", web_active=True)
    out = iso.enable(srv, [])
    assert out.startswith("Runner isolation enabled") and iso.RESTART_NOTE in out
    srv2 = RunnerServer(version="1.0.14", web_active=False)
    assert iso.RESTART_NOTE not in iso.enable(srv2, [])


# --- L-e: guard waits for setfacl ----------------------------------------------------------------


def test_guard_waits_for_setfacl() -> None:
    patterns = re.findall(r'pgrep -f "([^"]+)"', GUARD_ACTION)
    assert any(re.search(p, "setfacl -P -m u:dango-nb:rw ./a.py") for p in patterns)
    for p in patterns:
        assert not re.search(p, GUARD_ACTION)
