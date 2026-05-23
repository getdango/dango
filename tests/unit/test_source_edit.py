"""tests/unit/test_source_edit.py

Tests for dango source edit command and SourceStatus sync mode fields.
"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.source import source_edit
from dango.web.models import SourceStatus


class TestSourceStatusSyncFields:
    """Test SourceStatus model accepts sync mode fields."""

    def test_sync_mode_defaults_none(self) -> None:
        status = SourceStatus(name="test", type="csv", enabled=True)
        assert status.sync_mode is None
        assert status.lookback_days is None
        assert status.write_disposition is None

    def test_sync_mode_incremental(self) -> None:
        status = SourceStatus(
            name="test",
            type="google_analytics",
            enabled=True,
            sync_mode="incremental",
            lookback_days=2,
            write_disposition="merge",
        )
        assert status.sync_mode == "incremental"
        assert status.lookback_days == 2
        assert status.write_disposition == "merge"

    def test_sync_mode_full_refresh(self) -> None:
        status = SourceStatus(
            name="test",
            type="csv",
            enabled=True,
            sync_mode="full_refresh",
            write_disposition="replace",
        )
        assert status.sync_mode == "full_refresh"
        assert status.lookback_days is None
        assert status.write_disposition == "replace"


class TestSourceEdit:
    """Test dango source edit command."""

    def test_no_project_root(self) -> None:
        runner = CliRunner()
        result = runner.invoke(source_edit, obj={"project_root": None})
        assert result.exit_code == 0
        assert "Not in a dango project" in result.output

    def test_no_sources_file(self, tmp_path: pytest.TempPathFactory) -> None:
        runner = CliRunner()
        result = runner.invoke(source_edit, obj={"project_root": str(tmp_path)})
        assert result.exit_code == 0
        assert "No sources.yml found" in result.output

    def test_no_editor_shows_path(self, tmp_path: pytest.TempPathFactory) -> None:
        """Without $EDITOR or TTY, shows file path instead of opening editor."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        sources_file = dango_dir / "sources.yml"
        sources_file.write_text("sources:\n  - name: test\n")

        runner = CliRunner()
        result = runner.invoke(source_edit, obj={"project_root": str(tmp_path)})
        assert result.exit_code == 0
        assert "sources.yml" in result.output

    def test_editor_returns_none(self, tmp_path: pytest.TempPathFactory) -> None:
        """Editor returns None when user doesn't save."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        sources_file = dango_dir / "sources.yml"
        sources_file.write_text("sources:\n  - name: test\n")

        runner = CliRunner()
        with (
            patch("click.edit", return_value=None),
            patch.dict("os.environ", {"EDITOR": "vim"}),
        ):
            result = runner.invoke(source_edit, obj={"project_root": str(tmp_path)})
        assert result.exit_code == 0

    def test_no_changes(self, tmp_path: pytest.TempPathFactory) -> None:
        """Editor returns same content."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        sources_file = dango_dir / "sources.yml"
        original = "sources:\n  - name: test\n"
        sources_file.write_text(original)

        runner = CliRunner()
        with (
            patch("click.edit", return_value=original),
            patch.dict("os.environ", {"EDITOR": "vim"}),
        ):
            result = runner.invoke(source_edit, obj={"project_root": str(tmp_path)})
        assert result.exit_code == 0
        assert "No changes detected" in result.output

    def test_valid_edit_saves(self, tmp_path: pytest.TempPathFactory) -> None:
        """Valid YAML edit is saved."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        sources_file = dango_dir / "sources.yml"
        original = "sources:\n  - name: test\n"
        edited = "sources:\n  - name: test\n    enabled: false\n"
        sources_file.write_text(original)

        runner = CliRunner()
        with (
            patch("click.edit", return_value=edited),
            patch.dict("os.environ", {"EDITOR": "vim"}),
        ):
            result = runner.invoke(source_edit, obj={"project_root": str(tmp_path)})
        assert result.exit_code == 0
        assert "sources.yml updated" in result.output
        assert sources_file.read_text() == edited

    def test_invalid_yaml_rejected(self, tmp_path: pytest.TempPathFactory) -> None:
        """Invalid YAML is rejected and file unchanged."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        sources_file = dango_dir / "sources.yml"
        original = "sources:\n  - name: test\n"
        invalid = "sources:\n  - name: test\n  bad: [unclosed\n"
        sources_file.write_text(original)

        runner = CliRunner()
        with (
            patch("click.edit", return_value=invalid),
            patch.dict("os.environ", {"EDITOR": "vim"}),
        ):
            result = runner.invoke(source_edit, obj={"project_root": str(tmp_path)})
        assert result.exit_code == 0
        assert "Invalid YAML" in result.output
        assert "Changes NOT saved" in result.output
        assert sources_file.read_text() == original
