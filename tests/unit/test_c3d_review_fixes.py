"""tests/unit/test_c3d_review_fixes.py

Review follow-ups for the push/upload change: large-file transfer path, empty-dir guard
edge cases, project reads as the dango user, and partial-upload cleanup.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.empty_guard import refuse_empty_mirrors
from dango.platform.cloud.ssh import CommandResult, SSHManager


def _ok(stdout: str = "", code: int = 0) -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", exit_code=code)


@pytest.mark.unit
class TestLargeFileTransfer:
    def test_write_remote_file_is_pipelined(self):
        manager = SSHManager.__new__(SSHManager)
        manager._client = MagicMock()
        remote_file = MagicMock()
        sftp = manager._client.open_sftp.return_value
        sftp.open.return_value.__enter__.return_value = remote_file
        sftp.open.return_value.__exit__.return_value = False
        with (
            patch.object(SSHManager, "_require_connected"),
            patch("dango.platform.cloud.ssh._ensure_paramiko", return_value=MagicMock()),
        ):
            manager.write_remote_file("/root/x", b"data")
        remote_file.set_pipelined.assert_called_once_with(True)
        remote_file.write.assert_called_once_with(b"data")

    def test_source_file_is_staged_with_put_not_read_into_sftp_write(self, tmp_path):
        from dango.platform.cloud.project_files import write_project_file

        src = tmp_path / "big.jar"
        src.write_bytes(b"x" * 10)
        ssh = MagicMock()
        ssh.exec_command.return_value = _ok()
        write_project_file(ssh, "/srv/dango/project/p/big.jar", b"", source=src)
        ssh.upload_file.assert_called_once()
        assert ssh.upload_file.call_args.args[0] == src
        assert ssh.upload_file.call_args.args[1].startswith("/root/.dango-upload-")
        ssh.write_remote_file.assert_not_called()
        cmds = [c.args[0] for c in ssh.exec_command.call_args_list]
        assert any("mv -fT" in c and c.startswith("runuser -u dango --") for c in cmds)

    def test_config_upload_of_local_file_uses_put(self, tmp_path):
        from dango.platform.cloud.file_sync import _upload_file_if_exists

        local = tmp_path / "duckdb.metabase-driver.jar"
        local.write_bytes(b"jar")
        ssh = MagicMock()
        ssh.exec_command.return_value = _ok()
        assert _upload_file_if_exists(ssh, local, "/srv/dango/project/m/duckdb.jar")
        ssh.upload_file.assert_called_once()
        ssh.write_remote_file.assert_not_called()


def _guard_ssh(remote_has_files: bool) -> MagicMock:
    ssh = MagicMock()
    ssh.exec_command.return_value = _ok(code=0 if remote_has_files else 1)
    return ssh


@pytest.mark.unit
class TestEmptyGuardEdges:
    @pytest.mark.parametrize("junk", [".DS_Store", ".gitkeep", "Thumbs.db"])
    def test_junk_only_local_dir_is_treated_as_empty(self, tmp_path, junk):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / junk).write_text("")
        with pytest.raises(CloudProvisioningError, match="--allow-empty"):
            refuse_empty_mirrors(
                _guard_ssh(True), tmp_path, ["scripts"], "/srv/dango/project", allow_empty=False
            )

    def test_only_empty_subdirs_is_treated_as_empty(self, tmp_path):
        (tmp_path / "scripts" / "a" / "b").mkdir(parents=True)
        with pytest.raises(CloudProvisioningError):
            refuse_empty_mirrors(
                _guard_ssh(True), tmp_path, ["scripts"], "/srv/dango/project", allow_empty=False
            )

    def test_real_nested_file_is_not_empty(self, tmp_path):
        (tmp_path / "scripts" / "a").mkdir(parents=True)
        (tmp_path / "scripts" / "a" / "run.py").write_text("x")
        ssh = _guard_ssh(True)
        refuse_empty_mirrors(ssh, tmp_path, ["scripts"], "/srv/dango/project", allow_empty=False)
        ssh.exec_command.assert_not_called()

    def test_server_probe_counts_files_as_dango(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        ssh = _guard_ssh(False)
        refuse_empty_mirrors(ssh, tmp_path, ["scripts"], "/srv/dango/project", allow_empty=False)
        cmd = ssh.exec_command.call_args.args[0]
        assert cmd.startswith("runuser -u dango --")
        assert "find" in cmd and "-type f" in cmd and "-quit" in cmd
        assert "/srv/dango/project/scripts" in cmd

    def test_server_with_only_empty_dirs_proceeds(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        refuse_empty_mirrors(
            _guard_ssh(False), tmp_path, ["scripts"], "/srv/dango/project", allow_empty=False
        )


@pytest.mark.unit
class TestRootReadsBecomeDangoReads:
    def test_validate_remote_sources_reads_as_dango(self):
        from dango.platform.cloud.deployer import _validate_remote_sources

        ssh = MagicMock()
        ssh.exec_command.return_value = _ok(code=3)
        assert _validate_remote_sources(ssh) == []
        cmds = [c.args[0] for c in ssh.exec_command.call_args_list]
        assert cmds
        assert all(c.startswith("runuser -u dango --") for c in cmds)

    def test_validate_remote_sources_secrets_read_as_dango(self):
        from dango.platform.cloud.deployer import _validate_remote_sources

        ssh = MagicMock()

        def _exec(cmd, **kw):
            if "sources.yml" in cmd:
                return _ok("sources:\n  - name: s\n    type: google_sheets\n")
            return _ok(code=3)

        ssh.exec_command.side_effect = _exec
        _validate_remote_sources(ssh)
        cmds = [c.args[0] for c in ssh.exec_command.call_args_list]
        assert any("secrets.toml" in c for c in cmds)
        assert all(c.startswith("runuser -u dango --") for c in cmds)

    def test_remote_compose_name_reads_project_yml_as_dango(self):
        from dango.platform.cloud.backup import get_remote_compose_project_name as fn

        ssh = MagicMock()
        ssh.exec_command.return_value = _ok("project:\n  id: ab12cd34ef56\n")
        assert fn(ssh, "/srv/dango/project") == "dango-ab12cd34"
        cmd = ssh.exec_command.call_args.args[0]
        assert cmd.startswith("runuser -u dango --")
        assert "project.yml" in cmd


class _FakeSSH:
    def __init__(self, exc: type[BaseException]) -> None:
        self.exc = exc
        self.commands: list[str] = []

    def exec_command(self, command: str, timeout=None, check: bool = False) -> CommandResult:
        self.commands.append(command)
        return _ok()

    def upload_file(self, local, remote, callback=None, timeout=None) -> None:
        raise self.exc

    def disconnect(self) -> None:
        pass


@pytest.mark.unit
class TestUploadCleanupOnAnyException:
    @pytest.mark.parametrize("exc", [KeyboardInterrupt, RuntimeError])
    def test_part_file_removed(self, tmp_path, exc):
        from dango.cli.commands.remote import remote

        f = tmp_path / "data" / "a.csv"
        f.parent.mkdir()
        f.write_text("x")
        ssh = _FakeSSH(exc)
        with patch(
            "dango.cli.commands.remote._ssh_connect_or_fail",
            return_value=(MagicMock(), ssh, tmp_path),
        ):
            CliRunner().invoke(remote, ["upload", str(f)], obj={})
        assert any(c.startswith("rm -f") and "a.csv.part" in c for c in ssh.commands)
