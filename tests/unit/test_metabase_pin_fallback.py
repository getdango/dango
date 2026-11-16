"""tests/unit/test_metabase_pin_fallback.py

A Metabase pin refusal (helper exit 68) and a missing or refusing helper never fall back to the
group or to a legacy Docker build: compose_exec, migration, first deploy and the rebuild command.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform import compose_exec as ce
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud import compose_steps as cs
from dango.platform.cloud.ssh import CommandResult
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _trusted_venv() -> Iterator[None]:
    with patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]):
        yield


LEGACY = "cd /srv/dango/project && sudo -u dango docker compose build"


def _legacy_build_ran(srv: FakeServer) -> bool:
    """A build by the service account's own docker access (never the installed helper script)."""
    direct = [c for c in srv.commands if "# step:" not in c]  # step scripts embed the helper text
    return any(
        c == LEGACY or "up -d --build metabase" in c or "sudo -u dango docker compose" in c
        for c in direct
    )


def _done(code: int, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", err)


# --- compose_exec ---------------------------------------------------------------------------------


def _render_with(code: int, tmp_path: Path, err: str = "dango-compose: pin-metabase: nope") -> Any:
    with (
        patch("dango.platform.compose_exec.subprocess.run", return_value=_done(code, err)),
        patch("dango.platform.compose_exec.render_args", return_value=["1", "2", "v0.1.0", "x"]),
        patch("dango.platform.compose_exec._warn_if_id_differs"),
    ):
        return ce.render(tmp_path, MagicMock(), force_helper=True)


def test_pin_error_survives_compose_exec(tmp_path: Path) -> None:
    from dango.platform.cloud import server_compose

    assert ce.EXIT_PIN == server_compose.EXIT_PIN == 68
    with pytest.raises(ce.ComposePinError, match="pin-metabase"):
        _render_with(68, tmp_path)
    assert issubclass(ce.ComposePinError, ce.ComposeRenderError)
    with (
        patch("dango.platform.compose_exec.render", side_effect=ce.ComposePinError("pin")),
        patch("dango.config.helpers.load_config"),
    ):
        assert ce.main(["render", "--helper", str(tmp_path)]) == 68


def test_other_helper_exits_keep_their_mapping(tmp_path: Path) -> None:
    with pytest.raises(ce.ComposeBusy):
        _render_with(75, tmp_path)
    with pytest.raises(ce.ComposeRenderError) as err:
        _render_with(66, tmp_path)
    assert not isinstance(err.value, ce.ComposePinError)
    with (
        patch("dango.platform.compose_exec.render", side_effect=ce.ComposeRenderError("x")),
        patch("dango.config.helpers.load_config"),
    ):
        assert ce.main(["render", "--helper", str(tmp_path)]) == 1


# --- migration never falls back on a pin error ----------------------------------------------------


def _migrate(srv: FakeServer) -> None:
    cm.migrate_docker_access(srv, [], version="1.0.14", manage_web=True, rebuild=True)


def _pin(srv: FakeServer) -> None:
    cm.pin_and_render(srv, [])


def _ensure(srv: FakeServer) -> None:
    cm.ensure_pinned(srv, [])


def _prebuild(srv: FakeServer) -> None:
    cm.prebuild_images(srv, [], legacy_command=LEGACY, own_marker=True)


@pytest.mark.parametrize(
    ("entry", "marker"),
    [(_migrate, False), (_migrate, True), (_pin, True), (_ensure, True), (_prebuild, True)],
)
def test_pin_error_never_enters_legacy_fallback(entry: Any, marker: bool) -> None:
    srv = FakeServer(marker=marker, in_group=not marker, fail={"P2": 68})
    with pytest.raises(cm.MetabasePinRequired, match="pin-metabase VERSION SHA256"):
        entry(srv)
    assert "FB" not in srv.events and "usermod" not in srv.events
    assert srv.marker is marker and srv.in_group is (not marker)
    assert "web-stop" not in srv.events and "G" not in srv.events and "M" not in srv.events
    assert not _legacy_build_ran(srv) and "build" not in srv.events


def test_pin_required_is_a_provisioning_error() -> None:
    assert issubclass(cm.MetabasePinRequired, CloudProvisioningError)


# --- fresh deploy ---------------------------------------------------------------------------------


def test_new_deploy_never_builds_metabase_without_helper() -> None:
    srv = FakeServer(marker=False, in_group=True)
    with pytest.raises(CloudProvisioningError, match="helper"):
        cm.prebuild_images(srv, [], legacy_command=LEGACY)
    assert not _legacy_build_ran(srv) and "build" not in srv.events


@pytest.mark.parametrize("code", [1, 64, 65, 66, 67, 75, 124])
@pytest.mark.parametrize("own_marker", [True, False])
def test_helper_build_failure_never_uses_legacy_command(code: int, own_marker: bool) -> None:
    srv = FakeServer(marker=True, in_group=False, fail={"build": code})
    with pytest.raises(CloudProvisioningError):
        cm.prebuild_images(srv, [], legacy_command=LEGACY, own_marker=own_marker)
    assert not _legacy_build_ran(srv)


@pytest.mark.parametrize("failing", ["P1", "pin", "P2", "P3"])
def test_pin_or_render_failure_never_uses_legacy_command(failing: str) -> None:
    srv = FakeServer(marker=True, in_group=False, fail={failing: 65})
    with pytest.raises(CloudProvisioningError):
        cm.prebuild_images(srv, [], legacy_command=LEGACY, own_marker=True)
    assert not _legacy_build_ran(srv) and "build" not in srv.events


def test_verified_build_still_runs_through_the_helper() -> None:
    srv = FakeServer(marker=True, in_group=False)
    cm.prebuild_images(srv, [], legacy_command=LEGACY)
    assert "build" in srv.events and not _legacy_build_ran(srv)


# --- rebuild command and step ---------------------------------------------------------------------


def test_strict_rebuild_command_has_no_legacy_branch() -> None:
    strict = cs.rebuild_command("dango-abc", require_helper=True)
    assert ch.MARKER_TEST in strict and "exit 68" in strict
    assert f"{ch.HELPER_PATH} pull" in strict and f"{ch.HELPER_PATH} up-build" in strict
    assert "docker compose" not in strict and "sudo -u dango" not in strict
    default = cs.rebuild_command("dango-abc")
    assert default == cs.rebuild_command("dango-abc", require_helper=False)
    assert "docker compose" in default and "up -d --build metabase" in default


class _Ssh:
    def __init__(self, code: int) -> None:
        self.code = code
        self.commands: list[str] = []

    def exec_command(self, command: str, timeout: int | None = None, **_k: Any) -> CommandResult:
        self.commands.append(command)
        if command == ch.MARKER_TEST:
            return CommandResult("", "", 0)
        return CommandResult("", "boom", self.code)


def test_strict_rebuild_failure_raises_while_the_default_only_warns() -> None:
    with patch(
        "dango.platform.cloud.backup.get_remote_compose_project_name", return_value="dango-abc"
    ):
        strict, warnings = _Ssh(68), []
        with pytest.raises(CloudProvisioningError):
            cs.rebuild_metabase(strict, warnings, require_helper=True)
        assert any("exit 68" in c for c in strict.commands)
        lenient: list[str] = []
        cs.rebuild_metabase(_Ssh(1), lenient)
        assert lenient == ["Docker rebuild did not complete successfully."]


def test_migration_rebuild_requires_the_helper() -> None:
    srv = FakeServer(project=True, in_group=True)
    _migrate(srv)
    rebuilds = [c for c in srv.commands if f"{ch.HELPER_PATH} pull" in c]
    assert rebuilds and all("exit 68" in c and "docker compose" not in c for c in rebuilds)
