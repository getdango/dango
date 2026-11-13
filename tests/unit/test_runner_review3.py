"""tests/unit/test_runner_review3.py

Round-3 review fixes for the runner isolation wiring.

H-1 the notebook kernel's loopback connection, H-2 no ssh login for the runners and no user
manager, L-1 one service start on the deploy paths, L-2 planted links never block a re-apply.
Shell text is run under dash against a temp tree with fake tools.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from dango.cli.commands import deploy_provision as dp
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as iso
from dango.platform.cloud import runner_persistence as rp
from dango.platform.cloud import runner_reapply
from dango.platform.cloud import runner_scripts as rs
from tests.unit.test_runner_hardening import Box
from tests.unit.test_runner_isolation_reapply import settled
from tests.unit.test_runner_review2 import _check_run, _clean_state, _paths

pytestmark = pytest.mark.unit

NB_EPHEMERAL = (
    'meta skuid "dango-nb" ip daddr 127.0.0.1 tcp dport 32768-60999'
    " tcp dport != { 80, 443, 2019, 3000, 8080, 8081, 8800 } accept"
)


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# --- H-1: the notebook's kernel connects back over loopback ------------------------------------


def _rules() -> list[str]:
    return [ln.strip() for ln in rs.NFT_RULESET.splitlines()]


def test_notebook_user_may_connect_to_loopback_ephemeral_ports_before_the_rejects() -> None:
    lines = _rules()
    assert NB_EPHEMERAL in lines
    at = lines.index(NB_EPHEMERAL)
    generic = [i for i, ln in enumerate(lines) if ln.endswith(("ip daddr 127.0.0.0/8 reject",))]
    local = [i for i, ln in enumerate(lines) if "fib daddr type local reject" in ln]
    assert generic and local and at < min(generic) and at < min(local)
    assert lines.index('meta skuid "dango-nb" ip daddr 127.0.0.1 tcp dport 7805 accept') < at


def test_script_user_gets_no_loopback_accept_except_dns() -> None:
    for ln in _rules():
        if ln.endswith("accept") and "dango-run" in ln:
            assert "dport 53" in ln, ln
    assert not [ln for ln in _rules() if "dango-run" in ln and "32768" in ln]


def test_ephemeral_range_leaves_the_fixed_dango_ports_blocked() -> None:
    assert not any(32768 <= port <= 60999 for port in (80, 443, 3000, 7805, 8080, 8081, 8800))
    text = rs.NFT_RULESET
    assert "fixed ports" in text and "kernel" in text


# --- H-2: no ssh login for the runners, no user manager -----------------------------------------

CONF = "dango-runner.conf"


def _ssh_tools(box: Box, *, sshd_rc: int = 0, effective: bool = True) -> None:
    box.tool("id", "echo 1001\n")
    box.tool("chown", 'echo "chown $*" >>"$LOG"\n')
    box.tool(
        "sshd",
        f'echo "sshd $*" >>"$LOG"\ncase "$1" in -t) exit {sshd_rc};; esac\n'
        + ('cat "$SSHD_DIR"/*.conf 2>/dev/null | tr A-Z a-z\n' if effective else "exit 0\n"),
    )
    box.tool(
        "systemctl", 'echo "systemctl $*" >>"$LOG"\ncase "$2" in ssh) exit "${SSH_RC:-0}";; esac\n'
    )


def _run_ssh(box: Box, d: Path, **env: str):
    return box.run(rp.ssh_deny_script(str(d)), SSHD_DIR=str(d), **env)


def test_ssh_drop_in_denies_only_the_two_runners_atomically(box: Box) -> None:
    d = box.root / "sshd_config.d"
    d.mkdir()
    _ssh_tools(box)
    res = _run_ssh(box, d)
    assert res.returncode == 0, res.stderr
    conf = d / CONF
    assert conf.read_text().splitlines()[-1] == "DenyUsers dango-run dango-nb"
    assert [ln for ln in conf.read_text().splitlines() if ln and not ln.startswith("#")] == [
        "DenyUsers dango-run dango-nb"
    ]
    assert oct(conf.stat().st_mode & 0o777) == "0o644"
    assert "chown root:root" in " ".join(box.calls())
    assert sorted(p.name for p in d.iterdir()) == [CONF]


def test_ssh_config_is_validated_before_reload_and_effect_is_checked() -> None:
    text = rp.ssh_deny_script()
    assert text.index("-t") < text.index("reload") < text.index("-T")
    assert "user=$u,host=localhost,addr=127.0.0.1" in text
    assert "for u in dango-run dango-nb; do" in text
    assert "systemctl reload ssh || systemctl reload sshd" in text


def test_ssh_validation_order_in_a_real_run(box: Box) -> None:
    d = box.root / "sshd_config.d"
    d.mkdir()
    _ssh_tools(box)
    assert _run_ssh(box, d).returncode == 0
    calls = box.calls()
    t = next(i for i, c in enumerate(calls) if c.startswith("sshd -t"))
    r = next(i for i, c in enumerate(calls) if c.startswith("systemctl reload ssh"))
    e = next(i for i, c in enumerate(calls) if c.startswith("sshd -T"))
    assert t < r < e


def test_ssh_failed_validation_restores_the_old_file_and_never_reloads(box: Box) -> None:
    d = box.root / "sshd_config.d"
    d.mkdir()
    (d / CONF).write_text("# old\n")
    _ssh_tools(box, sshd_rc=1)
    res = _run_ssh(box, d)
    assert res.returncode != 0
    assert (d / CONF).read_text() == "# old\n"
    assert not any("reload" in c for c in box.calls())
    d2 = box.root / "d2"
    d2.mkdir()
    res = _run_ssh(box, d2)
    assert res.returncode != 0 and not (d2 / CONF).exists()


def test_ssh_reload_falls_back_to_the_other_service_name(box: Box) -> None:
    d = box.root / "sshd_config.d"
    d.mkdir()
    _ssh_tools(box)
    box.tool("systemctl", 'echo "systemctl $*" >>"$LOG"\n[ "$2" != ssh ] || exit 5\n')
    res = _run_ssh(box, d)
    assert res.returncode == 0, res.stderr
    assert "systemctl reload sshd" in box.calls()


def test_ssh_drop_in_that_is_not_effective_is_a_fatal_failure(box: Box) -> None:
    d = box.root / "sshd_config.d"
    d.mkdir()
    _ssh_tools(box, effective=False)
    res = _run_ssh(box, d)
    assert res.returncode != 0
    assert "NOT enabled" in res.stderr and "Include" in res.stderr


def test_ssh_drop_in_masks_the_user_manager_of_both_runners_and_is_idempotent(box: Box) -> None:
    d = box.root / "sshd_config.d"
    d.mkdir()
    _ssh_tools(box)
    assert _run_ssh(box, d).returncode == 0
    assert _run_ssh(box, d).returncode == 0
    calls = box.calls()
    assert calls.count("systemctl mask user@1001.service") == 2 * 2
    assert "systemctl stop user@1001.service" in calls
    assert (d / CONF).read_text().count("DenyUsers") == 1


def test_ssh_script_refuses_a_symlinked_drop_in(box: Box) -> None:
    d = box.root / "sshd_config.d"
    d.mkdir()
    target = box.root / "target"
    target.write_text("keep\n")
    (d / CONF).symlink_to(target)
    _ssh_tools(box)
    assert _run_ssh(box, d).returncode != 0
    assert target.read_text() == "keep\n"


def test_persistence_step_installs_the_ssh_denial_as_root() -> None:
    step = next(s for s in iso.build_steps() if s.name == "persistence")
    assert "DenyUsers" in step.script and "cron.deny" in step.script
    assert step.run_as == "root" and step.on_failure == "fatal"
    assert "/srv/dango/project" not in step.script


def test_ssh_script_names_no_dango_writable_path() -> None:
    text = rp.ssh_deny_script()
    assert "/srv/dango" not in text and "/home" not in text


def _verify(box: Box, etc: Path):
    return _check_run(box, etc, _paths(box))


def test_verify_passes_with_ssh_denied_and_user_manager_masked(box: Box) -> None:
    etc, home = _clean_state(box)
    assert _verify(box, etc).returncode == 0


def test_verify_fails_when_sshd_does_not_deny_a_runner(box: Box) -> None:
    etc, home = _clean_state(box)
    box.tool("sshd", 'echo "denyusers dango-run"\n')
    res = _verify(box, etc)
    assert res.returncode == 1 and "ssh" in res.stderr and "dango-nb" in res.stderr


def test_verify_asks_sshd_about_each_runner(box: Box) -> None:
    etc, home = _clean_state(box)
    box.tool("sshd", 'echo "sshd $*" >>"$LOG"\necho "denyusers dango-run dango-nb"\n')
    assert _verify(box, etc).returncode == 0
    calls = [c for c in box.calls() if c.startswith("sshd -T")]
    assert any("user=dango-run,host=localhost,addr=127.0.0.1" in c for c in calls)
    assert any("user=dango-nb,host=localhost,addr=127.0.0.1" in c for c in calls)


def test_verify_fails_when_the_user_manager_is_not_masked(box: Box) -> None:
    etc, home = _clean_state(box)
    box.tool("systemctl", 'case "$1" in is-enabled) echo enabled;; esac\nexit 0\n')
    res = _verify(box, etc)
    assert res.returncode == 1 and "user@1001.service" in res.stderr


def test_verify_fails_on_a_runner_ssh_directory_or_link(box: Box) -> None:
    etc, home = _clean_state(box)
    (home / ".ssh").mkdir()
    res = _verify(box, etc)
    assert res.returncode == 1 and ".ssh" in res.stderr
    (home / ".ssh").rmdir()
    (home / ".ssh").symlink_to(box.root / "nowhere")
    assert ".ssh" in _verify(box, etc).stderr


def test_quiesce_removes_the_ssh_directory_and_stops_the_user_manager(box: Box) -> None:
    home = box.root / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "authorized_keys").write_text("key")
    other = box.root / "other"
    other.mkdir()
    (other / "keep").write_text("x")
    _quiesce_tools(box, home)
    res = box.run(access.quiesce_script(stop_web=False, paths=_paths(box)))
    assert res.returncode == 0, res.stderr
    assert not (home / ".ssh").exists()
    assert "systemctl stop user@1001.service" in box.calls()
    (home / ".ssh").symlink_to(other)
    assert box.run(access.quiesce_script(stop_web=False, paths=_paths(box))).returncode == 0
    assert not (home / ".ssh").is_symlink() and (other / "keep").exists()


# --- L-2: a planted link in a runner home never blocks a re-apply -------------------------------


def _quiesce_tools(box: Box, home: Path) -> None:
    box.tool("getent", f'echo "$2:x:1001:1001::{home}:/usr/sbin/nologin"\n')
    for name in ("crontab", "loginctl", "atrm"):
        box.tool(name, f'echo "{name} $*" >>"$LOG"\n')
    box.tool("atq", "exit 0\n")
    box.tool("id", "echo 1001\n")
    box.tool("pgrep", "exit 1\n")
    box.tool("pkill", "exit 0\n")
    box.tool("sleep", "exit 0\n")
    box.tool("sshd", 'echo "denyusers dango-run dango-nb"\n')
    box.tool(
        "systemctl",
        'echo "systemctl $*" >>"$LOG"\ncase "$1" in is-enabled) echo masked;; esac\nexit 0\n',
    )


def _reapply_cycle(box: Box, home: Path) -> None:
    _quiesce_tools(box, home)
    etc = box.root / "etc"
    etc.mkdir(exist_ok=True)
    for name in ("cron.deny", "at.deny"):
        (etc / name).write_text("dango-run\ndango-nb\n")
    box.tool("runuser", 'shift 3\n"$@"\n')
    box.tool("at", "cat >/dev/null\nexit 1\n")
    box.tool("crontab", 'echo "You (u) are not allowed to use this program" >&2\nexit 1\n')
    first = box.run(access.quiesce_script(stop_web=False, paths=_paths(box)))
    assert first.returncode == 0, first.stderr
    assert _check_run(box, etc, _paths(box)).returncode == 0
    again = box.run(access.quiesce_script(stop_web=False, paths=_paths(box)))
    assert again.returncode == 0


@pytest.mark.parametrize("link", [".config", ".local", ".local/share"])
def test_a_planted_directory_link_is_removed_and_the_next_verify_passes(
    box: Box, link: str
) -> None:
    home = box.root / "home"
    (home / Path(link).parent).mkdir(parents=True)
    victim = box.root / "victim"
    (victim / "systemd" / "user").mkdir(parents=True)
    (victim / "systemd" / "user" / "evil.service").write_text("x")
    (victim / "autostart").mkdir()
    (home / link).symlink_to(victim)
    _reapply_cycle(box, home)
    assert not (home / link).is_symlink()
    assert (victim / "systemd" / "user" / "evil.service").exists()
    assert (victim / "autostart").is_dir()


def test_a_dangling_config_link_is_removed_and_the_next_verify_passes(box: Box) -> None:
    home = box.root / "home"
    home.mkdir()
    (home / ".config").symlink_to(box.root / "does-not-exist")
    _reapply_cycle(box, home)
    assert not (home / ".config").is_symlink()


def test_verify_alone_does_not_follow_a_config_link(box: Box) -> None:
    etc, home = _clean_state(box)
    victim = box.root / "victim"
    (victim / "systemd").mkdir(parents=True)
    (home / ".config").symlink_to(victim)
    res = _verify(box, etc)
    assert res.returncode == 1 and ".config" in res.stderr
    assert (victim / "systemd").is_dir()


# --- L-1: one service start on the deploy paths ---------------------------------------------------


def _marked(version: str = "1.0.14"):
    srv = settled(version)
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    return srv


def test_reapply_can_leave_web_stopped_for_the_caller_to_start() -> None:
    srv = _marked()
    runner_reapply.reapply(srv, [], stop_web=True, restart=False)
    assert "web-stop" in srv.events and "web-start" not in srv.events
    assert not srv.web_active


def test_reapply_restarts_by_default() -> None:
    srv = _marked()
    runner_reapply.reapply(srv, [], stop_web=True)
    assert srv.web_active


def test_both_deploy_paths_leave_the_start_to_the_metabase_trigger() -> None:
    for fn in (dp.run_provisioning, dp.run_byos_setup):
        text = inspect.getsource(fn)
        call = re.search(r"runner_reapply\.reapply\(([^)]*)\)", text)
        assert call and "restart=False" in call.group(1), fn.__name__
        assert text.index("restart=False") < text.index("_trigger_metabase_setup(")
