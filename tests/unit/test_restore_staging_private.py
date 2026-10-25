"""tests/unit/test_restore_staging_private.py

Restore upload and staging directories are root-only: no local user (a running restricted
runner included) can list or read an uploaded archive or the extracted backup during a restore.
"""

from __future__ import annotations

import re
import shlex
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote
from dango.platform.cloud import backup
from dango.platform.cloud import backup_restore as br
from dango.platform.cloud.ssh import CommandResult
from tests.factories.backup_shell import ShellSSH, run_spaces_restore, shim_env

pytestmark = pytest.mark.unit

_UUID = re.compile(r"[0-9a-f]{32}")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _dash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["dash", "-c", script], capture_output=True, text=True, check=False, env=env
    )


def test_private_root_is_not_a_world_visible_location() -> None:
    assert br.RESTORE_PRIVATE_ROOT.startswith("/srv/dango/")
    assert backup.RESTORE_STAGING_ROOT == br.RESTORE_PRIVATE_ROOT
    assert not backup.RESTORE_STAGING_ROOT.startswith("/tmp")


@pytest.mark.parametrize("service_access", [True, False])
def test_private_dir_command_is_valid_dash_and_root_only(service_access: bool) -> None:
    path = f"{br.RESTORE_PRIVATE_ROOT}/x-{'a' * 32}"
    cmd = br.private_dir_cmd(path, service_access=service_access)
    assert _dash("exit 0; " + cmd).returncode == 0
    check = subprocess.run(["dash", "-n"], input=cmd, capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr
    assert "/tmp" not in cmd
    assert "0644" not in cmd and "0755" not in cmd
    if service_access:
        assert "root:dango" in cmd and "0750" in cmd
    else:
        assert f"root:dango {path}" not in cmd and "mkdir -m 0700" in cmd


@pytest.mark.parametrize(("service_access", "mode"), [(True, 0o750), (False, 0o700)])
def test_private_dir_command_creates_modes(tmp_path: Path, service_access: bool, mode: int) -> None:
    root = tmp_path / "srv" / ".restore"
    shim = tmp_path / "shim"
    env = shim_env(shim)  # chown is a no-op locally (no root, no dango user)
    target = root / f"d-{uuid.uuid4().hex}"
    cmd = br.private_dir_cmd(str(target), service_access=service_access)
    result = _dash(cmd, env)
    assert result.returncode == 0, result.stderr
    assert _mode(target) == mode
    assert _mode(root) == 0o710 and not root.is_symlink()
    # A second restore reuses the root; an existing target is refused (never reused).
    again = _dash(cmd, env)
    assert again.returncode != 0


def test_private_dir_command_refuses_symlinked_root(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / ".restore"
    root.symlink_to(elsewhere)
    env = shim_env(tmp_path / "shim")
    result = _dash(br.private_dir_cmd(str(root / "d-1"), service_access=True), env)
    assert result.returncode != 0
    assert list(elsewhere.iterdir()) == []


def test_server_restore_stages_inside_private_root(tmp_path: Path) -> None:
    import tarfile

    content = tmp_path / "content" / "backup-1"
    (content / "data").mkdir(parents=True)
    (content / "data" / "warehouse.duckdb").write_bytes(b"db")
    (content / "manifest.json").write_text("{}")
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(content), arcname="backup-1")
    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "private-root"
    ssh = ShellSSH(tmp_path / "shim")
    with (
        patch.object(backup, "PROJECT_DIR", str(project)),
        patch.object(backup, "RESTORE_STAGING_ROOT", str(root)),
        patch.object(backup, "create_backup", return_value=MagicMock(archive_path="s")),
        patch.object(backup, "stop_services"),
        patch.object(backup, "start_services"),
        patch.object(backup, "verify_health", return_value=True),
        patch.object(backup, "_get_metabase_volume_path", return_value=None),
    ):
        backup.restore_from_archive(ssh, str(archive))
    tar_cmd = next(c for c in ssh.commands if "tar -xzf" in c)
    staging = shlex.split(tar_cmd.split("-C ", 1)[1])[0]
    assert staging.startswith(f"{root}/dango-restore-staging-")
    assert _UUID.fullmatch(staging.rsplit("-", 1)[1])
    assert (project / "data" / "warehouse.duckdb").read_bytes() == b"db"
    assert list(root.iterdir()) == [], "staging left behind"
    create = next(c for c in ssh.commands if "mkdir -m 0750" in c)
    assert "root:dango" in create and "/tmp" not in create


def test_stale_sweep_covers_private_root_and_tmp() -> None:
    commands: list[str] = []
    ssh = MagicMock()
    ssh.exec_command.side_effect = lambda c, **_k: commands.append(c) or CommandResult("", "", 0)
    backup._sweep_stale_leftovers(ssh)
    sweep = next(c for c in commands if "-exec rm -rf" in c and "dango-restore-staging-*" in c)
    assert br.RESTORE_PRIVATE_ROOT in sweep and "/tmp" in sweep
    for name in ("dango-restore-upload-*", "dango-restore-meta-*"):
        assert name in sweep
    assert "-user root" in sweep and "-mmin +" in sweep


def _local_restore(tmp_path: Path, *, restore_error: Exception | None) -> tuple[MagicMock, Any]:
    from dango.platform.cloud.backup import RestoreResult

    archive = tmp_path / "backup-1.tar.gz"
    archive.write_bytes(b"x")
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
    ssh.connect.return_value = ssh
    result = RestoreResult("r", True, True, 1.0)
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.config.loader.ConfigLoader") as loader,
        patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
        patch(
            "dango.platform.cloud.backup.restore_from_archive",
            side_effect=restore_error,
            return_value=result,
        ) as restore,
    ):
        loader.return_value.load_cloud_config.return_value = MagicMock()
        out = CliRunner().invoke(
            remote, ["backup", "restore", str(archive), "--from-local", "-y"], catch_exceptions=True
        )
    return ssh, (out, restore)


@pytest.mark.parametrize("failing", [False, True])
def test_local_file_upload_goes_into_private_directory(tmp_path: Path, failing: bool) -> None:
    ssh, (_out, restore) = _local_restore(
        tmp_path, restore_error=RuntimeError("boom") if failing else None
    )
    commands = [c.args[0] for c in ssh.exec_command.call_args_list]
    upload_dest = ssh.upload_file.call_args.args[1]
    directory = upload_dest.rsplit("/", 1)[0]
    assert directory.startswith(br.RESTORE_PRIVATE_ROOT + "/dango-restore-upload-")
    assert not upload_dest.startswith("/tmp")
    make = next(c for c in commands if "mkdir -m 0700" in c and directory in c)
    assert f"root:dango {directory}" not in make
    # created before the upload, archive locked down right after, whole directory removed
    order = [make, f"chmod 0600 {shlex.quote(upload_dest)}", f"rm -rf {shlex.quote(directory)}"]
    positions = [next(i for i, c in enumerate(commands) if c == want) for want in order]
    assert positions == sorted(positions)
    assert restore.call_args.args[1] == upload_dest


def test_scheduled_restore_stages_in_private_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    top = tmp_path / "backup-1"
    (top / "data").mkdir(parents=True)
    (top / "data" / "warehouse.duckdb").write_bytes(b"db")
    (top / "manifest.json").write_text("{}")
    project.mkdir()
    root = tmp_path / "private"
    events: list[str] = []
    run_spaces_restore(tmp_path, project, "backup-1", events=events)
    extract = next(e for e in events if e.startswith("run: tar -xzf"))
    staging = Path(shlex.split(extract)[shlex.split(extract).index("-C") + 1])
    assert staging.parent == root and staging.name.startswith("dango-restore-staging-")
    assert not root.exists() or list(root.iterdir()) == []
    assert not any("chmod" in e and "755" in e for e in events)
    assert (project / "data" / "warehouse.duckdb").read_bytes() == b"db"
