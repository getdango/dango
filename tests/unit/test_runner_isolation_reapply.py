"""tests/unit/test_runner_isolation_reapply.py

Review-round behaviour of the runner-isolation wiring: the ownership check first, nothing runs as a
runner while permissions are applied, the build gate, failure wording and the version record, and the
re-apply of the permission steps after anything that can recreate protected paths (push, restore,
deploy).
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_provision as dp
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud import runner_scripts as scripts
from dango.platform.cloud import runtime_ownership as ro
from dango.platform.cloud import server_setup
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.runner_isolation_fakes import RunnerServer

pytestmark = pytest.mark.unit

STEPS = [s.name for s in ri.build_steps()]


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
) -> RunnerServer:
    """A server already on the compose helper at *version*."""
    srv = RunnerServer(version=version, marker=True, in_group=False)
    srv.rfail = rfail or {}
    srv.record = json.dumps({"dango": version, "docker_access": "helper"})
    return srv


def run(srv: RunnerServer, version: str) -> Any:
    return upgrade_dango(srv, version=version)


def at(srv: RunnerServer, event: str, start: int = 0) -> int:
    return next(i for i, e in enumerate(srv.events) if i >= start and e == event)


def test_the_ownership_check_is_the_first_step_and_a_failure_stops_everything() -> None:
    """M2: a failed non-strict ``secure_runtime`` (BYOS) must not let root work under ``/srv/dango``."""
    assert STEPS[:2] == ["ownership_check", "quiesce"]
    srv = settled("1.0.14", rfail={"ownership_check": 7})
    with patch.object(ro, "secure_runtime", return_value="Runtime ownership step failed (warning)"):
        result = run(srv, "1.0.14")
    assert srv.runner_events() == ["ownership_check"]
    assert srv.isolation_marker is None
    assert result.runner_isolation and result.runner_isolation.startswith(ri.ISOLATION_MARK)
    assert not [c for c in srv.runner_commands if "useradd" in c or "install -d" in c]


def test_setup_ownership_failure_is_fatal_for_isolation_before_any_root_mutation() -> None:
    srv = RunnerServer(project=False, in_group=False, rfail={"ownership_check": 7})
    result = server_setup.setup_server(srv)
    assert srv.runner_events() == ["ownership_check"]
    assert [w for w in result.warnings if w.startswith(ri.ISOLATION_MARK)]


def test_web_is_restarted_even_when_a_step_fails() -> None:
    srv = settled("1.0.14", rfail={"sudoers": 4})
    run(srv, "1.0.14")
    assert srv.web_active and "web-start" in srv.events


def test_main_path_does_not_start_web_early() -> None:
    srv = settled("1.0.14")
    run(srv, "1.0.15")
    stop = at(srv, "web-stop")
    assert not [e for e in srv.events[stop : at(srv, "r:record")] if e == "web-start"]


def test_a_build_without_isolation_is_not_enabled_and_runs_nothing() -> None:
    """Setup that installed a build lacking ``isolated_run`` must not set up users and then fail verify."""
    srv = RunnerServer(project=False, in_group=False)
    srv.build_present = False
    result = server_setup.setup_server(srv)
    assert srv.runner_events() == []
    loud = [w for w in result.warnings if w.startswith(ri.ISOLATION_MARK)]
    assert len(loud) == 1 and "does not include" in loud[0]


def test_upgrade_to_a_build_without_isolation_runs_nothing() -> None:
    srv = settled("1.0.14")
    srv.build_present = False
    result = run(srv, "1.0.14")
    assert srv.runner_events() == []
    assert result.runner_isolation and result.runner_isolation.startswith(ri.ISOLATION_MARK)


def test_failure_wording_follows_the_marker_state() -> None:
    srv = settled("1.0.14", rfail={"users": 5})
    result = run(srv, "1.0.14")
    text = next(w for w in result.warnings if w.startswith(ri.ISOLATION_MARK))
    assert "keep running as the service account" in text
    srv = settled("1.0.14", rfail={"users": 5})
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    result = run(srv, "1.0.14")
    text = next(w for w in result.warnings if w.startswith(ri.ISOLATION_MARK))
    assert "keep running as the service account" not in text
    assert "earlier" in text and "isolated" in text


def test_a_failed_run_without_a_marker_drops_the_stale_record_key() -> None:
    srv = settled("1.0.14", rfail={"users": 5})
    srv.record = json.dumps(
        {"dango": "1.0.14", "docker_access": "helper", "runner_isolation": {"schema": 1}}
    )
    run(srv, "1.0.14")
    assert "runner_isolation" not in json.loads(srv.record or "")
    assert json.loads(srv.record or "")["docker_access"] == "helper"


def test_record_script_runs_python_isolated_from_root() -> None:
    text = ri.record_script("/srv/dango")
    assert text.startswith("cd / && python3 -I -c ")


def test_record_python_merges_or_drops_the_key(tmp_path: Path) -> None:
    import subprocess
    import sys

    record = tmp_path / "server-version.json"
    marker = tmp_path / "active"
    record.write_text(json.dumps({"dango": "1", "runner_isolation": {"schema": 0}}))
    code = ri.record_python(trusted_uid=os.getuid())

    def run_py() -> None:
        subprocess.run([sys.executable, "-I", "-c", code, str(marker), str(record)], check=True)

    run_py()
    assert "runner_isolation" not in json.loads(record.read_text())
    marker.write_text(json.dumps({"schema": 1, "loopback": "restricted"}))
    run_py()
    assert json.loads(record.read_text())["runner_isolation"] == {
        "schema": 1,
        "loopback": "restricted",
    }
    assert json.loads(record.read_text())["dango"] == "1"


def test_package_script_refreshes_the_lists_once_before_installing() -> None:
    text = scripts.package_script("acl", "setfacl")
    assert text.count("apt-get update") == 1
    assert text.index("apt-get update") < text.index("apt-get install")
    assert text.count("DPkg::Lock::Timeout=120") == 2
    assert text.index("command -v") < text.index("apt-get update")


def test_the_firewall_unit_loads_early_and_without_default_dependencies() -> None:
    unit = scripts.NFT_UNIT
    for line in (
        "DefaultDependencies=no",
        "Wants=network-pre.target",
        "Before=network-pre.target",
        "Conflicts=shutdown.target",
    ):
        assert line in unit, line
    assert "NoNewPrivileges" not in unit


# --- the permission steps are re-applied after anything that can recreate protected paths -----------

REAPPLY = ["quiesce", "project_permissions", "notebooks_acl", "verify"]


def test_reapply_runs_the_permission_steps_only_when_isolation_is_on() -> None:
    from dango.platform.cloud import runner_reapply

    srv = settled("1.0.14")
    warnings: list[str] = []
    assert runner_reapply.reapply(srv, warnings) is None
    assert srv.runner_events() == []
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    runner_reapply.reapply(srv, warnings)
    assert srv.runner_events() == REAPPLY and warnings == []


def test_reapply_failure_is_a_loud_warning_and_web_comes_back() -> None:
    from dango.platform.cloud import runner_reapply

    srv = settled("1.0.14", rfail={"notebooks_acl": 1})
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    warnings: list[str] = []
    runner_reapply.reapply(srv, warnings, stop_web=True)
    assert srv.runner_events() == REAPPLY[:3]
    assert warnings and warnings[0].startswith(runner_reapply.REAPPLY_MARK)
    assert srv.web_active


def test_reapply_with_stop_web_stops_first_and_restarts_after() -> None:
    from dango.platform.cloud import runner_reapply

    srv = settled("1.0.14")
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    runner_reapply.reapply(srv, [], stop_web=True)
    assert at(srv, "web-stop") < at(srv, "r:project_permissions") < at(srv, "web-start")
    srv2 = settled("1.0.14")
    srv2.isolation_marker = srv.isolation_marker
    runner_reapply.reapply(srv2, [], stop_web=False)
    assert "web-stop" not in srv2.events and "web-start" not in srv2.events
    assert "systemctl stop dango-web" not in srv2.runner_commands[0]


def push_with(srv: RunnerServer, tmp_path: Path) -> Any:
    from dango.platform.cloud import deployer
    from dango.platform.cloud.file_sync import SyncResult

    sync = SyncResult(synced_files=["scripts/"], dry_run=False)
    backup = MagicMock(warnings=[], archive_path="/b.tar.gz")
    with (
        patch("dango.platform.cloud.file_sync.sync_project_files", return_value=sync),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
        patch("dango.platform.cloud.pushed_requirements.install_pushed_requirements"),
    ):
        return deployer.push_deploy(srv, tmp_path, "10.0.0.1")


def test_push_reapplies_permissions_after_the_files_are_synced_and_owned(tmp_path: Path) -> None:
    srv = settled("1.0.14")
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    push_with(srv, tmp_path)
    chown = next(i for i, c in enumerate(srv.commands) if "chown -R dango:dango" in c)
    perm = next(i for i, c in enumerate(srv.commands) if "# runner-step:project_permissions" in c)
    assert chown < perm
    assert srv.runner_events() == REAPPLY
    # services were stopped for the push, so the re-apply does not start them early
    assert at(srv, "web-start") > at(srv, "r:verify")


def test_push_without_isolation_runs_no_runner_step(tmp_path: Path) -> None:
    srv = settled("1.0.14")
    push_with(srv, tmp_path)
    assert srv.runner_events() == []


def test_archive_restore_reapplies_permissions_before_services_start() -> None:
    from dango.platform.cloud import backup

    srv = settled("1.0.14")
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    with (
        patch("dango.platform.cloud.backup.extract_checked_archive"),
        patch(
            "dango.platform.cloud.backup.create_backup", return_value=MagicMock(archive_path="/s")
        ),
        patch("dango.platform.cloud.backup._remove_leftovers"),
        patch("dango.platform.cloud.backup.verify_health", return_value=True),
    ):
        backup.restore_from_archive(srv, "/srv/dango/backups/deploy/a.tar.gz")
    assert srv.runner_events() == REAPPLY
    chown = next(
        i for i, c in enumerate(srv.commands) if c == "chown -R dango:dango /srv/dango/project"
    )
    perm = next(i for i, c in enumerate(srv.commands) if "# runner-step:project_permissions" in c)
    assert chown < perm
    assert at(srv, "r:verify") < len(srv.events) - 1 - srv.events[::-1].index("web-start")


def test_spaces_restore_reapplies_permissions_while_services_are_stopped(tmp_path: Path) -> None:
    from tests.factories.backup_shell import run_spaces_restore

    project = tmp_path / "project"
    project.mkdir()
    top = "backup-20260101-000000"
    (tmp_path / top / ".dango").mkdir(parents=True)
    (tmp_path / top / ".dango" / "auth.db").write_text("x")
    events: list[str] = []

    def fake(*_a: Any, **_k: Any) -> list[str]:
        events.append("reapply")
        return []

    with patch("dango.platform.cloud.runner_reapply.reapply_local", side_effect=fake):
        run_spaces_restore(tmp_path, project, top, events=events)
    assert events.index("stop") < events.index("reapply") < events.index("start")


def test_deploy_provisioning_reapplies_permissions_before_the_trigger_on_both_paths() -> None:
    for fn in (dp.run_provisioning, dp.run_byos_setup):
        text = inspect.getsource(fn)
        assert "runner_reapply" in text, fn.__name__
        assert text.index("runner_reapply") < text.index("_trigger_metabase_setup(")
        assert text.index("_trigger_metabase_setup(") < text.index("_health_check(")
