"""tests/unit/test_cli_start_guardrails.py

Unit tests for `dango start` guard rails (cloned project info, cloud warning).
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.platform import start

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _make_cloud_config(droplet_ip: str = "1.2.3.4", domain: str | None = None) -> MagicMock:
    """Create a mock CloudConfig."""
    cfg = MagicMock()
    cfg.droplet_ip = droplet_ip
    cfg.domain = domain
    return cfg


def _make_config_loader(cloud_config: MagicMock | None = None) -> MagicMock:
    """Create a mock ConfigLoader that returns the given cloud config."""
    mock_loader = MagicMock()
    mock_loader.return_value.load_config.return_value = MagicMock()
    mock_loader.return_value.load_config.return_value.project.name = "test-project"
    mock_loader.return_value.load_config.return_value.project.organization = None
    mock_loader.return_value.load_config.return_value.platform.port = 8800
    mock_loader.return_value.load_config.return_value.platform.metabase_port = 3000
    mock_loader.return_value.load_config.return_value.platform.dbt_docs_port = 8081
    mock_loader.return_value.load_config.return_value.platform.auto_sync = False
    mock_loader.return_value.load_cloud_config.return_value = cloud_config
    return mock_loader


@pytest.mark.unit
class TestStartGuardRails:
    """Tests for start command guard rails."""

    def test_cloned_project_message_shown(self, tmp_path: Path) -> None:
        """Cloned project message shown when sources.yml exists but no dango.db/warehouse."""
        # Set up cloned project state
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text("sources: []")
        # No dango.db, no data/warehouse.duckdb

        mock_loader = _make_config_loader()
        runner = CliRunner()
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
        ):
            # Abort after cloud check by injecting an error in version check
            mock_loader.return_value.load_cloud_config.return_value = None
            # Let it fail on the next step (version alignment already patched)
            # We just need to see the cloned project message in output
            result = runner.invoke(start, ["--yes"], obj={"project_root": str(tmp_path)})

        plain = _ANSI_RE.sub("", result.output)
        assert "cloned project" in plain.lower()
        assert "dango sync" in plain

    def test_cloned_project_message_not_shown_when_warehouse_exists(self, tmp_path: Path) -> None:
        """Cloned project message NOT shown when warehouse.duckdb exists."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text("sources: []")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "warehouse.duckdb").touch()

        mock_loader = _make_config_loader()
        runner = CliRunner()
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
        ):
            mock_loader.return_value.load_cloud_config.return_value = None
            result = runner.invoke(start, ["--yes"], obj={"project_root": str(tmp_path)})

        plain = _ANSI_RE.sub("", result.output)
        assert "cloned project" not in plain.lower()

    def test_cloud_info_shown_as_note(self, tmp_path: Path) -> None:
        """Cloud deployment shown as informational note (no blocking prompt)."""
        cloud_cfg = _make_cloud_config(droplet_ip="1.2.3.4")
        mock_loader = _make_config_loader(cloud_config=cloud_cfg)
        runner = CliRunner()
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", mock_loader),
            patch("dango.platform.common.startup.check_duckdb_version_alignment"),
        ):
            result = runner.invoke(start, [], obj={"project_root": str(tmp_path)})

        plain = _ANSI_RE.sub("", result.output)
        assert "deployed to 1.2.3.4" in plain
        assert "independent" in plain.lower()
