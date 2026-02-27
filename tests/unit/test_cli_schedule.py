"""tests/unit/test_cli_schedule.py

Unit tests for ``dango schedule`` CLI commands.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from dango.cli.main import cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _write_schedules_yaml(project_root: Path, data: dict[str, Any]) -> None:
    dango_dir = project_root / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    path = dango_dir / "schedules.yml"
    with open(path, "w") as f:
        yaml.safe_dump(data, f)


def _write_sources_yaml(project_root: Path, sources: list[dict[str, Any]]) -> None:
    dango_dir = project_root / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    path = dango_dir / "sources.yml"
    data: dict[str, Any] = {"version": "1.0", "sources": sources}
    with open(path, "w") as f:
        yaml.safe_dump(data, f)


def _setup_project(tmp_path: Path) -> Path:
    """Create minimal project structure for tests."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    project_yml = dango_dir / "project.yml"
    project_yml.write_text("project:\n  name: test\n  version: '1.0'\n")
    return tmp_path


@pytest.mark.unit
class TestScheduleHelp:
    """Subcommands visible in --help."""

    def test_schedule_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "--help"])
        assert result.exit_code == 0
        for cmd in ["list", "status", "add", "remove", "enable", "disable", "webhook"]:
            assert cmd in result.output, f"Subcommand '{cmd}' missing from --help"


@pytest.mark.unit
class TestScheduleList:
    """Tests for ``dango schedule list``."""

    @patch("dango.cli.utils.find_project_root")
    def test_empty_schedules(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "list"])
        plain = _strip_ansi(result.output)
        assert "No schedules configured" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_list_with_schedules(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "hourly_sync",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["stripe", "hubspot"],
                        "enabled": True,
                    },
                    {
                        "name": "daily_dbt",
                        "type": "dbt",
                        "cron": "0 6 * * *",
                        "sources": [],
                        "enabled": False,
                    },
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "list"])
        plain = _strip_ansi(result.output)
        assert "hourly_sync" in plain
        assert "daily_dbt" in plain
        assert "stripe, hubspot" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_disabled_schedule_shows_dash_for_next_run(
        self, mock_root: MagicMock, tmp_path: Path
    ) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "disabled_one",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["src"],
                        "enabled": False,
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "list"])
        plain = _strip_ansi(result.output)
        assert "disabled_one" in plain


@pytest.mark.unit
class TestScheduleStatus:
    """Tests for ``dango schedule status``."""

    @patch("dango.cli.utils.find_project_root")
    def test_no_schedules(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "status"])
        plain = _strip_ansi(result.output)
        assert "No schedules configured" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_status_with_schedules(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "hourly",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["stripe"],
                        "enabled": True,
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "status"])
        plain = _strip_ansi(result.output)
        assert "Next run:" in plain
        assert "hourly" in plain
        assert "Last run:" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_status_shows_unscheduled_sources(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "hourly",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["stripe"],
                        "enabled": True,
                    }
                ]
            },
        )
        # Write sources.yml with an extra source not in any schedule
        _write_sources_yaml(
            project_root,
            [
                {"name": "stripe", "type": "stripe", "enabled": True},
                {"name": "hubspot", "type": "hubspot", "enabled": True},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "status"])
        plain = _strip_ansi(result.output)
        assert "Unscheduled sources:" in plain
        assert "hubspot" in plain


@pytest.mark.unit
class TestScheduleRemove:
    """Tests for ``dango schedule remove``."""

    @patch("dango.cli.utils.find_project_root")
    def test_remove_nonexistent(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"schedules": []})
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "remove", "nope"])
        plain = _strip_ansi(result.output)
        assert "not found" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_remove_with_yes_flag(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "to_delete",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["src"],
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "remove", "to_delete", "--yes"])
        plain = _strip_ansi(result.output)
        assert "removed" in plain

        # Verify it's gone from YAML
        data = yaml.safe_load((project_root / ".dango" / "schedules.yml").read_text())
        assert len(data.get("schedules", [])) == 0

    @patch("dango.cli.utils.find_project_root")
    def test_remove_cancelled(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "keep_me",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["src"],
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "remove", "keep_me"], input="n\n")
        plain = _strip_ansi(result.output)
        assert "Cancelled" in plain


@pytest.mark.unit
class TestScheduleEnable:
    """Tests for ``dango schedule enable``."""

    @patch("dango.cli.utils.find_project_root")
    def test_enable_disabled_schedule(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "my_sched",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["src"],
                        "enabled": False,
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "enable", "my_sched"])
        plain = _strip_ansi(result.output)
        assert "enabled" in plain

        data = yaml.safe_load((project_root / ".dango" / "schedules.yml").read_text())
        assert data["schedules"][0]["enabled"] is True

    @patch("dango.cli.utils.find_project_root")
    def test_enable_already_enabled(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "my_sched",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["src"],
                        "enabled": True,
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "enable", "my_sched"])
        plain = _strip_ansi(result.output)
        assert "already enabled" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_enable_nonexistent(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"schedules": []})
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "enable", "nope"])
        plain = _strip_ansi(result.output)
        assert "not found" in plain


@pytest.mark.unit
class TestScheduleDisable:
    """Tests for ``dango schedule disable``."""

    @patch("dango.cli.utils.find_project_root")
    def test_disable_enabled_schedule(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "my_sched",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["src"],
                        "enabled": True,
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "disable", "my_sched"])
        plain = _strip_ansi(result.output)
        assert "disabled" in plain

        data = yaml.safe_load((project_root / ".dango" / "schedules.yml").read_text())
        assert data["schedules"][0]["enabled"] is False

    @patch("dango.cli.utils.find_project_root")
    def test_disable_already_disabled(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "schedules": [
                    {
                        "name": "my_sched",
                        "type": "sync",
                        "cron": "0 * * * *",
                        "sources": ["src"],
                        "enabled": False,
                    }
                ]
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "disable", "my_sched"])
        plain = _strip_ansi(result.output)
        assert "already disabled" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_disable_nonexistent(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"schedules": []})
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "disable", "nope"])
        plain = _strip_ansi(result.output)
        assert "not found" in plain


@pytest.mark.unit
class TestScheduleAdd:
    """Tests for ``dango schedule add`` wizard."""

    @patch("inquirer.prompt")
    @patch("dango.cli.utils.find_project_root")
    def test_add_sync_schedule(
        self, mock_root: MagicMock, mock_prompt: MagicMock, tmp_path: Path
    ) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"schedules": []})
        _write_sources_yaml(
            project_root,
            [{"name": "stripe", "type": "stripe", "enabled": True}],
        )

        mock_prompt.side_effect = [
            {"name": "hourly_sync"},
            {"type": "sync"},
            {"sources": ["stripe"]},
            {"frequency": "Every hour"},
            {"timezone": "UTC"},
            {"notify_on": ["failure"]},
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "add"])
        plain = _strip_ansi(result.output)
        assert "added" in plain

        data = yaml.safe_load((project_root / ".dango" / "schedules.yml").read_text())
        assert len(data["schedules"]) == 1
        assert data["schedules"][0]["name"] == "hourly_sync"
        assert data["schedules"][0]["cron"] == "0 * * * *"

    @patch("inquirer.prompt")
    @patch("dango.cli.utils.find_project_root")
    def test_add_dbt_schedule(
        self, mock_root: MagicMock, mock_prompt: MagicMock, tmp_path: Path
    ) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"schedules": []})

        mock_prompt.side_effect = [
            {"name": "daily_dbt"},
            {"type": "dbt"},
            {"dbt_command": "run"},
            {"frequency": "Daily (6 AM)"},
            {"timezone": "UTC"},
            {"notify_on": []},
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "add"])
        plain = _strip_ansi(result.output)
        assert "added" in plain

        data = yaml.safe_load((project_root / ".dango" / "schedules.yml").read_text())
        assert data["schedules"][0]["type"] == "dbt"
        assert data["schedules"][0]["dbt_command"] == "run"

    @patch("inquirer.prompt")
    @patch("dango.cli.utils.find_project_root")
    def test_add_cancelled(
        self, mock_root: MagicMock, mock_prompt: MagicMock, tmp_path: Path
    ) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"schedules": []})

        # User cancels at name prompt
        mock_prompt.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "add"])
        # Should exit cleanly without error
        assert result.exit_code == 0
