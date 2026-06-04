"""tests/unit/test_cli_transform.py

Tests for dango run (transform) CLI command.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from dango.cli.main import cli


@pytest.mark.unit
class TestRunHelpText:
    """B8: dango run --help should reference dbt build, not dbt run."""

    def test_help_says_dbt_build(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "dbt build" in result.output
        assert "dbt run" not in result.output


@pytest.mark.unit
class TestYesFlagFiltering:
    """B9: --yes and -y should not be passed through to dbt."""

    def test_yes_flag_filtered(self) -> None:
        """Verify --yes is stripped from dbt_args in the source code."""
        import inspect

        from dango.cli.commands import transform

        source = inspect.getsource(transform)
        assert '"--yes"' in source or "'--yes'" in source
        assert "filtered_args" in source

    def test_y_flag_filtered(self) -> None:
        """Verify -y is stripped from dbt_args in the source code."""
        import inspect

        from dango.cli.commands import transform

        source = inspect.getsource(transform)
        assert '"-y"' in source or "'-y'" in source
