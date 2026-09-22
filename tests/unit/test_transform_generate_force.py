"""tests/unit/test_transform_generate_force.py

Tests that `dango generate` respects the auto-generated marker comment by
default and only bypasses it with --force (1.0.8-N).
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


@pytest.mark.unit
class TestGenerateForceFlag:
    def _invoke(self, extra_args: list[str]):
        from dango.cli.commands.transform import generate

        mock_generator_instance = MagicMock()
        mock_generator_instance.generate_all_models.return_value = {
            "generated": [],
            "skipped": [],
            "errors": [],
        }
        mock_config = MagicMock()
        mock_config.sources.get_enabled_sources.return_value = [MagicMock()]

        with (
            patch("dango.cli.utils.require_project_context", return_value="/fake/project"),
            patch("dango.config.get_config", return_value=mock_config),
            patch(
                "dango.transformation.generator.DbtModelGenerator",
                return_value=mock_generator_instance,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(generate, extra_args)

        return result, mock_generator_instance

    def test_default_respects_marker(self) -> None:
        result, mock_gen = self._invoke([])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_gen.generate_all_models.call_args
        assert kwargs["skip_customized"] is True

    def test_force_bypasses_marker(self) -> None:
        result, mock_gen = self._invoke(["--force"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_gen.generate_all_models.call_args
        assert kwargs["skip_customized"] is False
