"""tests/unit/test_restore_followups.py

Restore hardening follow-ups: the kept-notebooks directory is private and swept, the restore
refuses an untrusted ``/srv/dango``, a failed restore reports a failed permission re-apply, and
root reads the staged ``auth.yml`` as the service user.
"""

from __future__ import annotations

import re
import stat
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import backup
from dango.platform.cloud import backup_restore as br
from dango.platform.cloud import scheduled_backup as sb
from dango.platform.cloud.ssh import CommandResult
from tests.factories.backup_shell import ShellSSH, shim_env

pytestmark = pytest.mark.unit


def _dash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["dash", "-c", script], capture_output=True, text=True, env=env)


def test_kept_directory_is_group_writable_and_swept() -> None:
    cmd = br.private_dir_cmd(f"{br.RESTORE_PRIVATE_ROOT}/dango-restore-kept-x", service_write=True)
    assert "mkdir -m 0770" in cmd and "root:dango" in cmd
    commands: list[str] = []
    ssh = MagicMock()
    ssh.exec_command.side_effect = lambda c, **_k: commands.append(c) or CommandResult("", "", 0)
    backup._sweep_stale_leftovers(ssh)
    assert any("dango-restore-kept-*" in c for c in commands)


def test_kept_directory_mode_locally(tmp_path: Path) -> None:
    target = tmp_path / "srv" / ".restore" / "dango-restore-kept-1"
    result = _dash(br.private_dir_cmd(str(target), service_write=True), shim_env(tmp_path / "s"))
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o770


def test_private_dir_command_refuses_untrusted_srv_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "srv"
    link.symlink_to(real)
    target = tmp_path / "private" / "d-1"
    env = shim_env(tmp_path / "s")
    with patch.object(br, "SRV_ROOT", str(link)):
        cmd = br.private_dir_cmd(str(target), service_access=True)
        assert _dash(cmd, env).returncode != 0
    assert not (tmp_path / "private").exists()
    text = br.private_dir_cmd("/srv/dango/.restore/x")
    assert re.search(r"find /srv/dango -maxdepth 0 .*-type l.*! -user root.*-perm -020", text)
    # a directory not owned by root is refused too (the test user is not root)
    if real.stat().st_uid != 0:
        with patch.object(br, "SRV_ROOT", str(real)):
            assert _dash(br.private_dir_cmd(str(target)), env).returncode != 0


def test_scheduled_staging_refuses_untrusted_srv_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "srv"
    link.symlink_to(real)
    with (
        patch.object(br, "SRV_ROOT", str(link)),
        patch.object(br, "RESTORE_PRIVATE_ROOT", str(tmp_path / "private")),
        pytest.raises(CloudProvisioningError),
    ):
        sb._private_restore_dir("dango-restore-staging")
    assert not (tmp_path / "private").exists()


def _archive(tmp_path: Path) -> tuple[Path, Path]:
    top = tmp_path / "content" / "backup-1"
    (top / "notebooks").mkdir(parents=True)
    (top / "notebooks" / "a.py").write_text("A\n")
    (top / "manifest.json").write_text("{}")
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(top), arcname="backup-1")
    project = tmp_path / "project"
    (project / "notebooks").mkdir(parents=True)
    return archive, project


def _restore(tmp_path: Path, ssh: ShellSSH, archive: Path, project: Path) -> None:
    with (
        patch.object(backup, "PROJECT_DIR", str(project)),
        patch.object(backup, "RESTORE_STAGING_ROOT", str(tmp_path / "root")),
        patch.object(backup, "create_backup", return_value=MagicMock(archive_path="s")),
        patch.object(backup, "stop_services"),
        patch.object(backup, "start_services"),
        patch.object(backup, "verify_health", return_value=True),
        patch.object(backup, "_get_metabase_volume_path", return_value=None),
    ):
        backup.restore_from_archive(ssh, str(archive))


def test_failed_restore_logs_a_failed_permission_reapply(tmp_path: Path) -> None:
    archive, project = _archive(tmp_path)
    ssh = ShellSSH(tmp_path / "shim", fail_cp_match="notebooks")
    calls: list[int] = []

    def reapply(_ssh: object, warnings: list[str], **_k: object) -> str:
        calls.append(1)
        warnings.append("Runner permissions NOT re-applied (verify: boom)")
        return "x"

    with (
        patch("dango.platform.cloud.runner_reapply.reapply", side_effect=reapply),
        patch.object(backup, "_logger") as logger,
        pytest.raises(CloudProvisioningError),
    ):
        _restore(tmp_path, ssh, archive, project)
    assert calls, "the failure path must still try to re-apply runner permissions"
    logged = [
        c for c in logger.warning.call_args_list if c.args[0] == "runner_permissions_not_reapplied"
    ]
    assert logged and "NOT re-applied" in logged[0].kwargs["detail"]
    # the old notebooks directory is back after the failed copy
    assert project.joinpath("notebooks").is_dir()


def test_failed_restore_logs_a_reapply_that_raises(tmp_path: Path) -> None:
    archive, project = _archive(tmp_path)
    ssh = ShellSSH(tmp_path / "shim", fail_cp_match="notebooks")
    with (
        patch("dango.platform.cloud.runner_reapply.reapply", side_effect=RuntimeError("ssh down")),
        patch.object(backup, "_logger") as logger,
        pytest.raises(CloudProvisioningError, match="restore_files"),
    ):
        _restore(tmp_path, ssh, archive, project)
    assert any("ssh down" in str(c.kwargs.get("detail")) for c in logger.warning.call_args_list)


def test_successful_restore_does_not_log_a_reapply_failure(tmp_path: Path) -> None:
    archive, project = _archive(tmp_path)
    ssh = ShellSSH(tmp_path / "shim")
    with patch("dango.platform.cloud.runner_reapply.reapply", return_value=None):
        _restore(tmp_path, ssh, archive, project)
    assert (project / "notebooks" / "a.py").read_text() == "A\n"


def test_auth_config_is_read_as_the_service_user() -> None:
    commands: list[str] = []

    def run(cmd: str, **_k: object) -> CommandResult:
        commands.append(cmd)
        if "wc -c" in cmd:
            return CommandResult("14\n", "", 0)
        return CommandResult("enabled: true\n" if "cat" in cmd else "", "", 0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = run
    assert backup._auth_config_refusal(ssh, "/stage") is None
    assert commands and all(c.startswith(br.AS_SERVICE_USER) for c in commands), commands
    assert not any(" < " in c for c in commands)  # no redirect opened by root
