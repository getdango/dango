"""tests/unit/test_root_compose_backup.py

Root-side Metabase stop/start/volume lookup in cloud/backup.py: the compose helper when the
marker exists (busy handling, order, timeouts), the unchanged legacy commands otherwise.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import backup
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.ssh import CommandResult
from tests.factories.root_compose_fakes import (
    FakeRootSSH,
    assert_no_project_compose_on_helper_branch,
)

pytestmark = pytest.mark.unit

_PROJECT = "dango-ab12cd34"
_LEGACY_STOP = (
    f"COMPOSE_PROJECT_NAME={_PROJECT} docker compose -f /srv/dango/project/docker-compose.yml "
    "stop metabase 2>/dev/null || true"
)
_LEGACY_START = _LEGACY_STOP.replace("stop metabase", "start metabase")
_LEGACY_VOLUME = (
    f"docker volume inspect {_PROJECT}_metabase-data --format '{{{{.Mountpoint}}}}' 2>/dev/null"
)


@pytest.fixture(autouse=True)
def _project_name():
    with patch.object(backup, "get_remote_compose_project_name", return_value=_PROJECT):
        yield


def _timeout_floor(verb: str) -> int:
    return ch.ROOT_LOCK_WAIT + ch.VERB_BUDGET[verb] + ch.CALLER_MARGIN


class TestHelperStop:
    def test_stops_metabase_before_and_after_dango_web(self) -> None:
        ssh = FakeRootSSH(helper=True)
        backup.stop_services(ssh)
        assert ssh.events == ["helper:stop metabase:0", "web:stop", "helper:stop metabase:0"]
        assert_no_project_compose_on_helper_branch(ssh)

    def test_busy_first_stop_aborts_with_nothing_stopped(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"stop metabase": 75})
        with pytest.raises(CloudProvisioningError, match="(?i)in progress"):
            backup.stop_services(ssh)
        assert "web:stop" not in ssh.events
        assert "web:start" not in ssh.events

    def test_busy_second_stop_puts_services_back_and_aborts(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"stop metabase": [0, 75]})
        with pytest.raises(CloudProvisioningError, match="(?i)in progress"):
            backup.stop_services(ssh)
        assert ssh.events.index("web:stop") < ssh.events.index("web:start")
        assert "helper:start metabase:0" in ssh.events

    def test_stop_timeouts_exceed_lock_wait_plus_budget(self) -> None:
        ssh = FakeRootSSH(helper=True)
        backup.stop_services(ssh)
        wrapped = [
            t for c, t in zip(ssh.commands, ssh.timeouts, strict=True) if "dango-compose" in c
        ]
        assert wrapped
        assert all(t is not None and t >= _timeout_floor("stop") for t in wrapped)


class TestHelperStart:
    def test_starts_metabase_then_dango_web(self) -> None:
        ssh = FakeRootSSH(helper=True)
        assert backup.start_services(ssh) is None
        assert ssh.events == ["web:start", "helper:start metabase:0"]
        assert_no_project_compose_on_helper_branch(ssh)

    def test_busy_start_still_starts_dango_web_and_reports(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"start metabase": 75})
        message = backup.start_services(ssh)
        assert ssh.events == ["web:start", "helper:start metabase:75"]
        assert message is not None and "dango-compose start metabase" in message

    def test_start_timeout_exceeds_lock_wait_plus_budget(self) -> None:
        ssh = FakeRootSSH(helper=True)
        backup.start_services(ssh)
        helper_timeouts = [
            t for c, t in zip(ssh.commands, ssh.timeouts, strict=True) if "dango-compose" in c
        ]
        assert helper_timeouts and helper_timeouts[0] >= _timeout_floor("start")


class TestCreateBackupBusy:
    def test_busy_stop_aborts_without_touching_services(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"stop metabase": 75})
        with (
            patch.object(backup, "_sweep_stale_leftovers"),
            patch.object(backup, "_check_disk_space"),
            pytest.raises(CloudProvisioningError),
        ):
            backup.create_backup(ssh)
        assert ssh.events == ["helper:stop metabase:75"]

    def test_busy_start_is_reported_as_a_warning(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"start metabase": 75})
        with (
            patch.object(backup, "_sweep_stale_leftovers"),
            patch.object(backup, "_check_disk_space"),
            patch.object(backup, "_checkpoint_duckdb", return_value=False),
            patch.object(backup, "_checkpoint_auth_db", return_value=False),
            patch.object(backup, "_checkpoint_sqlite", return_value=None),
            patch.object(backup, "_create_archive", return_value=("/b/x.tar.gz", {})),
            patch.object(backup, "rotate_local_backups"),
        ):
            result = backup.create_backup(ssh)
        assert any("dango-compose start metabase" in w for w in result.warnings)
        assert "web:start" in ssh.events


class TestVolumePath:
    def test_uses_the_helper_verb(self) -> None:
        ssh = FakeRootSSH(helper=True)
        assert backup._get_metabase_volume_path(ssh) == ssh.volume
        assert ssh.events == ["helper:volume-path:0"]
        assert_no_project_compose_on_helper_branch(ssh)

    def test_failed_lookup_is_none(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"volume-path": 1})
        assert backup._get_metabase_volume_path(ssh) is None


class TestLegacyUnchanged:
    def test_stop_legacy_command_and_order(self) -> None:
        ssh = FakeRootSSH(helper=False)
        backup.stop_services(ssh)
        assert ssh.events == ["web:stop", f"legacy:{_LEGACY_STOP}"]

    def test_start_legacy_command_and_order(self) -> None:
        ssh = FakeRootSSH(helper=False)
        assert backup.start_services(ssh) is None
        assert ssh.events == [f"legacy:{_LEGACY_START}", "web:start"]

    def test_volume_legacy_command(self) -> None:
        ssh = FakeRootSSH(helper=False)
        assert backup._get_metabase_volume_path(ssh) == ssh.volume
        assert ssh.events == [f"legacy:{_LEGACY_VOLUME}"]


class TestHelperFailuresAreFailures:
    """Any non-zero exit on the helper branch (not just busy) is a failure."""

    @pytest.mark.parametrize("code", [64, 67, 124, 126, 127])
    def test_first_stop_failure_aborts_and_restarts(self, code: int) -> None:
        ssh = FakeRootSSH(helper=True, rc={"stop metabase": code})
        with pytest.raises(backup.ComposeStopError):
            backup.stop_services(ssh)
        assert "web:stop" not in ssh.events
        assert "web:start" in ssh.events  # a timed-out stop may have completed

    @pytest.mark.parametrize("code", [64, 124, 127])
    def test_second_stop_failure_puts_services_back(self, code: int) -> None:
        ssh = FakeRootSSH(helper=True, rc={"stop metabase": [0, code]})
        with pytest.raises(backup.ComposeStopError):
            backup.stop_services(ssh)
        assert ssh.events.index("web:stop") < ssh.events.index("web:start")

    def test_failed_start_is_reported(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"start metabase": 127})
        assert backup.start_services(ssh) is not None
        assert "web:start" in ssh.events

    def test_busy_is_a_compose_stop_error(self) -> None:
        assert issubclass(backup.DockerBusyError, backup.ComposeStopError)

    def test_failed_stop_leaves_create_backup_with_services_running(self) -> None:
        ssh = FakeRootSSH(helper=True, rc={"stop metabase": 127})
        with (
            patch.object(backup, "_sweep_stale_leftovers"),
            patch.object(backup, "_check_disk_space"),
            pytest.raises(CloudProvisioningError),
        ):
            backup.create_backup(ssh)
        assert ssh.events[-1] != "web:stop"
        assert "web:stop" not in ssh.events


class TestStartOrderAndInterrupts:
    def test_second_stop_interrupt_still_starts_dango_web(self) -> None:
        class _Interrupting(FakeRootSSH):
            def exec_command(
                self, command: str, timeout: int | None = None, **kw: Any
            ) -> CommandResult:
                if "dango-compose start" in command and "web:stop" in self.events:
                    self.events.append("start-blocked")
                    raise SystemExit(1)
                return super().exec_command(command, timeout=timeout, **kw)

        ssh = _Interrupting(helper=True, rc={"stop metabase": [0, 75]})
        with pytest.raises(SystemExit):
            backup.stop_services(ssh)
        assert "web:start" in ssh.events
        assert ssh.events.index("web:start") < ssh.events.index("start-blocked")


class TestLegacyTimeouts:
    def test_stop_uses_the_legacy_timeouts(self) -> None:
        ssh = FakeRootSSH(helper=False)
        backup.stop_services(ssh)
        assert [t for t in ssh.timeouts if t is not None and t != 15] == [60, 60, 120]

    def test_start_uses_the_legacy_timeouts(self) -> None:
        ssh = FakeRootSSH(helper=False)
        backup.start_services(ssh)
        assert [t for t in ssh.timeouts if t != 15] == [120, 60]

    def test_volume_lookup_keeps_the_default_timeout(self) -> None:
        ssh = FakeRootSSH(helper=False)
        backup._get_metabase_volume_path(ssh)
        assert [t for t in ssh.timeouts if t != 15] == [None]

    def test_helper_branch_timeouts_are_exceeded_not_matched(self) -> None:
        ssh = FakeRootSSH(helper=True)
        backup.stop_services(ssh)
        backup.start_services(ssh)
        assert ssh.timeout_of("dango-compose start") == _timeout_floor("start")
