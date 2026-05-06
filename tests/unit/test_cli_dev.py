"""tests/unit/test_cli_dev.py

Tests for dango.cli.commands.dev — branch-based dbt development.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


@pytest.mark.unit
class TestDevDefaultFlow:
    """Tests for the default ``dango dev`` run."""

    def test_dev_creates_dev_database(self):
        """Copy of warehouse.duckdb is placed in .dango/dev/."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / ".dango").mkdir()
            (project_root / "data").mkdir()
            (project_root / "data" / "warehouse.duckdb").write_bytes(b"FAKE_DB")
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            (dbt_dir / "dbt_project.yml").write_text("name: 'test_proj'\nprofile: 'test_proj'\n")

            with (
                patch("dango.cli.utils.find_project_root", return_value=project_root),
                patch("dango.cli.commands.dev._run_dev_dbt", return_value=0) as mock_dbt,
                patch("dango.cli.commands.dev._parse_run_results", return_value=[]),
            ):
                from dango.cli.commands.dev import dev

                result = runner.invoke(dev, [])

            assert result.exit_code == 0
            dev_db = project_root / ".dango" / "dev" / "warehouse_dev.duckdb"
            assert dev_db.exists()
            assert dev_db.read_bytes() == b"FAKE_DB"
            mock_dbt.assert_called_once()

    def test_dev_select_passes_to_dbt(self):
        """--select flag is forwarded to _run_dev_dbt."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / ".dango").mkdir()
            (project_root / "data").mkdir()
            (project_root / "data" / "warehouse.duckdb").write_bytes(b"DB")
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            (dbt_dir / "dbt_project.yml").write_text("name: 'test_proj'\nprofile: 'test_proj'\n")

            with (
                patch("dango.cli.utils.find_project_root", return_value=project_root),
                patch("dango.cli.commands.dev._run_dev_dbt", return_value=0) as mock_dbt,
                patch("dango.cli.commands.dev._parse_run_results", return_value=[]),
            ):
                from dango.cli.commands.dev import dev

                result = runner.invoke(dev, ["--select", "stg_orders"])

            assert result.exit_code == 0
            # Second positional arg to _run_dev_dbt is dev_dir, third is select
            call_args = mock_dbt.call_args
            assert call_args[0][2] == "stg_orders"

    def test_dev_diff_shows_comparison(self):
        """--diff calls _show_row_count_diff."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / ".dango").mkdir()
            (project_root / "data").mkdir()
            (project_root / "data" / "warehouse.duckdb").write_bytes(b"DB")
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            (dbt_dir / "dbt_project.yml").write_text("name: 'test_proj'\nprofile: 'test_proj'\n")

            with (
                patch("dango.cli.utils.find_project_root", return_value=project_root),
                patch("dango.cli.commands.dev._run_dev_dbt", return_value=0),
                patch("dango.cli.commands.dev._parse_run_results", return_value=[]),
                patch("dango.cli.commands.dev._show_row_count_diff") as mock_diff,
            ):
                from dango.cli.commands.dev import dev

                result = runner.invoke(dev, ["--diff"])

            assert result.exit_code == 0
            mock_diff.assert_called_once()


@pytest.mark.unit
class TestDevNoProject:
    """Tests for error states."""

    def test_dev_no_project_root_errors(self):
        """Running outside a project aborts."""
        runner = CliRunner()
        from dango.config import ProjectNotFoundError

        with patch(
            "dango.cli.utils.find_project_root",
            side_effect=ProjectNotFoundError("not found"),
        ):
            from dango.cli.commands.dev import dev

            result = runner.invoke(dev, [])

        assert result.exit_code != 0

    def test_dev_no_warehouse_errors(self):
        """Missing warehouse.duckdb gives a clear error."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / ".dango").mkdir()
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            (dbt_dir / "dbt_project.yml").write_text("name: 'test_proj'\nprofile: 'test_proj'\n")
            # No data/warehouse.duckdb

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.dev import dev

                result = runner.invoke(dev, [])

        assert result.exit_code != 0
        assert "Production database not found" in result.output


@pytest.mark.unit
class TestDevClean:
    """Tests for ``dango dev clean``."""

    def test_dev_clean_removes_artifacts(self):
        """clean subcommand removes .dango/dev/."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            dev_dir = project_root / ".dango" / "dev"
            dev_dir.mkdir(parents=True)
            (dev_dir / "warehouse_dev.duckdb").write_bytes(b"DEV_DB")
            (dev_dir / "profiles.yml").write_text("profile: test")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.dev import dev

                result = runner.invoke(dev, ["clean"])

            assert result.exit_code == 0
            assert "Dev artifacts removed" in result.output
            assert not dev_dir.exists()

    def test_dev_clean_nothing_to_clean(self):
        """clean with no dev dir shows informational message."""
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            (project_root / ".dango").mkdir()

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.dev import dev

                result = runner.invoke(dev, ["clean"])

            assert result.exit_code == 0
            assert "Nothing to clean" in result.output
