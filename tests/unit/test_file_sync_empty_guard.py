"""tests/unit/test_file_sync_empty_guard.py

Push refuses to mirror an empty local directory over a non-empty server copy, and
writes into the project tree as the dango user without following planted symlinks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.file_sync import REMOTE_PROJECT_DIR, SYNC_CONFIG_FILES, sync_project_files
from dango.platform.cloud.ssh import CommandResult

_RSYNC = "dango.platform.cloud.file_sync._rsync_directory"


@pytest.fixture(autouse=True)
def _rsync_installed():
    with patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync"):
        yield


def _fake_ssh(*, remote_nonempty: bool) -> MagicMock:
    """Mock root SSHManager; ``find -type f`` on the server reports a non-empty directory when asked."""
    ssh = MagicMock()
    ssh.key_path = Path("/tmp/test_key")
    ssh.known_hosts_path = Path("/tmp/test_known_hosts")
    ssh.legacy_known_hosts_paths = []
    ssh.host = "10.0.0.1"
    ssh.port = 22
    ssh.username = "root"

    def _exec(cmd: str, **kwargs: object) -> CommandResult:
        if "-type f -print -quit" in cmd:
            return CommandResult(stdout="", stderr="", exit_code=0 if remote_nonempty else 1)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh.exec_command.side_effect = _exec
    return ssh


def _project(tmp_path: Path, *, empty_dirs: tuple[str, ...] = ()) -> Path:
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "sources.yml").write_text("sources: []")
    (root / "dbt" / "models").mkdir(parents=True)
    (root / "dbt" / "models" / "m.sql").write_text("select 1")
    for rel in empty_dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.unit
class TestEmptyDirGuard:
    @pytest.mark.parametrize("empty", ["dbt/seeds", "scripts", "metabase", "custom_sources"])
    def test_empty_local_dir_with_nonempty_remote_is_refused(self, tmp_path, empty):
        root = _project(tmp_path, empty_dirs=(empty,))
        ssh = _fake_ssh(remote_nonempty=True)
        with patch(_RSYNC) as rsync:
            with pytest.raises(CloudProvisioningError, match="--allow-empty"):
                sync_project_files(ssh, root, remote_host="10.0.0.1")
        rsync.assert_not_called()
        # Nothing was written before the refusal.
        ssh.write_remote_file.assert_not_called()
        ssh.upload_file.assert_not_called()

    def test_empty_both_sides_proceeds(self, tmp_path):
        root = _project(tmp_path, empty_dirs=("dbt/seeds",))
        ssh = _fake_ssh(remote_nonempty=False)
        with patch(_RSYNC, return_value="") as rsync:
            result = sync_project_files(ssh, root, remote_host="10.0.0.1")
        assert "dbt/seeds/" in result.synced_files
        assert rsync.call_count == 2  # models + seeds

    def test_missing_local_dir_still_skipped(self, tmp_path):
        root = _project(tmp_path)
        ssh = _fake_ssh(remote_nonempty=True)
        with patch(_RSYNC, return_value="") as rsync:
            result = sync_project_files(ssh, root, remote_host="10.0.0.1")
        assert "scripts/" not in result.synced_files
        assert rsync.call_count == 1
        assert not any("ls -A" in str(c) for c in ssh.exec_command.call_args_list)

    def test_allow_empty_overrides(self, tmp_path):
        root = _project(tmp_path, empty_dirs=("scripts",))
        ssh = _fake_ssh(remote_nonempty=True)
        with patch(_RSYNC, return_value="") as rsync:
            result = sync_project_files(ssh, root, remote_host="10.0.0.1", allow_empty=True)
        assert "scripts/" in result.synced_files
        assert rsync.call_count == 2

    def test_dry_run_reports_refusal(self, tmp_path):
        root = _project(tmp_path, empty_dirs=("dbt/seeds",))
        ssh = _fake_ssh(remote_nonempty=True)
        with patch(_RSYNC) as rsync:
            with pytest.raises(CloudProvisioningError, match="--allow-empty"):
                sync_project_files(ssh, root, remote_host="10.0.0.1", dry_run=True)
        rsync.assert_not_called()

    def test_push_deploy_forwards_allow_empty(self, tmp_path):
        from dango.platform.cloud.deployer import push_deploy
        from dango.platform.cloud.file_sync import SyncResult

        with patch(
            "dango.platform.cloud.file_sync.sync_project_files", return_value=SyncResult()
        ) as sync:
            push_deploy(MagicMock(), tmp_path, "10.0.0.1", dry_run=True, allow_empty=True)
        assert sync.call_args.kwargs["allow_empty"] is True

    def test_push_deploy_defaults_to_refusing(self, tmp_path):
        from dango.platform.cloud.deployer import push_deploy
        from dango.platform.cloud.file_sync import SyncResult

        with patch(
            "dango.platform.cloud.file_sync.sync_project_files", return_value=SyncResult()
        ) as sync:
            push_deploy(MagicMock(), tmp_path, "10.0.0.1", dry_run=True)
        assert sync.call_args.kwargs["allow_empty"] is False


@pytest.mark.unit
class TestRemotePushAllowEmptyFlag:
    @pytest.mark.parametrize(("args", "expected"), [([], False), (["--allow-empty"], True)])
    def test_flag_reaches_push_deploy(self, tmp_path, args, expected):
        from click.testing import CliRunner

        from dango.cli.commands.remote import remote

        cfg = MagicMock(droplet_ip="10.0.0.1", deploy_branch=None)
        with (
            patch(
                "dango.cli.commands.remote._require_cloud_deployment", return_value=(cfg, tmp_path)
            ),
            patch("dango.cli.commands.remote._connect_ssh", return_value=MagicMock()),
            patch("dango.platform.cloud.deployer.push_deploy") as push,
        ):
            push.return_value.sync_result = MagicMock(
                synced_files=[],
                added_models=[],
                changed_models=[],
                removed_models=[],
                packages_changed=False,
                port_changes=[],
            )
            push.return_value.git_info = None
            push.return_value.warnings = []
            CliRunner().invoke(remote, ["push", "--dry-run", *args], obj={})
        assert push.call_args.kwargs["allow_empty"] is expected


@pytest.mark.unit
class TestScriptsYmlIsPushed:
    def test_scripts_yml_in_sync_list(self):
        assert (
            ".dango/scripts.yml",
            f"{REMOTE_PROJECT_DIR}/.dango/scripts.yml",
        ) in SYNC_CONFIG_FILES

    def test_uploaded_when_present(self, tmp_path):
        root = _project(tmp_path)
        (root / ".dango" / "scripts.yml").write_text("scripts: {}\n")
        ssh = _fake_ssh(remote_nonempty=False)
        with patch(_RSYNC, return_value=""):
            result = sync_project_files(ssh, root, remote_host="10.0.0.1")
        assert ".dango/scripts.yml" in result.synced_files

    def test_absent_is_not_an_error(self, tmp_path):
        root = _project(tmp_path)
        ssh = _fake_ssh(remote_nonempty=False)
        with patch(_RSYNC, return_value=""):
            result = sync_project_files(ssh, root, remote_host="10.0.0.1")
        assert ".dango/scripts.yml" not in result.synced_files


@pytest.mark.unit
class TestPushWritesAsDangoUser:
    """K-28: root never writes into the dango-owned project tree."""

    def test_config_files_are_written_via_runuser_not_root_sftp(self, tmp_path):
        root = _project(tmp_path)
        ssh = _fake_ssh(remote_nonempty=False)
        with patch(_RSYNC, return_value=""):
            sync_project_files(ssh, root, remote_host="10.0.0.1")
        for call in ssh.upload_file.call_args_list:
            assert str(call.args[1]).startswith("/root/"), call
        for call in ssh.write_remote_file.call_args_list:
            assert not str(call.args[0]).startswith(REMOTE_PROJECT_DIR), call
        cmds = [str(c.args[0]) for c in ssh.exec_command.call_args_list]
        writes = [
            c for c in cmds if f"{REMOTE_PROJECT_DIR}/.dango/sources.yml" in c and "mv -fT" in c
        ]
        assert writes
        assert all(c.startswith("runuser -u dango --") for c in writes)
        # Directory creation under the project tree happens as dango too.
        mkdirs = [c for c in cmds if "mkdir" in c and REMOTE_PROJECT_DIR in c]
        assert mkdirs
        assert all(c.startswith("runuser -u dango --") for c in mkdirs)

    def test_rsync_runs_the_remote_side_as_dango(self, tmp_path):
        from dango.platform.cloud.rsync_transport import _rsync_directory

        completed = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("dango.platform.cloud.rsync_transport._require_pinned_host"),
            patch("subprocess.run", return_value=completed) as run,
        ):
            _rsync_directory(
                tmp_path,
                "root@10.0.0.1:/srv/dango/project/dbt/models",
                Path("/k"),
                known_hosts_path=Path("/kh"),
            )
        cmd = run.call_args.args[0]
        assert "--rsync-path=runuser -u dango -- rsync" in cmd


def _gnu_mv() -> bool:
    mv = shutil.which("gmv") or shutil.which("mv")
    if not mv:
        return False
    out = subprocess.run([mv, "--version"], capture_output=True, text=True, check=False)
    return out.returncode == 0 and "GNU" in out.stdout


def _docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


class _LocalTreeSSH:
    """Runs push commands against a real temp tree standing in for /srv/dango/project."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        (tmp / "proj").mkdir(exist_ok=True)
        (tmp / "root").mkdir(exist_ok=True)

    def _local(self, text: str) -> str:
        return (
            text.replace(REMOTE_PROJECT_DIR, str(self.tmp / "proj"))
            .replace("/root/", f"{self.tmp}/root/")
            .replace("runuser -u dango --", "")
        )

    def write_remote_file(self, path: str, content, mode: int = 0o644, timeout=None) -> None:
        data = content.encode() if isinstance(content, str) else content
        Path(self._local(path)).write_bytes(data)

    def upload_file(self, local_path, remote_path: str, **kwargs) -> None:
        shutil.copyfile(local_path, self._local(remote_path))

    def exec_command(self, command: str, timeout=None, check: bool = False) -> CommandResult:
        argv = ["sh", "-c", self._local(command)]
        if not _gnu_mv():
            if not _docker():
                pytest.skip("needs GNU mv or docker (python:3.11-slim)")
            argv = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{self.tmp}:{self.tmp}",
                "python:3.11-slim",
                *argv,
            ]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        return CommandResult(stdout=done.stdout, stderr=done.stderr, exit_code=done.returncode)


@pytest.mark.unit
class TestRealTree:
    def test_config_upload_replaces_planted_symlink_without_following(self, tmp_path):
        from dango.platform.cloud.file_sync import _upload_file_if_exists

        ssh = _LocalTreeSSH(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("keep")
        dest = tmp_path / "proj" / ".dango" / "sources.yml"
        dest.parent.mkdir()
        dest.symlink_to(outside)
        local = tmp_path / "local.yml"
        local.write_text("sources: []")

        uploaded = _upload_file_if_exists(ssh, local, f"{REMOTE_PROJECT_DIR}/.dango/sources.yml")

        assert uploaded
        assert outside.read_text() == "keep"
        assert not dest.is_symlink()
        assert dest.read_text() == "sources: []"


@pytest.mark.unit
class TestRsyncDoesNotFollowDestinationLinks:
    def test_real_rsync_replaces_links_inside_destination(self, tmp_path):
        """Run the command _rsync_directory builds against a real tree (local transport)."""
        from dango.platform.cloud.rsync_transport import _rsync_directory

        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.sql").write_text("new-a")
        (src / "sub" / "b.sql").write_text("new-b")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "victim.txt").write_text("keep")
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.sql").symlink_to(outside / "victim.txt")
        (dest / "sub").symlink_to(outside)

        captured: dict[str, list[str]] = {}
        real_run = subprocess.run

        def _run_local(cmd, **kwargs):
            captured["cmd"] = cmd
            local = [c for c in cmd if not c.startswith("--rsync-path")]
            i = local.index("-e")
            del local[i : i + 2]
            local[-1] = f"{dest}/"
            return real_run(local, capture_output=True, text=True, check=False)

        with (
            patch("dango.platform.cloud.rsync_transport._require_pinned_host"),
            patch("dango.platform.cloud.rsync_transport.subprocess.run", side_effect=_run_local),
        ):
            _rsync_directory(
                src,
                "root@10.0.0.1:/srv/dango/project/dbt/models",
                Path("/k"),
                known_hosts_path=Path("/kh"),
            )

        assert (outside / "victim.txt").read_text() == "keep"
        assert sorted(p.name for p in outside.iterdir()) == ["victim.txt"]
        assert (dest / "a.sql").read_text() == "new-a"
        assert (dest / "sub" / "b.sql").read_text() == "new-b"
        assert not (dest / "sub").is_symlink()
        assert "--rsync-path=runuser -u dango -- rsync" in captured["cmd"]
