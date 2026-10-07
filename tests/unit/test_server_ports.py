"""tests/unit/test_server_ports.py

1.0.13-T18: the project.yml uploaded to a server gets the standard ports
(Caddy proxies to 8800 / 7805); the local file is never modified.
Uses real YAML files in tmp_path and a fake SSH object (no network).
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.cli.commands.deploy_provision import _build_auth_timeout_script, _print_port_notice
from dango.platform.cloud.file_sync import REMOTE_PROJECT_DIR, sync_project_files
from dango.platform.cloud.server_ports import (
    format_port_notice,
    normalize_server_ports,
    server_port_defaults,
)
from dango.platform.cloud.ssh import CommandResult

REMOTE_PROJECT_YML = f"{REMOTE_PROJECT_DIR}/.dango/project.yml"

CUSTOM = """\
# my project
project:
  name: demo

platform:
  duckdb_path: ./data/warehouse.duckdb  # keep me
  port: 8861
  metabase_port: 3061   # laptop conflict
  dbt_docs_port: 8161
  marimo_port: 7861
  auto_sync: true

auth:
  enabled: true
"""

STANDARD = """\
project:
  name: demo
platform:
  port: 8800
  metabase_port: 3000
  dbt_docs_port: 8081
  marimo_port: 7805
"""


class FakeSSH:
    """Records uploads; optionally reports a remote md5 for project.yml."""

    def __init__(self, remote_project_yml: bytes | None = None) -> None:
        self.key_path = Path("/nonexistent/key")
        self.remote_project_yml = remote_project_yml
        self.uploaded: dict[str, bytes] = {}
        self.exec_command = MagicMock(side_effect=self._exec)

    def _exec(self, cmd: str, **_kw: object) -> CommandResult:
        if cmd.startswith("md5sum") and REMOTE_PROJECT_YML in cmd:
            if self.remote_project_yml is None:
                return CommandResult(stdout="", stderr="", exit_code=1)
            md5 = hashlib.md5(self.remote_project_yml, usedforsecurity=False).hexdigest()
            return CommandResult(stdout=f"{md5}  {REMOTE_PROJECT_YML}", stderr="", exit_code=0)
        if cmd.startswith("md5sum"):
            return CommandResult(stdout="", stderr="", exit_code=1)
        return CommandResult(stdout="", stderr="", exit_code=0)

    def upload_file(self, local_path: Path | str, remote_path: str) -> None:
        self.uploaded[remote_path] = Path(local_path).read_bytes()

    def write_remote_file(self, remote_path: str, content: str | bytes, mode: int = 0o644) -> None:
        self.uploaded[remote_path] = content.encode() if isinstance(content, str) else content


def _project(tmp_path: Path, text: str) -> Path:
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "project.yml").write_bytes(text.encode())
    return tmp_path


def _sync(ssh: FakeSSH, root: Path, **kw: Any) -> Any:
    with patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync"):
        return sync_project_files(ssh, root, remote_host="10.0.0.1", **kw)


@pytest.mark.unit
class TestUploadedCopy:
    def test_row1_custom_ports_normalized_local_untouched(self, tmp_path):
        root = _project(tmp_path, CUSTOM)
        local = root / ".dango" / "project.yml"
        before = local.read_bytes()
        ssh = FakeSSH()

        result = _sync(ssh, root)

        sent = yaml.safe_load(ssh.uploaded[REMOTE_PROJECT_YML])["platform"]
        assert (sent["port"], sent["metabase_port"]) == (8800, 3000)
        assert (sent["dbt_docs_port"], sent["marimo_port"]) == (8081, 7805)
        assert local.read_bytes() == before
        assert {c.key for c in result.port_changes} == set(server_port_defaults())

    def test_row2_standard_ports_bytes_identical(self, tmp_path):
        root = _project(tmp_path, STANDARD)
        ssh = FakeSSH()

        result = _sync(ssh, root)

        assert ssh.uploaded[REMOTE_PROJECT_YML] == STANDARD.encode()
        assert result.port_changes == []

    def test_row3_partial_custom_only_that_key_changes(self, tmp_path):
        text = "# top\nplatform:\n  port: 8861  # mine\n  auto_dbt: false\nauth:\n  enabled: true\n"
        root = _project(tmp_path, text)
        ssh = FakeSSH()

        _sync(ssh, root)

        assert ssh.uploaded[REMOTE_PROJECT_YML].decode() == text.replace("8861", "8800")

    def test_row3_comments_and_order_survive_full_custom(self, tmp_path):
        root = _project(tmp_path, CUSTOM)
        ssh = FakeSSH()

        _sync(ssh, root)

        expected = (
            CUSTOM.replace("8861", "8800")
            .replace("3061", "3000")
            .replace("8161", "8081")
            .replace("7861", "7805")
        )
        assert ssh.uploaded[REMOTE_PROJECT_YML].decode() == expected

    def test_row4_absent_ports_unchanged_nothing_added(self, tmp_path):
        text = "project:\n  name: demo\nplatform:\n  auto_sync: true\n"
        root = _project(tmp_path, text)
        ssh = FakeSSH()

        result = _sync(ssh, root)

        assert ssh.uploaded[REMOTE_PROJECT_YML].decode() == text
        assert result.port_changes == []

    def test_row4_no_platform_section(self, tmp_path):
        text = "project:\n  name: demo\n"
        assert normalize_server_ports(text) == (text, [])

    def test_row5_unchanged_project_not_reuploaded(self, tmp_path):
        root = _project(tmp_path, CUSTOM)
        normalized, _ = normalize_server_ports(CUSTOM)
        ssh = FakeSSH(remote_project_yml=normalized.encode())

        result = _sync(ssh, root)

        assert REMOTE_PROJECT_YML not in ssh.uploaded
        assert ".dango/project.yml" not in result.synced_files

    def test_row5_changed_local_is_reuploaded(self, tmp_path):
        root = _project(tmp_path, CUSTOM)
        ssh = FakeSSH(remote_project_yml=b"something: else\n")

        _sync(ssh, root)

        assert REMOTE_PROJECT_YML in ssh.uploaded

    def test_dry_run_reports_changes_without_uploading(self, tmp_path):
        root = _project(tmp_path, CUSTOM)
        ssh = FakeSSH()

        result = _sync(ssh, root, dry_run=True)

        assert ssh.uploaded == {}
        assert len(result.port_changes) == 4


@pytest.mark.unit
class TestNormalizeEdgeCases:
    def test_quoted_and_crlf(self):
        text = 'platform:\r\n  port: "8861"\r\n  marimo_port: 7861\r\n'
        out, changes = normalize_server_ports(text)
        assert out == "platform:\r\n  port: 8800\r\n  marimo_port: 7805\r\n"
        assert len(changes) == 2

    def test_port_key_outside_platform_untouched(self):
        text = "platform:\n  port: 8861\nother:\n  port: 9999\n"
        out, _ = normalize_server_ports(text)
        assert "other:\n  port: 9999" in out
        assert "port: 8800" in out

    def test_flow_style_falls_back_to_dump(self):
        out, changes = normalize_server_ports("platform: {port: 8861, auto_sync: true}\n")
        assert yaml.safe_load(out)["platform"] == {"port": 8800, "auto_sync": True}
        assert [c.key for c in changes] == ["port"]

    def test_defaults_come_from_models(self):
        assert server_port_defaults() == {
            "port": 8800,
            "metabase_port": 3000,
            "dbt_docs_port": 8081,
            "marimo_port": 7805,
        }


@pytest.mark.unit
class TestFollowUp:
    def test_crlf_preserved_on_sync_path(self, tmp_path):
        text = "platform:\r\n  port: 8861\r\n  auto_sync: true\r\nauth:\r\n  enabled: true\r\n"
        root = _project(tmp_path, text)
        ssh = FakeSSH()

        _sync(ssh, root)

        assert ssh.uploaded[REMOTE_PROJECT_YML] == text.replace("8861", "8800").encode()

    def test_quoted_standard_port_is_standard(self):
        text = 'platform:\n  port: "8800"  # ok\n'
        assert normalize_server_ports(text) == (text, [])

    def test_quoted_standard_with_other_custom_keeps_text_edit(self):
        text = '# c\nplatform:\n  port: "8800"\n  marimo_port: 7861\n'
        out, changes = normalize_server_ports(text)
        assert out == '# c\nplatform:\n  port: "8800"\n  marimo_port: 7805\n'
        assert [(c.key, c.old, c.new) for c in changes] == [("marimo_port", 7861, 7805)]

    @pytest.mark.parametrize("bad", ["true", "abc", "'abc'"])
    def test_invalid_values_replaced_without_sentinel(self, bad):
        text = f"# c\nplatform:\n  port: {bad}\n"
        out, changes = normalize_server_ports(text)
        assert out == "# c\nplatform:\n  port: 8800\n"
        assert [(c.key, c.old, c.new) for c in changes] == [("port", None, 8800)]
        notice = format_port_notice(changes)
        assert notice is not None
        assert "port invalid value -> 8800" in notice
        assert "-1" not in notice

    def test_non_utf8_uploaded_raw_without_crash(self, tmp_path):
        raw = b"# caf\xe9\nplatform:\n  port: 8861\n"
        (tmp_path / ".dango").mkdir()
        (tmp_path / ".dango" / "project.yml").write_bytes(raw)
        ssh = FakeSSH()

        result = _sync(ssh, tmp_path)

        assert ssh.uploaded[REMOTE_PROJECT_YML] == raw
        assert result.port_changes == []

    def test_nested_port_key_not_rewritten(self):
        text = "platform:\n  extra:\n    port: 99\n  port: 8861\n"
        out, changes = normalize_server_ports(text)
        assert out == "platform:\n  extra:\n    port: 99\n  port: 8800\n"
        assert [c.key for c in changes] == ["port"]

    def test_nested_only_is_untouched(self):
        text = "platform:\n  extra:\n    port: 99\n"
        assert normalize_server_ports(text) == (text, [])

    def test_no_notice_when_remote_already_normalized(self, tmp_path):
        root = _project(tmp_path, CUSTOM)
        normalized, _ = normalize_server_ports(CUSTOM)
        ssh = FakeSSH(remote_project_yml=normalized.encode())

        assert _sync(ssh, root).port_changes == []
        assert _sync(ssh, root, dry_run=True).port_changes == []

    def test_dry_run_reports_when_remote_differs(self, tmp_path):
        root = _project(tmp_path, CUSTOM)
        ssh = FakeSSH(remote_project_yml=b"old: 1\n")

        assert len(_sync(ssh, root, dry_run=True).port_changes) == 4
        assert ssh.uploaded == {}

    def test_sync_project_files_is_only_project_yml_upload_site(self):
        """Grep-style guard: no other module uploads/writes project.yml to a server."""
        root = Path(__file__).resolve().parents[2] / "dango"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name in {"file_sync.py", "server_ports.py"}:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if "project.yml" in line and ("upload_file" in line or "write_remote_file" in line):
                    offenders.append(f"{path.relative_to(root)}:{n}")
        assert offenders == []


@pytest.mark.unit
class TestNotice:
    def test_row8_notice_only_when_changed(self):
        assert format_port_notice([]) is None
        _, changes = normalize_server_ports("platform:\n  port: 8861\n")
        notice = format_port_notice(changes)
        assert notice is not None
        assert "standard server ports" in notice
        assert "local ports are unchanged" in notice
        assert "port 8861 -> 8800" in notice

    def test_row8_cli_helper_silent_without_changes(self, capsys):
        _print_port_notice(MagicMock(port_changes=[]))
        assert capsys.readouterr().out == ""

    def test_row8_cli_helper_prints_when_changed(self, capsys):
        _, changes = normalize_server_ports("platform:\n  port: 8861\n")
        _print_port_notice(MagicMock(port_changes=changes))
        assert "standard server ports" in capsys.readouterr().out


@pytest.mark.unit
class TestAuthTimeoutStillApplies:
    def test_row7_auth_script_applies_on_normalized_file(self, tmp_path):
        """The server-side edit runs after the upload and keeps the standard ports."""
        normalized, _ = normalize_server_ports(CUSTOM)
        (tmp_path / ".dango").mkdir()
        (tmp_path / ".dango" / "project.yml").write_text(normalized)
        script = _build_auth_timeout_script()
        ast.parse(script)
        script = script.replace("/srv/dango/project", str(tmp_path))

        subprocess.run([sys.executable, "-c", script], check=True, cwd=tmp_path)

        data = yaml.safe_load((tmp_path / ".dango" / "project.yml").read_text())
        assert data["auth"]["session_max_days"] == 30
        assert data["auth"]["idle_timeout_minutes"] == 60
        assert data["platform"]["port"] == 8800
