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

    def test_scaffolds_model_stub(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as td:
            project_root = Path(td)
            src = project_root / "source.csv"
            src.write_text("id\n1\n")

            with patch("dango.cli.utils.find_project_root", return_value=project_root):
                from dango.cli.commands.seed import seed

                result = runner.invoke(seed, ["add", str(src), "--model", "int_test"])

            assert result.exit_code == 0
            model = project_root / "dbt" / "models" / "staging" / "int_test.sql"
            assert model.exists()
            assert "{{ ref('source') }}" in model.read_text()


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
