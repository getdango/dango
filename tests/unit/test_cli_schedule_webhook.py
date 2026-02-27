"""tests/unit/test_cli_schedule_webhook.py

Unit tests for ``dango schedule webhook`` CLI commands.
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


def _setup_project(tmp_path: Path) -> Path:
    """Create minimal project structure for tests."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    project_yml = dango_dir / "project.yml"
    project_yml.write_text("project:\n  name: test\n  version: '1.0'\n")
    return tmp_path


@pytest.mark.unit
class TestWebhookList:
    """Tests for ``dango schedule webhook list``."""

    @patch("dango.cli.utils.find_project_root")
    def test_empty_webhooks(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {})
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "list"])
        plain = _strip_ansi(result.output)
        assert "No webhooks configured" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_list_with_webhooks(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "notifications": {
                    "webhooks": [
                        {
                            "name": "slack_alerts",
                            "url": "https://hooks.slack.com/test",
                            "format": "slack",
                        }
                    ]
                }
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "list"])
        plain = _strip_ansi(result.output)
        assert "slack_alerts" in plain
        assert "https://hooks.slack.com/test" in plain


@pytest.mark.unit
class TestWebhookAdd:
    """Tests for ``dango schedule webhook add``."""

    @patch("inquirer.prompt")
    @patch("dango.cli.utils.find_project_root")
    def test_add_webhook(
        self, mock_root: MagicMock, mock_prompt: MagicMock, tmp_path: Path
    ) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {})

        mock_prompt.return_value = {
            "name": "my_hook",
            "url": "https://example.com/hook",
            "format": "generic",
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "add"])
        plain = _strip_ansi(result.output)
        assert "added" in plain

        data = yaml.safe_load((project_root / ".dango" / "schedules.yml").read_text())
        webhooks = data["notifications"]["webhooks"]
        assert len(webhooks) == 1
        assert webhooks[0]["name"] == "my_hook"


@pytest.mark.unit
class TestWebhookRemove:
    """Tests for ``dango schedule webhook remove``."""

    @patch("dango.cli.utils.find_project_root")
    def test_remove_nonexistent(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"notifications": {"webhooks": []}})
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "remove", "nope"])
        plain = _strip_ansi(result.output)
        assert "not found" in plain

    @patch("dango.cli.utils.find_project_root")
    def test_remove_with_yes(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "notifications": {
                    "webhooks": [{"name": "to_delete", "url": "https://example.com/hook"}]
                }
            },
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "remove", "to_delete", "--yes"])
        plain = _strip_ansi(result.output)
        assert "removed" in plain

        data = yaml.safe_load((project_root / ".dango" / "schedules.yml").read_text())
        assert len(data["notifications"]["webhooks"]) == 0


@pytest.mark.unit
class TestWebhookTest:
    """Tests for ``dango schedule webhook test``."""

    @patch("dango.cli.utils.find_project_root")
    def test_test_nonexistent(self, mock_root: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(project_root, {"notifications": {"webhooks": []}})
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "test", "nope"])
        plain = _strip_ansi(result.output)
        assert "not found" in plain

    @patch("httpx.post")
    @patch("dango.cli.utils.find_project_root")
    def test_test_success(self, mock_root: MagicMock, mock_post: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "notifications": {
                    "webhooks": [{"name": "my_hook", "url": "https://example.com/hook"}]
                }
            },
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "test", "my_hook"])
        plain = _strip_ansi(result.output)
        assert "Success" in plain

    @patch("httpx.post")
    @patch("dango.cli.utils.find_project_root")
    def test_test_failure(self, mock_root: MagicMock, mock_post: MagicMock, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        _write_schedules_yaml(
            project_root,
            {
                "notifications": {
                    "webhooks": [{"name": "my_hook", "url": "https://example.com/hook"}]
                }
            },
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "webhook", "test", "my_hook"])
        plain = _strip_ansi(result.output)
        assert "Failed" in plain
