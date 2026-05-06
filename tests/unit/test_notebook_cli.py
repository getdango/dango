"""tests/unit/test_notebook_cli.py

Tests for dango.cli.commands.notebook — notebook CLI commands.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


@pytest.mark.unit
class TestNotebookList:
    def test_no_notebooks(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / ".dango").mkdir()

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.notebook import notebook

                result = runner.invoke(notebook, [])

            assert "No notebooks found" in result.output

    def test_lists_notebooks(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            nb_dir = project_root / "notebooks"
            nb_dir.mkdir()
            (nb_dir / "explore.py").write_text("# notebook")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.notebook import notebook

                result = runner.invoke(notebook, [])

            assert "explore" in result.output

    def test_shows_author_column(self):
        """Create notebook file + metadata entry and verify Author column appears."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            nb_dir = project_root / "notebooks"
            nb_dir.mkdir()
            (nb_dir / "my_nb.py").write_text("# notebook")
            (project_root / ".dango").mkdir()

            from dango.utils.dango_db import _schema_initialized, connect

            _schema_initialized.clear()
            with connect(project_root) as conn:
                conn.execute(
                    "INSERT INTO notebook_metadata (id, name, description, created_by, created_at, updated_at) "
                    "VALUES ('id1', 'my_nb', 'desc', 'alice@test.com', '2026-01-01', '2026-01-01')"
                )
                conn.commit()

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.notebook import notebook

                result = runner.invoke(notebook, [])

            assert "Author" in result.output
            assert "alice@test.com" in result.output

    def test_shows_dash_for_unknown_author(self):
        """Notebook on disk with no metadata shows '--' for author."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            nb_dir = project_root / "notebooks"
            nb_dir.mkdir()
            (nb_dir / "orphan.py").write_text("# notebook")
            (project_root / ".dango").mkdir()

            from dango.utils.dango_db import _schema_initialized

            _schema_initialized.clear()

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.notebook import notebook

                result = runner.invoke(notebook, [])

            assert "Author" in result.output
            assert "--" in result.output

    def test_works_without_metadata_db(self):
        """CLI falls back gracefully when dango.db doesn't exist."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            nb_dir = project_root / "notebooks"
            nb_dir.mkdir()
            (nb_dir / "solo.py").write_text("# notebook")

            # Patch connect at source to raise (lazy import inside function)
            with (
                patch("dango.cli.utils.find_project_root", return_value=project_root),
                patch(
                    "dango.utils.dango_db.connect",
                    side_effect=Exception("no db"),
                ),
            ):
                from dango.cli.commands.notebook import notebook

                result = runner.invoke(notebook, [])

            assert "solo" in result.output
            assert "--" in result.output


@pytest.mark.unit
class TestNotebookNew:
    def test_creates_notebook(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / ".dango").mkdir()

            with (
                patch("dango.cli.utils.find_project_root", return_value=project_root),
                patch("dango.utils.dango_db.get_connection") as mock_get_conn,
                patch("dango.auth.audit.log_auth_event"),
            ):
                mock_conn = MagicMock()
                mock_conn.execute.return_value = None
                mock_get_conn.return_value = mock_conn

                from dango.cli.commands.notebook import notebook

                result = runner.invoke(
                    notebook, ["new", "--name", "test_nb", "--template", "blank"]
                )

            assert result.exit_code == 0
            assert "Created notebook" in result.output

    def test_rejects_duplicate_name(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            nb_dir = project_root / "notebooks"
            nb_dir.mkdir()
            (nb_dir / "existing.py").write_text("# existing")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.notebook import notebook

                result = runner.invoke(
                    notebook, ["new", "--name", "existing", "--template", "blank"]
                )

            assert result.exit_code != 0


@pytest.mark.unit
class TestNotebookOpen:
    def test_open_starts_marimo(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            nb_dir = project_root / "notebooks"
            nb_dir.mkdir()
            (nb_dir / "test.py").write_text("# notebook")

            with (
                patch("dango.cli.utils.find_project_root", return_value=project_root),
                patch("dango.notebooks.manager.get_marimo_status") as mock_status,
                patch("dango.notebooks.manager.start_marimo", return_value=12345),
            ):
                mock_status.side_effect = [
                    {"running": False},
                    {"running": True, "port": 7805},
                ]

                from dango.cli.commands.notebook import notebook

                result = runner.invoke(notebook, ["open", "test"])

            assert "localhost:7805" in result.output
            # Rich inserts ANSI escape codes in URLs; strip before checking
            import re

            plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
            assert "?file=test.py" in plain

    def test_open_nonexistent_notebook(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / "notebooks").mkdir()

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.notebook import notebook

                result = runner.invoke(notebook, ["open", "nonexistent"])

            assert result.exit_code != 0


@pytest.mark.unit
class TestSnapshotCommand:
    @patch("dango.notebooks.snapshot.create_snapshot")
    def test_snapshot_success(self, mock_create):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            snap_path = (
                project_root / ".dango" / "snapshots" / "warehouse_default_20260101_120000.duckdb"
            )
            snap_path.parent.mkdir(parents=True)
            snap_path.write_bytes(b"x" * 1024)
            mock_create.return_value = snap_path

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.snapshot import snapshot

                result = runner.invoke(snapshot, ["db"])

            assert result.exit_code == 0
            assert "Snapshot created" in result.output
