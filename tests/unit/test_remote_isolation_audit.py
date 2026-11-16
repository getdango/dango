"""tests/unit/test_remote_isolation_audit.py

The durable record of an opt-out: who and when inside the root-owned marker, a best-effort journal
line, an audit read-back that cannot pass on a stale line, and the status probe that shows it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dango.cli.commands import remote_isolation as ri_cli
from dango.platform.cloud import isolation_status as iso
from tests.factories.remote_isolation_fakes import IsoSSH, probe

pytestmark = pytest.mark.unit

_AT = "2026-10-25T10:00:00+00:00"
_INFO = json.dumps({"schema": 1, "by": "alice via ssh root@h", "at": _AT})


def _script(by: str = "alice", at: str = _AT, server: str = "h") -> str:
    return ri_cli.disable_marker_script(by=by, at=at, server=server)


class TestMarkerRecord:
    def test_script_is_valid_shell_and_keeps_the_marker_rules(self) -> None:
        script = _script()
        assert subprocess.run(["sh", "-n", "-c", script], check=False).returncode == 0
        for needle in ("chown root:root", "chmod 0644", "mv -f", "-L"):
            assert needle in script

    def test_hostile_actor_stays_on_one_json_line(self) -> None:
        script = _script(by='eve"; touch /tmp/pwn\n{"event":"x"}')
        assert "/tmp/pwn\n" not in script
        line = next(ln for ln in script.splitlines() if "eve" in ln and "printf" in ln)
        assert "\n" not in line

    def test_content_is_bounded_json_with_client(self, tmp_path: Path) -> None:
        script = _script(by="a" * 5000, server="s" * 5000)
        out = tmp_path / "marker"
        lines = script.splitlines()
        wanted = [ln for ln in lines if ln.startswith(("S_C=", "C=")) or "printf '%s, " in ln]
        assert len(wanted) == 3, wanted
        body = "\n".join(wanted).replace("{tmp}", str(out))
        body = re.sub(r">\s*\S*\.disabled\.tmp", f">{out}", body)
        env = {**os.environ, "SSH_CLIENT": "198.51.100.7 5555 22"}
        proc = subprocess.run(
            ["sh", "-eu", "-c", body], env=env, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
        raw = out.read_text()
        assert raw.endswith("\n") and raw.count("\n") == 1 and len(raw) < 1024
        data = json.loads(raw)
        assert data["schema"] == 1
        assert data["client"] == "198.51.100.7"
        assert data["at"] == _AT
        assert len(data["by"]) <= 200

    def test_client_is_sanitised_when_ssh_client_is_unset(self, tmp_path: Path) -> None:
        script = _script()
        out = tmp_path / "marker"
        wanted = [
            ln
            for ln in script.splitlines()
            if ln.startswith(("S_C=", "C=")) or "printf '%s, " in ln
        ]
        body = re.sub(r">\s*\S*\.disabled\.tmp", f">{out}", "\n".join(wanted))
        env = {k: v for k, v in os.environ.items() if k != "SSH_CLIENT"}
        proc = subprocess.run(
            ["sh", "-eu", "-c", body], env=env, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(out.read_text())["client"] == ""

    def test_journal_line_is_best_effort(self) -> None:
        script = _script()
        line = next(ln for ln in script.splitlines() if "logger" in ln)
        assert "logger -t dango-isolation" in line
        assert line.rstrip().endswith("|| true")
        assert script.index("mv -f") < script.index("logger")

    def test_without_attribution_the_old_record_is_written(self) -> None:
        assert '{"schema": 1}' in ri_cli.disable_marker_script()


class TestAuditNonce:
    def _run(self, log_dir: Path, nonce: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        src = ri_cli.AUDIT_SOURCE.replace(ri_cli.AUDIT_LOG_DIR, str(log_dir))
        env = {
            **os.environ,
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            "HOME": str(tmp_path),
        }
        return subprocess.run(
            [sys.executable, "-I", "-c", src, "isolation_disabled", "alice", "h", nonce],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_nonce_is_written_and_read_back(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        assert self._run(log_dir, "abc123", tmp_path).returncode == 0
        entry = json.loads((log_dir / "audit.jsonl").read_text().splitlines()[-1])
        assert entry["details"]["nonce"] == "abc123"

    def test_stale_identical_line_does_not_pass(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        assert self._run(log_dir, "first", tmp_path).returncode == 0
        log = log_dir / "audit.jsonl"
        log.chmod(0o444)
        log_dir.chmod(0o555)
        try:
            proc = self._run(log_dir, "second", tmp_path)
        finally:
            log_dir.chmod(0o755)
            log.chmod(0o644)
        assert proc.returncode != 0

    def test_a_concurrent_line_after_ours_still_passes(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        assert self._run(log_dir, "mine", tmp_path).returncode == 0
        log = log_dir / "audit.jsonl"
        with log.open("a") as fh:
            fh.write(json.dumps({"event": "login", "details": {}}) + "\n")
        assert self._run(log_dir, "mine2", tmp_path).returncode == 0


class TestProbeShowsTheRecord:
    def test_probe_reads_the_marker_only_when_it_is_valid(self) -> None:
        cmd = iso.ISOLATION_PROBE_CMD
        assert "disabled_info=" in cmd
        assert 'safe f "$R/disabled"' in cmd
        assert subprocess.run(["sh", "-n", "-c", cmd], check=False).returncode == 0

    def test_parse_by_and_at(self) -> None:
        status = iso.parse_isolation_probe(probe("0", "1", disabled_info=_INFO))
        assert status.disabled_by == "alice via ssh root@h"
        assert status.disabled_at == _AT

    def test_line_shows_by_and_at(self) -> None:
        status = iso.parse_isolation_probe(probe("0", "1", disabled_info=_INFO))
        line = iso.format_isolation_line(status)
        assert line == f"disabled (admin opt-out by alice via ssh root@h at {_AT})"

    @pytest.mark.parametrize("info", ["", "{not json", "[1]", '{"by": 5}', '{"schema": 1}'])
    def test_unreadable_info_keeps_the_plain_line(self, info: str) -> None:
        status = iso.parse_isolation_probe(probe("0", "1", disabled_info=info))
        assert status.state == "disabled"
        assert iso.format_isolation_line(status) == "disabled (admin opt-out)"

    def test_info_is_cleaned_and_bounded(self) -> None:
        info = json.dumps({"by": "x\u0007" + "y" * 900, "at": _AT})
        status = iso.parse_isolation_probe(probe("0", "1", disabled_info=info))
        assert status.disabled_by is not None
        assert len(status.disabled_by) <= 120 and "\u0007" not in status.disabled_by

    def test_info_is_ignored_when_not_disabled(self) -> None:
        status = iso.parse_isolation_probe(probe("1", "0", disabled_info=_INFO))
        assert status.disabled_by is None
        assert iso.format_isolation_line(status) == "on (loopback restricted)"


class TestDisabledMarkerProbe:
    def test_present_and_absent(self) -> None:
        assert iso.disabled_marker_present(IsoSSH(disabled=True)) is True
        assert iso.disabled_marker_present(IsoSSH(disabled=False)) is False

    def test_uses_the_same_validity_test_as_run_as(self) -> None:
        cmd = iso.DISABLED_PROBE_CMD
        for needle in (
            "-maxdepth 0",
            "-type f",
            "-uid 0",
            "! -perm /022",
            "/srv/dango/runner/disabled",
        ):
            assert needle in cmd
