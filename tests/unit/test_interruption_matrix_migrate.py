"""tests/unit/test_interruption_matrix_migrate.py

Interruption matrix for migrate_server: kill at every progress boundary, then migrate again.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from dango.config.loader import ConfigLoader
from dango.platform.cloud.migrate import migrate_server
from dango.platform.cloud.provisioning import save_provisioning_metadata
from tests.factories.interruption_fakes import (
    EventRecorder,
    FakeServer,
    Killed,
    distinct_backup_names,
    distinct_events,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _distinct_backup_names() -> Iterator[None]:
    with distinct_backup_names():
        yield


OLD_ID = 42
_PATCH = "dango.platform.cloud.migrate"


class FakeDO:
    """DigitalOcean client that remembers which droplets are alive (and are being billed)."""

    def __init__(self, fail_delete: set[int] | None = None) -> None:
        self.alive: set[int] = {OLD_ID}
        self.deleted: list[int] = []
        self.created: list[int] = []
        self.fail_delete = fail_delete or set()
        self.firewall_droplets: set[int] = {OLD_ID}  # firewall "fw-1" protects the old server
        self._next = 99

    def get_firewall(self, firewall_id: str) -> dict[str, Any]:
        return {
            "name": "dango-fw",
            "droplet_ids": sorted(self.firewall_droplets),
            "inbound_rules": [],
            "outbound_rules": [],
        }

    def update_firewall(self, *, firewall_id: str, droplet_ids: list[int], **kwargs: Any) -> None:
        self.firewall_droplets = set(droplet_ids)

    def create(self) -> dict[str, Any]:
        droplet_id = self._next
        self._next += 1
        self.alive.add(droplet_id)
        self.created.append(droplet_id)
        return {
            "id": droplet_id,
            "networks": {"v4": [{"type": "public", "ip_address": f"10.0.0.{droplet_id}"}]},
        }

    def delete_droplet(self, droplet_id: int) -> None:
        if droplet_id in self.fail_delete:
            raise RuntimeError("DigitalOcean API unavailable")
        self.alive.discard(droplet_id)
        self.deleted.append(droplet_id)


class NewServer(FakeServer):
    """The new droplet's SSH session."""

    def connect(self, *args: Any, **kwargs: Any) -> None:
        return None

    def disconnect(self) -> None:
        return None


def _config(project: Path, spaces: Any) -> MagicMock:
    saved = ConfigLoader(project).load_cloud_config()
    assert saved is not None
    config = MagicMock()
    config.droplet_id = saved.droplet_id
    config.droplet_ip = saved.droplet_ip
    config.domain = None
    config.firewall_id = "fw-1"
    config.ssh_key_path = ".dango/cloud_key"
    config.ssh_key_id = 100
    config.spaces = spaces
    return config


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".dango" / "cloud_key").write_text("key")
    save_provisioning_metadata(tmp_path, OLD_ID, "1.2.3.4", "nyc1", "s-2vcpu-4gb")
    return tmp_path


def _migrate(
    project: Path,
    do: FakeDO,
    old: FakeServer,
    recorder: EventRecorder | None = None,
) -> Any:
    new_server = NewServer()
    with ExitStack() as stack:
        stack.enter_context(
            patch(f"{_PATCH}._upload_backup_to_spaces", return_value="migration/b.tar.gz")
        )
        stack.enter_context(
            patch(
                f"{_PATCH}._download_backup_from_spaces",
                return_value="/srv/dango/backups/deploy/b.tar.gz",
            )
        )
        stack.enter_context(patch(f"{_PATCH}._copy_secrets_between_servers", return_value=[]))
        stack.enter_context(
            patch("dango.platform.cloud.provisioning.provision_droplet", side_effect=_provision(do))
        )
        stack.enter_context(patch("dango.platform.cloud.ssh.SSHManager", return_value=new_server))
        stack.enter_context(patch("dango.platform.cloud.server_setup.setup_server"))
        stack.enter_context(patch("dango.platform.cloud.backup.restore_from_archive"))
        stack.enter_context(patch("dango.platform.cloud.backup.time.sleep"))
        return migrate_server(
            cast(Any, do),
            cast(Any, old),
            _config(project, spaces=MagicMock()),
            "s-4vcpu-8gb",
            "sfo3",
            project_root=project,
            on_progress=recorder or EventRecorder(),
        )


def _provision(do: FakeDO) -> Any:
    def provision(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return do.create()

    return provision


EVENTS = [
    ("backup", "running"),
    ("backup", "done"),
    ("upload_spaces", "running"),
    ("upload_spaces", "done"),
    ("provision", "running"),
    ("provision", "done"),
    ("setup_server", "running"),
    ("setup_server", "done"),
    ("copy_secrets", "running"),
    ("copy_secrets", "done"),
    ("download_spaces", "running"),
    ("download_spaces", "done"),
    ("restore", "running"),
    ("restore", "done"),
    ("firewall", "running"),
    ("firewall", "done"),
    ("verify_health", "running"),
    ("verify_health", "done"),
    ("cleanup", "running"),
    ("cleanup", "done"),
]


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    yield _project(tmp_path)


def _recorded_pointer(project: Path) -> int:
    saved = ConfigLoader(project).load_cloud_config()
    assert saved is not None and saved.droplet_id is not None
    return saved.droplet_id


def test_event_list_matches_a_healthy_run(project: Path) -> None:
    recorder = EventRecorder()
    _migrate(project, FakeDO(), FakeServer(), recorder)
    assert distinct_events(recorder.events) == EVENTS


@pytest.mark.parametrize("event", EVENTS, ids=[f"{s}:{st}" for s, st in EVENTS])
def test_killed_at_boundary_then_migrate_again(project: Path, event: tuple[str, str]) -> None:
    do, old = FakeDO(), FakeServer()
    with pytest.raises(Killed) as excinfo:
        _migrate(project, do, old, EventRecorder(kill_at=event))
    report = "\n".join([str(excinfo.value), *getattr(excinfo.value, "__notes__", [])])

    # The old server is only ever destroyed by the last step, and is serving until then.
    old_destroyed = OLD_ID in do.deleted
    assert old_destroyed == (event == ("cleanup", "done")), "old server removed too early"
    assert old.web_active and old.metabase_up, "old server's services were left stopped"
    # cloud.yml points at a droplet that exists, and no other paid droplet is left behind.
    pointer = _recorded_pointer(project)
    assert pointer in do.alive, "cloud.yml points at a deleted droplet"
    for orphan in do.alive - {pointer}:
        assert f"doctl compute droplet delete {orphan}" in report, f"unreported droplet {orphan}"

    # The server cloud.yml names is the one behind the firewall; the old one keeps its
    # protection until the switch is recorded.
    assert pointer in do.firewall_droplets, "the live server has no firewall"
    if pointer == OLD_ID:
        assert OLD_ID in do.firewall_droplets, "old server lost its firewall before the switch"

    reported = do.alive - {pointer}
    again = _migrate(project, do, old)
    assert again.new_droplet_id in do.alive
    # Only a droplet the interruption report named (to delete by hand) may remain besides it.
    assert do.alive - {again.new_droplet_id} <= reported | {pointer}
    assert _recorded_pointer(project) == again.new_droplet_id


@pytest.mark.parametrize(
    "event",
    [e for e in EVENTS if e[0] in {"setup_server", "download_spaces", "restore", "verify_health"}],
    ids=lambda e: f"{e[0]}:{e[1]}",
)
def test_a_failed_cleanup_names_the_orphaned_droplet(project: Path, event: tuple[str, str]) -> None:
    do = FakeDO(fail_delete={99})
    with pytest.raises(Killed) as excinfo:
        _migrate(project, do, FakeServer(), EventRecorder(kill_at=event))
    text = "\n".join([str(excinfo.value), *getattr(excinfo.value, "__notes__", [])])
    assert 99 in do.alive
    assert "99" in text and "doctl compute droplet delete 99" in text


def test_an_unhealthy_new_server_leaves_the_old_one_behind_its_firewall(project: Path) -> None:
    do, old = FakeDO(), FakeServer()
    with patch("dango.platform.cloud.backup.verify_health", return_value=False):
        result = _migrate(project, do, old)
    assert result.old_droplet_destroyed is False
    assert OLD_ID in do.alive
    assert OLD_ID in do.firewall_droplets, "old server lost its firewall"
    assert _recorded_pointer(project) == OLD_ID


def test_after_the_switch_the_firewall_follows_the_new_server(project: Path) -> None:
    do = FakeDO()
    result = _migrate(project, do, FakeServer())
    assert do.firewall_droplets == {result.new_droplet_id}


def test_the_report_after_a_completed_switch_only_mentions_a_live_old_droplet(
    project: Path,
) -> None:
    for event, old_alive in ((("cleanup", "running"), True), (("cleanup", "done"), False)):
        do = FakeDO()
        with pytest.raises(Killed) as excinfo:
            _migrate(project, do, FakeServer(), EventRecorder(kill_at=event))
        report = "\n".join([str(excinfo.value), *getattr(excinfo.value, "__notes__", [])])
        assert (f"doctl compute droplet delete {OLD_ID}" in report) is old_alive
        _project(project)  # reset cloud.yml for the next iteration
