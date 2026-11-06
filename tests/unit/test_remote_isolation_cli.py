"""tests/unit/test_remote_isolation_cli.py

``dango remote isolation status|disable|enable``: confirmation, command order, idempotence, the
disabled marker staying until verify passes, and the audit events (once, failures only warn).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from dango.platform.cloud import runner_isolation as ri
from tests.factories.remote_isolation_fakes import (
    DROPPED_RECORD,
    IP,
    IsoSSH,
    invoke,
    order,
)

pytestmark = pytest.mark.unit


# --- status command -------------------------------------------------------------------------


class TestStatusCommand:
    def test_on(self) -> None:
        result = invoke(IsoSSH(), ["status"])
        assert result.exit_code == 0, result.output
        assert "Isolation: on" in result.output
        assert "Loopback: restricted" in result.output
        assert "disabled marker" in result.output.lower()

    def test_disabled(self) -> None:
        result = invoke(IsoSSH(disabled=True), ["status"])
        assert "Isolation: disabled" in result.output

    def test_disabled_shows_who_and_when(self) -> None:
        info = '{"schema": 1, "by": "alice via ssh root@h", "at": "2026-10-25T10:00:00+00:00"}'
        result = invoke(IsoSSH(disabled=True, disabled_info=info), ["status"])
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        out = " ".join(plain.split())
        assert "alice via ssh root@h" in out
        assert "2026-10-25T10:00:00+00:00" in out

    def test_missing(self) -> None:
        result = invoke(IsoSSH(active=False), ["status"])
        assert "Isolation: missing" in result.output
        assert "dango remote upgrade" in result.output

    def test_unknown_record_key_shows_unknown(self) -> None:
        result = invoke(IsoSSH(record=DROPPED_RECORD), ["status"])
        assert "Isolation: on" in result.output
        assert "unknown" in result.output

    def test_no_secrets_printed(self) -> None:
        record = json.dumps(
            {
                "dango": "1.0.14",
                "token": "s3cr3t-value",
                "runner_isolation": {"schema": 1, "loopback": "restricted"},
            }
        )
        result = invoke(IsoSSH(record=record), ["status"])
        assert "s3cr3t-value" not in result.output

    def test_changes_nothing(self) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["status"])
        assert ssh.events == []


# --- disable ---------------------------------------------------------------------------------


class TestDisable:
    def test_confirmation_required(self) -> None:
        ssh = IsoSSH()
        result = invoke(ssh, ["disable"], input="no\n")
        assert result.exit_code != 0
        assert "disable_marker" not in ssh.events
        assert ssh.audits == []

    def test_no_input_without_yes_aborts(self) -> None:
        ssh = IsoSSH()
        assert invoke(ssh, ["disable"]).exit_code != 0
        assert "disable_marker" not in ssh.events

    def test_typed_confirmation(self) -> None:
        ssh = IsoSSH()
        result = invoke(ssh, ["disable"], input="disable\n")
        assert result.exit_code == 0, result.output
        assert ssh.disabled

    def test_yes_flag(self) -> None:
        ssh = IsoSSH()
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code == 0, result.output
        assert ssh.disabled
        assert "disabled" in result.output.lower()

    def test_warning_names_what_is_exposed(self) -> None:
        out = invoke(IsoSSH(), ["disable", "--yes"]).output.lower()
        assert "secrets" in out and "backups" in out and "warehouse" in out

    def test_warning_helper_state_is_not_root_equivalent(self) -> None:
        out = " ".join(invoke(IsoSSH(docker_group=False), ["disable", "--yes"]).output.split())
        assert "root-equivalent" in out
        assert "not root-equivalent" in out

    def test_warning_group_fallback_is_root_equivalent(self) -> None:
        out = " ".join(invoke(IsoSSH(docker_group=True), ["disable", "--yes"]).output.split())
        assert "docker group" in out
        assert "not root-equivalent" not in out

    def test_command_order(self) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"])
        order(ssh, "guard:arm", "stop_web", "disable_marker", "quiesce", "web:start")
        assert ssh.events.index("ownership_check") < ssh.events.index("stop_web")
        assert ssh.events[-2:] == ["audit", "guard:disarm"] or "audit" in ssh.events

    def test_quiesce_does_not_stop_web_again(self) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"])
        quiesce = next(c for c in ssh.commands if "runner-step:quiesce" in c)
        assert "pkill -KILL" in quiesce
        assert "systemctl stop dango-web" not in quiesce

    def test_already_disabled_changes_nothing_but_sweeps_runner_processes(self) -> None:
        ssh = IsoSSH(disabled=True)
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code == 0
        assert "already" in result.output.lower()
        assert ssh.events == ["quiesce"]
        assert ssh.audits == []

    def test_quiesce_failure_retry_works(self) -> None:
        ssh = IsoSSH(fail={"quiesce": 8})
        first = invoke(ssh, ["disable", "--yes"])
        assert first.exit_code != 0
        assert "again" in first.output.lower()
        ssh.fail = {}
        before = ssh.events.count("quiesce")
        second = invoke(ssh, ["disable", "--yes"])
        assert second.exit_code == 0, second.output
        assert ssh.events.count("quiesce") == before + 1
        assert len(ssh.audits) == 1

    def test_already_disabled_with_stuck_runners_fails_again(self) -> None:
        ssh = IsoSSH(disabled=True, fail={"quiesce": 8})
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code != 0
        assert "runner processes" in result.output

    def test_marker_carries_who_and_when(self) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"])
        command = next(c for c in ssh.commands if "runner-step:disable_marker" in c)
        assert "alice via ssh root@203.0.113.1" in command
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", command)
        assert "SSH_CLIENT" in command
        assert "logger -t dango-isolation" in command

    def test_audit_carries_a_fresh_nonce(self) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"])
        assert len(ssh.nonces) == 1
        assert re.fullmatch(r"[0-9a-f]{32}", ssh.nonces[0])

    def test_refused_while_an_upgrade_guard_is_active(self) -> None:
        ssh = IsoSSH(guard_active=True)
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code != 0
        assert "upgrade" in result.output.lower()
        assert ssh.events == []
        assert not ssh.disabled

    def test_actor_uses_the_ssh_key_fingerprint(self, tmp_path: Path) -> None:
        blob = b"fake-public-key-blob"
        key = tmp_path / "cloud_key"
        key.with_suffix(".pub").write_text(
            f"ssh-ed25519 {base64.b64encode(blob).decode()} me@laptop\n"
        )
        digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"], key_path=str(key.with_suffix("")))
        actor = ssh.audits[0][1]
        assert f"SHA256:{digest}" in actor
        assert "alice" in actor and "root@203.0.113.1" in actor

    def test_actor_falls_back_without_a_public_key(self, tmp_path: Path) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"], key_path=str(tmp_path / "missing_key"))
        assert ssh.audits[0][1] == "alice via ssh root@203.0.113.1"

    def test_second_run_after_first_changes_nothing(self) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"])
        count = len(ssh.audits)
        invoke(ssh, ["disable", "--yes"])
        assert len(ssh.audits) == count == 1

    def test_works_when_isolation_missing(self) -> None:
        ssh = IsoSSH(active=False)
        assert invoke(ssh, ["disable", "--yes"]).exit_code == 0
        assert ssh.disabled

    def test_marker_failure_restarts_web_and_changes_nothing(self) -> None:
        ssh = IsoSSH(fail={"disable_marker": 1})
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code != 0
        assert not ssh.disabled
        assert ssh.events[ssh.events.index("disable_marker") :].count("web:start") == 1
        assert ssh.audits == []
        assert "last line" in result.output

    def test_stop_failure_restarts_web_and_writes_no_marker(self) -> None:
        ssh = IsoSSH(fail={"stop_web": 8})
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code != 0
        assert "disable_marker" not in ssh.events
        assert "web:start" in ssh.events

    def test_runner_kill_failure_is_reported_and_still_audited(self) -> None:
        ssh = IsoSSH(fail={"quiesce": 8})
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code != 0
        assert ssh.disabled
        assert "web:start" in ssh.events
        assert len(ssh.audits) == 1

    def test_services_not_started_if_they_were_not_running(self) -> None:
        ssh = IsoSSH(web=False)
        invoke(ssh, ["disable", "--yes"])
        assert "web:start" not in ssh.events

    def test_audit_written_once_with_fields(self) -> None:
        ssh = IsoSSH()
        invoke(ssh, ["disable", "--yes"])
        assert ssh.audits == [("isolation_disabled", "alice via ssh root@203.0.113.1", IP)]

    def test_audit_failure_only_warns(self) -> None:
        ssh = IsoSSH(audit_rc=1)
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code == 0, result.output
        assert ssh.disabled
        assert "audit" in result.output.lower() and "warning" in result.output.lower()

    def test_connection_failure(self) -> None:
        ssh = IsoSSH(connect_error=RuntimeError("refused"))
        result = invoke(ssh, ["disable", "--yes"])
        assert result.exit_code != 0
        assert "refused" in result.output


# --- enable ----------------------------------------------------------------------------------


class TestEnable:
    def test_success_order_marker_last_then_disabled_removed(self) -> None:
        ssh = IsoSSH(active=False, disabled=True)
        result = invoke(ssh, ["enable"])
        assert result.exit_code == 0, result.output
        assert not ssh.disabled and ssh.active
        order(ssh, "ownership_check", "verify", "marker", "remove_disabled", "web:start")
        assert ssh.events.index("marker") > ssh.events.index("verify")

    def test_uses_the_upgrade_steps(self) -> None:
        ssh = IsoSSH(active=False, disabled=True)
        invoke(ssh, ["enable"])
        ran = [e for e in ssh.events if e in {s.name for s in ri.build_steps()}]
        assert ran == [s.name for s in ri.build_steps()]

    def test_verify_failure_keeps_disabled_marker(self) -> None:
        ssh = IsoSSH(active=False, disabled=True, fail={"verify": 1})
        result = invoke(ssh, ["enable"])
        assert result.exit_code != 0
        assert ssh.disabled
        assert not ssh.active
        assert "remove_disabled" not in ssh.events
        assert "marker" not in ssh.events
        assert "verify" in result.output
        assert "web:start" in ssh.events
        assert ssh.audits == []

    def test_fatal_step_failure_keeps_state(self) -> None:
        ssh = IsoSSH(active=True, disabled=True, fail={"sudoers": 4})
        result = invoke(ssh, ["enable"])
        assert result.exit_code != 0
        assert ssh.disabled
        assert "remove_disabled" not in ssh.events

    def test_removal_failure_is_an_error(self) -> None:
        ssh = IsoSSH(active=False, disabled=True, fail={"remove_disabled": 1})
        result = invoke(ssh, ["enable"])
        assert result.exit_code != 0
        assert ssh.audits == []

    def test_audit_written_once(self) -> None:
        ssh = IsoSSH(active=False, disabled=True)
        invoke(ssh, ["enable"])
        assert ssh.audits == [("isolation_enabled", "alice via ssh root@203.0.113.1", IP)]

    def test_audit_failure_only_warns(self) -> None:
        ssh = IsoSSH(active=False, disabled=True, audit_rc=1)
        result = invoke(ssh, ["enable"])
        assert result.exit_code == 0
        assert not ssh.disabled
        assert "audit" in result.output.lower()

    def test_already_on_is_a_no_op(self) -> None:
        ssh = IsoSSH(active=True, disabled=False)
        result = invoke(ssh, ["enable"])
        assert result.exit_code == 0
        assert "already" in result.output.lower()
        assert ssh.events == []
        assert ssh.audits == []

    def test_enable_from_missing(self) -> None:
        ssh = IsoSSH(active=False, disabled=False)
        result = invoke(ssh, ["enable"])
        assert result.exit_code == 0, result.output
        assert ssh.active
        assert "remove_disabled" not in ssh.events or not ssh.disabled

    def test_refused_while_an_upgrade_guard_is_active(self) -> None:
        ssh = IsoSSH(active=False, disabled=True, guard_active=True)
        result = invoke(ssh, ["enable"])
        assert result.exit_code != 0
        assert "upgrade" in result.output.lower()
        assert ssh.events == []
        assert ssh.disabled

    def test_build_without_isolation_changes_nothing(self) -> None:
        ssh = IsoSSH(active=False, disabled=True, build=False)
        result = invoke(ssh, ["enable"])
        assert result.exit_code != 0
        assert ssh.disabled
        assert "marker" not in ssh.events
