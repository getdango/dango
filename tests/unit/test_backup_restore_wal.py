"""tests/unit/test_backup_restore_wal.py

Restores must remove a live database's leftover write-ahead log before overwriting the database file.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tests.factories.backup_shell import (
    duckdb_state,
    make_duckdb,
    make_duckdb_with_leftover_wal,
    make_sqlite,
    make_sqlite_with_leftover_wal,
    sqlite_values,
)

_REAL_RUN = subprocess.run
_SB = "dango.platform.cloud.scheduled_backup"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("db_rel_path", "expected"),
    [
        ("data/warehouse.duckdb", ["data/warehouse.duckdb.wal"]),
        (".dango/auth.db", [".dango/auth.db-wal", ".dango/auth.db-shm"]),
        ("x.db", ["x.db-wal", "x.db-shm"]),
        (".dango/project.yml", []),
        (".dango/logs/audit.jsonl", []),
        ("data/warehouse.duckdb.wal", []),
        ("dbt/profiles.yml", []),
    ],
)
def test_wal_sidecar_names(db_rel_path: str, expected: list[str]) -> None:
    from dango.platform.cloud.backup import wal_sidecar_names

    assert wal_sidecar_names(db_rel_path) == expected


def _archive_from_dir(
    source: Path, archive_path: Path, top: str = "backup-20260101-000000"
) -> Path:
    """Tar *source*'s contents under a single top-level directory, like a server backup."""
    with tarfile.open(archive_path, mode="w:gz") as tf:
        tf.add(str(source), arcname=top)
    return archive_path


def _local_restore(project: Path, archive: Path) -> Any:
    from dango.cli.commands.local_backup import backup_group

    with patch("dango.cli.commands.local_backup.require_project_context", return_value=project):
        return CliRunner().invoke(
            backup_group, ["restore", str(archive), "--yes"], obj={"project_root": project}
        )


@pytest.mark.unit
def test_local_restore_removes_leftover_duckdb_wal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    live_db = project / "data" / "warehouse.duckdb"
    wal = make_duckdb_with_leftover_wal(live_db)

    content = tmp_path / "content"
    make_duckdb(content / "data" / "warehouse.duckdb", "archive")
    archive = _archive_from_dir(content, tmp_path / "backup-20260101-000000.tar.gz")

    result = _local_restore(project, archive)

    assert result.exit_code == 0, result.output
    assert "Restore complete" in result.output
    assert not wal.exists(), "live DuckDB WAL left in place after restore"
    tables, values = duckdb_state(live_db)
    assert tables == {"t"}, f"live-only data merged into restored warehouse: {tables}"
    assert values == ["archive"]


@pytest.mark.unit
def test_restore_removes_sqlite_sidecars(tmp_path: Path) -> None:
    project = tmp_path / "project"
    live_auth = project / ".dango" / "auth.db"
    wal, shm = make_sqlite_with_leftover_wal(live_auth)

    content = tmp_path / "content"
    make_sqlite(content / ".dango" / "auth.db", "archive")
    archive = _archive_from_dir(content, tmp_path / "backup-20260101-000000.tar.gz")

    result = _local_restore(project, archive)

    assert result.exit_code == 0, result.output
    assert not wal.exists(), "auth.db-wal left in place after restore"
    assert not shm.exists(), "auth.db-shm left in place after restore"
    assert sqlite_values(live_auth) == ["archive"]


@pytest.mark.unit
def test_restore_from_spaces_removes_wal(tmp_path: Path) -> None:
    from dango.platform.cloud import scheduled_backup as sb

    project = tmp_path / "project"
    live_db = project / "data" / "warehouse.duckdb"
    duck_wal = make_duckdb_with_leftover_wal(live_db)
    live_auth = project / ".dango" / "auth.db"
    auth_wal, auth_shm = make_sqlite_with_leftover_wal(live_auth)

    # The archive is "already extracted" into the staging directory restore_from_spaces uses.
    staging = tmp_path / "backup-20260101-000000"
    make_duckdb(staging / "data" / "warehouse.duckdb", "archive")
    make_sqlite(staging / ".dango" / "auth.db", "archive")

    real_path = Path

    def fake_path(value: Any = ".") -> Path:
        text = str(value)
        if text.startswith("/tmp/"):
            return tmp_path / text[len("/tmp/") :]
        return real_path(value)

    def run_local(command: str, **_kw: Any) -> str:
        if "tar -xzf" in command or command.startswith("chown"):
            return ""
        proc = _REAL_RUN(command, shell=True, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", tmp_path / "backups"),
        patch(f"{_SB}.download_from_spaces"),
        patch(f"{_SB}.Path", fake_path),
        patch(f"{_SB}._stop_services"),
        patch(f"{_SB}._start_services"),
        patch(f"{_SB}._run_local", side_effect=run_local),
        patch(f"{_SB}._get_metabase_volume_path", return_value=None),
        patch(f"{_SB}.subprocess.run"),
    ):
        sb.restore_from_spaces({}, "backups/backup-20260101-000000.tar.gz")

    assert not duck_wal.exists(), "live DuckDB WAL left in place after Spaces restore"
    # No safety backup on this path: the live WAL is kept aside, not deleted.
    assert len(list(duck_wal.parent.glob("warehouse.duckdb.wal.pre-restore-*"))) == 1
    assert not auth_wal.exists() and not auth_shm.exists()
    tables, values = duckdb_state(live_db)
    assert tables == {"t"}
    assert values == ["archive"]
    assert sqlite_values(live_auth) == ["archive"]


@pytest.mark.unit
def test_restore_from_spaces_fixes_ownership_after_failed_copy(tmp_path: Path) -> None:
    from dango.exceptions import CloudProvisioningError
    from dango.platform.cloud import scheduled_backup as sb

    project = tmp_path / "project"
    project.mkdir()
    staging = tmp_path / "backup-20260101-000000"
    make_duckdb(staging / "data" / "warehouse.duckdb", "archive")
    (staging / ".dango").mkdir()
    (staging / ".dango" / "project.yml").write_text("project: {}\n")
    events: list[str] = []
    real_path = Path

    def fake_path(value: Any = ".") -> Path:
        text = str(value)
        if text.startswith("/tmp/"):
            return tmp_path / text[len("/tmp/") :]
        return real_path(value)

    def run_local(command: str, **_kw: Any) -> str:
        if "project.yml" in command:
            raise CloudProvisioningError("Step 'restore_files' failed (exit 1): disk full")
        if "tar -xzf" in command or command.startswith("chown"):
            return ""
        proc = _REAL_RUN(command, shell=True, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", tmp_path / "backups"),
        patch(f"{_SB}.download_from_spaces"),
        patch(f"{_SB}.Path", fake_path),
        patch(f"{_SB}._stop_services"),
        patch(f"{_SB}._start_services", side_effect=lambda: events.append("start")),
        patch(f"{_SB}._run_local", side_effect=run_local),
        patch(f"{_SB}._get_metabase_volume_path", return_value=None),
        patch(f"{_SB}.subprocess.run", side_effect=lambda cmd, **_k: events.append(str(cmd))),
        pytest.raises(CloudProvisioningError, match="restore_files"),
    ):
        sb.restore_from_spaces({}, "backups/backup-20260101-000000.tar.gz")

    chown_at = [i for i, e in enumerate(events) if e.startswith("chown -R dango:dango")]
    assert chown_at, "partial restore left root-owned before services restart"
    assert events.index("start") > chown_at[0]
