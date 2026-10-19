"""tests/unit/test_interruption_matrix_create_backup.py

Interruption matrix for create_backup: kill at every progress boundary, then back up again.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal, cast

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.backup import create_backup
from tests.factories.interruption_fakes import (
    BACKUP_DIR,
    ConnectionLost,
    EventRecorder,
    FakeServer,
    Killed,
    KillRule,
    distinct_backup_names,
    distinct_events,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _distinct_backup_names() -> Iterator[None]:
    with distinct_backup_names():
        yield


def _backup(server: FakeServer, recorder: EventRecorder | None = None, **kwargs: Any) -> Any:
    return create_backup(
        cast(Any, server),
        backup_type="pre-deploy",
        on_progress=recorder or EventRecorder(),
        **kwargs,
    )


EVENTS = [
    ("check_disk_space", "running"),
    ("check_disk_space", "done"),
    ("stop_services", "running"),
    ("stop_services", "done"),
    ("checkpoint_databases", "running"),
    ("checkpoint_databases", "done"),
    ("get_metabase_volume", "running"),
    ("get_metabase_volume", "done"),
    ("create_archive", "running"),
    ("create_archive", "done"),
    ("start_services", "running"),
    ("start_services", "done"),
    ("rotate_backups", "running"),
    ("rotate_backups", "done"),
]


def test_event_list_matches_a_healthy_run() -> None:
    recorder = EventRecorder()
    _backup(FakeServer(), recorder)
    assert distinct_events(recorder.events) == EVENTS


@pytest.mark.parametrize("event", EVENTS, ids=[f"{s}:{st}" for s, st in EVENTS])
def test_killed_at_boundary_then_backup_again(event: tuple[str, str]) -> None:
    server = FakeServer()
    first = _backup(server)  # an existing good backup that retention must not lose
    good = first.archive_path

    with pytest.raises(Killed):
        _backup(server, EventRecorder(kill_at=event))

    assert server.web_active, "dango-web was left stopped"
    assert server.metabase_up, "Metabase was left stopped"
    assert server.partial_archives() == [], "a partial archive was left behind"
    assert server.staging == set(), "a staging directory with project data was left behind"
    # Retention (keep=1) may drop the older backup only once the new one is complete.
    assert server.complete_archives(), "no good backup is left"
    if good not in server.complete_archives():
        assert event[0] == "rotate_backups"

    second = _backup(server)
    assert second.archive_path in server.complete_archives()
    assert second.archive_path.replace(".tar.gz", ".json") in server.manifests
    assert server.web_active
    assert server.complete_archives() == [second.archive_path], "retention (keep=1) is corrupted"


@pytest.mark.parametrize(
    ("label", "rule"),
    [
        ("dango-web stopped, client dies", KillRule("systemctl stop dango-web", when="after")),
        ("metabase stopped, client dies", KillRule("stop metabase", when="after")),
        ("tar half written", KillRule("tar -czf", when="partial")),
        ("archive done, manifest copy missing", KillRule("manifest.json >", when="before")),
        ("manifest staged, client dies", KillRule("MANIFEST_EOF", when="after")),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_killed_inside_a_command_then_backup_again(label: str, rule: KillRule) -> None:
    server = FakeServer()
    first = _backup(server)
    server.kill = rule
    with pytest.raises(Killed):
        _backup(server)
    server.kill = None

    assert server.web_active, f"{label}: dango-web was left stopped"
    assert server.metabase_up, f"{label}: Metabase was left stopped"
    assert server.partial_archives() == [], f"{label}: partial archive left"
    assert server.staging == set(), f"{label}: staging directory left"
    assert first.archive_path in server.complete_archives()

    second = _backup(server)
    assert server.complete_archives() == [second.archive_path]


def test_services_stay_down_when_the_caller_asked_for_that() -> None:
    """restart_services=False (push_deploy) is the caller's job to undo; the contract is explicit."""
    server = FakeServer()
    _backup(server, restart_services=False)
    assert not server.web_active


ORPHAN = f"{BACKUP_DIR}/backup-20250101-000000.tar.gz.partial"


def test_an_old_orphaned_partial_archive_is_removed_by_the_next_backup() -> None:
    server = FakeServer()
    server.archives[ORPHAN] = "partial"
    server.stale.add(ORPHAN)
    _backup(server)
    assert ORPHAN not in server.archives
    assert any("-mmin +" in c and "-delete" in c for c in server.commands)


def test_a_recent_partial_archive_could_be_a_running_backup_and_is_left_alone() -> None:
    server = FakeServer()
    server.archives[ORPHAN] = "partial"
    _backup(server)
    assert ORPHAN in server.archives


def test_the_archive_is_renamed_without_clobbering() -> None:
    server = FakeServer()
    _backup(server)
    assert any("mv -T -n" in c for c in server.commands)


def test_a_name_collision_fails_loudly_and_leaves_the_existing_archive_alone() -> None:
    server = FakeServer()
    planted = f"{BACKUP_DIR}/backup-20260101-000001.tar.gz"
    server.archives[planted] = "complete"
    server._order.append(planted)
    with pytest.raises(CloudProvisioningError):
        _backup(server)
    assert server.archives[planted] == "complete"
    assert server.partial_archives() == []
    assert server.web_active


@pytest.mark.parametrize(
    ("label", "rule"),
    [
        ("dies before the copy", KillRule("tar -czf", when="before", drop_connection=True)),
        ("dies during tar", KillRule("tar -czf", when="partial", drop_connection=True)),
        ("dies after tar", KillRule("tar -czf", when="after", drop_connection=True)),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_lost_connection_leaves_nothing_that_stops_the_next_backup(
    label: str, rule: KillRule
) -> None:
    """The client is gone, so nothing on the server is cleaned up; the rerun must still work."""
    server = FakeServer()
    server.kill = rule
    with pytest.raises((Killed, ConnectionLost)):
        _backup(server)
    server.kill = None
    server.stale.update(server.partial_archives())  # time passes: a dead run's partial is old
    server.stale.update(server.staging)
    server.reconnect()

    again = _backup(server)
    assert server.staging == set(), "a dead run's staging directory (project data) was kept"

    assert again.archive_path in server.complete_archives()
    assert again.archive_path.replace(".tar.gz", ".json") in server.manifests
    assert server.web_active and server.metabase_up
    assert server.partial_archives() == []


def test_the_stale_sweep_runs_before_the_disk_check_so_a_full_disk_heals() -> None:
    server = FakeServer(disk_mb=10)
    server.archives[ORPHAN] = "partial"
    server.stale.add(ORPHAN)
    with pytest.raises(CloudProvisioningError, match="Insufficient disk space"):
        _backup(server)
    assert ORPHAN not in server.archives, "the orphan that filled the disk was not removed"
    sweep = next(i for i, c in enumerate(server.commands) if "-mmin +15" in c)
    disk = next(i for i, c in enumerate(server.commands) if c.startswith("df -m"))
    assert sweep < disk


def test_stale_root_owned_staging_directories_are_removed_without_following_links() -> None:
    server = FakeServer()
    leftover = "/tmp/dango-backup.deadbeef"
    server.staging.add(leftover)
    server.stale.add(leftover)
    _backup(server)
    assert leftover not in server.staging
    command = next(c for c in server.commands if "dango-backup.*" in c)
    assert "-user root" in command and "-maxdepth 1" in command and "-type d" in command
    assert "dango-restore-staging-*" in command


@pytest.mark.parametrize("when", ["before", "after"])
def test_a_complete_archive_always_has_its_manifest(when: Literal["before", "after"]) -> None:
    """The archive is the commit marker: interrupted before the rename, no archive exists."""
    server = FakeServer()
    server.kill = KillRule("mv -T -n", when=when)
    with pytest.raises(Killed):
        _backup(server)
    server.kill = None
    for archive in server.complete_archives():
        assert archive.replace(".tar.gz", ".json") in server.manifests
    if not server.complete_archives():
        assert server.manifests == {}, "a manifest was left without its archive"


def _staging_dir_age(minutes: int) -> FakeServer:
    server = FakeServer()
    server.staging.add("/tmp/dango-backup.cafe0001")
    server.age_minutes["/tmp/dango-backup.cafe0001"] = minutes
    return server


def test_a_staging_directory_20_minutes_old_may_be_a_running_operation_and_survives() -> None:
    # Directory mtime only changes when children are added or removed, so a restore or a
    # long tar can look old; rollback, restore, resize and migrate take no deploy lock.
    server = _staging_dir_age(20)
    _backup(server)
    assert "/tmp/dango-backup.cafe0001" in server.staging


def test_a_staging_directory_7_hours_old_is_removed() -> None:
    server = _staging_dir_age(7 * 60)
    _backup(server)
    assert "/tmp/dango-backup.cafe0001" not in server.staging


def test_the_staging_threshold_is_far_above_the_scheduled_backup_timeout() -> None:
    server = FakeServer()
    _backup(server)
    command = next(c for c in server.commands if "dango-backup.*" in c)
    minutes = int(command.split("-mmin +")[1].split()[0])
    assert minutes * 60 > 900 * 4  # scheduled backups time out at 900 s (TimeoutStartSec)


def test_partial_archives_use_the_short_threshold() -> None:
    server = FakeServer()
    old, young = f"{BACKUP_DIR}/backup-1.tar.gz.partial", f"{BACKUP_DIR}/backup-2.tar.gz.partial"
    server.archives.update({old: "partial", young: "partial"})
    server.age_minutes.update({old: 20, young: 10})
    _backup(server)
    assert old not in server.archives and young in server.archives


def test_an_old_manifest_without_an_archive_is_removed() -> None:
    server = FakeServer()
    orphan = f"{BACKUP_DIR}/backup-20250101-000000.json"
    young = f"{BACKUP_DIR}/backup-20250101-000001.json"
    paired = f"{BACKUP_DIR}/backup-20250101-000002.json"
    server.manifests.update({orphan: "{}", young: "{}", paired: "{}"})
    server.archives[paired.replace(".json", ".tar.gz")] = "complete"
    server._order.append(paired.replace(".json", ".tar.gz"))
    server.age_minutes.update({orphan: 20, young: 5, paired: 20})
    _backup(server)
    assert orphan not in server.manifests
    assert young in server.manifests and paired in server.manifests
