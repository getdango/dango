"""tests/unit/test_seed.py

Tests for dango.cli.commands.seed — dbt seed management commands.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


@pytest.mark.unit
class TestSeedAdd:
    def test_copies_csv_and_reports_ref(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "source.csv"
            src.write_text("id,name\n1,Alice\n")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code == 0
            assert (project_root / "dbt" / "seeds" / "source.csv").exists()
            assert "ref('source')" in result.output

    def test_rejects_non_csv(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "data.json"
            src.write_text('{"a": 1}')

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code != 0
            assert not (project_root / "dbt" / "seeds" / "data.json").exists()

    def test_rejects_invalid_identifier(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "bad name.csv"
            src.write_text("id\n1\n")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code != 0
            assert not (project_root / "dbt" / "seeds" / "bad name.csv").exists()

    def test_sets_seeds_schema_in_dbt_project(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "ref_data.csv"
            src.write_text("id,name\n1,Alice\n")
            # Create a minimal dbt_project.yml that seed_add will update
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            dbt_project = dbt_dir / "dbt_project.yml"
            dbt_project.write_text(
                "name: myproject\nseeds:\n  myproject:\n    +quote_columns: false\n"
            )

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code == 0
            import yaml

            data = yaml.safe_load(dbt_project.read_text())
            assert data["seeds"]["myproject"]["+schema"] == "seeds"

    def test_warns_when_dbt_project_missing(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "ref_data.csv"
            src.write_text("id,name\n1,Alice\n")
            # Create dbt directory but no dbt_project.yml
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code == 0
            assert (project_root / "dbt" / "seeds" / "ref_data.csv").exists()
            assert "dbt_project.yml not found" in result.output

    def test_warns_when_project_name_missing(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "ref_data.csv"
            src.write_text("id,name\n1,Alice\n")
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            dbt_project = dbt_dir / "dbt_project.yml"
            # Create dbt_project.yml without 'name' key
            dbt_project.write_text("seeds:\n  myproject:\n    +quote_columns: false\n")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code == 0
            assert (project_root / "dbt" / "seeds" / "ref_data.csv").exists()
            assert "has no" in result.output and "name" in result.output

    def test_warns_when_seeds_config_malformed(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "ref_data.csv"
            src.write_text("id,name\n1,Alice\n")
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            dbt_project = dbt_dir / "dbt_project.yml"
            # Create dbt_project.yml with 'seeds' as a string (malformed)
            dbt_project.write_text('name: myproject\nseeds: "invalid string"\n')

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code == 0
            assert (project_root / "dbt" / "seeds" / "ref_data.csv").exists()
            assert "malformed" in result.output

    def test_warns_when_yaml_invalid(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "ref_data.csv"
            src.write_text("id,name\n1,Alice\n")
            dbt_dir = project_root / "dbt"
            dbt_dir.mkdir()
            dbt_project = dbt_dir / "dbt_project.yml"
            # Create dbt_project.yml with invalid YAML syntax
            dbt_project.write_text("name: myproject\n  invalid: [unclosed\n")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src)])

            assert result.exit_code == 0
            assert (project_root / "dbt" / "seeds" / "ref_data.csv").exists()
            assert "Failed to parse" in result.output or "schema config skipped" in result.output


@pytest.mark.unit
class TestSeedList:
    def test_lists_seeds(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            seeds_dir = project_root / "dbt" / "seeds"
            seeds_dir.mkdir(parents=True)
            (seeds_dir / "alpha.csv").write_text("id\n1\n")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["list"])

            assert result.exit_code == 0
            assert "alpha.csv" in result.output
            assert "ref('alpha')" in result.output

    def test_no_seeds(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["list"])

            assert result.exit_code == 0
            assert "No dbt/seeds" in result.output
