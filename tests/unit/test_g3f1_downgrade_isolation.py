"""tests/unit/test_g3f1_downgrade_isolation.py

After a downgrade below the isolation release the server must not claim isolation: the marker is
removed, the result and ``remote status`` say it is not enforced, and an upgrade turns it back on.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud import isolation_status as iso
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.remote_isolation_fakes import IsoSSH, invoke, probe
from tests.factories.runner_isolation_fakes import RunnerServer

pytestmark = pytest.mark.unit

MARKER = json.dumps({"schema": 1, "loopback": "restricted"})


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


def isolated(version: str = "1.0.14") -> RunnerServer:
    """A server on the compose helper with runner isolation switched on."""
    srv = RunnerServer(version=version, marker=True, in_group=False)
    srv.isolation_marker = MARKER
    srv.record = json.dumps(
        {
            "dango": version,
            "docker_access": "helper",
            "runner_isolation": {"schema": 1, "loopback": "restricted"},
        }
    )
    return srv


# --- the downgrade ------------------------------------------------------------------------------


def test_downgrade_removes_the_active_marker_and_says_so() -> None:
    srv = isolated()
    result = upgrade_dango(srv, version="1.0.13", force=True)
    assert srv.isolation_marker is None
    line = (result.runner_isolation or "").lower()
    assert "not enforced" in line and "1.0.13" in line and "1.0.14" in line
    assert "enabled" not in line.replace("not enabled", "")
    assert result.health_check_passed and "web-start" in srv.events


def test_downgrade_drops_the_isolation_key_from_the_server_record() -> None:
    srv = isolated()
    upgrade_dango(srv, version="1.0.13", force=True)
    assert "runner_isolation" not in json.loads(srv.record or "{}")


def test_downgrade_removes_only_the_marker() -> None:
    srv = isolated()
    upgrade_dango(srv, version="1.0.13", force=True)
    destructive = ("userdel", "setfacl -b", "dango-runner", "nft delete", "rm -rf")
    assert not [c for c in srv.commands if any(d in c for d in destructive)]
    assert "marker" not in srv.runner_events()


def test_upgrade_back_turns_isolation_on_through_the_normal_steps() -> None:
    srv = isolated()
    upgrade_dango(srv, version="1.0.13", force=True)
    assert srv.isolation_marker is None
    result = upgrade_dango(srv, version="1.0.14")
    assert result.runner_isolation == "Runner isolation enabled"
    assert srv.isolation_marker is not None
    assert json.loads(srv.record or "{}")["runner_isolation"]["loopback"] == "restricted"


def test_a_downgrade_that_stopped_after_pip_converges_on_the_rerun() -> None:
    """The server already runs 1.0.13 but still has the marker (interrupted or manual downgrade)."""
    srv = isolated("1.0.13")
    srv.marker, srv.in_group = False, True
    result = upgrade_dango(srv, version="1.0.13")
    assert srv.isolation_marker is None
    assert "not enforced" in (result.runner_isolation or "").lower()


def test_a_downgrade_without_a_marker_has_nothing_to_report() -> None:
    srv = RunnerServer(version="1.0.14", marker=True, in_group=False)
    result = upgrade_dango(srv, version="1.0.13", force=True)
    assert result.runner_isolation is None
    assert srv.runner_events() == []


def test_a_failed_removal_is_loud_and_keeps_the_downgrade_going() -> None:
    srv = isolated()
    srv.rfail = {"ownership_check": 3}
    result = upgrade_dango(srv, version="1.0.13", force=True)
    assert srv.isolation_marker is not None
    text = " ".join([result.runner_isolation or "", *result.warnings]).lower()
    assert "not enforce" in text and "1.0.13" in text and "ownership_check" in text
    assert result.health_check_passed and "web-start" in srv.events


# --- what status says -----------------------------------------------------------------------------


def _parsed(installed: str | None, active: str = "1", disabled: str = "0") -> iso.IsolationStatus:
    text = probe(active=active, disabled=disabled)
    if installed is not None:
        text = text.replace("sudoers=1\n", f"sudoers=1\ninstalled={installed}\n")
    return iso.parse_isolation_probe(text)


@pytest.mark.parametrize("active", ["1", "0"])
def test_status_reads_an_old_installed_version_as_not_enforced(active: str) -> None:
    status = _parsed("1.0.13", active=active)
    assert status.state == "unsupported"
    line = iso.format_isolation_line(status).lower()
    assert "not enforced" in line and "1.0.13" in line
    assert not line.startswith("on")


@pytest.mark.parametrize("installed", ["1.0.14", "1.0.14rc1", "1.0.15", None])
def test_status_is_on_when_the_installed_version_enforces_isolation(installed: str | None) -> None:
    status = _parsed(installed)
    assert status.state == "on"
    assert iso.format_isolation_line(status).startswith("on (loopback")


def test_status_keeps_the_opt_out_over_the_version() -> None:
    assert _parsed("1.0.13", disabled="1").state == "disabled"


@pytest.mark.parametrize("junk", ["../x", "1.0.13 active=1", "not-a-version", ""])
def test_status_ignores_an_installed_value_that_is_not_a_version(junk: str) -> None:
    assert _parsed(junk).state == "on"


def test_the_probe_reads_the_installed_version_as_data() -> None:
    cmd = iso.ISOLATION_PROBE_CMD
    assert "getdango-*.dist-info" in cmd and "installed=" in cmd
    assert "venv/bin/python" not in cmd  # the interpreter is never run to find out


def test_get_isolation_carries_the_installed_version() -> None:
    class Ssh(IsoSSH):
        def _probe(self) -> str:
            return super()._probe().replace("sudoers=1\n", "sudoers=1\ninstalled=1.0.13\n")

    status = iso.get_isolation(Ssh())
    assert status.state == "unsupported" and status.installed == "1.0.13"


def test_isolation_status_command_names_the_cause_and_the_fix() -> None:
    class Ssh(IsoSSH):
        def _probe(self) -> str:
            return super()._probe().replace("sudoers=1\n", "sudoers=1\ninstalled=1.0.13\n")

    ssh = Ssh()
    result = invoke(ssh, ["status"])
    assert result.exit_code == 0, result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "Isolation: on" not in plain
    assert "1.0.13" in plain and "dango remote upgrade" in plain
    assert ssh.events == []


def test_isolation_enable_is_refused_on_a_build_without_isolation() -> None:
    class Ssh(IsoSSH):
        def _probe(self) -> str:
            return super()._probe().replace("sudoers=1\n", "sudoers=1\ninstalled=1.0.13\n")

    ssh = Ssh(active=False, build=False)
    result = invoke(ssh, ["enable"])
    assert result.exit_code != 0
    assert ri.ISOLATION_MARK in result.output and ssh.events == []
