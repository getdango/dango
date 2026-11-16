"""tests/unit/test_stop_services_guard_hook.py

`stop_services` can report the point between its two Metabase stops (so `remote upgrade` re-arms its
guard there), and a busy stop never causes a second slow `start_services` in restore.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud import backup
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    with patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]):
        yield


def test_hook_runs_between_the_two_stops() -> None:
    srv = FakeServer(marker=True, in_group=False)
    seen: list[int] = []
    backup.stop_services(srv, on_first_stop=lambda: seen.append(len(srv.events)))
    first = srv.events.index("helper:stop")
    assert seen and seen[0] == first + 1
    assert srv.events.index("web-stop") >= seen[0]
    assert srv.events.count("helper:stop") == 2


def test_hook_is_not_called_when_the_first_stop_fails() -> None:
    srv = FakeServer(marker=True, in_group=False, stop_rc=[75])
    seen: list[int] = []
    with pytest.raises(backup.DockerBusyError):
        backup.stop_services(srv, on_first_stop=lambda: seen.append(1))
    assert seen == []


def test_hook_is_optional_and_legacy_servers_call_it_too() -> None:
    srv = FakeServer(marker=False, in_group=True)
    seen: list[int] = []
    backup.stop_services(srv, on_first_stop=lambda: seen.append(1))
    backup.stop_services(srv)
    assert seen == [1]


def restore(srv: FakeServer) -> None:
    with (
        patch("dango.platform.cloud.backup.extract_checked_archive"),
        patch(
            "dango.platform.cloud.backup.create_backup", return_value=MagicMock(archive_path="/s")
        ),
        patch("dango.platform.cloud.backup._remove_leftovers"),
    ):
        backup.restore_from_archive(srv, "/srv/dango/backups/deploy/a.tar.gz")


def test_restore_with_a_busy_first_stop_does_not_wait_for_a_second_start() -> None:
    srv = FakeServer(marker=True, in_group=False, stop_rc=[0, 75])
    srv.events.clear()
    with pytest.raises(backup.DockerBusyError):
        restore(srv)
    # create_backup is patched, so the only stops are restore's own: the first succeeds, the second is busy;
    # stop_services itself put the services back, restore must not start them a second time.
    assert srv.events.count("web-start") == 1


def test_restore_with_a_failed_stop_leaves_cleanup_but_no_second_start() -> None:
    srv = FakeServer(marker=True, in_group=False, stop_rc=[75])
    with pytest.raises(backup.DockerBusyError):
        restore(srv)
    assert srv.events.count("web-start") == 0
