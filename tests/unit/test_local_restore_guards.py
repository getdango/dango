"""tests/unit/test_local_restore_guards.py

Local restore streams atomically, refuses while Dango runs, accepts only backup members, and keeps distinct safety archives.
"""

from __future__ import annotations

import io
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
)

_LB = "dango.cli.commands.local_backup"
_TOP = "backup-20260101-000000"


def _restore(project: Path, archive: Path) -> Any:
    from dango.cli.commands.local_backup import backup_group

    with patch(f"{_LB}.require_project_context", return_value=project):
        return CliRunner().invoke(
            backup_group, ["restore", str(archive), "--yes"], obj={"project_root": project}
        )


def _archive(tmp_path: Path, extra: dict[str, bytes] | None = None) -> Path:
    content = tmp_path / "content"
    make_duckdb(content / "data" / "warehouse.duckdb", "archive")
    make_sqlite(content / ".dango" / "auth.db", "archive")
    (content / "manifest.json").write_text("{}")
    archive = tmp_path / f"{_TOP}.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=_TOP)
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(f"{_TOP}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    make_duckdb(project / "data" / "warehouse.duckdb", "live")
    (project / ".dango").mkdir(exist_ok=True)
    return project


# ---------------------------------------------------------------------------
# (a) streaming, atomic replace
# ---------------------------------------------------------------------------


class _FailingStream(io.RawIOBase):
    """Archive member reader that fails after the first chunk, like a crash mid-copy."""

    def __init__(self, inner: Any, record: list[int | None]) -> None:
        self.inner, self.record, self.served = inner, record, 0

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        self.record.append(size)
        if size is None or size < 0:
            return bytes(self.inner.read())
        if self.served:
            raise OSError("simulated crash mid-copy")
        self.served += 1
        return bytes(self.inner.read(size))

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


@pytest.mark.unit
def test_local_restore_streams_and_is_atomic(tmp_path: Path) -> None:
    project = _project(tmp_path)
    live = project / "data" / "warehouse.duckdb"
    before = live.read_bytes()
    archive = _archive(tmp_path)
    reads: list[int | None] = []
    real_extract = tarfile.TarFile.extractfile
    real_write_bytes = Path.write_bytes

    def extractfile(self: tarfile.TarFile, member: Any) -> Any:
        handle = real_extract(self, member)
        name = member if isinstance(member, str) else member.name
        if handle is not None and name.endswith("data/warehouse.duckdb"):
            return _FailingStream(handle, reads)
        return handle

    def write_bytes(self: Path, data: Any) -> int:
        if self.name == "warehouse.duckdb":
            real_write_bytes(self, bytes(data)[: len(data) // 2])
            raise OSError("simulated crash mid-write")
        return real_write_bytes(self, data)

    with (
        patch.object(tarfile.TarFile, "extractfile", extractfile),
        patch.object(Path, "write_bytes", write_bytes),
    ):
        result = _restore(project, archive)

    assert result.exit_code != 0
    assert live.read_bytes() == before, "interrupted restore changed the live warehouse"
    assert not list(project.rglob("*.restore-tmp")), "temporary restore file left behind"
    assert reads and all(r is not None and r > 0 for r in reads), (
        f"member read whole into memory: {reads}"
    )


# ---------------------------------------------------------------------------
# (b) refuse while Dango is running or holds the lock
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_restore_refuses_when_server_running(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / "data" / "warehouse.duckdb").read_bytes()
    with patch("dango.cli.helpers.process_manager.is_project_server_running", return_value=True):
        result = _restore(project, _archive(tmp_path))
    assert result.exit_code != 0
    assert "dango stop" in result.output
    assert (project / "data" / "warehouse.duckdb").read_bytes() == before
    assert not (project / ".dango" / "backups").exists(), "safety backup made before refusing"


@pytest.mark.unit
def test_local_restore_refuses_while_dbt_lock_is_held(tmp_path: Path) -> None:
    from dango.utils.dbt_lock import DbtLock

    project = _project(tmp_path)
    before = (project / "data" / "warehouse.duckdb").read_bytes()
    holder = DbtLock(project, source="test", operation="sync")
    holder.acquire(timeout=0)
    try:
        result = _restore(project, _archive(tmp_path))
    finally:
        holder.release()
    assert result.exit_code != 0
    assert "dango stop" in result.output
    assert (project / "data" / "warehouse.duckdb").read_bytes() == before


# ---------------------------------------------------------------------------
# (d) only backup members are written
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_restore_rejects_unexpected_member(tmp_path: Path) -> None:
    project = _project(tmp_path)
    archive = _archive(
        tmp_path,
        {
            "scripts/ok.py": b"print('ok')\n",
            ".env": b"API_KEY=planted\n",
            ".dlt/secrets.toml": b"token = 'planted'\n",
            "dango_hooks/run.py": b"import os\n",
            ".dango/plugins/x.py": b"import os\n",
        },
    )
    result = _restore(project, archive)
    assert result.exit_code == 0, result.output
    assert (project / "scripts" / "ok.py").read_text() == "print('ok')\n"
    for rejected in (".env", ".dlt/secrets.toml", "dango_hooks/run.py", ".dango/plugins/x.py"):
        assert not (project / rejected).exists(), f"{rejected} was written"
        assert rejected in result.output, f"skipped member {rejected} not reported"
    _tables, values = duckdb_state(project / "data" / "warehouse.duckdb")
    assert values == ["archive"]


# ---------------------------------------------------------------------------
# (c)/(e) safety backup: checkpointed, covers the new directories, unique names (K-14)
# ---------------------------------------------------------------------------


def _safety_members(project: Path) -> set[str]:
    (safety,) = sorted((project / ".dango" / "backups").glob("pre-restore-*.tar.gz"))
    with tarfile.open(safety) as tf:
        return {m.name for m in tf.getmembers()}


@pytest.mark.unit
def test_safety_backup_includes_new_directories(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "notebooks").mkdir()
    (project / "notebooks" / "n.py").write_text("import marimo\n")
    (project / "data" / "uploads").mkdir()
    (project / "data" / "uploads" / "x.csv").write_text("a\n1\n")
    (project / ".dango" / "schedules.yml").write_text("schedules: []\n")
    result = _restore(project, _archive(tmp_path))
    assert result.exit_code == 0, result.output
    members = _safety_members(project)
    for rel in ("notebooks/n.py", "data/uploads/x.csv", ".dango/schedules.yml"):
        assert rel in members, f"safety backup lacks {rel}: {sorted(members)}"


@pytest.mark.unit
def test_safety_backup_includes_wal_data(tmp_path: Path) -> None:
    project = tmp_path / "project"
    make_duckdb_with_leftover_wal(project / "data" / "warehouse.duckdb")
    result = _restore(project, _archive(tmp_path))
    assert result.exit_code == 0, result.output
    (safety,) = sorted((project / ".dango" / "backups").glob("pre-restore-*.tar.gz"))
    out = tmp_path / "safety-out"
    with tarfile.open(safety) as tf:
        for m in tf.getmembers():
            if m.name.startswith("data/warehouse.duckdb"):
                handle = tf.extractfile(m)
                assert handle is not None
                dest = out / m.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(handle.read())
    tables, _values = duckdb_state(out / "data" / "warehouse.duckdb")
    assert "live_only" in tables, f"WAL-only table missing from safety backup: {tables}"


@pytest.mark.unit
def test_two_safety_backups_in_the_same_second_are_distinct(tmp_path: Path) -> None:
    from dango.cli.commands.local_backup import _create_safety_backup

    project = _project(tmp_path)
    with patch(f"{_LB}.time.strftime", return_value="20260101-000000"):
        first = _create_safety_backup(project)
        second = _create_safety_backup(project)
    assert first is not None and second is not None
    assert first != second
    assert first.exists() and second.exists()
