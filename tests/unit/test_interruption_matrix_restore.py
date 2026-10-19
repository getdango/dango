"""tests/unit/test_interruption_matrix_restore.py

Interruption matrix for restore_from_archive: kill at every progress boundary, restore again.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from dango.platform.cloud.backup import restore_from_archive
from tests.factories.interruption_fakes import (
    BACKUP_DIR,
    EventRecorder,
    FakeServer,
    Killed,
    KillRule,
    distinct_events,
)

pytestmark = pytest.mark.unit

ARCHIVE = f"{BACKUP_DIR}/backup-20260101-000000.tar.gz"


@pytest.fixture(autouse=True)
def _no_health_wait() -> Any:
    with patch("dango.platform.cloud.backup.time.sleep"):
        yield


def _server() -> FakeServer:
    server = FakeServer()
    server.archives[ARCHIVE] = "complete"
    server._order.append(ARCHIVE)
    return server


def _restore(server: FakeServer, recorder: EventRecorder | None = None) -> Any:
    return restore_from_archive(cast(Any, server), ARCHIVE, on_progress=recorder or EventRecorder())


EVENTS = [
    ("read_manifest", "done"),
    ("extract_archive", "running"),
    ("extract_archive", "done"),
    ("pre_restore_backup", "running"),
    ("pre_restore_backup", "done"),
    ("stop_services", "running"),
    ("stop_services", "done"),
    ("restore_files", "running"),
    ("restore_files", "done"),
    ("restore_metabase", "running"),
    ("restore_metabase", "done"),
    ("fix_ownership", "running"),
    ("fix_ownership", "done"),
    ("start_services", "running"),
    ("start_services", "done"),
    ("verify_health", "running"),
    ("verify_health", "done"),
]

BEFORE_FILES = {
    ("read_manifest", "done"),
    ("extract_archive", "running"),
    ("extract_archive", "done"),
    ("pre_restore_backup", "running"),
    ("pre_restore_backup", "done"),
    ("stop_services", "running"),
    ("stop_services", "done"),
    ("restore_files", "running"),
}


def _server_with_manifest() -> FakeServer:
    server = _server()
    server.manifests.add(ARCHIVE.replace(".tar.gz", ".json"))
    return server


def test_event_list_matches_a_healthy_run() -> None:
    recorder = EventRecorder()
    _restore(_server_with_manifest(), recorder)
    assert distinct_events(recorder.events) == EVENTS


@pytest.mark.parametrize("event", EVENTS, ids=[f"{s}:{st}" for s, st in EVENTS])
def test_killed_at_boundary_then_restore_again(event: tuple[str, str]) -> None:
    server = _server_with_manifest()
    with pytest.raises(Killed):
        _restore(server, EventRecorder(kill_at=event))

    assert server.web_active, "dango-web was left stopped"
    assert server.metabase_up, "Metabase was left stopped"
    assert server.staging == set(), "the extracted archive (project data) was left in /tmp"
    assert ARCHIVE in server.complete_archives(), "the archive being restored was damaged"
    assert server.partial_archives() == []
    if event in BEFORE_FILES:
        assert server.live_data == "live", "live data changed before the restore step"
    else:
        assert server.live_data == "restored"

    result = _restore(server)
    assert result.services_restarted
    assert result.health_check_passed
    assert server.live_data == "restored"
    assert server.web_active
    assert server.staging == set()
    assert ARCHIVE in server.complete_archives()


@pytest.mark.parametrize(
    ("label", "rule"),
    [
        ("extract half done", KillRule("tar -xzf", when="before")),
        ("safety backup tar half written", KillRule("tar -czf", when="partial")),
        ("services stopped, client dies", KillRule("systemctl stop dango-web", when="after")),
        ("files copied, client dies", KillRule("runuser -u dango -- cp", when="after")),
        ("restart issued, client dies", KillRule("systemctl start dango-web", when="after")),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_killed_inside_a_command_then_restore_again(label: str, rule: KillRule) -> None:
    server = _server_with_manifest()
    server.kill = rule
    with pytest.raises(Killed):
        _restore(server)
    server.kill = None

    assert server.web_active, f"{label}: dango-web was left stopped"
    assert server.metabase_up, f"{label}: Metabase was left stopped"
    assert server.staging == set(), f"{label}: extracted data left in /tmp"
    assert server.partial_archives() == [], f"{label}: partial archive left"
    assert ARCHIVE in server.complete_archives()

    result = _restore(server)
    assert result.health_check_passed
    assert server.live_data == "restored"
