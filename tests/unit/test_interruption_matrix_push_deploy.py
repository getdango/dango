"""tests/unit/test_interruption_matrix_push_deploy.py

Interruption matrix for push_deploy: kill at every progress boundary, then deploy again.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from dango.platform.cloud.deployer import push_deploy
from dango.platform.cloud.file_sync import SyncResult
from tests.factories.interruption_fakes import (
    JOURNAL_PATH,
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


def _fake_sync(
    ssh: Any,
    root: Path,
    *,
    remote_host: str,
    dry_run: bool = False,
    allow_empty: bool = False,
    on_progress: Any = None,
) -> SyncResult:
    """Stand-in for rsync/SFTP sync: reports the real step names, changes one model."""
    for step in ("detect_changes", "upload_config", "sync_dbt", "sync_extra"):
        if on_progress is not None:
            on_progress(step, "running")
            on_progress(step, "done")
    return SyncResult(
        synced_files=["dbt/models/"],
        changed_models=["stg_orders"],
        packages_changed=False,
        has_macro_changes=False,
        dry_run=dry_run,
    )


@pytest.fixture(autouse=True)
def _patched_edges() -> Iterator[None]:
    with (
        patch("dango.platform.cloud.file_sync.sync_project_files", side_effect=_fake_sync),
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
    ):
        yield


def _deploy(
    server: FakeServer, root: Path, recorder: EventRecorder | None = None, *, force: bool = False
) -> Any:
    return push_deploy(
        cast(Any, server),
        root,
        "203.0.113.7",
        force=force,
        on_progress=recorder or EventRecorder(),
    )


def _local_journal(root: Path) -> list[dict[str, Any]]:
    path = root / ".dango" / "state" / "deployments.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _healthy_events(tmp_path: Path) -> list[tuple[str, str]]:
    recorder = EventRecorder()
    _deploy(FakeServer(), tmp_path / "recording", recorder)
    return distinct_events(recorder.events)


EVENTS = [
    ("acquire_lock", "running"),
    ("acquire_lock", "done"),
    ("stop_web", "running"),
    ("stop_web", "done"),
    ("create_backup", "running"),
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
    ("rotate_backups", "running"),
    ("rotate_backups", "done"),
    ("create_backup", "done"),
    ("sync_files", "running"),
    ("detect_changes", "running"),
    ("detect_changes", "done"),
    ("upload_config", "running"),
    ("upload_config", "done"),
    ("sync_dbt", "running"),
    ("sync_dbt", "done"),
    ("sync_extra", "running"),
    ("sync_extra", "done"),
    ("sync_files", "done"),
    ("fix_ownership", "running"),
    ("fix_ownership", "done"),
    ("install_deps", "running"),
    ("install_deps", "done"),
    ("validate_sources", "running"),
    ("validate_sources", "done"),
    ("dbt_compile", "running"),
    ("dbt_compile", "done"),
    ("dbt_run", "running"),
    ("dbt_run", "done"),
    ("start_services", "running"),
    ("start_services", "done"),
]


def test_event_list_matches_a_healthy_run(tmp_path: Path) -> None:
    """Guards the matrix: a new progress step must be added to EVENTS (and so be killed at)."""
    assert _healthy_events(tmp_path) == EVENTS


def _assert_converges(server: FakeServer, root: Path, journal_before: int) -> None:
    """A second, healthy run succeeds and leaves one more journal row on each side."""
    result = _deploy(server, root)
    assert result.models_rebuilt == ["stg_orders"]
    assert server.lock is None
    assert server.web_active
    assert len(server.journal) == journal_before + 1
    assert len(_local_journal(root)) == journal_before + 1
    assert server.journal_records()[-1]["success"] is True
    assert server.partial_archives() == []
    assert server.staging == set()


@pytest.mark.parametrize("event", EVENTS, ids=[f"{s}:{st}" for s, st in EVENTS])
def test_killed_at_boundary_then_rerun(tmp_path: Path, event: tuple[str, str]) -> None:
    server = FakeServer()
    with pytest.raises(Killed):
        _deploy(server, tmp_path, EventRecorder(kill_at=event))

    assert server.lock is None, "deploy lock is still held after the interruption"
    assert server.web_active, "dango-web was left stopped"
    assert len(server.journal) == 1, "journal must hold exactly one row for the interrupted deploy"
    assert len(_local_journal(tmp_path)) == 1
    # Killed while the finally block reports the restart: the deploy itself had succeeded.
    finished = event in {("start_services", "running"), ("start_services", "done")}
    assert server.journal_records()[0]["success"] is finished
    assert server.partial_archives() == []

    _assert_converges(server, tmp_path, journal_before=1)


@pytest.mark.parametrize(
    ("label", "rule"),
    [
        ("lock file written, client dies", KillRule("set -C; echo", when="after")),
        ("web stopped, client dies", KillRule("systemctl stop dango-web", when="after")),
        ("restart issued, client dies", KillRule("systemctl restart dango-web", when="after")),
        ("tar half written", KillRule("tar -czf", when="partial")),
        ("remote journal not written", KillRule(f">> {JOURNAL_PATH}", when="before")),
        ("remote journal written", KillRule(f">> {JOURNAL_PATH}", when="after")),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_killed_inside_a_command_then_rerun(tmp_path: Path, label: str, rule: KillRule) -> None:
    server = FakeServer()
    server.kill = rule
    with pytest.raises(Killed):
        _deploy(server, tmp_path)
    server.kill = None

    assert server.lock is None, f"{label}: deploy lock is still held"
    assert server.web_active, f"{label}: dango-web was left stopped"
    assert server.partial_archives() == [], f"{label}: partial archive left behind"
    assert len(server.journal) <= 1
    assert len(_local_journal(tmp_path)) == 1

    result = _deploy(server, tmp_path)
    assert result.models_rebuilt == ["stg_orders"]
    assert server.lock is None
    assert server.web_active
    assert server.journal_records()[-1]["success"] is True
    assert len(_local_journal(tmp_path)) == 2


def test_a_held_lock_is_not_taken_by_the_second_run_and_survives_a_refused_deploy(
    tmp_path: Path,
) -> None:
    """A deploy refused for a live foreign lock must not remove that lock."""
    from dango.exceptions import CloudProvisioningError

    server = FakeServer()
    _deploy(server, tmp_path)
    foreign = json.dumps(
        {
            "deployer": "someone@else",
            "started_at": "2999-01-01T00:00:00+00:00",
            "expires_at": "2999-01-01T00:30:00+00:00",
        }
    )
    server.lock = foreign
    with pytest.raises(CloudProvisioningError, match="Deploy lock held"):
        _deploy(server, tmp_path)
    assert server.lock == foreign


@pytest.mark.parametrize(
    "event",
    [
        ("create_backup", "running"),
        ("sync_files", "running"),
        ("dbt_compile", "running"),
        ("dbt_run", "done"),
    ],
    ids=lambda e: f"{e[0]}:{e[1]}",
)
def test_a_lost_connection_is_recoverable_with_force(
    tmp_path: Path, event: tuple[str, str]
) -> None:
    """The client dies with its SSH session: the server keeps the lock and stays stopped."""
    from dango.exceptions import CloudProvisioningError

    server = FakeServer()
    with pytest.raises((Killed, ConnectionLost)):
        _deploy(server, tmp_path, EventRecorder(kill_at=event, drop=server))
    server.reconnect()
    assert server.lock is not None, "a dead client cannot have released the lock"

    with pytest.raises(CloudProvisioningError, match="Deploy lock held"):
        _deploy(server, tmp_path)

    result = _deploy(server, tmp_path, force=True)
    assert result.models_rebuilt == ["stg_orders"]
    assert server.lock is None
    assert server.web_active
    assert server.journal_records()[-1]["success"] is True
    assert server.partial_archives() == []


def test_a_lock_written_just_before_the_acquire_returns_is_still_released(tmp_path: Path) -> None:
    """An interrupt between the lock write and push_deploy taking hold of the lock object."""
    from dango.platform.cloud import deployer

    real = deployer._acquire_lock

    def interrupted(ssh: Any, **kwargs: Any) -> Any:
        real(ssh, **kwargs)
        raise Killed("interrupt after the lock file was written")

    server = FakeServer()
    with patch.object(deployer, "_acquire_lock", interrupted), pytest.raises(Killed):
        _deploy(server, tmp_path)
    assert server.lock is None
    assert server.web_active
