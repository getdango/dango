"""tests/unit/test_local_restore_safety.py

Local restore must not discard committed WAL-only data that the pre-restore safety archive lacks.
"""

from __future__ import annotations

import shutil
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

_LB = "dango.cli.commands.local_backup"


def _restore(project: Path, archive: Path) -> Any:
    from dango.cli.commands.local_backup import backup_group

    with patch(f"{_LB}.require_project_context", return_value=project):
        return CliRunner().invoke(
            backup_group, ["restore", str(archive), "--yes"], obj={"project_root": project}
        )


def _archive(tmp_path: Path) -> Path:
    content = tmp_path / "content"
    make_duckdb(content / "data" / "warehouse.duckdb", "archive")
    make_sqlite(content / ".dango" / "auth.db", "archive")
    archive = tmp_path / "backup-20260101-000000.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname="backup-20260101-000000")
    return archive


def _live_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    make_duckdb_with_leftover_wal(project / "data" / "warehouse.duckdb")
    make_sqlite_with_leftover_wal(project / ".dango" / "auth.db")
    return project


def _assert_live_rows_back(project: Path) -> None:
    tables, values = duckdb_state(project / "data" / "warehouse.duckdb")
    assert "live_only" in tables, f"WAL-only table lost: {tables}"
    assert values == ["live"]
    assert sqlite_values(project / ".dango" / "auth.db") == ["live", "old"]


@pytest.mark.unit
def test_safety_archive_preserves_wal_only_rows(tmp_path: Path) -> None:
    project = _live_project(tmp_path)

    first = _restore(project, _archive(tmp_path))
    assert first.exit_code == 0, first.output
    safety = sorted((project / ".dango" / "backups").glob("pre-restore-*.tar.gz"))
    assert len(safety) == 1

    # Restore from a copy: a second restore in the same second reuses the safety file name.
    kept = tmp_path / safety[0].name
    shutil.copy(safety[0], kept)
    back = _restore(project, kept)
    assert back.exit_code == 0, back.output
    _assert_live_rows_back(project)


@pytest.mark.unit
def test_safety_archive_keeps_sidecars_when_checkpoint_fails(tmp_path: Path) -> None:
    project = _live_project(tmp_path)

    with patch(f"{_LB}._checkpoint_database", return_value=False):
        first = _restore(project, _archive(tmp_path))
    assert first.exit_code == 0, first.output
    assert "checkpoint" in first.output.lower()
    safety = sorted((project / ".dango" / "backups").glob("pre-restore-*.tar.gz"))
    assert len(safety) == 1
    with tarfile.open(safety[0]) as tf:
        names = set(tf.getnames())
    assert {"data/warehouse.duckdb.wal", ".dango/auth.db-wal"} <= names

    # Restore from a copy: a second restore in the same second reuses the safety file name.
    kept = tmp_path / safety[0].name
    shutil.copy(safety[0], kept)
    back = _restore(project, kept)
    assert back.exit_code == 0, back.output
    _assert_live_rows_back(project)


@pytest.mark.unit
def test_wal_kept_aside_when_safety_backup_failed(tmp_path: Path) -> None:
    project = _live_project(tmp_path)
    wal = project / "data" / "warehouse.duckdb.wal"
    wal_bytes = wal.read_bytes()

    with patch(f"{_LB}._create_safety_backup", return_value=None):
        result = _restore(project, _archive(tmp_path))

    assert result.exit_code == 0, result.output
    assert not wal.exists()
    kept = list((project / "data").glob("warehouse.duckdb.wal.pre-restore-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == wal_bytes
    assert len(list((project / ".dango").glob("auth.db-wal.pre-restore-*"))) == 1
    tables, values = duckdb_state(project / "data" / "warehouse.duckdb")
    assert tables == {"t"} and values == ["archive"]
