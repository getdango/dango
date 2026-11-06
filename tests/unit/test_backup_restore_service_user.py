"""tests/unit/test_backup_restore_service_user.py

Server restores write project files as the service user and refuse symlinked root-written paths.
"""

from __future__ import annotations

import shlex
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from tests.factories.backup_shell import ShellSSH, cp_log, run_spaces_restore

_B = "dango.platform.cloud.backup"
_TOP = "backup-20260101-000000"


def _content(root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "data" / "warehouse.duckdb").write_bytes(b"archive-warehouse")
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "project.yml").write_text("from-archive\n")
    (root / ".dango" / "metabase.yml").write_text("url: http://x\n")
    (root / "dbt" / "models").mkdir(parents=True)
    (root / "dbt" / "models" / "m.sql").write_text("select 'archive'\n")
    (root / "metabase").mkdir()
    (root / "metabase" / "metabase.db.mv.db").write_bytes(b"h2-archive")
    (root / "manifest.json").write_text("{}")


def _project_with_linked_data(tmp_path: Path) -> tuple[Path, Path]:
    """A project whose data/ is a legitimate symlink to separate storage."""
    volume = tmp_path / "block-storage"
    volume.mkdir()
    (volume / "warehouse.duckdb").write_bytes(b"live-warehouse")
    project = tmp_path / "project"
    project.mkdir()
    (project / "data").symlink_to(volume, target_is_directory=True)
    return project, volume


def _archive(tmp_path: Path) -> str:
    content = tmp_path / "content"
    _content(content)
    archive = tmp_path / f"{_TOP}.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=_TOP)
    return str(archive)


@contextmanager
def _server(tmp_path: Path, project: Path, mb_volume: Path) -> Iterator[MagicMock]:
    (tmp_path / "staging-root").mkdir(exist_ok=True)
    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(
            f"{_B}.create_backup", return_value=MagicMock(archive_path="safety.tar.gz")
        ) as safety,
        patch(f"{_B}.stop_services"),
        patch(f"{_B}.start_services"),
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=str(mb_volume)),
    ):
        yield safety


def _assert_project_copies_as_dango(shim_dir: Path, project: Path, expected_min: int) -> None:
    log = cp_log(shim_dir)
    into_project = [(user, dest) for user, dest in log if dest.startswith(str(project))]
    assert len(into_project) >= expected_min, log
    assert all(user == "dango" for user, _ in into_project), into_project


# ---------------------------------------------------------------------------
# restore_from_archive (SSH)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_server_restore_writes_project_files_as_service_user(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    project, volume = _project_with_linked_data(tmp_path)
    mb_volume = tmp_path / "mb-volume"
    mb_volume.mkdir()
    ssh = ShellSSH(tmp_path / "shim")

    with _server(tmp_path, project, mb_volume):
        restore_from_archive(ssh, _archive(tmp_path))

    _assert_project_copies_as_dango(ssh.shim_dir, project, expected_min=3)
    # Root writes only the Metabase volume.
    root_writes = [dest for user, dest in cp_log(ssh.shim_dir) if user == "root"]
    assert root_writes and all(dest.startswith(str(mb_volume)) for dest in root_writes)
    assert (mb_volume / "metabase.db.mv.db").read_bytes() == b"h2-archive"
    # A legitimately symlinked data/ keeps working: the restore lands on the target.
    assert (project / "data").is_symlink()
    assert (volume / "warehouse.duckdb").read_bytes() == b"archive-warehouse"
    assert (project / "dbt" / "models" / "m.sql").read_text() == "select 'archive'\n"


@pytest.mark.unit
def test_server_restore_sanitizes_metabase_yaml_outside_project(tmp_path: Path) -> None:
    """The root-run sanitizer writes into staging; the project copy is made as dango."""
    from dango.platform.cloud.backup import restore_from_archive

    project = tmp_path / "project"
    project.mkdir()
    mb_volume = tmp_path / "mb-volume"
    mb_volume.mkdir()
    ssh = ShellSSH(tmp_path / "shim")

    with _server(tmp_path, project, mb_volume):
        restore_from_archive(ssh, _archive(tmp_path))

    sanitize = [c for c in ssh.commands if "/srv/dango/venv" in c]
    assert sanitize
    destination = shlex.split(sanitize[-1])[-1]
    assert not destination.startswith(str(project)), destination


@pytest.mark.unit
def test_server_restore_refuses_symlinked_project_dir(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    real = tmp_path / "real-project"
    real.mkdir()
    project = tmp_path / "project"
    project.symlink_to(real, target_is_directory=True)
    mb_volume = tmp_path / "mb-volume"
    mb_volume.mkdir()
    ssh = ShellSSH(tmp_path / "shim")

    with (
        _server(tmp_path, project, mb_volume) as safety,
        pytest.raises(CloudProvisioningError, match="symlink"),
    ):
        restore_from_archive(ssh, _archive(tmp_path))

    safety.assert_not_called()
    assert cp_log(ssh.shim_dir) == []


@pytest.mark.unit
@pytest.mark.parametrize("linked", ["file", "volume"])
def test_server_restore_refuses_symlinked_h2_destination(tmp_path: Path, linked: str) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "metabase.db.mv.db").write_bytes(b"outside")
    if linked == "volume":
        mb_volume = tmp_path / "mb-volume"
        mb_volume.symlink_to(outside, target_is_directory=True)
    else:
        mb_volume = tmp_path / "mb-volume"
        mb_volume.mkdir()
        (mb_volume / "metabase.db.mv.db").symlink_to(outside / "metabase.db.mv.db")
    ssh = ShellSSH(tmp_path / "shim")

    with (
        _server(tmp_path, project, mb_volume),
        pytest.raises(CloudProvisioningError, match="restore_metabase"),
    ):
        restore_from_archive(ssh, _archive(tmp_path))

    assert (outside / "metabase.db.mv.db").read_bytes() == b"outside"


# ---------------------------------------------------------------------------
# restore_from_spaces (on server, as root)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_spaces_restore_writes_project_files_as_service_user(tmp_path: Path) -> None:
    project, volume = _project_with_linked_data(tmp_path)
    _content(tmp_path / _TOP)
    mb_volume = tmp_path / "mb-volume"
    mb_volume.mkdir()

    shim_dir, _events = run_spaces_restore(tmp_path, project, _TOP, metabase_vol=mb_volume)

    _assert_project_copies_as_dango(shim_dir, project, expected_min=4)
    copied_to = [dest for _, dest in cp_log(shim_dir)]
    assert any(dest.endswith(".dango/metabase.yml") for dest in copied_to)
    assert (project / "data").is_symlink()
    assert (volume / "warehouse.duckdb").read_bytes() == b"archive-warehouse"
    assert "http://x" in (project / ".dango" / "metabase.yml").read_text()
    assert (mb_volume / "metabase.db.mv.db").read_bytes() == b"h2-archive"


@pytest.mark.unit
def test_spaces_restore_refuses_symlinked_project_dir(tmp_path: Path) -> None:
    real = tmp_path / "real-project"
    real.mkdir()
    project = tmp_path / "project"
    project.symlink_to(real, target_is_directory=True)
    _content(tmp_path / _TOP)

    with pytest.raises(CloudProvisioningError, match="symlink"):
        run_spaces_restore(tmp_path, project, _TOP)

    assert cp_log(tmp_path / "shim") == []


@pytest.mark.unit
def test_spaces_restore_refuses_symlinked_h2_destination(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _content(tmp_path / _TOP)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside")
    mb_volume = tmp_path / "mb-volume"
    mb_volume.mkdir()
    (mb_volume / "metabase.db.mv.db").symlink_to(outside)

    with pytest.raises(CloudProvisioningError, match="symlink"):
        run_spaces_restore(tmp_path, project, _TOP, metabase_vol=mb_volume)

    assert outside.read_bytes() == b"outside"
