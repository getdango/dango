"""tests/unit/test_backup_copy_failures.py

Server backup/restore shell commands must fail on a failed copy and remove stale WAL files on restore.
"""

from __future__ import annotations

import json
import re
import tarfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from tests.factories.backup_shell import (
    ShellSSH,
    duckdb_state,
    make_duckdb,
    make_duckdb_with_leftover_wal,
    make_sqlite,
    make_sqlite_with_leftover_wal,
    sqlite_values,
)

_B = "dango.platform.cloud.backup"
_TOP = "backup-20260101-000000"


def _build_archive(tmp_path: Path, files: dict[str, bytes | None]) -> str:
    """Build a server-style archive; a ``None`` value means a real DuckDB/SQLite file."""
    content = tmp_path / "archive-content"
    for rel, data in files.items():
        target = content / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            target.write_bytes(data)
        elif rel.endswith(".duckdb"):
            make_duckdb(target, "archive")
        else:
            make_sqlite(target, "archive")
    archive = tmp_path / "backups" / f"{_TOP}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=_TOP)
    return str(archive)


@contextmanager
def _restore_env(tmp_path: Path, project: Path) -> Iterator[MagicMock]:
    """Point restore_from_archive at tmp dirs; stub only server-only steps. Yields start_services."""
    (tmp_path / "staging-root").mkdir(exist_ok=True)
    (tmp_path / "mb-volume").mkdir(exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(f"{_B}.create_backup", return_value=MagicMock(archive_path="safety.tar.gz")),
        patch(f"{_B}.stop_services"),
        patch(f"{_B}.start_services") as start,
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=str(tmp_path / "mb-volume")),
    ):
        yield start


def _full_archive(tmp_path: Path) -> str:
    return _build_archive(
        tmp_path,
        {
            "manifest.json": b"{}",
            "data/warehouse.duckdb": None,
            ".dango/auth.db": None,
            ".dango/project.yml": b"project: {}\n",
            "dbt/models/m.sql": b"select 1\n",
            "metabase/metabase.db.mv.db": b"h2",
        },
    )


# ---------------------------------------------------------------------------
# restore_from_archive
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_restore_commands_remove_wal_and_do_not_swallow_cp(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    project = tmp_path / "project"
    duck_wal = make_duckdb_with_leftover_wal(project / "data" / "warehouse.duckdb")
    auth_wal, auth_shm = make_sqlite_with_leftover_wal(project / ".dango" / "auth.db")
    archive = _full_archive(tmp_path)
    ssh = ShellSSH(tmp_path / "shim")

    with _restore_env(tmp_path, project):
        restore_from_archive(ssh, archive)

    # Behaviour: sidecars gone, only archive rows survive.
    assert not duck_wal.exists() and not auth_wal.exists() and not auth_shm.exists()
    tables, values = duckdb_state(project / "data" / "warehouse.duckdb")
    assert tables == {"t"} and values == ["archive"]
    assert sqlite_values(project / ".dango" / "auth.db") == ["archive"]
    assert (project / "dbt" / "models" / "m.sql").read_text() == "select 1\n"

    # Command shape: the WAL removal precedes the warehouse copy; no copy is followed by `|| true`.
    restore_cmd = next(c for c in ssh.commands if "dango-restore-staging" in c and "cp " in c)
    rm_at = restore_cmd.find(f"rm -f {project}/data/warehouse.duckdb.wal")
    copy = re.search(r"cp \S+/data/warehouse\.duckdb ", restore_cmd)
    assert copy is not None
    cp_at = copy.start()
    assert rm_at != -1 and rm_at < cp_at
    staging_copies = [c for c in ssh.commands if "dango-restore-staging" in c and "cp " in c]
    assert staging_copies
    assert not [c for c in staging_copies if "|| true" in c]


@pytest.mark.unit
@pytest.mark.parametrize("failing", ["data/warehouse.duckdb", "dbt/models"])
def test_restore_files_cp_failure_raises(tmp_path: Path, failing: str) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    project = tmp_path / "project"
    archive = _full_archive(tmp_path)
    ssh = ShellSSH(tmp_path / "shim", fail_cp_match=failing)

    with _restore_env(tmp_path, project) as start, pytest.raises(CloudProvisioningError) as exc:
        restore_from_archive(ssh, archive)

    assert "restore_files" in str(exc.value)
    start.assert_called_once()
    # The partial restore is handed back to the service user before services restart.
    assert any(c.startswith("chown -R dango:dango") for c in ssh.commands)


@pytest.mark.unit
def test_restore_skips_missing_optional_files(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    project = tmp_path / "project"
    (project / "dbt").mkdir(parents=True)
    (project / "dbt" / "packages.yml").write_text("live\n")
    archive = _build_archive(
        tmp_path, {"manifest.json": b"{}", ".dango/project.yml": b"project: {}\n"}
    )
    ssh = ShellSSH(tmp_path / "shim")

    with _restore_env(tmp_path, project):
        result = restore_from_archive(ssh, archive)

    assert result.services_restarted is True
    assert (project / ".dango" / "project.yml").read_text() == "project: {}\n"
    assert (project / "dbt" / "packages.yml").read_text() == "live\n"


@pytest.mark.unit
def test_restore_layout_check_requires_real_content(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import restore_from_archive

    project = tmp_path / "project"
    archive = _build_archive(tmp_path, {"manifest.json": b"{}"})
    ssh = ShellSSH(tmp_path / "shim")

    with (
        _restore_env(tmp_path, project),
        pytest.raises(CloudProvisioningError, match="unexpected layout"),
    ):
        restore_from_archive(ssh, archive)

    assert not any(c.startswith("chown") for c in ssh.commands)
    assert not any("cp " in c for c in ssh.commands)


# ---------------------------------------------------------------------------
# create_backup (_create_archive)
# ---------------------------------------------------------------------------


@contextmanager
def _create_env(tmp_path: Path, project: Path) -> Iterator[Path]:
    """Point create_backup at tmp dirs; stub only server-only steps. Yields the backup dir."""
    backups = tmp_path / "backups"
    stamp = f"test-{uuid.uuid4().hex[:12]}"
    project.mkdir(parents=True, exist_ok=True)
    try:
        with (
            patch(f"{_B}.PROJECT_DIR", str(project)),
            patch(f"{_B}.BACKUP_DIR", str(backups)),
            patch(f"{_B}.time.strftime", return_value=stamp),
            patch(f"{_B}._check_disk_space"),
            patch(f"{_B}.stop_services"),
            patch(f"{_B}.start_services"),
            patch(f"{_B}._checkpoint_duckdb", return_value=True),
            patch(f"{_B}._checkpoint_auth_db", return_value=True),
            patch(f"{_B}._get_metabase_volume_path", return_value=None),
        ):
            yield backups
    finally:
        staging = Path(f"/tmp/backup-{stamp}")
        if staging.exists():
            import shutil

            shutil.rmtree(staging)


def _populate_project(project: Path) -> None:
    (project / "data").mkdir(parents=True, exist_ok=True)
    (project / "data" / "warehouse.duckdb").write_bytes(b"duck")
    (project / ".dango").mkdir(parents=True, exist_ok=True)
    (project / ".dango" / "auth.db").write_bytes(b"auth")
    (project / ".dango" / "project.yml").write_text("project: {}\n")
    (project / "dbt" / "models").mkdir(parents=True, exist_ok=True)
    (project / "dbt" / "models" / "m.sql").write_text("select 1\n")


@pytest.mark.unit
@pytest.mark.parametrize("failing", ["data/warehouse.duckdb", ".dango/project.yml", "dbt/models"])
def test_create_backup_fails_when_warehouse_copy_fails(tmp_path: Path, failing: str) -> None:
    from dango.platform.cloud.backup import create_backup

    project = tmp_path / "project"
    _populate_project(project)
    ssh = ShellSSH(tmp_path / "shim", fail_cp_match=failing)

    with _create_env(tmp_path, project) as backups, pytest.raises(CloudProvisioningError) as exc:
        create_backup(ssh)

    assert "copy_files" in str(exc.value)
    assert not list(backups.glob("*.tar.gz")), "an archive was produced despite a failed copy"


@pytest.mark.unit
def test_create_backup_without_warehouse_ok(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import create_backup

    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango" / "project.yml").write_text("project: {}\n")
    ssh = ShellSSH(tmp_path / "shim")

    with _create_env(tmp_path, project) as backups:
        result = create_backup(ssh)

    assert Path(result.archive_path).exists()
    assert list(backups.glob("*.tar.gz"))
    with tarfile.open(result.archive_path) as tf:
        names = tf.getnames()
    assert any(n.endswith(".dango/project.yml") for n in names)
    assert not any(n.endswith("warehouse.duckdb") for n in names)
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["backup_type"] == "pre-deploy"
