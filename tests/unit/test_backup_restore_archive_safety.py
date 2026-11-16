"""tests/unit/test_backup_restore_archive_safety.py

Restores refuse archives with links or special files and never use predictable or shared staging paths.
"""

from __future__ import annotations

import shlex
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.exceptions import CloudProvisioningError
from tests.factories.backup_shell import ShellSSH, cp_log, run_spaces_restore

_B = "dango.platform.cloud.backup"
_LB = "dango.cli.commands.local_backup"
_TOP = "backup-20260101-000000"
#: Archive members that root reads or writes during a restore.
_LINKED_MEMBERS = ["metabase.sanitized.yml", ".dango/metabase.yml", "metabase/metabase.db.mv.db"]


def _content(root: Path) -> None:
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "project.yml").write_text("from-archive\n")
    (root / "metabase").mkdir()
    (root / "manifest.json").write_text("{}")


def _hostile_content(root: Path, member: str, target: Path) -> None:
    """Archive content whose *member* is a symlink to *target* (outside everything)."""
    _content(root)
    link = root / member
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        link.unlink()
    link.symlink_to(target)


def _outside(tmp_path: Path) -> Path:
    target = tmp_path / "outside" / "authorized_keys"
    target.parent.mkdir(parents=True)
    target.write_text("ssh-ed25519 AAAA owner\n")
    return target


def _tar(root: Path, archive: Path, top: str = _TOP) -> Path:
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(root), arcname=top)
    return archive


# ---------------------------------------------------------------------------
# restore_from_archive (SSH)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("member", _LINKED_MEMBERS)
def test_server_restore_refuses_archive_with_link_member(tmp_path: Path, member: str) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    target = _outside(tmp_path)
    _hostile_content(tmp_path / "content", member, target)
    archive = _tar(tmp_path / "content", tmp_path / f"{_TOP}.tar.gz")
    project = tmp_path / "project"
    project.mkdir()
    mb_volume = tmp_path / "mb-volume"
    mb_volume.mkdir()
    (tmp_path / "staging-root").mkdir()
    ssh = ShellSSH(tmp_path / "shim")

    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(f"{_B}.create_backup") as safety,
        patch(f"{_B}.stop_services") as stop,
        patch(f"{_B}.start_services") as start,
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=str(mb_volume)),
        pytest.raises(CloudProvisioningError, match="link"),
    ):
        restore_from_archive(ssh, str(archive))

    assert target.read_text() == "ssh-ed25519 AAAA owner\n"
    safety.assert_not_called()
    stop.assert_not_called()
    start.assert_not_called()
    assert cp_log(ssh.shim_dir) == []
    assert not any("/srv/dango/venv" in c for c in ssh.commands), "sanitizer ran"
    assert list((tmp_path / "staging-root").iterdir()) == [], "staging left behind"


@pytest.mark.unit
def test_server_restore_sanitizes_into_private_dir_outside_staging(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    content = tmp_path / "content"
    _content(content)
    (content / ".dango" / "metabase.yml").write_text("url: http://x\n")
    archive = _tar(content, tmp_path / f"{_TOP}.tar.gz")
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "staging-root").mkdir()
    ssh = ShellSSH(tmp_path / "shim")

    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(f"{_B}.create_backup", return_value=MagicMock(archive_path="safety.tar.gz")),
        patch(f"{_B}.stop_services"),
        patch(f"{_B}.start_services"),
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=None),
    ):
        restore_from_archive(ssh, str(archive))

    sanitize = [c for c in ssh.commands if "/srv/dango/venv" in c]
    assert sanitize
    destination = Path(shlex.split(sanitize[-1])[-1])
    assert "dango-restore-staging-" not in str(destination), destination
    assert not str(destination).startswith(str(project))
    # The private directory is created fresh by mktemp and removed afterwards.
    assert any("mkdir -m 0750" in c and "dango-restore-meta-" in c for c in ssh.commands)
    assert not destination.parent.exists()


# ---------------------------------------------------------------------------
# restore_from_spaces (on server)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("member", _LINKED_MEMBERS)
def test_spaces_restore_refuses_archive_with_link_member(tmp_path: Path, member: str) -> None:
    target = _outside(tmp_path)
    _hostile_content(tmp_path / _TOP, member, target)
    project = tmp_path / "project"
    project.mkdir()
    mb_volume = tmp_path / "mb-volume"
    mb_volume.mkdir()
    events: list[str] = []

    with pytest.raises(CloudProvisioningError, match="link"):
        run_spaces_restore(tmp_path, project, _TOP, metabase_vol=mb_volume, events=events)

    assert target.read_text() == "ssh-ed25519 AAAA owner\n"
    assert "stop" not in events and "start" not in events
    assert not [e for e in events if "tar -xzf" in e], "archive was extracted"
    assert cp_log(tmp_path / "shim") == []


@pytest.mark.unit
def test_spaces_restore_never_uses_predictable_tmp_staging(tmp_path: Path) -> None:
    """A path pre-planted at /tmp/<archive name> is neither extracted into nor read."""
    top = f"backup-{uuid.uuid4().hex[:12]}"
    planted = Path("/tmp") / top
    decoy = tmp_path / "decoy"
    (decoy / ".dango").mkdir(parents=True)
    (decoy / ".dango" / "project.yml").write_text("planted\n")
    planted.symlink_to(decoy, target_is_directory=True)
    try:
        _content(tmp_path / top)
        project = tmp_path / "project"
        project.mkdir()

        _shim, events = run_spaces_restore(tmp_path, project, top)

        assert planted.is_symlink(), "the planted path was removed or replaced"
        assert (decoy / ".dango" / "project.yml").read_text() == "planted\n"
        assert (project / ".dango" / "project.yml").read_text() == "from-archive\n"
        commands = " ".join(events)
        assert str(planted) not in commands
        assert "--strip-components=1" in commands
    finally:
        if planted.is_symlink():
            planted.unlink()
        else:
            shutil.rmtree(planted, ignore_errors=True)


# ---------------------------------------------------------------------------
# Local `dango backup restore`
# ---------------------------------------------------------------------------


def _local_restore(project: Path, archive: Path) -> Any:
    from dango.cli.commands.local_backup import backup_group

    with patch(f"{_LB}.require_project_context", return_value=project):
        return CliRunner().invoke(
            backup_group, ["restore", str(archive), "--yes"], obj={"project_root": project}
        )


@pytest.mark.unit
@pytest.mark.parametrize("member", [".dango/project.yml", "data/warehouse.duckdb"])
def test_local_restore_refuses_archive_with_link_member(tmp_path: Path, member: str) -> None:
    target = _outside(tmp_path)
    _hostile_content(tmp_path / "content", member, target)
    archive = _tar(tmp_path / "content", tmp_path / f"{_TOP}.tar.gz")
    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango" / "project.yml").write_text("live\n")

    result = _local_restore(project, archive)

    assert result.exit_code != 0
    assert "Restore complete" not in result.output
    assert target.read_text() == "ssh-ed25519 AAAA owner\n"
    assert (project / ".dango" / "project.yml").read_text() == "live\n"
    assert not (project / ".dango" / "backups").exists(), "restore started before refusing"


@pytest.mark.unit
def test_local_restore_skips_server_archive_h2_files(tmp_path: Path) -> None:
    content = tmp_path / "content"
    _content(content)
    (content / "metabase" / "metabase.db.mv.db").write_bytes(b"h2")
    archive = _tar(content, tmp_path / f"{_TOP}.tar.gz")
    project = tmp_path / "project"
    project.mkdir()

    result = _local_restore(project, archive)

    assert result.exit_code == 0, result.output
    assert (project / ".dango" / "project.yml").read_text() == "from-archive\n"
    assert not (project / "metabase").exists(), "Metabase H2 files written into the project"


@pytest.fixture(autouse=True)
def _no_stray_tmp_staging() -> Any:
    """Remove any /tmp staging left by a failing assertion (pre-fix code extracts there)."""
    yield
    shutil.rmtree(Path("/tmp") / _TOP, ignore_errors=True)


@pytest.mark.unit
def test_server_restore_refused_when_archive_check_cannot_run(tmp_path: Path) -> None:
    """If `find` itself fails on the staging tree, the restore fails closed."""
    from dango.platform.cloud.backup import restore_from_archive

    _content(tmp_path / "content")
    archive = _tar(tmp_path / "content", tmp_path / f"{_TOP}.tar.gz")
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "staging-root").mkdir()
    ssh = ShellSSH(tmp_path / "shim", fail_find=True)

    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(f"{_B}.create_backup") as safety,
        patch(f"{_B}.stop_services") as stop,
        patch(f"{_B}.start_services"),
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=None),
        pytest.raises(CloudProvisioningError, match="link"),
    ):
        restore_from_archive(ssh, str(archive))

    safety.assert_not_called()
    stop.assert_not_called()
    assert cp_log(ssh.shim_dir) == []


@pytest.mark.unit
def test_spaces_restore_removes_staging_when_service_stop_fails(tmp_path: Path) -> None:
    import tempfile

    _content(tmp_path / _TOP)
    project = tmp_path / "project"
    project.mkdir()
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        made.append(path)
        return path

    with (
        patch("dango.platform.cloud.scheduled_backup.tempfile.mkdtemp", recording_mkdtemp),
        pytest.raises(RuntimeError, match="systemctl"),
    ):
        run_spaces_restore(tmp_path, project, _TOP, stop_error=RuntimeError("systemctl hung"))

    assert made, "no staging directory was created"
    assert [p for p in made if Path(p).exists()] == [], "staging left behind"
