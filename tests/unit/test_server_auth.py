"""tests/unit/test_server_auth.py

Cloud auth timeouts survive `dango remote push` (1.0.13-T20, C24).
Real YAML files in tmp_path and a fake SSH; nothing touches a network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from dango.platform.cloud.file_sync import REMOTE_PROJECT_DIR, sync_project_files
from dango.platform.cloud.ssh import CommandResult

REMOTE_YML = f"{REMOTE_PROJECT_DIR}/.dango/project.yml"


class FakeSSH:
    """Records uploads of project.yml; reports a configurable remote md5."""

    key_path = Path("/nonexistent/key")

    def __init__(self, remote_md5: str | None = None, first_deploy: bool = False) -> None:
        self.remote_md5 = remote_md5
        self.first_deploy = first_deploy
        self.uploaded: dict[str, bytes] = {}
        self.commands: list[str] = []

    def exec_command(self, cmd: str, **kwargs: object) -> CommandResult:
        self.commands.append(cmd)
        if cmd.startswith("test -f"):
            return CommandResult(stdout="", stderr="", exit_code=1 if self.first_deploy else 0)
        if cmd.startswith("md5sum") and cmd.split()[1] == REMOTE_YML and self.remote_md5:
            return CommandResult(stdout=f"{self.remote_md5}  {REMOTE_YML}", stderr="", exit_code=0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    def upload_file(self, local_path: Path, remote_path: str, **kwargs: object) -> None:
        self.uploaded[remote_path] = Path(local_path).read_bytes()

    def write_remote_file(self, remote_path: str, content: str | bytes, **kw: object) -> None:
        self.uploaded[remote_path] = content.encode() if isinstance(content, str) else content


def _push(tmp_path: Path, yml: bytes, ssh: FakeSSH | None = None) -> tuple[FakeSSH, Path]:
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    local = root / ".dango" / "project.yml"
    local.write_bytes(yml)
    ssh = ssh or FakeSSH()
    with patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync"):
        sync_project_files(ssh, root, remote_host="203.0.113.1")
    return ssh, local


def _uploaded_auth(ssh: FakeSSH) -> dict:
    return yaml.safe_load(ssh.uploaded[REMOTE_YML])["auth"]


@pytest.mark.unit
class TestPushKeepsCloudAuthTimeouts:
    def test_row1_enabled_only_gets_cloud_values(self, tmp_path):
        original = b"project: x\nauth:\n  enabled: true\n"
        ssh, local = _push(tmp_path, original)
        assert _uploaded_auth(ssh) == {
            "enabled": True,
            "session_max_days": 30,
            "idle_timeout_minutes": 60,
        }
        assert local.read_bytes() == original

    def test_row2_explicit_local_value_wins(self, tmp_path):
        ssh, _ = _push(tmp_path, b"auth:\n  enabled: true\n  session_max_days: 7\n")
        auth = _uploaded_auth(ssh)
        assert auth["session_max_days"] == 7
        assert auth["idle_timeout_minutes"] == 60

    def test_row3_no_auth_section(self, tmp_path):
        ssh, local = _push(tmp_path, b"project: x\nplatform:\n  port: 8800\n")
        data = yaml.safe_load(ssh.uploaded[REMOTE_YML])
        assert data["auth"] == {"session_max_days": 30, "idle_timeout_minutes": 60}
        assert data["platform"] == {"port": 8800}
        assert b"auth" not in local.read_bytes()

    def test_row4_already_normalized_server_not_reuploaded(self, tmp_path):
        import hashlib

        yml = b"auth:\n  enabled: true\n"
        first, _ = _push(tmp_path, yml)
        digest = hashlib.md5(first.uploaded[REMOTE_YML], usedforsecurity=False).hexdigest()
        second, _ = _push(tmp_path / "again", yml, FakeSSH(remote_md5=digest))
        assert REMOTE_YML not in second.uploaded

    def test_row5_comments_other_keys_and_crlf_preserved(self, tmp_path):
        yml = b"# top comment\r\nproject: x  # trailing\r\nauth:\r\n    enabled: true # why\r\nz: 1\r\n"
        ssh, _ = _push(tmp_path, yml)
        out = ssh.uploaded[REMOTE_YML]
        assert out == (
            b"# top comment\r\nproject: x  # trailing\r\nauth:\r\n    enabled: true # why\r\n"
            b"    session_max_days: 30\r\n    idle_timeout_minutes: 60\r\nz: 1\r\n"
        )

    def test_row6_first_deploy_has_cloud_values(self, tmp_path):
        ssh, _ = _push(tmp_path, b"auth:\n  enabled: true\n", FakeSSH(first_deploy=True))
        assert _uploaded_auth(ssh)["session_max_days"] == 30
        assert _uploaded_auth(ssh)["idle_timeout_minutes"] == 60

    def test_row6_provision_script_is_idempotent_over_uploaded_copy(self, tmp_path):
        from dango.cli.commands.deploy_provision import _build_auth_timeout_script

        ssh, _ = _push(tmp_path, b"auth:\n  enabled: true\n")
        before = yaml.safe_load(ssh.uploaded[REMOTE_YML])
        script = _build_auth_timeout_script()
        script = script.replace("/srv/dango/project", str(tmp_path / "srv"))
        server = tmp_path / "srv" / ".dango"
        server.mkdir(parents=True)
        (server / "project.yml").write_bytes(ssh.uploaded[REMOTE_YML])
        subprocess.run([sys.executable, "-c", script], check=True, cwd=tmp_path)
        assert yaml.safe_load((server / "project.yml").read_text()) == before

    def test_row7_one_code_path_for_do_and_byos(self):
        """DO and BYOS both call sync_project_files; push calls it too."""
        import inspect

        from dango.cli.commands import deploy_provision
        from dango.platform.cloud import deployer

        assert "sync_project_files(" in inspect.getsource(deploy_provision.run_provisioning)
        assert "sync_project_files(" in inspect.getsource(deploy_provision.run_byos_setup)
        assert "sync_project_files(" in inspect.getsource(deployer.push_deploy)

    def test_dry_run_does_not_upload(self, tmp_path):
        root = tmp_path / "p"
        (root / ".dango").mkdir(parents=True)
        (root / ".dango" / "project.yml").write_text("auth:\n  enabled: true\n")
        ssh = FakeSSH()
        with patch("dango.platform.cloud.file_sync.shutil.which", return_value="/x"):
            sync_project_files(ssh, root, remote_host="h", dry_run=True)
        assert ssh.uploaded == {}


@pytest.mark.unit
class TestApplyCloudAuthTimeouts:
    @pytest.mark.parametrize(
        "text",
        ["", "# only a comment\n", "auth: {enabled: true}\n", "auth: {}\n", "auth: ~\n", "a: [1\n"],
    )
    def test_odd_inputs_never_raise(self, text):
        from dango.platform.cloud.server_auth import apply_cloud_auth_timeouts

        out = apply_cloud_auth_timeouts(text)
        if text.startswith("auth"):
            auth = yaml.safe_load(out)["auth"]
            assert auth["session_max_days"] == 30 and auth["idle_timeout_minutes"] == 60

    def test_both_explicit_returns_text_unchanged(self):
        from dango.platform.cloud.server_auth import apply_cloud_auth_timeouts

        text = "auth:\n  session_max_days: 1\n  idle_timeout_minutes: 2\n"
        assert apply_cloud_auth_timeouts(text) == text

    def test_trailing_nested_block_and_comment_kept_in_place(self):
        from dango.platform.cloud.server_auth import apply_cloud_auth_timeouts

        text = "auth:\n  enabled: true\n  lockout:\n    max_attempts: 3\n  # tail\nnext: 1\n"
        out = apply_cloud_auth_timeouts(text)
        data = yaml.safe_load(out)
        assert data["auth"]["lockout"] == {"max_attempts": 3}
        assert data["auth"]["session_max_days"] == 30
        assert data["next"] == 1
        assert "  # tail\n" in out


@pytest.mark.unit
class TestRobustness:
    def test_non_utf8_uploaded_unchanged(self, tmp_path):
        yml = b"# caf\xe9\nauth:\n  enabled: true\n"
        ssh, local = _push(tmp_path, yml)
        assert ssh.uploaded[REMOTE_YML] == yml
        assert local.read_bytes() == yml

    def test_bom_preserved_on_text_edit_and_fallback(self, tmp_path):
        bom = b"\xef\xbb\xbf"
        ssh, _ = _push(tmp_path, bom + b"auth:\n  enabled: true\n")
        assert ssh.uploaded[REMOTE_YML].startswith(bom)
        assert _uploaded_auth(ssh)["session_max_days"] == 30
        ssh2, _ = _push(tmp_path / "b", bom + b"auth: {enabled: true}\n")
        assert ssh2.uploaded[REMOTE_YML].startswith(bom)
        assert yaml.safe_load(ssh2.uploaded[REMOTE_YML][3:])["auth"]["idle_timeout_minutes"] == 60

    @pytest.mark.parametrize("key", ["session_max_days", "idle_timeout_minutes"])
    def test_null_value_is_filled_not_left_to_silent_default(self, tmp_path, key):
        """AuthConfig rejects None (int field) -> web app falls back to 365 d / 24 h."""
        from dango.config.models import AuthConfig

        with pytest.raises(ValueError):
            AuthConfig(**{key: None})
        ssh, _ = _push(tmp_path, f"auth:\n  enabled: true\n  {key}:\n".encode())
        auth = _uploaded_auth(ssh)
        AuthConfig(**auth)  # the uploaded copy validates
        assert auth["session_max_days"] == 30 or key == "session_max_days"
        assert auth[key] == CLOUD[key]


CLOUD = {"session_max_days": 30, "idle_timeout_minutes": 60}


@pytest.mark.unit
class TestPortsAndAuthChain:
    YML = b"project: x\r\nplatform:\r\n  port: 8861\r\n  marimo_port: 7999\r\n# keep\r\nauth:\r\n  enabled: true\r\n"

    def test_chain_applies_both_and_keeps_crlf(self, tmp_path):
        ssh, local = _push(tmp_path, self.YML)
        out = ssh.uploaded[REMOTE_YML]
        data = yaml.safe_load(out)
        assert data["platform"]["port"] == 8800
        assert data["platform"]["marimo_port"] == 7805
        assert data["auth"] == {
            "enabled": True,
            "session_max_days": 30,
            "idle_timeout_minutes": 60,
        }
        assert b"\r\n" in out and b"\n" not in out.replace(b"\r\n", b"")
        assert b"# keep" in out
        assert local.read_bytes() == self.YML

    def test_chain_idempotent_md5_skip_on_combined_result(self, tmp_path):
        import hashlib

        first, _ = _push(tmp_path, self.YML)
        digest = hashlib.md5(first.uploaded[REMOTE_YML], usedforsecurity=False).hexdigest()
        second, _ = _push(tmp_path / "again", self.YML, FakeSSH(remote_md5=digest))
        assert REMOTE_YML not in second.uploaded
