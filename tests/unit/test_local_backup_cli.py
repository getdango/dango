"""tests/unit/test_local_backup_cli.py

Unit tests for dango/cli/commands/local_backup.py.

Uses Click's CliRunner with mocked project context and tmp_path
to avoid real filesystem side effects.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_REQUIRE_CTX = "dango.cli.commands.local_backup.require_project_context"


def _run(args: list[str], tmp_path: Path, *, input_str: str | None = None):
    """Invoke the local ``backup`` CLI group with the given args."""
    from dango.cli.commands.local_backup import backup_group

    runner = CliRunner()
    return runner.invoke(
        backup_group,
        args,
        obj={"project_root": tmp_path},
        input=input_str,
    )


def _make_archive(archive_path: Path, files: dict[str, bytes], *, metabase: bool = False):
    """Create a .tar.gz archive with the given files."""
    with tarfile.open(archive_path, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        if metabase:
            mv_info = tarfile.TarInfo(name="metabase/metabase.db.mv.db")
            mv_info.size = 1024
            tf.addfile(mv_info, io.BytesIO(b"m" * 1024))
            trace_info = tarfile.TarInfo(name="metabase/metabase.db.trace.db")
            trace_info.size = 512
            tf.addfile(trace_info, io.BytesIO(b"t" * 512))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLocalBackupRestore:
    def test_restore_success(self, tmp_path):
        """Restoring a valid .tar.gz extracts files to project root."""
        archive_path = tmp_path / "backup-test.tar.gz"
        _make_archive(
            archive_path,
            {
                "/srv/dango/project/data/warehouse.duckdb": b"duckdb-data",
                "/srv/dango/project/.dango/project.yml": b"project-yml",
            },
        )

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            result = _run(["restore", str(archive_path), "--yes"], tmp_path)

        assert result.exit_code == 0
        assert "Restore complete" in result.output
        # Verify files were extracted
        assert (tmp_path / "data" / "warehouse.duckdb").exists()
        assert (tmp_path / ".dango" / "project.yml").exists()

    def test_not_tar_gz_rejected(self, tmp_path):
        """Non-.tar.gz file is rejected."""
        bad_file = tmp_path / "backup.zip"
        bad_file.write_text("not-an-archive")

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            result = _run(["restore", str(bad_file), "--yes"], tmp_path)

        assert result.exit_code != 0
        assert ".tar.gz" in result.output

    def test_prompts_confirmation(self, tmp_path):
        """Without --yes, user can cancel with 'no'."""
        archive_path = tmp_path / "backup-test.tar.gz"
        _make_archive(archive_path, {"/srv/dango/project/data/warehouse.duckdb": b"data"})

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            result = _run(["restore", str(archive_path)], tmp_path, input_str="n\n")

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()

    def test_safety_backup_created_before_restore(self, tmp_path):
        """A pre-restore safety backup is created before extracting."""
        # Create project files first so they can be backed up
        (tmp_path / ".dango" / "logs").mkdir(parents=True)
        (tmp_path / "dbt").mkdir(parents=True)
        (tmp_path / ".dlt" / "pipelines").mkdir(parents=True)
        (tmp_path / "data").mkdir(parents=True)

        for fpath in [".dango/project.yml", "data/warehouse.duckdb", ".dango/auth.db"]:
            full = tmp_path / fpath
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("test-data")

        archive_path = tmp_path / "backup-test.tar.gz"
        _make_archive(
            archive_path,
            {"/srv/dango/project/data/warehouse.duckdb": b"restored-data"},
        )

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            result = _run(["restore", str(archive_path), "--yes"], tmp_path)

        assert result.exit_code == 0
        # Check that safety backup was created
        safety_backups = list((tmp_path / ".dango" / "backups").glob("pre-restore-*.tar.gz"))
        assert len(safety_backups) == 1

    def test_metabase_h2_skipped(self, tmp_path):
        """Metabase H2 files in archive are NOT extracted to project root."""
        archive_path = tmp_path / "backup-test.tar.gz"
        _make_archive(
            archive_path,
            {"/srv/dango/project/data/warehouse.duckdb": b"data"},
            metabase=True,
        )

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            result = _run(["restore", str(archive_path), "--yes"], tmp_path)

        assert result.exit_code == 0
        # Metabase files should NOT exist in project root
        assert not (tmp_path / "metabase").exists()
        assert not (tmp_path / "metabase" / "metabase.db.mv.db").exists()
