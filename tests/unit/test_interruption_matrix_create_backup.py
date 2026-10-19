"""tests/unit/test_interruption_matrix_create_backup.py

Interruption matrix for create_backup: kill at every progress boundary, then back up again.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from dango.platform.cloud.backup import create_backup
from tests.factories.interruption_fakes import (
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
    assert good in server.complete_archives(), "the earlier good backup was lost"

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
