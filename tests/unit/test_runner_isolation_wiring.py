"""tests/unit/test_runner_isolation_wiring.py

Server setup and ``remote upgrade`` run the runner-isolation steps (design L13): order on the main
and the at-target paths, the upgrade guard re-arm, idempotent re-runs, interrupted runs, the marker
last and never after a failed verify, fatal versus warning failures in the result and in setup,
the ownership interplay, rollback to a build without isolation and an old Marimo that is not adopted.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_provision as dp
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud import runner_scripts as scripts
from dango.platform.cloud import runtime_ownership as ro
from dango.platform.cloud import server_setup, upgrade
from dango.platform.cloud._server_templates import build_systemd_unit
from dango.platform.cloud.upgrade import upgrade_dango
from dango.platform.cloud.upgrade_guard import GUARD_ACTION
from tests.factories.runner_isolation_fakes import RunnerServer

pytestmark = pytest.mark.unit

STEPS = [s.name for s in ri.build_steps()]
FATAL = [s.name for s in ri.build_steps() if s.on_failure == "fatal"]
HELPER_RECORD = '{"dango": "1.0.14", "docker_access": "helper"}'


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    backup = MagicMock()
    backup.archive_path = "/srv/dango/backups/deploy/b.tar.gz"
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
        patch("dango.platform.cloud.backup.verify_health", return_value=True),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.14"),
    ):
        yield


def settled(
    version: str = "1.0.14",
    *,
    rfail: dict[str, int] | None = None,
    raise_on: dict[str, Exception] | None = None,
) -> RunnerServer:
    """A server already on the compose helper at *version*."""
    srv = RunnerServer(version=version, marker=True, in_group=False)
    srv.rfail = rfail or {}
    srv.raise_on = raise_on or {}
    srv.record = json.dumps({"dango": version, "docker_access": "helper"})
    return srv


def run(srv: RunnerServer, version: str, force: bool = False) -> Any:
    return upgrade_dango(srv, version=version, force=force)


def at(srv: RunnerServer, event: str, start: int = 0) -> int:
    return next(i for i, e in enumerate(srv.events) if i >= start and e == event)


# --- setup ------------------------------------------------------------------------------------


def test_setup_has_a_runner_isolation_step_after_the_compose_helper() -> None:
    names = [f.__name__ for f in server_setup._SETUP_STEPS]
    assert names[-2:] == ["_setup_compose_helper", "_setup_runner_isolation"]
    assert len(names) == 18


@pytest.mark.parametrize("ufw", [False, True])
def test_setup_runs_the_plan_in_order_with_the_marker_last(ufw: bool) -> None:
    """BYOS (``setup_ufw``) and DigitalOcean setup both reach the step."""
    srv = RunnerServer(project=False, in_group=False)
    result = server_setup.setup_server(srv, setup_ufw=ufw)
    assert srv.runner_events()[: len(STEPS)] == STEPS
    assert srv.runner_events()[-2:] == ["marker", "record"]
    assert "runner_isolation" in result.steps_completed
    assert json.loads(srv.isolation_marker or "")["loopback"] == "restricted"
    assert not [w for w in result.warnings if w.startswith(ri.ISOLATION_MARK)]


def test_setup_records_schema_and_loopback_in_the_server_record() -> None:
    srv = RunnerServer(project=False, in_group=False)
    server_setup.setup_server(srv)
    data = json.loads(srv.record or "")
    assert data["docker_access"] == "helper"
    assert data["runner_isolation"] == {"schema": 1, "loopback": "restricted"}


def test_setup_fatal_failure_warns_loudly_and_finishes() -> None:
    srv = RunnerServer(project=False, in_group=False, rfail={"sudoers": 4})
    result = server_setup.setup_server(srv)
    assert srv.isolation_marker is None
    assert "runner_isolation" in result.steps_completed
    loud = [w for w in result.warnings if w.startswith(ri.ISOLATION_MARK)]
    assert len(loud) == 1 and "sudoers" in loud[0] and "dango remote upgrade" in loud[0]
    end: list[str] = []
    dp._carry_fallback_warnings(result.warnings, end)
    assert end == loud


def test_setup_warning_level_failure_continues_with_unrestricted_loopback() -> None:
    srv = RunnerServer(project=False, in_group=False, rfail={"nft_rules": 6})
    result = server_setup.setup_server(srv)
    assert json.loads(srv.isolation_marker or "")["loopback"] == "unrestricted"
    assert json.loads(srv.record or "")["runner_isolation"]["loopback"] == "unrestricted"
    assert any("loopback: unrestricted" in w for w in result.warnings)
    assert not [w for w in result.warnings if w.startswith(ri.ISOLATION_MARK)]


# --- upgrade order (main path) ------------------------------------------------------------------


def test_main_path_runs_isolation_after_docker_access_and_before_the_rebuild() -> None:
    srv = RunnerServer(version="1.0.13")
    result = run(srv, "1.0.14")
    first, last = at(srv, f"r:{STEPS[0]}"), at(srv, "r:record")
    assert at(srv, "pip") < at(srv, "H1") < at(srv, "T") < first
    assert srv.runner_events() == [*STEPS, "record"]
    assert last < at(srv, "helper:pull") < srv.events.index("web-start", last)
    assert result.runner_isolation == "Runner isolation enabled"
    assert srv.isolation_marker is not None


def test_main_path_runs_after_the_ownership_script_and_re_applies_acls_every_time() -> None:
    srv = settled("1.0.14")
    run(srv, "1.0.15")
    own = next(i for i, c in enumerate(srv.commands) if "# compose_helper" in c)
    acl = next(i for i, c in enumerate(srv.commands) if "# runner-step:project_permissions" in c)
    assert own < acl
    assert srv.runner_events() == [*STEPS, "record"]


def test_unsupported_old_target_runs_nothing() -> None:
    srv = RunnerServer(version="1.0.12")
    result = run(srv, "1.0.13")
    assert srv.runner_events() == [] and result.runner_isolation is None


def test_guard_is_re_armed_after_every_step_on_the_main_path() -> None:
    srv = settled("1.0.14")
    run(srv, "1.0.15")
    seq = [e for e in srv.events if e.startswith("r:") or e == "guard-arm"]
    start = seq.index(f"r:{STEPS[0]}")
    for i, name in enumerate(STEPS):
        pos = start + 2 * i
        assert seq[pos] == f"r:{name}"
        assert seq[pos + 1] == "guard-arm", f"no re-arm after {name}"


# --- at-target path ---------------------------------------------------------------------------


def test_at_target_runs_isolation_after_secure_runtime_and_the_switch() -> None:
    srv = settled("1.0.14")
    result = run(srv, "1.0.14")
    own = next(i for i, c in enumerate(srv.commands) if "# compose_helper" in c)
    first = next(i for i, c in enumerate(srv.commands) if f"# runner-step:{STEPS[0]}" in c)
    assert own < first
    assert srv.runner_events() == [*STEPS, "record"]
    # nothing runs as a runner while permissions are re-applied: web is stopped for the steps and
    # back afterwards (the stop also ends a Marimo that was started before isolation)
    assert at(srv, "r:quiesce") < at(srv, "web-stop") < at(srv, "r:project_permissions")
    assert at(srv, "r:record") < at(srv, "web-start")
    assert srv.web_active
    assert result.runner_isolation == f"Runner isolation enabled; {ri.RESTART_NOTE}"
    assert result.new_version == "1.0.14"


def test_at_target_arms_a_guard_around_the_steps_and_disarms_it() -> None:
    srv = settled("1.0.14")
    run(srv, "1.0.14")
    first, last = at(srv, f"r:{STEPS[0]}"), at(srv, "r:record")
    assert "guard-arm" in srv.events[:first][-3:]
    assert "guard-disarm" in srv.events[last:]
    assert srv.events[first:last].count("guard-arm") == len(STEPS)


_DISABLED_LINE = (
    "Runner isolation: DISABLED by admin opt-out (run `dango remote isolation enable` to turn on)"
)
_DISABLED_WARNING = (
    "Runner isolation is installed but DISABLED by an admin opt-out (/srv/dango/runner/disabled): "
    "scripts and notebooks still run as the dango service account. Run 'dango remote isolation "
    "enable' to turn it on, or 'dango remote isolation status' for details."
)


@pytest.mark.parametrize("target", ["1.0.14", "1.0.15"])
def test_upgrade_reports_a_present_opt_out_instead_of_enabled(target: str) -> None:
    srv = settled("1.0.14")
    srv.disabled_marker = True
    result = run(srv, target)
    assert result.runner_isolation == _DISABLED_LINE
    assert _DISABLED_WARNING in result.warnings
    assert "Runner isolation enabled" not in (result.runner_isolation or "")


def test_upgrade_without_an_opt_out_still_says_enabled() -> None:
    srv = settled("1.0.14")
    result = run(srv, "1.0.14")
    assert (result.runner_isolation or "").startswith("Runner isolation enabled")
    assert _DISABLED_WARNING not in result.warnings


def test_opt_out_does_not_hide_a_failed_run() -> None:
    srv = settled("1.0.14", rfail={"verify": 1})
    srv.disabled_marker = True
    result = run(srv, "1.0.14")
    assert (result.runner_isolation or "").startswith(ri.ISOLATION_MARK)


def test_at_target_below_the_helper_release_runs_nothing() -> None:
    srv = RunnerServer(version="1.0.13")
    result = run(srv, "1.0.13")
    assert srv.runner_events() == [] and result.runner_isolation is None


def test_guard_waits_for_the_new_long_running_commands() -> None:
    patterns = re.findall(r'pgrep -f "([^"]+)"', GUARD_ACTION)
    for command in ("apt-get install -y -qq acl", "useradd -r dango-run", "visudo -cf x"):
        assert any(re.search(p, command) for p in patterns), command
    assert any(re.search(p, "/usr/sbin/nft -f /etc/x") for p in patterns)
    for p in patterns:  # the waiting shell must not match its own command line
        assert not re.search(p, GUARD_ACTION)


# --- idempotence, interruption, policy ---------------------------------------------------------


def test_rerun_runs_the_same_commands_and_ends_in_the_same_state() -> None:
    srv = settled("1.0.14")
    run(srv, "1.0.14")
    first_cmds, first_marker, first_record = (
        list(srv.runner_commands),
        srv.isolation_marker,
        srv.record,
    )
    srv.runner_commands.clear()
    run(srv, "1.0.14")
    assert srv.runner_commands == first_cmds
    assert (srv.isolation_marker, srv.record) == (first_marker, first_record)


@pytest.mark.parametrize("failing", FATAL)
def test_interrupted_run_leaves_no_marker_and_the_rerun_converges(failing: str) -> None:
    srv = settled("1.0.14", rfail={failing: 1})
    result = run(srv, "1.0.14")
    ran = srv.runner_events()
    # the record is brought in line with the (missing) marker, except under an untrusted /srv/dango
    tail = [] if failing == "ownership_check" else ["record"]
    assert ran == [*STEPS[: STEPS.index(failing) + 1], *tail]
    assert srv.isolation_marker is None
    assert ("marker" in ran) == (failing == "marker")
    assert result.runner_isolation and result.runner_isolation.startswith(ri.ISOLATION_MARK)
    assert failing in result.runner_isolation
    assert [w for w in result.warnings if w.startswith(ri.ISOLATION_MARK)]
    assert result.health_check_passed
    srv.rfail.clear()
    srv.events.clear()
    again = run(srv, "1.0.14")
    assert srv.runner_events() == [*STEPS, "record"]
    assert srv.isolation_marker is not None
    assert again.runner_isolation == f"Runner isolation enabled; {ri.RESTART_NOTE}"


def test_failed_verify_never_writes_the_marker() -> None:
    srv = settled("1.0.14", rfail={"verify": 1})
    run(srv, "1.0.14")
    assert "marker" not in srv.runner_events() and srv.isolation_marker is None


def test_warning_level_failure_is_reported_as_enabled_with_unrestricted_loopback() -> None:
    srv = settled("1.0.14", rfail={"nftables_package": 6})
    result = run(srv, "1.0.14")
    assert result.runner_isolation == (
        f"Runner isolation enabled (loopback: unrestricted); {ri.RESTART_NOTE}"
    )
    assert "nft_rules" not in srv.runner_events()
    assert any("nftables package" in w for w in result.warnings)
    assert json.loads(srv.record or "")["runner_isolation"]["loopback"] == "unrestricted"


def test_acl_package_failure_alone_does_not_block() -> None:
    srv = settled("1.0.14", rfail={"acl_package": 6})
    result = run(srv, "1.0.14")
    assert srv.isolation_marker is not None
    assert result.runner_isolation == f"Runner isolation enabled; {ri.RESTART_NOTE}"


def test_a_dropped_connection_in_a_step_is_a_failed_step() -> None:
    from dango.exceptions import CloudSSHError

    srv = settled("1.0.14", raise_on={"# runner-step:users": CloudSSHError("gone")})
    result = run(srv, "1.0.14")
    assert srv.isolation_marker is None
    assert result.runner_isolation and "users" in result.runner_isolation


def test_record_script_is_valid_python_and_names_only_the_marker_and_the_record() -> None:
    text = ri.record_script("/srv/dango")
    assert "/srv/dango/runner/active" in text and "/srv/dango/server-version.json" in text
    compile(ri.record_python(), "record", "exec")
    assert "secret" not in text.lower() and "password" not in text.lower()


# --- ownership interplay -------------------------------------------------------------------------


def test_ownership_script_never_touches_the_runner_tree_users_sudoers_or_marker() -> None:
    script = ro.build_ownership_script(cm.ownership_steps())
    for needle in (
        "/srv/dango/runner",
        scripts.SUDOERS_PATH,
        scripts.NFT_PATH,
        "dango-run",
        "dango-nb",
        "setfacl -b",
        "setfacl -R",
    ):
        assert needle not in script, needle
    for line in script.splitlines():
        if "-R" in line.split():
            assert "/srv/dango/runner" not in line
            assert not line.rstrip().endswith(" /srv/dango"), line
    assert "setfacl -k /srv/dango" in script and "setfacl -k /srv/dango/" not in script


def test_secure_runtime_after_isolation_leaves_runner_state_intact() -> None:
    srv = settled("1.0.14")
    ri.enable(srv, [])
    before = (set(srv.done), srv.isolation_marker, srv.record)
    ro.secure_runtime(
        srv, [], installed_version="1.0.14", strict=True, extra_steps=cm.ownership_steps()
    )
    assert (set(srv.done), srv.isolation_marker, srv.record) == before
    assert srv.runner_events().count("marker") == 1


def test_ownership_steps_do_not_target_runner_paths() -> None:
    steps = [*ro.ownership_steps(), *cm.ownership_steps()]
    assert all("runner" not in s.script for s in steps)


# --- rollback, running notebooks, unit file -------------------------------------------------------


def test_downgrade_to_a_build_without_isolation_leaves_a_working_server() -> None:
    """Rollback below 1.0.14 (before the restricted users).

    Assumption: the older code ignores the users, the sudoers file, the ACLs and the rules, because
    the ACLs only deny the runner users and the older code runs everything as ``dango``.  Only the
    marker is taken away, so that nothing claims isolation the older code does not enforce; no
    user, file or rule is removed, and the upgrade to 1.0.14 writes the marker again.
    """
    srv = settled("1.0.14")
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    result = run(srv, "1.0.13", force=True)
    assert "marker" not in srv.runner_events()
    assert srv.isolation_marker is None
    assert "not enforced" in (result.runner_isolation or "").lower()
    destructive = ("userdel", "setfacl -b", "dango-runner", "nft delete")
    assert not [c for c in srv.commands if any(d in c for d in destructive)]
    assert result.health_check_passed and "web-start" in srv.events


def test_a_marimo_started_before_the_upgrade_is_not_adopted(tmp_path: Path) -> None:
    from dango.notebooks import manager, marimo_process

    pid_file = tmp_path / "marimo.pid"
    pid_file.write_text(json.dumps({"pid": 4242, "start_time": 1.0}))
    assert not marimo_process.is_runner_record(pid_file)
    with (
        patch.object(manager, "get_marimo_pid_file_path", return_value=pid_file),
        patch.object(manager, "is_process_running", return_value=True),
        patch.object(manager, "_stop_marimo_process", return_value="stopped") as legacy,
        patch.object(manager, "_stop_runner_marimo") as runner,
        patch.object(manager, "remove_token_file"),
    ):
        assert manager.stop_marimo(tmp_path) is True
    legacy.assert_called_once()
    runner.assert_not_called()


def test_the_web_unit_keeps_privilege_escalation_for_sudo() -> None:
    unit = build_systemd_unit(workers=None)
    assert "NoNewPrivileges" not in unit


def test_modules_stay_within_the_size_limit() -> None:
    assert len(inspect.getsource(upgrade).splitlines()) <= 500
    assert len(inspect.getsource(ri).splitlines()) <= 500
