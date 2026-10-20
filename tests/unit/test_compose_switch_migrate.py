"""tests/unit/test_compose_switch_migrate.py

The Docker-access migration (helper install, pin, render, marker, group removal, process drop) and its
fallback, against a stateful fake server: order, idempotence and the marker/group invariant.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import compose_migrate as cm
from tests.factories.compose_switch_fakes import PROJECT_ID, FakeServer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _trusted_venv() -> Iterator[None]:
    with patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]):
        yield


ORDER = ["H1", "H2", "H3", "P1", "pin-project", "P2", "P3", "M", "G", "gpasswd", "T"]


def steps(srv: FakeServer) -> list[str]:
    return [e for e in srv.events if e in set(ORDER)]


def test_full_migration_runs_every_step_in_order() -> None:
    srv = FakeServer()
    warnings: list[str] = []
    state = cm.migrate_docker_access(srv, warnings, version="1.0.14")
    assert state == "helper"
    assert steps(srv) == ORDER
    assert srv.marker and not srv.in_group
    assert srv.pinned == PROJECT_ID
    assert warnings == []
    assert srv.record == '{"dango": "1.0.14", "docker_access": "helper"}'


def test_marker_is_written_only_after_the_helper_verified() -> None:
    srv = FakeServer()
    cm.migrate_docker_access(srv, [], version="1.0.14")
    assert srv.events.index("P3") < srv.events.index("M") < srv.events.index("G")


def test_render_runs_in_helper_mode_without_the_marker() -> None:
    srv = FakeServer()
    cm.migrate_docker_access(srv, [], version="1.0.14")
    render = next(c for c in srv.commands if "compose_exec render" in c)
    assert (
        "--helper" in render and "runuser -u dango" in render and "DANGO_CLOUD_MODE=true" in render
    )
    assert render.index("cd /") < render.index("runuser")


@pytest.mark.parametrize("failing", ["H1", "H2", "H3", "P1", "pin", "P2", "P3", "M"])
def test_a_failure_before_the_switch_keeps_the_old_working_state(failing: str) -> None:
    srv = FakeServer(fail={failing: 1})
    warnings: list[str] = []
    state = cm.migrate_docker_access(srv, warnings, version="1.0.14")
    assert state == "group"
    assert not srv.marker and srv.in_group
    assert "G" not in srv.events and "gpasswd" not in srv.events and "T" not in srv.events
    assert warnings and "group" in " ".join(warnings).lower()
    assert srv.record == '{"dango": "1.0.14", "docker_access": "group"}'


def test_failing_step_is_named_in_the_warning() -> None:
    srv = FakeServer(fail={"H3": 1})
    warnings: list[str] = []
    cm.migrate_docker_access(srv, warnings, version="1.0.14")
    assert any("H3 failed" in w for w in warnings)


def test_failure_on_a_migrated_server_gives_access_back() -> None:
    srv = FakeServer(marker=True, in_group=False, pinned=PROJECT_ID, fail={"P2": 1})
    assert cm.migrate_docker_access(srv, [], version="1.0.14") == "group"
    assert not srv.marker and srv.in_group


def test_group_removal_failure_leaves_both_paths_working() -> None:
    srv = FakeServer(fail={"G": 1})
    warnings: list[str] = []
    assert cm.migrate_docker_access(srv, warnings, version="1.0.14") == "migrating"
    assert srv.marker and srv.in_group
    assert "T" not in srv.events
    assert srv.record == '{"dango": "1.0.14", "docker_access": "migrating"}'
    assert warnings


def test_surviving_processes_leave_the_state_migrating() -> None:
    srv = FakeServer(remain=True)
    warnings: list[str] = []
    assert cm.migrate_docker_access(srv, warnings, version="1.0.14") == "migrating"
    assert srv.record == '{"dango": "1.0.14", "docker_access": "migrating"}'
    assert any("dango remote upgrade" in w for w in warnings)


def test_second_run_neither_pins_nor_removes_the_group_again() -> None:
    srv = FakeServer()
    cm.migrate_docker_access(srv, [], version="1.0.14")
    srv.events.clear()
    assert cm.migrate_docker_access(srv, [], version="1.0.14") == "helper"
    assert "pin-project" not in srv.events and "gpasswd" not in srv.events
    for again in ("H3", "P2", "P3"):  # re-run, in case the templates changed
        assert again in srv.events


def test_pin_to_another_id_is_refused_and_falls_back() -> None:
    srv = FakeServer(pinned="f" * 32)
    warnings: list[str] = []
    assert cm.migrate_docker_access(srv, warnings, version="1.0.14") == "group"
    assert "pin-project" not in srv.events
    assert any("pinned" in w for w in warnings)


def test_missing_project_id_falls_back() -> None:
    srv = FakeServer(project=False)
    warnings: list[str] = []
    assert cm.migrate_docker_access(srv, warnings, version="1.0.14") == "group"
    assert warnings


def test_managed_web_is_stopped_before_the_switch_and_started_after() -> None:
    srv = FakeServer()
    cm.migrate_docker_access(srv, [], version="1.0.14", manage_web=True)
    ev = srv.events
    assert ev.index("web-stop") > ev.index("M")
    assert ev.index("web-stop") < ev.index("G") < ev.index("T") < ev.index("web-start")


def test_managed_web_is_not_touched_when_the_migration_fails_early() -> None:
    srv = FakeServer(fail={"H2": 1})
    cm.migrate_docker_access(srv, [], version="1.0.14", manage_web=True)
    assert "web-stop" not in srv.events and "web-start" not in srv.events


def test_managed_web_is_started_even_when_the_process_drop_fails() -> None:
    srv = FakeServer(remain=True)
    cm.migrate_docker_access(srv, [], version="1.0.14", manage_web=True)
    assert "web-start" in srv.events


def test_every_step_reports_to_on_step() -> None:
    srv = FakeServer()
    seen: list[str] = []
    cm.migrate_docker_access(srv, [], version="1.0.14", on_step=lambda: seen.append("x"))
    assert len(seen) >= 5


def test_install_refuses_an_untrusted_venv() -> None:
    srv = FakeServer()
    warnings: list[str] = []
    with patch(
        "dango.platform.cloud.runtime_ownership.venv_trust_findings",
        return_value=["/srv/dango/venv"],
    ):
        assert cm.install_helper(srv, warnings) is False
    assert "H2" not in srv.events and warnings


def test_install_checks_the_required_tools_and_server_code() -> None:
    text = cm.prechecks_script()
    for tool in (
        "sudo",
        "flock",
        "visudo",
        "runuser",
        "loginctl",
        "timeout",
        "getent",
        "gpasswd",
        "pgrep",
        "pkill",
    ):
        assert tool in text
    assert "docker compose version" in text
    assert "dango.platform.cloud.server_compose" in text


def test_self_test_checks_ok_and_that_root_verbs_are_refused() -> None:
    text = cm.self_test_script()
    assert f"sudo -n {ch.HELPER_PATH} self-test" in text
    assert f"sudo -n {ch.HELPER_PATH} volume-path" in text
    assert "64" in text


def test_pin_project_is_root_only_and_unforced() -> None:
    srv = FakeServer()
    cm.migrate_docker_access(srv, [], version="1.0.14")
    pins = [c for c in srv.commands if f"{ch.HELPER_PATH} pin-project" in c]
    assert len(pins) == 1 and "sudo" not in pins[0] and "--replace" not in pins[0]
    assert pins[0].endswith(PROJECT_ID)


def test_consistency_check_refuses_odd_volumes_and_foreign_containers() -> None:
    srv = FakeServer()
    cm.migrate_docker_access(srv, [], version="1.0.14")
    p1 = next(c for c in srv.commands if "# step:P1" in c)
    assert "com.docker.compose.project.working_dir=/srv/dango/project" in p1
    assert "dango-01234567_metabase-data" in p1
    assert "com.dango.project_id" in p1
    assert "local" in p1 and ".Options" in p1
    assert "dango-01234567" in p1


def test_fallback_restores_group_and_records_it() -> None:
    srv = FakeServer(marker=True, in_group=False)
    warnings: list[str] = []
    cm.enter_fallback(srv, warnings, "1.0.14", "the sudoers rule was rejected")
    assert not srv.marker and srv.in_group
    assert any("sudoers rule was rejected" in w for w in warnings)
    assert srv.record == '{"dango": "1.0.14", "docker_access": "group"}'


def test_fallback_script_only_adds_the_group_when_missing() -> None:
    text = cm.fallback_script("1.0.14")
    assert f"rm -f {ch.MARKER}" in text
    assert text.index(f"rm -f {ch.MARKER}") < text.index("usermod -aG docker dango")
    assert "grep -qx docker" in text


def test_needs_migration_only_when_the_state_is_not_settled() -> None:
    assert cm.needs_migration(FakeServer(marker=False, in_group=True)) is True
    assert cm.needs_migration(FakeServer(marker=True, in_group=True)) is True
    settled = FakeServer(marker=True, in_group=False)
    settled.record = '{"dango": "1.0.14", "docker_access": "helper"}'
    assert cm.needs_migration(settled) is False
    unsettled = FakeServer(marker=True, in_group=False)
    unsettled.record = '{"dango": "1.0.14", "docker_access": "migrating"}'
    assert cm.needs_migration(unsettled) is True


def test_restore_runs_the_restore_script_and_reports_problems() -> None:
    srv = FakeServer(marker=True, in_group=False)
    warnings: list[str] = []
    cm.restore_docker_access(srv, warnings, version="1.0.13")
    assert "R" in srv.events and srv.in_group and not srv.marker
    assert srv.record == '{"dango": "1.0.13", "docker_access": "group"}'
    assert warnings == []


def test_restore_failure_raises() -> None:
    from dango.exceptions import CloudProvisioningError

    srv = FakeServer(marker=True, in_group=False, fail={"R": 1})
    with pytest.raises(CloudProvisioningError):
        cm.restore_docker_access(srv, [], version="1.0.13")
