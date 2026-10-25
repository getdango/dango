"""tests/unit/test_remote_isolation_safety.py

The probe and its parsing (state matrix), the ``Isolation:`` line of ``remote status``, root never
running code the service account can write, the audit snippet, registration and the audit events.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.auth.audit import AuditEvent
from dango.cli.commands import remote_isolation as ri_cli
from dango.cli.commands.remote import remote
from dango.platform.cloud import isolation_status as iso
from dango.platform.cloud import server_status as ss
from dango.platform.cloud.ssh import CommandResult
from tests.factories.remote_isolation_fakes import (
    DROPPED_RECORD,
    IP,
    VALID_RECORD,
    IsoSSH,
    invoke,
    probe,
)
from tests.unit.test_server_status import _make_cloud_config, _make_result, _make_ssh

pytestmark = pytest.mark.unit


# --- probe parsing and state ------------------------------------------------------------------


class TestStatusMatrix:
    @pytest.mark.parametrize(
        ("active", "disabled", "state"),
        [("1", "0", "on"), ("0", "1", "disabled"), ("1", "1", "disabled"), ("0", "0", "missing")],
    )
    def test_states(self, active: str, disabled: str, state: str) -> None:
        assert iso.parse_isolation_probe(probe(active, disabled)).state == state

    @pytest.mark.parametrize(
        "text", ["", "garbage", "active=1\n", "active=x\ndisabled=0\n", "---\n{}"]
    )
    def test_unreadable_probe_is_unknown(self, text: str) -> None:
        status = iso.parse_isolation_probe(text)
        assert status.state == "unknown"
        assert status.loopback == "unknown"

    def test_missing_record_key_is_unknown_not_off(self) -> None:
        status = iso.parse_isolation_probe(probe("1", "0", record=DROPPED_RECORD))
        assert status.state == "on"
        assert status.record == "unknown"

    def test_record_content_is_recorded(self) -> None:
        assert iso.parse_isolation_probe(probe()).record == "restricted"

    def test_garbled_record_is_unknown(self) -> None:
        status = iso.parse_isolation_probe(probe(record="{not json"))
        assert status.state == "on"
        assert status.record == "unknown"

    @pytest.mark.parametrize(
        ("nft", "record", "loopback"),
        [
            ("1", VALID_RECORD, "restricted"),
            ("0", VALID_RECORD, "unrestricted"),
            (None, VALID_RECORD, "restricted"),
            (None, DROPPED_RECORD, "unknown"),
        ],
    )
    def test_loopback(self, nft: str | None, record: str, loopback: str) -> None:
        assert iso.parse_isolation_probe(probe(nft=nft, record=record)).loopback == loopback

    def test_pieces(self) -> None:
        status = iso.parse_isolation_probe(probe(users="1", sudoers="0"))
        assert status.users == "1"
        assert status.sudoers is False
        assert status.active_marker is True
        assert status.disabled_marker is False

    @pytest.mark.parametrize(
        ("kwargs", "line"),
        [
            ({}, "on (loopback restricted)"),
            ({"nft": "0"}, "on (loopback unrestricted)"),
            ({"nft": None, "record": DROPPED_RECORD}, "on (loopback unknown)"),
            ({"active": "0", "disabled": "1"}, "disabled (admin opt-out)"),
            ({"active": "1", "disabled": "1"}, "disabled (admin opt-out)"),
            ({"active": "0"}, "NOT enabled: run dango remote upgrade"),
            ({"active": None}, "unknown"),
        ],
    )
    def test_line_variants(self, kwargs: dict[str, Any], line: str) -> None:
        assert iso.format_isolation_line(iso.parse_isolation_probe(probe(**kwargs))) == line

    def test_probe_failure_is_unknown(self) -> None:
        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult("", "boom", 1)
        assert iso.get_isolation(ssh).state == "unknown"

    def test_probe_exception_is_unknown(self) -> None:
        from dango.exceptions import CloudSSHError

        ssh = MagicMock()
        ssh.exec_command.side_effect = CloudSSHError("down")
        assert iso.get_isolation(ssh).state == "unknown"


class TestProbeCommand:
    def test_marker_rules_match_run_as(self) -> None:
        cmd = iso.ISOLATION_PROBE_CMD
        for needle in (
            "-maxdepth 0",
            "-type",
            "-uid",
            "! -perm /022",
            "R=$S/runner",
            "$R/active",
            "$R/disabled",
        ):
            assert needle in cmd

    def test_reads_only_root_owned_things(self) -> None:
        assert "/srv/dango/project" not in iso.ISOLATION_PROBE_CMD
        assert "source " not in iso.ISOLATION_PROBE_CMD

    def test_is_valid_shell(self) -> None:
        assert (
            subprocess.run(["sh", "-n", "-c", iso.ISOLATION_PROBE_CMD], check=False).returncode == 0
        )

    def test_collect_server_status_has_the_line(self) -> None:
        ssh = _make_ssh({"echo active=": _make_result(stdout=probe())})
        assert (
            ss.collect_server_status(ssh, _make_cloud_config()).isolation
            == "on (loopback restricted)"
        )

    def test_collect_server_status_default_is_unknown(self) -> None:
        assert ss.collect_server_status(_make_ssh(), _make_cloud_config()).isolation == "unknown"


# --- root never runs dango-writable code ----------------------------------------------------


class TestRootSafety:
    def _all_commands(self) -> list[str]:
        out: list[str] = []
        for ssh, args in (
            (IsoSSH(), ["disable", "--yes"]),
            (IsoSSH(active=False, disabled=True), ["enable"]),
            (IsoSSH(), ["status"]),
        ):
            invoke(ssh, args)
            out.extend(ssh.commands)
        return out

    def test_project_paths_only_through_the_service_user(self) -> None:
        for command in self._all_commands():
            if "/srv/dango/project" in command or "/home/dango" in command:
                assert "runuser -u dango --" in command, command

    def test_root_never_executes_from_the_project(self) -> None:
        for command in self._all_commands():
            assert "/srv/dango/project/.dango/" not in command or "runuser -u dango --" in command
            assert "source /srv" not in command
            assert ". /srv/dango/project" not in command

    def test_marker_scripts_touch_only_the_runner_directory(self) -> None:
        for script in (ri_cli.disable_marker_script(), ri_cli.remove_disabled_script()):
            assert "/srv/dango/runner" in script
            assert "/srv/dango/project" not in script
            assert subprocess.run(["sh", "-n", "-c", script], check=False).returncode == 0

    def test_disabled_marker_is_root_owned_atomic_0644(self) -> None:
        script = ri_cli.disable_marker_script()
        assert "chown root:root" in script
        assert "chmod 0644" in script
        assert "mv -f" in script
        assert "-L" in script  # symlink refusal

    def test_marker_path_matches_run_as(self) -> None:
        from dango.platform.run_as import DISABLED_MARKER

        assert DISABLED_MARKER in ri_cli.disable_marker_script()
        assert DISABLED_MARKER in ri_cli.remove_disabled_script()

    def test_audit_command_runs_as_service_user_with_isolated_python(self) -> None:
        command = ri_cli.audit_command(
            AuditEvent.ISOLATION_DISABLED, "alice via ssh root@h", "h", "n0nce"
        )
        assert command.startswith("cd / && runuser -u dango -- ")
        assert " -I -c " in command
        assert "isolation_disabled" in command

    def test_audit_source_runs_and_writes(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        src = ri_cli.AUDIT_SOURCE.replace(ri_cli.AUDIT_LOG_DIR, str(log_dir))
        env = {
            **os.environ,
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            "HOME": str(tmp_path),
        }
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                src,
                "isolation_enabled",
                "alice via ssh root@h",
                "h",
                "n1",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        entry = json.loads((log_dir / "audit.jsonl").read_text().splitlines()[-1])
        assert entry["event"] == "isolation_enabled"
        assert entry["details"]["actor"] == "alice via ssh root@h"
        assert entry["details"]["server"] == "h"

    def test_audit_source_exits_nonzero_when_it_cannot_write(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        src = ri_cli.AUDIT_SOURCE.replace(ri_cli.AUDIT_LOG_DIR, str(blocker / "logs"))
        env = {
            **os.environ,
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            "HOME": str(tmp_path),
        }
        proc = subprocess.run(
            [sys.executable, "-I", "-c", src, "isolation_enabled", "a", "h", "n1"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode != 0


# --- remote status line, registration, audit events ---------------------------------------


class TestRemoteStatusLine:
    @pytest.mark.parametrize(
        "line",
        [
            "on (loopback restricted)",
            "disabled (admin opt-out)",
            "NOT enabled: run dango remote upgrade",
            "unknown",
        ],
    )
    def test_line_is_printed(self, line: str) -> None:
        cfg = MagicMock(droplet_ip=IP, provider="byos", domain="x.example")
        server = ss.ServerStatus(isolation=line)
        with (
            patch(
                "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
                return_value=(cfg, Path("/project")),
            ),
            patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=IsoSSH()),
            patch("dango.platform.cloud.server_status.collect_server_status", return_value=server),
            patch(
                "dango.platform.cloud.server_status.check_latest_pypi_version", return_value=None
            ),
        ):
            result = CliRunner().invoke(remote, ["status"])
        assert result.exit_code == 0, result.output
        assert f"Isolation: {line}" in result.output

    def test_default_field_is_unknown(self) -> None:
        assert ss.ServerStatus().isolation == "unknown"


class TestRegistration:
    def test_group_in_remote_help(self) -> None:
        result = CliRunner().invoke(remote, ["--help"])
        assert "isolation" in result.output

    def test_subcommands(self) -> None:
        result = CliRunner().invoke(remote, ["isolation", "--help"])
        assert result.exit_code == 0
        for name in ("status", "disable", "enable"):
            assert name in result.output

    def test_disable_help_mentions_yes(self) -> None:
        assert "--yes" in CliRunner().invoke(remote, ["isolation", "disable", "--help"]).output

    def test_audit_events_exist(self) -> None:
        assert AuditEvent.ISOLATION_DISABLED.value == "isolation_disabled"
        assert AuditEvent.ISOLATION_ENABLED.value == "isolation_enabled"
        assert len(set(AuditEvent)) == len(list(AuditEvent))
