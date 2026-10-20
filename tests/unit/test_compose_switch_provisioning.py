"""tests/unit/test_compose_switch_provisioning.py

Fresh deploy (setup, first image build) and `remote push` with the compose helper: the helper is installed
and verified at setup, the group is only used as the loud fallback, and push renders through the helper.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud import deployer, server_setup
from dango.platform.cloud.file_sync import SyncResult
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.14"),
    ):
        yield


def setup_result() -> server_setup.SetupResult:
    return server_setup.SetupResult()


# --- setup (V1, V11) -------------------------------------------------------------------------


def test_setup_steps_have_no_docker_group_step_and_end_with_the_helper() -> None:
    names = [f.__name__ for f in server_setup._SETUP_STEPS]
    assert "_setup_docker_group" not in names
    assert names[-2:] == ["_setup_runtime_ownership", "_setup_compose_helper"]
    assert not hasattr(server_setup, "_setup_docker_group")


def test_fresh_setup_installs_the_helper_and_never_adds_the_group() -> None:
    srv = FakeServer(project=False, in_group=False)
    result = setup_result()
    server_setup._setup_compose_helper(srv, result, None)
    assert srv.events[:3] == ["H1", "H2", "H3"]
    assert srv.marker and not srv.in_group
    assert "usermod" not in srv.events and "FB" not in srv.events
    assert "P1" not in srv.events and "G" not in srv.events and "T" not in srv.events
    assert "compose_helper" in result.steps_completed
    assert srv.record == '{"dango": "1.0.14", "docker_access": "helper"}'
    assert srv.events.index("H3") < srv.events.index("M")


@pytest.mark.parametrize("failing", ["H1", "H2", "H3", "M"])
def test_fresh_setup_falls_back_to_the_group_with_a_loud_warning(failing: str) -> None:
    srv = FakeServer(project=False, in_group=False, fail={failing: 1})
    result = setup_result()
    server_setup._setup_compose_helper(srv, result, None)
    assert not srv.marker and srv.in_group
    assert "FB" in srv.events
    assert result.warnings and "docker group" in result.warnings[0]
    assert "dango remote upgrade" in result.warnings[0]
    assert "compose_helper" in result.steps_completed


def test_setup_rerun_over_an_existing_project_runs_the_full_migration() -> None:
    srv = FakeServer(project=True, in_group=True)
    server_setup._setup_compose_helper(srv, setup_result(), None)
    ev = srv.events
    assert srv.marker and not srv.in_group
    assert ev.index("P2") < ev.index("M") < ev.index("G") < ev.index("T")
    assert ev.index("web-stop") < ev.index("G")
    assert ev.index("helper:up-build") < len(ev) - 1 - ev[::-1].index("web-start")


def test_whole_setup_run_never_runs_usermod_when_the_helper_installs() -> None:
    srv = FakeServer(project=False, in_group=False)
    with patch("dango.platform.cloud.server_setup._preflight_check"):
        result = server_setup.setup_server(srv)
    assert "usermod" not in srv.events
    assert not any("usermod -aG docker dango" in c for c in srv.commands)
    assert "docker_group" not in result.steps_completed
    assert "compose_helper" in result.steps_completed


def test_setup_ownership_step_covers_the_helper_files() -> None:
    srv = FakeServer(project=False, in_group=False)
    with patch("dango.platform.cloud.server_setup._preflight_check"):
        server_setup.setup_server(srv)
    assert any("# compose_helper" in c for c in srv.commands)


# --- first deploy (D3) --------------------------------------------------------------------------------


LEGACY = "cd /srv/dango/project && sudo -u dango docker compose build"


def test_first_deploy_pins_renders_and_builds_through_the_helper() -> None:
    srv = FakeServer(marker=True, in_group=False)
    warnings: list[str] = []
    cm.prebuild_images(srv, warnings, legacy_command=LEGACY)
    assert [e for e in srv.events if e in ("P1", "pin-project", "P2", "P3", "build")] == [
        "P1",
        "pin-project",
        "P2",
        "P3",
        "build",
    ]
    assert LEGACY not in srv.commands
    assert warnings == []


@pytest.mark.parametrize("code", [64, 65, 66, 67, 75])
def test_helper_build_refusal_falls_back_to_the_group_and_legacy_build(code: int) -> None:
    srv = FakeServer(marker=True, in_group=False, fail={"build": code})
    warnings: list[str] = []
    cm.prebuild_images(srv, warnings, legacy_command=LEGACY)
    assert not srv.marker and srv.in_group
    assert srv.commands[-1] == LEGACY
    assert warnings and "docker group" in warnings[0]


@pytest.mark.parametrize("failing", ["P1", "pin", "P2", "P3"])
def test_pin_or_render_failure_falls_back_before_building(failing: str) -> None:
    srv = FakeServer(marker=True, in_group=False, fail={failing: 1})
    warnings: list[str] = []
    cm.prebuild_images(srv, warnings, legacy_command=LEGACY)
    assert "build" not in srv.events
    assert not srv.marker and srv.in_group and srv.commands[-1] == LEGACY
    assert warnings


def test_an_ordinary_build_failure_still_raises() -> None:
    srv = FakeServer(marker=True, in_group=False, fail={"build": 1})
    with pytest.raises(CloudProvisioningError):
        cm.prebuild_images(srv, [], legacy_command=LEGACY)
    assert srv.marker and "FB" not in srv.events


def test_without_the_marker_only_the_legacy_build_runs() -> None:
    srv = FakeServer(marker=False, in_group=True)
    warnings: list[str] = []
    cm.prebuild_images(srv, warnings, legacy_command=LEGACY)
    assert "P1" not in srv.events and "build" not in srv.events
    assert srv.commands[-1] == LEGACY
    assert warnings == []


def test_legacy_build_failure_raises() -> None:
    from dango.platform.cloud.ssh import CommandResult

    class Failing(FakeServer):
        def exec_command(self, command: str, timeout: int | None = None, **_k: Any) -> Any:
            if command == LEGACY:
                return CommandResult("", "no network", 1)
            return super().exec_command(command, timeout)

    with pytest.raises(CloudProvisioningError, match="Docker image build failed"):
        cm.prebuild_images(Failing(marker=False, in_group=True), [], legacy_command=LEGACY)


def test_both_deploy_paths_use_the_shared_prebuild() -> None:
    from dango.cli.commands import deploy_provision as dp

    assert callable(dp._prebuild_images)
    for fn in (dp.run_provisioning, dp.run_byos_setup):
        source = inspect.getsource(fn)
        assert "_prebuild_images(" in source
        assert "_prebuild_command(" not in source


def test_prebuild_images_helper_in_deploy_provision() -> None:
    from dango.cli.commands import deploy_provision as dp

    srv = FakeServer(marker=True, in_group=False)
    warnings: list[str] = []
    dp._prebuild_images(srv, warnings)
    assert "build" in srv.events and warnings == []


# --- push (A15) -------------------------------------------------------------------------------------------


def push(srv: FakeServer, synced: list[str], tmp_path: Path) -> Any:
    sync = SyncResult(synced_files=synced, dry_run=False)
    backup = MagicMock(warnings=[], archive_path="/b.tar.gz")
    with (
        patch("dango.platform.cloud.file_sync.sync_project_files", return_value=sync),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
        patch("dango.platform.cloud.pushed_requirements.install_pushed_requirements"),
    ):
        return deployer.push_deploy(srv, tmp_path, "10.0.0.1")


def test_push_with_docker_file_changes_renders_then_removes_images_through_the_helper(
    tmp_path: Path,
) -> None:
    srv = FakeServer(marker=True, in_group=False)
    push(srv, ["Dockerfile.metabase"], tmp_path)
    ev = srv.events
    assert ev.index("P2") < ev.index("helper:down-rmi") < len(ev) - 1 - ev[::-1].index("web-start")


def test_push_down_rmi_is_a_helper_verb_with_the_legacy_command_as_the_fallback_branch(
    tmp_path: Path,
) -> None:
    srv = FakeServer(marker=True, in_group=False)
    push(srv, ["docker-compose.yml"], tmp_path)
    cmd = next(c for c in srv.commands if "dango-compose down-rmi" in c)
    assert cmd.startswith("if ") and "sudo -u dango" in cmd.split("; else ", 1)[1]
    assert "sudo -u dango" not in cmd.split("; else ", 1)[0]
    assert ch.HELPER_PATH in cmd


@pytest.mark.parametrize("synced", [["metabase-plugins/x.jar"], [".dango/project.yml"]])
def test_push_of_plugins_or_project_yml_renders_without_removing_images(
    tmp_path: Path, synced: list[str]
) -> None:
    srv = FakeServer(marker=True, in_group=False)
    push(srv, synced, tmp_path)
    assert "P2" in srv.events and "helper:down-rmi" not in srv.events


def test_push_of_models_only_does_not_render(tmp_path: Path) -> None:
    srv = FakeServer(marker=True, in_group=False)
    push(srv, ["dbt/models/"], tmp_path)
    assert "P2" not in srv.events and "helper:down-rmi" not in srv.events


def test_push_without_the_marker_keeps_the_legacy_commands(tmp_path: Path) -> None:
    srv = FakeServer(marker=False, in_group=True)
    push(srv, ["Dockerfile.metabase"], tmp_path)
    assert "P2" not in srv.events and "legacy:down-rmi" in srv.events


def test_push_render_failure_is_a_warning_and_the_service_still_restarts(tmp_path: Path) -> None:
    srv = FakeServer(marker=True, in_group=False, fail={"P2": 1})
    result = push(srv, ["Dockerfile.metabase"], tmp_path)
    assert "web-start" in srv.events
    assert any("render" in w.lower() for w in result.warnings)


def test_push_warns_when_the_server_was_upgraded_by_a_newer_client(tmp_path: Path) -> None:
    srv = FakeServer(marker=True, in_group=False)
    srv.record = '{"dango": "1.0.20", "docker_access": "helper"}'
    result = push(srv, ["dbt/models/"], tmp_path)
    assert any("upgraded by Dango 1.0.20" in w for w in result.warnings)


def test_push_is_silent_when_the_client_is_current(tmp_path: Path) -> None:
    srv = FakeServer(marker=True, in_group=False)
    srv.record = '{"dango": "1.0.14", "docker_access": "helper"}'
    result = push(srv, ["dbt/models/"], tmp_path)
    assert not any("upgraded by Dango" in w for w in result.warnings)
