"""tests/unit/test_upgrade_switch.py

`remote upgrade` with the compose helper: migration order, rebuild through the helper, fallback, downgrade
restore, guard coverage of the two Metabase stops, and the client/server version notices.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.backup import DockerBusyError
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit

WEB_UNIT = "/etc/systemd/system/dango-web.service"


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


def run(srv: FakeServer, version: str, **kw: Any) -> Any:
    return upgrade_dango(srv, version=version, **kw)


def index(srv: FakeServer, event: str, start: int = 0) -> int:
    return next(i for i, e in enumerate(srv.events) if i >= start and e == event)


def test_upgrade_to_the_helper_release_switches_docker_access_before_the_rebuild() -> None:
    srv = FakeServer(version="1.0.13")
    result = run(srv, "1.0.14")
    ev = srv.events
    assert srv.marker and not srv.in_group
    pip, h1, t = index(srv, "pip"), index(srv, "H1"), index(srv, "T")
    pull, build = index(srv, "helper:pull"), index(srv, "helper:up-build")
    web_stop = index(srv, "web-stop")
    assert web_stop < pip < h1 < index(srv, "G") < t < pull < build < ev.index("web-start", build)
    assert result.docker_rebuilt is True
    assert result.warnings == []


def test_upgrade_rebuild_runs_through_the_helper_with_the_legacy_command_as_fallback() -> None:
    srv = FakeServer(version="1.0.13")
    run(srv, "1.0.14")
    cmd = next(c for c in srv.commands if "dango-compose pull" in c)
    assert "dango-compose up-build" in cmd
    for then in re.findall(r"then (.*?); else", cmd, re.S):
        assert "/srv/dango/project" not in then and "sudo -u dango" not in then
    assert "docker compose -f /srv/dango/project/docker-compose.yml pull" in cmd
    assert "up -d --build metabase </dev/null" in cmd


def test_rebuild_timeout_covers_the_helper_waits() -> None:
    srv = FakeServer(version="1.0.13")
    run(srv, "1.0.14")
    i = next(i for i, c in enumerate(srv.commands) if "dango-compose pull" in c)
    assert (srv.timeouts[i] or 0) >= ch.ROOT_LOCK_WAIT + ch.VERB_BUDGET["pull"] + ch.VERB_BUDGET[
        "up-build"
    ]


def test_ownership_script_covers_the_helper_files() -> None:
    srv = FakeServer(version="1.0.13")
    run(srv, "1.0.14")
    assert any("# compose_helper" in c and ch.SUDOERS_PATH in c for c in srv.commands)


def test_the_web_unit_is_refreshed_before_dango_web_starts() -> None:
    srv = FakeServer(version="1.0.13")
    run(srv, "1.0.14")
    assert "TimeoutStopSec=" in srv.written[WEB_UNIT]
    assert srv.events.index(f"write:{WEB_UNIT}") < index(srv, "web-start", index(srv, "pip"))


def test_failed_migration_keeps_the_old_path_and_rebuilds_the_legacy_way() -> None:
    srv = FakeServer(version="1.0.13", fail={"H3": 1})
    result = run(srv, "1.0.14")
    assert not srv.marker and srv.in_group
    assert "legacy:pull" in srv.events and "legacy:up-build" in srv.events
    assert "helper:pull" not in srv.events
    assert any("group" in w.lower() for w in result.warnings)
    assert "web-start" in srv.events


def test_record_is_written_at_the_end_of_the_upgrade() -> None:
    srv = FakeServer(version="1.0.13")
    run(srv, "1.0.14")
    assert srv.record == '{"dango": "1.0.14", "docker_access": "helper"}'


def test_older_target_does_not_migrate() -> None:
    srv = FakeServer(version="1.0.12")
    run(srv, "1.0.13")
    assert "H1" not in srv.events and not srv.marker and "R" not in srv.events


def test_downgrade_below_the_helper_release_restores_docker_access() -> None:
    srv = FakeServer(version="1.0.14", marker=True, in_group=False)
    run(srv, "1.0.13", force=True)
    ev = srv.events
    assert srv.in_group and not srv.marker
    assert "H1" not in ev and "G" not in ev
    assert index(srv, "pip") < index(srv, "R") < index(srv, "legacy:pull")
    assert srv.record == '{"dango": "1.0.13", "docker_access": "group"}'
    assert "web-start" in ev[index(srv, "R") :]


def test_downgrade_between_helper_releases_keeps_the_helper() -> None:
    srv = FakeServer(version="1.0.15", marker=True, in_group=False)
    srv.record = '{"dango": "1.0.15", "docker_access": "helper"}'
    run(srv, "1.0.14", force=True)
    assert "R" not in srv.events and srv.marker


def test_at_target_with_settled_helper_touches_nothing() -> None:
    srv = FakeServer(version="1.0.14", marker=True, in_group=False)
    srv.record = '{"dango": "1.0.14", "docker_access": "helper"}'
    result = run(srv, "1.0.14")
    assert "web-stop" not in srv.events and "T" not in srv.events and "H1" not in srv.events
    assert result.new_version == "1.0.14"


def test_at_target_unmigrated_server_is_switched_with_dango_web_stopped_for_the_process_drop() -> (
    None
):
    srv = FakeServer(version="1.0.14", marker=False, in_group=True)
    run(srv, "1.0.14")
    ev = srv.events
    assert srv.marker and not srv.in_group
    assert ev.index("web-stop") < ev.index("G") < ev.index("T") < ev.index("helper:pull")
    assert ev.index("helper:up-build") < len(ev) - 1 - ev[::-1].index("web-start")


def test_at_target_failure_leaves_dango_web_running_on_the_old_path() -> None:
    srv = FakeServer(version="1.0.14", marker=False, in_group=True, fail={"P2": 1})
    result = run(srv, "1.0.14")
    assert not srv.marker and srv.in_group
    assert "web-stop" not in srv.events  # dango-web keeps running on the old path
    assert any("Already at target" in w for w in result.warnings)


def test_guard_is_rearmed_between_the_two_metabase_stops() -> None:
    srv = FakeServer(version="1.0.15", marker=True, in_group=False)
    srv.record = '{"dango": "1.0.15", "docker_access": "helper"}'
    run(srv, "1.0.16")
    ev = srv.events
    first = index(srv, "helper:stop")
    web = index(srv, "web-stop")
    second = index(srv, "helper:stop", first + 1)
    assert "guard-arm" in ev[first:web]
    assert web < second


def test_busy_first_stop_aborts_and_disarms_the_guard() -> None:
    srv = FakeServer(version="1.0.15", marker=True, in_group=False, stop_rc=[75])
    with pytest.raises(DockerBusyError):
        run(srv, "1.0.16")
    assert "pip" not in srv.events and "web-stop" not in srv.events
    assert srv.events[-1] == "guard-disarm"


def test_busy_second_stop_starts_services_once_and_disarms_the_guard() -> None:
    srv = FakeServer(version="1.0.15", marker=True, in_group=False, stop_rc=[0, 75])
    with pytest.raises(DockerBusyError):
        run(srv, "1.0.16")
    assert srv.events.count("web-start") == 1
    assert "pip" not in srv.events
    assert srv.events[-1] == "guard-disarm"


def test_older_client_is_told_to_upgrade_first() -> None:
    srv = FakeServer(version="1.0.13")
    with patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.13"):
        result = run(srv, "1.0.14")
    assert "Upgrade your local Dango" in result.warnings[0]


def test_current_client_gets_no_upgrade_line() -> None:
    srv = FakeServer(version="1.0.13")
    result = run(srv, "1.0.14")
    assert not any("Upgrade your local Dango" in w for w in result.warnings)


def test_client_older_than_the_recorded_server_version_is_warned() -> None:
    srv = FakeServer(version="1.0.20", marker=True, in_group=False)
    srv.record = '{"dango": "1.0.20", "docker_access": "helper"}'
    result = run(srv, "1.0.20")
    assert any("upgraded by Dango 1.0.20" in w for w in result.warnings)


def test_at_target_refreshes_an_old_web_unit_without_stopping_dango_web() -> None:
    srv = FakeServer(version="1.0.14", marker=True, in_group=False)
    srv.record = '{"dango": "1.0.14", "docker_access": "helper"}'
    srv.web_unit = "[Service]\nExecStart=/srv/dango/venv/bin/dango serve --host 127.0.0.1\n"
    run(srv, "1.0.14")
    assert "TimeoutStopSec=" in srv.written[WEB_UNIT]
    assert "web-stop" not in srv.events


# --- review round 1 ---------------------------------------------------------------------------------


def switched(version: str = "1.0.14", **kw: Any) -> FakeServer:
    srv = FakeServer(version=version, marker=True, in_group=False, **kw)
    srv.record = f'{{"dango": "{version}", "docker_access": "helper"}}'
    return srv


def test_a_failed_rerun_on_a_switched_server_never_adds_the_group() -> None:
    srv = switched("1.0.13", pinned="0123456789abcdef0123456789abcdef", fail={"P2": 65})
    result = run(srv, "1.0.14")
    assert srv.marker and not srv.in_group
    assert "FB" not in srv.events and "usermod" not in srv.events
    assert any("marker is kept" in w for w in result.warnings)


def test_ssh_error_while_stopping_leaves_the_guard_armed() -> None:
    from dango.exceptions import CloudSSHError

    srv = switched("1.0.15", raise_on={"systemctl stop dango-web": CloudSSHError("timed out")})
    with pytest.raises(CloudSSHError):
        run(srv, "1.0.16")
    assert srv.events[-1] != "guard-disarm"
    assert "guard-arm" in srv.events


def test_ssh_error_stopping_dango_web_in_the_at_target_switch_leaves_the_guard_armed() -> None:
    from dango.exceptions import CloudSSHError

    srv = FakeServer(
        version="1.0.14",
        marker=False,
        in_group=True,
        raise_on={"systemctl stop dango-web": CloudSSHError("timed out")},
    )
    with pytest.raises(CloudSSHError):
        run(srv, "1.0.14")
    assert srv.events[-1] != "guard-disarm"
    assert "web-start" in srv.events  # the migration still tries to bring dango-web back


def test_an_interrupted_downgrade_converges_on_the_next_run() -> None:
    srv = switched("1.0.13")  # old code installed, the restore never ran
    run(srv, "1.0.13", force=True)
    assert "R" in srv.events and srv.in_group and not srv.marker
    assert srv.record == '{"dango": "1.0.13", "docker_access": "group"}'


def test_at_target_restore_stops_dango_web_and_starts_it_again() -> None:
    srv = switched("1.0.13")
    run(srv, "1.0.13", force=True)
    ev = srv.events
    assert ev.index("web-stop") < ev.index("R") < len(ev) - 1 - ev[::-1].index("web-start")


def test_the_restore_runs_right_after_pip_before_the_steps_that_can_fail() -> None:
    srv = switched("1.0.14")
    with patch(
        "dango.platform.cloud.runtime_ownership.secure_runtime", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError):
            run(srv, "1.0.13", force=True)
    assert srv.in_group and not srv.marker and "R" in srv.events
    migrations = next(i for i, c in enumerate(srv.commands) if "apply_all_pending" in c)
    restore = next(i for i, c in enumerate(srv.commands) if "# step:R" in c)
    assert restore < migrations


def test_a_server_that_never_had_the_helper_is_not_restored() -> None:
    srv = FakeServer(version="1.0.13", marker=False, in_group=True)
    run(srv, "1.0.13", force=True)
    assert "R" not in srv.events


def test_stop_and_restart_of_dango_web_get_an_ssh_timeout_above_the_unit_stop_timeout() -> None:
    srv = FakeServer(version="1.0.13")
    run(srv, "1.0.14")
    stops = [
        t
        for c, t in zip(srv.commands, srv.timeouts, strict=True)
        if c == "systemctl stop dango-web"
    ]
    assert stops and all((t or 0) >= 190 for t in stops)
