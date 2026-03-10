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
                from dango.cli.commands.notebook import snapshot

                result = runner.invoke(snapshot, [])

            assert result.exit_code == 0
            assert "Snapshot created" in result.output
