"""tests/unit/test_interruption_matrix_provisioning.py

Interruption matrix for run_provisioning: kill at every status line, then deploy again.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_provision
from dango.cli.commands.deploy_provision import run_provisioning
from dango.cli.commands.deploy_wizard import WizardConfig
from dango.platform.cloud.provisioning import provision_droplet
from dango.platform.cloud.ssh import CommandResult
from tests.factories.interruption_fakes import Killed

pytestmark = pytest.mark.unit

_MODULE = "dango.cli.commands.deploy_provision"


class FakeDO:
    """DigitalOcean client that records every resource that exists (and is billed or counted)."""

    def __init__(self, *, fail_delete: bool = False) -> None:
        self.droplets: set[int] = set()
        self.firewalls: set[str] = set()
        self.ssh_keys: set[int] = set()
        self.fail_delete = fail_delete
        self.ever_created: list[str] = []
        self._next = 200

    def _id(self) -> int:
        self._next += 1
        self.ever_created.append(str(self._next))
        return self._next

    def upload_ssh_key(self, name: str, public_key: str) -> dict[str, Any]:
        key_id = self._id()
        self.ssh_keys.add(key_id)
        return {"id": key_id}

    def create_droplet(self, **kwargs: Any) -> dict[str, Any]:
        droplet_id = self._id()
        self.droplets.add(droplet_id)
        return {"id": droplet_id}

    def create_firewall(self, **kwargs: Any) -> dict[str, Any]:
        firewall_id = f"fw-{self._id()}"
        self.firewalls.add(firewall_id)
        return {"id": firewall_id}

    def _delete(self, bucket: set[Any], item: Any) -> None:
        if self.fail_delete:
            raise RuntimeError("DigitalOcean API unavailable")
        bucket.discard(item)

    def delete_droplet(self, droplet_id: int) -> None:
        self._delete(self.droplets, droplet_id)

    def delete_firewall(self, firewall_id: str) -> None:
        self._delete(self.firewalls, firewall_id)

    def delete_ssh_key(self, key_id: int) -> None:
        self._delete(self.ssh_keys, key_id)

    def nothing_left(self) -> bool:
        return not (self.droplets or self.firewalls or self.ssh_keys)


class FakeSSH:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def generate_key_pair(self) -> str:
        return "ssh-ed25519 AAAA"

    def connect(self, *args: Any, **kwargs: Any) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def exec_command(self, command: str, timeout: int | None = None) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)


class StatusLines:
    """Replaces ``_status``: records each line and raises :class:`Killed` at ``kill_at``."""

    def __init__(self, kill_at: int | None = None) -> None:
        self.lines: list[str] = []
        self.kill_at = kill_at

    def __call__(self, message: str) -> None:
        self.lines.append(message)
        if self.kill_at is not None and len(self.lines) - 1 == self.kill_at:
            raise Killed(f"killed at: {message}")


def _wait_ready(client: Any, droplet_id: int, **kwargs: Any) -> dict[str, Any]:
    return {
        "id": droplet_id,
        "networks": {"v4": [{"type": "public", "ip_address": f"198.51.100.{droplet_id % 200}"}]},
    }


def _config() -> WizardConfig:
    return WizardConfig(
        region="nyc1",
        size_slug="s-2vcpu-4gb",
        size_tier=None,
        domain=None,
        admin_email="admin@test.com",
        admin_password="strongpassword123",
        skip_oauth=True,
        enable_backups=False,
        monthly_cost=24,
        push_secrets=False,
    )


def _printed(console: MagicMock) -> str:
    return "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)


def _provision(
    project: Path,
    do: FakeDO,
    status: StatusLines,
    *,
    killed_in_wait: bool = False,
    console: MagicMock | None = None,
) -> Any:
    console = console if console is not None else MagicMock()
    wait = MagicMock(side_effect=Killed("killed while waiting")) if killed_in_wait else _wait_ready
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=do))
        enter(patch("dango.platform.cloud.ssh.SSHManager", FakeSSH))
        enter(
            patch("dango.platform.cloud.size_availability.check_size_in_region", return_value=True)
        )
        enter(patch("dango.platform.cloud.provisioning.wait_for_droplet_ready", wait))
        enter(patch("dango.platform.cloud.provisioning.wait_for_ssh"))
        enter(
            patch(
                "dango.platform.cloud.server_setup.setup_server",
                return_value=MagicMock(warnings=[]),
            )
        )
        enter(patch("dango.platform.cloud.server_setup.resolve_install_source"))
        enter(patch("dango.platform.cloud.file_sync.sync_project_files"))
        enter(patch(f"{_MODULE}._status", status))
        enter(patch(f"{_MODULE}.console", console))
        for name in (
            "_fix_project_ownership",
            "_generate_cloud_profiles",
            "_start_services",
            "_create_admin_and_enable_auth",
            "_trigger_metabase_setup",
            "_wait_for_metabase",
            "_print_port_notice",
            "_save_extra_metadata",
        ):
            enter(patch(f"{_MODULE}.{name}"))
        enter(patch(f"{_MODULE}._health_check", return_value=True))
        enter(patch(f"{_MODULE}._deployer_ip", return_value=None))
        enter(patch(f"{_MODULE}._prebuild_command", return_value="true"))
        enter(
            patch("dango.platform.cloud.backup.get_remote_compose_project_name", return_value="p")
        )
        return run_provisioning(project, _config())


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "project.yml").write_text("project:\n  name: test\n")
    yield tmp_path


def _healthy_lines(project: Path) -> list[str]:
    status = StatusLines()
    _provision(project, FakeDO(), status)
    return status.lines


def test_a_healthy_run_creates_one_of_each(project: Path) -> None:
    do = FakeDO()
    result = _provision(project, do, StatusLines())
    assert do.droplets == {result.droplet_id}
    assert do.firewalls == {result.firewall_id}
    assert do.ssh_keys == {result.ssh_key_id}


def test_every_status_line_is_a_matrix_row(project: Path) -> None:
    """Fails when a status line is added or removed; update STEPS_EXPECTED with it."""
    assert len(_healthy_lines(project)) == STEPS_EXPECTED


STEPS_EXPECTED = 14


@pytest.mark.parametrize("index", range(STEPS_EXPECTED))
def test_killed_at_status_line_cleans_everything_and_rerun_starts_clean(
    project: Path, index: int
) -> None:
    do = FakeDO()
    with pytest.raises(Killed):
        _provision(project, do, StatusLines(kill_at=index))

    assert do.nothing_left(), (
        f"left behind: droplets={sorted(do.droplets)} firewalls={sorted(do.firewalls)} "
        f"ssh_keys={sorted(do.ssh_keys)}"
    )

    result = _provision(project, do, StatusLines())
    assert do.droplets == {result.droplet_id}
    assert do.firewalls == {result.firewall_id}
    assert do.ssh_keys == {result.ssh_key_id}


@pytest.mark.parametrize("index", [3, 5, 10])
def test_cleanup_names_every_resource_it_removed(project: Path, index: int) -> None:
    do = FakeDO()
    console = MagicMock()
    with pytest.raises(Killed):
        _provision(project, do, StatusLines(kill_at=index), console=console)
    out = _printed(console)
    assert "Cleaning up" in out
    assert do.nothing_left()
    # The ids of what was removed appear in the report, so the user can check the console.
    for resource_id in do.ever_created:
        assert resource_id in out


def test_failed_deletes_name_each_orphaned_resource(project: Path) -> None:
    do = FakeDO(fail_delete=True)
    console = MagicMock()
    with pytest.raises(Killed):
        _provision(project, do, StatusLines(kill_at=STEPS_EXPECTED - 2), console=console)
    out = _printed(console)
    for droplet_id in do.droplets:
        assert f"Droplet {droplet_id}" in out
    for firewall_id in do.firewalls:
        assert f"Firewall {firewall_id}" in out
    for key_id in do.ssh_keys:
        assert f"SSH key {key_id}" in out
    assert do.droplets and do.firewalls and do.ssh_keys


def test_provision_droplet_deletes_a_droplet_it_created_when_interrupted_while_waiting() -> None:
    do = FakeDO()
    with (
        patch(
            "dango.platform.cloud.provisioning.wait_for_droplet_ready",
            side_effect=Killed("killed while waiting"),
        ),
        pytest.raises(Killed),
    ):
        provision_droplet(do, "dango-x", "nyc1", "s-2vcpu-4gb", [1])
    assert do.droplets == set(), "the droplet created before the interruption is still running"


def test_module_exposes_the_helpers_this_matrix_patches() -> None:
    for name in ("_status", "_fix_project_ownership", "_health_check", "_deployer_ip"):
        assert hasattr(deploy_provision, name)
