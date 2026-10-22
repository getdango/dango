"""tests/unit/test_compose_switch_keep.py

A server whose compose helper marker already existed never gets the docker group back from a failing
step, whichever command ran it; plus the guard re-arm, the at-target restore order and the restore
decision for unreadable or fallback servers.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud import server_setup
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit

LEGACY = "cd /srv/dango/project && sudo -u dango docker compose build"
HELPER = '{"dango": "1.0.14", "docker_access": "helper"}'


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


def settled(**kw: Any) -> FakeServer:
    srv = FakeServer(marker=True, in_group=False, **kw)
    srv.record = HELPER
    return srv


def assert_kept(srv: FakeServer, warnings: list[str]) -> None:
    assert srv.marker and not srv.in_group
    assert "FB" not in srv.events and "usermod" not in srv.events
    assert any("marker is kept" in w for w in warnings), warnings


# --- the three other fallback callers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "fail", [{"P1": 1}, {"pin": 1}, {"P2": 65}, {"P3": 1}, {"build": 65}, {"build": 75}]
)
def test_prebuild_on_a_switched_server_keeps_the_switch_and_stops(fail: dict[str, int]) -> None:
    srv = settled(fail=fail)
    warnings: list[str] = []
    with pytest.raises(CloudProvisioningError, match="stays enabled"):
        cm.prebuild_images(srv, warnings, legacy_command=LEGACY)
    assert_kept(srv, warnings)
    assert LEGACY not in srv.commands


def test_byos_setup_rerun_prebuild_never_adds_the_group() -> None:
    from dango.cli.commands import deploy_provision as dp

    srv = settled(fail={"P2": 65})
    with pytest.raises(CloudProvisioningError):
        dp._prebuild_images(srv, [])
    assert srv.marker and not srv.in_group and "FB" not in srv.events


@pytest.mark.parametrize("failing", ["H1", "H2", "H3", "M"])
def test_enable_fresh_on_a_switched_server_keeps_the_switch(failing: str) -> None:
    srv = settled(project=False, fail={failing: 1})
    warnings: list[str] = []
    access = cm.enable_fresh(srv, warnings, "1.0.14")
    assert access != cm.ACCESS_GROUP
    assert_kept(srv, warnings)


def test_setup_with_a_deleted_project_yml_never_adds_the_group() -> None:
    srv = settled(project=False, fail={"H3": 1})
    result = server_setup.SetupResult()
    server_setup._setup_compose_helper(srv, result, None)
    assert_kept(srv, result.warnings)
    assert not result.helper_created and "compose_helper" in result.steps_completed


def test_setup_marks_the_marker_as_its_own_only_when_none_existed() -> None:
    fresh = FakeServer(project=False, in_group=False)
    result = server_setup.SetupResult()
    server_setup._setup_compose_helper(fresh, result, None)
    assert result.helper_created
    again = server_setup.SetupResult()
    server_setup._setup_compose_helper(fresh, again, None)
    assert not again.helper_created


@pytest.mark.parametrize("project", [True, False])
def test_ensure_pinned_failure_keeps_the_switch(project: bool) -> None:
    srv = settled(project=project, pinned=None, fail={} if not project else {"P2": 1})
    warnings: list[str] = []
    cm.ensure_pinned(srv, warnings)
    assert_kept(srv, warnings)


def test_fallback_is_refused_for_a_preexisting_marker_unless_the_deploy_made_it() -> None:
    srv = settled()
    warnings: list[str] = []
    cm.enter_fallback(srv, warnings, "1.0.14", "x", switched=True)
    assert srv.marker and not srv.in_group
    cm.enter_fallback(srv, warnings, "1.0.14", "x", switched=True, own_marker=True)
    assert not srv.marker and srv.in_group


# --- guard window (N3) -------------------------------------------------------------------------------


def test_the_guard_is_rearmed_after_dango_web_stops_in_the_switch() -> None:
    srv = FakeServer(marker=False, in_group=True)
    cm.migrate_docker_access(
        srv,
        [],
        version="1.0.14",
        on_step=lambda: srv.events.append("tick"),
        manage_web=True,
    )
    assert srv.events[srv.events.index("web-stop") + 1] == "tick"


def test_the_guard_is_rearmed_after_dango_web_stops_in_the_restore() -> None:
    srv = settled()
    cm.restore_docker_access(
        srv,
        [],
        version="1.0.13",
        manage_web=True,
        on_step=lambda: srv.events.append("tick"),
    )
    assert srv.events[srv.events.index("web-stop") + 1] == "tick"


# --- at-target restore order (N4) ---------------------------------------------------------------------


def test_at_target_downgrade_converges_even_when_the_strict_runtime_check_raises() -> None:
    srv = settled(version="1.0.13")
    srv.record = '{"dango": "1.0.13", "docker_access": "helper"}'
    with patch(
        "dango.platform.cloud.runtime_ownership.secure_runtime", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError):
            upgrade_dango(srv, version="1.0.13", force=True)
    assert "R" in srv.events and srv.in_group and not srv.marker


# --- needs_restore (N5) -------------------------------------------------------------------------------


def test_an_unreadable_probe_still_restores() -> None:
    assert cm.needs_restore(FakeServer(blind=True)) is True


def test_group_fallback_after_helper_use_is_restored_while_the_sudoers_rule_remains() -> None:
    srv = FakeServer(marker=False, in_group=True, sudoers=True)
    srv.record = '{"dango": "1.0.14", "docker_access": "group"}'
    assert cm.needs_restore(srv) is True


def test_group_fallback_without_a_leftover_rule_and_legacy_groups_are_not_restored() -> None:
    fallback = FakeServer(marker=False, in_group=True)
    fallback.record = '{"dango": "1.0.14", "docker_access": "group"}'
    assert cm.needs_restore(fallback) is False
    assert cm.needs_restore(FakeServer(marker=False, in_group=True)) is False


def test_downgrade_of_a_fallback_server_that_used_the_helper_runs_the_restore() -> None:
    srv = FakeServer(version="1.0.14", marker=False, in_group=True, sudoers=True)
    srv.record = '{"dango": "1.0.14", "docker_access": "group"}'
    upgrade_dango(srv, version="1.0.13", force=True)
    assert "R" in srv.events and not srv.sudoers
